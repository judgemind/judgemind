"""Tests for the backfill_judge_names script.

All database access is mocked -- these tests verify the extraction, comparison,
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
backfill = importlib.import_module("backfill_judge_names")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JUDGE_ID = str(uuid.uuid4())


def _make_judge_row(
    canonical_name: str,
    ruling_text: str,
    judge_id: str | None = None,
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        judge_id if judge_id is not None else _JUDGE_ID,
        canonical_name,
        ruling_text,
    )


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

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)
        assert processed == 0
        assert updated == 0

    def test_fixes_truncated_name(self) -> None:
        """Judge with truncated name gets updated when ruling text contains full name."""
        judge_id = str(uuid.uuid4())
        truncated_name = "James I. Montgomer"
        ruling_text = "The motion is GRANTED.\nJames I. Montgomery Judge of the Superior Court"
        row = _make_judge_row(truncated_name, ruling_text, judge_id=judge_id)

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        # We need separate cursors for fetch and updates
        cur_update_judge = MagicMock()
        cur_update_alias = MagicMock()
        contexts = [cur_fetch, cur_update_judge, cur_update_alias]
        context_iter = iter(contexts)

        def cursor_ctx() -> MagicMock:
            ctx = MagicMock()
            cur = next(context_iter)
            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        conn.cursor.side_effect = cursor_ctx

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 1

        # Verify UPDATE judges was called with the full name
        update_args = cur_update_judge.execute.call_args[0][1]
        assert update_args[0] == "James I. Montgomery"
        assert update_args[1] == judge_id

        # Verify UPDATE judge_aliases was also called
        alias_args = cur_update_alias.execute.call_args[0][1]
        assert alias_args[0] == "James I. Montgomery"
        assert alias_args[1] == judge_id
        assert alias_args[2] == truncated_name

    def test_skips_non_truncated_name(self) -> None:
        """Judge with full name is not updated even when ruling has the same name."""
        judge_id = str(uuid.uuid4())
        full_name = "James I. Montgomery"
        ruling_text = "The motion is GRANTED.\nJames I. Montgomery Judge of the Superior Court"
        row = _make_judge_row(full_name, ruling_text, judge_id=judge_id)

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur_fetch)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 0

    def test_skips_when_no_judge_name_extractable(self) -> None:
        """Judge whose rulings don't contain extractable names is skipped."""
        judge_id = str(uuid.uuid4())
        row = _make_judge_row(
            "John Smith",
            "The court sets a case management conference.",
            judge_id=judge_id,
        )

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur_fetch)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 0

    def test_picks_longest_name_from_multiple_rulings(self) -> None:
        """When a judge has multiple rulings, the longest extracted name wins."""
        judge_id = str(uuid.uuid4())
        truncated_name = "William A. Crowfoo"
        rows = [
            _make_judge_row(
                truncated_name,
                "The motion is GRANTED.\nWilliam A. Crowfoot Judge of the Superior Court",
                judge_id=judge_id,
            ),
            _make_judge_row(
                truncated_name,
                "Some text without a judge name.",
                judge_id=judge_id,
            ),
        ]

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = rows

        cur_update_judge = MagicMock()
        cur_update_alias = MagicMock()
        contexts = [cur_fetch, cur_update_judge, cur_update_alias]
        context_iter = iter(contexts)

        def cursor_ctx() -> MagicMock:
            ctx = MagicMock()
            cur = next(context_iter)
            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        conn.cursor.side_effect = cursor_ctx

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 1

        update_args = cur_update_judge.execute.call_args[0][1]
        assert update_args[0] == "William A. Crowfoot"

    def test_handles_sb_style_judge_name(self) -> None:
        """San Bernardino style 'Department S22 - Judge Bobby P. Luna' is extracted."""
        judge_id = str(uuid.uuid4())
        truncated_name = "Bobby P. Lun"
        ruling_text = "Department S22 - Judge Bobby P. Luna\nThe motion is denied."
        row = _make_judge_row(truncated_name, ruling_text, judge_id=judge_id)

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        cur_update_judge = MagicMock()
        cur_update_alias = MagicMock()
        contexts = [cur_fetch, cur_update_judge, cur_update_alias]
        context_iter = iter(contexts)

        def cursor_ctx() -> MagicMock:
            ctx = MagicMock()
            cur = next(context_iter)
            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        conn.cursor.side_effect = cursor_ctx

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 1

        update_args = cur_update_judge.execute.call_args[0][1]
        assert update_args[0] == "Bobby P. Luna"


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_judge_names.psycopg")
    @patch("backfill_judge_names.backfill_batch")
    def test_dry_run_rolls_back_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.side_effect = [(50, 10), (5, 1)]

        stats = backfill.run_backfill("postgresql://test", batch_size=50, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 55
        assert stats["total_updated"] == 11

    @patch("backfill_judge_names.psycopg")
    @patch("backfill_judge_names.backfill_batch")
    def test_commits_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.side_effect = [(50, 10), (20, 5)]

        stats = backfill.run_backfill("postgresql://test", batch_size=50)

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 70
        assert stats["total_updated"] == 15

    @patch("backfill_judge_names.psycopg")
    @patch("backfill_judge_names.backfill_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.side_effect = [(25, 5)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, limit=25)

        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 25  # effective_batch = min(100, 25)
        assert stats["total_processed"] == 25
