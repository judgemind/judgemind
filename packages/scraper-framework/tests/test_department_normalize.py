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


# ---------------------------------------------------------------------------
# Alphanumeric department codes — locked in for #2602
# ---------------------------------------------------------------------------


class TestAlphanumericDepartmentCodes:
    """Lock in that alphanumeric dept codes (J6, S1, M4, C3, N1, W5, CM01, L10,
    PS2, MV1, CX02, etc.) are preserved for non-LA counties.

    Regression guard for #2602: Ventura J6 rulings had NULL judge because the
    caller suspected ``normalize_department`` was stripping the ``J`` or the
    ``6``.  In fact the pass-through already preserves these codes for non-LA
    counties (LA has its own letter+digits -> letter rule, intentional for
    LA's courtroom-number convention).  These tests document and lock in the
    correct pass-through behavior so that any future normalization changes
    do not silently break dept-to-judge lookups.
    """

    def test_issue_2602_j6(self) -> None:
        """The exact case from #2602: Ventura J6 must pass through unchanged."""
        assert normalize_department("Ventura", "J6") == "J6"

    @pytest.mark.parametrize(
        "raw",
        [
            "J6",
            "J1",
            "20",
            "42",
            "43",
            "44",
        ],
    )
    def test_ventura_passthrough(self, raw: str) -> None:
        """Ventura has no county-specific rules — all codes pass through."""
        assert normalize_department("Ventura", raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "M4",
            "M205",
            "M301",
            "M302",
            "MV1",
            "C1",
            "C3",
            "PS1",
            "PS2",
            "PS4",
            "T1",
        ],
    )
    def test_riverside_alphanumeric_passthrough(self, raw: str) -> None:
        """Riverside alphanumeric codes must not be altered (only numeric-only
        codes have leading zeros stripped)."""
        assert normalize_department("Riverside", raw) == raw

    def test_riverside_numeric_zero_strip_still_works(self) -> None:
        """AC #4: numeric zero-stripping still works for numeric-only codes."""
        assert normalize_department("Riverside", "07") == "7"
        assert normalize_department("Riverside", "007") == "7"
        assert normalize_department("Riverside", "01") == "1"
        # Edge case: bare zero stays zero, not empty
        assert normalize_department("Riverside", "0") == "0"
        # Edge case: double-zero normalizes to "0"
        assert normalize_department("Riverside", "00") == "0"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("S17", "S17"),
            ("S-17", "S17"),
            ("R14", "R14"),
            ("R-14", "R14"),
            ("S1", "S1"),
        ],
    )
    def test_san_bernardino_alpha_prefix_preserved(self, raw: str, expected: str) -> None:
        """SB strips the hyphen but must preserve the alpha prefix (S*, R*)."""
        assert normalize_department("San Bernardino", raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "N1",
            "N15",
            "C44",
            "CM01",
            "CM7",
            "CX02",
            "W5",
            "L10",
        ],
    )
    def test_orange_alphanumeric_passthrough(self, raw: str) -> None:
        """Orange uses alphanumeric codes (CX*, CM*, N*, W*, L*, C*) — all
        must pass through."""
        assert normalize_department("Orange", raw) == raw
