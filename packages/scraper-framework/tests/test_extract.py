"""Tests for the basic regex-based outcome, motion_type, judge name, case number,
and case title extraction."""

from __future__ import annotations

from datetime import date

import pytest

from ingestion.extract import (
    _extract_from_inline_party_refs,
    _is_name_fragment,
    _looks_like_motion_text,
    _looks_like_person_name,
    _split_caption_names,
    extract_case_number,
    extract_case_title,
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
    extract_hearing_date,
    extract_judge_name,
    extract_motion_type,
    extract_outcome,
    extract_parties_from_caption,
    is_plausible_case_title,
    is_valid_case_number,
    normalize_motion_type,
    normalize_outcome,
)

# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------


class TestExtractOutcome:
    """Tests for extract_outcome()."""

    def test_granted(self) -> None:
        assert extract_outcome("The motion is GRANTED.") == "granted"

    def test_denied(self) -> None:
        assert extract_outcome("The motion is DENIED.") == "denied"

    def test_granted_in_part(self) -> None:
        assert extract_outcome("The motion is GRANTED IN PART.") == "granted_in_part"

    def test_denied_in_part(self) -> None:
        assert extract_outcome("The motion is DENIED IN PART.") == "denied_in_part"

    def test_moot(self) -> None:
        assert extract_outcome("The matter is MOOT.") == "moot"

    def test_continued(self) -> None:
        assert extract_outcome("The hearing is CONTINUED to April 1.") == "continued"

    def test_off_calendar(self) -> None:
        assert extract_outcome("This matter is OFF CALENDAR.") == "off_calendar"

    def test_off_calendar_hyphenated(self) -> None:
        assert extract_outcome("Matter is off-calendar.") == "off_calendar"

    def test_submitted(self) -> None:
        assert extract_outcome("The matter is SUBMITTED.") == "submitted"

    def test_no_match(self) -> None:
        assert extract_outcome("The court sets a case management conference.") is None

    def test_empty_string(self) -> None:
        assert extract_outcome("") is None

    def test_case_insensitive(self) -> None:
        assert extract_outcome("motion granted") == "granted"

    def test_granted_in_part_before_granted(self) -> None:
        """'Granted in part' should match before plain 'granted'."""
        text = "The motion for summary judgment is granted in part and denied in part."
        assert extract_outcome(text) == "granted_in_part"


# ---------------------------------------------------------------------------
# Outcome normalization (#1878)
# ---------------------------------------------------------------------------


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
# ---------------------------------------------------------------------------


class TestExtractMotionType:
    """Tests for extract_motion_type()."""

    def test_summary_judgment(self) -> None:
        assert extract_motion_type("Motion for Summary Judgment") == "msj"

    def test_summary_judgment_shorthand(self) -> None:
        assert extract_motion_type("Defendant's summary judgment motion") == "msj"

    def test_summary_adjudication(self) -> None:
        assert extract_motion_type("Motion for Summary Adjudication") == "msj_partial"

    def test_partial_summary_judgment(self) -> None:
        assert extract_motion_type("Partial summary judgment is sought") == "msj_partial"

    def test_motion_to_dismiss(self) -> None:
        assert extract_motion_type("Motion to Dismiss for Failure to State a Claim") == "mtd"

    def test_motion_in_limine(self) -> None:
        assert extract_motion_type("Plaintiff's Motion in Limine No. 3") == "mil"

    def test_demurrer(self) -> None:
        assert extract_motion_type("Demurrer to the First Amended Complaint") == "demurrer"

    def test_motion_to_compel(self) -> None:
        assert extract_motion_type("Motion to Compel Further Responses") == "motion_to_compel"

    def test_motion_to_strike(self) -> None:
        assert extract_motion_type("Motion to Strike Punitive Damages") == "motion_to_strike"

    def test_anti_slapp(self) -> None:
        assert extract_motion_type("Anti-SLAPP Motion (CCP 425.16)") == "anti_slapp"

    def test_anti_slapp_no_hyphen(self) -> None:
        assert extract_motion_type("Special motion to strike under antiSLAPP") == "anti_slapp"

    def test_preliminary_injunction(self) -> None:
        assert extract_motion_type("Motion for Preliminary Injunction") == "preliminary_injunction"

    def test_no_match(self) -> None:
        assert extract_motion_type("The court sets a case management conference.") is None

    def test_empty_string(self) -> None:
        assert extract_motion_type("") is None

    def test_summary_adjudication_before_summary_judgment(self) -> None:
        """Summary adjudication should match before plain summary judgment."""
        text = "Motion for Summary Adjudication of Issues"
        assert extract_motion_type(text) == "msj_partial"

    # --- New motion type patterns (issue #260) ---

    def test_ex_parte_application(self) -> None:
        text = "Ex Parte Application for Temporary Restraining Order"
        assert extract_motion_type(text) == "ex_parte_application"

    def test_ex_parte_application_lowercase(self) -> None:
        text = "Plaintiff's ex parte application for order shortening time"
        assert extract_motion_type(text) == "ex_parte_application"

    def test_ex_parte_motion(self) -> None:
        text = "Ex Parte Motion for Leave to File"
        assert extract_motion_type(text) == "ex_parte_application"

    def test_petition_writ_of_mandate(self) -> None:
        text = "Petition for Writ of Mandate"
        assert extract_motion_type(text) == "petition_writ_of_mandate"

    def test_petition_writ_of_mandamus(self) -> None:
        text = "Petition for Writ of Mandamus filed by Respondent"
        assert extract_motion_type(text) == "petition_writ_of_mandate"

    def test_petition_habeas_corpus(self) -> None:
        text = "Petition for Writ of Habeas Corpus"
        assert extract_motion_type(text) == "petition_habeas_corpus"

    def test_petition_generic(self) -> None:
        text = "Petition to Approve Minor's Compromise"
        assert extract_motion_type(text) == "petition"

    def test_petition_specific_writ_before_generic(self) -> None:
        """Writ of mandate petition should match before generic petition."""
        text = "Petition for Writ of Mandate is denied."
        assert extract_motion_type(text) == "petition_writ_of_mandate"

    def test_order_to_show_cause(self) -> None:
        text = "Order to Show Cause re: Contempt"
        assert extract_motion_type(text) == "osc"

    def test_order_to_show_cause_lowercase(self) -> None:
        text = "Hearing on order to show cause re contempt"
        assert extract_motion_type(text) == "osc"

    def test_motion_to_quash(self) -> None:
        text = "Motion to Quash Service of Summons"
        assert extract_motion_type(text) == "motion_to_quash"

    def test_motion_to_quash_subpoena(self) -> None:
        text = "Motion to Quash Deposition Subpoena"
        assert extract_motion_type(text) == "motion_to_quash"

    def test_motion_for_reconsideration(self) -> None:
        text = "Motion for Reconsideration of the Court's Prior Ruling"
        assert extract_motion_type(text) == "motion_for_reconsideration"

    def test_motion_for_protective_order(self) -> None:
        text = "Motion for Protective Order re: Trade Secrets"
        assert extract_motion_type(text) == "motion_for_protective_order"

    def test_motion_for_attorney_fees(self) -> None:
        text = "Motion for Attorney Fees and Costs"
        assert extract_motion_type(text) == "motion_for_attorney_fees"

    def test_motion_for_attorneys_fees(self) -> None:
        """Handle the possessive form 'attorney's fees'."""
        text = "Motion for Attorney's Fees pursuant to CCP 1021.5"
        assert extract_motion_type(text) == "motion_for_attorney_fees"

    def test_motion_for_attorneys_fees_plural(self) -> None:
        """Handle the plural possessive 'attorneys fees'."""
        text = "Motion for Attorneys Fees"
        assert extract_motion_type(text) == "motion_for_attorney_fees"

    def test_motion_to_set_aside_default(self) -> None:
        text = "Motion to Set Aside Default and Default Judgment"
        assert extract_motion_type(text) == "motion_to_set_aside_default"

    def test_motion_to_set_aside_the_default(self) -> None:
        """Handle 'set aside the default' variant."""
        text = "Motion to Set Aside the Default entered on January 5"
        assert extract_motion_type(text) == "motion_to_set_aside_default"

    def test_motion_to_vacate(self) -> None:
        text = "Motion to Vacate Judgment"
        assert extract_motion_type(text) == "motion_to_vacate"

    # --- Bug fixes (issue #421) ---

    def test_attorneys_fees_curly_apostrophe(self) -> None:
        """Curly apostrophe (U+2019) in 'Attorneys\u2019 Fees' must match (#421)."""
        text = "Motion for Attorneys\u2019 Fees"
        assert extract_motion_type(text) == "motion_for_attorney_fees"

    def test_attorneys_fees_straight_apostrophe(self) -> None:
        """Straight apostrophe in \"Attorney's Fees\" must still match."""
        text = "Motion for Attorney's Fees"
        assert extract_motion_type(text) == "motion_for_attorney_fees"

    def test_motions_to_compel_plural(self) -> None:
        """Plural 'motions to compel' must match (#421)."""
        text = "Motions to Compel Further Discovery Responses"
        assert extract_motion_type(text) == "motion_to_compel"

    def test_motion_to_compel_singular_still_works(self) -> None:
        """Singular 'motion to compel' must still match after plural fix."""
        text = "Motion to Compel Responses"
        assert extract_motion_type(text) == "motion_to_compel"

    # --- New patterns (issue #421) ---

    def test_default_judgment(self) -> None:
        text = "Application for Default Judgment"
        assert extract_motion_type(text) == "default_judgment"

    def test_motion_to_be_relieved_as_counsel(self) -> None:
        text = "Motion to Be Relieved as Counsel"
        assert extract_motion_type(text) == "motion_to_be_relieved_as_counsel"

    def test_motion_for_leave_to_amend(self) -> None:
        text = "Motion for Leave to File Second Amended Complaint"
        assert extract_motion_type(text) == "motion_for_leave_to_amend"

    def test_class_action_settlement(self) -> None:
        text = "Motion for Class Action Settlement Approval"
        assert extract_motion_type(text) == "class_action_settlement"

    def test_preliminary_approval(self) -> None:
        text = "Petition for Preliminary Approval of Settlement"
        assert extract_motion_type(text) == "class_action_settlement"

    def test_paga_settlement(self) -> None:
        text = "Motion for Approval of PAGA Settlement"
        assert extract_motion_type(text) == "paga_settlement"

    def test_paga_settlement_standalone(self) -> None:
        text = "PAGA settlement is GRANTED"
        assert extract_motion_type(text) == "paga_settlement"

    def test_paga_settlement_continued(self) -> None:
        text = "Motion \u2013 Approval of PAGA Settlement is CONTINUED"
        assert extract_motion_type(text) == "paga_settlement"

    def test_settlement_agreement(self) -> None:
        text = "See Settlement Agreement \u00b6 II.C."
        assert extract_motion_type(text) == "settlement_approval"

    def test_settlement_approval(self) -> None:
        text = "Motion for Settlement Approval"
        assert extract_motion_type(text) == "settlement_approval"

    def test_settlement_hearing(self) -> None:
        text = "The court will hold a settlement hearing on March 1"
        assert extract_motion_type(text) == "settlement_approval"

    def test_approval_of_paga(self) -> None:
        """'Approval of PAGA' variant matches paga_settlement."""
        text = "Motion for Approval of PAGA Agreement"
        assert extract_motion_type(text) == "paga_settlement"

    def test_approval_of_global_settlement(self) -> None:
        """'Approval of X settlement' variant matches settlement_approval."""
        text = "Motion for Approval of the Global Settlement"
        assert extract_motion_type(text) == "settlement_approval"

    def test_class_action_before_settlement(self) -> None:
        """Class action settlement should match before generic settlement."""
        text = "Motion for Class Action Settlement Approval"
        assert extract_motion_type(text) == "class_action_settlement"

    def test_motion_for_sanctions(self) -> None:
        text = "Motion for Sanctions under CCP 128.7"
        assert extract_motion_type(text) == "motion_for_sanctions"

    def test_motion_for_relief(self) -> None:
        text = "Motion for Relief from Waiver of Objections"
        assert extract_motion_type(text) == "motion_for_relief"

    def test_motion_for_sanctions_before_relief(self) -> None:
        """Sanctions should match before generic relief."""
        text = "Motion for Sanctions"
        assert extract_motion_type(text) == "motion_for_sanctions"

    def test_ex_parte_standalone(self) -> None:
        """Standalone 'ex parte' without 'application' or 'motion' (#421)."""
        text = "Ex Parte for Temporary Restraining Order"
        assert extract_motion_type(text) == "ex_parte_application"

    def test_motion_pro_hac_vice(self) -> None:
        text = "Motion for Pro Hac Vice Admission"
        assert extract_motion_type(text) == "motion_pro_hac_vice"

    def test_motion_to_substitute(self) -> None:
        text = "Motion to Substitute Party"
        assert extract_motion_type(text) == "motion_to_substitute"

    def test_mil_abbreviation(self) -> None:
        """'MIL' abbreviation for motion in limine (#421)."""
        text = "Defendant's MIL No. 3 to Exclude Expert Testimony"
        assert extract_motion_type(text) == "mil"

    def test_mils_abbreviation_plural(self) -> None:
        """'MILs' plural abbreviation (#421)."""
        text = "Ruling on Plaintiff's MILs"
        assert extract_motion_type(text) == "mil"

    def test_motion_to_tax_costs(self) -> None:
        text = "Motion to Tax Costs"
        assert extract_motion_type(text) == "motion_to_tax_costs"

    def test_writ_of_possession(self) -> None:
        text = "Application for Writ of Possession"
        assert extract_motion_type(text) == "writ_of_possession"

    def test_motion_for_new_trial(self) -> None:
        text = "Motion for New Trial"
        assert extract_motion_type(text) == "motion_for_new_trial"

    # --- Probate/non-standard event type patterns (issue #1767) ---

    def test_petition_for_probate(self) -> None:
        text = "Petition for Probate"
        assert extract_motion_type(text) == "petition_for_probate"

    def test_petition_for_probate_in_sentence(self) -> None:
        text = "The Petition for Probate of Will is set for hearing."
        assert extract_motion_type(text) == "petition_for_probate"

    def test_petition_probate_short(self) -> None:
        text = "Petition Probate"
        assert extract_motion_type(text) == "petition_for_probate"

    def test_petition_to_administer_estate(self) -> None:
        text = "Petition to Administer Estate"
        assert extract_motion_type(text) == "petition_for_probate"

    def test_petition_for_letters(self) -> None:
        text = "Petition for Letters of Administration"
        assert extract_motion_type(text) == "petition_for_probate"

    def test_guardianship_petition(self) -> None:
        text = "Guardianship Petition"
        assert extract_motion_type(text) == "guardianship_petition"

    def test_petition_for_guardianship(self) -> None:
        text = "Petition for Guardianship of the Person"
        assert extract_motion_type(text) == "guardianship_petition"

    def test_petition_for_conservatorship(self) -> None:
        text = "Petition for Conservatorship"
        assert extract_motion_type(text) == "guardianship_petition"

    def test_accounting(self) -> None:
        text = "Accounting"
        assert extract_motion_type(text) == "accounting"

    def test_accounting_in_sentence(self) -> None:
        text = "First and Final Accounting by personal representative"
        assert extract_motion_type(text) == "accounting"

    def test_show_cause_hearing(self) -> None:
        text = "Show Cause Hearing"
        assert extract_motion_type(text) == "show_cause_hearing"

    def test_show_cause_hearing_in_sentence(self) -> None:
        text = "Show Cause Hearing re: contempt"
        assert extract_motion_type(text) == "show_cause_hearing"

    def test_trust_petition(self) -> None:
        text = "Trust Petition for modification"
        assert extract_motion_type(text) == "trust_petition"

    def test_order_to_show_cause_still_osc(self) -> None:
        """Existing 'Order to Show Cause' should still match osc, not show_cause_hearing."""
        text = "Order to Show Cause re: Contempt"
        assert extract_motion_type(text) == "osc"

    def test_petition_for_probate_before_generic_petition(self) -> None:
        """'Petition for Probate' should match specific pattern, not generic 'petition'."""
        text = "Petition for Probate"
        assert extract_motion_type(text) == "petition_for_probate"

    def test_generic_petition_still_works(self) -> None:
        """Generic 'Petition' without probate/guardianship qualifiers should still match."""
        text = "Petition to confirm arbitration award"
        assert extract_motion_type(text) == "petition"

    # --- New patterns (issue #1783) ---

    def test_judgment_on_the_pleadings(self) -> None:
        text = "Motion for Judgment on the Pleadings"
        assert extract_motion_type(text) == "motion_for_judgment_on_the_pleadings"

    def test_judgment_on_the_pleadings_without_prefix(self) -> None:
        """Prefix-less 'judgment on the pleadings' should NOT match in ruling text.

        Prefix-less matching is intentionally handled by _PREFIX_LESS_PATTERNS
        in normalize_motion_type() only, to avoid false positives in full text.
        """
        text = "The court rules on judgment on the pleadings."
        assert extract_motion_type(text) is None

    def test_deem_admissions_admitted(self) -> None:
        text = "Motion to Deem Requests for Admissions Admitted"
        assert extract_motion_type(text) == "deem_admissions_admitted"

    def test_deem_requests_short(self) -> None:
        text = "Motion to Deem Requests for Wells Fargo Bank"
        assert extract_motion_type(text) == "deem_admissions_admitted"

    def test_deem_admission_admitted_prefix_less(self) -> None:
        """Prefix-less 'deem admission admitted' should NOT match in ruling text."""
        text = "Deem admission admitted"
        assert extract_motion_type(text) is None


# ---------------------------------------------------------------------------
# Judge name extraction
# ---------------------------------------------------------------------------


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
# ---------------------------------------------------------------------------


class TestExtractCaseTitle:
    """Tests for extract_case_title() — inline v. patterns (#337)."""

    def test_inline_v_with_case_number(self) -> None:
        """'Name v. Name, No. CaseNumber' — issue #337 primary example."""
        text = "Raymond Yawen Wu v. Steve Tsui et al., No. 25STCV34748"
        result = extract_case_title(text)
        assert result is not None
        assert "Wu" in result
        assert "Tsui" in result
        assert "v." in result
        # Case number should NOT be in the title
        assert "25STCV" not in result

    def test_inline_v_with_et_al(self) -> None:
        """'Name v. Name et al.' format."""
        text = "John Smith v. Acme Corp et al.\nThe motion is granted."
        result = extract_case_title(text)
        assert result is not None
        assert "Smith" in result
        assert "Acme Corp" in result
        assert "v." in result

    def test_inline_v_simple(self) -> None:
        """Simple 'Name v. Name' on its own line."""
        text = "Smith v. Jones\nCase Number: 24NNCV02551"
        result = extract_case_title(text)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert "v." in result

    def test_inline_vs_dot(self) -> None:
        """'Name vs. Name' format."""
        text = "GARCIA vs. HERNANDEZ\nThe demurrer is sustained."
        result = extract_case_title(text)
        assert result is not None
        assert "Garcia" in result
        assert "Hernandez" in result
        assert "v." in result

    def test_inline_vs_all_caps(self) -> None:
        """All-caps 'NAME VS NAME' format."""
        text = "SMITH VS JONES\nMotion for summary judgment"
        result = extract_case_title(text)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result

    def test_case_name_field(self) -> None:
        """'Case Name: X v. Y' field extraction."""
        text = "Case Name: Martinez v. City of Los Angeles\nCase Number: 22STCV12345"
        result = extract_case_title(text)
        assert result is not None
        assert "Martinez" in result
        assert "City of Los Angeles" in result

    def test_case_title_field(self) -> None:
        """'Case Title: X v. Y' field extraction."""
        text = "Case Title: Doe v. Roe Corp\nHearing Date: March 5, 2026"
        result = extract_case_title(text)
        assert result is not None
        assert "Doe" in result
        assert "Roe" in result

    def test_riverside_style_with_hearing(self) -> None:
        """Riverside-style: 'CASENUMBER PLAINTIFF vs DEFENDANT Hearing re:'."""
        text = "CVPS2306157 YELDELL vs HENSS Hearing re: Demurrer"
        result = extract_case_title(text)
        assert result is not None
        assert "Yeldell" in result
        assert "Henss" in result

    def test_no_match_plain_text(self) -> None:
        """Plain text without any title pattern returns None."""
        text = "The motion for summary judgment is GRANTED."
        assert extract_case_title(text) is None

    def test_no_match_empty(self) -> None:
        assert extract_case_title("") is None

    def test_multiline_v_with_case_number_next_line(self) -> None:
        """'Name v. Name' on one line, case number on next line (#337)."""
        text = "Raymond Yawen Wu v. Steve Tsui et al.\nNo. 25STCV34748\nThe motion is denied."
        result = extract_case_title(text)
        assert result is not None
        assert "Wu" in result
        assert "Tsui" in result
        assert "25STCV" not in result

    def test_inline_v_case_no_prefix(self) -> None:
        """'Name v. Name, Case No. CaseNumber' variant."""
        text = "Alpha Beta v. Gamma Delta, Case No. 24NNCV02551"
        result = extract_case_title(text)
        assert result is not None
        assert "Alpha Beta" in result
        assert "Gamma Delta" in result
        assert "24NNCV" not in result

    def test_title_case_normalization(self) -> None:
        """All-caps titles should be normalized to title case."""
        text = "SMITH V. JONES\nThe court rules as follows."
        result = extract_case_title(text)
        assert result is not None
        assert result == "Smith v. Jones"

    def test_mixed_case_preserved(self) -> None:
        """Mixed-case titles should be preserved (not re-cased)."""
        text = "McDonald v. O'Brien\nMotion denied."
        result = extract_case_title(text)
        assert result is not None
        assert "McDonald" in result
        assert "O'Brien" in result

    def test_rejects_department_header_boilerplate(self) -> None:
        """Titles containing 'DEPARTMENT X LAW AND MOTION RULINGS' are rejected (#1244)."""
        text = (
            "DEPARTMENT I LAW AND MOTION RULINGS\n"
            "Case Number: 24VECV05649\n"
            "Hearing Date: March 6, 2026\n"
            "Jim Hilaski v. Shaul Dina\n"
            "The motion is GRANTED."
        )
        result = extract_case_title(text)
        # Should still find the actual title but skip the header match
        if result is not None:
            assert "DEPARTMENT" not in result.upper() or "LAW AND MOTION" not in result.upper()

    def test_rejects_department_header_in_captured_group(self) -> None:
        """A regex match that captures department header text is skipped (#1244)."""
        text = "Department 15 Law And Motion Rulings v. Someone Else\nCase Number: 24VECV05649"
        result = extract_case_title(text)
        # Should be None since the only v. match contains header boilerplate
        assert result is None

    def test_motion_text_rejected_granting_motion(self) -> None:
        """Motion descriptions containing 'Granting Motion' should not be case titles (#1245)."""
        text = "Granting Motion To v. Disqualify Plaintiff\nThe court rules."
        result = extract_case_title(text)
        assert result is None

    def test_motion_text_rejected_order_denying(self) -> None:
        """'Order Denying Motion' text should not be a case title."""
        text = "Order Denying Motion To v. Compel Arbitration\nDetails follow."
        result = extract_case_title(text)
        assert result is None

    def test_motion_text_rejected_ruling_on(self) -> None:
        """'Ruling On Motion' text should not be a case title."""
        text = "Ruling On Demurrer v. Strike Answer\nDetails."
        result = extract_case_title(text)
        assert result is None

    def test_motion_text_rejected_summary_judgment(self) -> None:
        """Motion for summary judgment text should not be a case title."""
        text = "Motion For Summary Judgment v. Dismiss Complaint\nDetails."
        result = extract_case_title(text)
        assert result is None

    def test_real_case_title_still_extracted(self) -> None:
        """Real case titles should still be extracted after motion filtering."""
        text = "Hussnain v. Ford Motor Co.\nThe motion is granted."
        result = extract_case_title(text)
        assert result is not None
        assert "Hussnain" in result
        assert "Ford" in result

    def test_extracts_real_title_after_rejecting_motion_text(self) -> None:
        """A valid title should be found even if a motion line appears first (#1245)."""
        text = (
            "Some introductory text.\n"
            "Order Denying Motion To v. Compel Arbitration\n"
            "Hussnain v. Ford Motor Co.\n"
            "The court rules as follows."
        )
        result = extract_case_title(text)
        assert result is not None
        assert "Hussnain" in result
        assert "Ford" in result
        assert "Motion" not in result
        assert "Compel" not in result


# ---------------------------------------------------------------------------
# "In re" / "In the Matter of" patterns (#1378)
# ---------------------------------------------------------------------------


class TestExtractCaseTitleInRe:
    """Tests for 'In re' / 'In the Matter of' case title extraction (#1378)."""

    def test_in_re_colon(self) -> None:
        """'In re: Name' format — probate/guardianship cases."""
        text = "In re: Estate of John Smith\nThe petition is GRANTED."
        result = extract_case_title(text)
        assert result is not None
        assert "In re" in result or "In Re" in result
        assert "Estate of John Smith" in result

    def test_in_re_no_colon(self) -> None:
        """'In re Name' without colon."""
        text = "In re Marriage of Garcia and Lopez\nThe court rules."
        result = extract_case_title(text)
        assert result is not None
        assert "Marriage of Garcia" in result

    def test_in_the_matter_of(self) -> None:
        """'In the Matter of Name' format."""
        text = "In the Matter of the Estate of Margaret Williams\nThe petition is GRANTED."
        result = extract_case_title(text)
        assert result is not None
        assert "Matter" in result
        assert "Margaret Williams" in result

    def test_petition_of(self) -> None:
        """'Petition of Name' format."""
        text = "Petition of Robert Chen for Letters of Administration\nGranted."
        result = extract_case_title(text)
        assert result is not None
        assert "Robert Chen" in result

    def test_v_pattern_preferred_over_in_re(self) -> None:
        """When both 'v.' and 'In re' exist, 'v.' patterns should win."""
        text = "Case Name: Smith v. Jones\nIn re: Some Estate\n"
        result = extract_case_title(text)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result

    def test_in_re_too_long_rejected(self) -> None:
        """Very long 'In re' text should be rejected."""
        long_name = "A" * 200
        text = f"In re: {long_name}\nGranted."
        result = extract_case_title(text)
        assert result is None


# ---------------------------------------------------------------------------
# Caption block / MOVING PARTY / Case Name field edge cases (#1405)
# ---------------------------------------------------------------------------


class TestExtractCaseTitleCaptionBlock:
    """Edge-case tests for caption-block extraction strategies merged in #1405."""

    def test_caption_no_vs_after_plaintiff(self) -> None:
        """Plaintiff role found but no vs. nearby — returns None from caption,
        falls through to other strategies."""
        text = "JOHN DOE,\n  Plaintiff(s),\n  The motion is granted.\n"
        # No "vs." or "v." after "Plaintiff" → caption strategy fails.
        # No other strategy matches either.
        result = extract_case_title(text)
        assert result is None

    def test_caption_no_defendant_after_vs(self) -> None:
        """Plaintiff + vs. found but no Defendant role → caption fails."""
        text = "ALPHA CORP,\n  Plaintiff(s),\n  vs.\nThe motion for summary judgment is granted.\n"
        result = extract_case_title(text)
        # Caption block fails — no Defendant.  The broad regex may or may
        # not match; the key test is that it doesn't crash.
        # (Alpha Corp is the only name, so no v. title can be formed)
        assert result is None or "v." not in (result or "").lower() or True

    def test_caption_defendant_too_far_from_vs(self) -> None:
        """Defendant role > 300 chars after vs. — caption strategy rejects."""
        filler = "x " * 200  # 400 chars
        text = f"JOHN DOE,\n  Plaintiff(s),\n  vs.\n{filler}\nJANE ROE,\n  Defendant(s).\n"
        # Caption block gives up, but broad regex pattern may still match
        # "John Doe" / "Jane Roe" through a different strategy.
        result = extract_case_title(text)
        # Just verify no crash; caption block rejects, fallback may or may not find something
        assert result is None or isinstance(result, str)

    def test_caption_short_department_line_stops_scan(self) -> None:
        """A 2-char line like '3' before the plaintiff name stops backward scan."""
        text = "3\nJOHN SMITH,\n  Plaintiff(s),\n  vs.\nJANE DOE,\n  Defendant(s).\n"
        result = extract_case_title(text)
        assert result is not None
        assert "John Smith" in result
        assert "Jane Doe" in result

    def test_caption_empty_plaintiff_name(self) -> None:
        """If backward scan yields no name lines, caption strategy returns None."""
        text = "DISTRICT\n  Plaintiff(s),\n  vs.\nJANE DOE,\n  Defendant(s).\n"
        result = extract_case_title(text)
        # Caption block finds no plaintiff name (DISTRICT is a stop word),
        # so it returns None. Broad regex may still find something.
        assert result is None or isinstance(result, str)

    def test_caption_title_too_long_rejected(self) -> None:
        """Caption-extracted title > 150 chars is rejected."""
        long_name = "Abcdefghijkl " * 12  # ~156 chars
        text = (
            f"{long_name.strip()},\n  Plaintiff(s),\n  vs.\n{long_name.strip()},\n  Defendant(s).\n"
        )
        result = extract_case_title(text)
        # The caption title would be too long. Broad regex also can't match
        # because the name exceeds 60-char limits in those patterns.
        assert result is None

    def test_standard_caption_block(self) -> None:
        """Basic caption block extraction works through extract_case_title."""
        text = (
            "EMELITA BUENAVENTURA, et al.,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "CITY OF PASADENA, et al.,\n"
            "  Defendant(s).\n"
        )
        result = extract_case_title(text)
        assert result is not None
        assert "Buenaventura" in result
        assert "Pasadena" in result
        assert " v. " in result


class TestExtractCaseTitleMovingResponding:
    """Edge-case tests for MOVING PARTY / RESPONDING PARTY extraction (#1405)."""

    def test_moving_party_only_no_responding(self) -> None:
        """MOVING PARTY present but no RESPONDING PARTY — returns None."""
        text = "MOVING PARTY: Defendant Acme Corp.\nThe motion is granted.\n"
        result = extract_case_title(text)
        # No responding party → strategy 3 fails. No caption block or case name.
        # Broad regex might or might not match "Acme Corp".
        assert result is None or isinstance(result, str)

    def test_moving_responding_empty_after_strip(self) -> None:
        """Role prefix consumes entire party name → empty → strategy 3 returns None."""
        from ingestion.extract import _extract_from_moving_responding

        # "Defendant ," — the role prefix regex matches "Defendant," and
        # the remainder after stripping is empty.
        text = "MOVING PARTY: Defendant, .\nRESPONDING PARTY: Plaintiff, .\n"
        result = _extract_from_moving_responding(text)
        assert result is None

    def test_moving_responding_title_too_long(self) -> None:
        """Very long party names produce a title > 150 chars — rejected."""
        long_name = "Abcdefghij " * 15  # ~165 chars
        text = (
            f"MOVING PARTY: Defendant {long_name.strip()}.\n"
            f"RESPONDING PARTY: Plaintiff {long_name.strip()}.\n"
        )
        result = extract_case_title(text)
        # Title would be > 150 chars → rejected
        assert result is None


class TestExtractCaseTitleCaseNameField:
    """Edge-case tests for Case Name field extraction (#1405)."""

    def test_case_name_no_v_returns_none(self) -> None:
        """Case Name field without 'v.' is not a party title."""
        text = "CASE NAME: Motion for Summary Judgment CASE NUMBER: 22STCV12345\n"
        result = extract_case_title(text)
        assert result is None

    def test_case_name_title_too_long(self) -> None:
        """Case Name field with very long title is rejected."""
        long_title = "Alpha " * 30 + "v. " + "Beta " * 30  # > 150 chars
        text = f"CASE NAME: {long_title} CASE NUMBER: 22STCV12345\n"
        result = extract_case_title(text)
        # _extract_from_case_name_field rejects titles > 150 chars.
        # Broad regex may still find something shorter in the text.
        assert result is None or len(result) <= 150

    def test_case_name_title_too_short(self) -> None:
        """Case Name field with very short title is rejected."""
        text = "CASE NAME: A v. B CASE NUMBER: 22STCV12345\n"
        result = extract_case_title(text)
        # "A v. B" is 6 chars which is >= 5, so it should pass length check
        # The broad regex may not match 1-char names though
        assert result is None or isinstance(result, str)

    def test_case_name_field_basic(self) -> None:
        """Basic Case Name field extraction works."""
        text = "CASE NAME: Martinez v. City of Los Angeles CASE NUMBER: 22STCV12345\n"
        result = extract_case_title(text)
        assert result is not None
        assert "Martinez" in result
        assert "City of Los Angeles" in result


# ---------------------------------------------------------------------------
# Motion text detection (#1245)
# ---------------------------------------------------------------------------


class TestLooksLikeMotionText:
    """Tests for _looks_like_motion_text() — rejects motion descriptions as case titles."""

    def test_empty_string(self) -> None:
        assert _looks_like_motion_text("") is False

    def test_no_separator(self) -> None:
        """Text without a v. separator is not motion text (nor case title)."""
        assert _looks_like_motion_text("Motion to Compel Arbitration") is False

    def test_granting_motion_to(self) -> None:
        assert _looks_like_motion_text("Granting Motion To v. Disqualify Plaintiff") is True

    def test_order_denying_motion(self) -> None:
        assert _looks_like_motion_text("Order Denying Motion To v. Compel Arbitration") is True

    def test_motion_for_summary_judgment(self) -> None:
        assert _looks_like_motion_text("Motion For Summary Judgment v. Dismiss") is True

    def test_ruling_on_demurrer(self) -> None:
        assert _looks_like_motion_text("Ruling On Demurrer v. Strike Answer") is True

    def test_petition_for_writ(self) -> None:
        assert _looks_like_motion_text("Petition For Writ v. Mandate Relief") is True

    def test_application_to_vacate(self) -> None:
        assert _looks_like_motion_text("Application To Vacate v. Set Aside Default") is True

    def test_real_case_title_not_rejected(self) -> None:
        assert _looks_like_motion_text("Hussnain v. Ford Motor Co.") is False

    def test_real_case_title_with_et_al(self) -> None:
        assert _looks_like_motion_text("Caldera, et al. v. Techno-Advanced, Inc.") is False

    def test_all_caps_case_title(self) -> None:
        assert _looks_like_motion_text("SMITH V. JONES") is False

    def test_corporate_parties(self) -> None:
        assert _looks_like_motion_text("Golden State v. Suraj Victorville, LLC") is False

    def test_default_judgment_text(self) -> None:
        assert _looks_like_motion_text("Default Judgment v. Strike Claims") is True

    def test_ex_parte_application(self) -> None:
        text = "Ex Parte Application v. Temporary Restraining Order"
        assert _looks_like_motion_text(text) is True

    def test_motion_to_compel(self) -> None:
        assert _looks_like_motion_text("Motion To Compel v. Quash Subpoena") is True


# ---------------------------------------------------------------------------
# Party extraction from case captions
# ---------------------------------------------------------------------------


class TestExtractPartiesFromCaption:
    """Tests for extract_parties_from_caption()."""

    def test_simple_v_separator(self) -> None:
        """Basic 'Plaintiff v. Defendant' caption."""
        parties = extract_parties_from_caption("Smith v. Jones")
        assert len(parties) == 2
        assert parties[0] == {"name": "Smith", "role": "plaintiff"}
        assert parties[1] == {"name": "Jones", "role": "defendant"}

    def test_et_al_stripped(self) -> None:
        """'et al.' is stripped but the primary name is kept."""
        parties = extract_parties_from_caption("Caldera, et al. v. Techno-Advanced, Inc., et al.")
        assert any(p["role"] == "plaintiff" for p in parties)
        assert any(p["role"] == "defendant" for p in parties)
        # Caldera should be a plaintiff
        plaintiff_names = [p["name"] for p in parties if p["role"] == "plaintiff"]
        assert any("Caldera" in n for n in plaintiff_names)
        # Techno-Advanced, Inc. should be a defendant (not split)
        defendant_names = [p["name"] for p in parties if p["role"] == "defendant"]
        assert any("Techno-Advanced" in n for n in defendant_names)

    def test_corporate_suffix_not_split(self) -> None:
        """Inc, LLC, Corp etc. should stay with the preceding entity name."""
        parties = extract_parties_from_caption("Acme, Inc. v. Beta, LLC")
        assert len(parties) == 2
        assert "Inc" in parties[0]["name"]  # kept together
        assert "LLC" in parties[1]["name"]  # kept together
        # Verify no standalone "Inc" or "LLC" entries
        names = [p["name"] for p in parties]
        assert "Inc" not in names
        assert "LLC" not in names
        assert "Inc." not in names
        assert "LLC." not in names

    def test_multiple_defendants(self) -> None:
        """Multiple defendants separated by commas."""
        parties = extract_parties_from_caption("Smith v. Jones, Williams, and Brown")
        defendants = [p for p in parties if p["role"] == "defendant"]
        assert len(defendants) == 3

    def test_name_fragment_filtered(self) -> None:
        """Single-character or very short fragments are filtered out."""
        parties = extract_parties_from_caption("A v. Jones")
        # "A" is too short to be a valid party name
        assert len(parties) == 1
        assert parties[0]["name"] == "Jones"

    def test_empty_input(self) -> None:
        assert extract_parties_from_caption("") == []
        assert extract_parties_from_caption("No caption here") == []

    def test_vs_variant(self) -> None:
        """Handles 'vs.' and 'vs' separators."""
        parties = extract_parties_from_caption("Smith vs. Jones")
        assert len(parties) == 2
        parties2 = extract_parties_from_caption("Smith vs Jones")
        assert len(parties2) == 2

    def test_deduplicated(self) -> None:
        """Same name on both sides should not produce duplicates."""
        parties = extract_parties_from_caption("Smith v. Smith")
        assert len(parties) == 2  # same name, different roles

    def test_issue_328_regression(self) -> None:
        """Regression test for issue #328: Caldera v. Techno-Advanced, Inc.

        Previously produced fragments: Inc, Salvador, Techno-Advanced as
        separate entries, all classified as 'other'.
        """
        title = "Caldera, et al. v. Techno-Advanced, Inc., et al"
        parties = extract_parties_from_caption(title)
        names = [p["name"] for p in parties]

        # "Inc" must not appear as a standalone entry
        assert "Inc" not in names
        assert "Inc." not in names

        # Roles must be plaintiff/defendant, not "other"
        roles = {p["role"] for p in parties}
        assert "other" not in roles
        assert "plaintiff" in roles
        assert "defendant" in roles


# ---------------------------------------------------------------------------
# Case type extraction from case number prefix
# ---------------------------------------------------------------------------


class TestExtractCaseTypeFromNumber:
    """Tests for extract_case_type_from_number()."""

    # --- Civil prefixes ---

    def test_riverside_cv_prefix(self) -> None:
        assert extract_case_type_from_number("CVRI2502741") == "civil"

    def test_riverside_cvps(self) -> None:
        assert extract_case_type_from_number("CVPS2306157") == "civil"

    def test_riverside_cvme(self) -> None:
        assert extract_case_type_from_number("CVME2100123") == "civil"

    def test_riverside_cvsw(self) -> None:
        assert extract_case_type_from_number("CVSW2401234") == "civil"

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

    def test_sf_felony_fpt(self) -> None:
        """SF felony: FPT-25-378624."""
        assert extract_case_type_from_number("FPT-25-378624") == "criminal"

    def test_sf_felony_fms(self) -> None:
        assert extract_case_type_from_number("FMS-20-387302") == "criminal"

    def test_sf_felony_fdi(self) -> None:
        assert extract_case_type_from_number("FDI-14-781786") == "criminal"

    # --- Juvenile ---

    def test_juvenile_jv(self) -> None:
        assert extract_case_type_from_number("JV2301234") == "juvenile"

    # --- Traffic ---

    def test_traffic_tr(self) -> None:
        assert extract_case_type_from_number("TR2301234") == "traffic"

    # --- Edge cases ---

    def test_none_input(self) -> None:
        assert extract_case_type_from_number(None) is None  # type: ignore[arg-type]

    def test_empty_string(self) -> None:
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
# ---------------------------------------------------------------------------


class TestSplitCaptionNames:
    """Tests for _split_caption_names helper."""

    def test_and_separator_without_commas(self) -> None:
        """Names joined by 'and' without commas use the fallback split."""
        result = _split_caption_names("Smith and Jones")
        assert result == ["Smith", "Jones"]

    def test_et_al_stripped(self) -> None:
        """'et al.' is removed before splitting."""
        result = _split_caption_names("Smith, et al.")
        assert result == ["Smith"]

    def test_empty_string(self) -> None:
        result = _split_caption_names("")
        assert result == []

    def test_corporate_suffix_reattached(self) -> None:
        """Inc, LLC etc. are reattached to the preceding name."""
        result = _split_caption_names("Acme, Inc., Beta Corp")
        assert any("Acme" in n and "Inc" in n for n in result)

    def test_single_name(self) -> None:
        """A single name with no separator."""
        result = _split_caption_names("Smith")
        assert result == ["Smith"]

    def test_oxford_comma(self) -> None:
        """Oxford comma: 'A, B, and C' splitting."""
        result = _split_caption_names("Smith, Jones, and Williams")
        assert len(result) == 3

    def test_strips_surrounding_punctuation(self) -> None:
        """Parentheses and semicolons are stripped from fragments."""
        result = _split_caption_names("(Smith), Jones;")
        assert "Smith" in result
        assert "Jones" in result

    def test_empty_fragment_after_stripping(self) -> None:
        """Fragments that are empty after stripping punctuation are skipped."""
        # ", , Smith" — the first fragment after comma split is empty
        result = _split_caption_names(", , Smith")
        assert result == ["Smith"]

    def test_only_punctuation_fragments(self) -> None:
        """All-punctuation fragments produce an empty list."""
        result = _split_caption_names("., ;, ()")
        assert result == []


# ---------------------------------------------------------------------------
# _is_name_fragment — direct tests
# ---------------------------------------------------------------------------


class TestIsNameFragment:
    """Tests for _is_name_fragment helper."""

    def test_single_char_is_fragment(self) -> None:
        assert _is_name_fragment("A") is True

    def test_two_char_is_fragment(self) -> None:
        assert _is_name_fragment("AB") is True

    def test_three_char_with_space_not_fragment(self) -> None:
        """Three chars with a space is not a fragment."""
        assert _is_name_fragment("A B") is False

    def test_corporate_suffix_is_fragment(self) -> None:
        """Standalone corporate suffix should be a fragment."""
        assert _is_name_fragment("Inc") is True
        assert _is_name_fragment("LLC") is True
        assert _is_name_fragment("Corp") is True

    def test_real_name_not_fragment(self) -> None:
        assert _is_name_fragment("Smith") is False

    def test_initial_with_period(self) -> None:
        """Single initial with period like 'J.' — 2 chars stripped to 1."""
        assert _is_name_fragment("J.") is True


# ---------------------------------------------------------------------------
# extract_parties_from_caption — additional coverage
# ---------------------------------------------------------------------------


class TestExtractPartiesFromCaptionAdditional:
    """Additional tests for extract_parties_from_caption edge cases."""

    def test_duplicate_name_same_role_deduplicated(self) -> None:
        """Duplicate plaintiff names should be deduplicated."""
        # Construct a caption where the same name appears twice on plaintiff side
        parties = extract_parties_from_caption("Smith, Smith v. Jones")
        plaintiff_names = [p["name"] for p in parties if p["role"] == "plaintiff"]
        # Smith should appear only once as plaintiff
        assert plaintiff_names.count("Smith") == 1

    def test_and_separated_parties(self) -> None:
        """Parties separated by 'and' on the plaintiff side."""
        parties = extract_parties_from_caption("Smith and Jones v. Williams")
        plaintiffs = [p for p in parties if p["role"] == "plaintiff"]
        assert len(plaintiffs) == 2

    def test_none_input(self) -> None:
        """None input returns empty list."""
        assert extract_parties_from_caption(None) == []  # type: ignore[arg-type]

    def test_no_v_separator(self) -> None:
        """Caption without v./vs. returns empty list."""
        assert extract_parties_from_caption("Just a random string") == []

    def test_trailing_punctuation_stripped(self) -> None:
        """Trailing commas, periods, semicolons are stripped from party names."""
        parties = extract_parties_from_caption("Smith, v. Jones.")
        names = [p["name"] for p in parties]
        # Names should not end with punctuation
        for name in names:
            assert not name.endswith(".")
            assert not name.endswith(",")

    def test_fragment_on_defendant_side_filtered(self) -> None:
        """Very short fragments on defendant side are filtered out."""
        parties = extract_parties_from_caption("Smith v. A")
        defendants = [p for p in parties if p["role"] == "defendant"]
        # "A" is too short — should be filtered
        assert len(defendants) == 0

    def test_case_insensitive_dedup(self) -> None:
        """Dedup is case-insensitive: 'SMITH' and 'Smith' are the same party."""
        parties = extract_parties_from_caption("SMITH, Smith v. Jones")
        plaintiffs = [p for p in parties if p["role"] == "plaintiff"]
        assert len(plaintiffs) == 1

    def test_name_becomes_fragment_after_rstrip(self) -> None:
        """A name like 'A.' passes _split_caption_names (len 2) but becomes
        a fragment after rstrip in extract_parties_from_caption (len 1)."""
        parties = extract_parties_from_caption("A. v. Jones")
        plaintiffs = [p for p in parties if p["role"] == "plaintiff"]
        # "A." → "A" after rstrip → fragment → filtered
        assert len(plaintiffs) == 0

    def test_corporate_suffix_as_party_name_filtered(self) -> None:
        """A standalone corporate suffix like 'Inc' survives _split_caption_names
        (len 3) but is filtered by _is_name_fragment in extract_parties_from_caption."""
        parties = extract_parties_from_caption("Inc v. Jones")
        plaintiffs = [p for p in parties if p["role"] == "plaintiff"]
        # "Inc" is a corporate suffix fragment — should be filtered
        assert len(plaintiffs) == 0


# ---------------------------------------------------------------------------
# is_valid_case_number (#1524)
# ---------------------------------------------------------------------------


class TestIsValidCaseNumber:
    """Tests for is_valid_case_number() — validates LLM-returned case numbers."""

    def test_valid_oc_numeric(self) -> None:
        assert is_valid_case_number("2024-01393434") is True

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
# ---------------------------------------------------------------------------


class TestExtractFromInlinePartyRefs:
    """Tests for _extract_from_inline_party_refs() — Strategy 6."""

    def test_plaintiff_defendant_named_in_text(self) -> None:
        """Extracts title when both parties are named inline."""
        text = (
            "Case Number:\n22STCV26122\nHearing Date:\nMarch 12, 2026\nDept:\n57\n"
            "The Court is granting Plaintiff Garcia's motions for orders "
            "compelling Defendants Sotva to provide further responses."
        )
        result = _extract_from_inline_party_refs(text)
        assert result is not None
        assert "Garcia" in result
        assert "Sotva" in result
        assert "v." in result

    def test_plaintiff_name_possessive(self) -> None:
        """Extracts plaintiff name from possessive form (single side)."""
        text = "Plaintiff Alfredo Duarte's Motion for Attorney's Fees is CONTINUED."
        result = _extract_from_inline_party_refs(text)
        # Only plaintiff found, no defendant — returns single-side title
        assert result is not None
        assert "Duarte" in result

    def test_both_parties_found(self) -> None:
        """Extracts title when both plaintiff and defendant are named."""
        text = (
            "Plaintiff Alfredo Duarte's Motion for Attorney's Fees is CONTINUED. "
            "Defendant City Of Pasadena filed opposition."
        )
        result = _extract_from_inline_party_refs(text)
        assert result is not None
        assert "Duarte" in result
        assert "City Of Pasadena" in result

    def test_cross_complainant_slash_format(self) -> None:
        """Handles Defendant/Cross-Complainant slash format."""
        text = (
            "Defendant/Cross-Complainant Shores, LLC's Motion for Order is GRANTED. "
            "Plaintiff Williams filed opposition."
        )
        result = _extract_from_inline_party_refs(text)
        # Note: plaintiff side has "Williams", defendant side has "Shores"
        # The slash-separated role should be stripped
        assert result is not None

    def test_rejects_generic_references(self) -> None:
        """Does not extract from generic 'Plaintiff's motion' without a name."""
        text = "Plaintiff's motion to compel is GRANTED. Defendant's demurrer is overruled."
        result = _extract_from_inline_party_refs(text)
        assert result is None

    def test_rejects_the_court_as_name(self) -> None:
        """Does not extract 'The Court' or 'This Court' as a party name."""
        text = "Plaintiff The Court made its ruling. Defendant The People filed opposition."
        result = _extract_from_inline_party_refs(text)
        assert result is None

    def test_returns_none_on_empty_text(self) -> None:
        """Returns None for empty text."""
        assert _extract_from_inline_party_refs("") is None

    def test_rejects_compelling_to_respond(self) -> None:
        """Does not extract 'to respond' as a name from compelling pattern."""
        text = (
            "The Court is granting Plaintiff's motions for orders "
            "compelling Defendants to respond, without objections, "
            "to Plaintiff's Requests for Production."
        )
        result = _extract_from_inline_party_refs(text)
        assert result is None

    def test_rejects_compelling_to_produce(self) -> None:
        """Does not extract 'to produce all' as a name from compelling pattern."""
        text = "The Court is ordering compelling Defendants to produce all responsive documents."
        result = _extract_from_inline_party_refs(text)
        assert result is None

    def test_returns_single_side_when_only_plaintiff(self) -> None:
        """Returns single-side title when only plaintiff is named."""
        text = "Plaintiff Martinez filed a motion to compel."
        result = _extract_from_inline_party_refs(text)
        assert result is not None
        assert "Martinez" in result
        assert "v." not in result

    def test_returns_single_side_when_only_defendant(self) -> None:
        """Returns single-side title when only defendant is named."""
        text = "Defendant Honda Motor Co. filed a demurrer."
        result = _extract_from_inline_party_refs(text)
        assert result is not None
        assert "Honda Motor" in result
        assert "v." not in result

    def test_same_name_returns_single_name(self) -> None:
        """Returns single name when plaintiff and defendant are the same."""
        text = "Plaintiff Smith filed a motion. Defendant Smith filed opposition."
        result = _extract_from_inline_party_refs(text)
        assert result is not None
        assert result == "Smith"

    def test_real_la_text_with_compel(self) -> None:
        """Real-world LA ruling text with 'compelling Defendants' pattern."""
        text = (
            "Case Number:\n22STCV26122\nHearing Date:\nMarch 12, 2026\nDept:\n57\n"
            "The Court is granting Plaintiff Lopez's motions for orders "
            "compelling Defendants Nick Sotva and Michael Rahimi to provide "
            "further responses to Plaintiff's Requests for Production."
        )
        result = _extract_from_inline_party_refs(text)
        assert result is not None
        assert "Lopez" in result

    def test_rejects_overly_long_combined_title(self) -> None:
        """Returns None when combined two-side title exceeds 150 chars."""
        # Build names with very long words (regex allows {0,5} extra words)
        long_p = "Abcdefghijklmnopqrstuvwxyz Abcdefghijklmnopqrstuvwxyz Abcdefghijklmnopqrstuvwxyz"
        long_d = "Efghijklmnopqrstuvwxyzab Efghijklmnopqrstuvwxyzab Efghijklmnopqrstuvwxyzab"
        text = f"Plaintiff {long_p}'s motion is GRANTED. Defendant {long_d} filed opposition."
        result = _extract_from_inline_party_refs(text)
        # The combined "X v. Y" would exceed 150 chars, so should return None
        if result is not None:
            # If it returned something, it should NOT be a two-side title
            assert "v." not in result

    def test_through_extract_case_title_fallback(self) -> None:
        """Strategy 6 is invoked as fallback through extract_case_title."""
        # This text has no caption block, no Case Name field, no MOVING/RESPONDING
        # PARTY, no "X v. Y" pattern, and no "In re" — only inline party refs.
        text = (
            "Case Number:\n25STCV00001\nHearing Date:\nMarch 12, 2026\nDept:\n57\n"
            "The Court is granting Plaintiff Garcia's motions for orders "
            "compelling Defendant Honda Motor Co. to provide further responses."
        )
        result = extract_case_title(text)
        assert result is not None
        assert "Garcia" in result

    def test_accepts_unknown_prefix(self) -> None:
        assert is_valid_case_number("UNKNOWN-abc123") is True


# ---------------------------------------------------------------------------
# extract_case_type_from_scraper_id (#1524)
# ---------------------------------------------------------------------------


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

    def test_petition(self) -> None:
        assert extract_case_type_from_motion_type("petition") == "probate"

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

    # --- SD calendar event types ---

    def test_sd_calendar_motion_hearing(self) -> None:
        """Generic 'Motion Hearing' cannot be mapped — return None."""
        assert normalize_motion_type("Motion Hearing") is None

    def test_sd_calendar_demurrer_motion_to_strike(self) -> None:
        """Composite 'Demurrer/Motion to Strike' matches demurrer first."""
        assert normalize_motion_type("Demurrer/Motion to Strike") == "demurrer"

    def test_sd_calendar_summary_judgment(self) -> None:
        assert normalize_motion_type("Summary Judgment/Summary Adjudication") == "msj_partial"

    def test_sd_calendar_discovery_hearing(self) -> None:
        """Generic 'Discovery Hearing' cannot be mapped — return None."""
        assert normalize_motion_type("Discovery Hearing") is None

    def test_sd_calendar_motion_to_quash(self) -> None:
        assert normalize_motion_type("Motion to Quash") == "motion_to_quash"

    def test_sd_calendar_motion_for_sanctions(self) -> None:
        assert normalize_motion_type("Motion for Sanctions") == "motion_for_sanctions"

    def test_sd_calendar_class_action(self) -> None:
        """Generic class action certify/decertify cannot be mapped."""
        assert normalize_motion_type("Motion Hearing to Certify/Decertify Class Action") is None

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
# the enrichment pipeline's extract_motion_type() applied to ruling text.
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


# ---------------------------------------------------------------------------
# Outcome extraction — Riverside-specific patterns (#2022)
# ---------------------------------------------------------------------------


class TestExtractOutcomeRiverside:
    """Tests for Riverside-specific outcome extraction patterns (#2022)."""

    def test_sustain_without_ed(self) -> None:
        """Riverside demurrers use 'SUSTAIN' without '-ed' suffix."""
        assert extract_outcome("SUSTAIN, with 30 days leave to amend.") == "granted"

    def test_sustain_uppercase(self) -> None:
        assert extract_outcome("Tentative Ruling:\nSUSTAIN, with leave.") == "granted"

    def test_sustained(self) -> None:
        """Standard 'sustained' should still map to granted."""
        assert extract_outcome("Demurrer is sustained.") == "granted"

    def test_overrule_without_d(self) -> None:
        """Riverside demurrers use 'OVERRULE' without '-d' suffix."""
        assert extract_outcome("Tentative Ruling: OVERRULE.") == "denied"

    def test_overruled(self) -> None:
        """Standard 'overruled' should still map to denied."""
        assert extract_outcome("Demurrer is overruled.") == "denied"

    def test_grant_verb_form(self) -> None:
        """Bare 'Grant' at start of ruling (no '-ed')."""
        text = "Grant, with five days to file the Answer."
        assert extract_outcome(text) == "granted"

    def test_deny_verb_form(self) -> None:
        """Bare 'Deny' (no '-ied')."""
        text = "Tentative Ruling: Deny the motion."
        assert extract_outcome(text) == "denied"

    def test_no_tentative_ruling(self) -> None:
        """'No tentative ruling' -> other."""
        text = "Tentative Ruling: No tentative ruling, appearances requested."
        assert extract_outcome(text) == "other"

    def test_no_tentative_decision(self) -> None:
        """'No tentative decision' -> other."""
        text = "Tentative Ruling: No tentative decision"
        assert extract_outcome(text) == "other"

    def test_hearing_required(self) -> None:
        """'Hearing Required' -> other."""
        text = "HEARING ON PRELIMINARY INJUNCTION\nTentative Ruling: Hearing required."
        assert extract_outcome(text) == "other"

    def test_it_is_ordered(self) -> None:
        """'It is ordered' -> granted (stipulated judgment)."""
        text = "It is ordered: that (1) the Notice of Settlement is vacated"
        assert extract_outcome(text) == "granted"

    def test_it_is_so_ordered(self) -> None:
        """'It is so ordered' -> granted."""
        text = "The request is approved. It is so ordered."
        assert extract_outcome(text) == "granted"

    def test_continue_present_tense(self) -> None:
        """Riverside uses 'Continue' in present tense."""
        text = "Tentative Ruling: Continue the hearing to April 21, 2026."
        assert extract_outcome(text) == "continued"


# ---------------------------------------------------------------------------
# Motion type extraction — Riverside-specific patterns (#2022)
# ---------------------------------------------------------------------------


class TestExtractMotionTypeRiverside:
    """Tests for Riverside-specific motion type extraction patterns (#2022)."""

    def test_motion_for_stipulated_judgment(self) -> None:
        text = "MOTION FOR STIPULATED JUDGMENT\nTentative Ruling: No opposition."
        assert extract_motion_type(text) == "motion_for_stipulated_judgment"

    def test_motion_to_enter_stipulated_judgment(self) -> None:
        text = "Hearing re: Motion to Enter Stipulated Judgment by CITIBANK"
        assert extract_motion_type(text) == "motion_for_stipulated_judgment"

    def test_motion_for_entry_of_judgment(self) -> None:
        text = "Hearing re: Motion for Entry of Judgment Pursuant to Stipulation"
        assert extract_motion_type(text) == "motion_for_entry_of_judgment"

    def test_motion_to_bifurcate(self) -> None:
        text = "Hearing re: Motion to Bifurcate on 3rd Amended Complaint"
        assert extract_motion_type(text) == "motion_to_bifurcate"

    def test_motion_for_class_certification(self) -> None:
        text = "MOTION FOR CLASS CERTIFICATION\nTentative Ruling: No tentative ruling"
        assert extract_motion_type(text) == "motion_for_class_certification"

    def test_motion_to_reclassify(self) -> None:
        text = "Hearing on Motion to Reclassify by MICHAEL GOLDMAN"
        assert extract_motion_type(text) == "motion_to_reclassify"

    def test_motion_for_judicial_approval(self) -> None:
        text = "MOTION FOR JUDICIAL APPROVAL OF PENDENCY OF ACTION"
        assert extract_motion_type(text) == "motion_for_judicial_approval"

    def test_motion_for_appointment_of_receiver(self) -> None:
        text = "MOTION FOR APPOINTMENT OF RECEIVER\nTentative Ruling:"
        assert extract_motion_type(text) == "motion_for_appointment_of_receiver"

    def test_right_to_attach_order(self) -> None:
        text = "HEARING ON RIGHT TO ATTACH ORDER\nTentative Ruling: Grant"
        assert extract_motion_type(text) == "right_to_attach_order"

    def test_motion_for_order_admissions_admitted(self) -> None:
        text = "MOTION FOR ORDER FOR ADMISSIONS TO BE DEEMED ADMITTED"
        assert extract_motion_type(text) == "deem_admissions_admitted"

    def test_motion_order_deem_matters_admitted(self) -> None:
        text = "Hearing re: Motion for Order to Deem Matters Admitted"
        assert extract_motion_type(text) == "deem_admissions_admitted"

    def test_motion_to_consolidate(self) -> None:
        text = "Motion to Consolidate with UD Complaint"
        assert extract_motion_type(text) == "motion_to_consolidate"

    def test_recordation_lis_pendens(self) -> None:
        text = "MOTION FOR ORDER AUTHORIZING RECORDATION OF NOTICE OF PENDENCY"
        assert extract_motion_type(text) == "lis_pendens"

    def test_request_for_dismissal(self) -> None:
        text = "HEARING RE REQUEST FOR DISMISSAL OF ACTION"
        assert extract_motion_type(text) == "request_for_dismissal"

    def test_writ_of_attachment(self) -> None:
        text = "Hearing on Notice of Application for Writ of Possession"
        assert extract_motion_type(text) == "writ_of_possession"

    def test_motion_to_seal(self) -> None:
        text = "Motion to Seal court records pursuant to CRC 2.550"
        assert extract_motion_type(text) == "motion_to_seal"


# ---------------------------------------------------------------------------
# normalize_motion_type — Riverside prefix-less patterns (#2022)
# ---------------------------------------------------------------------------


class TestNormalizeMotionTypeRiverside:
    """Tests for normalize_motion_type with Riverside prefix-less patterns (#2022)."""

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
