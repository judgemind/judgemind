"""Unit tests for the _start_watchdog baseline re-seed contract (#3386).

When a cold ECS boot takes 30-60s between ``__init__`` and
``run_forever``, the ``_last_scheduler_tick_at`` value set in
``__init__`` becomes stale.  Without a re-seed, the watchdog's first
poll would observe an elapsed gap of 30-60s and potentially fire a
spurious ``scheduler_tick_stalled`` WARNING before the first real tick
has had a chance to run.

Fix (#3386): ``_start_watchdog`` re-seeds ``_last_scheduler_tick_at``
to ``time.monotonic()`` as its FIRST substantive action, after the
idempotency early-return and BEFORE spawning the thread.

Tests:

* ``test_start_watchdog_reseeds_baseline_to_call_time`` — verify that
  calling ``_start_watchdog`` resets a stale baseline to the current
  time.
* ``test_slow_boot_does_not_fire_warn_before_first_tick`` — verify that
  a 90s stale baseline does NOT trigger ``scheduler_tick_stalled`` after
  ``_start_watchdog`` re-seeds it.
* ``test_idempotent_call_does_not_reseed_when_thread_alive`` — verify
  that the re-seed does NOT run when the early-return path fires
  (i.e. the idempotency check guards the re-seed).
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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_watchdog.py so this file is self-contained)
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
    logger = logging.getLogger(f"dispatcher.test.reseed.{id(tmp_path)}")
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
# #3386 — _start_watchdog baseline re-seed tests
# --------------------------------------------------------------------------


class TestWatchdogBaselineReseed:
    def test_start_watchdog_reseeds_baseline_to_call_time(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """AC1 — calling ``_start_watchdog`` resets a stale baseline.

        Simulates a slow boot by pre-setting ``_last_scheduler_tick_at``
        90s in the past, then asserts that ``_start_watchdog`` resets it
        to approximately ``time.monotonic()`` (within 1s of the call).
        """
        d, _conn, _handler = _make_daemon(tmp_path)

        # Simulate 90s of slow boot elapsed since __init__.
        d._last_scheduler_tick_at = time.monotonic() - 90.0

        # Stub _watchdog_loop to a no-op so the thread exits immediately.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_watchdog_loop",
            lambda self: self._watchdog_stop.wait(),
        )

        before_call = time.monotonic()
        d._start_watchdog()
        after_call = time.monotonic()

        try:
            # The re-seed must have moved _last_scheduler_tick_at to a value
            # within the [before_call, after_call] window (tolerance: 1s).
            assert d._last_scheduler_tick_at >= before_call - 1.0, (
                f"baseline was not re-seeded: {d._last_scheduler_tick_at} < {before_call - 1.0}"
            )
            assert d._last_scheduler_tick_at <= after_call + 1.0, (
                f"baseline is in the future: {d._last_scheduler_tick_at} > {after_call + 1.0}"
            )
        finally:
            d._watchdog_stop.set()
            if d._watchdog_thread is not None:
                d._watchdog_thread.join(timeout=2.0)

    def test_slow_boot_does_not_fire_warn_before_first_tick(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """AC2 — a 90s stale baseline must NOT trigger scheduler_tick_stalled.

        After ``_start_watchdog`` re-seeds the baseline, the watchdog's
        first poll should see elapsed ~= 0 regardless of how long the
        daemon spent between ``__init__`` and ``run_forever``.
        """
        d, _conn, handler = _make_daemon(tmp_path)

        # Simulate 90s of slow boot.
        d._last_scheduler_tick_at = time.monotonic() - 90.0

        # Shrink the poll interval so the watchdog runs quickly in tests.
        monkeypatch.setattr(daemon, "WATCHDOG_POLL_INTERVAL_SECONDS", 0)

        # Stub _cb_config_int to return defaults — avoids real DB calls.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_cb_config_int",
            lambda self, key, default: default,
        )

        # Stub _watchdog_loop to a single-poll iteration: run one check
        # then stop.  We use _watchdog_stop to signal it.
        poll_ran = threading.Event()

        def _one_poll_loop(self: daemon.DispatcherDaemon) -> None:
            # Run one iteration of the watchdog's body, then stop.
            # Mirror the real loop's structure: check elapsed, then exit.
            now = time.monotonic()
            elapsed = now - self._last_scheduler_tick_at
            warn_s = self._watchdog_warn_threshold()
            if elapsed >= warn_s:
                self._emit_scheduler_stall_warning(elapsed_seconds=elapsed)
            poll_ran.set()
            # Do not loop — exit so the thread joins cleanly.

        monkeypatch.setattr(daemon.DispatcherDaemon, "_watchdog_loop", _one_poll_loop)

        d._start_watchdog()

        try:
            # Wait for the poll to complete.
            assert poll_ran.wait(timeout=3.0), "watchdog poll did not run in time"

            # The re-seed in _start_watchdog should have reset the baseline
            # to ~now, so elapsed in the poll is ~0 — well below WARN (60s).
            # No scheduler_tick_stalled event should have been emitted.
            stalled_events = handler.events("scheduler_tick_stalled")
            assert stalled_events == [], (
                f"spurious scheduler_tick_stalled fired after re-seed: "
                f"{[(r.levelno, getattr(r, 'elapsed_seconds', None)) for r in stalled_events]}"
            )
        finally:
            d._watchdog_stop.set()
            if d._watchdog_thread is not None:
                d._watchdog_thread.join(timeout=2.0)

    def test_idempotent_call_does_not_reseed_when_thread_alive(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Idempotency guard — re-seed must NOT run when early-return fires.

        If ``_start_watchdog`` is called a second time while the thread is
        still alive, the idempotency early-return must win; the re-seed line
        must NOT execute.  This guards against accidentally moving the
        re-seed before the early-return in a future refactor.
        """
        d, _conn, _handler = _make_daemon(tmp_path)

        # Stub _watchdog_loop so the thread stays alive until we stop it.
        monkeypatch.setattr(
            daemon.DispatcherDaemon,
            "_watchdog_loop",
            lambda self: self._watchdog_stop.wait(),
        )

        # First call — spins up the thread and re-seeds the baseline.
        d._start_watchdog()
        assert d._watchdog_thread is not None
        assert d._watchdog_thread.is_alive()

        # Record the baseline after the first call.
        baseline_after_first = d._last_scheduler_tick_at

        # Artificially age the baseline so we can detect an unwanted re-seed.
        d._last_scheduler_tick_at = baseline_after_first - 50.0
        expected_stale = d._last_scheduler_tick_at

        try:
            # Second call while thread is alive — must be a no-op.
            d._start_watchdog()

            # The baseline must NOT have been refreshed (early-return fired).
            assert d._last_scheduler_tick_at == expected_stale, (
                f"idempotent call incorrectly re-seeded _last_scheduler_tick_at: "
                f"expected {expected_stale}, got {d._last_scheduler_tick_at}"
            )
        finally:
            d._watchdog_stop.set()
            if d._watchdog_thread is not None:
                d._watchdog_thread.join(timeout=2.0)
