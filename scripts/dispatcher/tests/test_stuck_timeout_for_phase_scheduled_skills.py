"""Regression tests for scheduled-skill phase entries in STUCK_TIMEOUT_SECONDS_BY_PHASE (#3723).

Verifies that :data:`~dispatcher.daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE` contains
explicit entries for each registered scheduled-skill phase name
(``spotcheck``, ``audit``, ``dispatcher-audit``, ``dispatcher-daily-report``)
with thresholds well above the respective documented runtimes, so the stuck
supervisor does not prematurely reap a legitimately long-running scheduled skill.

Regression: agent ``1d1f090e`` (kind='scheduled_skill', phase='spotcheck') was
reaped after 1818s on 2026-04-28 because ``spotcheck`` was not in the dict and
fell back to the 1800s global :data:`~dispatcher.daemon.STUCK_TIMEOUT_SECONDS`
default.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Minimal fake fixtures (mirrors test_daemon_stuck_check_operational.py)
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


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.stuck_scheduled_skills.{id(tmp_path)}")
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


# --------------------------------------------------------------------------
# Dict-presence and threshold assertions (AC1)
# --------------------------------------------------------------------------


def test_stuck_timeout_dict_has_spotcheck() -> None:
    """STUCK_TIMEOUT_SECONDS_BY_PHASE has an explicit entry for 'spotcheck' >= 3600."""
    assert "spotcheck" in daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE
    assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["spotcheck"] >= 3600


def test_stuck_timeout_dict_has_audit() -> None:
    """STUCK_TIMEOUT_SECONDS_BY_PHASE has an explicit entry for 'audit' >= 3600."""
    assert "audit" in daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE
    assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["audit"] >= 3600


def test_stuck_timeout_dict_has_dispatcher_audit() -> None:
    """STUCK_TIMEOUT_SECONDS_BY_PHASE has an explicit entry for 'dispatcher-audit' >= 3600."""
    assert "dispatcher-audit" in daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE
    assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["dispatcher-audit"] >= 3600


def test_stuck_timeout_dict_has_dispatcher_daily_report() -> None:
    """STUCK_TIMEOUT_SECONDS_BY_PHASE has an explicit entry for 'dispatcher-daily-report' >= 1800."""
    assert "dispatcher-daily-report" in daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE
    assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["dispatcher-daily-report"] >= 1800


# --------------------------------------------------------------------------
# _stuck_timeout_for_phase lookup test (AC2)
# --------------------------------------------------------------------------


def test_stuck_timeout_for_phase_spotcheck_lookup(tmp_path: Path) -> None:
    """_stuck_timeout_for_phase('spotcheck') returns the dict value and is >= 3600.

    Exercises the actual lookup path the supervisor walks at runtime (#3723).
    """
    d, _conn, _handler = _make_daemon(tmp_path)
    result = d._stuck_timeout_for_phase("spotcheck")
    assert result == daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["spotcheck"]
    assert result >= 3600
