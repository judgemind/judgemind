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

    def test_no_false_positive_on_judge_word_in_ruling(self) -> None:
        """The word 'judge' in ruling body text should not trigger a match."""
        text = "The judge granted the motion."
        assert extract_judge_name(text) is None

    def test_no_false_positive_on_judicial_notice(self) -> None:
        """'judicial notice' should not trigger the JUDICIAL OFFICER pattern."""
        text = "The court takes judicial notice of the following."
        assert extract_judge_name(text) is None
