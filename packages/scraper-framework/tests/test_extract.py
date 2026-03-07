"""Tests for the basic regex-based outcome, motion_type, and judge name extraction."""

from __future__ import annotations

from ingestion.extract import extract_judge_name, extract_motion_type, extract_outcome

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
