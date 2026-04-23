"""Tests for the backfill_newline_titles script.

All database access is mocked -- these tests verify the cleaning logic
and batch processing without requiring a live database.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill = importlib.import_module("backfill_newline_titles")


# ---------------------------------------------------------------------------
# _clean_title unit tests
# ---------------------------------------------------------------------------


class TestCleanTitle:
    """Tests for the _clean_title helper."""

    def test_newline_replaced(self) -> None:
        assert backfill._clean_title("Husain\nv. McDonald") == "Husain v. McDonald"

    def test_carriage_return_replaced(self) -> None:
        assert backfill._clean_title("Husain\r\nv. McDonald") == "Husain v. McDonald"

    def test_tab_replaced(self) -> None:
        assert backfill._clean_title("Husain\tv. McDonald") == "Husain v. McDonald"

    def test_multiple_newlines_collapsed(self) -> None:
        assert backfill._clean_title("Smith\n\nv.\n\nJones") == "Smith v. Jones"

    def test_mixed_whitespace_collapsed(self) -> None:
        assert backfill._clean_title("Smith \n\t v. \r\n Jones") == "Smith v. Jones"

    def test_leading_trailing_stripped(self) -> None:
        assert backfill._clean_title("\nSmith v. Jones\n") == "Smith v. Jones"

    def test_already_clean_unchanged(self) -> None:
        assert backfill._clean_title("Smith v. Jones") == "Smith v. Jones"


# ---------------------------------------------------------------------------
# backfill_batch tests
# ---------------------------------------------------------------------------

_DEFAULT_CURSOR = backfill._CURSOR_MIN_UUID


class TestBackfillBatch:
    """Tests for backfill_batch()."""

    def test_no_rows_returns_zero(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        processed, updated, next_cursor = backfill.backfill_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )
        assert processed == 0
        assert updated == 0
        assert next_cursor == _DEFAULT_CURSOR

    def test_cleans_newline_and_updates(self) -> None:
        """Case with newline in title gets updated."""
        case_id = str(uuid.uuid4())
        row = (case_id, "Husain\nv. McDonald")

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]
        cur_update = MagicMock()

        contexts = [cur_fetch, cur_update]
        context_iter = iter(contexts)

        def cursor_ctx() -> MagicMock:
            ctx = MagicMock()
            cur = next(context_iter)
            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        conn.cursor.side_effect = cursor_ctx

        processed, updated, _cursor = backfill.backfill_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )

        assert processed == 1
        assert updated == 1

        # Verify the UPDATE was called with cleaned title
        update_args = cur_update.execute.call_args[0][1]
        assert update_args[0] == "Husain v. McDonald"
        assert update_args[1] == case_id

    def test_already_clean_skips_update(self) -> None:
        """Title without whitespace issues is not updated."""
        case_id = str(uuid.uuid4())
        row = (case_id, "Smith v. Jones")

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur_fetch)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        processed, updated, _cursor = backfill.backfill_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )

        assert processed == 1
        assert updated == 0


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_newline_titles.psycopg")
    @patch("backfill_newline_titles.backfill_batch")
    def test_dry_run_rolls_back_per_batch(
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
        mock_batch.side_effect = [(100, 5, cursor_1), (3, 1, cursor_2)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 103
        assert stats["total_updated"] == 6

    @patch("backfill_newline_titles.psycopg")
    @patch("backfill_newline_titles.backfill_batch")
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
        mock_batch.side_effect = [(100, 5, cursor_1), (30, 2, cursor_2)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100)

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 130
        assert stats["total_updated"] == 7

    @patch("backfill_newline_titles.psycopg")
    @patch("backfill_newline_titles.backfill_batch")
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
        mock_batch.side_effect = [(50, 3, cursor_1)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, limit=50)

        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 50  # effective_batch = min(100, 50)
        assert stats["total_processed"] == 50

    def test_query_uses_keyset_not_offset(self) -> None:
        """The query must use keyset pagination, not OFFSET."""
        assert "OFFSET" not in backfill.FETCH_QUERY
        assert "id > %s" in backfill.FETCH_QUERY
