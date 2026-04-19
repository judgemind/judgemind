"""Unit tests for the daemon-side command handlers (issue #2801).

Covers:
  - _consume_commands: marks consumed_at after handler, leaves unconsumed on error
  - _handle_start / stop / drain
  - _handle_pause / resume
  - _handle_retry (valid + invalid state)
  - _handle_force_kill (valid + missing agentId)
  - Claim gate respects dispatcher.config.paused
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Ensure a `psycopg` module exists in sys.modules before importing the daemon.
if "psycopg" not in sys.modules:  # pragma: no cover — fresh-venv guard
    sys.modules["psycopg"] = MagicMock()

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_phase3c.py shape)
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
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

    def fetchall(self) -> list[Any]:
        if not self.fetchall_queue:
            return []
        return self.fetchall_queue.pop(0)


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
    """Collect emitted ``LogRecord``s so tests can assert on them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon_with_capture() -> tuple[
    daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler
]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.commands.{id(handler)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake-for-tests",
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


def _command_row(
    command_id: int,
    command: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, str, dict[str, Any]]:
    """Build a fake row as returned by the commands SELECT."""
    return (command_id, command, payload or {})


# --------------------------------------------------------------------------
# _handle_start
# --------------------------------------------------------------------------


class TestHandleStart:
    """_handle_start flips concurrency_cap from 0 to 1."""

    def test_start_flips_cap_when_zero(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        # Stub fetchall to return one 'start' command.
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "start")]]
        # concurrency_cap UPDATE: rowcount=1 (was 0, now 1).
        conn.cursor_instance.rowcount = 1

        count = d._consume_commands()

        assert count == 1
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "concurrency_cap" in e[0]
            and "value = '1'" in e[0]
        ]
        assert len(updates) == 1
        # consumed_at set after handler.
        consumed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "SET consumed_at = now()" in e[0]
        ]
        assert len(consumed_updates) == 1

    def test_start_noop_when_cap_positive(self) -> None:
        """start when cap > 0 is a safe no-op (rowcount == 0)."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "start")]]
        conn.cursor_instance.rowcount = 0  # already > 0, no rows updated

        count = d._consume_commands()

        assert count == 1
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "value = '1'" in e[0]
        ]
        assert len(updates) == 1
        # Still consumed — a no-op is a valid success.
        consumed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "SET consumed_at = now()" in e[0]
        ]
        assert len(consumed_updates) == 1


# --------------------------------------------------------------------------
# _handle_stop / _handle_drain
# --------------------------------------------------------------------------


class TestHandleStopAndDrain:
    """stop and drain both set concurrency_cap to 0."""

    def test_stop_sets_cap_zero(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "stop")]]

        count = d._consume_commands()

        assert count == 1
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "value = '0'" in e[0]
            and "concurrency_cap" in e[0]
        ]
        assert len(updates) == 1

    def test_drain_sets_cap_zero(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "drain")]]

        count = d._consume_commands()

        assert count == 1
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "value = '0'" in e[0]
            and "concurrency_cap" in e[0]
        ]
        assert len(updates) == 1


# --------------------------------------------------------------------------
# _handle_pause / _handle_resume
# --------------------------------------------------------------------------


class TestHandlePauseResume:
    """pause and resume UPSERT the paused config key."""

    def test_pause_upserts_paused_true(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "pause")]]

        count = d._consume_commands()

        assert count == 1
        upserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.config" in e[0]
            and "'paused'" in e[0]
            and "'true'" in e[0]
        ]
        assert len(upserts) == 1
        # ON CONFLICT clause present.
        assert "ON CONFLICT" in upserts[0][0]

    def test_resume_upserts_paused_false(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "resume")]]

        count = d._consume_commands()

        assert count == 1
        upserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.config" in e[0]
            and "'paused'" in e[0]
            and "'false'" in e[0]
        ]
        assert len(upserts) == 1
        assert "ON CONFLICT" in upserts[0][0]


# --------------------------------------------------------------------------
# _handle_retry
# --------------------------------------------------------------------------


class TestHandleRetry:
    """retry command creates a retry marker for a failed agent."""

    def test_retry_creates_marker_for_failed_agent(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "retry", {"agentId": agent_id})]
        ]
        # SELECT agents returns (status='failed', retries_used=1).
        conn.cursor_instance.fetch_queue = [("failed", 1)]

        count = d._consume_commands()

        assert count == 1
        # Agent flipped to 'retrying'.
        agent_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "'retrying'" in e[0]
        ]
        assert len(agent_updates) == 1
        # Retry marker inserted.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        # Attempt is retries_used + 1 = 2.
        _sql, params = marker_inserts[0]
        assert params[1] == 2  # attempt = retries_used + 1

    def test_retry_writes_failure_for_non_failed_agent(self) -> None:
        """retry for a running agent raises CommandError → failures row."""
        d, conn, _handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "retry", {"agentId": agent_id})]
        ]
        # Agent is 'running', not 'failed'.
        conn.cursor_instance.fetch_queue = [("running", 0)]

        count = d._consume_commands()

        # Handler raises CommandError → command stays unconsumed.
        assert count == 0
        assert conn.rollbacks >= 1
        # failures row inserted for invalid_command.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        _sql, params = failure_inserts[0]
        assert params[1] == "invalid_command"


# --------------------------------------------------------------------------
# _handle_force_kill
# --------------------------------------------------------------------------


class TestHandleForceKill:
    """force_kill transitions an agent to crashed."""

    def test_force_kill_transitions_agent_to_crashed(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_kill", {"agentId": agent_id})]
        ]
        # SELECT returns pid=None (no live pid to kill).
        conn.cursor_instance.fetch_queue = [(None,)]

        count = d._consume_commands()

        assert count == 1
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "'crashed'" in e[0]
        ]
        assert len(updates) == 1

    def test_force_kill_missing_agent_id_writes_failure(self) -> None:
        """force_kill with no agentId raises CommandError → failures row."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_kill", {})]  # no agentId
        ]

        count = d._consume_commands()

        assert count == 0
        assert conn.rollbacks >= 1
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        _sql, params = failure_inserts[0]
        assert params[1] == "invalid_command"


# --------------------------------------------------------------------------
# _consume_commands — lifecycle guarantees
# --------------------------------------------------------------------------


class TestConsumeCommands:
    """_consume_commands marks consumed_at AFTER handler, leaves unconsumed on error."""

    def test_consume_commands_marks_consumed_after_handler_success(self) -> None:
        """consumed_at is set in the same transaction as the handler's work."""
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "stop")]]

        count = d._consume_commands()

        assert count == 1
        executed = conn.cursor_instance.executed
        # The UPDATE dispatcher.commands SET consumed_at comes after handler.
        consumed_idx = next(
            (i for i, e in enumerate(executed) if "SET consumed_at = now()" in e[0]),
            None,
        )
        config_idx = next(
            (i for i, e in enumerate(executed) if "UPDATE dispatcher.config" in e[0]),
            None,
        )
        assert consumed_idx is not None
        assert config_idx is not None
        # Handler work happens before consumed_at update in the same cursor block.
        assert config_idx < consumed_idx

    def test_consume_commands_leaves_unconsumed_on_handler_exception(self) -> None:
        """A handler error rolls back and leaves consumed_at NULL (AC6)."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "retry", {"agentId": str(uuid.uuid4())})]
        ]
        # Agent not found → raises CommandError.
        conn.cursor_instance.fetch_queue = [None]  # SELECT returns no row

        count = d._consume_commands()

        # Command NOT consumed.
        assert count == 0
        # Transaction rolled back.
        assert conn.rollbacks >= 1
        # No consumed_at UPDATE.
        consumed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "SET consumed_at = now()" in e[0]
        ]
        assert len(consumed_updates) == 0

    def test_consume_commands_returns_zero_when_no_pending(self) -> None:
        """Empty commands table returns 0 without errors."""
        d, conn, _handler = _make_daemon_with_capture()
        # fetchall_queue empty → fetchall returns []

        count = d._consume_commands()

        assert count == 0
        assert conn.rollbacks == 0


# --------------------------------------------------------------------------
# Claim gate respects paused config key
# --------------------------------------------------------------------------


class TestClaimGateRespectsPausedKey:
    """Claim gate blocks orchestration when dispatcher.config.paused = true."""

    def test_claim_gate_respects_paused_key(self) -> None:
        """When paused=true, orchestration must not fire even if cap > 0."""
        d, conn, handler = _make_daemon_with_capture()
        # SELECTs (in scheduler_tick order, cap>0 branch):
        #   1. concurrency_cap = 1 (gate would normally proceed)
        #   2. cap_flipped_by = None (overnight CB auto-close #2860; no-op)
        #   3. _is_paused() = "true" — JSONB string 'true'
        conn.cursor_instance.fetch_queue = [(1,), None, ("true",)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not spawn when paused")
        )
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        summary = d.scheduler_tick()

        # Orchestration was not attempted.
        assert summary["orchestration_attempted"] == 0
        assert d._maybe_spawn_orchestration_thread.call_count == 0  # type: ignore[attr-defined]

    def test_claim_gate_allows_spawn_when_not_paused(self) -> None:
        """When paused key is absent/false, normal spawn proceeds."""
        d, conn, _handler = _make_daemon_with_capture()
        # SELECTs (in scheduler_tick order, cap>0 branch):
        #   1. concurrency_cap = 1
        #   2. cap_flipped_by = None (overnight CB auto-close #2860; no-op)
        #   3. _is_paused() = None (absent)
        #   4. _has_active_agent() COUNT = None (no active agent)
        conn.cursor_instance.fetch_queue = [(1,), None, None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=False,
        )
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        summary = d.scheduler_tick()

        assert d._maybe_spawn_orchestration_thread.call_count == 1  # type: ignore[attr-defined]
