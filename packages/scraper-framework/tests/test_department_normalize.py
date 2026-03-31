"""Tests for department name normalization across all counties.

See: https://github.com/judgemind/judgemind/issues/2141
"""

from __future__ import annotations

import pytest

from ingestion.department_normalize import normalize_department

# ---------------------------------------------------------------------------
# None / empty handling
# ---------------------------------------------------------------------------


class TestNoneAndEmpty:
    def test_none_returns_none(self) -> None:
        assert normalize_department("Los Angeles", None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_department("Los Angeles", "") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_department("Los Angeles", "   ") is None


# ---------------------------------------------------------------------------
# LA County — courtroom suffix stripping
# ---------------------------------------------------------------------------


class TestLACountyCourtroom:
    """Strip ' #N' courtroom/calendar suffixes from LA department letters."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("X #14", "X"),
            ("X #5", "X"),
            ("X #13", "X"),
            ("X #17", "X"),
            ("X #22", "X"),
            ("X #23", "X"),
            ("R #10", "R"),
            ("R #17", "R"),
            ("R #21", "R"),
            ("R #15", "R"),
            ("R #16", "R"),
            ("R #14", "R"),
            ("L #9", "L"),
        ],
    )
    def test_strip_courtroom_suffix(self, raw: str, expected: str) -> None:
        assert normalize_department("Los Angeles", raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("X14", "X"),
            ("L10", "L"),
            ("L13", "L"),
            ("L14", "L"),
            ("L15", "L"),
            ("L17", "L"),
            ("L18", "L"),
        ],
    )
    def test_strip_glued_digits(self, raw: str, expected: str) -> None:
        assert normalize_department("Los Angeles", raw) == expected

    def test_bare_letter_unchanged(self) -> None:
        assert normalize_department("Los Angeles", "X") == "X"
        assert normalize_department("Los Angeles", "R") == "R"
        assert normalize_department("Los Angeles", "L") == "L"


# ---------------------------------------------------------------------------
# LA County — courthouse prefix stripping
# ---------------------------------------------------------------------------


class TestLACountyCourthouse:
    """Strip courthouse prefixes like SSC- and map aliases like SEP."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SSC-9", "9"),
            ("SSC-1", "1"),
            ("ssc-9", "9"),  # case insensitive
        ],
    )
    def test_strip_ssc_prefix(self, raw: str, expected: str) -> None:
        assert normalize_department("Los Angeles", raw) == expected

    def test_sep_maps_to_p(self) -> None:
        assert normalize_department("Los Angeles", "SEP") == "P"

    def test_bare_number_unchanged(self) -> None:
        assert normalize_department("Los Angeles", "9") == "9"
        assert normalize_department("Los Angeles", "1") == "1"

    def test_bare_p_unchanged(self) -> None:
        assert normalize_department("Los Angeles", "P") == "P"


# ---------------------------------------------------------------------------
# LA County — multi-char departments (should NOT be split)
# ---------------------------------------------------------------------------


class TestLAMultiChar:
    """Multi-character numeric or code departments should pass through."""

    def test_numeric_department_unchanged(self) -> None:
        assert normalize_department("Los Angeles", "205") == "205"
        assert normalize_department("Los Angeles", "15") == "15"

    def test_multi_letter_code_unchanged(self) -> None:
        # Multi-letter codes like "SP" are not single-letter+digits
        assert normalize_department("Los Angeles", "SP") == "SP"


# ---------------------------------------------------------------------------
# Riverside — leading zeros
# ---------------------------------------------------------------------------


class TestRiverside:
    """Strip leading zeros from purely numeric Riverside departments."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("07", "7"),
            ("06", "6"),
            ("01", "1"),
            ("007", "7"),
        ],
    )
    def test_strip_leading_zeros(self, raw: str, expected: str) -> None:
        assert normalize_department("Riverside", raw) == expected

    def test_no_leading_zero_unchanged(self) -> None:
        assert normalize_department("Riverside", "7") == "7"
        assert normalize_department("Riverside", "6") == "6"
        assert normalize_department("Riverside", "1") == "1"

    def test_alpha_prefix_unchanged(self) -> None:
        """Departments like PS1, T1 should not be altered."""
        assert normalize_department("Riverside", "PS1") == "PS1"
        assert normalize_department("Riverside", "T1") == "T1"

    def test_zero_department(self) -> None:
        """Edge case: department "0" should remain "0", not empty."""
        assert normalize_department("Riverside", "0") == "0"

    def test_double_zero_department(self) -> None:
        """Edge case: department "00" should normalize to "0"."""
        assert normalize_department("Riverside", "00") == "0"


# ---------------------------------------------------------------------------
# San Bernardino — hyphen removal
# ---------------------------------------------------------------------------


class TestSanBernardino:
    """Strip hyphens from SB department codes."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("S-17", "S17"),
            ("R-14", "R14"),
            ("S-36", "S36"),
        ],
    )
    def test_strip_hyphen(self, raw: str, expected: str) -> None:
        assert normalize_department("San Bernardino", raw) == expected

    def test_no_hyphen_unchanged(self) -> None:
        assert normalize_department("San Bernardino", "S17") == "S17"
        assert normalize_department("San Bernardino", "R14") == "R14"

    def test_whitespace_stripped(self) -> None:
        assert normalize_department("San Bernardino", "  S-17  ") == "S17"


# ---------------------------------------------------------------------------
# Other counties — passthrough
# ---------------------------------------------------------------------------


class TestOtherCounties:
    """Other counties should pass through unchanged (except whitespace strip)."""

    @pytest.mark.parametrize(
        "county",
        [
            "Orange",
            "San Diego",
            "Fresno",
            "Ventura",
            "Contra Costa",
            "Santa Clara",
            "San Francisco",
            "Kern",
        ],
    )
    def test_passthrough(self, county: str) -> None:
        assert normalize_department(county, "C25") == "C25"
        assert normalize_department(county, "14") == "14"

    def test_whitespace_stripped(self) -> None:
        assert normalize_department("Orange", "  C25  ") == "C25"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_case_sensitivity_county(self) -> None:
        """County matching should be case-insensitive."""
        assert normalize_department("los angeles", "X #14") == "X"
        assert normalize_department("LOS ANGELES", "X #14") == "X"

    def test_la_combined_suffix_and_prefix(self) -> None:
        """Test combined patterns with SSC- prefix and courtroom suffix."""
        # "SSC-9 #3": strip SSC- -> "9 #3", strip #N suffix -> "9"
        assert normalize_department("Los Angeles", "SSC-9 #3") == "9"

    def test_la_ssc_with_letter_digits(self) -> None:
        """Test SSC- prefix combined with letter+digits pattern."""
        # "SSC-X14": strip SSC- -> "X14", letter+digits -> "X"
        assert normalize_department("Los Angeles", "SSC-X14") == "X"
