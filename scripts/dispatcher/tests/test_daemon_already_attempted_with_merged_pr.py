"""Unit tests for migration 57 merged-aware ``_issue_already_attempted`` fix.

Issue #3738.  ``dispatcher.issue_has_active_agent`` (migration 57) was
narrowed so that a ``succeeded`` row with ``merged_at IS NOT NULL`` no
longer blocks re-claim.  These tests pin:

* ``_issue_already_attempted`` delegates to the SQL function and surfaces
  True/False correctly.
* ``_cleanup_stale_succeeded_rows`` iterates stale rows and calls
  ``_close_issue_post_merge`` per row.
* ``_housekeeping_tick`` emits ``daemon.housekeeping_succeeded_cleanup``
  with the stubbed counts.

All DB interaction is against ``_FakeCursor`` / ``_FakeConnection`` stubs
matching the shape in ``test_daemon_execution_mode_audit.py``; no real
Postgres is needed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (same shape as test_daemon_execution_mode_audit.py)
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
    logger = logging.getLogger(
        f"dispatcher.test.already_attempted_merged.{id(tmp_path)}"
    )
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
# (a) _issue_already_attempted returns False when SQL stubs (False,)
# --------------------------------------------------------------------------


class TestIssueAlreadyAttemptedReturnsFalse:
    """SQL function returning False → method returns False (eligible)."""

    def test_returns_false_when_sql_returns_false(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # SQL stub: issue_has_active_agent returns FALSE (not blocked).
        conn.cursor_instance.fetch_queue = [(False,)]

        result = d._issue_already_attempted(3738)

        assert result is False, (
            "_issue_already_attempted must return False when SQL returns False "
            "(issue is eligible for re-claim)"
        )

        selects = [
            e for e in conn.cursor_instance.executed if "issue_has_active_agent" in e[0]
        ]
        assert selects, "expected SELECT dispatcher.issue_has_active_agent(%s)"
        assert selects[0][1] == (3738,)


# --------------------------------------------------------------------------
# (b) _issue_already_attempted returns True when SQL stubs (True,)
# --------------------------------------------------------------------------


class TestIssueAlreadyAttemptedReturnsTrue:
    """SQL function returning True → method returns True (blocked)."""

    def test_returns_true_when_sql_returns_true(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # SQL stub: issue_has_active_agent returns TRUE (still blocked).
        conn.cursor_instance.fetch_queue = [(True,)]

        result = d._issue_already_attempted(1234)

        assert result is True, (
            "_issue_already_attempted must return True when SQL returns True "
            "(issue is blocked from re-claim)"
        )


# --------------------------------------------------------------------------
# (c) _cleanup_stale_succeeded_rows calls _close_issue_post_merge once
#     and returns {rows_scanned:1, issues_closed:1, errors:0}
# --------------------------------------------------------------------------


class TestCleanupStaleSucceededRowsOneRow:
    """One stale row → _close_issue_post_merge called once, counts correct."""

    def test_calls_close_and_returns_counts(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # SELECT returns one (issue_number=999, pr_number=888) row.
        conn.cursor_instance.fetchall_queue = [[(999, 888)]]

        close_calls: list[tuple[int, int | None]] = []

        def fake_close(issue_number: int, pr_number: int | None) -> None:
            close_calls.append((issue_number, pr_number))

        d._close_issue_post_merge = fake_close  # type: ignore[assignment]

        result = d._cleanup_stale_succeeded_rows()

        assert close_calls == [(999, 888)], (
            "_cleanup_stale_succeeded_rows must call "
            "_close_issue_post_merge(999, 888) exactly once"
        )
        assert result == {"rows_scanned": 1, "issues_closed": 1, "errors": 0}, (
            f"expected {{rows_scanned:1, issues_closed:1, errors:0}}, got {result}"
        )

        # Confirm the SELECT targeted the right predicate.
        selects = [
            e
            for e in conn.cursor_instance.executed
            if "status = 'succeeded'" in e[0] and "merged_at IS NOT NULL" in e[0]
        ]
        assert selects, (
            "expected SELECT … WHERE status = 'succeeded' AND merged_at IS NOT NULL"
        )


# --------------------------------------------------------------------------
# (d) empty fetchall_queue → all-zero counts
# --------------------------------------------------------------------------


class TestCleanupStaleSucceededRowsEmpty:
    """Empty SELECT result → zero counts, no _close_issue_post_merge calls."""

    def test_all_zero_counts_when_no_rows(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # SELECT returns no rows.
        conn.cursor_instance.fetchall_queue = [[]]

        close_calls: list[Any] = []

        def fake_close(issue_number: int, pr_number: int | None) -> None:
            close_calls.append((issue_number, pr_number))

        d._close_issue_post_merge = fake_close  # type: ignore[assignment]

        result = d._cleanup_stale_succeeded_rows()

        assert close_calls == [], (
            "_close_issue_post_merge must not be called when there are no rows"
        )
        assert result == {"rows_scanned": 0, "issues_closed": 0, "errors": 0}, (
            f"expected all-zero counts, got {result}"
        )


# --------------------------------------------------------------------------
# (e) _housekeeping_tick emits housekeeping_succeeded_cleanup log record
# --------------------------------------------------------------------------


class TestHousekeepingTickEmitsSucceededCleanupLog:
    """_housekeeping_tick emits daemon.housekeeping_succeeded_cleanup."""

    def test_log_record_emitted_with_stubbed_counts(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Stub _cleanup_stale_succeeded_rows to avoid real DB/GitHub calls
        # and return a deterministic result.
        stub_result = {"rows_scanned": 2, "issues_closed": 1, "errors": 0}

        def fake_cleanup() -> dict[str, int]:
            return stub_result

        monkeypatch.setattr(d, "_cleanup_stale_succeeded_rows", fake_cleanup)

        # Stub the other housekeeping helpers that would otherwise fail.
        monkeypatch.setattr(d, "_reconcile_stale_merged_at", lambda: {})
        monkeypatch.setattr(d, "_clear_stale_agent_task_arns", lambda: 0)
        monkeypatch.setattr(d, "_backfill_terminal_ended_at", lambda: 0)
        monkeypatch.setattr(d, "_housekeeping_close_orphan_prs", lambda: {})

        # Pad fetch_queue with None × len(_HOUSEKEEPING_TARGETS) so the
        # retention-lookup path in _read_retention_days doesn't crash.
        target_count = len(daemon.DispatcherDaemon._HOUSEKEEPING_TARGETS)
        conn.cursor_instance.fetch_queue = [None] * target_count

        d._housekeeping_tick()

        events = handler.events("housekeeping_succeeded_cleanup")
        assert len(events) == 1, (
            f"expected exactly one housekeeping_succeeded_cleanup event, "
            f"got {len(events)}"
        )
        record = events[0]
        assert getattr(record, "rows_scanned") == stub_result["rows_scanned"]
        assert getattr(record, "issues_closed") == stub_result["issues_closed"]
        assert getattr(record, "errors") == stub_result["errors"]
        assert getattr(record, "run_id") == "test-run-id"
