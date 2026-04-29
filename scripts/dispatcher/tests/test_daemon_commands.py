"""Unit tests for the daemon-side command handlers.

Originally landed with issue #2801 covering six commands
(start/stop/drain/pause/resume/retry/force_kill); revised for issue
#2884 which simplified the command surface to four
(start/stop/force_stop/retry) and removed the MFA re-auth gate.

Covers:
  - _consume_commands: marks consumed_at after handler, leaves unconsumed on error
  - _handle_start — flips cap 0→1, clears #2884 stop-intent flags
  - _handle_stop — graceful: sets flag, UPDATEs cap=0, does NOT engage killswitch
  - _handle_force_stop — global: cap=0 + _pause_requested.set() + phase=force_stopped
  - _handle_force_stop — per-agent (payload.agentId): kill agent, no global side-effects
  - _handle_retry (valid + invalid state)
  - scheduler_tick respects _graceful_stop_requested (does NOT set _pause_requested)
  - scheduler_tick cap>0 clears both stop-intent flags
  - _check_killswitch_and_abort picks force_stopped vs paused_by_killswitch terminal phase
  - Claim gate still respects dispatcher.config.paused (legacy _is_paused, not written
    by any handler any more; kept as a manual escape hatch for operators)
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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above
from dispatcher.tests._fixtures import psycopg_jsonb, psycopg_jsonb_legacy  # noqa: E402


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
    """_handle_start flips concurrency_cap from 0 to 1 and clears stop-intent flags."""

    def test_start_flips_cap_when_zero(self) -> None:
        """start flips cap 0→target_concurrency_cap (default 1 when row absent).

        Pre-#3779 the SQL embedded the literal ``value = '1'``. Post-#3779
        the value is bound as a ``%s::jsonb`` parameter so the operator's
        ``target_concurrency_cap`` (e.g. 4) is restored on reset rather
        than the legacy hard-coded 1.
        """
        d, conn, handler = _make_daemon_with_capture()
        # Stub fetchall to return one 'start' command.
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "start")]]
        # target_concurrency_cap row absent → fetchone returns None →
        # default 1. concurrency_cap UPDATE: rowcount=1 (was 0, now 1).
        conn.cursor_instance.fetch_queue = [None]
        conn.cursor_instance.rowcount = 1

        count = d._consume_commands()

        assert count == 1
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "concurrency_cap" in e[0]
            and "%s::jsonb" in e[0]
        ]
        assert len(cap_updates) == 1
        # The bound parameter encodes the target cap value as text
        # (psycopg coerces to JSONB).
        assert cap_updates[0][1] == ("1",)
        # consumed_at set after handler.
        consumed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "SET consumed_at = now()" in e[0]
        ]
        assert len(consumed_updates) == 1

    def test_start_uses_target_concurrency_cap_when_set(self) -> None:
        """#3779: start restores cap to operator-configured target (e.g. 4)."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "start")]]
        # target_concurrency_cap = 4 (psycopg3 unwrapped JSONB int).
        conn.cursor_instance.fetch_queue = [(4,)]
        conn.cursor_instance.rowcount = 1

        count = d._consume_commands()

        assert count == 1
        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "concurrency_cap" in e[0]
            and "%s::jsonb" in e[0]
            and "value::int = 0" in e[0]
        ]
        assert len(cap_updates) == 1
        assert cap_updates[0][1] == ("4",)

    def test_start_noop_when_cap_positive(self) -> None:
        """start when cap > 0 is a safe no-op (rowcount == 0)."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "start")]]
        # No target_concurrency_cap row → fall through to default 1.
        conn.cursor_instance.fetch_queue = [None]
        conn.cursor_instance.rowcount = 0  # already > 0, no rows updated

        count = d._consume_commands()

        assert count == 1
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "concurrency_cap" in e[0]
            and "%s::jsonb" in e[0]
        ]
        assert len(updates) == 1
        # Still consumed — a no-op is a valid success.
        consumed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "SET consumed_at = now()" in e[0]
        ]
        assert len(consumed_updates) == 1

    def test_start_clears_circuit_breaker_flag(self) -> None:
        """#3779: start also clears ``cap_flipped_by`` if breaker had opened.

        The auto-close paths (#2860 + time-based #3779) clear the flag
        on the next scheduler tick, but doing it inline here means the
        admin cockpit's "open" banner clears immediately rather than
        waiting one tick.
        """
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "start")]]
        conn.cursor_instance.fetch_queue = [None]  # target_cap missing → 1
        conn.cursor_instance.rowcount = 1

        d._consume_commands()

        flag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
            and "cap_flipped_by" in e[0]
            and "value = 'null'" in e[0]
        ]
        assert len(flag_updates) == 1

    def test_start_clears_graceful_and_force_stop_flags(self) -> None:
        """#2884: start clears any prior stop-intent flags."""
        d, conn, _handler = _make_daemon_with_capture()
        d._graceful_stop_requested = True
        d._force_stop_requested = True
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "start")]]
        # target_concurrency_cap row absent → default 1 (#3779).
        conn.cursor_instance.fetch_queue = [None]

        d._consume_commands()

        assert d._graceful_stop_requested is False
        assert d._force_stop_requested is False


# --------------------------------------------------------------------------
# _handle_stop — #2884 graceful semantic
# --------------------------------------------------------------------------


class TestHandleStop:
    """stop is graceful: cap=0, set flag, do NOT engage killswitch."""

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

    def test_stop_sets_graceful_flag(self) -> None:
        """#2884 AC3: stop lets in-flight finish — scheduler_tick will not
        engage the killswitch because _graceful_stop_requested is True."""
        d, conn, _handler = _make_daemon_with_capture()
        assert d._graceful_stop_requested is False
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "stop")]]

        d._consume_commands()

        assert d._graceful_stop_requested is True
        # And stop does NOT set _pause_requested directly; scheduler_tick
        # is the only place that touches it.
        assert d._pause_requested.is_set() is False

    def test_stop_clears_force_stop_flag(self) -> None:
        """If a prior force_stop left _force_stop_requested set and the
        operator now re-issues a graceful stop, clear it so the aborting
        agent does not get mis-tagged as force_stopped on the next phase
        boundary."""
        d, conn, _handler = _make_daemon_with_capture()
        d._force_stop_requested = True
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "stop")]]

        d._consume_commands()

        assert d._force_stop_requested is False

    def test_stop_logs_mode_graceful(self) -> None:
        """Log event contains ``mode=graceful`` for CloudWatch filtering."""
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "stop")]]

        d._consume_commands()

        events = handler.events("command_stop_applied")
        assert len(events) == 1
        assert getattr(events[0], "mode", None) == "graceful"


# --------------------------------------------------------------------------
# _handle_force_stop — #2884 global variant (no agentId)
# --------------------------------------------------------------------------


class TestHandleForceStopGlobal:
    """force_stop with no agentId: cap=0 + engage killswitch immediately."""

    def test_force_stop_sets_cap_zero(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "force_stop")]]

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

    def test_force_stop_engages_killswitch_immediately(self) -> None:
        """#2884 AC4: force_stop aborts in-flight at next phase boundary.
        The handler must set _pause_requested itself rather than waiting
        for the next scheduler_tick — the worker thread may check the
        event at any time."""
        d, conn, _handler = _make_daemon_with_capture()
        assert d._pause_requested.is_set() is False
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "force_stop")]]

        d._consume_commands()

        assert d._pause_requested.is_set() is True

    def test_force_stop_sets_force_stop_flag(self) -> None:
        """_force_stop_requested True tells _check_killswitch_and_abort
        to use the force_stopped terminal phase name."""
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "force_stop")]]

        d._consume_commands()

        assert d._force_stop_requested is True

    def test_force_stop_clears_graceful_flag(self) -> None:
        """A pending graceful stop is overridden by force_stop."""
        d, conn, _handler = _make_daemon_with_capture()
        d._graceful_stop_requested = True
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "force_stop")]]

        d._consume_commands()

        assert d._graceful_stop_requested is False

    def test_force_stop_logs_mode_global(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "force_stop")]]

        d._consume_commands()

        events = handler.events("command_force_stop_applied")
        assert len(events) == 1
        assert getattr(events[0], "mode", None) == "global"


# --------------------------------------------------------------------------
# _handle_force_stop — #2884 per-agent variant (payload.agentId)
# --------------------------------------------------------------------------


class TestHandleForceStopPerAgent:
    """force_stop with agentId: kill that agent, no global side-effects."""

    def test_force_stop_agent_transitions_to_crashed(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_stop", {"agentId": agent_id})]
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

    def test_force_stop_agent_does_not_touch_concurrency_cap(self) -> None:
        """Per-agent kill has narrow scope — other in-flight agents and
        new claims are unaffected."""
        d, conn, _handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_stop", {"agentId": agent_id})]
        ]
        conn.cursor_instance.fetch_queue = [(None,)]

        d._consume_commands()

        cap_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0] and "concurrency_cap" in e[0]
        ]
        assert cap_updates == []

    def test_force_stop_agent_does_not_engage_global_killswitch(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_stop", {"agentId": agent_id})]
        ]
        conn.cursor_instance.fetch_queue = [(None,)]

        d._consume_commands()

        assert d._pause_requested.is_set() is False
        assert d._force_stop_requested is False

    def test_force_stop_agent_missing_in_db_writes_failure(self) -> None:
        """Unknown agentId → CommandError → failures row, command stays unconsumed."""
        d, conn, _handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_stop", {"agentId": agent_id})]
        ]
        conn.cursor_instance.fetch_queue = [None]  # SELECT returns no row

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

    def test_force_stop_agent_logs_per_agent_event(self) -> None:
        """Per-agent and global variants emit distinct log events so
        CloudWatch filtering can distinguish them."""
        d, conn, handler = _make_daemon_with_capture()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_stop", {"agentId": agent_id})]
        ]
        conn.cursor_instance.fetch_queue = [(None,)]

        d._consume_commands()

        assert handler.events("command_force_stop_agent_applied") != []
        assert handler.events("command_force_stop_applied") == []


# --------------------------------------------------------------------------
# Removed handlers — regression guard (#2884)
# --------------------------------------------------------------------------


class TestRemovedCommandsRaiseInvalidCommand:
    """Old command names are no longer registered; _consume_commands
    raises CommandError → failures row with category='invalid_command'."""

    @pytest.mark.parametrize(
        "removed_command", ["drain", "pause", "resume", "force_kill"]
    )
    def test_removed_command_produces_failure(self, removed_command: str) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, removed_command)]]

        count = d._consume_commands()

        # Not consumed — CommandError rolled back.
        assert count == 0
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        _sql, params = failure_inserts[0]
        assert params[1] == "invalid_command"


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
        # Empty-pending fast path opens no transaction; no rollback is the contract (#2797).
        assert conn.rollbacks == 0


# --------------------------------------------------------------------------
# scheduler_tick graceful-stop behaviour (#2884)
# --------------------------------------------------------------------------


class TestSchedulerTickGracefulStop:
    """scheduler_tick observes cap=0 but respects _graceful_stop_requested."""

    def test_graceful_stop_cap_zero_does_not_engage_killswitch(self) -> None:
        """AC3: stop finishes in-flight agent then sets cap=0.
        The flag (set by _handle_stop) tells scheduler_tick to NOT set
        _pause_requested, so the in-flight worker keeps running."""
        d, conn, handler = _make_daemon_with_capture()
        d._graceful_stop_requested = True
        # SELECTs in scheduler_tick cap==0 branch:
        #   1. concurrency_cap = 0
        conn.cursor_instance.fetch_queue = [(0,)]
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        d.scheduler_tick()

        assert d._pause_requested.is_set() is False
        # Distinct log event so CloudWatch can see graceful-stop ticks.
        assert handler.events("graceful_stop_in_progress") != []
        assert handler.events("killswitch_engaged") == []

    def test_force_stop_path_engages_killswitch(self) -> None:
        """When NOT in graceful-stop mode, cap=0 still engages the
        killswitch — this is the default #2847 behaviour for config
        edits / circuit-breaker / force_stop."""
        d, conn, handler = _make_daemon_with_capture()
        assert d._graceful_stop_requested is False
        conn.cursor_instance.fetch_queue = [(0,)]
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        d.scheduler_tick()

        assert d._pause_requested.is_set() is True
        assert handler.events("killswitch_engaged") != []
        assert handler.events("graceful_stop_in_progress") == []

    def test_cap_positive_clears_stop_intent_flags(self) -> None:
        """An operator who sets cap back to ≥1 via a direct config
        edit (bypassing the start command) still gets a clean slate."""
        d, conn, _handler = _make_daemon_with_capture()
        d._graceful_stop_requested = True
        d._force_stop_requested = True
        d._pause_requested.set()
        # SELECTs (scheduler_tick cap>0 branch):
        #   1. concurrency_cap = 1
        #   2. cap_flipped_by = None (overnight CB auto-close; no-op)
        #   3. _is_paused = None (not paused)
        #   4. _has_active_agent COUNT = None
        conn.cursor_instance.fetch_queue = [(1,), None, None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=False,
        )
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        d.scheduler_tick()

        assert d._graceful_stop_requested is False
        assert d._force_stop_requested is False
        assert d._pause_requested.is_set() is False


# --------------------------------------------------------------------------
# _check_killswitch_and_abort picks the terminal phase name (#2884)
# --------------------------------------------------------------------------


class TestCheckKillswitchTerminalPhase:
    """_check_killswitch_and_abort distinguishes force_stopped vs
    paused_by_killswitch based on _force_stop_requested."""

    def test_default_terminal_phase_is_paused_by_killswitch(self) -> None:
        d, conn, handler = _make_daemon_with_capture()
        d._pause_requested.set()
        assert d._force_stop_requested is False
        # _observe_external_terminal → SELECT returns None (no terminal)
        conn.cursor_instance.fetch_queue = [None]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]

        aborted = d._check_killswitch_and_abort("agent-xyz", "plan")

        assert aborted is True
        d._mark_agent_terminal.assert_called_once()  # type: ignore[attr-defined]
        _args, kwargs = d._mark_agent_terminal.call_args  # type: ignore[attr-defined]
        assert kwargs["phase"] == daemon.KILLSWITCH_TERMINAL_PHASE
        assert kwargs["phase"] == "paused_by_killswitch"

    def test_force_stop_terminal_phase_is_force_stopped(self) -> None:
        """#2884 AC4: force_stop marks aborting agent phase=force_stopped."""
        d, conn, handler = _make_daemon_with_capture()
        d._pause_requested.set()
        d._force_stop_requested = True
        # _observe_external_terminal → SELECT returns None (no external terminal)
        conn.cursor_instance.fetch_queue = [None]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]

        aborted = d._check_killswitch_and_abort("agent-xyz", "plan")

        assert aborted is True
        d._mark_agent_terminal.assert_called_once()  # type: ignore[attr-defined]
        _args, kwargs = d._mark_agent_terminal.call_args  # type: ignore[attr-defined]
        assert kwargs["phase"] == daemon.FORCE_STOP_TERMINAL_PHASE
        assert kwargs["phase"] == "force_stopped"

    def test_no_killswitch_no_abort(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        assert d._pause_requested.is_set() is False
        # _observe_external_terminal → SELECT returns None (no terminal)
        conn.cursor_instance.fetch_queue = [None]

        aborted = d._check_killswitch_and_abort("agent-xyz", "plan")

        assert aborted is False


# --------------------------------------------------------------------------
# Claim gate legacy paused key (kept as manual escape hatch — #2884)
# --------------------------------------------------------------------------


class TestClaimGateLegacyPausedKey:
    """The ``dispatcher.config.paused`` key is no longer written by any
    command handler (pause/resume were removed in #2884), but
    ``_is_paused()`` is retained as a manual operator escape hatch —
    a direct ``INSERT INTO dispatcher.config ('paused', 'true', ...)``
    from dev-db-query still blocks new claims. These tests verify that
    escape hatch still works."""

    @pytest.mark.parametrize(
        "paused_value,shape_id",
        [
            (psycopg_jsonb(True), "psycopg3_native_bool"),
            (psycopg_jsonb_legacy(True), "legacy_json_string"),
        ],
        ids=["psycopg3_native_bool", "legacy_json_string"],
    )
    def test_paused_via_is_paused_gate(self, paused_value: Any, shape_id: str) -> None:
        """When paused=true in either JSONB shape, orchestration must not fire.

        Issue #2874: psycopg3 native bool (production shape) and
        JSON-encoded string (legacy fake-cursor shape) both block claims.
        """
        d, conn, _handler = _make_daemon_with_capture()
        # SELECTs (in scheduler_tick order, cap>0 branch):
        #   1. concurrency_cap = 1 (gate would normally proceed)
        #   2. cap_flipped_by = None (overnight CB auto-close #2860; no-op)
        #   3. _is_paused() = paused_value — the parametrized shape
        conn.cursor_instance.fetch_queue = [(1,), None, (paused_value,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError(
                f"must not spawn when paused (shape={shape_id!r})"
            )
        )
        d._consume_commands = lambda: 0  # type: ignore[method-assign]

        summary = d.scheduler_tick()

        # Orchestration was not attempted.
        assert summary["orchestration_attempted"] == 0
        assert d._maybe_spawn_orchestration_thread.call_count == 0  # type: ignore[attr-defined]

    def test_claim_gate_respects_paused_key(self) -> None:
        """When paused=true (manually inserted), orchestration must not
        fire even if cap > 0.

        Kept as a standalone regression guard; the parametrized
        ``test_paused_via_is_paused_gate`` covers both shapes.
        """
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

        d.scheduler_tick()

        assert d._maybe_spawn_orchestration_thread.call_count == 1  # type: ignore[attr-defined]
