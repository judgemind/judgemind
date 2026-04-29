"""Real-concurrency stall repro tests for the scheduler-tick watchdog.

The existing ``test_daemon_watchdog.py`` exercises WARN/EXIT tiers by
stubbing ``time.monotonic()`` forward — that proves the *logic* is correct
but does NOT prove the watchdog can fire when the scheduler thread is
*genuinely* wedged (real wall-clock seconds, real GIL contention).

This file exercises real concurrency:

* ``test_stall_triggers_exit`` (AC #1) — wedge ``_last_scheduler_tick_at``
  via a sleeping worker thread and run ``_watchdog_loop`` with WARN/EXIT
  thresholds dialled to ~0.05s / ~0.1s.  Asserts
  ``scheduler_tick_stalled_exiting`` log + exit code 137 within a generous
  wall-clock budget.

* ``test_multi_substep_wedge_triggers_primary_watchdog`` (#3801) —
  regression for the per-sub-step heartbeat refresh that hid
  multi-sub-step wedges. Pre-#3801 ``_record_scheduler_step`` /
  ``_record_supervisor_step`` rewrote ``_last_scheduler_tick_at = now``
  between every sub-step, so a long-running tick whose individual
  sub-steps were each below the EXIT threshold could go silent for
  arbitrary multiples of the threshold without tripping the watchdog.
  The fix removed the per-sub-step refresh; this test pins the
  behaviour by simulating a tick that processes 12 sub-steps (each
  successfully recorded via ``_record_scheduler_step``) and asserting
  the watchdog still fires on the cumulative elapsed.

The pre-#3801 tests for ``_backup_watchdog_loop`` were deleted alongside
that loop — its load-bearing rationale (logging-RLock deadlock during a
gh-subprocess-storm wedge) is gone now that the storm itself is gone
(housekeeping bulk-clears stale ARNs; the per-row finalize loop in the
reaper that produced the storm is deleted). See #3801.

No real ``os._exit`` is ever called — the watchdog is monkeypatched to a
sentinel-raising stub.  No real Postgres is used — ``psycopg.connect`` is
stubbed via the conftest fixture.
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
# Shared fakes — mirror test_daemon_watchdog.py
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
    logger = logging.getLogger(f"dispatcher.test.watchdog_stall.{id(tmp_path)}")
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
    d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
    d._scan_blocked_and_snapshot = lambda: 0  # type: ignore[method-assign]
    d._maybe_spawn_orchestration_thread = MagicMock(return_value=False)  # type: ignore[method-assign]
    return d, conn, handler


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _wait_for_exit_call(exit_calls: list[int], code: int, timeout: float = 5.0) -> bool:
    """Poll until exit_calls contains ``code`` (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if code in exit_calls:
            return True
        time.sleep(0.005)
    return False


def _wait_for_event(
    handler: _CapturingLogHandler, name: str, timeout: float = 5.0
) -> logging.LogRecord | None:
    """Poll until the named event appears in the handler (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = handler.events(name)
        if events:
            return events[-1]
        time.sleep(0.005)
    return None


# --------------------------------------------------------------------------
# AC #1 — real stall (no logging-lock contention)
# --------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_stall_triggers_exit(
    tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
) -> None:
    """Wedge ``_last_scheduler_tick_at`` in the past and assert the watchdog
    calls ``os._exit(137)`` with a ``scheduler_tick_stalled_exiting`` log.

    Uses *real* wall-clock time (tiny thresholds ~0.05s WARN / ~0.1s EXIT)
    so the test exercises genuine concurrency rather than a stubbed clock.
    """
    d, _conn, handler = _make_daemon(tmp_path)

    # Tiny thresholds so the watchdog fires in <200ms of real wall time.
    WARN_S = 0.05
    EXIT_S = 0.10

    # Override the threshold helpers to return tiny values.
    monkeypatch.setattr(
        daemon.DispatcherDaemon,
        "_watchdog_warn_threshold",
        lambda self: WARN_S,  # type: ignore[misc]
    )
    monkeypatch.setattr(
        daemon.DispatcherDaemon,
        "_watchdog_exit_threshold",
        lambda self: EXIT_S,  # type: ignore[misc]
    )

    # Shrink poll interval to zero so the loop busy-polls (safe for tests).
    monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)

    # Stub psycopg so the watchdog's DB connection attempt is a no-op.
    psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

    # Capture os._exit calls without dying.
    exit_calls: list[int] = []

    def fake_exit(code: int) -> None:
        exit_calls.append(code)
        raise _ExitCalled(code)

    monkeypatch.setattr(daemon.os, "_exit", fake_exit)

    # Wedge: set _last_scheduler_tick_at to EXIT_S + 0.5s in the past so
    # the watchdog's very first poll sees an elapsed time above EXIT_S.
    d._last_scheduler_tick_at = time.monotonic() - (EXIT_S + 0.5)

    # Start the watchdog in a background thread.
    watchdog_thread = threading.Thread(target=d._watchdog_loop, daemon=True)
    watchdog_thread.start()

    try:
        # Wait for the exit-tier event.
        rec = _wait_for_event(handler, "scheduler_tick_stalled_exiting", timeout=5.0)
        assert rec is not None, (
            "watchdog did not emit scheduler_tick_stalled_exiting within 5s; "
            "this means the watchdog thread is genuinely wedged or the thresholds "
            "were not applied correctly."
        )
        assert rec.levelno == logging.ERROR
        assert getattr(rec, "exit_code", None) == 137

        # Wait for os._exit to be called (the _ExitCalled exception kills the thread).
        assert _wait_for_exit_call(exit_calls, 137), (
            f"os._exit(137) not called within timeout; got {exit_calls!r}"
        )
    finally:
        d._watchdog_stop.set()
        watchdog_thread.join(timeout=3.0)


# --------------------------------------------------------------------------
# #3801 — multi-sub-step wedge MUST trip the primary watchdog.
#
# Pre-#3801 ``_record_scheduler_step`` rewrote ``_last_scheduler_tick_at =
# now`` between each sub-step, which meant a long-running tick whose
# individual sub-steps were each below the EXIT threshold would never
# trip the watchdog. The fix removed the per-sub-step heartbeat refresh.
# This test pins the new behaviour: simulate a tick that calls
# ``_record_scheduler_step`` 12 times, each successfully (sub-second
# elapsed each), but with an aggregate elapsed exceeding the EXIT
# threshold. The watchdog must fire.
# --------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_multi_substep_wedge_triggers_primary_watchdog(
    tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
) -> None:
    """A scheduler tick that successfully records 12 sub-steps but takes
    longer than the EXIT threshold in aggregate must trip the primary
    watchdog (#3801).

    Pre-#3801 the per-sub-step ``_last_scheduler_tick_at = now`` write
    in :meth:`_record_scheduler_step` would refresh the heartbeat after
    each sub-step, hiding the cumulative wedge from the watchdog. This
    test wedges via the cumulative path, asserts the watchdog still
    fires, and pins the no-refresh contract.
    """
    d, _conn, handler = _make_daemon(tmp_path)

    WARN_S = 0.05
    EXIT_S = 0.10

    monkeypatch.setattr(
        daemon.DispatcherDaemon,
        "_watchdog_warn_threshold",
        lambda self: WARN_S,  # type: ignore[misc]
    )
    monkeypatch.setattr(
        daemon.DispatcherDaemon,
        "_watchdog_exit_threshold",
        lambda self: EXIT_S,  # type: ignore[misc]
    )
    monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)
    psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

    exit_calls: list[int] = []

    def fake_exit(code: int) -> None:
        exit_calls.append(code)
        raise _ExitCalled(code)

    monkeypatch.setattr(daemon.os, "_exit", fake_exit)

    # Wedge: pretend the tick started long ago. Then call
    # ``_record_scheduler_step`` 12 times in a row — each one is
    # individually fast but post-#3801 it does not refresh the
    # heartbeat. Pre-#3801 the watchdog would not fire because each
    # call would have set ``_last_scheduler_tick_at = now``; post-#3801
    # the wedge persists.
    d._last_scheduler_tick_at = time.monotonic() - (EXIT_S + 0.5)

    # Simulate 12 successful sub-steps.
    t_step = time.monotonic()
    for step_name in (
        "consume_commands",
        "concurrency_cap_read",
        "circuit_breaker_auto_close",
        "process_retry_markers",
        "reap_agent_tasks",
        "scan_queue",
        "scan_blocked",
        "active_agent_count",
        "spawn_orchestration",
        "step_9",
        "step_10",
        "step_11",
    ):
        t_step = d._record_scheduler_step(step_name, t_step)

    # The contract: the recorder MUST NOT have refreshed the
    # ``_last_scheduler_tick_at`` heartbeat — so the watchdog still sees
    # an elapsed gap > EXIT_S and fires.
    assert d._last_scheduler_tick_at < t_step - (EXIT_S + 0.4), (
        "post-#3801 contract violated: _record_scheduler_step refreshed "
        "_last_scheduler_tick_at, which would hide multi-sub-step wedges "
        "from the primary watchdog (the load-bearing wedge of 2026-04-29)."
    )

    watchdog_thread = threading.Thread(target=d._watchdog_loop, daemon=True)
    watchdog_thread.start()

    try:
        rec = _wait_for_event(handler, "scheduler_tick_stalled_exiting", timeout=5.0)
        assert rec is not None, (
            "watchdog did not fire on a multi-sub-step wedge — the per-sub-step "
            "heartbeat refresh hack from #3205/#3403 must stay deleted (#3801)."
        )
        assert rec.levelno == logging.ERROR
        assert getattr(rec, "exit_code", None) == 137
        assert _wait_for_exit_call(exit_calls, 137), (
            f"os._exit(137) not called within timeout; got {exit_calls!r}"
        )
    finally:
        d._watchdog_stop.set()
        watchdog_thread.join(timeout=3.0)
