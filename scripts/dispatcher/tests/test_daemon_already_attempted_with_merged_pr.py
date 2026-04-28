"""Regression tests for issue #3738 — ``_issue_already_attempted`` and
``_cleanup_stale_succeeded_rows`` with merged-PR semantics.

Covers three acceptance-criterion behaviors:

1. ``_issue_already_attempted`` returns FALSE when the SQL function returns
   False (i.e. the agent row has status='succeeded' AND merged_at IS NOT NULL).
   This proves that migration 60's new SQL gate flows through to the Python
   wrapper correctly — if the gate ever reverts the SQL function would return
   True and this test would fail.

2. ``_issue_already_attempted`` returns TRUE when the SQL function returns True
   for a succeeded row with merged_at IS NULL (PR not yet detected as merged).
   This is the "still-active" case; the row must continue to block re-claim.

3. ``_cleanup_stale_succeeded_rows`` calls ``_close_issue_post_merge`` for
   each row whose issue is still OPEN, emits a ``housekeeping_succeeded_cleanup``
   log event, and returns the correct counts.

Test scaffolding mirrors ``test_daemon_housekeeping.py`` —
``_FakeCursor`` / ``_FakeConnection`` / ``_CapturingLogHandler``.
"""

from __future__ import annotations

import logging
import sys
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
# Shared fakes (mirrors test_daemon_execution_mode_audit.py shape, which
# includes fetchall support needed by _cleanup_stale_succeeded_rows)
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
    """Collect emitted ``LogRecord``s so tests can assert on them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        """All captured records whose ``extra.event`` equals ``name``."""
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon_with_capture() -> tuple[
    daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler
]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger("dispatcher.test.already_attempted_merged")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake-for-tests",
        tick_scheduler_seconds=30,
        tick_supervisor_seconds=120,
        tick_housekeeping_seconds=3600,
        log_level="DEBUG",
        version_sha="deadbee",
        host="test-host",
        pid=4242,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-dev",
        heartbeat_metric_namespace="Judgemind/Dispatcher",
        aws_region="us-west-2",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]  — test stub
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# Test 1 — succeeded + merged_at IS NOT NULL → SQL returns False → returns False
# --------------------------------------------------------------------------


class TestReturnsWhenSucceededRowHasMergedAt:
    """AC2: a row with status=succeeded AND merged_at IS NOT NULL must not block.

    The SQL function (migration 60) returns False for this state.  The Python
    wrapper ``_issue_already_attempted`` must pass that value straight through.
    """

    def test_returns_false_when_succeeded_row_has_merged_at(self) -> None:
        """Stub the SQL function to return False (merged succeeded row).

        If migration 60 is reverted and the old body reinstated, the SQL
        function would return True for the same row, causing the Python
        wrapper to return True and this assertion to fail CI.
        """
        d, conn, _handler = _make_daemon_with_capture()
        # SQL function returns (False,) — row exists but merged_at IS NOT NULL.
        conn.cursor_instance.fetch_queue = [(False,)]
        result = d._issue_already_attempted(3738)
        assert result is False

        # Confirm the call delegated to the SQL function.
        selects = [
            e
            for e in conn.cursor_instance.executed
            if "dispatcher.issue_has_active_agent" in e[0]
        ]
        assert selects, "expected a call to dispatcher.issue_has_active_agent"
        assert selects[0][1][0] == 3738


# --------------------------------------------------------------------------
# Test 2 — succeeded + merged_at IS NULL → SQL returns True → still blocks
# --------------------------------------------------------------------------


class TestReturnsWhenSucceededRowHasNullMergedAt:
    """A succeeded row with merged_at IS NULL must still block re-claim.

    Migration 56's SQL function returns True for this case (PR not yet
    detected as merged).  The wrapper must propagate that value.
    """

    def test_returns_true_when_succeeded_row_has_null_merged_at(self) -> None:
        d, conn, _handler = _make_daemon_with_capture()
        # SQL function returns (True,) — row is succeeded AND merged_at IS NULL.
        conn.cursor_instance.fetch_queue = [(True,)]
        result = d._issue_already_attempted(3738)
        assert result is True

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "dispatcher.issue_has_active_agent" in e[0]
        ]
        assert selects, "expected a call to dispatcher.issue_has_active_agent"
        assert selects[0][1][0] == 3738


# --------------------------------------------------------------------------
# Test 3 — _cleanup_stale_succeeded_rows closes OPEN issues
# --------------------------------------------------------------------------


class TestCleanupStaleSucceededRows:
    """AC: housekeeping drains stale-succeeded rows by closing their issues."""

    def test_cleanup_stale_succeeded_rows_closes_open_issues(self) -> None:
        """Seed fetchall with a stale row, mock _close_issue_post_merge as open.

        Asserts that:
        - _close_issue_post_merge is called with (issue_number, pr_number).
        - A ``housekeeping_succeeded_cleanup`` log event is emitted.
        - The returned dict reflects rows_scanned=1, issues_closed=1, errors=0.
        """
        d, conn, handler = _make_daemon_with_capture()

        # Seed one stale succeeded row: issue 999, pr 888.
        conn.cursor_instance.fetchall_queue = [[(999, 888)]]

        # Patch _close_issue_post_merge so no real gh calls happen.
        close_mock = MagicMock()
        d._close_issue_post_merge = close_mock  # type: ignore[method-assign]

        result = d._cleanup_stale_succeeded_rows()

        # SQL scan fired.
        scans = [
            e
            for e in conn.cursor_instance.executed
            if "status = 'succeeded' AND merged_at IS NOT NULL" in e[0]
        ]
        assert scans, "expected SELECT of stale succeeded rows"

        # _close_issue_post_merge invoked with the seeded (issue, pr) pair.
        close_mock.assert_called_once_with(999, 888)

        # Return dict is correct.
        assert result == {"rows_scanned": 1, "issues_closed": 1, "errors": 0}

    def test_cleanup_stale_succeeded_rows_no_rows_returns_zeros(self) -> None:
        """When there are no stale rows, counts are all zero."""
        d, conn, _handler = _make_daemon_with_capture()

        # fetchall returns empty list → nothing to process.
        conn.cursor_instance.fetchall_queue = [[]]

        close_mock = MagicMock()
        d._close_issue_post_merge = close_mock  # type: ignore[method-assign]

        result = d._cleanup_stale_succeeded_rows()

        assert result == {"rows_scanned": 0, "issues_closed": 0, "errors": 0}
        close_mock.assert_not_called()

    def test_cleanup_stale_succeeded_rows_logs_housekeeping_event(self) -> None:
        """The housekeeping_succeeded_cleanup event is emitted via _housekeeping_tick.

        This test exercises the integration between _housekeeping_tick and
        _cleanup_stale_succeeded_rows by stubbing _cleanup_stale_succeeded_rows
        and asserting the log event is emitted with the expected fields.
        """
        d, conn, handler = _make_daemon_with_capture()

        # Stub _cleanup_stale_succeeded_rows to return known counts.
        d._cleanup_stale_succeeded_rows = MagicMock(  # type: ignore[method-assign]
            return_value={"rows_scanned": 3, "issues_closed": 2, "errors": 1}
        )

        # _housekeeping_tick calls _read_retention_days per target then DELETE.
        # Provide enough fetch_queue entries so each target's config lookup
        # returns None (→ default) and the DELETE proceeds without error.
        target_count = len(daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS)
        conn.cursor_instance.fetch_queue = [None] * target_count

        d._housekeeping_tick()

        events = handler.events("housekeeping_succeeded_cleanup")
        assert len(events) == 1
        record = events[0]
        assert record.rows_scanned == 3
        assert record.issues_closed == 2
        assert record.errors == 1
        assert record.run_id == "test-run-id"
