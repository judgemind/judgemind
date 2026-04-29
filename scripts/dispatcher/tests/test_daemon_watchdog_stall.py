"""Real-concurrency stall repro tests for the scheduler-tick watchdog (#3794).

The existing ``test_daemon_watchdog.py`` exercises WARN/EXIT tiers by
stubbing ``time.monotonic()`` forward — that proves the *logic* is correct
but does NOT prove the watchdog can fire when the scheduler thread is
*genuinely* wedged (real wall-clock seconds, real GIL contention, real
logging-lock contention).

This file exercises real concurrency:

* ``test_stall_triggers_exit`` (AC #1) — wedge ``_last_scheduler_tick_at``
  via a sleeping worker thread and run ``_watchdog_loop`` with WARN/EXIT
  thresholds dialled to ~0.05s / ~0.1s.  Asserts
  ``scheduler_tick_stalled_exiting`` log + exit code 137 within a generous
  wall-clock budget.

* ``test_stall_triggers_exit_when_main_holds_logging_handler_lock`` — same
  wedge, but the "main" thread additionally holds the stdlib
  logging-handler RLock (the suspected production root cause).  This test
  is ``xfail(strict=True)`` — it *documents* that the primary watchdog
  deadlocks on ``self._log.error()`` when the logging RLock is contended
  (confirmed by #3794 investigation).  The test must keep failing so we do
  NOT accidentally regress by making the primary watchdog skip logging on
  the kill path (which would change its semantics under non-deadlocked
  conditions).  The *fix* for the deadlock is the backup watchdog below.

* ``test_backup_watchdog_fires_when_logging_blocked`` (AC #2) — same
  logging-lock wedge as above, but runs ``_backup_watchdog_loop`` (not
  ``_watchdog_loop``) with a tiny threshold.  Asserts ``os._exit(138)``
  fires within 5s even while the logging RLock is held by the wedged
  thread.  Uses ``os.write`` to stderr — no logging, no locks.

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
# AC #1 variant — logging-RLock deadlock documentation (known failure)
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#3794: primary watchdog deadlocks on self._log.error() when the "
        "logging-handler RLock is held by the wedged thread.  This xfail "
        "documents the known limitation — the fix is the backup watchdog "
        "(test_backup_watchdog_fires_when_logging_blocked).  Do NOT remove "
        "this xfail — the primary watchdog SHOULD continue logging before "
        "os._exit under normal (non-deadlocked) conditions."
    ),
)
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_stall_triggers_exit_when_main_holds_logging_handler_lock(
    tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
) -> None:
    """Documents that the *primary* watchdog deadlocks when the logging RLock
    is held by the wedged thread (#3794).

    The primary watchdog calls ``self._log.error(...)`` before ``os._exit``.
    ``self._log.error(...)`` acquires the logging-handler RLock internally
    (``Handler.emit`` is called under ``Handler.acquire()``).  If the wedged
    scheduler thread stalled mid-log-call and holds that RLock, the watchdog
    blocks forever — it never reaches ``os._exit``.

    This test is ``xfail(strict=True)``:
    - A FAIL (the deadlock manifests — watchdog does not fire within 5s) is the
      *expected outcome* and confirms the production hypothesis.
    - An unexpected PASS would mean the primary watchdog has been changed to
      skip logging on its kill path, which changes semantics and should be
      reviewed explicitly.

    The AC #2 fix is ``test_backup_watchdog_fires_when_logging_blocked`` below,
    which exercises ``_backup_watchdog_loop`` — a kill path that uses only
    ``os.write`` + ``os._exit``, bypassing logging entirely.
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

    stall_event = threading.Event()
    release_event = threading.Event()

    # The logging.Handler base class has an RLock at self.lock.
    # Handler.emit() is called under Handler.acquire() which takes that lock.
    # Holding it from a separate thread blocks any concurrent self._log.error().
    handler_lock = handler.lock

    def wedged_main_thread() -> None:
        """Simulate a scheduler thread that stalled while holding the logging lock."""
        with handler_lock:
            stall_event.set()
            release_event.wait(timeout=10.0)

    wedger = threading.Thread(
        target=wedged_main_thread, daemon=True, name="test-wedger"
    )
    wedger.start()

    assert stall_event.wait(timeout=2.0), "wedger thread did not acquire lock in time"

    d._last_scheduler_tick_at = time.monotonic() - (EXIT_S + 0.5)

    watchdog_thread = threading.Thread(target=d._watchdog_loop, daemon=True)
    watchdog_thread.start()

    try:
        # This assertion FAILS (xfail) — the watchdog never emits the event
        # because it deadlocks waiting for the logging-handler RLock.
        rec = _wait_for_event(handler, "scheduler_tick_stalled_exiting", timeout=5.0)
        release_event.set()
        assert rec is not None, (
            "DEADLOCK CONFIRMED: primary watchdog blocked on logging-handler RLock "
            "held by wedged thread — backup watchdog needed (AC #2)."
        )
        assert rec.levelno == logging.ERROR
        assert getattr(rec, "exit_code", None) == 137
        assert _wait_for_exit_call(exit_calls, 137), (
            f"os._exit(137) not called within timeout; got {exit_calls!r}"
        )
    finally:
        release_event.set()
        d._watchdog_stop.set()
        watchdog_thread.join(timeout=3.0)
        wedger.join(timeout=3.0)


# --------------------------------------------------------------------------
# AC #2 — backup watchdog fires via os._exit(138) when logging is blocked
# --------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_backup_watchdog_fires_when_logging_blocked(
    tmp_path: Path, monkeypatch: Any, psycopg_stub: Any
) -> None:
    """Backup watchdog (#3794) calls ``os._exit(138)`` even when the logging-
    handler RLock is held by the wedged thread.

    ``_backup_watchdog_loop`` uses only ``os.write`` + ``os._exit`` — no
    stdlib logging, no RLock acquisition — so a logging-blocked kill path
    cannot prevent it from firing.

    The test:
    1. Holds the logging-handler RLock in a wedger thread.
    2. Sets ``_last_scheduler_tick_at`` far in the past.
    3. Monkeypatches ``DEFAULT_BACKUP_WATCHDOG_EXIT_THRESHOLD_SECONDS`` to
       a tiny value (0.1s) and ``BACKUP_WATCHDOG_POLL_INTERVAL_SECONDS`` to 0.
    4. Runs ``_backup_watchdog_loop`` directly.
    5. Asserts ``os._exit(138)`` is called within 5s.

    Note: the monkeypatch of the module-level constants propagates into the
    loop because ``_backup_watchdog_loop`` reads them from the module namespace
    at call time (not captured in a closure).
    """
    d, _conn, handler = _make_daemon(tmp_path)

    # Tiny threshold so the backup watchdog fires in <200ms.
    BACKUP_EXIT_S = 0.10

    monkeypatch.setattr(
        daemon, "DEFAULT_BACKUP_WATCHDOG_EXIT_THRESHOLD_SECONDS", BACKUP_EXIT_S
    )
    monkeypatch.setattr(daemon, "BACKUP_WATCHDOG_POLL_INTERVAL_SECONDS", 0)

    psycopg_stub.connect = MagicMock(return_value=_FakeConnection())

    exit_calls: list[int] = []

    def fake_exit(code: int) -> None:
        exit_calls.append(code)
        raise _ExitCalled(code)

    monkeypatch.setattr(daemon.os, "_exit", fake_exit)

    stall_event = threading.Event()
    release_event = threading.Event()

    handler_lock = handler.lock

    def wedged_main_thread() -> None:
        """Hold the logging-handler RLock to block the primary watchdog."""
        with handler_lock:
            stall_event.set()
            release_event.wait(timeout=10.0)

    wedger = threading.Thread(
        target=wedged_main_thread, daemon=True, name="test-wedger-backup"
    )
    wedger.start()

    assert stall_event.wait(timeout=2.0), "wedger thread did not acquire lock in time"

    # Set tick timestamp far enough in the past to exceed BACKUP_EXIT_S.
    d._last_scheduler_tick_at = time.monotonic() - (BACKUP_EXIT_S + 0.5)

    # Run the backup watchdog loop directly (not _watchdog_loop).
    backup_thread = threading.Thread(target=d._backup_watchdog_loop, daemon=True)
    backup_thread.start()

    try:
        # The backup watchdog must call os._exit(138) even with logging blocked.
        assert _wait_for_exit_call(exit_calls, 138, timeout=5.0), (
            "backup watchdog did not call os._exit(138) within 5s even though "
            "the logging-handler RLock was held — the backup watchdog kill path "
            "must NOT use stdlib logging.  Check _backup_watchdog_loop."
        )
        assert exit_calls == [138], f"expected exactly [138], got {exit_calls!r}"
    finally:
        release_event.set()
        d._backup_watchdog_stop.set()
        backup_thread.join(timeout=3.0)
        wedger.join(timeout=3.0)
