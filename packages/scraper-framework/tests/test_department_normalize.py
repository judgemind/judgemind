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
# Orange County — leading-zero strip
# ---------------------------------------------------------------------------


class TestOrangeCounty:
    """Strip leading zeros from letter+number Orange County department codes.

    Canonical form is unpadded (W08 -> W8, H01 -> H1).  Consistent with the
    Riverside leading-zero-strip pattern for numeric-only departments.

    See: https://github.com/judgemind/judgemind/issues/3968
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("W08", "W8"),
            ("W8", "W8"),  # idempotent
            ("H01", "H1"),
            ("H001", "H1"),
            ("C03", "C3"),
            ("H012", "H12"),  # multi-digit suffix preserved
            ("L0612", "L612"),
            ("CX02", "CX2"),
            ("CM01", "CM1"),
        ],
    )
    def test_leading_zero_stripped(self, raw: str, expected: str) -> None:
        assert normalize_department("Orange", raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "L10",  # no leading zero — unchanged
            "L11",
            "W5",
            "L611",
            "H0",  # letter+zero only, no significant digit — unchanged
            "C44",
        ],
    )
    def test_no_leading_zero_unchanged(self, raw: str) -> None:
        assert normalize_department("Orange", raw) == raw

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("w08", "W8"),
            ("Cm01", "CM1"),
            ("cm01", "CM1"),
            ("h001", "H1"),
            ("cx02", "CX2"),
            ("l0612", "L612"),
        ],
    )
    def test_case_insensitive_leading_zero(self, raw: str, expected: str) -> None:
        assert normalize_department("Orange", raw) == expected


# ---------------------------------------------------------------------------
# Other counties — passthrough
# ---------------------------------------------------------------------------


class TestOtherCounties:
    """Other counties should pass through unchanged (except whitespace strip)."""

    @pytest.mark.parametrize(
        "county",
        [
            # Orange is no longer pure passthrough — it has a leading-zero
            # strip rule; see TestOrangeCounty below.
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
        # C25 has no leading zeros so it survives Orange's leading-zero strip.
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
            # CM01 and CX02 are now actively normalized to CM1/CX2 by the
            # Orange leading-zero strip rule — removed from passthrough list.
            "CM7",
            "W5",
            "L10",
        ],
    )
    def test_orange_alphanumeric_passthrough(self, raw: str) -> None:
        """Orange uses alphanumeric codes (CX*, CM*, N*, W*, L*, C*) — codes
        without leading zeros must pass through unchanged."""
        assert normalize_department("Orange", raw) == raw


# ---------------------------------------------------------------------------
# LA County — Long Beach / Chatsworth / AV courthouses (letter+digits must
# survive for regional courthouses, #3741 and #4014)
# ---------------------------------------------------------------------------


class TestLALetterDigitsKeep:
    """S25–S29, F43–F51, A14 etc. dept codes survive for regional courthouses.

    Long Beach (S-prefix), Chatsworth (F-prefix), and Antelope Valley
    (A-prefix) all use letter+digits codes that identify distinct courtrooms.
    The generic LA letter+digits collapse rule must be skipped for these
    courthouses.

    Carve-out mechanisms:
    1. Courthouse name/code in the ``courthouse`` kwarg.
    2. Case-number prefix (LBCV/LBCP/CHCV/CHCP/AVCV/AVCP) in the
       ``case_number`` kwarg (covers the rebuild path where courthouse may
       not be in the event).

    See: https://github.com/judgemind/judgemind/issues/3741
    See: https://github.com/judgemind/judgemind/issues/4014
    """

    # ------------------------------------------------------------------
    # Long Beach — courthouse name path
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("dept", ["S25", "S26", "S27", "S28", "S29"])
    @pytest.mark.parametrize(
        "courthouse",
        [
            "Long Beach Courthouse",
            "long beach courthouse",
            "LBC",
            "LBCV",
            "lbc",
            "lbcv",
        ],
    )
    def test_lb_dept_codes_survive(self, dept: str, courthouse: str) -> None:
        """S25–S29 must not be collapsed to 'S' for Long Beach Courthouse."""
        assert normalize_department("Los Angeles", dept, courthouse=courthouse) == dept

    # ------------------------------------------------------------------
    # Long Beach — case_number prefix path (AC1)
    # ------------------------------------------------------------------

    def test_lb_via_case_number(self) -> None:
        """S27 + LB case_number (25LBCV03352) must survive as 'S27' (AC1)."""
        assert normalize_department("Los Angeles", "S27", case_number="25LBCV03352") == "S27"

    @pytest.mark.parametrize(
        "case_number",
        ["25LBCV03352", "24LBCP01234", "23LBCV99999"],
    )
    def test_lb_case_number_variants(self, case_number: str) -> None:
        """Any LBCV/LBCP case_number should keep letter+digit dept intact."""
        assert normalize_department("Los Angeles", "S27", case_number=case_number) == "S27"

    # ------------------------------------------------------------------
    # Chatsworth — courthouse name path (AC2)
    # ------------------------------------------------------------------

    def test_chatsworth_via_courthouse_name(self) -> None:
        """F49 + Chatsworth courthouse must survive as 'F49' (AC2)."""
        assert (
            normalize_department("Los Angeles", "F49", courthouse="Chatsworth Courthouse North")
            == "F49"
        )

    @pytest.mark.parametrize(
        "courthouse",
        ["Chatsworth Courthouse North", "chatsworth courthouse north", "CHC", "chc"],
    )
    def test_chatsworth_courthouse_variants(self, courthouse: str) -> None:
        """All Chatsworth courthouse name/code variants keep F-prefix depts."""
        assert normalize_department("Los Angeles", "F49", courthouse=courthouse) == "F49"

    # ------------------------------------------------------------------
    # Chatsworth — case_number prefix path
    # ------------------------------------------------------------------

    def test_chatsworth_via_case_number(self) -> None:
        """F49 + CHCV case_number must survive as 'F49'."""
        assert normalize_department("Los Angeles", "F49", case_number="24CHCV03577") == "F49"

    @pytest.mark.parametrize(
        "case_number",
        ["24CHCV03577", "23CHCP00123", "25CHCV99999"],
    )
    def test_chatsworth_case_number_variants(self, case_number: str) -> None:
        """Any CHCV/CHCP case_number should keep letter+digit dept intact."""
        assert normalize_department("Los Angeles", "F49", case_number=case_number) == "F49"

    # ------------------------------------------------------------------
    # Antelope Valley — courthouse name path (AC3)
    # ------------------------------------------------------------------

    def test_av_via_courthouse_name(self) -> None:
        """A14 + Antelope Valley courthouse must survive as 'A14' (AC3)."""
        assert (
            normalize_department("Los Angeles", "A14", courthouse="Antelope Valley Courthouse")
            == "A14"
        )

    @pytest.mark.parametrize(
        "courthouse",
        ["Antelope Valley Courthouse", "antelope valley courthouse", "AV", "av"],
    )
    def test_av_courthouse_variants(self, courthouse: str) -> None:
        """All AV courthouse name/code variants keep A-prefix depts."""
        assert normalize_department("Los Angeles", "A14", courthouse=courthouse) == "A14"

    # ------------------------------------------------------------------
    # Antelope Valley — case_number prefix path
    # ------------------------------------------------------------------

    def test_av_via_case_number(self) -> None:
        """A14 + AVCV case_number must survive as 'A14'."""
        assert normalize_department("Los Angeles", "A14", case_number="23AVCV00370") == "A14"

    @pytest.mark.parametrize(
        "case_number",
        ["23AVCV00370", "24AVCP01234", "25AVCV99999"],
    )
    def test_av_case_number_variants(self, case_number: str) -> None:
        """Any AVCV/AVCP case_number should keep letter+digit dept intact."""
        assert normalize_department("Los Angeles", "A14", case_number=case_number) == "A14"

    # ------------------------------------------------------------------
    # Guard test — Stanley Mosk must still collapse (no carve-out)
    # ------------------------------------------------------------------

    def test_stanley_mosk_still_collapses(self) -> None:
        """X14 + Stanley Mosk case_number (25STCV) must still collapse to 'X'."""
        assert normalize_department("Los Angeles", "X14", case_number="25STCV12345") == "X"

    def test_pomona_l10_still_collapses(self) -> None:
        """Non-LB courthouse: L10 must still collapse to 'L'."""
        assert normalize_department("Los Angeles", "L10", courthouse="Pomona Courthouse") == "L"

    def test_courthouse_omitted_back_compat(self) -> None:
        """Omitting courthouse kwarg preserves existing collapse behaviour."""
        assert normalize_department("Los Angeles", "X14") == "X"

    def test_case_number_none_back_compat(self) -> None:
        """Passing case_number=None preserves existing collapse behaviour."""
        assert normalize_department("Los Angeles", "X14", case_number=None) == "X"

    def test_three_arg_call_back_compat(self) -> None:
        """3-arg call (no case_number kwarg) still collapses as before."""
        assert normalize_department("Los Angeles", "X14") == "X"
        assert normalize_department("Los Angeles", "S27") == "S"
