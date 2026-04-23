"""Unit tests for the scheduler_tick stall watchdog (#3097).

The watchdog is a supervisor thread alongside the scheduler thread
that records ``time.monotonic()`` of each ``scheduler_tick`` entry to
``self._last_scheduler_tick_at`` and fires two tiers of alarm when the
gap exceeds configurable thresholds:

* ``SCHEDULER_TICK_STALL_WARN_SECONDS`` (default 300s) — WARNING
  ``scheduler_tick_stalled`` with a bounded thread dump. Debounced so
  duplicate warnings don't spam CloudWatch while the stall persists.
* ``SCHEDULER_TICK_STALL_EXIT_SECONDS`` (default 3000s) — ERROR
  ``scheduler_tick_stalled_exiting`` with a final thread dump followed
  by ``os._exit(137)`` so ECS restarts the task.

Tests cover:

* AC #1 — the scheduler_tick records ``_last_scheduler_tick_at``.
* AC #2 — WARNING fires at the WARN threshold with thread-dump output.
* AC #3 — ERROR + ``os._exit(137)`` fires at the EXIT threshold.
* AC #4 — config overrides via ``dispatcher.config.scheduler_tick_stall_*``
  seconds are honored.
* AC #5 — thread dump is bounded, contains thread names + frame info.
* AC #6 — WARNING debounce: doesn't re-fire while the stall persists;
  does re-fire after recovery.
* AC #7 — watchdog exits cleanly on ``_watchdog_stop``.

No real ``os._exit`` is ever called — the test monkeypatches it to a
sentinel-raising stub. No real Postgres is used — the watchdog's
``psycopg.connect`` is stubbed so it falls back to the defaults.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes
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


class _ExitCalled(BaseException):
    """Sentinel raised in place of ``os._exit`` during tests.

    BaseException (not Exception) so the watchdog loop's defensive
    ``except Exception`` does NOT catch it — matching the real
    ``os._exit`` semantics where nothing can catch the process death.
    """

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"os._exit({code})")


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.watchdog.{id(tmp_path)}")
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
    # Stub the queue scan so scheduler_tick does not touch ``gh``.
    d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
    d._scan_blocked_and_snapshot = lambda: 0  # type: ignore[method-assign]
    d._maybe_spawn_orchestration_thread = MagicMock(return_value=False)  # type: ignore[method-assign]
    return d, conn, handler


# --------------------------------------------------------------------------
# Module-level constants
# --------------------------------------------------------------------------


class TestModuleConstants:
    def test_warn_default_is_300(self) -> None:
        assert daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS == 300

    def test_exit_default_is_3000(self) -> None:
        assert daemon.DEFAULT_SCHEDULER_TICK_STALL_EXIT_SECONDS == 3000

    def test_exit_is_10x_warn(self) -> None:
        """#3097 design — exit threshold is 10× warn so a transient
        slowdown is caught loudly (WARN) but not escalated to a
        process restart until the gap becomes catastrophic."""
        assert (
            daemon.DEFAULT_SCHEDULER_TICK_STALL_EXIT_SECONDS
            == 10 * daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS
        )

    def test_poll_interval_is_positive(self) -> None:
        assert daemon.WATCHDOG_POLL_INTERVAL_SECONDS > 0
        # Bounded reasonable window — at most once every 2 min.
        assert 10 <= daemon.WATCHDOG_POLL_INTERVAL_SECONDS <= 120

    def test_max_frames_per_thread_is_bounded(self) -> None:
        assert daemon.WATCHDOG_THREAD_DUMP_MAX_FRAMES_PER_THREAD > 0
        # Hard-bound so a deep stack cannot blow past CloudWatch's
        # 256KB log-event limit.
        assert daemon.WATCHDOG_THREAD_DUMP_MAX_FRAMES_PER_THREAD <= 100


# --------------------------------------------------------------------------
# AC #1 — scheduler_tick records _last_scheduler_tick_at
# --------------------------------------------------------------------------


class TestSchedulerTickRecordsTimestamp:
    def test_scheduler_tick_updates_last_scheduler_tick_at(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]

        before = time.monotonic()
        d._last_scheduler_tick_at = 0.0  # reset to a known-stale value
        d.scheduler_tick()
        after = time.monotonic()

        assert before <= d._last_scheduler_tick_at <= after

    def test_last_scheduler_tick_at_initialized_in_ctor(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        # Ctor seeds a fresh value so the watchdog's first observation
        # has a valid reference even before the first tick fires.
        assert isinstance(d._last_scheduler_tick_at, float)
        assert d._last_scheduler_tick_at > 0.0


# --------------------------------------------------------------------------
# AC #5 — thread dump formatting
# --------------------------------------------------------------------------


class TestThreadDumpFormat:
    def test_thread_dump_contains_current_thread(self) -> None:
        dump = daemon.DispatcherDaemon._format_thread_dump()
        assert isinstance(dump, list)
        assert len(dump) >= 1
        # Each entry has the expected shape.
        for entry in dump:
            assert "name" in entry
            assert "ident" in entry
            assert "daemon" in entry
            assert "alive" in entry
            assert "frames" in entry
            assert isinstance(entry["frames"], list)

    def test_thread_dump_frames_have_file_line_function(self) -> None:
        dump = daemon.DispatcherDaemon._format_thread_dump()
        # At least the current (main/test) thread has some frames.
        main = next((e for e in dump if e["frames"]), None)
        assert main is not None, "expected at least one thread with frames"
        first_frame = main["frames"][0]
        assert "file" in first_frame
        assert "line" in first_frame
        assert "function" in first_frame
        assert isinstance(first_frame["line"], int)
        assert isinstance(first_frame["function"], str)

    def test_thread_dump_respects_max_frames(self) -> None:
        """Frames are bounded per thread so CloudWatch row-size caps
        are respected even on a deep stack."""
        dump = daemon.DispatcherDaemon._format_thread_dump(max_frames_per_thread=2)
        for entry in dump:
            assert len(entry["frames"]) <= 2

    def test_thread_dump_contains_spawned_thread_by_name(self) -> None:
        """Named threads show up in the dump so the operator can
        identify which thread was wedged."""
        ready = threading.Event()
        done = threading.Event()

        def _worker() -> None:
            ready.set()
            done.wait(timeout=5.0)

        t = threading.Thread(target=_worker, name="test-wedge-dummy", daemon=True)
        t.start()
        try:
            assert ready.wait(timeout=2.0)
            dump = daemon.DispatcherDaemon._format_thread_dump()
            names = {entry["name"] for entry in dump}
            assert "test-wedge-dummy" in names
        finally:
            done.set()
            t.join(timeout=2.0)


# --------------------------------------------------------------------------
# AC #2 / AC #3 — WARN + EXIT tiers via the watchdog loop
#
# We test the helpers directly AND test the loop end-to-end by shrinking
# the poll interval and advancing monotonic() via a stub.
# --------------------------------------------------------------------------


class TestEmitHelpers:
    def test_emit_warning_logs_scheduler_tick_stalled(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        d._emit_scheduler_stall_warning(elapsed_seconds=350.0)
        events = handler.events("scheduler_tick_stalled")
        assert len(events) == 1
        rec = events[0]
        assert rec.levelno == logging.WARNING
        assert getattr(rec, "elapsed_seconds", None) == 350.0
        dump = getattr(rec, "thread_dump", None)
        assert isinstance(dump, list)
        assert len(dump) >= 1

    def test_emit_exit_logs_error_and_calls_os_exit(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_exit(code: int) -> None:
            raise _ExitCalled(code)

        monkeypatch.setattr(daemon.os, "_exit", fake_exit)
        with pytest.raises(_ExitCalled) as excinfo:
            d._emit_scheduler_stall_exit(elapsed_seconds=3100.0)
        assert excinfo.value.code == 137

        events = handler.events("scheduler_tick_stalled_exiting")
        assert len(events) == 1
        rec = events[0]
        assert rec.levelno == logging.ERROR
        assert getattr(rec, "elapsed_seconds", None) == 3100.0
        assert getattr(rec, "exit_code", None) == 137
        dump = getattr(rec, "thread_dump", None)
        assert isinstance(dump, list)


# --------------------------------------------------------------------------
# AC #4 — config overrides honored
# --------------------------------------------------------------------------


class TestConfigOverrides:
    def test_warn_threshold_reads_config_row(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # _cb_config_int does SELECT value FROM dispatcher.config WHERE key = %s
        conn.cursor_instance.fetch_queue = [(123,)]
        assert d._watchdog_warn_threshold() == 123

    def test_warn_threshold_falls_back_on_missing_row(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert (
            d._watchdog_warn_threshold()
            == daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS
        )

    def test_exit_threshold_reads_config_row(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(9999,)]
        assert d._watchdog_exit_threshold() == 9999

    def test_exit_threshold_falls_back_on_missing_row(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert (
            d._watchdog_exit_threshold()
            == daemon.DEFAULT_SCHEDULER_TICK_STALL_EXIT_SECONDS
        )

    def test_thresholds_fall_back_on_db_error(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The watchdog must survive a DB hiccup — _cb_config_int
        catches the exception and returns the default."""
        d, _conn, _handler = _make_daemon(tmp_path)

        class _BoomCursor(_FakeCursor):
            def execute(self, sql: str, params: Any = None) -> None:
                raise RuntimeError("db down")

        boom = _BoomCursor()

        class _BoomConn(_FakeConnection):
            def cursor(self) -> _BoomCursor:
                return boom

        d._conn = _BoomConn()  # type: ignore[assignment]
        assert (
            d._watchdog_warn_threshold()
            == daemon.DEFAULT_SCHEDULER_TICK_STALL_WARN_SECONDS
        )
        assert (
            d._watchdog_exit_threshold()
            == daemon.DEFAULT_SCHEDULER_TICK_STALL_EXIT_SECONDS
        )


# --------------------------------------------------------------------------
# End-to-end loop — the watchdog thread observes stall → WARN → EXIT
# --------------------------------------------------------------------------


class _TickClock:
    """Monotonic-time stub driven by the test."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def __call__(self) -> float:
        return self.value


class TestWatchdogLoopBehaviour:
    def _run_loop_in_thread(
        self,
        d: daemon.DispatcherDaemon,
    ) -> threading.Thread:
        t = threading.Thread(target=d._watchdog_loop, daemon=True)
        t.start()
        return t

    def _wait_for_event(
        self, handler: _CapturingLogHandler, name: str, timeout: float = 3.0
    ) -> logging.LogRecord | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = handler.events(name)
            if events:
                return events[-1]
            time.sleep(0.01)
        return None

    def test_warning_fires_when_elapsed_exceeds_warn_threshold(
        self, tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
    ) -> None:
        """AC #2 — a mocked monotonic that simulates a 301s stall
        against a 300s threshold triggers exactly one WARNING."""
        d, conn, handler = _make_daemon(tmp_path)

        # Shrink poll interval so the loop spins fast.
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)

        # Control time. Start at 1000.0; tick was at 1000.0. Then
        # advance to 1301.0 so elapsed = 301 > 300 (warn) but
        # < 3000 (exit).
        clock = _TickClock(start=1000.0)
        d._last_scheduler_tick_at = 1000.0
        monkeypatch.setattr(daemon.time, "monotonic", clock)

        # Stub _cb_config_int to return defaults — avoid DB-cursor
        # interaction ordering in the background thread.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_cb_config_int",
            lambda self, key, default: default,
        )

        # Stub psycopg.connect so the watchdog's DB setup path is inert.
        psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

        clock.advance(301.0)
        t = self._run_loop_in_thread(d)
        try:
            rec = self._wait_for_event(handler, "scheduler_tick_stalled")
            assert rec is not None, "expected a scheduler_tick_stalled WARNING"
            assert rec.levelno == logging.WARNING
            assert getattr(rec, "elapsed_seconds", 0) >= 300
        finally:
            d._watchdog_stop.set()
            t.join(timeout=2.0)
            assert not t.is_alive()

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_exit_fires_when_elapsed_exceeds_exit_threshold(
        self, tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
    ) -> None:
        """AC #3 — a 3001s stall against a 3000s threshold triggers
        ``os._exit(137)`` with an ERROR log.

        The fake ``os._exit`` raises ``_ExitCalled`` (a BaseException
        subclass) from the watchdog thread — pytest captures that as
        an "unhandled thread exception" warning. That is by design:
        real ``os._exit`` is uncatchable by any handler. The
        ``filterwarnings`` decorator suppresses the noise.
        """
        d, conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)

        clock = _TickClock(start=5000.0)
        d._last_scheduler_tick_at = 5000.0
        monkeypatch.setattr(daemon.time, "monotonic", clock)

        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_cb_config_int",
            lambda self, key, default: default,
        )
        psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

        exit_calls: list[int] = []

        def fake_exit(code: int) -> None:
            exit_calls.append(code)
            raise _ExitCalled(code)

        monkeypatch.setattr(daemon.os, "_exit", fake_exit)

        clock.advance(3001.0)
        t = self._run_loop_in_thread(d)
        try:
            rec = self._wait_for_event(handler, "scheduler_tick_stalled_exiting")
            assert rec is not None, "expected a scheduler_tick_stalled_exiting ERROR"
            assert rec.levelno == logging.ERROR
            assert getattr(rec, "exit_code", None) == 137
            # Wait for os._exit to be called.
            deadline = time.monotonic() + 2.0
            while not exit_calls and time.monotonic() < deadline:
                time.sleep(0.01)
            assert exit_calls == [137]
        finally:
            d._watchdog_stop.set()
            t.join(timeout=2.0)

    def test_no_warning_below_threshold(
        self, tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
    ) -> None:
        """A healthy tick cadence produces no WARN / EXIT events."""
        d, conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)

        clock = _TickClock(start=10.0)
        d._last_scheduler_tick_at = 10.0
        monkeypatch.setattr(daemon.time, "monotonic", clock)
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_cb_config_int",
            lambda self, key, default: default,
        )
        psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

        # Only advance 30s — well below the 300s WARN threshold.
        clock.advance(30.0)
        t = self._run_loop_in_thread(d)
        try:
            # Let the loop run a few polls.
            time.sleep(0.05)
            assert handler.events("scheduler_tick_stalled") == []
            assert handler.events("scheduler_tick_stalled_exiting") == []
        finally:
            d._watchdog_stop.set()
            t.join(timeout=2.0)

    def test_warning_debounces_while_stall_persists(
        self, tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
    ) -> None:
        """AC #6 — a persistent stall fires WARNING exactly once, not
        once per poll cycle."""
        d, conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)

        clock = _TickClock(start=1000.0)
        d._last_scheduler_tick_at = 1000.0
        monkeypatch.setattr(daemon.time, "monotonic", clock)
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_cb_config_int",
            lambda self, key, default: default,
        )
        psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

        # Advance into the WARN zone but not the EXIT zone.
        clock.advance(500.0)
        t = self._run_loop_in_thread(d)
        try:
            rec = self._wait_for_event(handler, "scheduler_tick_stalled")
            assert rec is not None
            # Let the loop cycle a few more times.
            time.sleep(0.05)
            # Exactly one WARNING, debounced.
            assert len(handler.events("scheduler_tick_stalled")) == 1
        finally:
            d._watchdog_stop.set()
            t.join(timeout=2.0)

    def test_warning_rearms_after_recovery(
        self, tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
    ) -> None:
        """AC #6 — after the tick recovers (gap drops below WARN),
        a subsequent stall fires a fresh WARNING."""
        d, conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)

        clock = _TickClock(start=1000.0)
        d._last_scheduler_tick_at = 1000.0
        monkeypatch.setattr(daemon.time, "monotonic", clock)
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_cb_config_int",
            lambda self, key, default: default,
        )
        psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

        # Step 1: stall → WARN.
        clock.advance(500.0)
        t = self._run_loop_in_thread(d)
        try:
            rec1 = self._wait_for_event(handler, "scheduler_tick_stalled")
            assert rec1 is not None

            # Step 2: "tick" — scheduler wakes up again. Advance the
            # last_tick_at to the current clock (simulates a real tick
            # writing the timestamp at the top of scheduler_tick).
            d._last_scheduler_tick_at = clock.value
            # Wait for the loop to observe recovery.
            deadline = time.monotonic() + 2.0
            while d._watchdog_warn_emitted_for_gap and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not d._watchdog_warn_emitted_for_gap

            # Step 3: stall again.
            clock.advance(500.0)
            deadline = time.monotonic() + 2.0
            while (
                len(handler.events("scheduler_tick_stalled")) < 2
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert len(handler.events("scheduler_tick_stalled")) >= 2
        finally:
            d._watchdog_stop.set()
            t.join(timeout=2.0)

    def test_watchdog_exits_cleanly_on_stop(
        self, tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
    ) -> None:
        """AC #7 — setting ``_watchdog_stop`` terminates the loop."""
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_cb_config_int",
            lambda self, key, default: default,
        )
        psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

        # A future-tick time so the loop sees no stall.
        d._last_scheduler_tick_at = time.monotonic() + 10_000.0

        t = self._run_loop_in_thread(d)
        # Let it loop a couple times, then stop.
        time.sleep(0.02)
        d._watchdog_stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive()


# --------------------------------------------------------------------------
# _start_watchdog idempotency + daemon flag
# --------------------------------------------------------------------------


class TestStartWatchdog:
    def test_start_watchdog_spawns_named_daemon_thread(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        # Replace the loop with a stop-aware stub so the thread exits.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_watchdog_loop",
            lambda self: self._watchdog_stop.wait(),
        )
        d._start_watchdog()
        try:
            assert d._watchdog_thread is not None
            assert d._watchdog_thread.daemon is True
            assert d._watchdog_thread.name == "dispatcher-watchdog"
            assert d._watchdog_thread.is_alive()
        finally:
            d._watchdog_stop.set()
            if d._watchdog_thread is not None:
                d._watchdog_thread.join(timeout=2.0)

    def test_start_watchdog_is_idempotent_when_alive(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_watchdog_loop",
            lambda self: self._watchdog_stop.wait(),
        )
        d._start_watchdog()
        first = d._watchdog_thread
        d._start_watchdog()  # second call — no-op.
        try:
            assert d._watchdog_thread is first
        finally:
            d._watchdog_stop.set()
            if d._watchdog_thread is not None:
                d._watchdog_thread.join(timeout=2.0)
