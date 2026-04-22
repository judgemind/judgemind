"""Unit tests for final-ralph-retry Opus upgrade (#2955).

On a fresh ``/task-v2-ralph`` spawn the daemon historically used
:data:`daemon.PHASE_MODELS["ralph"]` (``sonnet``) for every attempt in
the retry budget. Issue #2955 upgrades the **final** budgeted attempt
to ``opus`` — if Sonnet has already failed ``MAX_RETRY_ATTEMPTS - 1``
times on the same issue, burning the last attempt on the same model
yields the same result. Opus on the final attempt gives the queue a
real shot at unblocking before the agent is given up on.

Covers the two seams the change touches:

* :func:`daemon._ralph_model_for_attempt` — pure function, returns
  ``"opus"`` on the 1-indexed final attempt and ``"sonnet"`` earlier.
  The boundary matches :data:`daemon.MAX_RETRY_ATTEMPTS` so any future
  change to that cap automatically propagates.
* :meth:`daemon.DispatcherDaemon._spawn_phase_subprocess` — the
  ``--model`` argument it hands to ``claude -p`` reflects the attempt-
  aware model for ``phase == "ralph"``, and the ``phase_started``
  structured log event records the chosen model so CloudWatch queries
  can distinguish Opus-ralph runs from Sonnet-ralph runs retroactively.

Same mocking strategy as :mod:`test_daemon_token_metering` — psycopg
is stubbed before the daemon import so the test runner never needs a
real DB or the ``psycopg`` wheel. No ``claude`` / ``gh`` / ``git``
subprocesses run.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


class _UniqueViolation(Exception):
    """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules.setdefault("psycopg", _psycopg_stub)

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirror test_daemon_token_metering's lightweight doubles.
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
    logger = logging.getLogger(f"dispatcher.test.ralph_opus.{id(tmp_path)}")
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
# _ralph_model_for_attempt — pure function
# --------------------------------------------------------------------------


class TestRalphModelForAttempt:
    """The pure attempt → model mapping. 1-indexed attempt numbers: the
    first run is attempt 1, and :data:`daemon.MAX_RETRY_ATTEMPTS` is the
    final budgeted attempt."""

    def test_first_attempt_returns_sonnet(self) -> None:
        assert daemon._ralph_model_for_attempt(1) == "sonnet"

    def test_middle_attempt_returns_sonnet(self) -> None:
        # Applies regardless of how MAX_RETRY_ATTEMPTS is configured — any
        # attempt strictly less than the cap stays on Sonnet.
        assert daemon.MAX_RETRY_ATTEMPTS >= 2
        assert (
            daemon._ralph_model_for_attempt(daemon.MAX_RETRY_ATTEMPTS - 1) == "sonnet"
        )

    def test_final_attempt_returns_opus(self) -> None:
        assert daemon._ralph_model_for_attempt(daemon.MAX_RETRY_ATTEMPTS) == "opus"

    def test_beyond_final_attempt_stays_opus(self) -> None:
        """Defensive: if a caller computes an attempt past the budget we
        still return Opus rather than silently downgrading. The retry
        cap is enforced elsewhere (``_create_retry_marker``)."""
        assert daemon._ralph_model_for_attempt(daemon.MAX_RETRY_ATTEMPTS + 1) == "opus"

    def test_non_ralph_attempts_do_not_touch_other_phases(self) -> None:
        """Regression guard: PHASE_MODELS entries for ``plan``, ``summary``,
        ``fix-ci``, ``verify``, ``retro`` stay exactly what migration 21
        seeded. The upgrade rule applies to ralph only (#2955 Scope
        Boundaries)."""
        assert daemon.PHASE_MODELS["plan"] == "opus"
        assert daemon.PHASE_MODELS["ralph"] == "sonnet"
        assert daemon.PHASE_MODELS["summary"] == "haiku"
        assert daemon.PHASE_MODELS["fix-ci"] == "sonnet"
        assert daemon.PHASE_MODELS["verify"] == "haiku"
        assert daemon.PHASE_MODELS["retro"] == "haiku"


# --------------------------------------------------------------------------
# _spawn_phase_subprocess — --model argument is attempt-aware for ralph
# --------------------------------------------------------------------------


def _extract_model_arg(cmd: list[str]) -> str:
    """Return the value immediately following ``--model`` in a cmd list."""
    assert "--model" in cmd, cmd
    return cmd[cmd.index("--model") + 1]


class TestRalphSpawnUsesAttemptAwareModel:
    """``_spawn_phase_subprocess("ralph", ...)`` reads
    ``dispatcher.agents.retries_used`` via :meth:`_current_attempt_for`
    and hands the matching model to the ``claude -p`` subprocess."""

    def test_first_ralph_attempt_spawns_sonnet(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # retries_used = 0 → 1-indexed attempt 1 → Sonnet.
        conn.cursor_instance.fetch_queue = [(0,)]

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-uuid")

        assert _extract_model_arg(captured["cmd"]) == "sonnet"

    def test_final_ralph_attempt_spawns_opus(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # retries_used = MAX_RETRY_ATTEMPTS - 1 → 1-indexed attempt ==
        # MAX_RETRY_ATTEMPTS (the final budgeted attempt) → Opus.
        conn.cursor_instance.fetch_queue = [(daemon.MAX_RETRY_ATTEMPTS - 1,)]

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-uuid")

        assert _extract_model_arg(captured["cmd"]) == "opus"

    def test_middle_ralph_attempt_still_spawns_sonnet(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """With MAX_RETRY_ATTEMPTS=3 this exercises retries_used=1 → attempt
        2. If the cap ever moves, this test still covers "strictly less
        than final"."""
        assert daemon.MAX_RETRY_ATTEMPTS >= 2
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(daemon.MAX_RETRY_ATTEMPTS - 2,)]

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-uuid")

        assert _extract_model_arg(captured["cmd"]) == "sonnet"

    def test_phase_started_event_records_chosen_ralph_model_on_final(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The ``phase_started`` structured log event carries the actual
        model that was handed to claude -p, not the static PHASE_MODELS
        default. A CloudWatch query over a retry stretch can therefore
        grep ``model = 'opus'`` to find final-attempt ralph runs."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(daemon.MAX_RETRY_ATTEMPTS - 1,)]

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-uuid")

        starts = handler.events("phase_started")
        assert starts, "expected a phase_started event"
        assert getattr(starts[-1], "model", None) == "opus"
        assert getattr(starts[-1], "phase", None) == "ralph"

    def test_phase_started_event_records_sonnet_on_first_attempt(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Symmetry check — the log records ``sonnet`` on non-final
        attempts so a retroactive query can correctly bucket runs."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-uuid")

        starts = handler.events("phase_started")
        assert starts
        assert getattr(starts[-1], "model", None) == "sonnet"

    def test_non_ralph_phase_model_is_unchanged(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Scope guard: the attempt-aware upgrade applies to ralph only.
        Even if the agent's ``retries_used`` is at the final-attempt
        boundary, a ``plan`` spawn still uses ``PHASE_MODELS["plan"]``
        (Opus) — not because of the upgrade rule but because that is
        the static default. The assertion is that the model comes from
        PHASE_MODELS, not the attempt-aware path."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Even if retries_used is set, plan does not query it — the
        # _current_attempt_for SELECT must not fire for non-ralph phases,
        # so we leave the fetch queue empty. If the code path regressed
        # to call it anyway, fetchone() would return None and the
        # default (0) would still yield the correct static model.
        conn.cursor_instance.fetch_queue = []

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("plan", tmp_path, "agent-uuid")

        assert _extract_model_arg(captured["cmd"]) == daemon.PHASE_MODELS["plan"]
        # Guard that the attempt-aware query never fired on this path.
        # Only the plan phase's own queries (none, in this path) should
        # appear — any ``retries_used`` SELECT would be a regression.
        retries_selects = [
            (sql, params)
            for (sql, params) in conn.cursor_instance.executed
            if "retries_used" in sql
        ]
        assert retries_selects == []
