"""Unit tests for diagnoser token + cost metering (#2881).

Covers the diagnoser-specific metering path introduced in issue #2881:

* :meth:`daemon.DispatcherDaemon._spawn_diagnoser_subprocess` — adds
  ``--output-format json`` and writes stdout to
  ``{repo_root}/tmp/claude-p-diagnose-<id>.stdout.json``.
* :meth:`daemon.DispatcherDaemon._parse_diagnoser_usage` — thin wrapper
  around ``_parse_usage_envelope`` that computes the diagnoser stdout
  path and supplies ``DIAGNOSER_MODEL`` as the fallback model.
* :meth:`daemon.DispatcherDaemon._persist_diagnoser_phase_output` —
  inserts one ``dispatcher.phase_outputs`` row with ``phase='diagnose'``
  and the six metering columns.
* :meth:`daemon.DispatcherDaemon._run_diagnoser_pass` — parses usage and
  persists on every exit branch (timeout, non-zero, exit 0).

Same mocking strategy as :mod:`test_daemon_token_metering` — ``psycopg``
is stubbed before the daemon import so the test runner never needs a real
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
# Shared fakes — mirror test_daemon_token_metering's lightweight DB doubles.
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
    logger = logging.getLogger(f"dispatcher.test.diagnoser_metering.{id(tmp_path)}")
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
        baseline_repo_root=tmp_path,
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# _spawn_diagnoser_subprocess — --output-format json flag
# --------------------------------------------------------------------------


class TestSpawnDiagnoserAddsOutputFormatJsonFlag:
    """``_spawn_diagnoser_subprocess`` must include ``--output-format json``
    so the stdout stream is a parseable JSON envelope for metering."""

    def test_command_includes_output_format_json(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], _kwargs: dict[str, Any]) -> None:
            captured["cmd"] = cmd

        monkeypatch.setattr(subprocess, "Popen", make_popen_factory(on_start=on_start))
        d._spawn_diagnoser_subprocess(42)

        cmd = captured["cmd"]
        assert "--output-format" in cmd, cmd
        i = cmd.index("--output-format")
        assert cmd[i + 1] == "json"


# --------------------------------------------------------------------------
# _spawn_diagnoser_subprocess — stdout file written to repo_root/tmp/
# --------------------------------------------------------------------------


class TestSpawnDiagnoserWritesStdoutFile:
    """``_spawn_diagnoser_subprocess`` must open the diagnoser stdout
    file at ``{repo_root}/tmp/claude-p-diagnose-<id>.stdout.json`` so
    the (real) child writes its JSON envelope there.

    Issue #3376: under async fire-and-forget, the file FD is opened in
    the parent and dup'd to the child via Popen ``stdout=``. The
    parent's copy is closed immediately after Popen returns; the child
    writes directly. Tests use an in-memory ``StringIO`` fake that
    doesn't actually receive the dup, so we can only assert that the
    file was created (not its content) at the unit-test level. The
    metering parser tests (``TestParseDiagnoserUsage``) cover the
    content-shape side of the contract.
    """

    def test_stdout_file_created_in_repo_root_tmp(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        captured: dict[str, Any] = {}

        def on_start(_cmd: list[str], kwargs: dict[str, Any]) -> None:
            captured["kwargs"] = kwargs

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(on_start=on_start),
        )
        d._spawn_diagnoser_subprocess(99)

        # The file is opened (so the FD exists for Popen to dup) and
        # the parent then closes its copy. The empty file lands at the
        # canonical path.
        stdout_path = tmp_path / "tmp" / "claude-p-diagnose-99.stdout.json"
        assert stdout_path.exists(), f"Expected stdout file at {stdout_path}"
        # Popen received a real file handle (not subprocess.PIPE) for
        # stdout. This is the structural guarantee that the child can
        # write to the file directly without parent threads.
        assert "stdout" in captured["kwargs"]
        # IO objects don't compare as ints; just verify it isn't PIPE.
        assert captured["kwargs"]["stdout"] != subprocess.PIPE


# --------------------------------------------------------------------------
# _parse_diagnoser_usage
# --------------------------------------------------------------------------


class TestParseDiagnoserUsage:
    """``_parse_diagnoser_usage`` is a thin wrapper around
    ``_parse_usage_envelope`` that resolves the diagnoser stdout path and
    supplies ``DIAGNOSER_MODEL`` as the model fallback."""

    def _write_stdout(self, tmp_path: Path, diagnosis_id: int, payload: str) -> None:
        path = tmp_path / "tmp" / f"claude-p-diagnose-{diagnosis_id}.stdout.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

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
            "total_cost_usd": 0.5678,
            "model": "claude-opus-4-5-20250929",
        }
        self._write_stdout(tmp_path, 7, json.dumps(envelope))

        usage = d._parse_diagnoser_usage(7)
        assert usage is not None
        assert usage["tokens_input"] == 1234
        assert usage["tokens_output"] == 567
        assert usage["tokens_cache_write"] == 8901
        assert usage["tokens_cache_read"] == 23456
        assert usage["cost_usd"] == 0.5678
        assert usage["model_used"] == "claude-opus-4-5-20250929"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._parse_diagnoser_usage(999) is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        self._write_stdout(tmp_path, 1, "")
        assert d._parse_diagnoser_usage(1) is None

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        self._write_stdout(tmp_path, 2, "not json at all")
        assert d._parse_diagnoser_usage(2) is None

    def test_envelope_without_usage_block_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        self._write_stdout(tmp_path, 3, json.dumps({"result": "hi"}))
        assert d._parse_diagnoser_usage(3) is None

    def test_partial_usage_falls_back_to_diagnoser_model(self, tmp_path: Path) -> None:
        """A partial usage block (model absent) falls back to DIAGNOSER_MODEL."""
        d, _conn, _handler = _make_daemon(tmp_path)
        envelope = {
            "usage": {"input_tokens": 100},
            "total_cost_usd": None,
        }
        self._write_stdout(tmp_path, 4, json.dumps(envelope))

        usage = d._parse_diagnoser_usage(4)
        assert usage is not None
        assert usage["tokens_input"] == 100
        assert usage["tokens_output"] is None
        assert usage["tokens_cache_read"] is None
        assert usage["tokens_cache_write"] is None
        assert usage["cost_usd"] is None
        # Model falls back to DIAGNOSER_MODEL.
        assert usage["model_used"] == daemon.DIAGNOSER_MODEL


# --------------------------------------------------------------------------
# _persist_diagnoser_phase_output
# --------------------------------------------------------------------------


class TestPersistDiagnoserPhaseOutput:
    """``_persist_diagnoser_phase_output`` inserts into
    ``dispatcher.phase_outputs`` with ``phase='diagnose'``."""

    def test_writes_phase_outputs_row_with_phase_diagnose(self, tmp_path: Path) -> None:
        """11-param INSERT with phase='diagnose', agent_id bound to the
        failing-agent UUID, and all six metering columns populated."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Migration-30 contract: _current_attempt_for reads retries_used first.
        conn.cursor_instance.fetch_queue = [(0,)]

        usage = {
            "tokens_input": 1000,
            "tokens_output": 500,
            "tokens_cache_read": 2000,
            "tokens_cache_write": 3000,
            "cost_usd": 0.0742,
            "model_used": "claude-opus-4-5",
        }
        d._persist_diagnoser_phase_output(
            agent_id="agent-failing-uuid",
            diagnosis_id=77,
            usage=usage,
            log_text="stderr tail here",
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
        assert params[0] == "agent-failing-uuid"  # agent_id
        assert params[1] == "diagnose"  # phase constant
        # output_json contains the diagnosis_id for join-back
        output = json.loads(params[2])
        assert output["diagnosis_id"] == 77
        assert params[3] == "stderr tail here"  # log_text
        assert params[5] == 1000  # tokens_input
        assert params[6] == 500  # tokens_output
        assert params[7] == 2000  # tokens_cache_read
        assert params[8] == 3000  # tokens_cache_write
        assert params[9] == 0.0742  # cost_usd
        assert params[10] == "claude-opus-4-5"  # model_used
        # SQL references every metering column name.
        assert "tokens_input" in sql
        assert "tokens_output" in sql
        assert "tokens_cache_read" in sql
        assert "tokens_cache_write" in sql
        assert "cost_usd" in sql
        assert "model_used" in sql

    def test_no_usage_signal_still_inserts_with_nulls(self, tmp_path: Path) -> None:
        """``usage=None`` path must still insert — metering columns become NULL."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        d._persist_diagnoser_phase_output(
            agent_id="agent-no-signal",
            diagnosis_id=88,
            usage=None,
            log_text=None,
        )

        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert len(inserts) == 1
        _sql, params = inserts[0]
        # All six metering columns must be None.
        for idx in range(5, 11):
            assert params[idx] is None, f"param idx={idx} should be None"

    def test_does_not_write_phase_transitions_row(self, tmp_path: Path) -> None:
        """The diagnoser is not an orchestration phase — no
        ``dispatcher.phase_transitions`` row should be inserted."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        d._persist_diagnoser_phase_output(
            agent_id="agent-transitions-check",
            diagnosis_id=55,
            usage=None,
            log_text=None,
        )

        all_sql = [e[0] for e in conn.cursor_instance.executed]
        transitions_inserts = [
            s for s in all_sql if "INSERT INTO dispatcher.phase_transitions" in s
        ]
        assert transitions_inserts == [], (
            "No phase_transitions INSERT expected for diagnose phase, "
            f"but found: {transitions_inserts}"
        )
        # But the phase_outputs INSERT must have happened.
        phase_output_inserts = [
            s for s in all_sql if "INSERT INTO dispatcher.phase_outputs" in s
        ]
        assert len(phase_output_inserts) == 1


# --------------------------------------------------------------------------
# _run_diagnoser_pass — metering persisted on every exit branch
# --------------------------------------------------------------------------


_FAKE_CANDIDATE = {
    "failure_id": 101,
    "agent_id": "agent-failing-for-diagnoser",
    "category": "subprocess_crash",
    "tier": 2,
}


def _setup_run_diagnoser_pass(
    d: daemon.DispatcherDaemon,
    conn: _FakeConnection,
    diagnosis_id: int = 42,
) -> None:
    """Wire the daemon so ``_run_diagnoser_pass`` can run a single candidate."""
    # diagnoser_enabled: reads one config row
    # _find_diagnoser_candidates: returns our fake candidate
    # _build_diagnoser_context: returns empty dict
    # _insert_pending_diagnosis: returns our diagnosis_id
    # _current_attempt_for (inside _persist_diagnoser_phase_output):
    #   reads retries_used from agents table
    # _mark_diagnosis_failed / _apply_mechanical_escalation need conn too
    pass  # monkeypatching done inline in each test


class TestReapPersistMeteringOnTimeout:
    """Issue #3376 — under async fire-and-forget spawn, metering happens
    in the reaper (``_reap_persist_metering``), not the spawn pass.
    When the reaper SIGTERM/SIGKILLs a timed-out diagnoser, the persist
    path must still fire so opus-cost-vs-savings is recorded."""

    def test_persist_called_on_reaper_timeout(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        # Pre-write a stdout file so _parse_diagnoser_usage can succeed.
        stdout_path = tmp_path / "tmp" / "claude-p-diagnose-42.stdout.json"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            json.dumps({"usage": {"input_tokens": 50}, "total_cost_usd": 0.005}),
            encoding="utf-8",
        )

        persisted: list[dict[str, Any]] = []

        def fake_persist(
            agent_id: str,
            diagnosis_id: int,
            usage: dict[str, Any] | None,
            log_text: str | None,
        ) -> None:
            persisted.append(
                {
                    "agent_id": agent_id,
                    "diagnosis_id": diagnosis_id,
                    "usage": usage,
                }
            )

        monkeypatch.setattr(d, "_persist_diagnoser_phase_output", fake_persist)

        row = {
            "diagnosis_id": 42,
            "subprocess_pid": 1234,
            "agent_id": _FAKE_CANDIDATE["agent_id"],
            "category": _FAKE_CANDIDATE["category"],
            "issue_number": 999,
            "details": {},
            "failure_id": _FAKE_CANDIDATE["failure_id"],
        }
        d._reap_persist_metering(row)

        assert len(persisted) == 1, "persist must fire even on reaper timeout"
        assert persisted[0]["diagnosis_id"] == 42
        assert persisted[0]["agent_id"] == _FAKE_CANDIDATE["agent_id"]


class TestReapPersistMeteringOnExit:
    """Issue #3376 — when the reaper observes an exited subprocess
    (process gone, recommendation read), metering also persists."""

    def test_persist_called_on_reaper_exit(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        # Pre-write a stdout file (diagnoser emitted partial JSON).
        stdout_path = tmp_path / "tmp" / "claude-p-diagnose-43.stdout.json"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            json.dumps({"usage": {"input_tokens": 20}, "total_cost_usd": 0.002}),
            encoding="utf-8",
        )

        persisted: list[dict[str, Any]] = []

        def fake_persist(
            agent_id: str,
            diagnosis_id: int,
            usage: dict[str, Any] | None,
            log_text: str | None,
        ) -> None:
            persisted.append({"diagnosis_id": diagnosis_id})

        monkeypatch.setattr(d, "_persist_diagnoser_phase_output", fake_persist)

        row = {
            "diagnosis_id": 43,
            "subprocess_pid": 5678,
            "agent_id": _FAKE_CANDIDATE["agent_id"],
            "category": _FAKE_CANDIDATE["category"],
            "issue_number": None,
            "details": {},
            "failure_id": _FAKE_CANDIDATE["failure_id"],
        }
        d._reap_persist_metering(row)

        assert len(persisted) == 1, "persist must fire on reaper exit branch"
        assert persisted[0]["diagnosis_id"] == 43
