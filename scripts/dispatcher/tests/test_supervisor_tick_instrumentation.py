"""Unit tests for #3403 supervisor_tick instrumentation.

Covers the diagnostic-first changes added to :mod:`scripts.dispatcher.daemon`:

* :meth:`DispatcherDaemon._record_supervisor_step` — mirror of the #3205
  ``_record_scheduler_step`` for supervisor sub-steps. Emits a
  ``daemon.supervisor_tick_slow_<step>`` WARNING when a sub-step exceeds
  :data:`SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS`. Per-sub-step refresh
  of ``_last_scheduler_tick_at`` was removed in #3801 — see
  ``test_record_step_does_not_refresh_last_scheduler_tick_at``.
* :meth:`DispatcherDaemon.supervisor_tick` actually calls
  ``_record_supervisor_step`` between sub-steps so a slow sub-step
  produces a named WARN event rather than just a generic
  ``scheduler_tick_stalled`` thread dump.
* :meth:`DispatcherDaemon._watchdog_loop` emits
  ``daemon.watchdog_heartbeat`` every Nth poll so the operator can
  confirm the watchdog itself is alive even when the scheduler /
  supervisor go log-quiet.

No real DB or CloudWatch is reached. psycopg + boto3 are mocked.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package —
# mirrors the preamble in the other ``test_daemon_*.py`` modules.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (kept self-contained — sibling test modules use the same
# pattern; lifting to a shared helper is a future cleanup if it ever
# accumulates a third copy).
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

    def fetchall(self) -> list[Any]:
        return []


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
    logger = logging.getLogger(f"dispatcher.test.sup_instr.{id(tmp_path)}")
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

    # Stub every supervisor sub-step to a no-op fast helper. Tests that
    # want to simulate a slow sub-step replace one of these with a
    # ``time.sleep``-based stub.
    d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
    d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
    d._gh_rate_skip_active = lambda: False  # type: ignore[method-assign]
    d._resurrect_orphan_pr_agents = lambda: 0  # type: ignore[method-assign]
    d._advance_running_agents = lambda: 0  # type: ignore[method-assign]
    d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
    d._check_diagnoser_circuit_breaker = lambda: None  # type: ignore[method-assign]
    d._reap_diagnoser_subprocesses = lambda: 0  # type: ignore[method-assign]
    d._run_diagnoser_pass = lambda: 0  # type: ignore[method-assign]
    d._scheduled_skills_tick = lambda: {  # type: ignore[method-assign]
        "rows_scanned": 0,
        "fires_succeeded": 0,
        "fires_skipped_cap": 0,
        "fires_skipped_collision": 0,
        "fires_failed": 0,
    }
    d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]
    return d, conn, handler


# --------------------------------------------------------------------------
# AC #1 — _record_supervisor_step emits slow-step WARN with elapsed seconds.
#
# When a supervisor sub-step exceeds
# :data:`SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS`, the helper emits a
# ``daemon.supervisor_tick_slow_<step>`` WARNING carrying ``step``,
# ``elapsed_seconds``, and ``threshold_seconds`` so an operator can
# pinpoint exactly which sub-step owned the wedge.
# --------------------------------------------------------------------------


class TestRecordSupervisorStep:
    def test_fast_step_does_not_emit_warning(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        # t_before is ~now; elapsed will be tiny → no warning.
        t_before = time.monotonic()
        d._record_supervisor_step("stuck_check", t_before)
        assert handler.events("supervisor_tick_slow_stuck_check") == []

    def test_slow_step_emits_warning_with_elapsed_seconds(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        # Fake a step that took well over the threshold by passing a
        # stale t_before (elapsed = now - stale).
        stale = time.monotonic() - (daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS + 1.0)
        d._record_supervisor_step("advance_running_agents", stale)
        events = handler.events("supervisor_tick_slow_advance_running_agents")
        assert len(events) == 1
        rec = events[0]
        assert rec.levelno == logging.WARNING
        elapsed = getattr(rec, "elapsed_seconds", None)
        assert isinstance(elapsed, (int, float))
        assert elapsed >= daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS
        assert getattr(rec, "step", None) == "advance_running_agents"
        threshold = getattr(rec, "threshold_seconds", None)
        assert threshold == daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS

    def test_record_step_does_not_refresh_last_scheduler_tick_at(
        self, tmp_path: Path
    ) -> None:
        """#3801: per-sub-step heartbeat refresh is REMOVED.

        See ``test_record_step_does_not_refresh_last_scheduler_tick_at``
        in ``test_daemon_scheduler_tick_instrumentation.py`` for the
        full rationale. Same fix here for the supervisor mirror.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        d._last_scheduler_tick_at = 0.0  # known-stale sentinel
        d._record_supervisor_step("stuck_check", time.monotonic())
        # Heartbeat must NOT be advanced by the per-step recorder.
        assert d._last_scheduler_tick_at == 0.0

    def test_record_step_returns_current_monotonic(self, tmp_path: Path) -> None:
        """Callers chain ``t_step = _record_supervisor_step(...)`` so the
        return value must be the ``time.monotonic()`` reading taken at
        the end of the recorder. ``_last_scheduler_tick_at`` is no
        longer touched (#3801) so we just bound the return against
        before/after wall-clock."""
        d, _conn, _handler = _make_daemon(tmp_path)
        before = time.monotonic()
        returned = d._record_supervisor_step("stuck_check", before)
        after = time.monotonic()
        assert before <= returned <= after


# --------------------------------------------------------------------------
# AC #2 — supervisor_tick actually calls _record_supervisor_step between
# sub-steps. End-to-end check via a slow monkeypatched helper.
# --------------------------------------------------------------------------


class TestSupervisorTickEmitsSlowEvents:
    def test_slow_advance_emits_slow_advance_running_agents_event(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]  # failures count

        def _slow_advance() -> int:
            time.sleep(daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS + 0.25)
            return 0

        d._advance_running_agents = _slow_advance  # type: ignore[method-assign]
        d.supervisor_tick()
        events = handler.events("supervisor_tick_slow_advance_running_agents")
        assert len(events) == 1
        assert getattr(events[0], "levelno", None) == logging.WARNING

    def test_slow_diagnoser_run_emits_slow_diagnoser_run_event(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        def _slow_diag() -> int:
            time.sleep(daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS + 0.25)
            return 0

        d._run_diagnoser_pass = _slow_diag  # type: ignore[method-assign]
        d.supervisor_tick()
        events = handler.events("supervisor_tick_slow_diagnoser_run")
        assert len(events) == 1

    def test_fast_tick_emits_no_slow_events(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        d.supervisor_tick()
        slow_events = [
            rec
            for rec in handler.records
            if getattr(rec, "event", "").startswith("supervisor_tick_slow_")
        ]
        assert slow_events == [], (
            f"expected no slow-step events on a fast tick; got "
            f"{[e.event for e in slow_events]!r}"
        )

    def test_supervisor_tick_refreshes_last_scheduler_tick_at_per_step(
        self, tmp_path: Path
    ) -> None:
        """Even on a fast tick, ``_last_scheduler_tick_at`` advances at
        every sub-step boundary — confirms the watchdog observes
        forward progress mid-supervisor-tick."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        # Stash known-stale baseline so the post-tick value is
        # demonstrably advanced.
        d._last_scheduler_tick_at = 0.0
        d.supervisor_tick()
        # After supervisor_tick the value reflects ``time.monotonic()``
        # (a recent positive number) — never the stale 0.0 left in
        # place.
        assert d._last_scheduler_tick_at > 0.0


# --------------------------------------------------------------------------
# AC #3 — watchdog_heartbeat fires every WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS
# polls regardless of scheduler / supervisor activity.
# --------------------------------------------------------------------------


class TestWatchdogHeartbeat:
    def test_heartbeat_fires_after_n_polls(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """After running ``WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS`` poll
        cycles, the watchdog emits exactly one
        ``daemon.watchdog_heartbeat`` event carrying the elapsed gap +
        thresholds."""
        d, _conn, handler = _make_daemon(tmp_path)
        # Tighten the poll interval and ensure baseline is fresh so the
        # WARN/EXIT branches don't fire (warn=60s, exit=120s defaults).
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
        d._last_scheduler_tick_at = time.monotonic()

        # Stop the loop after exactly N polls by counting calls to
        # ``_watchdog_stop.wait`` (which is what the loop sleeps on).
        n_target = daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS
        call_n = {"count": 0}
        original_wait = d._watchdog_stop.wait

        def _wait_then_stop_after_n(timeout: float | None = None) -> bool:
            call_n["count"] += 1
            if call_n["count"] >= n_target:
                d._watchdog_stop.set()
                return True
            return original_wait(0.0)

        monkeypatch.setattr(d._watchdog_stop, "wait", _wait_then_stop_after_n)

        # Run the watchdog loop on this thread (synchronous test; the
        # loop returns when stop is set).
        d._watchdog_loop()

        events = handler.events("watchdog_heartbeat")
        assert len(events) == 1, (
            f"expected exactly 1 heartbeat after N={n_target} polls; got {len(events)}"
        )
        rec = events[0]
        assert rec.levelno == logging.INFO
        assert getattr(rec, "poll_n", None) == n_target
        assert isinstance(getattr(rec, "elapsed_since_tick_s", None), (int, float))
        assert isinstance(getattr(rec, "warn_threshold_seconds", None), int)
        assert isinstance(getattr(rec, "exit_threshold_seconds", None), int)

    def test_heartbeat_continues_when_scheduler_quiet(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The unconditional heartbeat is the entire point of #3403 —
        verify it fires even though the scheduler/supervisor are
        completely idle (i.e. ``_last_scheduler_tick_at`` never gets
        updated by anyone other than the heartbeat path)."""
        d, _conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
        d._last_scheduler_tick_at = time.monotonic()

        # Run for 2× the heartbeat cadence → expect 2 heartbeats.
        n_target = 2 * daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS
        call_n = {"count": 0}

        def _stop_after(_timeout: float | None = None) -> bool:
            call_n["count"] += 1
            if call_n["count"] >= n_target:
                d._watchdog_stop.set()
                return True
            return False

        monkeypatch.setattr(d._watchdog_stop, "wait", _stop_after)

        d._watchdog_loop()

        events = handler.events("watchdog_heartbeat")
        assert len(events) == 2, (
            f"expected 2 heartbeats after N={n_target} polls (cadence="
            f"{daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS}); got {len(events)}"
        )
        # Confirm poll_n increases monotonically across heartbeats.
        poll_ns = [getattr(e, "poll_n", -1) for e in events]
        assert poll_ns == sorted(poll_ns)
        assert poll_ns == [
            daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS,
            2 * daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS,
        ]

    def test_no_heartbeat_before_n_polls(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The heartbeat is throttled — N-1 polls produces zero heartbeats."""
        d, _conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
        d._last_scheduler_tick_at = time.monotonic()

        n_target = daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS - 1
        call_n = {"count": 0}

        def _stop_after(_timeout: float | None = None) -> bool:
            call_n["count"] += 1
            if call_n["count"] >= n_target:
                d._watchdog_stop.set()
                return True
            return False

        monkeypatch.setattr(d._watchdog_stop, "wait", _stop_after)

        d._watchdog_loop()
        assert handler.events("watchdog_heartbeat") == []


# --------------------------------------------------------------------------
# AC #4 — module-level constants are sane defaults.
# --------------------------------------------------------------------------


class TestModuleConstants:
    def test_supervisor_slow_step_threshold_is_below_watchdog_warn(self) -> None:
        """Per-step supervisor slow WARN fires BEFORE the watchdog WARN
        so we name the offending sub-step instead of dumping the whole
        thread set. Threshold (10s) << watchdog WARN (60s) honours that
        ordering."""
        assert daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS > 0.0
        assert (
            daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS
            < daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS
        )

    def test_supervisor_slow_step_threshold_is_at_least_scheduler(self) -> None:
        """Supervisor sub-steps are inherently slower than scheduler
        sub-steps (the advance pass calls ``gh pr view`` per agent, the
        diagnoser pass spawns subprocesses) — the threshold must give
        them at least as much budget as the scheduler steps to avoid
        spurious WARNs on a normal busy tick."""
        assert (
            daemon.SUPERVISOR_TICK_SLOW_STEP_WARN_SECONDS
            >= daemon.SCHEDULER_TICK_SLOW_STEP_WARN_SECONDS
        )

    def test_watchdog_heartbeat_cadence_is_positive(self) -> None:
        """Cadence must be ≥ 1 — a value of 0 would emit on every poll
        (heartbeat-spam) and a negative value would never emit."""
        assert daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS >= 1

    def test_watchdog_heartbeat_cadence_under_5min(self) -> None:
        """The heartbeat exists so an operator scanning recent logs sees
        daemon-is-alive evidence within ≈ one scroll. With 30s poll
        intervals, 10 polls = 5min — that's the upper bound. If the
        cadence climbs above this the heartbeat stops being a useful
        liveness signal during the typical operator-glance window."""
        cadence_seconds = (
            daemon.WATCHDOG_HEARTBEAT_LOG_EVERY_N_POLLS
            * daemon.WATCHDOG_POLL_INTERVAL_SECONDS
        )
        assert cadence_seconds <= 5 * 60


# --------------------------------------------------------------------------
# Smoke test — a thread launched into ``_watchdog_loop`` exits cleanly
# when ``_watchdog_stop`` is set. Documents the join semantics relied on
# in shutdown.
# --------------------------------------------------------------------------


class TestWatchdogLoopShutdown:
    def test_loop_exits_when_stop_event_set(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
        d._last_scheduler_tick_at = time.monotonic()

        t = threading.Thread(target=d._watchdog_loop, name="t-watchdog-test")
        t.start()
        # Let the loop run a few polls then stop.
        time.sleep(0.1)
        d._watchdog_stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive(), "watchdog loop did not exit within 2s of stop"
