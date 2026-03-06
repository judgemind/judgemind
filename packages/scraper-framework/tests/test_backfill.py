"""Tests for the backfill_ruling_fields script.

All database access is mocked — these tests verify the extraction and
update logic without requiring a live database.
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
backfill = importlib.import_module("backfill_ruling_fields")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COURT_ID = str(uuid.uuid4())


def _make_ruling_row(
    ruling_text: str,
    *,
    judge_id: str | None = None,
    outcome: str | None = None,
    motion_type: str | None = None,
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        str(uuid.uuid4()),  # r.id
        ruling_text,
        _COURT_ID,
        judge_id,
        outcome,
        motion_type,
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

    @patch("backfill_ruling_fields.resolve_judge")
    def test_extracts_outcome_and_motion_type(self, mock_resolve: MagicMock) -> None:
        """Ruling with text containing outcome/motion keywords gets updated."""
        ruling_text = "The motion for summary judgment is GRANTED."
        row = _make_ruling_row(ruling_text)

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]
        cur_update = MagicMock()

        # First cursor context = fetch, subsequent = update
        contexts = [cur_fetch, cur_update]
        context_iter = iter(contexts)

        def cursor_ctx() -> MagicMock:
            ctx = MagicMock()
            cur = next(context_iter)
            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        conn.cursor.side_effect = cursor_ctx

        # No judge name in this text
        mock_resolve.return_value = None

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 1

        # Verify the UPDATE was called with extracted values
        update_args = cur_update.execute.call_args[0][1]
        assert update_args[1] == "granted"  # outcome
        assert update_args[2] == "msj"  # motion_type

    @patch("backfill_ruling_fields.resolve_judge")
    def test_skips_already_populated(self, mock_resolve: MagicMock) -> None:
        """Ruling with all fields already populated is skipped."""
        ruling_text = "The motion is GRANTED."
        row = _make_ruling_row(
            ruling_text,
            judge_id=str(uuid.uuid4()),
            outcome="granted",
            motion_type="msj",
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
        mock_resolve.assert_not_called()

    @patch("backfill_ruling_fields.resolve_judge")
    def test_extracts_judge_name_from_la_text(self, mock_resolve: MagicMock) -> None:
        """LA-style ruling text with judge signature gets judge resolved."""
        ruling_text = "The motion is GRANTED.\nWilliam A. Crowfoot Judge of the Superior Court"
        row = _make_ruling_row(ruling_text)

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
        mock_resolve.return_value = "judge-uuid-123"

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 1

        # resolve_judge was called with the extracted name
        mock_resolve.assert_called_once_with(
            conn,
            "William A. Crowfoot",
            _COURT_ID,
        )

        # Verify judge_id was passed to UPDATE
        update_args = cur_update.execute.call_args[0][1]
        assert update_args[0] == "judge-uuid-123"

    @patch("backfill_ruling_fields.resolve_judge")
    def test_no_extractable_data_skips_update(self, mock_resolve: MagicMock) -> None:
        """Ruling with text that matches nothing gets skipped."""
        ruling_text = "The court sets a case management conference for April 1."
        row = _make_ruling_row(ruling_text)

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


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_ruling_fields.psycopg")
    @patch("backfill_ruling_fields.backfill_batch")
    def test_dry_run_rolls_back(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = (5, 3)

        # Only one batch (returns less than batch_size)
        mock_batch.side_effect = [(5, 3)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, dry_run=True)

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 5
        assert stats["total_updated"] == 3

    @patch("backfill_ruling_fields.psycopg")
    @patch("backfill_ruling_fields.backfill_batch")
    def test_commits_on_success(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        # Two batches: first full, second partial (signals end)
        mock_batch.side_effect = [(100, 80), (30, 20)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100)

        mock_conn.commit.assert_called_once()
        assert stats["total_processed"] == 130
        assert stats["total_updated"] == 100

    @patch("backfill_ruling_fields.psycopg")
    @patch("backfill_ruling_fields.backfill_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        # Limit 50, batch_size 100 — should process at most 50
        mock_batch.side_effect = [(50, 40)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, limit=50)

        # The effective batch size should have been capped to 50
        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 50  # effective_batch = min(100, 50)
        assert stats["total_processed"] == 50
