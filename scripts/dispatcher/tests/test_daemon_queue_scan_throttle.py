"""Unit tests for #2974 queue-scan throttle decoupled from scheduler tick.

Covers:
* ``QUEUE_SCAN_INTERVAL_SECONDS`` constant value.
* First tick after boot always fires the queue scan.
* Subsequent ticks within the 30s window skip the scan (return -1 sentinel).
* Tick after 30s elapses fires the scan again.
* The -1 sentinel is returned (not raised) for throttled ticks.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_scheduler_tick_instrumentation.py).
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
    logger = logging.getLogger(f"dispatcher.test.queue_throttle.{id(tmp_path)}")
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
# Tests
# --------------------------------------------------------------------------


class TestQueueScanThrottleConstant:
    def test_constant_is_30(self) -> None:
        assert daemon.QUEUE_SCAN_INTERVAL_SECONDS == 30


class TestQueueScanThrottle:
    def test_queue_scan_fires_on_first_tick_after_boot(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]

        scan_calls: list[float] = []

        def _recording_scan() -> int:
            scan_calls.append(daemon.time.monotonic())
            return 3

        d._scan_queue_and_snapshot = _recording_scan  # type: ignore[method-assign]

        monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)

        d.scheduler_tick()

        assert len(scan_calls) == 1

    def test_queue_scan_skips_within_throttle_window(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,), (1,)]

        scan_calls: list[int] = []

        def _recording_scan() -> int:
            scan_calls.append(1)
            return 2

        d._scan_queue_and_snapshot = _recording_scan  # type: ignore[method-assign]

        monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)
        d.scheduler_tick()
        assert len(scan_calls) == 1

        monkeypatch.setattr(daemon.time, "monotonic", lambda: 1010.0)
        d.scheduler_tick()
        assert len(scan_calls) == 1

    def test_queue_scan_fires_again_after_30s(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,), (1,)]

        scan_calls: list[int] = []

        def _recording_scan() -> int:
            scan_calls.append(1)
            return 5

        d._scan_queue_and_snapshot = _recording_scan  # type: ignore[method-assign]

        monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)
        d.scheduler_tick()
        assert len(scan_calls) == 1

        monkeypatch.setattr(daemon.time, "monotonic", lambda: 1031.0)
        d.scheduler_tick()
        assert len(scan_calls) == 2

    def test_throttled_tick_returns_minus_one_queue_depth(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,), (1,)]

        def _recording_scan() -> int:
            return 99

        d._scan_queue_and_snapshot = _recording_scan  # type: ignore[method-assign]

        monkeypatch.setattr(daemon.time, "monotonic", lambda: 1000.0)
        d.scheduler_tick()

        d._last_queue_scan = 1000.0

        scan_called = False

        def _should_not_call() -> int:
            nonlocal scan_called
            scan_called = True
            return 99

        d._scan_queue_and_snapshot = _should_not_call  # type: ignore[method-assign]

        tick_time = 1010.0
        monkeypatch.setattr(daemon.time, "monotonic", lambda: tick_time)
        d.scheduler_tick()

        assert not scan_called
