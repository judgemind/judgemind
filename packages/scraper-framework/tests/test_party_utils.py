"""Tests for framework.party_utils — shared party name splitting utilities."""

from __future__ import annotations

from framework.party_utils import (
    CORP_SUFFIX_RE,
    is_contaminated_party_name,
    is_name_fragment,
    split_party_names,
)

# ---------------------------------------------------------------------------
# is_contaminated_party_name
# ---------------------------------------------------------------------------


class TestIsContaminatedPartyName:
    """Tests for detection of court headers, ruling text, and motion
    descriptions incorrectly stored as party names.  See issue #1932.
    """

    # -- Court header patterns (LA-style) --

    def test_la_court_header_full(self) -> None:
        name = (
            "Department O Law And Motion Rulings Case Number: 24Vecv03334 "
            "Hearing Date: March 5, 2026 Dept: O Superior Court Of The "
            "State Of California County Of Los Angeles - Northwest District "
            "Eric Atayde"
        )
        assert is_contaminated_party_name(name) is True

    def test_la_court_header_dept_50(self) -> None:
        name = (
            "Department 50 Law And Motion Rulings Case Number: 20Stcv41848 "
            "Hearing Date: March 5, 2026 Dept: 50 Superior Court Of "
            "California County Of Los Angeles Department 50 La Live Theatre, Llc"
        )
        assert is_contaminated_party_name(name) is True

    def test_law_and_motion_rulings(self) -> None:
        assert is_contaminated_party_name("Law And Motion Rulings") is True

    def test_superior_court_of(self) -> None:
        assert is_contaminated_party_name("Superior Court Of California") is True

    def test_hearing_date_colon(self) -> None:
        assert is_contaminated_party_name("Hearing Date: March 5, 2026") is True

    def test_hearing_time_colon(self) -> None:
        assert is_contaminated_party_name("Hearing Time: 10:00 A.M.") is True

    def test_case_number_colon(self) -> None:
        assert is_contaminated_party_name("Case Number: 24Vecv03334") is True

    def test_case_no_colon(self) -> None:
        assert is_contaminated_party_name("Case No.: 25Stcv08708") is True

    # -- Ruling text patterns --

    def test_ruling_text_with_case_no(self) -> None:
        name = (
            "Case No.: 25Stcv08708 Hearing Date: March 6, 2026 "
            "Hearing Time: 10:00 A.M. [Tentative] Ruling Re: "
            "Plaintiff's Motion For Default Judgment"
        )
        assert is_contaminated_party_name(name) is True

    def test_before_the_court(self) -> None:
        assert (
            is_contaminated_party_name("Before The Court Are The Following Two Petitions Regarding")
            is True
        )

    def test_before_the_court_oc(self) -> None:
        assert (
            is_contaminated_party_name(
                "J Star Before The Court Is The Continued Hearing On The Motion Of"
            )
            is True
        )

    def test_tentative_ruling(self) -> None:
        assert is_contaminated_party_name("[Tentative] Ruling Re: Something") is True

    def test_decl_ex(self) -> None:
        assert (
            is_contaminated_party_name(
                "Llc Barnes Decl. Ex. B (At 1) (Italics Added). The Same Section Further States"
            )
            is True
        )

    def test_the_court_finds(self) -> None:
        assert is_contaminated_party_name("The Court Finds that") is True

    def test_the_court_orders(self) -> None:
        assert is_contaminated_party_name("The Court Orders the parties to") is True

    def test_italics_added(self) -> None:
        assert is_contaminated_party_name("(Italics Added)") is True

    def test_emphasis_added(self) -> None:
        assert is_contaminated_party_name("(Emphasis Added)") is True

    def test_ruling_re(self) -> None:
        assert is_contaminated_party_name("Ruling Re: Demurrer") is True

    # -- Motion-only patterns --

    def test_motion_for_attorney(self) -> None:
        assert is_contaminated_party_name("Motion For Attorney") is True

    def test_motion_for_summary(self) -> None:
        assert is_contaminated_party_name("Ford Motor Motion For Summary") is True

    def test_motion_to_be_relieved(self) -> None:
        assert (
            is_contaminated_party_name("Inc. 30-2024-01404258 Motion to Be Relieved As Counsel")
            is True
        )

    def test_motion_of(self) -> None:
        assert is_contaminated_party_name("The Continued Hearing On The Motion Of") is True

    def test_granting_motion_for(self) -> None:
        """Regression: 'Granting Motion For' survived cleanup — issue #1950."""
        assert is_contaminated_party_name("Granting Motion For") is True

    # -- Docket metadata patterns --

    def test_docket_filing_date(self) -> None:
        """'Et Al. Comp. Filed : 03-27-25' is docket metadata, not a party."""
        assert is_contaminated_party_name("Et Al. Comp. Filed : 03-27-25") is True

    def test_comp_filed(self) -> None:
        assert is_contaminated_party_name("Comp. Filed something") is True

    def test_filed_date_no_space(self) -> None:
        assert is_contaminated_party_name("Filed: 01-15-26") is True

    def test_filed_word_not_contaminated(self) -> None:
        """The word 'filed' alone in a name should not trigger false positive."""
        assert is_contaminated_party_name("Filed Insurance Group") is False

    # -- Length check --

    def test_very_long_name_is_contaminated(self) -> None:
        long_name = "A" * 151
        assert is_contaminated_party_name(long_name) is True

    def test_name_at_boundary_not_contaminated(self) -> None:
        boundary_name = "A" * 150
        assert is_contaminated_party_name(boundary_name) is False

    # -- Valid party names that should NOT be flagged --

    def test_normal_person_name(self) -> None:
        assert is_contaminated_party_name("John Doe") is False

    def test_corporate_name(self) -> None:
        assert is_contaminated_party_name("Techno-Advanced, Inc.") is False

    def test_ford_motor_company(self) -> None:
        """Ford Motor Company is a valid party — should not be flagged."""
        assert is_contaminated_party_name("Ford Motor Company") is False

    def test_la_live_theatre_llc(self) -> None:
        assert is_contaminated_party_name("La Live Theatre, Llc") is False

    def test_google(self) -> None:
        assert is_contaminated_party_name("Google") is False

    def test_eric_atayde(self) -> None:
        assert is_contaminated_party_name("Eric Atayde") is False

    def test_empty_string(self) -> None:
        assert is_contaminated_party_name("") is False

    def test_whitespace_only(self) -> None:
        assert is_contaminated_party_name("   ") is False

    def test_party_with_vs(self) -> None:
        assert is_contaminated_party_name("Smith v. Jones") is False

    def test_state_of_california_as_party(self) -> None:
        """The State of California can be a party — not a court header."""
        assert is_contaminated_party_name("State of California") is False

    def test_county_of_los_angeles_as_party(self) -> None:
        """County of Los Angeles can be a party name."""
        assert is_contaminated_party_name("County of Los Angeles") is False


# ---------------------------------------------------------------------------
# is_name_fragment — now includes contamination checks
# ---------------------------------------------------------------------------


class TestIsNameFragment:
    def test_corporate_suffixes(self) -> None:
        assert is_name_fragment("Inc") is True
        assert is_name_fragment("Inc.") is True
        assert is_name_fragment("LLC") is True
        assert is_name_fragment("Corp") is True
        assert is_name_fragment("Ltd") is True
        assert is_name_fragment("LP") is True
        assert is_name_fragment("LLP") is True
        assert is_name_fragment("PLLC") is True
        assert is_name_fragment("PLC") is True

    def test_short_words(self) -> None:
        assert is_name_fragment("AB") is True
        assert is_name_fragment("") is True
        assert is_name_fragment("   ") is True

    def test_valid_names(self) -> None:
        assert is_name_fragment("John Doe") is False
        assert is_name_fragment("Techno-Advanced, Inc.") is False
        assert is_name_fragment("Google") is False
        assert is_name_fragment("Jane Smith Corporation") is False

    def test_contaminated_names_rejected(self) -> None:
        """is_name_fragment should reject contaminated names via delegation."""
        assert is_name_fragment("Law And Motion Rulings") is True
        assert is_name_fragment("Before The Court") is True
        assert is_name_fragment("Motion For Attorney") is True
        assert is_name_fragment("Hearing Date: March 5, 2026") is True

    def test_contaminated_name_in_split(self) -> None:
        """split_party_names should filter out contaminated names."""
        result = split_party_names("Law And Motion Rulings")
        assert result == []


# ---------------------------------------------------------------------------
# split_party_names
# ---------------------------------------------------------------------------


class TestSplitPartyNames:
    def test_keeps_corporate_suffix(self) -> None:
        result = split_party_names("Techno-Advanced, Inc.")
        assert result == ["Techno-Advanced, Inc."]

    def test_keeps_llc_suffix(self) -> None:
        result = split_party_names("Big Corp, LLC")
        assert result == ["Big Corp, LLC"]

    def test_multiple_with_corporate(self) -> None:
        result = split_party_names("John Doe, Techno-Advanced, Inc., and Jane Smith")
        assert len(result) == 3
        assert "John Doe" in result
        assert "Jane Smith" in result
        assert any("Techno" in r and "Inc" in r for r in result)

    def test_two_corporates(self) -> None:
        result = split_party_names("Alpha, LLC, and Beta, Inc.")
        assert len(result) == 2
        assert any("Alpha" in r and "LLC" in r for r in result)
        assert any("Beta" in r and "Inc" in r for r in result)

    def test_filters_fragment(self) -> None:
        result = split_party_names("Inc")
        assert result == []

    def test_filters_short_fragment(self) -> None:
        result = split_party_names("AB")
        assert result == []

    def test_keeps_long_single_word(self) -> None:
        result = split_party_names("Google")
        assert result == ["Google"]

    def test_oxford_comma_split(self) -> None:
        result = split_party_names("Alice Smith, Robert Jones, and Charlie Brown")
        assert result == ["Alice Smith", "Robert Jones", "Charlie Brown"]

    def test_and_split_without_commas(self) -> None:
        result = split_party_names("Alpha Corp and Beta Corp")
        assert result == ["Alpha Corp", "Beta Corp"]

    def test_filters_contaminated_in_list(self) -> None:
        """Contaminated entries within a comma-separated list are filtered."""
        result = split_party_names("John Doe, Before The Court Are The Following, and Jane Smith")
        assert "John Doe" in result
        assert "Jane Smith" in result
        assert len(result) == 2


# ---------------------------------------------------------------------------
# CORP_SUFFIX_RE
# ---------------------------------------------------------------------------


class TestCorpSuffixRe:
    def test_matches_inc(self) -> None:
        assert CORP_SUFFIX_RE.search(", Inc.")

    def test_matches_llc(self) -> None:
        assert CORP_SUFFIX_RE.search(", LLC")

    def test_no_match_on_plain_text(self) -> None:
        assert CORP_SUFFIX_RE.search("Hello World") is None
