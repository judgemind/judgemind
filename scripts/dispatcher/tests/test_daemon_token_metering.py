"""Unit tests for per-phase token + cost metering (#2869).

Covers the full read/write loop for the ``dispatcher.phase_outputs``
metering columns introduced by migration 31:

* :meth:`daemon.DispatcherDaemon._spawn_phase_subprocess` — adds the
  ``--output-format json`` flag, splits stdout / stderr to their own
  files, and composes the legacy combined ``.log`` triage view.
* :meth:`daemon.DispatcherDaemon._parse_phase_usage` — reads the
  stdout JSON envelope and normalizes the usage block.
* :meth:`daemon.DispatcherDaemon._persist_phase_output` — threads the
  parsed usage dict into the six new DB columns (``tokens_input``,
  ``tokens_output``, ``tokens_cache_read``, ``tokens_cache_write``,
  ``cost_usd``, ``model_used``), and keeps every column ``NULL`` when
  ``usage=None`` (the "no metering signal" case).

Same mocking strategy as :mod:`test_daemon_phase3a` — ``psycopg`` is
stubbed before the daemon import so the test runner never needs a real
DB or the ``psycopg`` wheel. No ``claude`` / ``gh`` / ``git``
subprocesses run.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above
from dispatcher.tests._popen_fake import make_popen_factory  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes — mirror test_daemon_phase3a's lightweight DB doubles.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self.fetch_queue:
            return None
        return self.fetch_queue.pop(0)


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _CapturingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.metering.{id(tmp_path)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# _spawn_phase_subprocess — new flag + split stdout/stderr files
# --------------------------------------------------------------------------


class TestSpawnAddsOutputFormatJsonFlag:
    """``_spawn_phase_subprocess`` must add ``--output-format json`` so the
    stdout stream is a parseable JSON envelope. Without the flag the usage
    block is not emitted and metering is silent (#2869)."""

    def test_command_includes_output_format_json(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], _kwargs: dict[str, Any]) -> None:
            captured["cmd"] = cmd

        monkeypatch.setattr(subprocess, "Popen", make_popen_factory(on_start=on_start))
        d._spawn_phase_subprocess("plan", tmp_path, "agent-uuid")

        cmd = captured["cmd"]
        assert "--output-format" in cmd, cmd
        # "json" must follow the flag, not some other token.
        i = cmd.index("--output-format")
        assert cmd[i + 1] == "json"

    def test_stdout_and_stderr_written_to_separate_files(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``--output-format json`` would corrupt the JSON envelope if stderr
        merged into stdout. The spawn helper must split the streams so
        :meth:`_parse_phase_usage` can ``json.loads`` the stdout file
        directly.

        Post-#3017: the spawn helper uses :class:`subprocess.Popen` with
        a stream-forwarder that tees each line to the per-stream sink
        files — same two output files, same split content, different
        plumbing.
        """
        d, _conn, _handler = _make_daemon(tmp_path)

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(
                stdout_chunks=['{"usage":{"input_tokens":1}}\n'],
                stderr_chunks=["boot-log line\n"],
            ),
        )
        d._spawn_phase_subprocess("plan", tmp_path, "agent-uuid")

        stdout_path = tmp_path / "tmp" / "claude-p-plan.stdout.json"
        stderr_path = tmp_path / "tmp" / "claude-p-plan.stderr.log"
        assert stdout_path.exists()
        assert stderr_path.exists()
        assert '{"usage":{"input_tokens":1}}' in stdout_path.read_text()
        assert "boot-log line" in stderr_path.read_text()

    def test_combined_log_file_still_written_for_backward_compat(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Pre-#2869 triage helpers (``_log_tail``, ``_read_full_phase_log``,
        ``_emit_phase_failure_log_event``) key on
        ``claude-p-<phase>.log``. The combined file must still be written so
        those helpers keep working unchanged, and operators still have a
        single ``cat``-able triage target."""
        d, _conn, _handler = _make_daemon(tmp_path)

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(
                stdout_chunks=['{"result":"ok"}\n'],
                stderr_chunks=["stderr content\n"],
            ),
        )
        d._spawn_phase_subprocess("plan", tmp_path, "agent-uuid")

        combined = tmp_path / "tmp" / "claude-p-plan.log"
        assert combined.exists()
        body = combined.read_text()
        # Both streams present, with a visible separator.
        assert "stderr content" in body
        assert '{"result":"ok"}' in body
        assert "=== stderr ===" in body
        assert "=== stdout (JSON envelope) ===" in body

    def test_combined_log_written_even_on_nonzero_exit(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Triage matters most when the subprocess failed. The combined log
        composition runs in a ``finally`` block so it is produced even when
        claude -p exits non-zero — operators still see whatever partial
        output the streams captured before the fault."""
        d, _conn, _handler = _make_daemon(tmp_path)

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(
                stderr_chunks=["crash-trace here\n"],
                returncode=1,
            ),
        )
        exit_code, _duration = d._spawn_phase_subprocess("plan", tmp_path, "agent-uuid")
        assert exit_code == 1
        combined = tmp_path / "tmp" / "claude-p-plan.log"
        assert combined.exists()
        assert "crash-trace here" in combined.read_text()


# --------------------------------------------------------------------------
# _parse_phase_usage
# --------------------------------------------------------------------------


class TestParsePhaseUsage:
    """``_parse_phase_usage`` reads the stdout JSON envelope and returns a
    normalized ``{tokens_input, tokens_output, tokens_cache_read,
    tokens_cache_write, cost_usd, model_used}`` dict — or ``None`` when any
    of the graceful-degradation conditions fires."""

    def _write_stdout(self, tmp_path: Path, phase: str, payload: str) -> None:
        path = tmp_path / "tmp" / f"claude-p-{phase}.stdout.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)

    def test_parses_full_usage_envelope(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        envelope = {
            "result": "...",
            "usage": {
                "input_tokens": 1234,
                "output_tokens": 567,
                "cache_creation_input_tokens": 8901,
                "cache_read_input_tokens": 23456,
            },
            "total_cost_usd": 0.1234,
            "model": "claude-sonnet-4-5-20250929",
        }
        self._write_stdout(tmp_path, "plan", json.dumps(envelope))

        usage = d._parse_phase_usage(tmp_path, "plan")
        assert usage is not None
        assert usage["tokens_input"] == 1234
        assert usage["tokens_output"] == 567
        assert usage["tokens_cache_write"] == 8901
        assert usage["tokens_cache_read"] == 23456
        assert usage["cost_usd"] == 0.1234
        assert usage["model_used"] == "claude-sonnet-4-5-20250929"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._parse_phase_usage(tmp_path, "plan") is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        self._write_stdout(tmp_path, "plan", "")
        assert d._parse_phase_usage(tmp_path, "plan") is None

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        self._write_stdout(tmp_path, "plan", "not json at all")
        assert d._parse_phase_usage(tmp_path, "plan") is None

    def test_envelope_without_usage_block_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        self._write_stdout(tmp_path, "plan", json.dumps({"result": "hi"}))
        assert d._parse_phase_usage(tmp_path, "plan") is None

    def test_missing_individual_fields_produce_none_values(
        self, tmp_path: Path
    ) -> None:
        """A partial usage block (e.g. only input_tokens present) should
        still surface — the columns are independently nullable so a
        partial signal is better than no signal at all."""
        d, _conn, _handler = _make_daemon(tmp_path)
        envelope = {
            "usage": {"input_tokens": 100},
            "total_cost_usd": None,
        }
        self._write_stdout(tmp_path, "plan", json.dumps(envelope))

        usage = d._parse_phase_usage(tmp_path, "plan")
        assert usage is not None
        assert usage["tokens_input"] == 100
        assert usage["tokens_output"] is None
        assert usage["tokens_cache_read"] is None
        assert usage["tokens_cache_write"] is None
        assert usage["cost_usd"] is None
        # Model falls back to PHASE_MODELS default.
        assert usage["model_used"] == daemon.PHASE_MODELS["plan"]

    def test_falls_back_to_phase_model_when_envelope_omits_it(
        self, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        envelope = {"usage": {"input_tokens": 1}, "total_cost_usd": 0.01}
        self._write_stdout(tmp_path, "ralph", json.dumps(envelope))

        usage = d._parse_phase_usage(tmp_path, "ralph")
        assert usage is not None
        assert usage["model_used"] == daemon.PHASE_MODELS["ralph"]

    def test_non_dict_envelope_returns_none(self, tmp_path: Path) -> None:
        """A top-level JSON array or string is not a usage envelope."""
        d, _conn, _handler = _make_daemon(tmp_path)
        self._write_stdout(tmp_path, "plan", '["not", "an", "envelope"]')
        assert d._parse_phase_usage(tmp_path, "plan") is None

    def test_non_numeric_token_values_coerce_to_none(self, tmp_path: Path) -> None:
        """The helper must not crash on a string value — fall back to
        None so the column stays NULL and the INSERT still runs."""
        d, _conn, _handler = _make_daemon(tmp_path)
        envelope = {
            "usage": {
                "input_tokens": "not an int",
                "output_tokens": 42,
            },
            "total_cost_usd": "nope",
        }
        self._write_stdout(tmp_path, "plan", json.dumps(envelope))

        usage = d._parse_phase_usage(tmp_path, "plan")
        assert usage is not None
        assert usage["tokens_input"] is None
        assert usage["tokens_output"] == 42
        assert usage["cost_usd"] is None


# --------------------------------------------------------------------------
# _persist_phase_output — threads usage into the INSERT
# --------------------------------------------------------------------------


class TestPersistPhaseOutputWritesUsage:
    def test_usage_dict_lands_in_insert_params(self, tmp_path: Path) -> None:
        """Every metering column must be written in the correct position.
        The INSERT binds parameters in the order
        ``(agent_id, phase, output_json, log_text, attempt,
           tokens_input, tokens_output, tokens_cache_read,
           tokens_cache_write, cost_usd, model_used)``."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Migration-30 contract: persist reads retries_used first.
        conn.cursor_instance.fetch_queue = [(0,)]

        usage = {
            "tokens_input": 100,
            "tokens_output": 50,
            "tokens_cache_read": 200,
            "tokens_cache_write": 300,
            "cost_usd": 0.0042,
            "model_used": "claude-sonnet-4-5",
        }
        d._persist_phase_output(
            "agent-a", "plan", {"go": True}, log_text="log-body", usage=usage
        )

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert len(inserts) == 1
        sql, params = inserts[0]
        # 11 bind params: 5 original + 6 metering.
        assert len(params) == 11
        # Token metering columns (positions 5-8 are tokens_* in our binding
        # order, 9 is cost_usd, 10 is model_used).
        assert params[5] == 100  # tokens_input
        assert params[6] == 50  # tokens_output
        assert params[7] == 200  # tokens_cache_read
        assert params[8] == 300  # tokens_cache_write
        assert params[9] == 0.0042  # cost_usd
        assert params[10] == "claude-sonnet-4-5"  # model_used
        # The SQL statement references every new column name — guards
        # against a position-only pass with a stale SQL string.
        assert "tokens_input" in sql
        assert "tokens_output" in sql
        assert "tokens_cache_read" in sql
        assert "tokens_cache_write" in sql
        assert "cost_usd" in sql
        assert "model_used" in sql

    def test_no_usage_signal_writes_nulls_but_still_inserts(
        self, tmp_path: Path
    ) -> None:
        """The ``usage=None`` path is the "no metering signal" case — the
        row must still insert, with every new column as ``NULL``. Without
        this, a malformed JSON envelope from claude would silently drop
        the whole phase output (same failure mode migration 30 fixed for
        attempt collisions)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        d._persist_phase_output(
            "agent-a", "plan", {"go": True}, log_text="log", usage=None
        )

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert len(inserts) == 1
        _sql, params = inserts[0]
        # tokens_input..model_used all None.
        for idx in range(5, 11):
            assert params[idx] is None, f"param idx={idx} should be None"

    def test_backward_compat_call_without_usage_kwarg(self, tmp_path: Path) -> None:
        """Callers that predate #2869 (don't pass ``usage=``) must keep
        working — the new columns default to NULL."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        d._persist_phase_output("agent-a", "plan", {"go": True})

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert len(inserts) == 1
        _sql, params = inserts[0]
        for idx in range(5, 11):
            assert params[idx] is None
