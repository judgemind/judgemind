"""Tests for the backfill_la_entity_descriptors script (#1267).

All database access is mocked — these tests verify the title sanitization
and extraction logic without requiring a live database.
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
backfill = importlib.import_module("backfill_la_entity_descriptors")


# ---------------------------------------------------------------------------
# sanitize_title — direct title cleaning
# ---------------------------------------------------------------------------


class TestSanitizeTitle:
    """Tests for the sanitize_title() function."""

    def test_strips_an_individual(self) -> None:
        title = "Jim Hilaski, An Individual v. Shaul Dina, An Individual"
        result = backfill.sanitize_title(title)
        assert result is not None
        assert "An Individual" not in result
        assert "Hilaski" in result
        assert "Dina" in result

    def test_strips_california_corporation(self) -> None:
        title = (
            "Old Master Products, Inc., A California Corporation "
            "v. Big Corp, A Delaware Corporation"
        )
        result = backfill.sanitize_title(title)
        assert result is not None
        assert "Corporation" not in result
        assert "Old Master Products" in result

    def test_strips_derivatively_on_behalf(self) -> None:
        title = (
            "Jim Hilaski, An Individual And Derivatively On Behalf Of "
            "Old Master Products, Inc. v. Shaul Dina, An Individual"
        )
        result = backfill.sanitize_title(title)
        assert result is not None
        assert "Derivatively" not in result
        assert "Hilaski" in result
        assert "Dina" in result

    def test_strips_does(self) -> None:
        title = "Smith v. Jones; Does 1 To 20, Inclusive"
        result = backfill.sanitize_title(title)
        assert result is not None
        assert "Does" not in result

    def test_normalizes_vs(self) -> None:
        title = "Jim Hilaski, An Individual vs. Shaul Dina, An Individual"
        result = backfill.sanitize_title(title)
        assert result is not None
        assert " v. " in result
        assert "vs." not in result

    def test_passes_normal_title(self) -> None:
        assert backfill.sanitize_title("Aasi v. Honda") == "Aasi v. Honda"

    def test_rejects_none(self) -> None:
        assert backfill.sanitize_title(None) is None

    def test_rejects_empty(self) -> None:
        assert backfill.sanitize_title("") is None

    def test_rejects_too_long_after_cleaning(self) -> None:
        long_title = "A" * 60 + " v. " + "B" * 60
        assert backfill.sanitize_title(long_title) is None

    def test_rejects_department_header(self) -> None:
        title = "Department 15 Law And Motion Rulings Something v. Someone"
        assert backfill.sanitize_title(title) is None

    def test_strips_llc(self) -> None:
        title = "Smith v. Acme Holdings, A Limited Liability Company"
        result = backfill.sanitize_title(title)
        assert result is not None
        assert "Limited Liability" not in result
        assert "Acme Holdings" in result

    def test_strips_trustee(self) -> None:
        title = "Smith, As Trustee Of The Smith Family Trust v. Jones"
        result = backfill.sanitize_title(title)
        assert result is not None
        assert "Trustee" not in result
        assert "Smith" in result
        assert "Jones" in result


# ---------------------------------------------------------------------------
# sanitize_title_lenient — lenient length for backfill
# ---------------------------------------------------------------------------


class TestSanitizeTitleLenient:
    """Tests for the sanitize_title_lenient() function."""

    def test_accepts_title_between_120_and_250(self) -> None:
        """Titles > 120 chars but <= 250 chars pass the lenient check."""
        # Build a title that is > 120 chars after cleaning
        long_plaintiff = "Smith, Jones, Williams, Brown, Davis, Miller, Wilson, Thompson, Garcia"
        long_defendant = "Anderson, Taylor, Thomas, Jackson, White, Harris, Martin, Robinson, Clark"
        title = f"{long_plaintiff} v. {long_defendant}"
        assert len(title) > 120
        assert len(title) <= 250
        result = backfill.sanitize_title_lenient(title)
        assert result is not None
        assert result == title

    def test_rejects_title_over_250(self) -> None:
        """Titles > 250 chars are rejected even with lenient check."""
        title = "A" * 125 + " v. " + "B" * 125
        assert backfill.sanitize_title_lenient(title) is None

    def test_strips_descriptors_from_long_title(self) -> None:
        """Entity descriptors are stripped from multi-party titles."""
        title = (
            "Safoura Majma, An Individual, Kaveh Majma, An Individual "
            "v. Oak Enterprises, A California Corporation, Bob Smith, An Individual"
        )
        result = backfill.sanitize_title_lenient(title)
        assert result is not None
        assert "An Individual" not in result
        assert "A California Corporation" not in result
        assert "Safoura Majma" in result
        assert "Oak Enterprises" in result


# ---------------------------------------------------------------------------
# clean_case_title — combined strategy
# ---------------------------------------------------------------------------


class TestCleanCaseTitle:
    """Tests for the clean_case_title() orchestration function."""

    def test_strategy1_sanitize_succeeds(self) -> None:
        """When sanitize_title works, ruling_text is not needed."""
        old = "Jim Hilaski, An Individual v. Shaul Dina, An Individual"
        result = backfill.clean_case_title(old, ruling_text=None)
        assert result is not None
        assert "An Individual" not in result

    def test_strategy2_fallback_to_caption(self) -> None:
        """When sanitize_title returns None, extract from ruling text caption."""
        # A title too long even for lenient sanitization (> 250 chars)
        old = "A" * 260
        ruling_text = (
            "Smith\n  Plaintiff(s),\n  vs.\n  Jones\n  Defendant(s).\nMOVING PARTY: Someone"
        )
        result = backfill.clean_case_title(old, ruling_text)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result

    def test_strategy2_fallback_to_moving_responding(self) -> None:
        """When caption fails, fall back to MOVING/RESPONDING PARTY fields."""
        old = "A" * 260
        ruling_text = (
            "MOVING PARTY: Plaintiff Alice Walker.\nRESPONDING PARTY: Defendant Bob Smith.\n"
        )
        result = backfill.clean_case_title(old, ruling_text)
        assert result is not None
        assert "Walker" in result
        assert "Smith" in result

    def test_returns_none_when_all_fail(self) -> None:
        """Returns None when both strategies fail."""
        old = "A" * 260
        result = backfill.clean_case_title(old, ruling_text="no useful content")
        assert result is None

    def test_returns_none_no_ruling_text(self) -> None:
        """Returns None when sanitize fails and no ruling text available."""
        old = "A" * 260
        result = backfill.clean_case_title(old, ruling_text=None)
        assert result is None

    def test_lenient_sanitize_preferred_over_ruling_text(self) -> None:
        """Multi-party titles > 120 chars are cleaned via lenient sanitize."""
        old = (
            "Safoura Majma, An Individual, Kaveh Majma, An Individual "
            "v. Oak Enterprises, A California Corporation, Bob Smith, An Individual"
        )
        result = backfill.clean_case_title(old, ruling_text=None)
        assert result is not None
        assert "An Individual" not in result
        assert "A California Corporation" not in result


# ---------------------------------------------------------------------------
# main() — DB integration (mocked)
# ---------------------------------------------------------------------------


_COURT_ID = str(uuid.uuid4())


def _make_case_row(
    case_title: str,
    *,
    case_id: str | None = None,
    case_number: str = "24STCV01234",
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        case_id or str(uuid.uuid4()),
        case_number,
        case_title,
    )


def _make_ruling_row(ruling_text: str) -> tuple:
    """Return a tuple matching the ruling text query columns."""
    return (ruling_text,)


class TestMain:
    """Tests for the main() entry point with mocked DB."""

    @patch("backfill_la_entity_descriptors.psycopg")
    def test_dry_run_does_not_write(self, mock_psycopg: MagicMock) -> None:
        """In dry-run mode, no UPDATE is issued."""
        case_id = str(uuid.uuid4())
        long_title = (
            "Jim Hilaski, An Individual And Derivatively On Behalf Of "
            "Old Master Products, Inc., A California Corporation "
            "v. Shaul Dina, An Individual"
        )

        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        # First cursor: fetch long cases
        mock_cur1 = MagicMock()
        mock_cur1.fetchall.return_value = [
            _make_case_row(long_title, case_id=case_id),
        ]
        mock_cur1.__enter__ = MagicMock(return_value=mock_cur1)
        mock_cur1.__exit__ = MagicMock(return_value=False)

        # Second cursor: fetch ruling text
        mock_cur2 = MagicMock()
        mock_cur2.fetchone.return_value = None
        mock_cur2.__enter__ = MagicMock(return_value=mock_cur2)
        mock_cur2.__exit__ = MagicMock(return_value=False)

        mock_conn.cursor.side_effect = [mock_cur1, mock_cur2]

        with patch("sys.argv", ["backfill", "--dry-run"]):
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                backfill.main()

        # No commit should have been called
        mock_conn.commit.assert_not_called()

    @patch("backfill_la_entity_descriptors.psycopg")
    def test_updates_when_not_dry_run(self, mock_psycopg: MagicMock) -> None:
        """When not in dry-run mode, UPDATE + commit is issued."""
        case_id = str(uuid.uuid4())
        long_title = "Jim Hilaski, An Individual v. Shaul Dina, An Individual"

        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        # First cursor: fetch long cases
        mock_cur1 = MagicMock()
        mock_cur1.fetchall.return_value = [
            _make_case_row(long_title, case_id=case_id),
        ]
        mock_cur1.__enter__ = MagicMock(return_value=mock_cur1)
        mock_cur1.__exit__ = MagicMock(return_value=False)

        # Second cursor: fetch ruling text (not needed, sanitize will succeed)
        mock_cur2 = MagicMock()
        mock_cur2.fetchone.return_value = None
        mock_cur2.__enter__ = MagicMock(return_value=mock_cur2)
        mock_cur2.__exit__ = MagicMock(return_value=False)

        # Third cursor: UPDATE
        mock_cur3 = MagicMock()
        mock_cur3.__enter__ = MagicMock(return_value=mock_cur3)
        mock_cur3.__exit__ = MagicMock(return_value=False)

        mock_conn.cursor.side_effect = [mock_cur1, mock_cur2, mock_cur3]

        with patch("sys.argv", ["backfill"]):
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                backfill.main()

        # An UPDATE should have been executed
        mock_cur3.execute.assert_called_once()
        update_args = mock_cur3.execute.call_args
        assert "UPDATE cases SET case_title" in update_args[0][0]
        assert case_id in update_args[0][1]
        mock_conn.commit.assert_called()

    @patch("backfill_la_entity_descriptors.psycopg")
    def test_skips_when_title_unchanged(self, mock_psycopg: MagicMock) -> None:
        """Cases where sanitize produces same title are skipped."""
        # A short title that won't change
        short_title = "Smith v. Jones"

        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        mock_cur1 = MagicMock()
        mock_cur1.fetchall.return_value = [
            _make_case_row(short_title),
        ]
        mock_cur1.__enter__ = MagicMock(return_value=mock_cur1)
        mock_cur1.__exit__ = MagicMock(return_value=False)

        mock_cur2 = MagicMock()
        mock_cur2.fetchone.return_value = None
        mock_cur2.__enter__ = MagicMock(return_value=mock_cur2)
        mock_cur2.__exit__ = MagicMock(return_value=False)

        mock_conn.cursor.side_effect = [mock_cur1, mock_cur2]

        with patch("sys.argv", ["backfill"]):
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                backfill.main()

        # No commit since nothing changed
        mock_conn.commit.assert_not_called()
