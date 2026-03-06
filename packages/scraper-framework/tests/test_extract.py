"""Tests for the basic regex-based outcome and motion_type extraction."""

from __future__ import annotations

from ingestion.extract import extract_motion_type, extract_outcome

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
