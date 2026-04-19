"""Unit tests for the #2847 killswitch — concurrency_cap propagation.

Covers the three acceptance criteria from issue #2847:

1. Cap=0 takes effect within ≤60s of DB commit under any daemon state
   (idle, orchestrating, spawning). Verified here by asserting that
   :meth:`DispatcherDaemon.scheduler_tick` sets
   :attr:`DispatcherDaemon._pause_requested` when cap=0 is observed,
   and that an in-flight orchestration thread observes the event.

2. ``scheduler_tick`` cadence ≤60s wall-clock worst-case. Verified here
   by a "concurrent orchestration" test that starts a fake worker
   thread holding the claim, then calls ``scheduler_tick`` from the
   main thread and confirms it returns promptly without waiting for
   the worker.

3. Synthetic test: flip cap mid-claim, assert the next scheduler tick
   observes the new value AND no new claim succeeds. Verified here by
   :meth:`test_mid_claim_cap_flip_is_observed_next_tick` and
   :meth:`test_mid_claim_cap_flip_blocks_new_claim`.

Structure mirrors ``test_daemon_phase3a.py`` — mocked psycopg, fake
connection + cursor, capturing log handler. No real DB or subprocess
is exercised.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# Provide a stub ``psycopg`` module before importing the daemon. Same
# pattern as test_daemon_phase3a.py — the daemon's ``_atomic_claim``
# catches ``psycopg.errors.UniqueViolation`` so that must be a real
# Exception subclass. We also need ``psycopg.connect`` to return a
# controllable mock so the worker thread entry can be exercised without
# opening a real DB connection.


class _UniqueViolation(Exception):
    """Stand-in for real psycopg.errors.UniqueViolation."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules["psycopg"] = _psycopg_stub

import psycopg  # noqa: E402,F401  — re-import after stub install

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — minimal copy of test_daemon_phase3a.py, focused on the
# single-cursor, single-connection pattern used by scheduler_tick.
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


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.killswitch.{id(tmp_path)}")
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
    # Stub the queue scan and blocked scan so scheduler_tick does not
    # attempt ``gh`` subprocess calls.
    d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
    d._scan_blocked_and_snapshot = lambda: 0  # type: ignore[method-assign]
    return d, conn, handler


# --------------------------------------------------------------------------
# Observation + pause event state
# --------------------------------------------------------------------------


class TestCapObservation:
    """``scheduler_tick`` records the cap it observed + updates the pause event."""

    def test_cap_observed_is_updated_each_tick(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=False
        )
        summary = d.scheduler_tick()
        assert summary["concurrency_cap"] == 1
        assert d._last_cap_observed == 1
        assert d._last_cap_observed_monotonic is not None

    def test_cap_zero_sets_pause_requested(self, tmp_path: Path) -> None:
        """The killswitch must engage within a single tick of cap=0."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("spawn must not fire at cap=0")
        )
        assert not d._pause_requested.is_set()
        d.scheduler_tick()
        assert d._pause_requested.is_set()
        assert d._last_cap_observed == 0
        # ``killswitch_engaged`` is logged on the 0→1 transition so
        # operators see exactly when the killswitch engaged.
        assert handler.events("killswitch_engaged") != []

    def test_cap_positive_clears_pause_requested(self, tmp_path: Path) -> None:
        """cap>0 on a later tick must clear pause — allow new orchestration."""
        d, conn, handler = _make_daemon(tmp_path)
        # Pre-set pause from a prior hypothetical cap=0 tick.
        d._pause_requested.set()
        conn.cursor_instance.fetch_queue = [(1,), None]
        d._maybe_spawn_orchestration_thread = MagicMock(  # type: ignore[method-assign]
            return_value=True
        )
        d.scheduler_tick()
        assert not d._pause_requested.is_set()
        # ``killswitch_cleared`` fires exactly once per transition.
        assert len(handler.events("killswitch_cleared")) == 1

    def test_cap_zero_logs_killswitch_engaged_once_per_transition(
        self, tmp_path: Path
    ) -> None:
        """Two successive cap=0 ticks emit ``killswitch_engaged`` only once."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,), (0,)]
        d._maybe_spawn_orchestration_thread = MagicMock()  # type: ignore[method-assign]
        d.scheduler_tick()
        d.scheduler_tick()
        assert len(handler.events("killswitch_engaged")) == 1

    def test_cap_none_does_not_engage_killswitch(self, tmp_path: Path) -> None:
        """A missing cap row leaves ``_pause_requested`` untouched (fail-open)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        d._maybe_spawn_orchestration_thread = MagicMock()  # type: ignore[method-assign]
        d.scheduler_tick()
        assert not d._pause_requested.is_set()
        assert d._last_cap_observed is None


# --------------------------------------------------------------------------
# Cadence decoupling: scheduler_tick does not block on orchestration
# --------------------------------------------------------------------------


class TestSchedulerCadenceDecoupling:
    """scheduler_tick must NOT wait for the orchestration worker thread.

    This is the AC-2 smoking gun — the whole reason #2847 exists. Before
    the fix, ``_claim_and_orchestrate_one`` ran inline and blocked the
    tick for up to 180 minutes. After the fix, the spawn path returns
    in ~ms and the next tick can re-observe the cap on schedule.
    """

    def test_scheduler_tick_returns_quickly_even_with_long_running_worker(
        self, tmp_path: Path
    ) -> None:
        """A worker thread that sleeps must not delay ``scheduler_tick``."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,), None]

        # Install a slow orchestration target so the worker thread will
        # still be running when we assert. The thread is daemon=True, so
        # it dies with the test process even if we don't join it.
        worker_started = threading.Event()
        worker_blocker = threading.Event()

        def slow_claim() -> None:
            worker_started.set()
            # Block until the test explicitly releases us — or a
            # worst-case 5s safety timeout.
            worker_blocker.wait(timeout=5.0)

        d._claim_and_orchestrate_one = slow_claim  # type: ignore[method-assign]

        start = time.monotonic()
        summary = d.scheduler_tick()
        elapsed = time.monotonic() - start

        try:
            # The tick itself must complete in well under 60s (AC-2).
            # 1s is a generous ceiling for a thread spawn + DB stub work
            # — real target is <100ms, but CI runners vary.
            assert elapsed < 1.0, (
                f"scheduler_tick took {elapsed:.3f}s with a blocked worker"
            )
            # The worker thread did start — verifies the spawn ran.
            assert worker_started.wait(timeout=2.0), (
                "orchestration worker did not start"
            )
            assert summary["orchestration_thread_alive"] == 1
        finally:
            worker_blocker.set()
            # Join best-effort so the worker does not leak into the next
            # test. daemon=True would let it die anyway.
            if d._orchestration_thread is not None:
                d._orchestration_thread.join(timeout=2.0)

    def test_second_tick_while_worker_alive_skips_spawn(self, tmp_path: Path) -> None:
        """While a worker thread is alive, a second tick must not spawn a second."""
        d, conn, handler = _make_daemon(tmp_path)
        # Two scheduler ticks, each with: cap=1, is_paused=None, has_active=None.
        # The second tick's gate passes but _maybe_spawn_orchestration_thread
        # notices the thread is still alive and skips.
        conn.cursor_instance.fetch_queue = [(1,), None, None, (1,), None, None]

        worker_blocker = threading.Event()

        def slow_claim() -> None:
            worker_blocker.wait(timeout=5.0)

        d._claim_and_orchestrate_one = slow_claim  # type: ignore[method-assign]

        # First tick spawns the thread.
        summary1 = d.scheduler_tick()
        assert summary1["orchestration_attempted"] == 1
        first_thread = d._orchestration_thread

        # Second tick with the worker still alive must NOT spawn a
        # second thread — logs ``orchestration_in_progress`` instead.
        try:
            # Small pause so the first thread is definitely ``is_alive()``.
            time.sleep(0.02)
            summary2 = d.scheduler_tick()
            assert summary2["orchestration_attempted"] == 0
            assert summary2["orchestration_thread_alive"] == 1
            assert handler.events("orchestration_in_progress") != []
            # Same thread object — no swap happened.
            assert d._orchestration_thread is first_thread
        finally:
            worker_blocker.set()
            if d._orchestration_thread is not None:
                d._orchestration_thread.join(timeout=2.0)


# --------------------------------------------------------------------------
# Worker-thread entry: exceptions captured, DB conn closed in finally
# --------------------------------------------------------------------------


class TestOrchestrationWorkerThread:
    """``_orchestration_worker_entry`` wraps the claim with proper plumbing.

    Note on the psycopg stub: other test files in this directory
    (e.g. test_daemon_phase3a.py) install their own psycopg MagicMock
    into ``sys.modules["psycopg"]`` at module import time. Because the
    daemon imports psycopg lazily inside each path, the stub that is
    actually called is whichever one is currently in
    ``sys.modules["psycopg"]`` — not necessarily the one this file
    installed. These tests therefore read ``sys.modules["psycopg"]``
    at runtime and install a fresh controllable stub for each test.
    """

    def test_worker_opens_and_closes_its_own_conn(self, tmp_path: Path) -> None:
        """The worker thread must not share ``self._conn`` with the main thread."""
        d, _conn, _handler = _make_daemon(tmp_path)

        thread_conn = MagicMock()
        thread_conn.close = MagicMock()
        live_stub = sys.modules["psycopg"]
        live_stub.connect.reset_mock()  # type: ignore[union-attr]
        live_stub.connect.return_value = thread_conn  # type: ignore[union-attr]

        observed_thread_conn: list[Any] = []

        def fake_claim() -> None:
            # When the worker runs, ``self._conn`` must resolve via the
            # thread-local to the worker's own connection — not the
            # main thread's.
            observed_thread_conn.append(d._conn)

        d._claim_and_orchestrate_one = fake_claim  # type: ignore[method-assign]

        d._orchestration_worker_entry()

        # The worker opened + closed its own connection exactly once.
        assert live_stub.connect.call_count == 1  # type: ignore[union-attr]
        thread_conn.close.assert_called_once()
        # The worker saw its own conn inside ``_claim_and_orchestrate_one``.
        assert len(observed_thread_conn) == 1
        assert observed_thread_conn[0] is thread_conn
        # After the worker exits, the main thread sees its original conn
        # again — no leak across the thread-local.
        assert d._conn is not thread_conn

    def test_worker_exception_is_logged_and_does_not_leak(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        thread_conn = MagicMock()
        thread_conn.close = MagicMock()
        live_stub = sys.modules["psycopg"]
        live_stub.connect.reset_mock()  # type: ignore[union-attr]
        live_stub.connect.return_value = thread_conn  # type: ignore[union-attr]

        d._claim_and_orchestrate_one = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom in claim")
        )

        # The entry point catches the exception — must not raise.
        d._orchestration_worker_entry()

        assert handler.events("orchestration_worker_failed") != []
        # Connection was still closed in the ``finally`` block.
        thread_conn.close.assert_called_once()


# --------------------------------------------------------------------------
# Mid-pipeline abort when pause is requested
# --------------------------------------------------------------------------


class TestOrchestrationPhasePause:
    """``_run_orchestration_phases`` aborts at the next phase on pause."""

    def test_pause_before_plan_marks_terminal_without_running_phases(
        self, tmp_path: Path
    ) -> None:
        """Cap=0 observed before plan → no phase helpers run; agent failed."""
        d, _conn, handler = _make_daemon(tmp_path)
        d._pause_requested.set()

        plan_mock = MagicMock(return_value=True)
        ralph_mock = MagicMock(return_value=True)
        summary_mock = MagicMock(return_value=True)
        push_mock = MagicMock()
        terminal_mock = MagicMock()
        d._run_plan_phase = plan_mock  # type: ignore[method-assign]
        d._run_ralph_phase = ralph_mock  # type: ignore[method-assign]
        d._run_summary_phase = summary_mock  # type: ignore[method-assign]
        d._push_and_open_pr = push_mock  # type: ignore[method-assign]
        d._mark_agent_terminal = terminal_mock  # type: ignore[method-assign]

        d._run_orchestration_phases("agent-xxx", 42, tmp_path)

        # No phase helper was invoked.
        assert plan_mock.call_count == 0
        assert ralph_mock.call_count == 0
        assert summary_mock.call_count == 0
        assert push_mock.call_count == 0
        # Agent marked failed with the killswitch terminal phase.
        terminal_mock.assert_called_once_with(
            "agent-xxx",
            status="failed",
            phase=daemon.KILLSWITCH_TERMINAL_PHASE,
            exit_code=None,
        )
        # Structured event logged so CloudWatch can confirm the abort.
        events = handler.events("orchestration_paused")
        assert len(events) == 1
        assert events[0].__dict__.get("after_phase") == "claiming"

    def test_pause_after_plan_skips_ralph_summary_push(self, tmp_path: Path) -> None:
        """Cap=0 flipped during plan → ralph/summary/push are skipped."""
        d, _conn, handler = _make_daemon(tmp_path)

        ralph_mock = MagicMock(return_value=True)
        summary_mock = MagicMock(return_value=True)
        push_mock = MagicMock()
        terminal_mock = MagicMock()
        d._run_ralph_phase = ralph_mock  # type: ignore[method-assign]
        d._run_summary_phase = summary_mock  # type: ignore[method-assign]
        d._push_and_open_pr = push_mock  # type: ignore[method-assign]
        d._mark_agent_terminal = terminal_mock  # type: ignore[method-assign]

        # Plan succeeds, then the killswitch fires before ralph.
        plan_call_count = {"count": 0}

        def plan_and_set_pause(*_args: Any, **_kwargs: Any) -> bool:
            plan_call_count["count"] += 1
            d._pause_requested.set()
            return True

        d._run_plan_phase = plan_and_set_pause  # type: ignore[method-assign]

        d._run_orchestration_phases("agent-xxx", 42, tmp_path)

        assert plan_call_count["count"] == 1
        # Ralph / summary / push must NOT have run.
        assert ralph_mock.call_count == 0
        assert summary_mock.call_count == 0
        assert push_mock.call_count == 0
        terminal_mock.assert_called_once_with(
            "agent-xxx",
            status="failed",
            phase=daemon.KILLSWITCH_TERMINAL_PHASE,
            exit_code=None,
        )
        events = handler.events("orchestration_paused")
        assert len(events) == 1
        assert events[0].__dict__.get("after_phase") == "planning"

    def test_no_pause_runs_all_phases(self, tmp_path: Path) -> None:
        """Smoke test: pause event clear → full pipeline runs."""
        d, _conn, _handler = _make_daemon(tmp_path)

        plan_mock = MagicMock(return_value=True)
        ralph_mock = MagicMock(return_value=True)
        summary_mock = MagicMock(return_value=True)
        push_mock = MagicMock()
        d._run_plan_phase = plan_mock  # type: ignore[method-assign]
        d._run_ralph_phase = ralph_mock  # type: ignore[method-assign]
        d._run_summary_phase = summary_mock  # type: ignore[method-assign]
        d._push_and_open_pr = push_mock  # type: ignore[method-assign]

        d._run_orchestration_phases("agent-xxx", 42, tmp_path)
        assert plan_mock.call_count == 1
        assert ralph_mock.call_count == 1
        assert summary_mock.call_count == 1
        assert push_mock.call_count == 1


# --------------------------------------------------------------------------
# The synthetic killswitch scenario from issue #2847 acceptance criteria
# --------------------------------------------------------------------------


class TestSyntheticMidClaimCapFlip:
    """AC-3: flip cap mid-claim, next tick observes AND no new claim succeeds."""

    def test_mid_claim_cap_flip_is_observed_next_tick(self, tmp_path: Path) -> None:
        """Tick sequence: cap=1 → worker starts → cap=0 → next tick sees 0."""
        d, conn, handler = _make_daemon(tmp_path)
        # Tick 1: cap read = 1 (spawns worker). Tick 2: cap read = 0.
        # Note: the ``_is_paused`` and ``_has_active_agent`` checks fire after
        # the cap read on tick 1; on tick 2 cap=0 short-circuits both.
        # is_paused=None (not paused), has_active_agent=None (no active agent).
        conn.cursor_instance.fetch_queue = [(1,), None, None, (0,)]

        worker_blocker = threading.Event()

        def slow_claim() -> None:
            worker_blocker.wait(timeout=5.0)

        d._claim_and_orchestrate_one = slow_claim  # type: ignore[method-assign]

        try:
            summary1 = d.scheduler_tick()
            assert summary1["concurrency_cap"] == 1
            assert summary1["orchestration_attempted"] == 1
            assert not d._pause_requested.is_set()

            # Operator flips cap=0 in the DB — simulated by the next
            # fetch_queue entry. Scheduler tick 2 must observe it.
            summary2 = d.scheduler_tick()
            assert summary2["concurrency_cap"] == 0
            assert d._pause_requested.is_set()
            # The in-flight worker sees the pause event (not joined
            # here — the phase-boundary abort path is covered by
            # :class:`TestOrchestrationPhasePause`).
        finally:
            worker_blocker.set()
            if d._orchestration_thread is not None:
                d._orchestration_thread.join(timeout=2.0)

    def test_mid_claim_cap_flip_blocks_new_claim(self, tmp_path: Path) -> None:
        """Once cap=0, subsequent ticks must not attempt to spawn a new claim."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Three ticks: cap=1 (spawn), cap=0 (pause), cap=0 (still pause).
        # Thread exits between ticks 1 and 3 (we force this by making
        # the worker return immediately). Tick 3's job is to prove that
        # even with no worker alive, cap=0 still blocks the spawn.
        conn.cursor_instance.fetch_queue = [
            (1,),
            None,  # _is_paused on tick 1
            None,  # _has_active_agent on tick 1
            (0,),  # tick 2
            (0,),  # tick 3
        ]

        spawn_count = {"count": 0}
        real_spawn = d._maybe_spawn_orchestration_thread

        def counting_spawn() -> bool:
            spawn_count["count"] += 1
            return real_spawn()

        d._maybe_spawn_orchestration_thread = counting_spawn  # type: ignore[method-assign]

        d._claim_and_orchestrate_one = lambda: None  # type: ignore[method-assign]

        try:
            d.scheduler_tick()  # cap=1 → spawn #1
            # Let the trivial worker finish.
            if d._orchestration_thread is not None:
                d._orchestration_thread.join(timeout=2.0)

            d.scheduler_tick()  # cap=0 → must NOT spawn
            d.scheduler_tick()  # cap=0 → must NOT spawn
        finally:
            if d._orchestration_thread is not None:
                d._orchestration_thread.join(timeout=2.0)

        # Exactly one spawn attempt total — the cap=1 tick.
        assert spawn_count["count"] == 1

    def test_worker_aborts_at_next_phase_when_pause_set_mid_run(
        self, tmp_path: Path
    ) -> None:
        """Full integration: spawn worker → flip cap → worker aborts cleanly."""
        d, _conn, handler = _make_daemon(tmp_path)

        # Scenario: the worker finishes plan successfully, then the
        # scheduler on the main thread sets ``_pause_requested`` (the
        # effect of a cap=0 tick). Ralph/summary/push must be skipped.
        plan_ran = threading.Event()
        plan_release = threading.Event()

        def slow_plan(*_args: Any, **_kwargs: Any) -> bool:
            plan_ran.set()
            # Wait for the "cap flip" signal before returning from plan.
            plan_release.wait(timeout=5.0)
            return True

        ralph_mock = MagicMock(return_value=True)
        summary_mock = MagicMock(return_value=True)
        push_mock = MagicMock()
        terminal_mock = MagicMock()

        d._run_plan_phase = slow_plan  # type: ignore[method-assign]
        d._run_ralph_phase = ralph_mock  # type: ignore[method-assign]
        d._run_summary_phase = summary_mock  # type: ignore[method-assign]
        d._push_and_open_pr = push_mock  # type: ignore[method-assign]
        d._mark_agent_terminal = terminal_mock  # type: ignore[method-assign]

        worker = threading.Thread(
            target=d._run_orchestration_phases,
            args=("agent-xxx", 42, tmp_path),
        )
        worker.start()
        try:
            assert plan_ran.wait(timeout=2.0), "plan did not start"
            # Simulate the scheduler observing cap=0 on the main thread.
            d._pause_requested.set()
            # Release plan so the worker advances to the post-plan
            # killswitch check.
            plan_release.set()
            worker.join(timeout=5.0)
            assert not worker.is_alive(), "worker did not exit after pause"

            # Ralph / summary / push skipped.
            assert ralph_mock.call_count == 0
            assert summary_mock.call_count == 0
            assert push_mock.call_count == 0
            # Agent marked terminal.
            terminal_mock.assert_called_once_with(
                "agent-xxx",
                status="failed",
                phase=daemon.KILLSWITCH_TERMINAL_PHASE,
                exit_code=None,
            )
            assert handler.events("orchestration_paused") != []
        finally:
            plan_release.set()
            worker.join(timeout=2.0)


# --------------------------------------------------------------------------
# Thread-aware self._conn property
# --------------------------------------------------------------------------


class TestThreadLocalConnProperty:
    """The ``_conn`` property resolves to the thread-local conn when set."""

    def test_main_thread_sees_main_conn(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        assert d._conn is conn

    def test_worker_thread_sees_its_own_conn(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        worker_conn = MagicMock(name="worker_conn")
        observed: dict[str, Any] = {}

        def worker() -> None:
            d._thread_state.conn = worker_conn
            observed["thread_conn"] = d._conn

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        # Worker saw its thread-local conn.
        assert observed["thread_conn"] is worker_conn
        # Main thread still sees the main conn — no bleed.
        assert d._conn is conn

    def test_setter_assigns_to_main_conn_only(self, tmp_path: Path) -> None:
        """``d._conn = x`` always targets the main thread's slot."""
        d, _conn, _handler = _make_daemon(tmp_path)
        new_conn = MagicMock()
        d._conn = new_conn  # type: ignore[assignment]
        assert d._main_conn is new_conn
        # Thread-local untouched.
        assert getattr(d._thread_state, "conn", None) is None
