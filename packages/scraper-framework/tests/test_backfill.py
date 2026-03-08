"""Tests for the backfill_ruling_fields script.

All database access is mocked — these tests verify the extraction and
update logic without requiring a live database.
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
backfill = importlib.import_module("backfill_ruling_fields")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COURT_ID = str(uuid.uuid4())
_CASE_ID = str(uuid.uuid4())
_HEARING_DATE = date(2026, 3, 5)


def _make_ruling_row(
    ruling_text: str,
    *,
    judge_id: str | None = None,
    outcome: str | None = None,
    motion_type: str | None = None,
    case_id: str | None = None,
    hearing_date: date | None = None,
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        str(uuid.uuid4()),  # r.id
        ruling_text,
        _COURT_ID,
        judge_id,
        outcome,
        motion_type,
        case_id if case_id is not None else _CASE_ID,
        hearing_date if hearing_date is not None else _HEARING_DATE,
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

    @patch("backfill_ruling_fields.upsert_case_judge")
    @patch("backfill_ruling_fields.resolve_judge")
    def test_extracts_judge_name_from_la_text(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock
    ) -> None:
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

        # Verify case_judges link was created
        mock_upsert_cj.assert_called_once_with(conn, _CASE_ID, "judge-uuid-123", _HEARING_DATE)

    @patch("backfill_ruling_fields.upsert_case_judge")
    @patch("backfill_ruling_fields.resolve_judge")
    def test_existing_judge_id_creates_case_judges_link(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock
    ) -> None:
        """Ruling with existing judge_id but NULL outcome still gets case_judges link."""
        existing_judge = str(uuid.uuid4())
        ruling_text = "The motion for summary judgment is GRANTED."
        row = _make_ruling_row(ruling_text, judge_id=existing_judge)

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

        processed, updated = backfill.backfill_batch(conn, batch_size=10, offset=0)

        assert processed == 1
        assert updated == 1

        # resolve_judge should NOT have been called (judge_id already set)
        mock_resolve.assert_not_called()

        # case_judges link should still be created using existing judge_id
        mock_upsert_cj.assert_called_once_with(conn, _CASE_ID, existing_judge, _HEARING_DATE)

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


class TestBackfillCaseJudgesBatch:
    """Tests for backfill_case_judges_batch()."""

    @patch("backfill_ruling_fields.upsert_case_judge")
    def test_no_orphan_rows(self, mock_upsert_cj: MagicMock) -> None:
        """When no rulings are missing case_judges links, returns 0."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        created = backfill.backfill_case_judges_batch(conn, batch_size=10, offset=0)
        assert created == 0
        mock_upsert_cj.assert_not_called()

    @patch("backfill_ruling_fields.upsert_case_judge")
    def test_creates_missing_case_judges(self, mock_upsert_cj: MagicMock) -> None:
        """Creates case_judges for rulings that have judge_id but no link."""
        case_id_1 = str(uuid.uuid4())
        judge_id_1 = str(uuid.uuid4())
        case_id_2 = str(uuid.uuid4())
        judge_id_2 = str(uuid.uuid4())
        hd = date(2026, 3, 5)

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [
            (case_id_1, judge_id_1, hd),
            (case_id_2, judge_id_2, hd),
        ]

        created = backfill.backfill_case_judges_batch(conn, batch_size=10, offset=0)

        assert created == 2
        assert mock_upsert_cj.call_count == 2
        mock_upsert_cj.assert_any_call(conn, case_id_1, judge_id_1, hd)
        mock_upsert_cj.assert_any_call(conn, case_id_2, judge_id_2, hd)


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_ruling_fields.backfill_case_judges_batch")
    @patch("backfill_ruling_fields.psycopg")
    @patch("backfill_ruling_fields.backfill_batch")
    def test_dry_run_rolls_back_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_cj_batch: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        # Two batches: first full, second partial (signals end)
        mock_batch.side_effect = [(100, 80), (5, 3)]
        mock_cj_batch.return_value = 0  # no orphan case_judges

        stats = backfill.run_backfill("postgresql://test", batch_size=100, dry_run=True)

        # Rollback is called after each batch in dry-run mode
        # (2 ruling batches + 1 case_judges batch)
        assert mock_conn.rollback.call_count == 3
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 105
        assert stats["total_updated"] == 83

    @patch("backfill_ruling_fields.backfill_case_judges_batch")
    @patch("backfill_ruling_fields.psycopg")
    @patch("backfill_ruling_fields.backfill_batch")
    def test_commits_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_cj_batch: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        # Two batches: first full, second partial (signals end)
        mock_batch.side_effect = [(100, 80), (30, 20)]
        mock_cj_batch.return_value = 0  # no orphan case_judges

        stats = backfill.run_backfill("postgresql://test", batch_size=100)

        # Commit is called after each batch for resilience
        # (2 ruling batches + 1 case_judges batch)
        assert mock_conn.commit.call_count == 3
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 130
        assert stats["total_updated"] == 100

    @patch("backfill_ruling_fields.backfill_case_judges_batch")
    @patch("backfill_ruling_fields.psycopg")
    @patch("backfill_ruling_fields.backfill_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_cj_batch: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        # Limit 50, batch_size 100 — should process at most 50
        mock_batch.side_effect = [(50, 40)]
        mock_cj_batch.return_value = 0

        stats = backfill.run_backfill("postgresql://test", batch_size=100, limit=50)

        # The effective batch size should have been capped to 50
        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 50  # effective_batch = min(100, 50)
        assert stats["total_processed"] == 50

    @patch("backfill_ruling_fields.backfill_case_judges_batch")
    @patch("backfill_ruling_fields.psycopg")
    @patch("backfill_ruling_fields.backfill_batch")
    def test_case_judges_second_pass_runs(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_cj_batch: MagicMock,
    ) -> None:
        """The second pass creates case_judges rows for previously-backfilled rulings."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        # No ruling field updates needed
        mock_batch.side_effect = [(0, 0)]
        # 100 orphan case_judges links (full batch), then 5 more (partial = done)
        mock_cj_batch.side_effect = [100, 5]

        stats = backfill.run_backfill("postgresql://test", batch_size=100)

        assert stats["total_case_judges_created"] == 105
        assert mock_cj_batch.call_count == 2  # first full batch, then partial
