"""Tests for deterministic validation rules.

Verifies each rule catches the expected failure patterns and that the
aggregation logic correctly computes the overall result.  Test cases
include real spotcheck findings from issue #2329.
"""

from __future__ import annotations

from datetime import date

from validation.deterministic import (
    check_case_number_not_unknown,
    check_hearing_date_in_range,
    check_no_concatenated_titles,
    check_no_html_in_ruling_text,
    check_ruling_text_not_empty,
    check_ruling_text_reasonable_length,
    run_deterministic_rules,
)

# ---------------------------------------------------------------------------
# no_html_in_ruling_text
# ---------------------------------------------------------------------------


class TestNoHtmlInRulingText:
    """Tests for the no_html_in_ruling_text rule."""

    def test_pass_normal_text(self) -> None:
        result = check_no_html_in_ruling_text("The motion for summary judgment is GRANTED.")
        assert result.result == "pass"
        assert result.rule == "no_html_in_ruling_text"

    def test_pass_none(self) -> None:
        result = check_no_html_in_ruling_text(None)
        assert result.result == "pass"

    def test_pass_empty(self) -> None:
        result = check_no_html_in_ruling_text("")
        assert result.result == "pass"

    def test_fail_html_tag(self) -> None:
        """LA ruling 65932ec6 — raw HTML stored as ruling text."""
        result = check_no_html_in_ruling_text("<html><head><title>LA Court</title></head><body>")
        assert result.result == "fail"
        assert "raw HTML detected" in (result.reason or "")

    def test_fail_doctype(self) -> None:
        result = check_no_html_in_ruling_text("<!DOCTYPE html><html><body>ruling text</body>")
        assert result.result == "fail"

    def test_fail_div_tag(self) -> None:
        result = check_no_html_in_ruling_text("<div class='ruling'>The motion is granted</div>")
        assert result.result == "fail"

    def test_fail_case_insensitive(self) -> None:
        result = check_no_html_in_ruling_text("<HTML><HEAD>")
        assert result.result == "fail"

    def test_fail_with_leading_whitespace(self) -> None:
        result = check_no_html_in_ruling_text("   <html><body>content</body>")
        assert result.result == "fail"

    def test_pass_html_in_middle(self) -> None:
        """HTML tags in the middle of ruling text are fine (e.g. formatted rulings)."""
        result = check_no_html_in_ruling_text("The court finds that <b>the motion</b> is granted.")
        assert result.result == "pass"


# ---------------------------------------------------------------------------
# hearing_date_in_range
# ---------------------------------------------------------------------------


class TestHearingDateInRange:
    """Tests for the hearing_date_in_range rule."""

    def test_pass_same_day(self) -> None:
        result = check_hearing_date_in_range(
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 5),
        )
        assert result.result == "pass"

    def test_pass_within_range(self) -> None:
        result = check_hearing_date_in_range(
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
        )
        assert result.result == "pass"

    def test_pass_at_boundary(self) -> None:
        """Exactly 180 days apart should still pass."""
        result = check_hearing_date_in_range(
            hearing_date=date(2026, 9, 1),
            captured_at=date(2026, 3, 5),
        )
        assert result.result == "pass"

    def test_fail_too_far_past(self) -> None:
        """SD ruling 5eac1c2d — hearing_date=2003, captured in 2026."""
        result = check_hearing_date_in_range(
            hearing_date=date(2003, 7, 15),
            captured_at=date(2026, 3, 4),
        )
        assert result.result == "fail"
        assert "exceeds" in (result.reason or "")
        assert "2003-07-15" in (result.reason or "")

    def test_fail_too_far_future(self) -> None:
        result = check_hearing_date_in_range(
            hearing_date=date(2028, 1, 1),
            captured_at=date(2026, 3, 4),
        )
        assert result.result == "fail"

    def test_pass_none_hearing_date(self) -> None:
        result = check_hearing_date_in_range(
            hearing_date=None,
            captured_at=date(2026, 3, 4),
        )
        assert result.result == "pass"

    def test_pass_none_captured_at(self) -> None:
        result = check_hearing_date_in_range(
            hearing_date=date(2026, 3, 5),
            captured_at=None,
        )
        assert result.result == "pass"

    def test_pass_both_none(self) -> None:
        result = check_hearing_date_in_range(
            hearing_date=None,
            captured_at=None,
        )
        assert result.result == "pass"


# ---------------------------------------------------------------------------
# no_concatenated_titles
# ---------------------------------------------------------------------------


class TestNoConcatenatedTitles:
    """Tests for the no_concatenated_titles rule."""

    def test_pass_normal_title(self) -> None:
        result = check_no_concatenated_titles("Smith v. Jones")
        assert result.result == "pass"

    def test_pass_vs_dot(self) -> None:
        result = check_no_concatenated_titles("Smith vs. Jones")
        assert result.result == "pass"

    def test_pass_none(self) -> None:
        result = check_no_concatenated_titles(None)
        assert result.result == "pass"

    def test_pass_empty(self) -> None:
        result = check_no_concatenated_titles("")
        assert result.result == "pass"

    def test_flag_two_v_dots(self) -> None:
        result = check_no_concatenated_titles("Smith v. Jones; Doe v. Roe")
        assert result.result == "flag"
        assert "2" in (result.reason or "")

    def test_flag_five_concatenated(self) -> None:
        """Fresno ruling 4647a509 — 5 concatenated titles."""
        title = "Alpha v. Beta; Gamma v. Delta; Epsilon v. Zeta; Eta v. Theta; Iota v. Kappa"
        result = check_no_concatenated_titles(title)
        assert result.result == "flag"
        assert "5" in (result.reason or "")

    def test_pass_no_separator(self) -> None:
        result = check_no_concatenated_titles("In re Marriage of Smith")
        assert result.result == "pass"

    def test_flag_mixed_vs(self) -> None:
        """Mix of v. and vs. separators."""
        result = check_no_concatenated_titles("Smith v. Jones; Doe vs. Roe")
        assert result.result == "flag"


# ---------------------------------------------------------------------------
# ruling_text_not_empty
# ---------------------------------------------------------------------------


class TestRulingTextNotEmpty:
    """Tests for the ruling_text_not_empty rule."""

    def test_pass_has_content(self) -> None:
        result = check_ruling_text_not_empty("The motion is granted.")
        assert result.result == "pass"

    def test_flag_none(self) -> None:
        result = check_ruling_text_not_empty(None)
        assert result.result == "flag"
        assert "null or empty" in (result.reason or "")

    def test_flag_empty_string(self) -> None:
        result = check_ruling_text_not_empty("")
        assert result.result == "flag"

    def test_flag_whitespace_only(self) -> None:
        result = check_ruling_text_not_empty("   \n\t  ")
        assert result.result == "flag"


# ---------------------------------------------------------------------------
# case_number_not_unknown
# ---------------------------------------------------------------------------


class TestCaseNumberNotUnknown:
    """Tests for the case_number_not_unknown rule."""

    def test_pass_normal(self) -> None:
        result = check_case_number_not_unknown("23STCV12345")
        assert result.result == "pass"

    def test_pass_none(self) -> None:
        result = check_case_number_not_unknown(None)
        assert result.result == "pass"

    def test_flag_unknown(self) -> None:
        result = check_case_number_not_unknown("UNKNOWN-abc123")
        assert result.result == "flag"
        assert "UNKNOWN-" in (result.reason or "")


# ---------------------------------------------------------------------------
# ruling_text_reasonable_length
# ---------------------------------------------------------------------------


class TestRulingTextReasonableLength:
    """Tests for the ruling_text_reasonable_length rule."""

    def test_pass_normal_length(self) -> None:
        result = check_ruling_text_reasonable_length("x" * 500)
        assert result.result == "pass"

    def test_pass_none(self) -> None:
        result = check_ruling_text_reasonable_length(None)
        assert result.result == "pass"

    def test_flag_truncation_sentinel(self) -> None:
        result = check_ruling_text_reasonable_length("x" * 50_000)
        assert result.result == "flag"
        assert "50000" in (result.reason or "")

    def test_pass_near_sentinel(self) -> None:
        """Length close to but not exactly 50000 should pass."""
        result = check_ruling_text_reasonable_length("x" * 49_999)
        assert result.result == "pass"

    def test_pass_above_sentinel(self) -> None:
        """Length above 50000 should pass (only exact sentinel is flagged)."""
        result = check_ruling_text_reasonable_length("x" * 50_001)
        assert result.result == "pass"


# ---------------------------------------------------------------------------
# run_deterministic_rules (aggregation)
# ---------------------------------------------------------------------------


class TestRunDeterministicRules:
    """Tests for the aggregated rule runner."""

    def test_all_pass(self) -> None:
        result = run_deterministic_rules(
            ruling_text="The motion is granted.",
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
        )
        assert result.overall == "pass"
        assert len(result.rules) == 6
        assert all(r.result == "pass" for r in result.rules)
        assert result.failed_rules == []
        assert result.flagged_rules == []
        assert result.reasons == []

    def test_fail_trumps_flag(self) -> None:
        """When both fail and flag rules fire, overall is fail."""
        result = run_deterministic_rules(
            ruling_text="<html><body>raw html</body>",
            case_number="UNKNOWN-abc123",
            case_title=None,
            hearing_date=None,
            captured_at=None,
        )
        assert result.overall == "fail"
        assert len(result.failed_rules) >= 1
        assert any(r.rule == "no_html_in_ruling_text" for r in result.failed_rules)

    def test_flag_only(self) -> None:
        """When only flag rules fire, overall is flag."""
        result = run_deterministic_rules(
            ruling_text=None,  # triggers ruling_text_not_empty flag
            case_number="UNKNOWN-abc",  # triggers case_number_not_unknown flag
            case_title=None,
            hearing_date=None,
            captured_at=None,
        )
        assert result.overall == "flag"
        assert len(result.flagged_rules) >= 1

    def test_spotcheck_la_html_ruling(self) -> None:
        """LA ruling 65932ec6 — raw HTML should be caught by no_html_in_ruling_text."""
        result = run_deterministic_rules(
            ruling_text="<html><head><title>LASC</title></head><body><div>ruling</div></body>",
            case_number="23STCV12345",
            case_title="Doe v. Roe",
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
        )
        assert result.overall == "fail"
        assert any(r.rule == "no_html_in_ruling_text" and r.result == "fail" for r in result.rules)

    def test_spotcheck_sd_wrong_date(self) -> None:
        """SD ruling 5eac1c2d — hearing_date=2003 should be caught."""
        result = run_deterministic_rules(
            ruling_text="The motion is granted.",
            case_number="37-2023-12345",
            case_title="Smith v. Jones",
            hearing_date=date(2003, 7, 15),
            captured_at=date(2026, 3, 4),
        )
        assert result.overall == "fail"
        assert any(r.rule == "hearing_date_in_range" and r.result == "fail" for r in result.rules)

    def test_spotcheck_fresno_concatenated(self) -> None:
        """Fresno ruling 4647a509 — 5 concatenated titles should be caught."""
        title = "Alpha v. Beta; Gamma v. Delta; Epsilon v. Zeta; Eta v. Theta; Iota v. Kappa"
        result = run_deterministic_rules(
            ruling_text="The motion is granted.",
            case_number="24CECG12345",
            case_title=title,
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
        )
        assert result.overall == "flag"
        assert any(r.rule == "no_concatenated_titles" and r.result == "flag" for r in result.rules)

    def test_reasons_property(self) -> None:
        """The reasons property should return only non-pass reasons."""
        result = run_deterministic_rules(
            ruling_text=None,
            case_number="UNKNOWN-abc",
            case_title="Smith v. Jones; Doe v. Roe",
            hearing_date=None,
            captured_at=None,
        )
        reasons = result.reasons
        assert len(reasons) >= 2
        assert all(isinstance(r, str) for r in reasons)
