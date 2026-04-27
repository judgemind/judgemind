"""Tests for scripts/audit_oc_ruling_integrity.py.

Tests the core audit logic: misroute detection (ruling_text party names vs
case_title) and boilerplate detection, plus CLI output formatting.  All DB
access is mocked — no real database required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers — the script lives in scripts/, not in a package, so we add
# it to sys.path dynamically.
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_oc_ruling_integrity as audit  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Fixtures — sample ruling rows
# ---------------------------------------------------------------------------


def _make_row(
    ruling_id: str = "r1",
    case_id: str = "c1",
    case_title: str | None = "Smith v. Jones",
    ruling_text: str | None = "The motion by Smith against Jones is GRANTED.",
    ruling_len: int | None = None,
    county: str = "Orange",
) -> dict[str, Any]:
    """Build a dict mimicking a ruling row from the audit query."""
    if ruling_len is None:
        ruling_len = len(ruling_text) if ruling_text else 0
    return {
        "ruling_id": ruling_id,
        "case_id": case_id,
        "case_title": case_title,
        "ruling_text": ruling_text,
        "ruling_len": ruling_len,
        "county": county,
    }


# ---------------------------------------------------------------------------
# Party-name extraction
# ---------------------------------------------------------------------------


class TestExtractPartiesFromTitle:
    """Test _extract_parties_from_title()."""

    def test_standard_v_dot(self) -> None:
        parties = audit._extract_parties_from_title("Smith v. Jones")
        assert parties == ("smith", "jones")

    def test_standard_vs(self) -> None:
        parties = audit._extract_parties_from_title("Doe vs Garcia")
        assert parties == ("doe", "garcia")

    def test_title_case_vs_dot(self) -> None:
        parties = audit._extract_parties_from_title("People vs. Smith")
        assert parties == ("people", "smith")

    def test_no_vs(self) -> None:
        """Titles without v./vs return None."""
        result = audit._extract_parties_from_title("In re Marriage of Smith")
        assert result is None

    def test_empty_string(self) -> None:
        result = audit._extract_parties_from_title("")
        assert result is None

    def test_none_title(self) -> None:
        result = audit._extract_parties_from_title(None)
        assert result is None

    def test_strips_whitespace(self) -> None:
        parties = audit._extract_parties_from_title("  Smith  v.  Jones  ")
        assert parties == ("smith", "jones")

    def test_complex_names(self) -> None:
        parties = audit._extract_parties_from_title(
            "Zavala Construction Inc v. Becker Holdings LLC"
        )
        assert parties is not None
        left, right = parties
        assert "zavala" in left
        assert "becker" in right


# ---------------------------------------------------------------------------
# Misroute detection
# ---------------------------------------------------------------------------


class TestCheckMisroute:
    """Test _check_misroute() — does ruling_text mention different parties?"""

    def test_matching_parties_not_flagged(self) -> None:
        row = _make_row(
            case_title="Smith v. Jones",
            ruling_text="The motion by Smith against Jones is GRANTED.",
        )
        result = audit._check_misroute(row)
        assert result is None

    def test_mismatched_parties_flagged(self) -> None:
        row = _make_row(
            case_title="Smith v. Jones",
            ruling_text="The motion by Zavala against Becker is DENIED.",
        )
        result = audit._check_misroute(row)
        assert result is not None
        assert "misroute" in result.lower() or "mismatch" in result.lower()

    def test_no_vs_in_title_skipped(self) -> None:
        """Titles without v./vs cannot be checked — should return None."""
        row = _make_row(
            case_title="In re Marriage of Smith",
            ruling_text="The motion is GRANTED.",
        )
        result = audit._check_misroute(row)
        assert result is None

    def test_none_ruling_text_skipped(self) -> None:
        row = _make_row(
            case_title="Smith v. Jones",
            ruling_text=None,
        )
        result = audit._check_misroute(row)
        assert result is None

    def test_none_case_title_skipped(self) -> None:
        row = _make_row(
            case_title=None,
            ruling_text="Some text about Smith v. Jones.",
        )
        result = audit._check_misroute(row)
        assert result is None

    def test_partial_name_match_ok(self) -> None:
        """If at least one party name appears in ruling_text, not flagged."""
        row = _make_row(
            case_title="Smith v. Jones",
            ruling_text="Plaintiff Smith's motion for summary judgment is GRANTED.",
        )
        result = audit._check_misroute(row)
        assert result is None


# ---------------------------------------------------------------------------
# Boilerplate detection
# ---------------------------------------------------------------------------


class TestCheckBoilerplate:
    """Test _check_boilerplate()."""

    def test_short_with_keywords_flagged(self) -> None:
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION\n"
            "Dept. N7\n"
            "Date: 01/15/2025\n"
            "1. Smith v. Jones\n"
            "MOTION for Summary Judgment\n"
        )
        row = _make_row(ruling_text=text)
        result = audit._check_boilerplate(row)
        assert result is not None
        assert "boilerplate" in result.lower()

    def test_long_text_not_flagged(self) -> None:
        """Substantive ruling text (>700 chars) is never boilerplate."""
        row = _make_row(ruling_text="The court has reviewed " + "x" * 700)
        result = audit._check_boilerplate(row)
        assert result is None

    def test_short_without_keywords_not_flagged(self) -> None:
        """Short text that doesn't have boilerplate keywords is not flagged."""
        row = _make_row(ruling_text="The motion is GRANTED.")
        result = audit._check_boilerplate(row)
        assert result is None

    def test_none_text_not_flagged(self) -> None:
        row = _make_row(ruling_text=None)
        result = audit._check_boilerplate(row)
        assert result is None


# ---------------------------------------------------------------------------
# Full audit run (mocked DB)
# ---------------------------------------------------------------------------


class TestRunAudit:
    """Test run_audit() with mocked database connection."""

    @staticmethod
    def _mock_rows() -> list[tuple]:
        """Return sample rows as the DB query would produce."""
        return [
            # (ruling_id, case_id, case_title, ruling_text, ruling_len, county)
            # Good ruling — matching parties
            (
                "r1",
                "c1",
                "Smith v. Jones",
                "Smith's motion against Jones is GRANTED.",
                42,
                "Orange",
            ),
            # Misrouted — ruling text mentions different parties
            (
                "r2",
                "c2",
                "Smith v. Jones",
                "Zavala motion against Becker is DENIED.",
                40,
                "Orange",
            ),
            # Boilerplate — short with keywords
            (
                "r3",
                "c3",
                "Doe v. Roe",
                "TENTATIVE RULINGS\nLAW & MOTION\nDept N7\nMOTION\n",
                50,
                "Orange",
            ),
        ]

    def test_finds_misrouted_and_boilerplate(self) -> None:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = self._mock_rows()

        with patch("psycopg.connect", return_value=mock_conn):
            result = audit.run_audit("postgresql://test", output_json=True)

        assert not result["all_clean"]
        assert len(result["misrouted"]) >= 1
        assert len(result["boilerplate"]) >= 1

    def test_clean_db_returns_all_clean(self) -> None:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            (
                "r1",
                "c1",
                "Smith v. Jones",
                "Smith's motion against Jones is GRANTED.",
                42,
                "Orange",
            ),
        ]

        with patch("psycopg.connect", return_value=mock_conn):
            result = audit.run_audit("postgresql://test", output_json=True)

        assert result["all_clean"]
        assert len(result["misrouted"]) == 0
        assert len(result["boilerplate"]) == 0


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


class TestOutputFormatting:
    """Test JSON and text output formatting."""

    def test_json_output_structure(self) -> None:
        """JSON result contains expected keys."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        with patch("psycopg.connect", return_value=mock_conn):
            result = audit.run_audit("postgresql://test", output_json=True)

        assert "all_clean" in result
        assert "misrouted" in result
        assert "boilerplate" in result
        assert "total_checked" in result

    def test_text_output_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Text output path runs without error."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            ("r1", "c1", "Smith v. Jones", "Zavala v. Becker ruling text.", 30, "Orange"),
        ]

        with patch("psycopg.connect", return_value=mock_conn):
            audit.run_audit("postgresql://test", output_json=False)

        captured = capsys.readouterr()
        assert "ruling" in captured.out.lower() or "audit" in captured.out.lower()
