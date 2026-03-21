"""Tests for the backfill_normalize_caps_titles script.

All database access is mocked — these tests verify the normalization
and update logic without requiring a live database.
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
backfill = importlib.import_module("backfill_normalize_caps_titles")


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

        processed, updated, next_cursor = backfill.backfill_batch(conn, batch_size=10)
        assert processed == 0
        assert updated == 0

    def test_normalizes_all_caps_title(self) -> None:
        """ALL CAPS title gets normalized to title case."""
        case_id = str(uuid.uuid4())
        old_title = "SMITH VS JONES"
        row = (case_id, old_title)

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

        processed, updated, _cursor = backfill.backfill_batch(conn, batch_size=10)

        assert processed == 1
        assert updated == 1

        # Verify the UPDATE was called with the normalized title
        update_args = cur_update.execute.call_args[0][1]
        assert update_args[0] == "Smith v. Jones"
        assert update_args[1] == case_id

    def test_preserves_legal_acronyms(self) -> None:
        """Legal acronyms like LLC are preserved during normalization."""
        case_id = str(uuid.uuid4())
        old_title = "ACME LLC VS DOE CORP"
        row = (case_id, old_title)

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

        processed, updated, _cursor = backfill.backfill_batch(conn, batch_size=10)

        assert processed == 1
        assert updated == 1

        update_args = cur_update.execute.call_args[0][1]
        assert update_args[0] == "Acme LLC v. Doe Corp."

    def test_uses_keyset_pagination(self) -> None:
        """The query must use keyset pagination, not OFFSET."""
        assert "OFFSET" not in backfill.FETCH_QUERY
        assert "id > %s" in backfill.FETCH_QUERY


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_normalize_caps_titles.psycopg")
    @patch("backfill_normalize_caps_titles.backfill_batch")
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
        mock_batch.side_effect = [(10, 8, cursor_1), (5, 3, cursor_2)]

        stats = backfill.run_backfill("postgresql://test", batch_size=10, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 15
        assert stats["total_updated"] == 11

    @patch("backfill_normalize_caps_titles.psycopg")
    @patch("backfill_normalize_caps_titles.backfill_batch")
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
        mock_batch.side_effect = [(10, 8, cursor_1), (5, 3, cursor_2)]

        stats = backfill.run_backfill("postgresql://test", batch_size=10)

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 15
        assert stats["total_updated"] == 11

    @patch("backfill_normalize_caps_titles.psycopg")
    @patch("backfill_normalize_caps_titles.backfill_batch")
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
        mock_batch.side_effect = [(27, 25, cursor_1)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, limit=27)

        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 27  # effective_batch = min(100, 27)
        assert stats["total_processed"] == 27
