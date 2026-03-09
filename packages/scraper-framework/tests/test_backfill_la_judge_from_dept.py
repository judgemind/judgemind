"""Tests for the backfill_la_judge_from_dept script.

All database access is mocked — these tests verify the lookup, update,
and pagination logic without requiring a live database.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from datetime import date
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill = importlib.import_module("backfill_la_judge_from_dept")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COURT_ID = str(uuid.uuid4())
_CASE_ID = str(uuid.uuid4())
_HEARING_DATE = date(2026, 3, 5)
_DEPT_MAP = {"52": "Jerrold Abeles", "3": "William A. Crowfoot", "F46": "Kerry Duffy-Lewis"}


def _make_ruling_row(
    department: str,
    *,
    court_id: str | None = None,
    case_id: str | None = None,
    hearing_date: date | None = None,
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        str(uuid.uuid4()),  # r.id (ruling_id)
        department,  # r.department
        court_id or _COURT_ID,
        case_id or _CASE_ID,
        hearing_date or _HEARING_DATE,
    )


def _mock_conn_with_rows(rows: list[tuple]) -> MagicMock:
    """Create a mock connection where the first cursor returns rows."""
    conn = MagicMock()
    cur_fetch = MagicMock()
    cur_fetch.fetchall.return_value = rows

    # We need multiple cursor contexts: one for fetch, rest for updates
    call_count = 0

    def cursor_ctx() -> MagicMock:
        nonlocal call_count
        ctx = MagicMock()
        if call_count == 0:
            ctx.__enter__ = MagicMock(return_value=cur_fetch)
        else:
            cur_update = MagicMock()
            ctx.__enter__ = MagicMock(return_value=cur_update)
        ctx.__exit__ = MagicMock(return_value=False)
        call_count += 1
        return ctx

    conn.cursor.side_effect = cursor_ctx
    return conn


# ---------------------------------------------------------------------------
# backfill_batch tests
# ---------------------------------------------------------------------------


class TestBackfillBatch:
    """Tests for backfill_batch()."""

    def test_no_rows_returns_zero(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )
        assert processed == 0
        assert updated == 0

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    def test_updates_ruling_with_mapped_judge(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock
    ) -> None:
        """Ruling with department in mapping gets judge_id set."""
        row = _make_ruling_row("52")
        conn = _mock_conn_with_rows([row])

        mock_resolve.return_value = "judge-uuid-abeles"

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        assert processed == 1
        assert updated == 1

        # resolve_judge should be called with the mapped name
        mock_resolve.assert_called_once_with(conn, "Jerrold Abeles", _COURT_ID)

        # case_judges link should be created
        mock_upsert_cj.assert_called_once_with(conn, _CASE_ID, "judge-uuid-abeles", _HEARING_DATE)

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    def test_skips_unmapped_department(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock
    ) -> None:
        """Ruling with department NOT in mapping is skipped."""
        row = _make_ruling_row("999")
        conn = _mock_conn_with_rows([row])

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        assert processed == 1
        assert updated == 0
        mock_resolve.assert_not_called()
        mock_upsert_cj.assert_not_called()

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    def test_multiple_rulings_in_batch(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock
    ) -> None:
        """Multiple rulings are processed in a single batch."""
        row1 = _make_ruling_row("52")
        row2 = _make_ruling_row("F46")
        row3 = _make_ruling_row("999")  # unmapped

        conn = _mock_conn_with_rows([row1, row2, row3])
        mock_resolve.side_effect = ["judge-abeles", "judge-duffy-lewis"]

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        assert processed == 3
        assert updated == 2
        assert mock_resolve.call_count == 2

    def test_cursor_advances(self) -> None:
        """The cursor should advance to the last ruling_id processed."""
        ruling_id = str(uuid.uuid4())
        row = (ruling_id, "52", _COURT_ID, _CASE_ID, _HEARING_DATE)

        conn = _mock_conn_with_rows([row])
        with patch("backfill_la_judge_from_dept.resolve_judge", return_value="j-id"):
            with patch("backfill_la_judge_from_dept.upsert_case_judge"):
                _, _, cursor = backfill.backfill_batch(
                    conn,
                    _DEPT_MAP,
                    batch_size=10,
                    cursor=backfill._CURSOR_MIN_UUID,
                )

        assert cursor == ruling_id


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_la_judge_from_dept.psycopg")
    @patch("backfill_la_judge_from_dept.backfill_batch")
    def test_dry_run_rolls_back(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        # One partial batch (signals end)
        mock_batch.side_effect = [(5, 3, cursor_1)]

        stats = backfill.run_backfill(
            "postgresql://test",
            dept_map=_DEPT_MAP,
            batch_size=100,
            dry_run=True,
        )

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 5
        assert stats["total_updated"] == 3

    @patch("backfill_la_judge_from_dept.psycopg")
    @patch("backfill_la_judge_from_dept.backfill_batch")
    def test_commits_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        cursor_2 = str(uuid.uuid4())
        # Two batches: first full, second partial
        mock_batch.side_effect = [(100, 80, cursor_1), (30, 20, cursor_2)]

        stats = backfill.run_backfill(
            "postgresql://test",
            dept_map=_DEPT_MAP,
            batch_size=100,
        )

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 130
        assert stats["total_updated"] == 100

    @patch("backfill_la_judge_from_dept.psycopg")
    @patch("backfill_la_judge_from_dept.backfill_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        mock_batch.side_effect = [(50, 40, cursor_1)]

        stats = backfill.run_backfill(
            "postgresql://test",
            dept_map=_DEPT_MAP,
            batch_size=100,
            limit=50,
        )

        # The effective batch size should have been capped to 50
        call_args = mock_batch.call_args_list[0]
        assert call_args[0][2] == 50  # effective_batch = min(100, 50)
        assert stats["total_processed"] == 50

    def test_query_uses_keyset_not_offset(self) -> None:
        """The query must use keyset pagination, not OFFSET."""
        assert "OFFSET" not in backfill.FETCH_QUERY
        assert "r.id > %s::uuid" in backfill.FETCH_QUERY
