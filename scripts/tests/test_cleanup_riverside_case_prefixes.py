"""Tests for cleanup_riverside_case_prefixes script.

Tests cover:
- strip_case_number_prefix for various Riverside case number formats
- is_riverside_case_number_prefix detection
- cleanup_case_titles with mocked DB (dry-run and apply)
- cleanup_party_names with mocked DB
- cleanup_alias_names with mocked DB
- run_cleanup end-to-end with mocked DB
- CLI argument parsing

The script depends on psycopg, which may not be available in the
lightweight CI scripts-tests environment.  We mock it in sys.modules
before importing the script under test.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports psycopg at module level,
# which may not be installed in the CI scripts-tests environment.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_mock_psycopg = MagicMock()

_modules_to_mock = {
    "psycopg": _mock_psycopg,
}

_saved_modules: dict[str, object] = {}
for mod_name, mock_mod in _modules_to_mock.items():
    if mod_name in sys.modules:
        _saved_modules[mod_name] = sys.modules[mod_name]
    sys.modules[mod_name] = mock_mod

import cleanup_riverside_case_prefixes  # noqa: E402

strip_case_number_prefix = cleanup_riverside_case_prefixes.strip_case_number_prefix
is_riverside_case_number_prefix = (
    cleanup_riverside_case_prefixes.is_riverside_case_number_prefix
)
cleanup_case_titles = cleanup_riverside_case_prefixes.cleanup_case_titles
cleanup_party_names = cleanup_riverside_case_prefixes.cleanup_party_names
cleanup_alias_names = cleanup_riverside_case_prefixes.cleanup_alias_names
run_cleanup = cleanup_riverside_case_prefixes.run_cleanup


# ---------------------------------------------------------------------------
# strip_case_number_prefix
# ---------------------------------------------------------------------------


class TestStripCaseNumberPrefix:
    """Tests for the strip_case_number_prefix pure function."""

    def test_cvps_prefix(self) -> None:
        """Strips CVPS-style prefix."""
        assert strip_case_number_prefix(
            "CVPS2405799 Aragon v. J And S Hospitality, Inc."
        ) == ("Aragon v. J And S Hospitality, Inc.")

    def test_cvri_prefix(self) -> None:
        """Strips CVRI-style prefix."""
        assert strip_case_number_prefix("CVRI2306607 Cao v. Hu") == "Cao v. Hu"

    def test_cvmv_prefix(self) -> None:
        """Strips CVMV-style prefix."""
        assert (
            strip_case_number_prefix("CVMV2507098 Smith v. Jones") == "Smith v. Jones"
        )

    def test_ric_prefix(self) -> None:
        """Strips RIC-style prefix."""
        assert strip_case_number_prefix("RIC1904113 Doe v. Roe") == "Doe v. Roe"

    def test_mcc_prefix(self) -> None:
        """Strips MCC-style prefix."""
        assert strip_case_number_prefix("MCC2012345 Alpha v. Beta") == "Alpha v. Beta"

    def test_psc_prefix(self) -> None:
        """Strips PSC-style prefix."""
        assert strip_case_number_prefix("PSC2401234 Plaintiff v. Defendant") == (
            "Plaintiff v. Defendant"
        )

    def test_swc_prefix(self) -> None:
        """Strips SWC-style prefix."""
        assert strip_case_number_prefix("SWC2501234 A v. B") == "A v. B"

    def test_inc_prefix(self) -> None:
        """Strips INC-style prefix."""
        assert strip_case_number_prefix("INC2601234 C v. D") == "C v. D"

    def test_mixed_case_prefix(self) -> None:
        """Strips prefix in mixed case (e.g. Cvps)."""
        assert strip_case_number_prefix(
            "Cvps2405799 Aragon v. J And S Hospitality, Inc."
        ) == ("Aragon v. J And S Hospitality, Inc.")

    def test_lowercase_prefix(self) -> None:
        """Strips prefix in lowercase."""
        assert strip_case_number_prefix("cvri2306607 Cao v. Hu") == "Cao v. Hu"

    def test_mixed_case_cvri(self) -> None:
        """Strips Cvri prefix from issue example."""
        assert strip_case_number_prefix("Cvri2306607 Cao v. Hu") == "Cao v. Hu"

    def test_party_name_with_prefix(self) -> None:
        """Strips prefix from a party name."""
        assert strip_case_number_prefix("Cvps2406031 Garcia") == "Garcia"

    def test_party_name_single_word(self) -> None:
        """Strips prefix from single-word party name."""
        assert strip_case_number_prefix("Cvps2507128 Shannon") == "Shannon"

    def test_no_prefix_returns_none(self) -> None:
        """Returns None when there is no case number prefix."""
        assert strip_case_number_prefix("Smith v. Jones") is None

    def test_empty_string_returns_none(self) -> None:
        """Returns None for empty string."""
        assert strip_case_number_prefix("") is None

    def test_case_number_only_returns_none(self) -> None:
        """Returns None when text is just a case number (nothing after stripping)."""
        assert strip_case_number_prefix("CVPS2405799 ") is None

    def test_case_number_no_space_returns_none(self) -> None:
        """Returns None when case number has no trailing space."""
        assert strip_case_number_prefix("CVPS2405799") is None

    def test_preserves_whitespace_in_title(self) -> None:
        """Preserves internal whitespace in the cleaned title."""
        result = strip_case_number_prefix(
            "CVPS2405799   Aragon v. J And S Hospitality, Inc."
        )
        assert result == "Aragon v. J And S Hospitality, Inc."

    def test_all_caps_title(self) -> None:
        """Handles all-caps titles correctly."""
        result = strip_case_number_prefix(
            "CVRI2501693 JONES VS JONES B. Trigger of the ROFR"
        )
        assert result == "JONES VS JONES B. Trigger of the ROFR"

    def test_location_code_with_extra_letters(self) -> None:
        """Handles location codes with extra suffix letters (e.g. RICA)."""
        result = strip_case_number_prefix("RICA2401234 Test v. Case")
        assert result == "Test v. Case"

    def test_normal_name_not_matching(self) -> None:
        """Normal names that start with letters+digits are not matched."""
        # "John123 Smith" should not match — "John" is not a valid prefix
        assert strip_case_number_prefix("John Smith") is None

    def test_non_riverside_case_number(self) -> None:
        """LA-style case numbers (24STCV12345) should not match."""
        assert strip_case_number_prefix("24STCV12345 Smith v. Jones") is None

    def test_cv_two_letter_code(self) -> None:
        """CV with exactly 2-letter location code works."""
        assert strip_case_number_prefix("CVAB1234567 Test Title") == "Test Title"

    def test_cv_four_letter_code(self) -> None:
        """CV with 4-letter location code works."""
        assert strip_case_number_prefix("CVABCD1234567 Test Title") == "Test Title"


# ---------------------------------------------------------------------------
# is_riverside_case_number_prefix
# ---------------------------------------------------------------------------


class TestIsRiversideCaseNumberPrefix:
    """Tests for the is_riverside_case_number_prefix detection function."""

    def test_detects_cvps(self) -> None:
        assert is_riverside_case_number_prefix("CVPS2405799 Garcia") is True

    def test_detects_cvri(self) -> None:
        assert is_riverside_case_number_prefix("Cvri2306607 Cao v. Hu") is True

    def test_detects_ric(self) -> None:
        assert is_riverside_case_number_prefix("RIC1904113 Test") is True

    def test_rejects_normal_text(self) -> None:
        assert is_riverside_case_number_prefix("Smith v. Jones") is False

    def test_rejects_empty(self) -> None:
        assert is_riverside_case_number_prefix("") is False

    def test_rejects_case_number_without_space(self) -> None:
        assert is_riverside_case_number_prefix("CVPS2405799") is False


# ---------------------------------------------------------------------------
# cleanup_case_titles (mocked DB)
# ---------------------------------------------------------------------------


def _make_conn_with_rows(
    rows: list[tuple[str, str]],
) -> MagicMock:
    """Create a mock psycopg connection that returns *rows* on fetchall."""
    conn = MagicMock()
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = rows
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestCleanupCaseTitles:
    """Tests for cleanup_case_titles with mocked DB."""

    def test_dry_run_does_not_commit(self) -> None:
        """Dry-run mode does not commit changes."""
        conn = _make_conn_with_rows(
            [
                ("uuid-1", "CVPS2405799 Aragon v. Hospitality"),
            ]
        )
        stats = cleanup_case_titles(conn, dry_run=True)
        assert stats["found"] == 1
        assert stats["cleaned"] == 1
        assert stats["skipped"] == 0
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_apply_commits(self) -> None:
        """Apply mode commits changes."""
        conn = _make_conn_with_rows(
            [
                ("uuid-1", "CVPS2405799 Aragon v. Hospitality"),
            ]
        )
        stats = cleanup_case_titles(conn, dry_run=False)
        assert stats["cleaned"] == 1
        conn.commit.assert_called_once()

    def test_no_rows_found(self) -> None:
        """No rows found means no changes."""
        conn = _make_conn_with_rows([])
        stats = cleanup_case_titles(conn, dry_run=True)
        assert stats["found"] == 0
        assert stats["cleaned"] == 0

    def test_skips_sql_match_without_python_match(self) -> None:
        """Rows matching SQL regex but not Python regex are skipped."""
        # "12345 something" matches SQL '^[A-Za-z]*\\d{5,}' but not the
        # Python pattern which requires CV/RIC/MCC/etc. prefix.
        conn = _make_conn_with_rows(
            [
                ("uuid-1", "12345 Something"),
            ]
        )
        stats = cleanup_case_titles(conn, dry_run=True)
        assert stats["found"] == 1
        assert stats["cleaned"] == 0
        assert stats["skipped"] == 1


class TestCleanupPartyNames:
    """Tests for cleanup_party_names with mocked DB."""

    def test_dry_run_party_names(self) -> None:
        """Dry-run finds and reports party name prefixes."""
        conn = _make_conn_with_rows(
            [
                ("uuid-p1", "Cvps2406031 Garcia"),
                ("uuid-p2", "Cvps2507128 Shannon"),
            ]
        )
        stats = cleanup_party_names(conn, dry_run=True)
        assert stats["found"] == 2
        assert stats["cleaned"] == 2
        conn.commit.assert_not_called()

    def test_apply_party_names(self) -> None:
        """Apply mode updates party names."""
        conn = _make_conn_with_rows(
            [
                ("uuid-p1", "Cvps2406031 Garcia"),
            ]
        )
        stats = cleanup_party_names(conn, dry_run=False)
        assert stats["cleaned"] == 1
        conn.commit.assert_called_once()


class TestCleanupAliasNames:
    """Tests for cleanup_alias_names with mocked DB."""

    def test_dry_run_alias_names(self) -> None:
        """Dry-run finds and reports alias name prefixes."""
        conn = _make_conn_with_rows(
            [
                ("uuid-a1", "CVRI2306607 Cao"),
            ]
        )
        stats = cleanup_alias_names(conn, dry_run=True)
        assert stats["found"] == 1
        assert stats["cleaned"] == 1
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# run_cleanup end-to-end
# ---------------------------------------------------------------------------


@patch("cleanup_riverside_case_prefixes.psycopg")
def test_run_cleanup_end_to_end(mock_psycopg: MagicMock) -> None:
    """End-to-end run_cleanup processes all three entity types."""
    conn = MagicMock()
    mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

    cursor_mock = MagicMock()
    # Three fetchall calls: case_titles, party_names, alias_names
    cursor_mock.fetchall.side_effect = [
        [("uuid-c1", "CVPS2405799 Aragon v. Test")],
        [("uuid-p1", "Cvps2406031 Garcia")],
        [],  # no aliases
    ]
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    stats = run_cleanup("postgresql://test", dry_run=True)

    assert stats["case_titles"]["cleaned"] == 1
    assert stats["party_names"]["cleaned"] == 1
    assert stats["alias_names"]["cleaned"] == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_missing_database_url() -> None:
    """Main exits with error if DATABASE_URL is not set."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("DATABASE_URL", None)
        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["cleanup_riverside_case_prefixes.py"]):
                cleanup_riverside_case_prefixes.main()
        assert exc_info.value.code == 1


def test_main_default_is_dry_run() -> None:
    """Default mode (no --apply) is dry-run."""
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
        with patch("cleanup_riverside_case_prefixes.run_cleanup") as mock_run:
            mock_run.return_value = {
                "case_titles": {"found": 0, "cleaned": 0, "skipped": 0},
                "party_names": {"found": 0, "cleaned": 0, "skipped": 0},
                "alias_names": {"found": 0, "cleaned": 0, "skipped": 0},
            }
            with patch("sys.argv", ["cleanup_riverside_case_prefixes.py"]):
                cleanup_riverside_case_prefixes.main()
            mock_run.assert_called_once_with("postgresql://test", dry_run=True)


def test_main_apply_mode() -> None:
    """--apply flag sets dry_run=False."""
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
        with patch("cleanup_riverside_case_prefixes.run_cleanup") as mock_run:
            mock_run.return_value = {
                "case_titles": {"found": 0, "cleaned": 0, "skipped": 0},
                "party_names": {"found": 0, "cleaned": 0, "skipped": 0},
                "alias_names": {"found": 0, "cleaned": 0, "skipped": 0},
            }
            with patch("sys.argv", ["cleanup_riverside_case_prefixes.py", "--apply"]):
                cleanup_riverside_case_prefixes.main()
            mock_run.assert_called_once_with("postgresql://test", dry_run=False)
