"""Regression tests for the stuck_timeout_repeated alert signal (#2878).

The daemon emits ``daemon.stuck_timeout_repeated`` only when the same
agent fires ``stuck_timeout`` twice within ``STUCK_TIMEOUT_ALERT_WINDOW_SECONDS``
(10 min).  A plain CloudWatch metric filter on ``failure_detected`` cannot
express "same agent_id, count >= 2 in N seconds" — the daemon does the
per-agent DB check and writes a distinct structured-log event so the alarm
can count it directly.

Two test cases:
- First occurrence → only ``failure_detected`` emitted, NOT
  ``stuck_timeout_repeated``.
- Second occurrence on the same agent within the window → BOTH
  ``failure_detected`` AND ``stuck_timeout_repeated`` emitted.
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


from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (mirrors test_daemon_restart_cascade.py)
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
    logger = logging.getLogger(f"dispatcher.test.stuck_repeated.{id(tmp_path)}")
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
    # Stub out the complex sub-calls that consume additional DB fetchones
    # and are tested in their own dedicated test files.  This isolates the
    # stuck_timeout_repeated signal logic from the retry/terminal machinery.
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._create_retry_marker = MagicMock()  # type: ignore[method-assign]
    return d, conn, handler


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestStuckTimeoutRepeated:
    def test_first_stuck_timeout_no_repeated_event(self, tmp_path: Path) -> None:
        """First stuck_timeout for an agent emits only ``failure_detected``.

        The ``_has_prior_stuck_timeout_in_window`` DB query returns no row
        (this is the first occurrence) so ``stuck_timeout_repeated`` must
        NOT be emitted.
        """
        d, conn, handler = _make_daemon(tmp_path)

        # Post-#3731 row tuple: silent_seconds under 5400 (ralph silent
        # threshold) so total-runtime path fires (stuck_timeout
        # category, not silent_hang). Tuple shape: (agent_id,
        # issue_number, phase, silent_seconds, total_runtime_seconds,
        # execution_mode, agent_task_arn).
        conn.cursor_instance.fetchall_queue = [
            [("agent-first", 101, "ralph", 1800.0, 57600.0, "subprocess", None)],
        ]
        # Fetch order with _mark_agent_terminal / _create_retry_marker stubbed:
        # (1) stuck_timeout_s_by_phase config override (unset → None),
        # (2) silent_hang_timeout_s_by_phase override (None) [#3731],
        # (3) RETURNING failure_id from _write_failure INSERT = 10,
        # (4) _has_prior_stuck_timeout_in_window → None (no prior row).
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
            None,  # silent_hang_timeout_s_by_phase override — unset
            (10,),  # failure_id RETURNING from _write_failure
            None,  # _has_prior_stuck_timeout_in_window — no prior
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        # failure_detected emitted exactly once.
        detected = handler.events("failure_detected")
        assert len(detected) == 1
        assert detected[0].category == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT  # type: ignore[attr-defined]

        # stuck_timeout_repeated must NOT be emitted on the first occurrence.
        repeated = handler.events("stuck_timeout_repeated")
        assert len(repeated) == 0

    def test_second_stuck_timeout_within_window_emits_repeated_event(
        self, tmp_path: Path
    ) -> None:
        """Second stuck_timeout on the same agent within the alert window emits both events.

        When ``_has_prior_stuck_timeout_in_window`` finds a prior row
        (simulated by returning ``(9,)`` — failure_id 9 preceding the
        current failure_id 10), the daemon must emit ``stuck_timeout_repeated``
        in addition to ``failure_detected``.
        """
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [("agent-repeat", 202, "ralph", 1800.0, 57600.0, "subprocess", None)],
        ]
        # Fetch order with _mark_agent_terminal / _create_retry_marker stubbed:
        # (1) stuck_timeout_s_by_phase override — unset,
        # (2) silent_hang_timeout_s_by_phase override — unset [#3731],
        # (3) RETURNING failure_id = 10 (current failure),
        # (4) _has_prior_stuck_timeout_in_window → (9,) (prior row exists).
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
            None,  # silent_hang_timeout_s_by_phase override — unset
            (10,),  # failure_id RETURNING (current failure)
            (9,),  # prior failure_id within window
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        # failure_detected emitted.
        detected = handler.events("failure_detected")
        assert len(detected) == 1

        # stuck_timeout_repeated MUST be emitted on the second occurrence.
        repeated = handler.events("stuck_timeout_repeated")
        assert len(repeated) == 1

        rec = repeated[0]
        assert rec.agent_id == "agent-repeat"  # type: ignore[attr-defined]
        assert rec.issue_number == 202  # type: ignore[attr-defined]
        assert rec.window_seconds == daemon.STUCK_TIMEOUT_ALERT_WINDOW_SECONDS  # type: ignore[attr-defined]
        assert rec.prior_failure_id == 9  # type: ignore[attr-defined]
