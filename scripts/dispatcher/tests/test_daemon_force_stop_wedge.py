"""Unit tests for the force-stop-wedge recovery paths (#3184).

Two mechanisms under test, both born out of the 2026-04-24 08:03-08:26Z
incident where ``force_stop`` on a subprocess-mode agent left the
orchestration worker thread ``alive=True`` for ~23 minutes, blocking
every subsequent ``_maybe_spawn_orchestration_thread`` call while
``scheduler_tick`` kept ticking on its 30s cadence (so the existing
"0 scheduler_tick for >5m" wedge detector never fired).

Primary fix — **instrumentation first** (per CLAUDE.md
"instrument before you guess"):

* When ``_handle_force_stop`` fires in subprocess mode it records the
  force-stopped ``agent_id`` on ``self._force_stopped_agent_ids``.
* On the first ``orchestration_in_progress`` log after a
  ``force_stop_signal_sent`` has been recorded — i.e. the same wedge
  signature as the incident — ``_maybe_spawn_orchestration_thread``
  emits a structured ``orchestration_thread_stuck_after_force_stop``
  event with a faulthandler all-threads traceback attached. Fires once
  per wedged thread (reset when a new orchestration thread spawns) so
  the noise is bounded.

Secondary fix — supervisor orphan detection:

* Every scheduler tick, if the orchestration thread is alive AND
  ``_has_active_agent()`` returns False, increment
  ``self._orchestration_orphan_tick_count``. On five consecutive
  orphan ticks (≈150s at the 30s cadence), log
  ``orchestration_orphaned`` and reset ``self._orchestration_thread``
  to None so the NEXT tick can spawn a fresh thread.
* Any tick that does NOT see the orphan condition resets the counter
  to zero — the threshold is "consecutive orphan ticks," not
  "cumulative orphan ticks," so transient mismatches between the DB
  row update and the thread handle don't trip the guard prematurely.

Both mechanisms are belt-and-braces guards — even if the per-agent
``force_stop`` path is later extended to signal the thread directly,
the orphan detector still catches any future regression that produces
the same "thread alive but no active agent" state.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (mirror the pattern in test_daemon_commands.py /
# test_daemon_phase3a.py).
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
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon() -> tuple[
    daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler
]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.force_stop_wedge.{id(handler)}")
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


def _command_row(
    command_id: int,
    command: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, str, dict[str, Any]]:
    return (command_id, command, payload or {})


class _LiveThread:
    """A handle the helpers can interrogate with ``.is_alive()`` without
    actually running Python code on a worker thread.
    """

    def __init__(self, name: str = "orchestration-test") -> None:
        self.name = name
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self._alive = False


# --------------------------------------------------------------------------
# Supervisor orphan detection (#3184 secondary fix).
#
# The scheduler_tick must catch the "thread alive but no active agent"
# state and reset the ``_orchestration_thread`` handle after the
# configured number of consecutive orphan ticks so the next tick can
# spawn a fresh thread.
# --------------------------------------------------------------------------


class TestOrchestrationOrphanDetection:
    """Supervisor orphan guard runs inside ``scheduler_tick``."""

    def test_initial_orphan_counter_is_zero(self) -> None:
        d, _conn, _handler = _make_daemon()
        assert d._orchestration_orphan_tick_count == 0

    def test_orphan_counter_increments_when_thread_alive_and_no_active_agent(
        self,
    ) -> None:
        """Single orphan tick increments the counter but does NOT reset
        the thread handle — we want 5 consecutive ticks first.

        Fetch queue feeds scheduler_tick's cursor reads in order:

        1. ``(1,)`` — ``dispatcher.config.concurrency_cap`` read.
        2. ``None`` — ``cap_flipped_by`` read (circuit-breaker auto-close
           check short-circuits on ``None``).
        3. ``None`` — ``_is_paused`` read (not paused).
        4. ``None`` — ``_has_active_agent`` SELECT returns no row.
        """
        d, conn, _handler = _make_daemon()
        live = _LiveThread()
        d._orchestration_thread = live  # type: ignore[assignment]

        conn.cursor_instance.fetch_queue = [(1,), None, None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        # spawn path observes the live handle and declines; returns False.
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=False,
        )

        d.scheduler_tick()

        assert d._orchestration_orphan_tick_count == 1
        # Thread handle untouched — one tick isn't enough yet.
        assert d._orchestration_thread is live

    def test_orphan_counter_resets_when_active_agent_exists(self) -> None:
        """An active agent row means the thread IS doing legitimate
        work; the orphan state is not present, so the counter resets.

        Fetch queue: ``(1,)`` cap, ``None`` cap_flipped_by, ``None``
        is_paused, ``(1,)`` has_active_agent → True.
        """
        d, conn, _handler = _make_daemon()
        live = _LiveThread()
        d._orchestration_thread = live  # type: ignore[assignment]
        d._orchestration_orphan_tick_count = 3

        conn.cursor_instance.fetch_queue = [(1,), None, None, (1,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]

        d.scheduler_tick()

        assert d._orchestration_orphan_tick_count == 0
        assert d._orchestration_thread is live

    def test_orphan_counter_resets_when_thread_dead(self) -> None:
        """No live thread → no orphan condition → counter resets."""
        d, conn, _handler = _make_daemon()
        d._orchestration_orphan_tick_count = 3
        d._orchestration_thread = None

        conn.cursor_instance.fetch_queue = [(1,), None, None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=True,
        )

        d.scheduler_tick()

        assert d._orchestration_orphan_tick_count == 0

    def test_orphan_clears_thread_handle_after_threshold(self) -> None:
        """At the configured threshold (5 ticks ≈ 150s at 30s cadence)
        the daemon logs ``orchestration_orphaned`` and resets the
        thread handle so the NEXT tick can spawn a fresh thread."""
        d, conn, handler = _make_daemon()
        live = _LiveThread(name="orchestration-wedged")
        d._orchestration_thread = live  # type: ignore[assignment]
        # Pre-populate the counter so this tick is the 5th consecutive orphan.
        d._orchestration_orphan_tick_count = (
            daemon.ORCHESTRATION_ORPHAN_TICK_THRESHOLD - 1
        )

        conn.cursor_instance.fetch_queue = [(1,), None, None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=False,
        )

        d.scheduler_tick()

        # Orphan event logged and counter reset.
        orphan_events = handler.events("orchestration_orphaned")
        assert orphan_events, "expected orchestration_orphaned log event"
        assert d._orchestration_thread is None
        assert d._orchestration_orphan_tick_count == 0

    def test_orphan_event_payload_shape(self) -> None:
        """The log event carries enough context for CloudWatch filtering
        — run_id + the wedged thread's name + the number of ticks."""
        d, conn, handler = _make_daemon()
        live = _LiveThread(name="orchestration-230")
        d._orchestration_thread = live  # type: ignore[assignment]
        d._orchestration_orphan_tick_count = (
            daemon.ORCHESTRATION_ORPHAN_TICK_THRESHOLD - 1
        )

        conn.cursor_instance.fetch_queue = [(1,), None, None, None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=False,
        )

        d.scheduler_tick()

        orphan_events = handler.events("orchestration_orphaned")
        assert orphan_events
        rec = orphan_events[0]
        assert getattr(rec, "run_id", None) == "test-run-id"
        assert getattr(rec, "thread_name", None) == "orchestration-230"
        assert (
            getattr(rec, "tick_count", None)
            == daemon.ORCHESTRATION_ORPHAN_TICK_THRESHOLD
        )

    def test_orphan_counter_respects_threshold_constant(self) -> None:
        """The threshold constant is exposed on the module so operators
        can tune it and tests can reference it symbolically."""
        assert hasattr(daemon, "ORCHESTRATION_ORPHAN_TICK_THRESHOLD")
        assert daemon.ORCHESTRATION_ORPHAN_TICK_THRESHOLD >= 3, (
            "threshold should be >=3 to tolerate transient DB mismatches "
            "between an agent terminal status write and the thread exit"
        )

    def test_orphan_detection_skipped_when_paused(self) -> None:
        """When the daemon is paused the orphan check is skipped —
        a paused daemon may legitimately have a long-running thread
        finishing its current phase without claiming anything new.

        Fetch queue: cap=1, cap_flipped_by=None, is_paused=('true',).
        The claim gate short-circuits on paused=True and never calls
        ``_has_active_agent`` — so ``has_active_agent_this_tick`` stays
        ``None`` and the orphan check's else branch resets the counter.
        """
        d, conn, _handler = _make_daemon()
        live = _LiveThread()
        d._orchestration_thread = live  # type: ignore[assignment]
        d._orchestration_orphan_tick_count = 0

        conn.cursor_instance.fetch_queue = [(1,), None, ("true",)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._maybe_spawn_orchestration_thread = MagicMock()  # type: ignore[method-assign]

        d.scheduler_tick()

        # We did NOT increment — a paused daemon is not the wedge state.
        assert d._orchestration_orphan_tick_count == 0

    def test_orphan_threshold_respawn_sequence(self) -> None:
        """End-to-end: five consecutive orphan ticks → handle cleared →
        event fires exactly once (covers AC #2: recovers in ≤5 ticks)."""
        d, conn, handler = _make_daemon()
        first_live = _LiveThread(name="orchestration-wedged")
        d._orchestration_thread = first_live  # type: ignore[assignment]

        # Each tick needs 4 fetchone entries:
        # cap, cap_flipped_by, is_paused, has_active_agent.
        per_tick: list[Any] = [(1,), None, None, None]
        conn.cursor_instance.fetch_queue = (
            per_tick * daemon.ORCHESTRATION_ORPHAN_TICK_THRESHOLD
        )

        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        # Spawn declines each tick (thread is alive).
        spawn_mock = MagicMock(return_value=False)
        d._maybe_spawn_orchestration_thread = spawn_mock  # type: ignore[method-assign]

        for _ in range(daemon.ORCHESTRATION_ORPHAN_TICK_THRESHOLD):
            d.scheduler_tick()

        # After threshold ticks the handle is cleared and the orphan
        # event fired exactly once.
        orphan_events = handler.events("orchestration_orphaned")
        assert len(orphan_events) == 1
        assert d._orchestration_thread is None
        assert d._orchestration_orphan_tick_count == 0


# --------------------------------------------------------------------------
# Faulthandler instrumentation (#3184 primary-fix Step 1).
#
# The per-agent force_stop records the agent_id on
# ``self._force_stopped_agent_ids``; the first subsequent
# ``orchestration_in_progress`` log emits a one-shot
# ``orchestration_thread_stuck_after_force_stop`` event with a
# faulthandler traceback of all threads.
# --------------------------------------------------------------------------


class TestForceStopRecordsAgentId:
    """Per-agent ``force_stop`` adds the agent_id to
    ``_force_stopped_agent_ids`` so the wedge detector can fire."""

    def test_initial_set_is_empty(self) -> None:
        d, _conn, _handler = _make_daemon()
        assert d._force_stopped_agent_ids == set()

    def test_per_agent_force_stop_records_agent_id(self) -> None:
        d, conn, _handler = _make_daemon()
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_stop", {"agentId": agent_id})]
        ]
        # SELECT pid/execution_mode/agent_task_arn — pid=None suppresses SIGKILL,
        # but the agent_id recording happens regardless.
        conn.cursor_instance.fetch_queue = [(None,)]

        d._consume_commands()

        assert agent_id in d._force_stopped_agent_ids

    def test_per_agent_force_stop_ecs_mode_records_agent_id(self) -> None:
        """ECS-mode force_stop (no SIGKILL path) still records the id
        so the instrumentation isn't subprocess-only."""
        d, conn, _handler = _make_daemon()
        d._force_stop_ecs_task = MagicMock()  # type: ignore[method-assign]
        agent_id = str(uuid.uuid4())
        conn.cursor_instance.fetchall_queue = [
            [_command_row(1, "force_stop", {"agentId": agent_id})]
        ]
        conn.cursor_instance.fetch_queue = [(None, "ecs", "arn:aws:ecs:...:task/x")]

        d._consume_commands()

        assert agent_id in d._force_stopped_agent_ids

    def test_global_force_stop_does_not_touch_agent_id_set(self) -> None:
        """Global force_stop has no agentId — nothing to record."""
        d, conn, _handler = _make_daemon()
        conn.cursor_instance.fetchall_queue = [[_command_row(1, "force_stop")]]
        d._consume_commands()
        assert d._force_stopped_agent_ids == set()


class TestOrchestrationInProgressDumpsTraceback:
    """The first ``orchestration_in_progress`` after a
    ``force_stop_signal_sent`` dumps all-thread tracebacks so the next
    wedge self-diagnoses."""

    def test_no_force_stop_no_dump(self) -> None:
        """Without a force-stopped agent id recorded, the normal
        ``orchestration_in_progress`` path fires and nothing more."""
        d, _conn, handler = _make_daemon()
        live = threading.Thread(
            target=lambda: time.sleep(0.5), name="orchestration-test-live"
        )
        live.start()
        try:
            d._orchestration_thread = live
            # The spawn helper observes an alive thread and declines; no dump.
            assert d._maybe_spawn_orchestration_thread() is False
            assert handler.events("orchestration_in_progress") != []
            assert handler.events("orchestration_thread_stuck_after_force_stop") == []
        finally:
            live.join(timeout=2)

    def test_force_stop_triggers_faulthandler_dump_once(self) -> None:
        """With a recorded force_stop, the next alive-thread observation
        emits the one-shot stuck event."""
        d, _conn, handler = _make_daemon()
        d._force_stopped_agent_ids.add(str(uuid.uuid4()))

        live = threading.Thread(
            target=lambda: time.sleep(1.0),
            name="orchestration-wedged-test",
        )
        live.start()
        try:
            d._orchestration_thread = live

            # First observation: stuck event fires (once).
            assert d._maybe_spawn_orchestration_thread() is False
            assert handler.events("orchestration_in_progress") != []
            stuck = handler.events("orchestration_thread_stuck_after_force_stop")
            assert len(stuck) == 1
            # The captured traceback should be a non-empty string.
            # ``faulthandler.dump_traceback`` emits per-thread frames
            # (identified by hex thread-id, not Python name) so we only
            # assert the frame-header shape + the recorded force-stopped
            # agent_ids on the event record itself.
            tb = getattr(stuck[0], "traceback", None)
            assert isinstance(tb, str) and len(tb) > 0
            assert "Thread 0x" in tb or "Current thread 0x" in tb
            assert getattr(stuck[0], "thread_name", None) == (
                "orchestration-wedged-test"
            )
            recorded = getattr(stuck[0], "force_stopped_agent_ids", None)
            assert isinstance(recorded, list) and len(recorded) == 1

            # Second observation while the same thread is still alive:
            # stuck event does NOT re-fire (noise bound).
            assert d._maybe_spawn_orchestration_thread() is False
            assert (
                len(handler.events("orchestration_thread_stuck_after_force_stop")) == 1
            )
        finally:
            live.join(timeout=2)

    def test_fresh_thread_resets_dump_flag(self) -> None:
        """When the wedged thread is cleared and a new one spawns, the
        next force_stop + wedge combination dumps again.

        The recovery path is:

        * Phase 1: thread alive + force_stop recorded → dump #1 fires,
          the one-shot flag is set.
        * Orphan guard clears the handle (``_orchestration_thread = None``).
        * Phase 2: next ``_maybe_spawn_orchestration_thread`` sees a
          ``None`` handle and spawns a fresh worker — the spawn path
          is what clears the one-shot flag + the recorded agent_ids
          (a successful spawn means the wedge is resolved).
        * Phase 3: a NEW force_stop followed by a NEW wedge on the
          fresh thread must produce a second dump.
        """
        d, _conn, handler = _make_daemon()
        d._force_stopped_agent_ids.add(str(uuid.uuid4()))

        # Phase 1: wedged thread → dump fires once.
        first = threading.Thread(
            target=lambda: time.sleep(0.2), name="orchestration-first"
        )
        first.start()
        try:
            d._orchestration_thread = first
            d._maybe_spawn_orchestration_thread()
            assert (
                len(handler.events("orchestration_thread_stuck_after_force_stop")) == 1
            )
            assert d._force_stop_wedge_dump_emitted is True
        finally:
            first.join(timeout=2)

        # Orphan guard clears the handle — simulated here. In production,
        # ``scheduler_tick`` sets ``_orchestration_thread = None`` on the
        # N-th consecutive orphan tick.
        d._orchestration_thread = None

        # Phase 2: stub the worker entry so the fresh spawn returns
        # immediately without trying to open a psycopg connection.
        # The spawn path is what clears the one-shot flag.
        d._orchestration_worker_entry = lambda: None  # type: ignore[method-assign]
        assert d._maybe_spawn_orchestration_thread() is True
        # Joining the fresh thread keeps the test deterministic.
        fresh = d._orchestration_thread
        assert fresh is not None
        fresh.join(timeout=2)

        # Spawn cleared the flag + the recorded ids.
        assert d._force_stop_wedge_dump_emitted is False
        assert d._force_stopped_agent_ids == set()

        # Phase 3: fresh force_stop + fresh wedge (another live thread
        # handle) must fire a second dump.
        d._force_stopped_agent_ids.add(str(uuid.uuid4()))
        second = threading.Thread(
            target=lambda: time.sleep(0.2), name="orchestration-second"
        )
        second.start()
        try:
            d._orchestration_thread = second
            d._maybe_spawn_orchestration_thread()
            events = handler.events("orchestration_thread_stuck_after_force_stop")
            assert len(events) == 2, (
                "a fresh wedged thread after an orphan recovery must re-trigger "
                "the faulthandler dump"
            )
        finally:
            second.join(timeout=2)
