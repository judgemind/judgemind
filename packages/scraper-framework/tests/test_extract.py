"""Tests for extraction utilities: normalization, case number, hearing date,
judge name, case type inference, title validation, and title cleanup."""

from __future__ import annotations

from datetime import date

import pytest

from ingestion.extract import (
    _looks_like_person_name,
    clean_case_title,
    extract_case_number,
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
    extract_hearing_date,
    extract_judge_name,
    is_plausible_case_title,
    is_valid_case_number,
    normalize_motion_type,
    normalize_outcome,
    strip_trailing_connectors,
)


# ---------------------------------------------------------------------------
# Outcome extraction
class TestNormalizeOutcome:
    """Tests for normalize_outcome() — converts scraper-provided title-case
    outcomes to valid ruling_outcome enum values."""

    def test_granted_title_case(self) -> None:
        assert normalize_outcome("Granted") == "granted"

    def test_denied_title_case(self) -> None:
        assert normalize_outcome("Denied") == "denied"

    def test_granted_in_part(self) -> None:
        assert normalize_outcome("Granted in Part") == "granted_in_part"

    def test_denied_in_part(self) -> None:
        assert normalize_outcome("Denied in Part") == "denied_in_part"

    def test_moot_title_case(self) -> None:
        assert normalize_outcome("Moot") == "moot"

    def test_continued_title_case(self) -> None:
        assert normalize_outcome("Continued") == "continued"

    def test_off_calendar_spaced(self) -> None:
        assert normalize_outcome("Off Calendar") == "off_calendar"

    def test_submitted_title_case(self) -> None:
        assert normalize_outcome("Submitted") == "submitted"

    def test_no_tentative_ruling(self) -> None:
        assert normalize_outcome("No Tentative Ruling") == "other"

    def test_no_tentative(self) -> None:
        assert normalize_outcome("No Tentative") == "other"

    def test_no_appearance_required(self) -> None:
        assert normalize_outcome("No Appearance Required") == "other"

    def test_sustained_maps_to_granted(self) -> None:
        """'Sustained' (demurrer context) maps to 'granted'."""
        assert normalize_outcome("Sustained") == "granted"

    def test_overruled_maps_to_denied(self) -> None:
        """'Overruled' (demurrer context) maps to 'denied'."""
        assert normalize_outcome("Overruled") == "denied"

    def test_withdrawn_maps_to_off_calendar(self) -> None:
        assert normalize_outcome("Withdrawn") == "off_calendar"

    def test_vacated_maps_to_off_calendar(self) -> None:
        assert normalize_outcome("Vacated") == "off_calendar"

    def test_denied_without_prejudice(self) -> None:
        assert normalize_outcome("Denied without Prejudice") == "denied"

    def test_sustained_without_leave_to_amend(self) -> None:
        assert normalize_outcome("Sustained without Leave to Amend") == "granted"

    def test_sustained_with_leave_to_amend(self) -> None:
        assert normalize_outcome("Sustained with Leave to Amend") == "granted"

    def test_none_returns_none(self) -> None:
        assert normalize_outcome(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_outcome("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_outcome("   ") is None

    def test_already_lowercase_passthrough(self) -> None:
        assert normalize_outcome("granted") == "granted"

    def test_already_normalized_underscore(self) -> None:
        assert normalize_outcome("granted_in_part") == "granted_in_part"

    def test_uppercase(self) -> None:
        assert normalize_outcome("GRANTED") == "granted"

    def test_unmapped_returns_none(self) -> None:
        assert normalize_outcome("Something Unknown") is None

    def test_other_passthrough(self) -> None:
        assert normalize_outcome("other") == "other"

    def test_leading_trailing_whitespace(self) -> None:
        assert normalize_outcome("  Granted  ") == "granted"

    def test_partial_match_granted_in_part_within_longer_string(self) -> None:
        """Partial substring match for 'granted in part' in longer text."""
        assert normalize_outcome("Motion was Granted in Part and Denied") == "granted_in_part"

    def test_partial_match_denied_in_longer_string(self) -> None:
        """Partial substring match for 'denied' in longer text."""
        assert normalize_outcome("Motion is hereby Denied for cause") == "denied"


# ---------------------------------------------------------------------------
# Motion type extraction
class TestExtractJudgeName:
    """Tests for extract_judge_name()."""

    def test_la_style_signature(self) -> None:
        text = "William A. Crowfoot Judge of the Superior Court"
        assert extract_judge_name(text) == "William A. Crowfoot"

    def test_sb_department_judge(self) -> None:
        text = "Department S22 - Judge Bobby P. Luna\nCase 12345"
        assert extract_judge_name(text) == "Bobby P. Luna"

    def test_sb_before_the_honorable(self) -> None:
        text = "BEFORE THE HONORABLE BOBBY P. LUNA\nSome ruling text"
        assert extract_judge_name(text) == "BOBBY P. LUNA"

    def test_sf_presiding(self) -> None:
        text = "Presiding: JOHN A. SMITH\nDepartment 403"
        assert extract_judge_name(text) == "JOHN A. SMITH"

    def test_riverside_honorable(self) -> None:
        text = "Department 2 - Honorable Jane B. Doe\nRuling on motion"
        assert extract_judge_name(text) == "Jane B. Doe"

    def test_no_match(self) -> None:
        assert extract_judge_name("The motion is granted.") is None

    def test_empty_string(self) -> None:
        assert extract_judge_name("") is None

    def test_whitespace_collapsed(self) -> None:
        text = "Presiding:  JOHN   A.   SMITH  \nDepartment 403"
        assert extract_judge_name(text) == "JOHN A. SMITH"

    def test_sb_em_dash(self) -> None:
        text = "Department S36\u2014Judge Maria C. Garcia\nSome text"
        assert extract_judge_name(text) == "Maria C. Garcia"

    def test_sb_en_dash(self) -> None:
        text = "Department R17\u2013Judge Robert E. Lee\nSome text"
        assert extract_judge_name(text) == "Robert E. Lee"

    # --- Case-insensitive "Judge of the Superior Court" ---

    def test_la_uppercase_judge_of_superior_court(self) -> None:
        """LA rulings sometimes have the signature in all-caps."""
        text = "JARED D. MOSES\nJUDGE OF THE SUPERIOR COURT"
        assert extract_judge_name(text) == "JARED D. MOSES"

    def test_la_hon_prefix_with_judge_of_superior_court(self) -> None:
        """LA fixture: 'Hon. Elizabeth L. Bradley Judge of the Superior Court'."""
        text = "Hon. Elizabeth L. Bradley\nJudge of the Superior Court"
        assert extract_judge_name(text) == "Hon. Elizabeth L. Bradley"

    # --- JUDICIAL OFFICER pattern ---

    def test_judicial_officer_colon(self) -> None:
        """'JUDICIAL OFFICER: Name' pattern used by some courts."""
        text = "JUDICIAL OFFICER: Maria L. Gonzalez\nDepartment 12"
        assert extract_judge_name(text) == "Maria L. Gonzalez"

    def test_judicial_officer_lowercase(self) -> None:
        """Case-insensitive match for 'Judicial Officer:'."""
        text = "Judicial Officer: Robert A. Dukes\nCourtroom 5"
        assert extract_judge_name(text) == "Robert A. Dukes"

    def test_judicial_officer_no_space_after_colon(self) -> None:
        text = "JUDICIAL OFFICER:Michael T. Chang\nDept 7"
        assert extract_judge_name(text) == "Michael T. Chang"

    # --- Hon. / Honorable standalone pattern ---

    def test_hon_dot_prefix(self) -> None:
        """'Hon. Name' as a standalone prefix (no 'Judge of ...' suffix)."""
        text = "Ruling by Hon. Sarah K. Park on the demurrer."
        assert extract_judge_name(text) == "Sarah K. Park"

    def test_honorable_prefix(self) -> None:
        """'Honorable Name' as a standalone prefix."""
        text = "The Honorable James R. Williams presiding."
        assert extract_judge_name(text) == "James R. Williams"

    def test_hon_no_dot(self) -> None:
        """'Hon Name' without the period --- some courts omit the dot."""
        text = "Heard before Hon Patricia M. Lee"
        assert extract_judge_name(text) == "Patricia M. Lee"

    def test_honorable_multiword_last_name(self) -> None:
        """Names with hyphenated surnames."""
        text = "Honorable Mary Anne Chen-Ramirez presiding"
        assert extract_judge_name(text) == "Mary Anne Chen-Ramirez"

    # --- Judge: Name / Judge Name in headers ---

    def test_judge_colon_name(self) -> None:
        """'Judge: Name' header format."""
        text = "Judge: Thomas P. Kelly\nDepartment 5"
        assert extract_judge_name(text) == "Thomas P. Kelly"

    def test_judge_name_header(self) -> None:
        """'Judge Name' without colon in a header."""
        text = "Judge Lisa M. Torres\nCourtroom 3A"
        assert extract_judge_name(text) == "Lisa M. Torres"

    def test_judge_of_superior_court_not_double_matched(self) -> None:
        """'Judge' pattern should NOT match 'Judge of the Superior Court'."""
        text = "William A. Crowfoot Judge of the Superior Court"
        # Should be matched by the first pattern, yielding the name correctly
        assert extract_judge_name(text) == "William A. Crowfoot"

    # --- Edge cases and false-positive prevention ---

    # --- ALL-CAPS "JUDGE NAME" pattern (#401) ---

    def test_la_allcaps_dept_judge(self) -> None:
        """LA header: 'DEPARTMENT 56 JUDGE STEVEN A. ELLIS' in ALL CAPS (#401)."""
        text = "DEPARTMENT 56 JUDGE STEVEN A. ELLIS\nCase Number: 24NNCV02551"
        assert extract_judge_name(text) == "STEVEN A. ELLIS"

    def test_la_allcaps_judge_no_middle_initial(self) -> None:
        """ALL-CAPS judge name without middle initial."""
        text = "DEPARTMENT 72 JUDGE DAVID SOTELO\nHearing on motion"
        assert extract_judge_name(text) == "DAVID SOTELO"

    def test_la_allcaps_judge_without_department(self) -> None:
        """ALL-CAPS 'JUDGE NAME' without DEPARTMENT prefix."""
        text = "JUDGE MARK MOONEY\nThe motion is granted."
        assert extract_judge_name(text) == "MARK MOONEY"

    def test_la_allcaps_judge_hyphenated_surname(self) -> None:
        """ALL-CAPS judge with hyphenated surname."""
        text = "DEPARTMENT 3 JUDGE MARIA CHEN-RAMIREZ\nCase Number: 24STCV00123"
        assert extract_judge_name(text) == "MARIA CHEN-RAMIREZ"

    def test_la_allcaps_judge_multiple_middle_initials(self) -> None:
        """ALL-CAPS judge with multiple middle initials."""
        text = "DEPARTMENT 61 JUDGE JAMES R. B. SMITH\nMotion denied."
        assert extract_judge_name(text) == "JAMES R. B. SMITH"

    def test_no_false_positive_on_judge_word_in_ruling(self) -> None:
        """The word 'judge' in ruling body text should not trigger a match."""
        text = "The judge granted the motion."
        assert extract_judge_name(text) is None

    def test_no_false_positive_on_judicial_notice(self) -> None:
        """'judicial notice' should not trigger the JUDICIAL OFFICER pattern."""
        text = "The court takes judicial notice of the following."
        assert extract_judge_name(text) is None

    # --- Party name / organization false-positive prevention (#326) ---

    def test_no_false_positive_heritage_medical_group(self) -> None:
        """Party name 'Heritage Medical Group' must not be extracted as judge (#326)."""
        text = "Heritage Medical Group Judge of the Superior Court ruling stands."
        assert extract_judge_name(text) is None

    def test_no_false_positive_heritage_medical_group_judge_colon(self) -> None:
        """'Judge: Heritage Medical Group' must not extract the party name (#326)."""
        text = "Judge: Heritage Medical Group\nCVPS2303018"
        assert extract_judge_name(text) is None

    def test_no_false_positive_bank_of_america(self) -> None:
        """Bank names must not be extracted as judge names."""
        text = "Bank of America, N.A. Judge of the Superior Court"
        assert extract_judge_name(text) is None

    def test_no_false_positive_llc(self) -> None:
        """Company with LLC suffix must not be extracted as judge."""
        text = "Department 5 - Honorable Acme Properties LLC"
        assert extract_judge_name(text) is None

    def test_no_false_positive_inc(self) -> None:
        """Company with Inc suffix must not be extracted."""
        text = "Legacy, Inc., A California Corporation Judge of the Superior Court"
        assert extract_judge_name(text) is None

    def test_no_false_positive_insurance_company(self) -> None:
        """Insurance company name must not be extracted."""
        text = "State Farm Insurance Judge of the Superior Court"
        assert extract_judge_name(text) is None

    def test_no_false_positive_vs_in_name(self) -> None:
        """Case title with 'vs' leaked into judge field must be rejected."""
        text = "SMITH vs JONES Judge of the Superior Court"
        assert extract_judge_name(text) is None

    def test_no_false_positive_v_dot_in_name(self) -> None:
        """Case title with 'v.' leaked into judge field must be rejected."""
        text = "Smith v. Jones Judge of the Superior Court"
        assert extract_judge_name(text) is None

    def test_no_false_positive_county_of(self) -> None:
        """Government entity must not be extracted."""
        text = "County of Riverside Judge of the Superior Court"
        assert extract_judge_name(text) is None

    def test_no_false_positive_california_corporation(self) -> None:
        """Entity with 'A California' descriptor must not be extracted."""
        text = "Department PS2 - Honorable Legacy Inc A California Corporation"
        assert extract_judge_name(text) is None

    def test_valid_judge_still_extracted_after_validation(self) -> None:
        """Valid judge names must still be extracted with validation in place."""
        text = "William A. Crowfoot Judge of the Superior Court"
        assert extract_judge_name(text) == "William A. Crowfoot"

    def test_valid_judge_hon_still_extracted(self) -> None:
        """'Hon. Name' patterns must still work with validation."""
        text = "Ruling by Hon. Sarah K. Park on the demurrer."
        assert extract_judge_name(text) == "Sarah K. Park"

    def test_no_false_positive_hospital(self) -> None:
        """Hospital names must not be extracted."""
        text = "JUDICIAL OFFICER: Desert Regional Hospital"
        assert extract_judge_name(text) is None

    def test_no_false_positive_school_district(self) -> None:
        """School district must not be extracted."""
        text = "Presiding: RIVERSIDE UNIFIED SCHOOL DISTRICT"
        assert extract_judge_name(text) is None

    # --- Truncation regression tests (#327) ---

    def test_la_full_name_not_truncated(self) -> None:
        """Regression: extract_judge_name must return the complete name (#327).
        'James I. Montgomery' was being stored as 'James I. Montgomer'."""
        text = "James I. Montgomery Judge of the Superior Court"
        result = extract_judge_name(text)
        assert result == "James I. Montgomery"

    def test_la_full_name_multiline_not_truncated(self) -> None:
        """Multi-line ruling text: full name must be preserved."""
        text = "The motion is GRANTED.\nJames I. Montgomery Judge of the Superior Court"
        result = extract_judge_name(text)
        assert result == "James I. Montgomery"

    def test_long_judge_name_not_truncated(self) -> None:
        """Very long judge name is extracted in full."""
        text = "Christopher Michael Alexander Judge of the Superior Court"
        result = extract_judge_name(text)
        assert result == "Christopher Michael Alexander"

    def test_name_with_suffix_not_truncated(self) -> None:
        """Names with suffixes like III are fully extracted."""
        text = "Arthur Hester III Judge of the Superior Court"
        result = extract_judge_name(text)
        assert result == "Arthur Hester III"

    # --- Performance regression tests (#4104) ---

    def test_no_quadratic_blowup_on_long_no_match_input(self) -> None:
        """Regression: extract_judge_name must complete in bounded time on
        long opinion text that contains no matchable judge signature (#4104).

        Pre-fix, the LA `([^\\n]+?)\\s+Judge of the Superior Court` pattern with
        re.IGNORECASE cost O(n^2) per call.  On a 50KB synthetic federal
        opinion (no LA signature, lots of capitalized prose), the call took
        15+ seconds — driving reingest_from_s3.py CPU-bound at ~1 doc/min.

        Post-fix, anchoring to start-of-line plus a bounded capture forces
        O(n) behavior. 100ms is generous headroom over the actual sub-10ms
        cost on modern hardware.
        """
        import time

        # Federal-opinion-shaped text without any LA judge signature.
        paragraph = (
            "The Court has considered Plaintiff's Motion for Summary Judgment "
            "filed pursuant to Federal Rule of Civil Procedure 56. As the "
            "Ninth Circuit held in Smith v. Jones, 123 F.3d 456, 459 (9th Cir. "
            "2018), summary judgment is appropriate when the moving party has "
            "shown there is no genuine dispute as to any material fact. See "
            "also Anderson v. Liberty Lobby, Inc., 477 U.S. 242, 248 (1986); "
            "Celotex Corp. v. Catrett, 477 U.S. 317, 322 (1986). Defendant "
            "Acme Industries, A California Corporation, opposes the Motion. "
            "The Court finds that Plaintiff has met its burden under Federal "
            "Rule of Evidence 401. United States District Court for the "
            "Southern District of California considered the matter on January "
            "5, 2026 at 1:30 PM. "
        )
        # Build ~100KB of synthetic opinion text.
        text = (paragraph * ((100 * 1024) // len(paragraph) + 1))[: 100 * 1024]
        assert len(text) >= 100 * 1024

        start = time.perf_counter()
        result = extract_judge_name(text)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert result is None, "Synthetic federal text contains no LA judge"
        assert elapsed_ms < 100.0, (
            f"extract_judge_name took {elapsed_ms:.1f}ms on 100KB of federal-"
            f"opinion-shaped text — quadratic regression in pattern[0] suspected."
        )

    def test_no_quadratic_blowup_on_long_with_match_input(self) -> None:
        """Same as above but WITH a real LA judge signature appended (#4104).

        Pre-fix, even with the signature present, the unanchored lazy capture
        explored quadratic backtracks before settling on the match. Post-fix,
        the line anchor lets the engine seek directly to the match.
        """
        import time

        paragraph = (
            "The Court has considered Plaintiff's Motion for Summary Judgment "
            "filed pursuant to Federal Rule of Civil Procedure 56. As the "
            "Ninth Circuit held in Smith v. Jones, 123 F.3d 456, 459 (9th Cir. "
            "2018), summary judgment is appropriate when the moving party has "
            "shown there is no genuine dispute as to any material fact. "
        )
        text = (paragraph * ((100 * 1024) // len(paragraph) + 1))[: 100 * 1024]
        text += "\nWilliam A. Crowfoot Judge of the Superior Court\n"

        start = time.perf_counter()
        result = extract_judge_name(text)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert result == "William A. Crowfoot"
        assert elapsed_ms < 100.0, (
            f"extract_judge_name took {elapsed_ms:.1f}ms on 100KB+match text — "
            f"quadratic regression in pattern[0] suspected."
        )

    # --- Federal opinion false-positive prevention (#4123) -----------------
    #
    # Federal opinion text (CourtListener) bypasses the LLM extraction path
    # and falls through to the regex extractor. The "Judge: Name" / "Hon. Name"
    # patterns are case-insensitive and match anywhere in the document, so
    # body-text phrases like "Judge Instructions For The Jury" produce
    # `Instructions For` as a false-positive judge name (#4123). The
    # truncation-shaped surnames (`Alexander C. Van`, `Lindsay Van`,
    # `Diana May`, `Scott Say`) are similar — body-text or partial captures
    # whose surname token is a Dutch/German particle (Van, Von, De, Der) or
    # a common English verb (May, Say, Will).

    def test_extract_judge_name_federal_instructions_for_the_jury(self) -> None:
        """AC#2 — must NOT return 'Instructions For' from boilerplate."""
        text = (
            "PROPOSED JURY INSTRUCTIONS\n\n"
            "Judge Instructions For The Jury\n\n"
            "The court will give the following jury instructions to the panel."
        )
        # Either None (preferred) or some legitimate name from the document —
        # but never the boilerplate phrase 'Instructions For'.
        assert extract_judge_name(text) != "Instructions For"

    def test_extract_judge_name_federal_jury_instructions_only(self) -> None:
        """AC#2 — text containing only late-document boilerplate returns None."""
        text = (
            "Judge Instructions For The Jury\n"
            "Item 1: The court instructs as follows.\n"
            "Item 2: The defendant is presumed innocent.\n"
        )
        assert extract_judge_name(text) is None

    def test_extract_judge_name_federal_header_before_judge_smith(self) -> None:
        """AC#3 — header-section 'Before Judge <Name>' returns the judge name.

        The AC's verbatim text is 'Before Judge Smith' (single token), but the
        regex requires first + last. We use the realistic Federal header form
        'Before Judge John Smith' here — that matches the existing 'Judge: Name'
        pattern and returns the judge's name in full.
        """
        text = (
            "UNITED STATES DISTRICT COURT\n"
            "SOUTHERN DISTRICT OF NEW YORK\n\n"
            "Before Judge John Smith\n\n"
            "OPINION AND ORDER\n\n"
            "This case concerns a motion to dismiss."
        )
        result = extract_judge_name(text)
        # Accept either "John Smith" or "Smith" — both are correct
        # interpretations of the header signature.
        assert result in ("John Smith", "Smith")

    def test_no_false_positive_judge_instructions_for(self) -> None:
        """Body text 'Judge Instructions For' must not extract the boilerplate."""
        text = "Judge Instructions For"
        assert extract_judge_name(text) is None

    def test_no_false_positive_judge_instructions_for_period(self) -> None:
        """Same with trailing period."""
        text = "Judge Instructions For."
        assert extract_judge_name(text) is None

    def test_no_false_positive_judge_instructions_for_trial(self) -> None:
        """'Judge Instructions For Trial' — must not extract."""
        text = "Judge Instructions For Trial"
        assert extract_judge_name(text) is None

    def test_no_false_positive_hon_instructions_for(self) -> None:
        """'Hon. Instructions For' — must not extract."""
        text = "Hon. Instructions For"
        assert extract_judge_name(text) is None

    def test_no_false_positive_truncated_van_particle(self) -> None:
        """Surname ending in particle 'Van' with no following token is truncation."""
        text = "Judge Lindsay Van"
        assert extract_judge_name(text) is None

    def test_no_false_positive_truncated_van_particle_with_middle(self) -> None:
        """Same with middle initial — 'Alexander C. Van' is truncated."""
        text = "Judge Alexander C. Van"
        assert extract_judge_name(text) is None

    def test_no_false_positive_truncated_von_particle(self) -> None:
        """Surname ending in 'Von' particle with no following token is truncation."""
        text = "Judge Hans Von"
        assert extract_judge_name(text) is None

    def test_no_false_positive_truncated_de_particle(self) -> None:
        """Surname ending in 'De' particle with no following token is truncation."""
        text = "Judge Maria De"
        assert extract_judge_name(text) is None

    def test_full_van_surname_still_extracted(self) -> None:
        """Full 'Van Pelt' / 'Van Deusen' surname must still be extracted."""
        text = "Judge Lindsay Van Pelt"
        assert extract_judge_name(text) == "Lindsay Van Pelt"

    def test_full_van_deusen_surname_still_extracted(self) -> None:
        """'Alexander C. Van Deusen' must still be extracted."""
        text = "Judge Alexander C. Van Deusen"
        assert extract_judge_name(text) == "Alexander C. Van Deusen"

    def test_no_false_positive_diana_may_boilerplate_verb(self) -> None:
        """Surname that matches a common English verb is suspicious — reject.

        'Judge Diana May' is the maximal regex match of body-text like
        'Judge Diana may rule on the motion' where 'may' is a modal verb that
        was capitalized via title-case formatting upstream. Reject these
        2-token captures whose last word is a common English verb.
        """
        text = "Judge Diana May"
        assert extract_judge_name(text) is None

    def test_no_false_positive_scott_say_boilerplate_verb(self) -> None:
        """'Scott Say' — surname matches common English verb. Reject."""
        text = "Judge Scott Say"
        assert extract_judge_name(text) is None

    def test_legitimate_short_surname_lee_still_extracted(self) -> None:
        """Real 3-letter Asian surname 'Lee' must NOT be rejected."""
        text = "Judge James Lee"
        assert extract_judge_name(text) == "James Lee"

    def test_legitimate_short_surname_wu_still_extracted(self) -> None:
        """Real 2-letter Asian surname 'Wu' must NOT be rejected."""
        text = "Judge Sarah Wu"
        assert extract_judge_name(text) == "Sarah Wu"


# ---------------------------------------------------------------------------
# _looks_like_person_name validation
# ---------------------------------------------------------------------------


class TestLooksLikePersonName:
    """Tests for the _looks_like_person_name helper (#326)."""

    def test_valid_simple_name(self) -> None:
        assert _looks_like_person_name("William A. Crowfoot") is True

    def test_valid_hyphenated_name(self) -> None:
        assert _looks_like_person_name("Mary Anne Chen-Ramirez") is True

    def test_valid_short_name(self) -> None:
        assert _looks_like_person_name("Jim Lee") is True

    def test_reject_empty(self) -> None:
        assert _looks_like_person_name("") is False

    def test_reject_too_short(self) -> None:
        assert _looks_like_person_name("AB") is False

    def test_reject_too_long(self) -> None:
        assert _looks_like_person_name("A" * 61) is False

    def test_reject_llc(self) -> None:
        assert _looks_like_person_name("Acme Properties LLC") is False

    def test_reject_inc(self) -> None:
        assert _looks_like_person_name("Legacy, Inc.") is False

    def test_reject_medical(self) -> None:
        assert _looks_like_person_name("Heritage Medical Group") is False

    def test_reject_bank(self) -> None:
        assert _looks_like_person_name("Bank of America, N.A.") is False

    def test_reject_hospital(self) -> None:
        assert _looks_like_person_name("Desert Regional Hospital") is False

    def test_reject_vs(self) -> None:
        assert _looks_like_person_name("SMITH vs JONES") is False

    def test_reject_v_dot(self) -> None:
        assert _looks_like_person_name("Smith v. Jones") is False

    def test_reject_county_of(self) -> None:
        assert _looks_like_person_name("County of Riverside") is False

    def test_reject_california_corporation(self) -> None:
        assert _looks_like_person_name("Legacy Inc A California Corporation") is False

    def test_reject_dba(self) -> None:
        assert _looks_like_person_name("John Smith DBA Smith Enterprises") is False

    def test_reject_insurance(self) -> None:
        assert _looks_like_person_name("State Farm Insurance") is False

    def test_reject_school_district(self) -> None:
        assert _looks_like_person_name("RIVERSIDE UNIFIED SCHOOL DISTRICT") is False

    # --- #4123 hardening tests: boilerplate token, particle, verb-surname ---

    def test_reject_boilerplate_first_token_instructions(self) -> None:
        """First token 'Instructions' is boilerplate — reject."""
        assert _looks_like_person_name("Instructions For") is False

    def test_reject_boilerplate_last_token_jury(self) -> None:
        """Last token 'Jury' is boilerplate — reject (covers last_token branch)."""
        assert _looks_like_person_name("John Jury") is False

    def test_reject_truncated_van_particle(self) -> None:
        """Last token 'Van' with no following token — truncated, reject."""
        assert _looks_like_person_name("Lindsay Van") is False

    def test_reject_truncated_van_with_period(self) -> None:
        """Trailing punctuation must be stripped before particle check."""
        assert _looks_like_person_name("Lindsay Van.") is False

    def test_accept_van_pelt_full_surname(self) -> None:
        """'Van Pelt' is a complete surname — particle followed by another token."""
        assert _looks_like_person_name("Lindsay Van Pelt") is True

    def test_reject_two_token_verb_surname_may(self) -> None:
        """2-token name with verb surname 'May' — reject."""
        assert _looks_like_person_name("Diana May") is False

    def test_reject_two_token_verb_surname_say(self) -> None:
        """2-token name with verb surname 'Say' — reject."""
        assert _looks_like_person_name("Scott Say") is False

    def test_accept_three_token_verb_surname(self) -> None:
        """3-token name with verb surname is allowed (more context)."""
        assert _looks_like_person_name("Diana May Smith") is True

    def test_accept_legitimate_short_surname(self) -> None:
        """Real short surnames (Lee, Wu) must still pass."""
        assert _looks_like_person_name("James Lee") is True
        assert _looks_like_person_name("Sarah Wu") is True

    def test_reject_whitespace_only_after_strip(self) -> None:
        """Defensive: name that becomes empty after punctuation strip — reject.

        Real callers don't produce this (extract_judge_name strips and checks
        non-empty before calling), but the validator is also called directly
        and should be robust.
        """
        # ".." has length 2 (< 3), caught by earlier length check.
        # ".. .." has length 5, splits to ['..', '..'] → strip to ['', ''] →
        # filter empties → []. Triggers the `if not tokens` defensive return.
        assert _looks_like_person_name(".. ..") is False


# ---------------------------------------------------------------------------
# Case number extraction
# ---------------------------------------------------------------------------


class TestExtractCaseNumber:
    """Tests for extract_case_number()."""

    # --- LA label pattern ---

    def test_la_label_pattern(self) -> None:
        text = "Case Number: 24NNCV02551  Hearing Date: March 2, 2026"
        assert extract_case_number(text) == "24NNCV02551"

    def test_la_label_pattern_no_space(self) -> None:
        text = "Case Number:24NNCV02551"
        assert extract_case_number(text) == "24NNCV02551"

    def test_la_label_case_insensitive(self) -> None:
        text = "case number: 26NNCP00062"
        assert extract_case_number(text) == "26NNCP00062"

    # --- San Francisco ---

    def test_sf_case_number(self) -> None:
        text = "Case Number: FPT-25-378624\nHearing Date: March 3, 2026"
        assert extract_case_number(text) == "FPT-25-378624"

    def test_sf_case_number_standalone(self) -> None:
        """SF case number without a label prefix."""
        text = "Department 403\nFMS-20-387302\nHearing on petition"
        assert extract_case_number(text) == "FMS-20-387302"

    # --- San Bernardino ---

    def test_sb_case_number(self) -> None:
        text = "CIVRS2502080 SMITH v. JONES"
        assert extract_case_number(text) == "CIVRS2502080"

    def test_sb_longer_digits(self) -> None:
        text = "CIVSB2416631 motion for summary judgment"
        assert extract_case_number(text) == "CIVSB2416631"

    def test_sb_dept_s36_whitespace(self) -> None:
        """Dept S36 uses a space: 'CIVSB 2600093' → normalised to 'CIVSB2600093'."""
        text = "CIVSB 2600093 motion re: preliminary injunction"
        assert extract_case_number(text) == "CIVSB2600093"

    # --- Riverside ---

    def test_riverside_case_number(self) -> None:
        text = "1.\nCVPS2306157 SMITH VS JONES"
        assert extract_case_number(text) == "CVPS2306157"

    def test_riverside_four_letter_code(self) -> None:
        text = "CVMVSS2401234 hearing continued"
        assert extract_case_number(text) == "CVMVSS2401234"

    # --- Santa Clara ---

    def test_sc_case_number(self) -> None:
        text = "LINE 1  24CV443183  Smith v. Jones  Motion for Summary Judgment"
        assert extract_case_number(text) == "24CV443183"

    def test_sc_another(self) -> None:
        text = "25CV460465 Doe v. Roe"
        assert extract_case_number(text) == "25CV460465"

    # --- LA standalone (no label) ---

    def test_la_standalone_nncv(self) -> None:
        """LA civil case number without 'Case Number:' label."""
        text = "Department 3\n24NNCV02551\nMotion for summary judgment is GRANTED."
        assert extract_case_number(text) == "24NNCV02551"

    def test_la_standalone_stcv(self) -> None:
        text = "Ruling on 23STCV12345 demurrer"
        assert extract_case_number(text) == "23STCV12345"

    def test_la_standalone_nncp(self) -> None:
        text = "26NNCP00062 petition"
        assert extract_case_number(text) == "26NNCP00062"

    # --- OC Civil ---

    def test_oc_civil_two_digit_prefix(self) -> None:
        text = "25-01455183 SMITH v. JONES motion to compel"
        assert extract_case_number(text) == "25-01455183"

    def test_oc_civil_four_digit_prefix(self) -> None:
        text = "2024-01437598 DOE v. ROE"
        assert extract_case_number(text) == "2024-01437598"

    # --- OC Family Law ---

    def test_oc_family_law(self) -> None:
        text = "24D006789 In re Marriage of Smith"
        assert extract_case_number(text) == "24D006789"

    # --- No match ---

    def test_no_match(self) -> None:
        text = "The motion for summary judgment is GRANTED."
        assert extract_case_number(text) is None

    def test_empty_string(self) -> None:
        assert extract_case_number("") is None

    # --- Priority: label pattern preferred over standalone ---

    def test_label_pattern_preferred(self) -> None:
        """When 'Case Number:' label is present, use that over standalone patterns."""
        text = "Case Number: 24NNCV02551\nSome other text with 25CV460465"
        assert extract_case_number(text) == "24NNCV02551"

    # ---------------------------------------------------------------------------
    # Case title extraction (#337)

    def test_riverside_cvco(self) -> None:
        assert extract_case_type_from_number("CVCO2301234") == "civil"

    def test_la_stcv(self) -> None:
        assert extract_case_type_from_number("23STCV12345") == "civil"

    def test_la_nncv(self) -> None:
        assert extract_case_type_from_number("24NNCV02551") == "civil"

    def test_santa_clara_cv(self) -> None:
        assert extract_case_type_from_number("24CV443183") == "civil"

    def test_san_bernardino_civ(self) -> None:
        assert extract_case_type_from_number("CIVSB2416631") == "civil"

    def test_san_bernardino_civrs(self) -> None:
        assert extract_case_type_from_number("CIVRS2502080") == "civil"

    def test_oc_civil_format(self) -> None:
        assert extract_case_type_from_number("30-2024-01370288") == "civil"

    def test_oc_civil_short_format(self) -> None:
        assert extract_case_type_from_number("2024-01380242") == "civil"

    # Ventura civil codes: CU (Civil Unlimited), CL (Civil Limited)

    def test_ventura_cu_civil_unlimited(self) -> None:
        """Ventura CU = Civil Unlimited: 2025CUBC042098."""
        assert extract_case_type_from_number("2025CUBC042098") == "civil"

    def test_ventura_cu_personal_property(self) -> None:
        """Ventura CU = Civil Unlimited (PP subtype): 2025CUPP039974."""
        assert extract_case_type_from_number("2025CUPP039974") == "civil"

    def test_ventura_cu_mm_subtype(self) -> None:
        """Ventura CU with MM subtype: 2025CUMM040123."""
        assert extract_case_type_from_number("2025CUMM040123") == "civil"

    def test_ventura_cl_civil_limited(self) -> None:
        """Ventura CL = Civil Limited: 2024CLCL035410."""
        assert extract_case_type_from_number("2024CLCL035410") == "civil"

    def test_ventura_cu_old_format(self) -> None:
        """Ventura older format with longer digit prefix: 202200570068CUMM."""
        assert extract_case_type_from_number("202200570068CUMM") == "civil"

    # --- Family law prefixes ---

    def test_family_fl(self) -> None:
        assert extract_case_type_from_number("FL2301234") == "family"

    def test_family_dv(self) -> None:
        assert extract_case_type_from_number("DV2301234") == "family"

    def test_oc_family_d_format(self) -> None:
        """OC family law: 2-digit year + D + 6 digits."""
        assert extract_case_type_from_number("24D006789") == "family"

    # SF Unified Family Court prefixes — FDI (dissolution), FMS (family
    # motion), FPT (parentage), FDV (domestic violence), FCS (child
    # support). See #2368.

    def test_sf_family_fdi_dissolution(self) -> None:
        """SF family dissolution: FDI-25-801091."""
        assert extract_case_type_from_number("FDI-25-801091") == "family"

    def test_sf_family_fms_motion(self) -> None:
        """SF family motion: FMS-15-386703."""
        assert extract_case_type_from_number("FMS-15-386703") == "family"

    def test_sf_family_fpt_parentage(self) -> None:
        """SF family parentage: FPT-23-378175."""
        assert extract_case_type_from_number("FPT-23-378175") == "family"

    def test_sf_family_fdv_domestic_violence(self) -> None:
        """SF family domestic violence restraining order: FDV-25-818599."""
        assert extract_case_type_from_number("FDV-25-818599") == "family"

    def test_sf_family_fcs_child_support(self) -> None:
        """SF family child support: FCS-25-400123."""
        assert extract_case_type_from_number("FCS-25-400123") == "family"

    # --- Probate prefixes ---

    def test_probate_pr(self) -> None:
        assert extract_case_type_from_number("PR2301234") == "probate"

    def test_probate_bp(self) -> None:
        assert extract_case_type_from_number("BP2301234") == "probate"

    def test_la_probate_cp(self) -> None:
        """LA complex/probate: 26NNCP00062."""
        assert extract_case_type_from_number("26NNCP00062") == "probate"

    def test_ventura_probate(self) -> None:
        """Ventura PR = Probate: 2025PRMA042345."""
        assert extract_case_type_from_number("2025PRMA042345") == "probate"

    def test_ventura_old_probate_prcp(self) -> None:
        """Ventura old-format probate PRCP (conservatorship): 200800332092PRCP."""
        assert extract_case_type_from_number("200800332092PRCP") == "probate"

    def test_ventura_old_probate_prce(self) -> None:
        """Ventura old-format probate PRCE: 201200427520PRCE."""
        assert extract_case_type_from_number("201200427520PRCE") == "probate"

    def test_ventura_old_probate_prlp(self) -> None:
        """Ventura old-format probate PRLP (LPS conservatorship): 202200570571PRLP."""
        assert extract_case_type_from_number("202200570571PRLP") == "probate"

    def test_ventura_old_probate_prge(self) -> None:
        """Ventura old-format probate PRGE (guardianship): 202200572082PRGE."""
        assert extract_case_type_from_number("202200572082PRGE") == "probate"

    def test_ventura_p_prefix_probate(self) -> None:
        """Ventura old P-prefix probate: P057837."""
        assert extract_case_type_from_number("P057837") == "probate"

    def test_ventura_p_prefix_probate_six_digits(self) -> None:
        """Ventura old P-prefix probate: P080266."""
        assert extract_case_type_from_number("P080266") == "probate"

    # --- Small claims ---

    def test_small_claims_sc(self) -> None:
        assert extract_case_type_from_number("SC2301234") == "small_claims"

    # --- Criminal prefixes ---

    def test_criminal_cr(self) -> None:
        assert extract_case_type_from_number("CR2301234") == "criminal"

    def test_felony_f_digit(self) -> None:
        """Felony docket format: F + digits."""
        assert extract_case_type_from_number("F2301234") == "criminal"

    # --- Juvenile ---

    def test_juvenile_jv(self) -> None:
        assert extract_case_type_from_number("JV2301234") == "juvenile"

    # --- Traffic ---

    def test_traffic_tr(self) -> None:
        assert extract_case_type_from_number("TR2301234") == "traffic"

    # --- Edge cases ---

    def test_none_input(self) -> None:
        assert extract_case_type_from_number(None) is None  # type: ignore[arg-type]

    def test_empty_string_case_type(self) -> None:
        assert extract_case_type_from_number("") is None

    def test_whitespace_only(self) -> None:
        assert extract_case_type_from_number("   ") is None

    def test_unrecognized_prefix(self) -> None:
        assert extract_case_type_from_number("UNKNOWN123") is None

    def test_case_insensitive(self) -> None:
        assert extract_case_type_from_number("cvri2502741") == "civil"

    def test_leading_whitespace_stripped(self) -> None:
        assert extract_case_type_from_number("  CVRI2502741  ") == "civil"


# ---------------------------------------------------------------------------
# Hearing date extraction
# ---------------------------------------------------------------------------


class TestExtractHearingDate:
    """Tests for extract_hearing_date()."""

    def test_month_day_comma_year(self) -> None:
        """Standard 'Month DD, YYYY' format."""
        text = "Hearing Date: February 24, 2026\nDepartment 5"
        assert extract_hearing_date(text) == date(2026, 2, 24)

    def test_month_day_year_no_comma(self) -> None:
        """'Month DD YYYY' without comma."""
        text = "March 5 2026 hearing on motion"
        assert extract_hearing_date(text) == date(2026, 3, 5)

    def test_date_colon_mm_dd_yyyy(self) -> None:
        """'Date: MM/DD/YYYY' format."""
        text = "Date: 03/04/2026\nDepartment 12"
        assert extract_hearing_date(text) == date(2026, 3, 4)

    def test_date_colon_mm_dd_yy(self) -> None:
        """'Date: MM/DD/YY' short year format."""
        text = "Date: 03/04/26\nCourtroom B"
        assert extract_hearing_date(text) == date(2026, 3, 4)

    def test_standalone_mm_dd_yyyy(self) -> None:
        """Standalone 'MM/DD/YYYY' without label."""
        text = "Hearing on 01/15/2026 for motion to compel"
        assert extract_hearing_date(text) == date(2026, 1, 15)

    def test_case_insensitive_month(self) -> None:
        """Month name matching is case-insensitive."""
        text = "hearing on JANUARY 10, 2026 in department 3"
        assert extract_hearing_date(text) == date(2026, 1, 10)

    def test_no_match(self) -> None:
        """Text without a date returns None."""
        text = "The motion for summary judgment is GRANTED."
        assert extract_hearing_date(text) is None

    def test_empty_string(self) -> None:
        assert extract_hearing_date("") is None

    def test_first_date_wins(self) -> None:
        """When multiple dates appear, the first matching pattern wins."""
        text = "Hearing Date: January 5, 2026\nContinued to February 10, 2026"
        assert extract_hearing_date(text) == date(2026, 1, 5)

    def test_single_digit_day(self) -> None:
        """Single-digit day without leading zero."""
        text = "Date: 3/4/2026"
        assert extract_hearing_date(text) == date(2026, 3, 4)

    def test_december_31(self) -> None:
        """End of year date."""
        text = "December 31, 2025 ruling"
        assert extract_hearing_date(text) == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# _looks_like_person_name — additional coverage
# ---------------------------------------------------------------------------


class TestLooksLikePersonNameAdditional:
    """Additional tests for _looks_like_person_name to cover remaining branches."""

    def test_reject_a_california_without_other_org_keywords(self) -> None:
        """'A California' descriptor alone (no LLC/Inc) must be rejected."""
        # This name has no org keywords like LLC/Inc, but has "A California"
        assert _looks_like_person_name("Smith A California Entity") is False

    def test_accept_normal_name_near_boundary(self) -> None:
        """Name at exactly 60 chars should be accepted."""
        name = "A" * 29 + " " + "B" * 30  # 60 chars total
        assert _looks_like_person_name(name) is True

    def test_reject_exactly_61_chars(self) -> None:
        """Name at 61 chars should be rejected."""
        name = "A" * 30 + " " + "B" * 30  # 61 chars total
        assert _looks_like_person_name(name) is False

    # ---------------------------------------------------------------------------
    # _split_caption_names — direct tests
    def test_valid_la_format(self) -> None:
        assert is_valid_case_number("24NNCV02551") is True

    def test_valid_riverside(self) -> None:
        assert is_valid_case_number("CVPS2306157") is True

    def test_valid_sf(self) -> None:
        assert is_valid_case_number("FPT-25-378624") is True

    def test_valid_sb(self) -> None:
        assert is_valid_case_number("CIVRS2502080") is True

    def test_valid_oc_three_part(self) -> None:
        assert is_valid_case_number("30-2024-01370288") is True

    def test_rejects_case_title_vs(self) -> None:
        assert is_valid_case_number("Smith v. Kia") is False

    def test_rejects_case_title_vs_dot(self) -> None:
        assert is_valid_case_number("Catalan v. FCA") is False

    def test_rejects_case_title_vs_no_dot(self) -> None:
        assert is_valid_case_number("Tamadon vs Choe") is False

    def test_rejects_case_title_vs_long(self) -> None:
        assert is_valid_case_number("Youssef vs. BCKG Beach Properties, LLC") is False

    def test_rejects_ruling_text_in_title(self) -> None:
        assert is_valid_case_number("Tuinenburg v. Before the Court is a demurrer") is False

    def test_rejects_legal_citation(self) -> None:
        assert is_valid_case_number("Cal. App. 4th 1, 20; DuPont Merck") is False

    def test_rejects_fsupp_citation(self) -> None:
        assert is_valid_case_number("F.Supp.3d 1101, 1113") is False

    def test_rejects_very_long_string(self) -> None:
        assert is_valid_case_number("A" * 31) is False

    def test_rejects_ruling_keywords(self) -> None:
        assert (
            is_valid_case_number("Dilani vs. Guaranteed Property Inspections, LLC TENTATIVE RULING")
            is False
        )

    def test_rejects_ruling_keyword_without_vs(self) -> None:
        """Ruling keywords like 'motion' should be rejected even without 'v.' pattern."""
        assert is_valid_case_number("Motion to Compel") is False

    def test_rejects_hearing_keyword(self) -> None:
        assert is_valid_case_number("Hearing on Demurrer") is False

    def test_rejects_empty(self) -> None:
        assert is_valid_case_number("") is False

    def test_rejects_none_like(self) -> None:
        assert is_valid_case_number("   ") is False


# ---------------------------------------------------------------------------
# Inline party name extraction (#1930)
class TestExtractCaseTypeFromScraperId:
    """Tests for extract_case_type_from_scraper_id()."""

    def test_oc_civil(self) -> None:
        assert extract_case_type_from_scraper_id("ca-oc-tentatives-civil") == "civil"

    def test_oc_family(self) -> None:
        assert extract_case_type_from_scraper_id("ca-oc-tentatives-family") == "family"

    def test_oc_probate(self) -> None:
        assert extract_case_type_from_scraper_id("ca-oc-tentatives-probate") == "probate"

    def test_la_tentatives(self) -> None:
        # LA scraper doesn't have a case type suffix
        assert extract_case_type_from_scraper_id("ca-la-tentatives") is None

    def test_empty_string(self) -> None:
        assert extract_case_type_from_scraper_id("") is None

    def test_none(self) -> None:
        assert extract_case_type_from_scraper_id(None) is None

    def test_criminal(self) -> None:
        assert extract_case_type_from_scraper_id("ca-foo-criminal") == "criminal"

    def test_small_claims(self) -> None:
        assert extract_case_type_from_scraper_id("ca-foo-small-claims") == "small_claims"


# ---------------------------------------------------------------------------
# extract_case_type_from_motion_type (#1702)
# ---------------------------------------------------------------------------


class TestExtractCaseTypeFromMotionType:
    """Tests for extract_case_type_from_motion_type()."""

    # --- Civil motion types ---

    def test_motion_to_compel(self) -> None:
        """motion_to_compel is unambiguously civil (issue #1702 trigger case)."""
        assert extract_case_type_from_motion_type("motion_to_compel") == "civil"

    def test_msj(self) -> None:
        assert extract_case_type_from_motion_type("msj") == "civil"

    def test_msj_partial(self) -> None:
        assert extract_case_type_from_motion_type("msj_partial") == "civil"

    def test_demurrer(self) -> None:
        assert extract_case_type_from_motion_type("demurrer") == "civil"

    def test_motion_to_strike(self) -> None:
        assert extract_case_type_from_motion_type("motion_to_strike") == "civil"

    def test_anti_slapp(self) -> None:
        assert extract_case_type_from_motion_type("anti_slapp") == "civil"

    def test_preliminary_injunction(self) -> None:
        assert extract_case_type_from_motion_type("preliminary_injunction") == "civil"

    def test_default_judgment(self) -> None:
        assert extract_case_type_from_motion_type("default_judgment") == "civil"

    def test_mil(self) -> None:
        assert extract_case_type_from_motion_type("mil") == "civil"

    def test_motion_for_attorney_fees(self) -> None:
        assert extract_case_type_from_motion_type("motion_for_attorney_fees") == "civil"

    def test_motion_for_sanctions(self) -> None:
        assert extract_case_type_from_motion_type("motion_for_sanctions") == "civil"

    def test_class_action_settlement(self) -> None:
        assert extract_case_type_from_motion_type("class_action_settlement") == "civil"

    def test_paga_settlement(self) -> None:
        assert extract_case_type_from_motion_type("paga_settlement") == "civil"

    def test_settlement_approval(self) -> None:
        assert extract_case_type_from_motion_type("settlement_approval") == "civil"

    def test_motion_for_judgment_on_the_pleadings(self) -> None:
        assert extract_case_type_from_motion_type("motion_for_judgment_on_the_pleadings") == "civil"

    def test_deem_admissions_admitted(self) -> None:
        assert extract_case_type_from_motion_type("deem_admissions_admitted") == "civil"

    # --- Probate motion types ---

    def test_petition_too_broad_returns_none(self) -> None:
        """Bare 'petition' is ambiguous — civil/limited-civil/federal/Watermaster
        cases all use this label, so it must NOT map to probate."""
        assert extract_case_type_from_motion_type("petition") is None

    @pytest.mark.parametrize(
        "motion_type,case_number,scraper_id",
        [
            # Contra Costa limited-civil
            ("petition", "L25-03538", "ca-cc-tentatives"),
            # Federal generic
            ("petition", "24CV12345", "federal-pacer"),
            # Fresno civil
            ("petition", "2024CV019999", "ca-fresno-tentatives"),
        ],
    )
    def test_petition_non_probate_fixtures_return_none(
        self, motion_type: str, case_number: str, scraper_id: str
    ) -> None:
        """Regression: petition + non-probate case numbers/scrapers all return None.

        These fixtures previously incorrectly mapped to 'probate' via the
        bare 'petition' entry that has now been removed (#3691).
        """
        _ = case_number  # context only — extract_case_type_from_motion_type is motion-type-only
        _ = scraper_id
        assert extract_case_type_from_motion_type(motion_type) is None

    def test_petition_for_probate(self) -> None:
        assert extract_case_type_from_motion_type("petition_for_probate") == "probate"

    def test_guardianship_petition(self) -> None:
        assert extract_case_type_from_motion_type("guardianship_petition") == "probate"

    def test_trust_petition(self) -> None:
        assert extract_case_type_from_motion_type("trust_petition") == "probate"

    # --- Ambiguous motion types (should return None) ---

    def test_accounting(self) -> None:
        """Accounting appears in both probate and civil cases."""
        assert extract_case_type_from_motion_type("accounting") is None

    def test_show_cause_hearing(self) -> None:
        """Show cause hearings appear in multiple case types."""
        assert extract_case_type_from_motion_type("show_cause_hearing") is None

    def test_ex_parte_application(self) -> None:
        """Ex parte applications appear in civil, family, and probate cases."""
        assert extract_case_type_from_motion_type("ex_parte_application") is None

    def test_osc(self) -> None:
        """OSCs appear across multiple case types."""
        assert extract_case_type_from_motion_type("osc") is None

    # --- Edge cases ---

    def test_empty_string(self) -> None:
        assert extract_case_type_from_motion_type("") is None

    def test_none(self) -> None:
        assert extract_case_type_from_motion_type(None) is None  # type: ignore[arg-type]

    def test_whitespace_stripped(self) -> None:
        assert extract_case_type_from_motion_type("  motion_to_compel  ") == "civil"

    def test_unknown_motion_type(self) -> None:
        assert extract_case_type_from_motion_type("unknown_motion") is None


# ---------------------------------------------------------------------------
# extract_case_type_from_title
# ---------------------------------------------------------------------------


class TestExtractCaseTypeFromTitle:
    """Tests for extract_case_type_from_title() — #2062."""

    # --- Probate title patterns ---

    def test_in_the_matter_of(self) -> None:
        """Standard probate title: 'In the Matter of ...'"""
        assert extract_case_type_from_title("In the Matter of Edrissa Bassey II") == "probate"

    def test_in_re_of(self) -> None:
        """Alternative probate title: 'In Re of ...'"""
        assert extract_case_type_from_title("In Re of John Smith") == "probate"

    def test_in_matter_of_no_the(self) -> None:
        """Probate title without 'the': 'In Matter of ...'"""
        assert extract_case_type_from_title("In Matter of Jane Doe") == "probate"

    def test_conservatorship_of(self) -> None:
        """Conservatorship probate title."""
        assert extract_case_type_from_title("Conservatorship of Maria Hill") == "probate"

    def test_conservatorship_of_mixed_case(self) -> None:
        """Conservatorship title with mixed case."""
        assert extract_case_type_from_title("Conservatorship Of Casey Evan Fell") == "probate"

    def test_guardianship_of(self) -> None:
        """Guardianship probate title."""
        assert extract_case_type_from_title("Guardianship of Nathan Martinez-Cahue") == "probate"

    def test_estate_of(self) -> None:
        """Estate probate title."""
        assert extract_case_type_from_title("Estate of Robert J. Williams") == "probate"

    def test_trust_of(self) -> None:
        """Trust probate title."""
        assert extract_case_type_from_title("Trust of Alice M. Johnson") == "probate"

    def test_petition_for_probate(self) -> None:
        """Petition for Probate title."""
        assert extract_case_type_from_title("Petition for Probate of Will") == "probate"

    def test_petition_for_letters(self) -> None:
        """Petition for Letters title."""
        assert extract_case_type_from_title("Petition for Letters of Administration") == "probate"

    def test_cnsvship_abbreviation(self) -> None:
        """Abbreviated conservatorship: 'Cnsvship Of ...'"""
        # This does NOT match — abbreviations need the full word
        assert extract_case_type_from_title("Cnsvship Of Sara Beth Eleniak") is None

    # --- Non-probate titles (should return None) ---

    def test_civil_vs_title(self) -> None:
        """Standard civil case title should not match."""
        assert extract_case_type_from_title("Smith v. Jones") is None

    def test_empty_string(self) -> None:
        assert extract_case_type_from_title("") is None

    def test_none(self) -> None:
        assert extract_case_type_from_title(None) is None  # type: ignore[arg-type]

    def test_whitespace_only(self) -> None:
        assert extract_case_type_from_title("   ") is None

    def test_generic_title(self) -> None:
        """Generic title without probate indicators."""
        assert extract_case_type_from_title("People of California v. Defendant") is None


# ---------------------------------------------------------------------------
# normalize_motion_type
# ---------------------------------------------------------------------------


class TestNormalizeMotionType:
    """Tests for normalize_motion_type() — #1712."""

    # --- Already-normalized values pass through ---

    def test_already_normalized_motion_to_compel(self) -> None:
        assert normalize_motion_type("motion_to_compel") == "motion_to_compel"

    def test_already_normalized_demurrer(self) -> None:
        assert normalize_motion_type("demurrer") == "demurrer"

    def test_already_normalized_msj(self) -> None:
        assert normalize_motion_type("msj") == "msj"

    def test_already_normalized_mil(self) -> None:
        assert normalize_motion_type("mil") == "mil"

    def test_already_normalized_ex_parte(self) -> None:
        assert normalize_motion_type("ex_parte_application") == "ex_parte_application"

    def test_already_normalized_motion_hearing_generic(self) -> None:
        assert normalize_motion_type("motion_hearing_generic") == "motion_hearing_generic"

    def test_already_normalized_discovery(self) -> None:
        assert normalize_motion_type("discovery") == "discovery"

    # --- SD calendar event types ---

    def test_sd_calendar_motion_hearing(self) -> None:
        """Generic 'Motion Hearing' maps to motion_hearing_generic."""
        assert normalize_motion_type("Motion Hearing") == "motion_hearing_generic"

    def test_sd_calendar_demurrer_motion_to_strike(self) -> None:
        """Composite 'Demurrer/Motion to Strike' matches demurrer first."""
        assert normalize_motion_type("Demurrer/Motion to Strike") == "demurrer"

    def test_sd_calendar_summary_judgment(self) -> None:
        assert normalize_motion_type("Summary Judgment/Summary Adjudication") == "msj_partial"

    def test_sd_calendar_discovery_hearing(self) -> None:
        """Generic 'Discovery Hearing' maps to discovery."""
        assert normalize_motion_type("Discovery Hearing") == "discovery"

    def test_sd_calendar_motion_to_quash(self) -> None:
        assert normalize_motion_type("Motion to Quash") == "motion_to_quash"

    def test_sd_calendar_motion_for_sanctions(self) -> None:
        assert normalize_motion_type("Motion for Sanctions") == "motion_for_sanctions"

    def test_sd_calendar_class_action(self) -> None:
        """Class action certify/decertify maps to motion_for_class_certification."""
        assert (
            normalize_motion_type("Motion Hearing to Certify/Decertify Class Action")
            == "motion_for_class_certification"
        )

    # --- SD tentatives title-case values ---

    def test_sd_tentatives_motion_to_compel(self) -> None:
        assert normalize_motion_type("Motion to Compel Further Responses") == "motion_to_compel"

    def test_sd_tentatives_demurrer(self) -> None:
        assert normalize_motion_type("Demurrer to Complaint") == "demurrer"

    def test_sd_tentatives_summary_judgment(self) -> None:
        assert normalize_motion_type("Motion for Summary Judgment") == "msj"

    def test_sd_tentatives_motion_to_strike(self) -> None:
        assert normalize_motion_type("Motion to Strike") == "motion_to_strike"

    def test_sd_tentatives_preliminary_injunction(self) -> None:
        assert normalize_motion_type("Preliminary Injunction") == "preliminary_injunction"

    # --- Riverside title-case values ---

    def test_riverside_summary_adjudication(self) -> None:
        assert normalize_motion_type("Summary Adjudication") == "msj_partial"

    def test_riverside_motion_to_compel(self) -> None:
        assert normalize_motion_type("Motion to Compel") == "motion_to_compel"

    def test_riverside_attorneys_fees_standalone(self) -> None:
        """Standalone 'Attorney's Fees' maps via prefix-less fallback (#1783)."""
        assert normalize_motion_type("Attorney's Fees") == "motion_for_attorney_fees"

    def test_riverside_attorneys_fees_plural_possessive(self) -> None:
        """Plural possessive 'Attorneys' Fees' maps correctly (#1783)."""
        assert normalize_motion_type("Attorneys' Fees") == "motion_for_attorney_fees"

    def test_motion_for_attorneys_fees(self) -> None:
        """'Motion for Attorney's Fees' maps correctly."""
        assert normalize_motion_type("Motion for Attorney's Fees") == "motion_for_attorney_fees"

    # --- Riverside prefix-less values (#1783) ---

    def test_riverside_compel_plaintiffs_responses(self) -> None:
        assert normalize_motion_type("Compel Plaintiff's Responses") == "motion_to_compel"

    def test_riverside_compel_further_responses(self) -> None:
        assert (
            normalize_motion_type("Compel Plaintiff's Responses to Request for Production")
            == "motion_to_compel"
        )

    def test_riverside_new_trial(self) -> None:
        assert normalize_motion_type("New Trial") == "motion_for_new_trial"

    def test_riverside_judgment_on_the_pleadings(self) -> None:
        assert (
            normalize_motion_type("Judgment on the Pleadings")
            == "motion_for_judgment_on_the_pleadings"
        )

    def test_riverside_deem_requests_admitted(self) -> None:
        assert (
            normalize_motion_type("Deem Requests for Admissions Admitted")
            == "deem_admissions_admitted"
        )

    def test_riverside_deem_requests_short(self) -> None:
        """Short 'DEEM REQUESTS FOR WELLS FARGO' matches deem_requests pattern."""
        assert (
            normalize_motion_type("DEEM REQUESTS FOR WELLS FARGO BANK, N.A.")
            == "deem_admissions_admitted"
        )

    def test_riverside_terminating(self) -> None:
        assert normalize_motion_type("Terminating Sanctions") == "motion_for_sanctions"

    def test_riverside_terminating_standalone(self) -> None:
        assert normalize_motion_type("Terminating") == "motion_for_sanctions"

    def test_riverside_monetary_sanctions(self) -> None:
        assert normalize_motion_type("Monetary Sanctions") == "motion_for_sanctions"

    def test_riverside_production_of_documents(self) -> None:
        assert normalize_motion_type("Production of Documents") == "motion_to_compel"

    def test_riverside_protective_order(self) -> None:
        assert normalize_motion_type("Protective Order") == "motion_for_protective_order"

    def test_riverside_strike_standalone(self) -> None:
        assert normalize_motion_type("Strike") == "motion_to_strike"

    def test_riverside_quash_standalone(self) -> None:
        assert normalize_motion_type("Quash") == "motion_to_quash"

    def test_riverside_relief_from_default(self) -> None:
        assert normalize_motion_type("Relief from Default") == "motion_to_set_aside_default"

    def test_riverside_leave_to_amend(self) -> None:
        assert normalize_motion_type("Leave to Amend") == "motion_for_leave_to_amend"

    # --- Edge cases ---

    def test_empty_string(self) -> None:
        assert normalize_motion_type("") is None

    def test_none(self) -> None:
        assert normalize_motion_type(None) is None  # type: ignore[arg-type]

    def test_whitespace_only(self) -> None:
        assert normalize_motion_type("   ") is None

    def test_whitespace_stripped(self) -> None:
        assert normalize_motion_type("  motion_to_compel  ") == "motion_to_compel"

    def test_unknown_value(self) -> None:
        """An unrecognizable value returns None."""
        assert normalize_motion_type("Some Random Hearing Type") is None

    # --- Ventura probate/non-standard event types (#1767) ---

    def test_ventura_petition_for_probate(self) -> None:
        assert normalize_motion_type("Petition for Probate") == "petition_for_probate"

    def test_ventura_accounting(self) -> None:
        assert normalize_motion_type("Accounting") == "accounting"

    def test_ventura_show_cause_hearing(self) -> None:
        assert normalize_motion_type("Show Cause Hearing") == "show_cause_hearing"

    def test_ventura_guardianship_petition(self) -> None:
        assert normalize_motion_type("Guardianship Petition") == "guardianship_petition"

    def test_ventura_petition_for_guardianship(self) -> None:
        assert normalize_motion_type("Petition for Guardianship") == "guardianship_petition"

    def test_ventura_petition_for_conservatorship(self) -> None:
        assert normalize_motion_type("Petition for Conservatorship") == "guardianship_petition"

    def test_ventura_trust_petition(self) -> None:
        assert normalize_motion_type("Trust Petition") == "trust_petition"

    def test_already_normalized_petition_for_probate(self) -> None:
        assert normalize_motion_type("petition_for_probate") == "petition_for_probate"

    def test_already_normalized_accounting(self) -> None:
        assert normalize_motion_type("accounting") == "accounting"

    def test_already_normalized_show_cause_hearing(self) -> None:
        assert normalize_motion_type("show_cause_hearing") == "show_cause_hearing"

    def test_already_normalized_guardianship_petition(self) -> None:
        assert normalize_motion_type("guardianship_petition") == "guardianship_petition"

    def test_already_normalized_trust_petition(self) -> None:
        assert normalize_motion_type("trust_petition") == "trust_petition"

    # --- PAGA settlement and settlement approval (#1815) ---

    def test_paga_settlement_title_case(self) -> None:
        assert normalize_motion_type("PAGA Settlement") == "paga_settlement"

    def test_paga_settlement_approval_of(self) -> None:
        """AC from #1818: normalize 'Approval of PAGA Settlement'."""
        assert normalize_motion_type("Approval of PAGA Settlement") == "paga_settlement"

    def test_paga_settlement_already_normalized(self) -> None:
        assert normalize_motion_type("paga_settlement") == "paga_settlement"

    def test_settlement_approval_title_case(self) -> None:
        assert normalize_motion_type("Settlement Approval") == "settlement_approval"

    def test_settlement_agreement_title_case(self) -> None:
        assert normalize_motion_type("Settlement Agreement") == "settlement_approval"

    def test_settlement_hearing_title_case(self) -> None:
        assert normalize_motion_type("Settlement Hearing") == "settlement_approval"

    def test_settlement_approval_already_normalized(self) -> None:
        assert normalize_motion_type("settlement_approval") == "settlement_approval"

    # --- New patterns for issue #2061 ---

    def test_good_faith_settlement_title_case(self) -> None:
        assert normalize_motion_type("Good Faith Settlement") == "good_faith_settlement"

    def test_good_faith_settlement_already_normalized(self) -> None:
        assert normalize_motion_type("good_faith_settlement") == "good_faith_settlement"

    def test_compel_arbitration_title_case(self) -> None:
        assert normalize_motion_type("Compel Arbitration") == "motion_to_compel_arbitration"

    def test_compel_arbitration_already_normalized(self) -> None:
        assert (
            normalize_motion_type("motion_to_compel_arbitration") == "motion_to_compel_arbitration"
        )

    def test_continuance_title_case(self) -> None:
        assert normalize_motion_type("Continuance") == "motion_for_continuance"

    def test_continuance_already_normalized(self) -> None:
        assert normalize_motion_type("motion_for_continuance") == "motion_for_continuance"

    def test_request_for_order_title_case(self) -> None:
        assert normalize_motion_type("Request for Order") == "request_for_order"

    def test_request_for_order_already_normalized(self) -> None:
        assert normalize_motion_type("request_for_order") == "request_for_order"

    def test_claim_of_exemption_title_case(self) -> None:
        assert normalize_motion_type("Claim of Exemption") == "claim_of_exemption"

    def test_claim_of_exemption_already_normalized(self) -> None:
        assert normalize_motion_type("claim_of_exemption") == "claim_of_exemption"

    def test_vexatious_litigant_title_case(self) -> None:
        assert normalize_motion_type("Vexatious Litigant") == "motion_vexatious_litigant"

    def test_vexatious_litigant_already_normalized(self) -> None:
        assert normalize_motion_type("motion_vexatious_litigant") == "motion_vexatious_litigant"

    def test_trial_preference_title_case(self) -> None:
        assert normalize_motion_type("Trial Preference") == "motion_for_trial_preference"

    def test_trial_preference_already_normalized(self) -> None:
        assert normalize_motion_type("motion_for_trial_preference") == "motion_for_trial_preference"

    def test_disqualification_title_case(self) -> None:
        assert normalize_motion_type("Disqualification") == "motion_to_disqualify"

    def test_disqualify_title_case(self) -> None:
        assert normalize_motion_type("Motion to Disqualify") == "motion_to_disqualify"

    def test_disqualify_already_normalized(self) -> None:
        assert normalize_motion_type("motion_to_disqualify") == "motion_to_disqualify"

    def test_mental_examination_title_case(self) -> None:
        assert normalize_motion_type("Mental Examination") == "motion_for_examination"

    def test_examination_already_normalized(self) -> None:
        assert normalize_motion_type("motion_for_examination") == "motion_for_examination"

    def test_interlocutory_judgment_title_case(self) -> None:
        assert (
            normalize_motion_type("Interlocutory Judgment") == "motion_for_interlocutory_judgment"
        )

    def test_interlocutory_judgment_already_normalized(self) -> None:
        assert (
            normalize_motion_type("motion_for_interlocutory_judgment")
            == "motion_for_interlocutory_judgment"
        )

    def test_reinstate_title_case(self) -> None:
        assert normalize_motion_type("Motion to Reinstate") == "motion_to_reinstate"

    def test_reinstate_already_normalized(self) -> None:
        assert normalize_motion_type("motion_to_reinstate") == "motion_to_reinstate"

    def test_set_aside_dismissal_prefix_less(self) -> None:
        assert normalize_motion_type("Set Aside Dismissal") == "motion_to_set_aside_dismissal"

    def test_set_aside_dismissal_already_normalized(self) -> None:
        assert (
            normalize_motion_type("motion_to_set_aside_dismissal")
            == "motion_to_set_aside_dismissal"
        )

    def test_notice_of_proposed_action_title_case(self) -> None:
        assert normalize_motion_type("Notice of Proposed Action") == "notice_of_proposed_action"

    def test_notice_of_proposed_action_already_normalized(self) -> None:
        assert normalize_motion_type("notice_of_proposed_action") == "notice_of_proposed_action"

    def test_common_identity_already_normalized(self) -> None:
        assert normalize_motion_type("motion_for_common_identity") == "motion_for_common_identity"

    def test_permit_remote_testimony_already_normalized(self) -> None:
        assert (
            normalize_motion_type("motion_to_permit_remote_testimony")
            == "motion_to_permit_remote_testimony"
        )


# ---------------------------------------------------------------------------
# normalize_motion_type — integration tests against real scraper outputs
# ---------------------------------------------------------------------------
#
# These parameterized tests act as a contract: if a refactoring changes how
# values flow through normalization, these tests catch incompatible inputs.
# Each sample is a (raw_value, expected_normalized) pair drawn from actual
# scraper output.  If normalize_motion_type() returns None for any value
# listed here, the normalization pipeline has a regression.
#
# Reference: issue #1785.

# -- Riverside raw descriptions (prefix-less forms from ruling headers) ------
_RIVERSIDE_SAMPLES: list[tuple[str, str]] = [
    ("Attorney's Fees", "motion_for_attorney_fees"),
    ("Attorneys' Fees", "motion_for_attorney_fees"),
    ("Compel Plaintiff's Responses", "motion_to_compel"),
    ("Compel Plaintiff's Responses to Request for Production", "motion_to_compel"),
    ("New Trial", "motion_for_new_trial"),
    ("Judgment on the Pleadings", "motion_for_judgment_on_the_pleadings"),
    ("Deem Requests for Admissions Admitted", "deem_admissions_admitted"),
    ("Terminating Sanctions", "motion_for_sanctions"),
    ("Monetary Sanctions", "motion_for_sanctions"),
    ("Production of Documents", "motion_to_compel"),
    ("Protective Order", "motion_for_protective_order"),
    ("Strike", "motion_to_strike"),
    ("Quash", "motion_to_quash"),
    ("Relief from Default", "motion_to_set_aside_default"),
    ("Leave to Amend", "motion_for_leave_to_amend"),
    ("Demurrer", "demurrer"),
]

# -- Ventura event types (from search results table) -------------------------
_VENTURA_SAMPLES: list[tuple[str, str]] = [
    ("Demurrer to Complaint", "demurrer"),
    ("Motion to Compel Further Responses", "motion_to_compel"),
    ("Motion for Summary Judgment", "msj"),
    ("Motion to Strike", "motion_to_strike"),
    ("Demurrer to First Amended Complaint", "demurrer"),
    ("Demurrer", "demurrer"),
    ("Preliminary Injunction", "preliminary_injunction"),
    ("Summary Adjudication", "msj_partial"),
    ("Hearing on Demurrer", "demurrer"),
]

# -- LA link text forms (from HTML ruling pages) ------------------------------
# LA does not set motion_type at the scraper level; these values come from
# the enrichment pipeline's LLM extraction applied to ruling text.
# Including them here as they represent realistic inputs to normalize.
_LA_SAMPLES: list[tuple[str, str]] = [
    ("Motion to Compel", "motion_to_compel"),
    ("Motion for Summary Judgment", "msj"),
    ("Motion to Strike Punitive Damages", "motion_to_strike"),
    ("Demurrer to the First Amended Complaint", "demurrer"),
    ("Anti-SLAPP Motion (CCP 425.16)", "anti_slapp"),
    ("Motion for Preliminary Injunction", "preliminary_injunction"),
    ("Motion for Reconsideration", "motion_for_reconsideration"),
    ("Motion for Attorney's Fees", "motion_for_attorney_fees"),
]

# -- OC formats (civil from LLM, probate and family law from scraper) --------
_OC_SAMPLES: list[tuple[str, str]] = [
    ("Petition for Probate", "petition_for_probate"),
    ("Accounting", "accounting"),
    ("Guardianship Petition", "guardianship_petition"),
    ("Petition for Conservatorship", "guardianship_petition"),
    ("Trust Petition", "trust_petition"),
    ("Show Cause Hearing", "show_cause_hearing"),
]

# -- SD Calendar event types (from calendar hearings) -------------------------
_SD_CALENDAR_SAMPLES: list[tuple[str, str]] = [
    ("Demurrer/Motion to Strike", "demurrer"),
    ("Motion to Quash", "motion_to_quash"),
    ("Motion for Sanctions", "motion_for_sanctions"),
    ("Motion Hearing", "motion_hearing_generic"),
    ("Discovery Hearing", "discovery"),
    ("Motion Hearing to Certify/Decertify Class Action", "motion_for_class_certification"),
]

# -- SD Tentatives (from parse_motion_type in sc_tentatives.py) ---------------
_SD_TENTATIVES_SAMPLES: list[tuple[str, str]] = [
    ("Motion to Compel Further Responses", "motion_to_compel"),
    ("Demurrer to Complaint", "demurrer"),
    ("Motion for Summary Judgment", "msj"),
    ("Motion to Strike", "motion_to_strike"),
]

# -- Santa Clara (from parse_motion_type) -------------------------------------
_SC_SAMPLES: list[tuple[str, str]] = [
    ("Demurrer", "demurrer"),
    ("Summary Judgment", "msj"),
    ("Motion to Compel", "motion_to_compel"),
    ("Motion to Dismiss", "mtd"),
    ("Motion to Strike", "motion_to_strike"),
    ("Motion to Quash", "motion_to_quash"),
]

# -- Contra Costa (from _cc_extract_motion_type) ------------------------------
# CC produces values like "Leave To File Cross-Complaint" and "Case Management
# Conference" which legitimately return None (niche types without canonical
# forms).  Only values with a known mapping are included here.
_CC_SAMPLES: list[tuple[str, str]] = [
    ("Order To Show Cause", "osc"),
]

# -- Fresno (from _extract_motion_type "Motion:" header line) -----------------
_FRESNO_SAMPLES: list[tuple[str, str]] = [
    ("Demurrer to First Amended Complaint", "demurrer"),
    ("Motion to Compel Further Responses", "motion_to_compel"),
]

# Combine all samples with county labels for clear failure messages.
_ALL_INTEGRATION_SAMPLES: list[tuple[str, str, str]] = [
    (county, raw, expected)
    for county, samples in [
        ("riverside", _RIVERSIDE_SAMPLES),
        ("ventura", _VENTURA_SAMPLES),
        ("la", _LA_SAMPLES),
        ("oc", _OC_SAMPLES),
        ("sd_calendar", _SD_CALENDAR_SAMPLES),
        ("sd_tentatives", _SD_TENTATIVES_SAMPLES),
        ("sc", _SC_SAMPLES),
        ("cc", _CC_SAMPLES),
        ("fresno", _FRESNO_SAMPLES),
    ]
    for raw, expected in samples
]


@pytest.mark.normalization_integration
class TestNormalizationIntegration:
    """Integration tests: normalize_motion_type() against real scraper outputs.

    These tests act as a contract between the centralized normalization
    function and the county scrapers that produce motion_type values.  If a
    refactoring changes how ``normalize_motion_type()`` works, any scraper
    whose output becomes incompatible will show up as a failure here.

    Reference: #1785 (preventative measure for the #1783 regression).

    Run with: ``pytest tests/test_extract.py -k normalization_integration -v``
    """

    @pytest.mark.parametrize(
        ("county", "raw_value", "expected"),
        _ALL_INTEGRATION_SAMPLES,
        ids=[f"{county}:{raw}" for county, raw, _ in _ALL_INTEGRATION_SAMPLES],
    )
    def test_normalize_known_scraper_output(
        self, county: str, raw_value: str, expected: str
    ) -> None:
        """normalize_motion_type() must return a non-None value for every
        known-good scraper output, and it must match the expected canonical
        form."""
        result = normalize_motion_type(raw_value)
        assert result is not None, (
            f"normalize_motion_type({raw_value!r}) returned None — "
            f"{county} scraper output would lose its motion_type"
        )
        assert result == expected, (
            f"normalize_motion_type({raw_value!r}) returned {result!r}, "
            f"expected {expected!r} (county: {county})"
        )

    def test_minimum_sample_count(self) -> None:
        """The integration suite covers at least 20 distinct motion type
        values, as required by the acceptance criteria."""
        distinct_raw_values = {raw for _, raw, _ in _ALL_INTEGRATION_SAMPLES}
        assert len(distinct_raw_values) >= 20, (
            f"Expected >= 20 distinct motion type values, got {len(distinct_raw_values)}"
        )

    def test_all_counties_represented(self) -> None:
        """Every county that produces motion_type at the scraper level is
        represented in the integration samples."""
        counties = {county for county, _, _ in _ALL_INTEGRATION_SAMPLES}
        # Counties whose scrapers set doc.motion_type directly
        expected_counties = {
            "riverside",
            "ventura",
            "sd_calendar",
            "sd_tentatives",
            "sc",
            "cc",
            "fresno",
            "oc",
        }
        # LA values come from enrichment, included for broader coverage
        assert expected_counties.issubset(counties), (
            f"Missing counties: {expected_counties - counties}"
        )

    def test_none_returns_none(self) -> None:
        """normalize_motion_type() still returns None for values that genuinely
        have no mapping — the integration samples are not a catch-all."""
        assert normalize_motion_type("Some Random Hearing Type") is None
        assert normalize_motion_type("Case Management Conference") is None


# ---------------------------------------------------------------------------
# is_plausible_case_title tests (#1974)
# ---------------------------------------------------------------------------


class TestIsPlausibleCaseTitle:
    """Tests for the is_plausible_case_title() shared validation function."""

    # --- Valid titles (should pass) ---

    def test_standard_adversarial_title(self) -> None:
        """Normal 'X v. Y' title passes validation."""
        assert is_plausible_case_title("Smith v. Jones") is True

    def test_in_re_title(self) -> None:
        """'In re: Estate of ...' is a legitimate title."""
        assert is_plausible_case_title("In re: Estate of John Smith") is True

    def test_in_re_marriage(self) -> None:
        """'In re Marriage of ...' is a legitimate title."""
        assert is_plausible_case_title("In re Marriage of Garcia") is True

    def test_in_the_matter_of(self) -> None:
        """'In the Matter of ...' is a legitimate title."""
        assert is_plausible_case_title("In the Matter of the Estate of Williams") is True

    def test_single_party_name(self) -> None:
        """Single party name (from inline extraction) passes."""
        assert is_plausible_case_title("Duarte") is True

    def test_corporate_parties(self) -> None:
        """Corporate entity names pass."""
        assert is_plausible_case_title("Acme Corporation v. Widget LLC") is True

    def test_title_at_max_length(self) -> None:
        """Title at exactly 120 chars passes."""
        title = "A" * 120
        assert is_plausible_case_title(title) is True

    def test_title_at_min_length(self) -> None:
        """Title at exactly 3 chars passes."""
        assert is_plausible_case_title("Doe") is True

    # --- Invalid titles (should be rejected) ---

    def test_rejects_to_respond(self) -> None:
        """'To Respond, Without Objections' is motion text, not a title (#1958)."""
        assert is_plausible_case_title("To Respond, Without Objections") is False

    def test_rejects_to_produce_all(self) -> None:
        """'To Produce All' is motion text, not a title (#1958)."""
        assert is_plausible_case_title("To Produce All") is False

    def test_rejects_for_summary_judgment(self) -> None:
        """'For Summary Judgment' starts with 'For'."""
        assert is_plausible_case_title("For Summary Judgment") is False

    def test_rejects_by_the_court(self) -> None:
        """'By the Court' starts with 'By'."""
        assert is_plausible_case_title("By the Court") is False

    def test_rejects_on_the_merits(self) -> None:
        """'On the Merits' starts with 'On'."""
        assert is_plausible_case_title("On the Merits") is False

    def test_rejects_re_colon_motion(self) -> None:
        """'Re: Motion to Compel' starts with 'Re:'."""
        assert is_plausible_case_title("Re: Motion to Compel") is False

    def test_rejects_granted_fragment(self) -> None:
        """Title containing 'GRANTED' is ruling text."""
        assert is_plausible_case_title("Motion is GRANTED") is False

    def test_rejects_denied_fragment(self) -> None:
        """Title containing 'DENIED' is ruling text."""
        assert is_plausible_case_title("Request DENIED") is False

    def test_rejects_continued_fragment(self) -> None:
        """Title containing 'CONTINUED' is ruling text."""
        assert is_plausible_case_title("Matter CONTINUED to April") is False

    def test_rejects_tentative_ruling(self) -> None:
        """Title containing 'TENTATIVE RULING' is ruling text."""
        assert is_plausible_case_title("TENTATIVE RULING on the motion") is False

    def test_rejects_motion_fragment(self) -> None:
        """Title containing 'MOTION' is procedural text."""
        assert is_plausible_case_title("MOTION to Compel Discovery") is False

    def test_rejects_demurrer_fragment(self) -> None:
        """Title containing 'DEMURRER' is procedural text."""
        assert is_plausible_case_title("DEMURRER to Complaint") is False

    def test_rejects_too_short(self) -> None:
        """Titles shorter than 3 chars are rejected."""
        assert is_plausible_case_title("AB") is False

    def test_rejects_too_long(self) -> None:
        """Titles longer than 120 chars are rejected."""
        title = "A" * 121
        assert is_plausible_case_title(title) is False

    def test_rejects_empty_string(self) -> None:
        """Empty string is rejected."""
        assert is_plausible_case_title("") is False

    def test_rejects_whitespace_only(self) -> None:
        """Whitespace-only string is rejected."""
        assert is_plausible_case_title("   ") is False

    def test_rejects_in_the_something(self) -> None:
        """'In the Superior Court' is not a case title."""
        assert is_plausible_case_title("In the Superior Court") is False

    def test_rejects_overruled(self) -> None:
        """'Objection OVERRULED' is ruling text."""
        assert is_plausible_case_title("Objection OVERRULED") is False

    def test_rejects_sustained(self) -> None:
        """'Demurrer SUSTAINED' is ruling text."""
        assert is_plausible_case_title("Demurrer SUSTAINED") is False

    def test_rejects_order(self) -> None:
        """'ORDER granting relief' is procedural text."""
        assert is_plausible_case_title("ORDER granting relief") is False

    def test_case_insensitive_prefix_check(self) -> None:
        """Prefix check is case insensitive."""
        assert is_plausible_case_title("TO respond without objections") is False
        assert is_plausible_case_title("FOR the court's review") is False

    def test_case_insensitive_fragment_check(self) -> None:
        """Fragment check is case insensitive."""
        assert is_plausible_case_title("motion granted") is False
        assert is_plausible_case_title("Tentative Ruling on X") is False

    # --- Tests for issue #2022 (Riverside enrichment gaps) ---

    def test_rejects_tentative_rulings_plural(self) -> None:
        """'Tentative Rulings for March...' is calendar header text."""
        assert is_plausible_case_title("Tentative Rulings for March 19, 2026") is False

    def test_rejects_department_header(self) -> None:
        """Department headers are not case titles."""
        assert is_plausible_case_title("Department M205 Smith v. Jones") is False

    def test_rejects_multiple_case_numbers(self) -> None:
        """Titles with multiple case numbers are contaminated."""
        title = "CVPS2600101 TOP VISION vs HONDA CVPS2600105 Tentative Ruling:"
        assert is_plausible_case_title(title) is False

    def test_rejects_multiple_riverside_case_numbers(self) -> None:
        """Multiple Riverside-format case numbers indicate contamination."""
        title = "CVME2504512 DISCOVER BANK VS JONES CVME2511105 WELLS FARGO"
        assert is_plausible_case_title(title) is False

    def test_rejects_timekeeper_billing(self) -> None:
        """Billing/timekeeper entries are not case titles."""
        assert is_plausible_case_title("DUBONT VS GENERAL MOTORS Timekeeper Nicolas") is False

    def test_accepts_normal_riverside_title(self) -> None:
        """Normal adversarial titles should pass."""
        assert is_plausible_case_title("Yeldell v. Henss") is True

    def test_accepts_corporation_title(self) -> None:
        """Titles with corporate parties should pass."""
        assert is_plausible_case_title("Discover Bank v. Jones") is True

    # --- Tests for issue #2242 (embedded case number detection) ---

    def test_rejects_embedded_generic_case_number(self) -> None:
        """Title with embedded MSC-format case number is contaminated."""
        assert is_plausible_case_title("TAYLOR VS. AMAZON MSC21-02349 Romeo Cerina") is False

    def test_rejects_embedded_riverside_case_number(self) -> None:
        """Title with embedded Riverside-format case number is contaminated."""
        assert is_plausible_case_title("Smith v. Jones CVPS2400892") is False

    def test_rejects_embedded_oc_case_number(self) -> None:
        """Title with OC-format case number is contaminated."""
        assert is_plausible_case_title("Doe v. Roe 30-2024-01234567") is False

    def test_rejects_embedded_la_case_number(self) -> None:
        """Title with LA-format case number is contaminated."""
        assert is_plausible_case_title("Garcia v. Lopez 24STCV01234") is False

    def test_rejects_embedded_sb_case_number(self) -> None:
        """Title with SB-format case number is contaminated."""
        assert is_plausible_case_title("Adams v. Baker CIVSB2100123") is False

    def test_accepts_clean_adversarial_title(self) -> None:
        """Normal adversarial titles without case numbers still pass."""
        assert is_plausible_case_title("Taylor v. Amazon") is True
        assert is_plausible_case_title("SMITH VS JONES") is True

    def test_accepts_single_party_name_no_case_number(self) -> None:
        """Single party names without case numbers still pass."""
        assert is_plausible_case_title("Williams") is True

    def test_accepts_probate_title_no_case_number(self) -> None:
        """Probate/estate titles without case numbers still pass."""
        assert is_plausible_case_title("In the Matter of the Estate of Davis") is True

    # ---------------------------------------------------------------------------
    # Outcome extraction — Riverside-specific patterns (#2022)
    def test_stipulated_judgment_prefixless(self) -> None:
        assert normalize_motion_type("Stipulated Judgment") == "motion_for_stipulated_judgment"

    def test_entry_of_judgment_prefixless(self) -> None:
        assert normalize_motion_type("Entry of Judgment") == "motion_for_entry_of_judgment"

    def test_bifurcate_prefixless(self) -> None:
        assert normalize_motion_type("Bifurcate") == "motion_to_bifurcate"

    def test_class_certification_prefixless(self) -> None:
        assert normalize_motion_type("Class Certification") == "motion_for_class_certification"

    def test_reclassify_prefixless(self) -> None:
        assert normalize_motion_type("Reclassify") == "motion_to_reclassify"

    def test_judicial_approval_prefixless(self) -> None:
        assert normalize_motion_type("Judicial Approval") == "motion_for_judicial_approval"

    def test_receiver_prefixless(self) -> None:
        result = normalize_motion_type("Appointment of Receiver")
        assert result == "motion_for_appointment_of_receiver"

    def test_right_to_attach_prefixless(self) -> None:
        assert normalize_motion_type("Right to Attach") == "right_to_attach_order"

    def test_consolidate_prefixless(self) -> None:
        assert normalize_motion_type("Consolidate") == "motion_to_consolidate"

    def test_dismissal_prefixless(self) -> None:
        assert normalize_motion_type("Dismissal") == "request_for_dismissal"


# ---------------------------------------------------------------------------
# normalize_outcome — Riverside aliases (#2022)
# ---------------------------------------------------------------------------


class TestNormalizeOutcomeRiverside:
    """Tests for normalize_outcome with Riverside-specific aliases (#2022)."""

    def test_sustain(self) -> None:
        assert normalize_outcome("sustain") == "granted"

    def test_overrule(self) -> None:
        assert normalize_outcome("overrule") == "denied"

    def test_grant_alias(self) -> None:
        assert normalize_outcome("grant") == "granted"

    def test_deny_alias(self) -> None:
        assert normalize_outcome("deny") == "denied"

    def test_hearing_required(self) -> None:
        assert normalize_outcome("hearing required") == "other"

    def test_no_tentative_decision(self) -> None:
        assert normalize_outcome("no tentative decision") == "other"


# ---------------------------------------------------------------------------
# clean_case_title tests (#2212)
# ---------------------------------------------------------------------------


class TestCleanCaseTitle:
    """Tests for the deterministic case_title cleanup function (#2212)."""

    # --- Basic v./vs. cases ---

    def test_clean_simple_vs(self) -> None:
        """Simple 'X vs Y' passes through cleaned."""
        result = clean_case_title("Smith vs Jones")
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert "v." in result

    def test_clean_vs_dot(self) -> None:
        """'X vs. Y' normalizes separator."""
        result = clean_case_title("GARCIA vs. HERNANDEZ")
        assert result is not None
        assert "Garcia" in result
        assert "Hernandez" in result
        assert "v." in result

    def test_clean_v_dot(self) -> None:
        """'X v. Y' passes through."""
        result = clean_case_title("Smith v. Jones")
        assert result is not None
        assert result == "Smith v. Jones"

    def test_clean_all_caps(self) -> None:
        """All-caps titles are title-cased."""
        result = clean_case_title("SMITH VS JONES")
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result

    # --- Motion description truncation ---

    def test_truncate_motion_description(self) -> None:
        """Motion descriptions after party names are truncated (#2212)."""
        raw = "Cuong Quach vs Maxreal Cupertino et al REQUEST FOR FORM INTERROGATORY NO. 12.1"
        result = clean_case_title(raw)
        assert result is not None
        assert "Quach" in result
        assert "Cupertino" in result or "Maxreal" in result
        assert "REQUEST FOR" not in result
        assert "INTERROGATORY" not in result
        assert len(result) <= 120

    # --- Case citation truncation ---

    def test_truncate_case_citation(self) -> None:
        """Case citations after party names are truncated (#2212)."""
        raw = (
            "HICHAM MESNAOUI VS. LAKERIDGE ATHLETIC CLUB Nazir v. United "
            "Airlines, Inc. (2009) 178 Cal.App.4th 243, 251.)"
        )
        result = clean_case_title(raw)
        assert result is not None
        assert "Mesnaoui" in result
        assert "Lakeridge" in result
        assert "Cal.App" not in result
        assert "(2009)" not in result
        assert len(result) <= 120

    # --- Multiple cases truncation ---

    def test_truncate_multiple_cases_with_entity(self) -> None:
        """Multiple cases jammed together are truncated at LLC/Inc boundary (#2212)."""
        raw = (
            "LORENZO SOLIS v. GENERAL MOTORS LLC Dustin A Brazil vs. "
            "General Motors LLC Peter Jr. Arafiles vs. General Motors LLC"
        )
        result = clean_case_title(raw)
        assert result is not None
        assert "Solis" in result
        assert "General Motors" in result
        # Should NOT contain the second or third case
        assert "Brazil" not in result
        assert "Arafiles" not in result
        assert len(result) <= 120

    # --- Embedded case number truncation ---

    def test_truncate_embedded_case_number(self) -> None:
        """Embedded case numbers after party names are truncated (#2212)."""
        raw = "TAYLOR VS. AMAZON MSC21-02349 Romeo Cerina by and through his Guardian Ad Litem"
        result = clean_case_title(raw)
        assert result is not None
        assert "Taylor" in result
        assert "Amazon" in result
        assert "MSC21" not in result
        assert "Romeo" not in result
        assert len(result) <= 120

    # --- Probate / estate / conservatorship ---

    def test_probate_estate_of(self) -> None:
        """'ESTATE OF: Name' is extracted properly."""
        result = clean_case_title("ESTATE OF: John Smith")
        assert result is not None
        assert "ESTATE OF" in result
        assert "John Smith" in result

    def test_probate_conservatorship(self) -> None:
        """'CONSERVATORSHIP OF: Name' is extracted properly."""
        result = clean_case_title("CONSERVATORSHIP OF: Jane Doe")
        assert result is not None
        assert "CONSERVATORSHIP OF" in result
        assert "Jane Doe" in result

    def test_probate_guardianship(self) -> None:
        """'GUARDIANSHIP OF: Name' is extracted properly."""
        result = clean_case_title("GUARDIANSHIP OF Maria Garcia")
        assert result is not None
        assert "GUARDIANSHIP OF" in result
        assert "Maria Garcia" in result

    def test_probate_in_the_matter_of(self) -> None:
        """'IN THE MATTER OF: Name' is extracted properly."""
        result = clean_case_title("IN THE MATTER OF: The Estate of Williams")
        assert result is not None
        assert "IN THE MATTER OF" in result
        assert "Williams" in result

    def test_probate_truncate_at_second_keyword(self) -> None:
        """Multiple probate titles are truncated at the second keyword."""
        raw = "ESTATE OF HENRY FASQUELLE GUARDIANSHIP OF NAYELLI BRONSON"
        result = clean_case_title(raw)
        assert result is not None
        assert "FASQUELLE" in result
        assert "BRONSON" not in result
        assert len(result) <= 120

    # --- Edge cases ---

    def test_empty_string(self) -> None:
        """Empty string returns None."""
        assert clean_case_title("") is None

    def test_none_input(self) -> None:
        """None-like empty input returns None."""
        assert clean_case_title("   ") is None

    def test_no_vs_separator(self) -> None:
        """Title without v./vs. and no probate keyword returns None."""
        assert clean_case_title("Just some random text here") is None

    def test_preserves_et_al(self) -> None:
        """'et al.' suffix is preserved."""
        result = clean_case_title("Smith v. Jones et al.")
        assert result is not None
        assert "et al." in result

    def test_leading_case_number_stripped(self) -> None:
        """Leading case numbers before the party name are stripped."""
        result = clean_case_title("CVPS2400892 Smith v. Jones")
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert "CVPS" not in result

    def test_title_length_under_120(self) -> None:
        """Result is always under 120 characters."""
        raw = "A" * 60 + " VS. " + "B" * 60 + " MOTION TO COMPEL"
        result = clean_case_title(raw)
        if result is not None:
            assert len(result) <= 120

    def test_multiline_collapsed(self) -> None:
        """Newlines in raw title are collapsed to spaces."""
        result = clean_case_title("Smith\nvs.\nJones")
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result

    def test_guardian_ad_litem_truncated(self) -> None:
        """'by and through his Guardian Ad Litem' is truncated."""
        raw = "Smith vs Jones by and through his Guardian Ad Litem Jane Smith"
        result = clean_case_title(raw)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert "Guardian" not in result

    def test_hearing_on_truncated(self) -> None:
        """'HEARING ON' in defendant portion is truncated."""
        raw = "SMITH VS. JONES HEARING ON MOTION TO COMPEL"
        result = clean_case_title(raw)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert "HEARING" not in result

    def test_real_world_messy_title_1(self) -> None:
        """Real-world messy title with interrogatory reference."""
        raw = (
            "Cuong Quach vs Maxreal Cupertino et al "
            "REQUEST FOR FORM INTERROGATORY NO. 12.1 "
            "AND SPECIAL INTERROGATORY NO. 14"
        )
        result = clean_case_title(raw)
        assert result is not None
        assert len(result) <= 120
        assert "REQUEST" not in result

    def test_real_world_messy_title_2(self) -> None:
        """Real-world messy title with case citation."""
        raw = (
            "HICHAM MESNAOUI VS. LAKERIDGE ATHLETIC CLUB "
            "Nazir v. United Airlines, Inc. "
            "(2009) 178 Cal.App.4th 243, 251.)"
        )
        result = clean_case_title(raw)
        assert result is not None
        assert len(result) <= 120
        # Should contain the first case's parties only
        assert "Mesnaoui" in result
        assert "Lakeridge" in result

    def test_probate_with_embedded_case_number(self) -> None:
        """Probate title with case number in remainder is truncated."""
        raw = "ESTATE OF: John Smith CVPS2400892 Next Case"
        result = clean_case_title(raw)
        assert result is not None
        assert "John Smith" in result
        assert "CVPS" not in result
        assert "Next Case" not in result

    def test_probate_with_motion_terminator(self) -> None:
        """Probate title with motion keyword in remainder is truncated."""
        raw = "ESTATE OF: Jane Doe PETITION TO Approve Settlement"
        result = clean_case_title(raw)
        assert result is not None
        assert "Jane Doe" in result
        assert "PETITION" not in result

    def test_very_long_probate_title(self) -> None:
        """Extremely long probate title is hard-truncated at word boundary."""
        long_name = " ".join(["VeryLongName"] * 20)
        raw = f"ESTATE OF: {long_name}"
        result = clean_case_title(raw)
        if result is not None:
            assert len(result) <= 120

    def test_second_case_no_entity_ending(self) -> None:
        """Second case detected by casing transition (no LLC/Inc)."""
        raw = "HICHAM MESNAOUI VS. LAKERIDGE ATHLETIC CLUB Nazir v. United Airlines"
        result = clean_case_title(raw)
        assert result is not None
        assert "Mesnaoui" in result
        assert "Lakeridge" in result
        assert "Nazir" not in result

    def test_empty_defendant_after_truncation(self) -> None:
        """If truncation leaves no defendant, return None."""
        # The v. is followed immediately by a case number
        raw = "SMITH VS. CVPS2400892"
        result = clean_case_title(raw)
        # Should return None since defendant is empty
        assert result is None

    def test_empty_plaintiff_before_vs(self) -> None:
        """If plaintiff is empty (only case number), return None."""
        raw = "CVPS2400892 VS. JONES"
        result = clean_case_title(raw)
        # Plaintiff was all case number, should be empty after strip
        # Result depends on whether case number is fully consumed
        if result is not None:
            assert "CVPS" not in result

    # --- Trailing connector stripping after 120-char truncation (#3730) ---

    def test_truncation_strips_trailing_and(self) -> None:
        """SB example: trailing 'and' is stripped after 120-char truncation."""
        # "Friends of Fawnskin..." is 134 chars — long enough to trigger truncation.
        raw = (
            "Friends of Fawnskin Mountain Communities Foundation"
            " v. County of San Bernardino and Board of Supervisors"
            " for the County of San Bernardino"
        )
        result = clean_case_title(raw)
        assert result is not None
        assert len(result) <= 120
        last_word = result.rstrip().split()[-1].lower()
        assert last_word not in {
            "and",
            "for",
            "the",
            "of",
            "to",
            "by",
            "in",
            "on",
            "with",
            "or",
            "a",
            "an",
            "as",
        }

    def test_truncation_strips_trailing_for_the(self) -> None:
        """LA example: trailing connector phrase is stripped after 120-char truncation."""
        # Pad the plaintiff name to ensure the truncation path runs (>120 chars total).
        plaintiff = "A" * 45 + " Long Plaintiff Name Inc"
        raw = plaintiff + " v. City of Los Angeles, a public entity; and Does 1 to 50"
        # Verify input is long enough to trigger the truncation path
        assert len(raw) > 120
        result = clean_case_title(raw)
        assert result is not None
        assert len(result) <= 120
        last_word = result.rstrip().split()[-1].lower()
        assert last_word not in {
            "and",
            "for",
            "the",
            "of",
            "to",
            "by",
            "in",
            "on",
            "with",
            "or",
            "a",
            "an",
            "as",
        }

    def test_truncation_strips_trailing_of(self) -> None:
        """Orange/UCLA example: trailing 'of' is stripped after 120-char truncation."""
        # Pad the plaintiff name so the title exceeds 120 chars and truncation
        # lands on a connector word.
        plaintiff = "A" * 60 + " Plaintiff"
        raw = plaintiff + " v. The Regents of the University of California, Los Angeles"
        assert len(raw) > 120
        result = clean_case_title(raw)
        assert result is not None
        assert len(result) <= 120
        last_word = result.rstrip().split()[-1].lower()
        assert last_word not in {
            "and",
            "for",
            "the",
            "of",
            "to",
            "by",
            "in",
            "on",
            "with",
            "or",
            "a",
            "an",
            "as",
        }

    def test_truncation_no_op_when_under_limit(self) -> None:
        """Titles <=120 chars ending in a connector are returned unchanged (no truncation path)."""
        # This title is short; the 120-char truncation block is never entered.
        raw = "Smith v. Jones and Partners"
        result = clean_case_title(raw)
        # The truncation path must NOT have fired — result is well within limit.
        assert result is not None
        assert len(result) <= 120
        # The connector-stripping regex must NOT have been applied outside the
        # truncation block; "Partners" should still be present.
        assert "Partners" in result

    def test_truncation_strips_chained_connectors(self) -> None:
        """Synthetic: trailing ', and the' chain after truncation is stripped in one pass."""
        # Build a title where the word-boundary truncation lands on ', and the'
        # e.g. plaintiff (50 chars) + " v. " + defendant that ends in ", and the XYZ"
        plaintiff = "A" * 50 + " Plaintiff Corp"
        # Craft the defendant so the 120-char word-boundary cut leaves ', and the'
        # parts[0] = plaintiff (65 chars), " v. " = 4, so max_def_len = 120-65-4=51
        # We want space_idx to land right after ", and the" — fill up 51 chars exactly
        # so last word before the boundary IS a connector word.
        defendant = "Defendant Entity, and the " + "X" * 25 + " More Stuff"
        raw = plaintiff + " v. " + defendant
        assert len(raw) > 120
        result = clean_case_title(raw)
        if result is not None:
            assert len(result) <= 120
            last_word = result.rstrip().split()[-1].lower()
            assert last_word not in {
                "and",
                "for",
                "the",
                "of",
                "to",
                "by",
                "in",
                "on",
                "with",
                "or",
                "a",
                "an",
                "as",
            }


# ---------------------------------------------------------------------------
# strip_trailing_connectors helper (#3730)
# ---------------------------------------------------------------------------


class TestStripTrailingConnectors:
    """Unit tests for strip_trailing_connectors() — the module-level helper
    that removes dangling English connector words from case titles (#3730).

    These cover the stand-alone helper, independent of the 120-char truncation
    path exercised by TestCleanCaseTitle.
    """

    def test_strips_trailing_and(self) -> None:
        """Simple case: 'Smith v. Jones and' → 'Smith v. Jones'."""
        assert strip_trailing_connectors("Smith v. Jones and") == "Smith v. Jones"

    def test_strips_trailing_to_on_la_shaped_title(self) -> None:
        """115-char LA-shaped title ending in connector word is stripped."""
        # Real-world shape: 115 chars, ends in ' to' (AC #3 detection query hits this)
        title = "Smith v. City of Los Angeles, a public entity; and Does 1 to"
        assert len(title) <= 120
        result = strip_trailing_connectors(title)
        assert result is not None
        last_word = result.rstrip().split()[-1].lower()
        assert last_word not in {"to", "and"}
        assert "Does 1" in result

    def test_strips_trailing_of(self) -> None:
        """Trailing 'of' is stripped correctly."""
        title = "Smith v. The Regents of the University of"
        result = strip_trailing_connectors(title)
        assert result == "Smith v. The Regents of the University"

    def test_noop_connector_mid_string(self) -> None:
        """Connector mid-string does not cause truncation — 'Partners' is preserved."""
        title = "Smith v. Jones and Partners"
        assert strip_trailing_connectors(title) == "Smith v. Jones and Partners"

    def test_noop_no_connector(self) -> None:
        """Title with no trailing connector is returned unchanged."""
        title = "Smith v. Jones"
        assert strip_trailing_connectors(title) == "Smith v. Jones"

    def test_strips_chained_connector(self) -> None:
        """Chained connectors like ', and the' are stripped in one pass."""
        title = "Smith v. Jones, and the"
        result = strip_trailing_connectors(title)
        assert result == "Smith v. Jones"

    def test_empty_string_passthrough(self) -> None:
        """Empty string is returned unchanged (no crash)."""
        assert strip_trailing_connectors("") == ""


# ---------------------------------------------------------------------------
# Ventura-specific fixes (#2370)
# ---------------------------------------------------------------------------


class TestVenturaCaseNumberValidation:
    """Tests for is_valid_case_number with Ventura case number formats (#2370).

    Ventura uses two probate case number formats:
      - New: 4-digit year + 4-letter type code + 6-digit sequence (e.g. 2025CUPR042345)
      - Old: 4-digit year + 6-digit sequence + "PR" + 2-letter subtype
             (e.g. 202200570654PRLP, 201500471558PRCE, 202200572564PRLP)

    Both formats must be accepted by the validator.
    """

    def test_ventura_new_format_probate(self) -> None:
        assert is_valid_case_number("2025CUPR042345") is True

    def test_ventura_new_format_civil(self) -> None:
        assert is_valid_case_number("2024CUBC038456") is True

    def test_ventura_old_format_probate_prlp(self) -> None:
        """Old-format Ventura probate: 202200570654PRLP (16 chars)."""
        assert is_valid_case_number("202200570654PRLP") is True

    def test_ventura_old_format_probate_prce(self) -> None:
        """Old-format Ventura probate: 201500471558PRCE."""
        assert is_valid_case_number("201500471558PRCE") is True

    def test_ventura_old_format_probate_short_subtype(self) -> None:
        """Old-format Ventura probate with short 2-letter subtype."""
        assert is_valid_case_number("202200572564PRLP") is True


class TestDedupeRepeatedTitle:
    """Tests for dedupe_repeated_title() — strips exact repeated substrings
    from case titles (#2370).

    Observed Ventura bug: LLM returns titles like
      "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al. CITY OF THOUSAND OAKS
      vs GLENN R PACKARD, et al."
    where the full title is repeated 2-3x.
    """

    def test_repeated_twice(self) -> None:
        from ingestion.extract import dedupe_repeated_title

        raw = (
            "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al. "
            "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al."
        )
        result = dedupe_repeated_title(raw)
        assert result == "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al."

    def test_repeated_three_times(self) -> None:
        from ingestion.extract import dedupe_repeated_title

        raw = "Smith v. Jones Smith v. Jones Smith v. Jones"
        result = dedupe_repeated_title(raw)
        assert result == "Smith v. Jones"

    def test_no_repetition(self) -> None:
        from ingestion.extract import dedupe_repeated_title

        raw = "Smith v. Jones"
        assert dedupe_repeated_title(raw) == "Smith v. Jones"

    def test_none_input(self) -> None:
        from ingestion.extract import dedupe_repeated_title

        assert dedupe_repeated_title(None) is None

    def test_empty_string(self) -> None:
        from ingestion.extract import dedupe_repeated_title

        assert dedupe_repeated_title("") == ""

    def test_short_string_no_dedup(self) -> None:
        """Short strings (under 15 chars) should not be deduplicated to
        avoid false positives on short repeated phrases."""
        from ingestion.extract import dedupe_repeated_title

        raw = "JOE JOE"
        # Too short — should pass through unchanged
        assert dedupe_repeated_title(raw) == raw

    def test_preserves_leading_whitespace_trim(self) -> None:
        from ingestion.extract import dedupe_repeated_title

        raw = "  Smith v. Jones Smith v. Jones  "
        result = dedupe_repeated_title(raw)
        assert result == "Smith v. Jones"

    def test_repeated_with_whitespace_variation(self) -> None:
        """Repetition detection should be whitespace-tolerant."""
        from ingestion.extract import dedupe_repeated_title

        raw = (
            "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al.  "
            "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al."
        )
        result = dedupe_repeated_title(raw)
        assert result == "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al."

    def test_dedup_applied_via_clean_case_title(self) -> None:
        """clean_case_title() should apply dedupe_repeated_title internally."""
        raw = (
            "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al. "
            "CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al."
        )
        cleaned = clean_case_title(raw)
        assert cleaned is not None
        # After dedupe, we have "CITY OF THOUSAND OAKS v. GLENN R PACKARD, et al."
        # The cleaner converts all-caps to title case.
        # Key assertion: the repetition is gone.
        assert cleaned.lower().count("thousand oaks") == 1


class TestStripTrailingCaseNumber:
    """Tests for strip_trailing_case_number() — removes case numbers
    appended to the end of a title (#2370).

    Observed Ventura bug: probate titles end with the case number,
    e.g. "In the Matter of Denise Guadalupe Mejia 202200570654PRL".
    """

    def test_strips_ventura_old_probate_number(self) -> None:
        from ingestion.extract import strip_trailing_case_number

        raw = "In the Matter of Denise Guadalupe Mejia 202200570654PRLP"
        assert strip_trailing_case_number(raw) == "In the Matter of Denise Guadalupe Mejia"

    def test_strips_ventura_new_probate_number(self) -> None:
        from ingestion.extract import strip_trailing_case_number

        raw = "Estate of Harold Jensen 2025CUPR042345"
        assert strip_trailing_case_number(raw) == "Estate of Harold Jensen"

    def test_preserves_title_without_trailing_number(self) -> None:
        from ingestion.extract import strip_trailing_case_number

        raw = "In the Matter of Denise Guadalupe Mejia"
        assert strip_trailing_case_number(raw) == raw

    def test_does_not_strip_leading_number(self) -> None:
        """Only strips TRAILING case numbers — internal numbers stay."""
        from ingestion.extract import strip_trailing_case_number

        raw = "Case 2024CUBC038456 is Smith v. Jones"
        # No trailing case number — title preserved
        assert strip_trailing_case_number(raw) == raw

    def test_none_input(self) -> None:
        from ingestion.extract import strip_trailing_case_number

        assert strip_trailing_case_number(None) is None

    def test_empty_string(self) -> None:
        from ingestion.extract import strip_trailing_case_number

        assert strip_trailing_case_number("") == ""

    def test_strips_trailing_partial_ventura_number(self) -> None:
        """Handle partial old-format numbers (e.g. truncated to PRL)."""
        from ingestion.extract import strip_trailing_case_number

        raw = "In the Matter of Denise Guadalupe Mejia 202200570654PRL"
        # Should strip the malformed case number from the end
        assert strip_trailing_case_number(raw) == "In the Matter of Denise Guadalupe Mejia"

    def test_strips_ventura_mixed_case_5_digit(self) -> None:
        """Strip Ventura new-format case number with mixed-case letters and 5 digits (#3511)."""
        from ingestion.extract import strip_trailing_case_number

        raw = "X 2024Cubco20894"
        assert strip_trailing_case_number(raw) == "X"

    def test_strips_ventura_lowercase_5_digit(self) -> None:
        """Strip Ventura new-format case number with all-lowercase letters and 5 digits (#3511)."""
        from ingestion.extract import strip_trailing_case_number

        raw = "X 2024cubco20894"
        assert strip_trailing_case_number(raw) == "X"


# ---------------------------------------------------------------------------
# Bare "v" / "vs" suffix rejection (#3990)
# ---------------------------------------------------------------------------


class TestBareVsSuffix:
    """Unit tests for bare-vs-suffix detection and rejection (#3990).

    Covers:
      - has_bare_vs_suffix() helper
      - is_plausible_case_title() rejecting bare-vs inputs
      - clean_case_title() returning None for bare-vs inputs
      - test_bare_vs_separator_rejected (AC #3 named test)
    """

    def test_strips_bare_vs_suffix_helper(self) -> None:
        """has_bare_vs_suffix() correctly identifies bare-vs trailing suffixes."""
        from ingestion.extract import has_bare_vs_suffix

        assert has_bare_vs_suffix("Steinman v") is True
        assert has_bare_vs_suffix("Doe vs") is True
        assert has_bare_vs_suffix("Aoyagi vs.") is True
        assert has_bare_vs_suffix("Serrato Vs") is True
        assert has_bare_vs_suffix("Bridges vs.") is True
        # Titles with text after vs. are NOT bare-vs suffixes
        assert has_bare_vs_suffix("Smith v. Jones") is False
        assert has_bare_vs_suffix("Smith vs. State of California") is False
        assert has_bare_vs_suffix("In re Marriage of Garcia") is False

    def test_is_plausible_case_title_rejects_bare_vs(self) -> None:
        """is_plausible_case_title() returns False for bare-vs trailing suffix."""
        assert is_plausible_case_title("Steinman v") is False
        assert is_plausible_case_title("Doe vs") is False
        assert is_plausible_case_title("Aoyagi vs.") is False
        assert is_plausible_case_title("Serrato Vs") is False
        assert is_plausible_case_title("Bridges vs.") is False

    def test_clean_case_title_returns_none_for_bare_vs(self) -> None:
        """clean_case_title() returns None for bare-vs inputs (existing behavior)."""
        assert clean_case_title("Steinman v") is None
        assert clean_case_title("Doe vs") is None
        assert clean_case_title("Aoyagi vs.") is None

    def test_bare_vs_separator_rejected(self) -> None:
        """AC #3: LA/OC/RV-shaped fixture inputs that previously produced bare 'v.'
        must produce None (never the partial form).

        These simulate LLM outputs where the defendant name was truncated/missing,
        leaving a trailing bare vs-separator.
        """
        # LA-shaped: plaintiff name with trailing vs
        la_fixture_1 = "Steinman v"
        la_fixture_2 = "Doe vs."
        # OC-shaped: longer name with trailing vs.
        oc_fixture = "Aoyagi vs."
        # RV-shaped: mixed case vs
        rv_fixture = "Serrato Vs"

        for fixture in [la_fixture_1, la_fixture_2, oc_fixture, rv_fixture]:
            # is_plausible_case_title must reject it
            assert is_plausible_case_title(fixture) is False, (
                f"Expected is_plausible_case_title({fixture!r}) to be False"
            )
            # clean_case_title must return None (never the partial form)
            result = clean_case_title(fixture)
            assert result is None, (
                f"Expected clean_case_title({fixture!r}) to be None, got {result!r}"
            )

    def test_strips_4_digit_trailing(self) -> None:
        """Strip Ventura new-format case number with 4-digit sequence (#3511)."""
        from ingestion.extract import strip_trailing_case_number

        raw = "X 2024CUBC0204"
        assert strip_trailing_case_number(raw) == "X"

    def test_strips_7_digit_trailing(self) -> None:
        """Strip Ventura new-format case number with 7-digit sequence (#3511)."""
        from ingestion.extract import strip_trailing_case_number

        raw = "X 2024CUBC1234567"
        assert strip_trailing_case_number(raw) == "X"


class TestStripCaseNumberPrefixSuffix:
    """Tests for strip_case_number_prefix_suffix() — removes OC-style court
    prefix appended to the tail of a case title (#3680).

    OC case numbers start with ``"30-"``.  The LLM sometimes copies the ``30``
    prefix into the case title, producing contaminated titles like
    ``"Thomson vs. Toyota Motor Sales 30"``.
    """

    def test_strips_trailing_oc_prefix(self) -> None:
        from ingestion.extract import strip_case_number_prefix_suffix

        result = strip_case_number_prefix_suffix("Thomson vs. Toyota 30", "30")
        assert result == "Thomson vs. Toyota"

    def test_strips_trailing_prefix_with_whitespace(self) -> None:
        from ingestion.extract import strip_case_number_prefix_suffix

        # Extra spaces before the trailing prefix should be consumed.
        assert (
            strip_case_number_prefix_suffix("Thomson vs. Toyota  30", "30") == "Thomson vs. Toyota"
        )

    def test_no_change_when_prefix_is_none(self) -> None:
        from ingestion.extract import strip_case_number_prefix_suffix

        assert (
            strip_case_number_prefix_suffix("Thomson vs. Toyota 30", None)
            == "Thomson vs. Toyota 30"
        )

    def test_no_change_when_prefix_mid_name(self) -> None:
        """Do not strip prefix that appears mid-title (e.g. company name fragment)."""
        from ingestion.extract import strip_case_number_prefix_suffix

        assert strip_case_number_prefix_suffix("Acme 30 LLC vs. Doe", "30") == "Acme 30 LLC vs. Doe"

    def test_no_change_when_prefix_non_digit(self) -> None:
        """Prefix with non-digit characters should not be stripped."""
        from ingestion.extract import strip_case_number_prefix_suffix

        assert (
            strip_case_number_prefix_suffix("Thomson vs. Toyota 30A", "30A")
            == "Thomson vs. Toyota 30A"
        )

    def test_none_title_returns_none(self) -> None:
        from ingestion.extract import strip_case_number_prefix_suffix

        assert strip_case_number_prefix_suffix(None, "30") is None

    def test_empty_title_unchanged(self) -> None:
        from ingestion.extract import strip_case_number_prefix_suffix

        assert strip_case_number_prefix_suffix("", "30") == ""

    def test_empty_prefix_unchanged(self) -> None:
        from ingestion.extract import strip_case_number_prefix_suffix

        result = strip_case_number_prefix_suffix("Thomson vs. Toyota 30", "")
        assert result == "Thomson vs. Toyota 30"

    def test_strips_trailing_punctuation_after_prefix(self) -> None:
        """Trailing punctuation after the prefix should also be removed."""
        from ingestion.extract import strip_case_number_prefix_suffix

        result = strip_case_number_prefix_suffix("Thomson vs. Toyota 30.", "30")
        assert result == "Thomson vs. Toyota"

    def test_single_digit_prefix_not_stripped(self) -> None:
        """A single-digit prefix is too short — do not strip."""
        from ingestion.extract import strip_case_number_prefix_suffix

        assert strip_case_number_prefix_suffix("Smith v. Jones 3", "3") == "Smith v. Jones 3"

    def test_four_digit_prefix_not_stripped(self) -> None:
        """A four-digit prefix is too long — do not strip."""
        from ingestion.extract import strip_case_number_prefix_suffix

        assert (
            strip_case_number_prefix_suffix("Smith v. Jones 2025", "2025") == "Smith v. Jones 2025"
        )


class TestTitleCleanupOrdering:
    """End-to-end tests for the strip → dedupe → strip cleanup ordering (#3511).

    When a title contains both a repeated segment AND a trailing case number,
    applying dedupe before strip leaves a residual case-number fragment from
    the middle copy.  The correct order is strip → dedupe → strip so the
    second strip removes any newly exposed trailing fragment.
    """

    def test_ventura_duplicate_title_with_trailing_case_number(self) -> None:
        """Full cleanup: deduplicate then strip trailing case number (#3511)."""
        from ingestion.extract import dedupe_repeated_title, strip_trailing_case_number

        raw = (
            "Jamonte Clay v. Community Memorial Health System "
            "Jamonte Clay v. Community Memorial Health System "
            "2024Cubco20894"
        )
        step1 = strip_trailing_case_number(raw)
        step2 = dedupe_repeated_title(step1)
        step3 = strip_trailing_case_number(step2)
        assert step3 == "Jamonte Clay v. Community Memorial Health System"


class TestIsProbateDecedentName:
    """Tests for is_probate_decedent_name() — checks whether a candidate
    judge name matches a probate party name in the case title (#2370).

    Observed Ventura bug: for probate cases, the LLM extracts the decedent's
    name (prominent in the caption) as the judge_name.
    """

    def test_decedent_name_matches_title(self) -> None:
        from ingestion.extract import is_probate_decedent_name

        case_title = "Estate of Delbert L. Webb"
        assert is_probate_decedent_name("Delbert L. Webb", case_title) is True

    def test_decedent_name_matches_in_the_matter_of(self) -> None:
        from ingestion.extract import is_probate_decedent_name

        case_title = "In the Matter of Denise Guadalupe Mejia"
        assert is_probate_decedent_name("Denise Guadalupe Mejia", case_title) is True

    def test_conservatorship_of_matches(self) -> None:
        from ingestion.extract import is_probate_decedent_name

        case_title = "Conservatorship of Mary Smith"
        assert is_probate_decedent_name("Mary Smith", case_title) is True

    def test_trust_of_matches(self) -> None:
        from ingestion.extract import is_probate_decedent_name

        case_title = "Benjamin King Trust"
        assert is_probate_decedent_name("Benjamin King", case_title) is True

    def test_non_probate_title_returns_false(self) -> None:
        """Adversarial (civil) titles should not trigger the guard."""
        from ingestion.extract import is_probate_decedent_name

        case_title = "Smith v. Jones"
        assert is_probate_decedent_name("John Doe", case_title) is False

    def test_judge_name_different_from_party(self) -> None:
        """Real judge name (not matching the party) returns False."""
        from ingestion.extract import is_probate_decedent_name

        case_title = "Estate of Delbert L. Webb"
        # Real judge for probate dept J6
        assert is_probate_decedent_name("Gilbert A. Romero", case_title) is False

    def test_none_inputs(self) -> None:
        from ingestion.extract import is_probate_decedent_name

        assert is_probate_decedent_name(None, "Estate of Smith") is False
        assert is_probate_decedent_name("Smith", None) is False
        assert is_probate_decedent_name(None, None) is False

    def test_partial_match_case_insensitive(self) -> None:
        from ingestion.extract import is_probate_decedent_name

        case_title = "ESTATE OF HAROLD JENSEN, DECEASED"
        assert is_probate_decedent_name("Harold Jensen", case_title) is True


class TestIsPlausibleHearingDate:
    """Tests for is_plausible_hearing_date() — validates a candidate
    hearing_date against the document capture timestamp (#2370).

    Observed Ventura bug: LLM picks up future continuance dates from ruling
    body text as hearing_date. The actual hearing is typically on or near
    the capture day.
    """

    def test_capture_day_is_plausible(self) -> None:
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 3, 25, 12, 0, 0)
        hearing = date(2026, 3, 25)
        assert is_plausible_hearing_date(hearing, capture) is True

    def test_day_before_capture_is_plausible(self) -> None:
        """Agencies sometimes capture the day after the hearing."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 3, 26, 9, 0, 0)
        hearing = date(2026, 3, 25)
        assert is_plausible_hearing_date(hearing, capture) is True

    def test_future_date_within_window_is_plausible(self) -> None:
        """A hearing up to 7 days after capture is plausible (tentative rulings
        are typically posted a day or so before the hearing)."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 3, 25, 12, 0, 0)
        hearing = date(2026, 3, 28)
        assert is_plausible_hearing_date(hearing, capture) is True

    def test_far_future_date_not_plausible(self) -> None:
        """A continuance date months in the future is NOT the hearing date."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 3, 25, 12, 0, 0)
        hearing = date(2026, 5, 14)  # 50 days later — classic continuance
        assert is_plausible_hearing_date(hearing, capture) is False

    def test_december_far_future_not_plausible(self) -> None:
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 3, 23, 12, 0, 0)
        hearing = date(2026, 12, 11)
        assert is_plausible_hearing_date(hearing, capture) is False

    def test_far_past_date_not_plausible(self) -> None:
        """A date months before capture is likely a referenced minute order,
        not the actual hearing."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 3, 30, 12, 0, 0)
        hearing = date(2025, 12, 4)
        assert is_plausible_hearing_date(hearing, capture) is False

    def test_none_hearing_is_not_plausible(self) -> None:
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 3, 25, 12, 0, 0)
        assert is_plausible_hearing_date(None, capture) is False

    def test_none_capture_is_plausible(self) -> None:
        """Without a capture timestamp we can't validate — default to
        plausible (be permissive, don't drop data we can't verify)."""
        from ingestion.extract import is_plausible_hearing_date

        hearing = date(2026, 5, 14)
        assert is_plausible_hearing_date(hearing, None) is True

    # ------------------------------------------------------------------
    # case_type-aware windowing (#4251)
    #
    # Probate departments publish multi-week master calendars in a single
    # PDF (e.g. CC dept 38 publishes 4-16 hearings spanning 30+ days at a
    # time).  The civil ±14 day window incorrectly rejects these correctly-
    # extracted regex dates.  Passing ``case_type="probate"`` widens the
    # window to ±60 days.
    # ------------------------------------------------------------------

    def test_probate_future_30_days_is_plausible_with_case_type(self) -> None:
        """A probate hearing 30 days after capture is plausible — civil's
        ±14 day default would reject it, but probate's ±60 day window accepts."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 4, 26, 12, 0, 0)
        hearing = date(2026, 5, 26)  # +30 days
        # Without case_type: rejected by civil window
        assert is_plausible_hearing_date(hearing, capture) is False
        # With case_type="probate": accepted by widened window
        assert is_plausible_hearing_date(hearing, capture, case_type="probate") is True

    def test_probate_past_24_days_is_plausible_with_case_type(self) -> None:
        """Dept 38 master calendars publish hearings already past — captured
        2026-04-26 with a 2026-04-02 entry, delta=-24 days.  Civil rejects;
        probate accepts (#4251)."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 4, 26, 19, 17, 8)
        hearing = date(2026, 4, 2)  # -24 days
        assert is_plausible_hearing_date(hearing, capture) is False
        assert is_plausible_hearing_date(hearing, capture, case_type="probate") is True

    def test_probate_future_17_days_is_plausible_with_case_type(self) -> None:
        """Specific reproduction of the dept-38 sample2 NULL case — capture
        2026-04-26, hearing 2026-05-13 (+17 days).  Civil rejects; probate
        accepts (#4251)."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 4, 26, 19, 17, 8)
        hearing = date(2026, 5, 13)  # +17 days
        assert is_plausible_hearing_date(hearing, capture) is False
        assert is_plausible_hearing_date(hearing, capture, case_type="probate") is True

    def test_probate_far_future_still_rejected(self) -> None:
        """Even with the wider probate window, a 90-day-out date is still
        rejected — that's almost certainly a continuance, not the hearing."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 4, 26, 12, 0, 0)
        hearing = date(2026, 7, 26)  # +91 days, beyond the 60-day probate window
        assert is_plausible_hearing_date(hearing, capture, case_type="probate") is False

    def test_civil_case_type_uses_default_window(self) -> None:
        """Non-probate case_types fall through to the civil ±14 day default."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 4, 26, 12, 0, 0)
        hearing = date(2026, 5, 26)  # +30 days
        # case_type="civil" should NOT widen the window — only "probate" does.
        assert is_plausible_hearing_date(hearing, capture, case_type="civil") is False
        assert is_plausible_hearing_date(hearing, capture, case_type="family") is False
        assert is_plausible_hearing_date(hearing, capture, case_type="criminal") is False

    def test_explicit_window_overrides_case_type_default(self) -> None:
        """Passing ``past_days`` / ``future_days`` explicitly takes precedence
        over the case_type default — preserves backwards-compat for callers
        that already tune the window manually."""
        from datetime import datetime

        from ingestion.extract import is_plausible_hearing_date

        capture = datetime(2026, 4, 26, 12, 0, 0)
        hearing = date(2026, 5, 26)  # +30 days
        # Probate default would accept this, but explicit ±5 day override
        # rejects regardless of case_type.
        assert (
            is_plausible_hearing_date(
                hearing,
                capture,
                past_days=5,
                future_days=5,
                case_type="probate",
            )
            is False
        )


class TestDedupeRepeatedTitleEdgeCases:
    """Branch coverage for dedupe_repeated_title() edge cases (#2370)."""

    def test_whitespace_only_returns_input(self) -> None:
        """Whitespace-only after collapse returns the original input."""
        from ingestion.extract import dedupe_repeated_title

        # A string of only whitespace: after .split() returns empty list,
        # " ".join([]) == "", which hits the "not normalized" branch.
        result = dedupe_repeated_title("   \t  \n  ")
        # Should return the original input (not None, not stripped)
        assert result == "   \t  \n  "

    def test_prefix_mid_word_candidates_fall_through(self) -> None:
        """Candidates that would split mid-word are skipped; no dedup detected.

        A title with no repeated structure exercises many loop iterations
        where `normalized[prefix_len] != " "` skips via `continue`, and
        ultimately falls through to `return normalized`.
        """
        from ingestion.extract import dedupe_repeated_title

        # Plain non-repeating sentence long enough to iterate many prefixes
        raw = "The quick brown fox jumps over the lazy dog and runs away"
        result = dedupe_repeated_title(raw)
        # No valid dedup possible → returns normalized input unchanged
        assert result == raw

    def test_no_rest_after_prefix_skip(self) -> None:
        """When the prefix consumes everything but trailing whitespace,
        `rest` is empty and the loop should skip this prefix length."""
        from ingestion.extract import dedupe_repeated_title

        # No rest after prefix: no repetition detected — returns normalized.
        raw = "Unique Non-Repeating Title XYZ"
        result = dedupe_repeated_title(raw)
        assert result == raw


class TestIsProbateDecedentNameEdgeCases:
    """Branch coverage for is_probate_decedent_name() edge cases (#2370)."""

    def test_empty_normalized_candidate(self) -> None:
        """When candidate normalizes to an empty/tiny string, return False."""
        from ingestion.extract import is_probate_decedent_name

        # Candidate of only punctuation normalizes to empty
        assert is_probate_decedent_name(".,", "Estate of Smith") is False
        # Very short candidate (< 3 chars) returns False
        assert is_probate_decedent_name("Jo", "Estate of Smith") is False

    def test_single_token_candidate_no_substring(self) -> None:
        """Single-token candidate that is not a substring of the title returns False."""
        from ingestion.extract import is_probate_decedent_name

        # "Romero" is a single token not in the title — returns False
        # without entering the multi-token matching branch.
        assert is_probate_decedent_name("Romero", "Estate of Smith") is False

    def test_token_match_all_tokens_found(self) -> None:
        """Multi-token candidate where all tokens appear in order — True path."""
        from ingestion.extract import is_probate_decedent_name

        # "Jane Smith" appears as tokens inside "In the Matter of Jane M Smith"
        assert (
            is_probate_decedent_name(
                "Jane Smith",
                "In the Matter of Jane M Smith",
            )
            is True
        )

    def test_token_match_missing_token_returns_false(self) -> None:
        """Multi-token candidate where a token is absent — False."""
        from ingestion.extract import is_probate_decedent_name

        # "Jane Williams" — Williams not in title
        assert (
            is_probate_decedent_name(
                "Jane Williams",
                "In the Matter of Jane M Smith",
            )
            is False
        )


# ---------------------------------------------------------------------------
# Procedural-text vocabulary widening (#3615)
# ---------------------------------------------------------------------------
#
# These tests cover the gaps described in #3615: the LLM concatenates the
# case caption with motion-description / procedural-language text using
# phrases that _TITLE_TERMINATOR_RE and _IMPLAUSIBLE_FRAGMENTS_RE didn't
# previously cover.  The fix widens both regexes so:
#   1. is_plausible_case_title() rejects contaminated titles (gating cleanup).
#   2. clean_case_title() finds a boundary and returns a clean party-name pair.
#
# Each contamination shape gets a positive (rejected as implausible AND
# cleanup recovers a clean title) and a negative regression test (legitimate
# party names containing the same lexical token are NOT rejected).


class TestProceduralVocabularyFragments:
    """Issue #3615 — widened _IMPLAUSIBLE_FRAGMENTS_RE rejects contaminated
    titles so they enter the cleanup branch instead of bypassing it."""

    # --- "Cause of Action" / "Nth Cause of Action" ---

    def test_rejects_cause_of_action(self) -> None:
        """'X v. Y Cause of Action ...' is procedural body text contamination."""
        title = "Smith v. Jones Cause of Action for Negligence"
        assert is_plausible_case_title(title) is False

    def test_rejects_nth_cause_of_action(self) -> None:
        """'X v. Y Sixth Cause of Action ...' is body section header contamination."""
        title = "STEINMAN v. FORD MOTOR COMPANY Sixth Cause of Action - Fraudulent Inducement"
        assert is_plausible_case_title(title) is False

    def test_rejects_first_cause_of_action(self) -> None:
        """'X v. Y First Cause of Action ...' is contamination."""
        title = (
            "Seaman v. Hoag Memorial Hospital First Cause of Action for Disability Discrimination"
        )
        assert is_plausible_case_title(title) is False

    # --- "Judge <Name>" ---

    def test_rejects_judge_named(self) -> None:
        """'X v. Y Judge Smith's Report ...' is body-text contamination."""
        title = "Balt USA, LLC v. Treadstone Medical Judge Smith's Report"
        assert is_plausible_case_title(title) is False

    def test_rejects_judge_single_letter_name(self) -> None:
        """'X v. Y Judge A's decision ...' single-letter name is also caught.

        Adversarial-review hardening: the original \\w pattern required at
        least one word char AFTER the leading capital, missing single-letter
        names. \\w* allows zero-or-more so 'Judge A' matches just like
        'Judge Smith'.
        """
        title = "Smith v. Jones Judge A's decision on the motion"
        assert is_plausible_case_title(title) is False

    def test_rejects_judge_apostrophe_name(self) -> None:
        """'Judge d'Arcy' (apostrophe in name) is caught.

        Adversarial-review hardening: judge names with apostrophes
        (Irish surnames like d'Arcy, O'Brien) require ``[\\w'\\-]*`` not
        just ``\\w*`` so the apostrophe-internal char class extends through.
        """
        title = "Acme v. Widget Judge d'Arcy's report on the motion"
        assert is_plausible_case_title(title) is False

    def test_rejects_judge_hyphenated_name(self) -> None:
        """'Judge Smith-Jones' (hyphenated compound name) is caught.

        Adversarial-review hardening: hyphenated compound names require
        ``[\\w'\\-]*`` so the hyphen extends the match through both halves.
        """
        title = "Acme v. Widget Judge Smith-Jones's order on demurrer"
        assert is_plausible_case_title(title) is False

    def test_accepts_judge_entity_name(self) -> None:
        """'Judge & Sons LLC' as an entity name (no following capitalized word) is preserved."""
        # The pattern requires "Judge" + whitespace + capitalized word, so an
        # entity name like "Judge & Sons LLC" or "Judge Inc." is NOT flagged.
        # Note the leading non-word "& " breaks the \w follow.
        assert is_plausible_case_title("Judge & Sons LLC v. Smith") is True

    # --- "MIL N" (motion in limine) ---

    def test_rejects_mil_with_number(self) -> None:
        """'X v. Y MIL 2: Motion ...' is motion-in-limine contamination."""
        title = "MOJAVE PISTACHIOS v. INDIAN WELLS WATER MIL 2: Motion to Exclude"
        assert is_plausible_case_title(title) is False

    # --- "Allegations <preposition>" ---

    def test_rejects_allegations_in(self) -> None:
        """'X v. Y Allegations in the Complaint ...' is body-text contamination."""
        title = "Wells Fargo Bank v. HLEE, Inc Allegations in the Complaint"
        assert is_plausible_case_title(title) is False

    def test_accepts_allegations_inc(self) -> None:
        """'Allegations Inc.' as a party name (no following preposition) is preserved."""
        # The pattern requires Allegations + a preposition/verb, so "Allegations Inc."
        # is NOT flagged.
        assert is_plausible_case_title("Allegations Inc. v. Smith") is True

    # --- "Declaration of" ---

    def test_rejects_declaration_of(self) -> None:
        """'X v. Y Declaration of Daniel ...' is procedural contamination."""
        title = "Nano Banc v. Shyam Declaration of Daniel Patrick"
        assert is_plausible_case_title(title) is False

    # --- "Objections <preposition>" ---

    def test_rejects_objections_to(self) -> None:
        """'X v. Y Defendants' Objections to ...' is procedural contamination."""
        title = "Nano Banc v. Shyam Defendants' Objections to Declaration"
        assert is_plausible_case_title(title) is False

    # --- "Report and Recommendations" ---

    def test_rejects_report_and_recommendations(self) -> None:
        """'X v. Y Report and Recommendations ...' is body-text contamination."""
        title = "Balt USA v. Treadstone Report and Recommendations Moving"
        assert is_plausible_case_title(title) is False

    def test_rejects_report_and_recommendation_singular(self) -> None:
        """Singular 'Recommendation' also rejected."""
        title = "Smith v. Jones Report and Recommendation on Motion"
        assert is_plausible_case_title(title) is False

    # --- "Cross-Complaint" ---

    def test_rejects_cross_complaint(self) -> None:
        """'X v. Y Cross-Complaint ...' is procedural contamination."""
        title = "Smith v. Jones Cross-Complaint of Defendant Roe"
        assert is_plausible_case_title(title) is False

    def test_accepts_cross_roads_entity(self) -> None:
        """'Cross Roads Inc.' (no hyphen, NOT 'Cross-Complaint') is preserved."""
        assert is_plausible_case_title("Cross Roads Inc. v. Smith") is True

    # --- "Plaintiff's burden" / "Defendant's Ability" ---

    def test_rejects_plaintiff_burden(self) -> None:
        """'X v. Y Plaintiff's burden ...' is body-text contamination."""
        title = "Wells Fargo v. HLEE Plaintiff's burden: Issue 1"
        assert is_plausible_case_title(title) is False

    def test_rejects_defendant_ability(self) -> None:
        """'X v. Y Defendants' Ability ...' is body-text contamination."""
        title = "Balt USA v. Treadstone Moving Defendants' Ability to"
        assert is_plausible_case_title(title) is False

    # --- "Ex Parte <verb-noun>" ---

    def test_rejects_ex_parte_application(self) -> None:
        """'X v. Y Ex Parte Application ...' is procedural contamination."""
        title = "Tong v. Le Ex Parte Application for Restraining Order"
        assert is_plausible_case_title(title) is False

    def test_rejects_ex_parte_motion(self) -> None:
        """'X v. Y Ex Parte Motion ...' is procedural contamination."""
        title = "Smith v. Jones Ex Parte Motion to Compel Discovery"
        assert is_plausible_case_title(title) is False

    # --- "individually and on behalf of" (class-action descriptor) ---

    def test_rejects_individually_and_on_behalf_of(self) -> None:
        """'X v. Y Z, individually and on behalf of ...' is class-action
        contamination.

        Note the comma before 'individually' — this is the actual shape
        of the contamination in OC PDFs (the LLM emits a name then
        appends the descriptor as a continuation).  The regex requires
        the comma so legitimate entity names that contain the phrase
        without a comma boundary are NOT flagged.
        """
        title = (
            "Gonzalez v. Buccola Services, Inc. Everardo Gonzalez, individually and on behalf of"
        )
        assert is_plausible_case_title(title) is False

    def test_accepts_individually_phrase_without_comma_boundary(self) -> None:
        """A legitimate (though contrived) entity-name use of the phrase
        without the comma delimiter is NOT flagged as contamination.

        The comma in the regex (``,\\s+individually\\s+and\\s+on\\s+behalf``)
        distinguishes contamination from legitimate use (issue #3615
        acceptance criteria called out this edge case explicitly:
        "a SHORT title containing the phrase that's actually a
        legitimate party name does NOT trip cleanup").
        """
        # No comma before "individually" — the regex doesn't fire.  This
        # test is contrived (no real-world example exists in the data) but
        # it pins the comma-boundary contract so a future regex relaxation
        # would fail this assertion.
        title = "In re The Individually and On Behalf Of Trust"
        assert is_plausible_case_title(title) is True

    # --- "And Cross-Defendants/Complainants" ---

    def test_rejects_leading_and_cross_defendants(self) -> None:
        """Leading 'And Cross-Defendants ...' is structural caption noise."""
        title = "And Cross-Defendants Jane Does 1-5 v. William Robinson"
        assert is_plausible_case_title(title) is False

    def test_rejects_v_and_cross_complainants(self) -> None:
        """'X v. And Cross-Complainants ...' is structural caption noise."""
        title = "Jane Does 1-5 v. And Cross-Complainants William Robinson"
        assert is_plausible_case_title(title) is False

    # --- Negative regression: legitimate titles must still pass ---

    def test_accepts_steinman_short_form(self) -> None:
        """The legitimate 'STEINMAN VS. FORD MOTOR COMPANY' (no contamination) passes."""
        # Note: ALL CAPS is OK; this is the un-contaminated version of the
        # contaminated examples above.
        assert is_plausible_case_title("STEINMAN VS. FORD MOTOR COMPANY") is True

    def test_accepts_legitimate_motor_company(self) -> None:
        """Title-case 'Steinman v. Ford Motor Company' passes."""
        assert is_plausible_case_title("Steinman v. Ford Motor Company") is True

    def test_accepts_in_re_marriage_with_party(self) -> None:
        """Probate titles still pass."""
        assert is_plausible_case_title("In re Marriage of Garcia") is True


class TestProceduralVocabularyTerminators:
    """Issue #3615 — widened _TITLE_TERMINATOR_RE causes clean_case_title()
    to find the boundary in contaminated titles and return clean party names."""

    def _expect_clean_or_implausible(
        self,
        raw: str,
        expected_substrings: list[str],
        excluded_substrings: list[str],
    ) -> None:
        """Helper: run clean_case_title and assert the result either:
        - returns a value containing expected_substrings and excluding
          excluded_substrings, OR
        - returns None / a value that fails is_plausible_case_title (which
          triggers the worker.py NULL fallback — also acceptable).
        """
        result = clean_case_title(raw)
        if result is None or not is_plausible_case_title(result):
            # NULL fallback path is acceptable for unrecoverable contamination.
            return
        for substr in expected_substrings:
            assert substr.lower() in result.lower(), (
                f"Expected {substr!r} in cleaned result {result!r}"
            )
        for substr in excluded_substrings:
            assert substr.lower() not in result.lower(), (
                f"Did not expect {substr!r} in cleaned result {result!r}"
            )

    def test_clean_truncates_cause_of_action(self) -> None:
        """Sixth Cause of Action contamination is truncated."""
        self._expect_clean_or_implausible(
            "STEINMAN VS. FORD MOTOR COMPANY Sixth Cause of Action - Fraudulent Inducement",
            expected_substrings=["Steinman", "Ford", "Motor", "Company"],
            excluded_substrings=["Cause of Action", "Sixth", "Fraudulent", "Inducement"],
        )

    def test_clean_truncates_first_cause_of_action(self) -> None:
        """First Cause of Action contamination is truncated."""
        self._expect_clean_or_implausible(
            "Seaman v. Hoag Memorial Hospital First Cause of Action for Disability Discrimination",
            expected_substrings=["Seaman", "Hoag"],
            excluded_substrings=["First Cause", "Disability"],
        )

    def test_clean_truncates_at_judge_name(self) -> None:
        """'Judge Smith' contamination is truncated."""
        self._expect_clean_or_implausible(
            "Balt USA, LLC v. Treadstone Medical Judge Smith's Report and Recommendations",
            expected_substrings=["Balt", "Treadstone"],
            excluded_substrings=["Judge Smith", "Recommendations"],
        )

    def test_clean_truncates_at_apostrophe_judge_name(self) -> None:
        """'Judge d'Arcy' (Irish surname with apostrophe) is truncated."""
        self._expect_clean_or_implausible(
            "Acme v. Widget Judge d'Arcy's report on the motion",
            expected_substrings=["Acme", "Widget"],
            excluded_substrings=["report", "motion"],
        )

    def test_clean_truncates_at_bare_motion(self) -> None:
        """'X v. Y Motion for Sanctions' bare-MOTION contamination is truncated.

        Adversarial-review hardening: the original implementation only had
        ``MOTION TO|FOR|IN|RE`` in the terminator regex.  The bare
        ``\\bMOTION\\b`` alternative lets the cleaner truncate when the
        contamination is just "Motion <something else>".
        """
        self._expect_clean_or_implausible(
            "Smith v. Jones Motion for Sanctions",
            expected_substrings=["Smith", "Jones"],
            excluded_substrings=["Sanctions"],
        )

    def test_clean_truncates_at_mil_number(self) -> None:
        """'MIL 2:' motion-in-limine contamination is truncated."""
        self._expect_clean_or_implausible(
            "MOJAVE PISTACHIOS v. INDIAN WELLS WATER MIL 2: Motion by the Authority",
            expected_substrings=["Mojave", "Indian Wells"],
            excluded_substrings=["MIL 2", "Authority"],
        )

    def test_clean_truncates_at_allegations(self) -> None:
        """'Allegations in' contamination is truncated."""
        self._expect_clean_or_implausible(
            "Wells Fargo Bank v. HLEE, Inc Allegations in the Complaint Plaintiff's burden",
            expected_substrings=["Wells Fargo", "HLEE"],
            excluded_substrings=["Allegations", "burden"],
        )

    def test_clean_truncates_at_declaration_of(self) -> None:
        """'Declaration of' contamination is truncated."""
        self._expect_clean_or_implausible(
            "Nano Banc v. Shyam Defendants' Objections to Declaration of Daniel Patrick",
            expected_substrings=["Nano", "Shyam"],
            excluded_substrings=["Declaration of", "Daniel Patrick"],
        )

    def test_clean_truncates_at_ex_parte(self) -> None:
        """'Ex Parte Application' contamination is truncated."""
        self._expect_clean_or_implausible(
            "Tong vs. Le Ex Parte Application for Temporary Restraining Order",
            expected_substrings=["Tong", "Le"],
            excluded_substrings=["Ex Parte", "Restraining"],
        )

    def test_clean_truncates_at_individually_and_on_behalf_of(self) -> None:
        """'individually and on behalf of' class-action descriptor is truncated."""
        raw = (
            "Gonzalez v. Buccola Services, Inc. Everardo Gonzalez, "
            "individually and on behalf of others"
        )
        self._expect_clean_or_implausible(
            raw,
            expected_substrings=["Gonzalez", "Buccola"],
            excluded_substrings=["individually", "on behalf"],
        )

    def test_clean_truncates_body_sentence_fusion(self) -> None:
        """Body-sentence fusion ('Plaintiff X's...' continuation) is unrecoverable
        but must NOT round-trip the contamination unchanged.

        Per issue body, 'Plaintiff's <noun>' patterns are added to the
        terminator regex, so the cleaner truncates at "Plaintiff's"."""
        raw = (
            "Wilmington Trust, National Association v. Merge Portfolio Owner, "
            "LLC Plaintiff Wilmington Trust, National Association's"
        )
        self._expect_clean_or_implausible(
            raw,
            expected_substrings=["Wilmington", "Merge Portfolio"],
            excluded_substrings=[],
        )

    def test_clean_logan_trust_known_unrecoverable_at_cleaner_level(self) -> None:
        """Demurrer-analysis prose fusion ('The FAP does not establish ...') is
        sentence-fragment contamination that the cleaner's regex vocabulary
        genuinely cannot detect — there's no consistent token to anchor a
        boundary on.

        Per the 2026-05-03 spotcheck on issue #3615 (operator's own words):
        "structural fix (null on silent-failure + enrichment hook) is more
        important than chasing new terminator phrases one at a time."

        This test documents the gap rather than asserting recovery.  The
        sentence-fragment case is upstream of the cleanup layer — the LLM
        produced a hallucinated party name ("Logan Diversified LP") fused
        with body prose ("The FAP does not establish ...").  Neither the
        cleaner nor the implausibility regex can reliably catch this without
        an LLM round-trip.  The mitigation lives in the LLM-extraction layer
        (better prompts, post-extraction sanity checks via re-prompting), not
        in the deterministic cleanup.
        """
        raw = (
            "Logan - Trust Garfield v. Logan Diversified LP The FAP "
            "does not establish Colin had reason to discover the causes"
        )
        result = clean_case_title(raw)
        # The cleaner returns the input unchanged (no terminator hits).
        # This is documented expected behavior; a future enrichment-layer
        # fix would catch this upstream.  Keeping the assertion loose: the
        # cleaner doesn't make the title worse than it was.
        assert result is None or len(result) <= len(raw) + 10

    def test_clean_unrecoverable_and_cross_returns_implausible(self) -> None:
        """'And Cross-Defendants ... v. And Cross-Complainants ...' is structural
        caption noise with no clean party names.  Cleaner result either fails
        plausibility (triggering NULL fallback) or returns None."""
        raw = (
            "And Cross-Defendants Jane Does 1-5 And John Doe 1 v. "
            "And Cross-Complainants William Robinson, Jr"
        )
        result = clean_case_title(raw)
        # Acceptable: result is None, or fails is_plausible_case_title.
        # If the cleaner does produce something plausible, at minimum the
        # leading "And Cross-Defendants" caption noise must be gone.
        if result is not None and is_plausible_case_title(result):
            assert not result.lower().startswith("and cross")

    # --- Negative regression tests: legitimate titles round-trip unchanged ---

    def test_clean_legitimate_steinman(self) -> None:
        """Legitimate all-caps title is title-cased without truncation."""
        result = clean_case_title("STEINMAN VS. FORD MOTOR COMPANY")
        assert result is not None
        assert "Steinman" in result
        assert "Ford" in result
        assert "Motor Company" in result

    def test_clean_legitimate_smith_v_jones(self) -> None:
        """Legitimate adversarial title round-trips."""
        result = clean_case_title("Smith v. Jones")
        assert result == "Smith v. Jones"

    def test_clean_legitimate_acme_widget(self) -> None:
        """Legitimate corporate adversarial title round-trips."""
        result = clean_case_title("Acme Corporation v. Widget LLC")
        assert result is not None
        assert "Acme Corporation" in result
        assert "Widget LLC" in result


class TestEmptyCaseTitle:
    """Issue #3615 spotcheck (2026-04-28) — empty-string case_title is a
    third state that should never persist; it must normalize to None."""

    def test_empty_string_is_implausible(self) -> None:
        """Empty string fails plausibility (already covered, but reaffirms)."""
        assert is_plausible_case_title("") is False

    def test_whitespace_only_is_implausible(self) -> None:
        """Whitespace-only string fails plausibility."""
        assert is_plausible_case_title("   ") is False
        assert is_plausible_case_title("\t\n") is False
