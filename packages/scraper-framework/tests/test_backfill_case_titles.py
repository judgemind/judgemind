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

    def test_real_la_format_with_full_header(self) -> None:
        """Real LA ruling format with newlines inside party names."""
        text = (
            "DEPARTMENT 3 LAW AND MOTION RULINGS\n"
            "Case Number:\n"
            "22STCV29629\n"
            "Hearing Date:\n"
            "March 5, 2026\n"
            "Dept:\n"
            "3\n"
            "SUPERIOR COURT OF THE STATE OF\n"
            "CALIFORNIA\n"
            "FOR THE COUNTY OF LOS ANGELES - NORTHEAST\n"
            "DISTRICT\n"
            "EMELITA\n"
            "   BUENAVENTURA\n"
            ",\n"
            "Plaintiff(s),\n"
            "vs.\n"
            "CITY OF\n"
            "   PASADENA\n"
            ",\n"
            "Defendant(s).\n"
            ")\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        # Must NOT include the header text
        assert "Department" not in result
        assert "Superior Court" not in result.lower()
        assert "Buenaventura" in result
        assert "Pasadena" in result
        assert " v. " in result

    def test_real_la_mixed_case_with_descriptor(self) -> None:
        """Mixed case names with descriptors like 'an individual'."""
        text = (
            "DISTRICT\n"
            "KRISTI BERTRAM, an individual,\n"
            "Plaintiff,\n"
            "vs.\n"
            "CITY OF LOS ANGELES, a\n"
            "  public entity; STATE OF CALIFORNIA, a public entity,\n"
            "Defendants.\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Bertram" in result
        assert " v. " in result

    def test_v_without_s(self) -> None:
        """Handle 'v.' (not 'vs.') as separator."""
        text = "MAYA KADOSH\n,\nPlaintiff\n,\nv.\nMOUSSA MASJEDI\n,\nDefendant\n"
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Kadosh" in result
        assert "Masjedi" in result


# ---------------------------------------------------------------------------
# MOVING PARTY / RESPONDING PARTY pattern tests
# ---------------------------------------------------------------------------


class TestExtractFromMovingResponding:
    """Tests for the MOVING PARTY / RESPONDING PARTY extraction pattern."""

    def test_basic_moving_responding(self) -> None:
        """Extract title from MOVING PARTY + RESPONDING PARTY fields."""
        text = (
            "MOVING PARTY: Defendant Acme Corporation.\n"
            "RESPONDING PARTY: Plaintiff John Smith.\n"
            "The motion is GRANTED.\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Acme Corporation" in result
        assert "John Smith" in result
        assert " v. " in result

    def test_strips_role_prefix_defendant(self) -> None:
        """Role prefix 'Defendant' is stripped from party names."""
        text = (
            "MOVING PARTY: Defendant Rayne Dealership Corporation.\n"
            "RESPONDING PARTY: Plaintiff Jane Doe.\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Rayne Dealership Corporation" in result
        assert "Defendant" not in result
        assert "Plaintiff" not in result

    def test_strips_role_prefix_plaintiffs(self) -> None:
        """Role prefix 'Plaintiffs' (plural) is stripped."""
        text = (
            "MOVING PARTY: Defendants Ashley Willowbrook LP and Ashley Willowbrook GP LP.\n"
            "RESPONDING PARTY: Plaintiffs David Keichline, Claudia Lopez, and Mason Keichline.\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Ashley Willowbrook" in result
        assert "David Keichline" in result
        assert "Defendants" not in result
        assert "Plaintiffs" not in result

    def test_no_opposition_returns_none(self) -> None:
        """When responding party is 'No opposition filed', return None."""
        text = (
            "MOVING PARTY: Defendant Rayne Dealership Corporation.\n"
            "RESPONDING PARTY: No opposition filed.\n"
        )
        result = backfill.extract_case_title(text)
        # No opposing party means we can't construct a vs. title.
        # The caption block fallback might still work, but with just
        # MOVING/RESPONDING fields and no caption, we return None.
        assert result is None

    def test_opposing_party_keyword(self) -> None:
        """OPPOSING PARTY works the same as RESPONDING PARTY."""
        text = "MOVING PARTY: Defendant Big Corp.\nOPPOSING PARTY: Plaintiff Small LLC.\n"
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Big Corp" in result
        assert "Small Llc" in result

    def test_moving_party_embedded_in_ruling(self) -> None:
        """MOVING/RESPONDING PARTY fields work when surrounded by other text."""
        text = (
            "DEPARTMENT F46 LAW AND MOTION RULINGS\n"
            "Case Number: 21CHCV00539\n"
            "Hearing Date: March 2, 2026\n\n"
            "MOVING PARTY: Defendant Rayne Dealership Corporation.\n"
            "RESPONDING PARTY: Plaintiffs Alpha Beta and Gamma Delta.\n\n"
            "RELIEF REQUESTED: Motion for summary judgment.\n"
            "The motion is DENIED.\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Rayne Dealership Corporation" in result
        assert "Alpha Beta" in result


# ---------------------------------------------------------------------------
# Case Name / Case Title field pattern tests
# ---------------------------------------------------------------------------


class TestExtractFromCaseNameField:
    """Tests for the Case Name / Case Title inline field extraction."""

    def test_case_name_field(self) -> None:
        """Extract title from 'CASE NAME:' field."""
        text = (
            "CASE NAME: Porsche Leasing Ltd. et al. v. Tsisana Mikia, et al. "
            "CASE NUMBER: 25SMCV01132\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Porsche" in result
        assert "Mikia" in result
        assert "v." in result

    def test_case_title_field(self) -> None:
        """Extract title from 'CASE TITLE:' field (alternate label)."""
        text = "CASE TITLE: Smith Corp v. Jones Industries CASE NUMBER: 22SMCV01940\n"
        result = backfill.extract_case_title(text)
        assert result is not None
        assert "Smith Corp" in result
        assert "Jones Industries" in result

    def test_case_name_without_v_returns_none(self) -> None:
        """Case Name field without 'v.' is not a party title."""
        text = "CASE NAME: Motion for Summary Judgment CASE NUMBER: 22STCV12345\n"
        result = backfill.extract_case_title(text)
        assert result is None

    def test_caption_block_preferred_over_case_name(self) -> None:
        """When both caption block and Case Name exist, caption block wins."""
        text = (
            "CASE NAME: Wrong Title v. Wrong Party CASE NUMBER: 12345\n"
            "JOHN SMITH,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "JANE DOE,\n"
            "  Defendant(s).\n"
        )
        result = backfill.extract_case_title(text)
        assert result is not None
        # Caption block should win
        assert "John Smith" in result
        assert "Jane Doe" in result


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
