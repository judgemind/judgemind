"""Tests for the backfill_case_titles script.

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
backfill = importlib.import_module("backfill_case_titles")


# ---------------------------------------------------------------------------
# extract_case_title unit tests
# ---------------------------------------------------------------------------


class TestExtractCaseTitle:
    """Tests for the extract_case_title() regex function."""

    def test_standard_plaintiff_defendant(self) -> None:
        text = (
            "EMELITA BUENAVENTURA, et al.,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "CITY OF PASADENA, et al.,\n"
            "  Defendant(s).\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Buenaventura" in result
        assert "City Of Pasadena" in result
        assert " v. " in result

    def test_petitioner_respondent(self) -> None:
        text = "JOHN DOE,\n  Petitioner(s),\n  vs.\nSTATE OF CALIFORNIA,\n  Respondent(s).\n"
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "John Doe" in result
        assert "State Of California" in result
        assert " v. " in result

    def test_cross_complainant_cross_defendant(self) -> None:
        text = "ACME CORP,\n  Cross-Complainant(s),\n  vs.\nWIDGET INC,\n  Cross-Defendant(s).\n"
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Acme Corp" in result
        assert "Widget Inc" in result

    def test_no_parties_returns_none(self) -> None:
        text = "The court sets a case management conference for April 1."
        result = backfill.extract_case_title(text)
        assert result is None

    def test_flat_single_line_format(self) -> None:
        text = "SMITH, Plaintiff(s), vs. JONES, Defendant(s)."
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert " v. " in result

    def test_et_al_stripped_from_name(self) -> None:
        text = (
            "SUMAYYA AASI, et al.,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "AMERICAN HONDA MOTOR CO., INC., et al.,\n"
            "  Defendant(s).\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Aasi" in result
        assert "Honda" in result

    def test_title_case_formatting(self) -> None:
        text = "ALL CAPS NAME,\n  Plaintiff(s),\n  vs.\nANOTHER ALL CAPS,\n  Defendant(s).\n"
        result = backfill.extract_case_title(text)
        assert result is not None
        # Should be title-cased, not all caps
        assert "All Caps Name" in result
        assert "Another All Caps" in result

    def test_embedded_in_ruling_text(self) -> None:
        """Title extraction works when parties block is inside longer text."""
        text = (
            "SUPERIOR COURT OF CALIFORNIA\n"
            "COUNTY OF LOS ANGELES\n\n"
            "EMELITA BUENAVENTURA,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "CITY OF PASADENA,\n"
            "  Defendant(s).\n\n"
            "The motion for summary judgment is GRANTED.\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Buenaventura" in result
        assert "City Of Pasadena" in result


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

    def test_extracts_title_and_updates(self) -> None:
        """Case with ruling text containing party names gets updated."""
        case_id = str(uuid.uuid4())
        ruling_text = "JOHN SMITH,\n  Plaintiff(s),\n  vs.\nJANE DOE,\n  Defendant(s).\n"
        row = (case_id, ruling_text)

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

        # Verify the UPDATE was called with the extracted title
        update_args = cur_update.execute.call_args[0][1]
        assert "John Smith" in update_args[0]
        assert "Jane Doe" in update_args[0]
        assert " v. " in update_args[0]
        assert update_args[1] == case_id

    def test_no_extractable_title_skips_update(self) -> None:
        """Case with ruling text that has no party names is skipped."""
        case_id = str(uuid.uuid4())
        ruling_text = "The court sets a case management conference for April 1."
        row = (case_id, ruling_text)

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

    @patch("backfill_case_titles.psycopg")
    @patch("backfill_case_titles.backfill_batch")
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

        mock_batch.side_effect = [(5, 3)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, dry_run=True)

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 5
        assert stats["total_updated"] == 3

    @patch("backfill_case_titles.psycopg")
    @patch("backfill_case_titles.backfill_batch")
    def test_commits_on_success(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.side_effect = [(100, 80), (30, 20)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100)

        mock_conn.commit.assert_called_once()
        assert stats["total_processed"] == 130
        assert stats["total_updated"] == 100

    @patch("backfill_case_titles.psycopg")
    @patch("backfill_case_titles.backfill_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.side_effect = [(50, 40)]

        stats = backfill.run_backfill("postgresql://test", batch_size=100, limit=50)

        # The effective batch size should have been capped to 50
        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 50  # effective_batch = min(100, 50)
        assert stats["total_processed"] == 50
