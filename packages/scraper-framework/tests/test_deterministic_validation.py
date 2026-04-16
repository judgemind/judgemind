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
    check_no_cross_case_ruling_text,
    check_no_duplicate_ruling_text,
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
# no_duplicate_ruling_text
# ---------------------------------------------------------------------------


class TestNoDuplicateRulingText:
    """Tests for the no_duplicate_ruling_text rule."""

    def test_pass_single_ruling(self) -> None:
        """Single-ruling documents cannot have duplicates."""
        result = check_no_duplicate_ruling_text([500])
        assert result.result == "pass"
        assert result.rule == "no_duplicate_ruling_text"

    def test_pass_empty_list(self) -> None:
        """Empty list should pass (no rulings to compare)."""
        result = check_no_duplicate_ruling_text([])
        assert result.result == "pass"

    def test_pass_distinct_lengths(self) -> None:
        """Two rulings with different lengths should pass."""
        result = check_no_duplicate_ruling_text([500, 1200])
        assert result.result == "pass"

    def test_pass_three_distinct_lengths(self) -> None:
        """Three rulings with all-different lengths should pass."""
        result = check_no_duplicate_ruling_text([500, 1200, 800])
        assert result.result == "pass"

    def test_flag_two_same_length(self) -> None:
        """Two rulings with the same length should flag."""
        result = check_no_duplicate_ruling_text([500, 500])
        assert result.result == "flag"
        assert "duplicate ruling_text lengths" in (result.reason or "")
        assert "length 500 appears 2 times" in (result.reason or "")

    def test_flag_three_same_length(self) -> None:
        """Three rulings with the same length should flag — acceptance criterion #3."""
        result = check_no_duplicate_ruling_text([1234, 1234, 1234])
        assert result.result == "flag"
        assert "length 1234 appears 3 times" in (result.reason or "")

    def test_flag_four_same_length(self) -> None:
        """Four rulings with the same length should flag."""
        result = check_no_duplicate_ruling_text([7500, 7500, 7500, 7500])
        assert result.result == "flag"
        assert "length 7500 appears 4 times" in (result.reason or "")

    def test_flag_mixed_some_duplicate(self) -> None:
        """Mix of distinct and duplicate lengths — only duplicates flagged."""
        result = check_no_duplicate_ruling_text([500, 1200, 500, 800])
        assert result.result == "flag"
        assert "length 500 appears 2 times" in (result.reason or "")

    def test_pass_all_none(self) -> None:
        """All None lengths (no text) should pass — handled by ruling_text_not_empty."""
        result = check_no_duplicate_ruling_text([None, None, None])
        assert result.result == "pass"

    def test_pass_one_non_none(self) -> None:
        """Only one non-None length — need 2+ to compare."""
        result = check_no_duplicate_ruling_text([500, None, None])
        assert result.result == "pass"

    def test_flag_ignores_none_values(self) -> None:
        """None values are excluded; duplicates among non-None still flagged."""
        result = check_no_duplicate_ruling_text([500, None, 500])
        assert result.result == "flag"
        assert "length 500 appears 2 times" in (result.reason or "")

    def test_flag_multiple_duplicate_groups(self) -> None:
        """Multiple groups of duplicates should all be reported."""
        result = check_no_duplicate_ruling_text([500, 1200, 500, 1200])
        assert result.result == "flag"
        assert "length 500 appears 2 times" in (result.reason or "")
        assert "length 1200 appears 2 times" in (result.reason or "")


# ---------------------------------------------------------------------------
# no_cross_case_ruling_text
# ---------------------------------------------------------------------------


class TestNoCrossCaseRulingText:
    """Tests for the no_cross_case_ruling_text rule (#2371)."""

    def test_pass_none_ruling_text(self) -> None:
        result = check_no_cross_case_ruling_text(
            ruling_text=None,
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999"],
        )
        assert result.result == "pass"
        assert result.rule == "no_cross_case_ruling_text"

    def test_pass_empty_ruling_text(self) -> None:
        result = check_no_cross_case_ruling_text(
            ruling_text="",
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999"],
        )
        assert result.result == "pass"

    def test_pass_empty_other_list(self) -> None:
        result = check_no_cross_case_ruling_text(
            ruling_text="The motion in 23STCV99999 is granted.",
            own_case_number="23STCV12345",
            other_case_numbers=[],
        )
        assert result.result == "pass"

    def test_pass_no_case_numbers_in_text(self) -> None:
        """Ruling text with no case-number-like tokens should pass."""
        result = check_no_cross_case_ruling_text(
            ruling_text="The motion for summary judgment is GRANTED.",
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999"],
        )
        assert result.result == "pass"

    def test_pass_only_own_case_number(self) -> None:
        """Ruling text that only references its own case number should pass."""
        result = check_no_cross_case_ruling_text(
            ruling_text="Case 23STCV12345: motion granted. See 23STCV12345.",
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999", "23STCV88888"],
        )
        assert result.result == "pass"

    def test_flag_other_case_la_format(self) -> None:
        """LA ruling text that references a sibling LA case — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text=(
                "In the matter of 23STCV99999, the court finds that the motion "
                "for summary judgment should be granted."
            ),
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999"],
        )
        assert result.result == "flag"
        assert "cross-case ruling text misattribution" in (result.reason or "")
        assert "23STCV99999" in (result.reason or "")

    def test_flag_other_case_sd_dashed(self) -> None:
        """SD-style dashed case number referenced in text — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="Please see ruling in case 37-2023-99999 for prior context.",
            own_case_number="37-2023-12345",
            other_case_numbers=["37-2023-99999"],
        )
        assert result.result == "flag"

    def test_flag_other_case_fresno(self) -> None:
        """Fresno-format case number referenced — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="Demurrer in 24CECG99999 is sustained with leave to amend.",
            own_case_number="24CECG12345",
            other_case_numbers=["24CECG99999"],
        )
        assert result.result == "flag"

    def test_flag_other_case_oc_civil(self) -> None:
        """OC civil format in text — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="The matter in 30-2024-12345678 is continued to next month.",
            own_case_number="30-2024-00001111",
            other_case_numbers=["30-2024-12345678"],
        )
        assert result.result == "flag"

    def test_flag_other_case_ventura(self) -> None:
        """Ventura-format case number in text — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="The court adopts the tentative ruling in 2024CUBC999999.",
            own_case_number="2024CUBC123456",
            other_case_numbers=["2024CUBC999999"],
        )
        assert result.result == "flag"

    def test_flag_other_case_contra_costa(self) -> None:
        """Contra Costa case number format in text — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="See related matter C21-09999 for additional background.",
            own_case_number="C21-01234",
            other_case_numbers=["C21-09999"],
        )
        assert result.result == "flag"

    def test_flag_other_case_sf_civil(self) -> None:
        """SF civil format in text — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="Prior order in CGC22999999 controls here.",
            own_case_number="CGC22123456",
            other_case_numbers=["CGC22999999"],
        )
        assert result.result == "flag"

    def test_flag_other_case_riverside(self) -> None:
        """Riverside RIC-format in text — flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="Motion in RIC9999999 is denied.",
            own_case_number="RIC1234567",
            other_case_numbers=["RIC9999999"],
        )
        assert result.result == "flag"

    def test_flag_case_insensitive_match(self) -> None:
        """Case-insensitive matching — lowercase in text should still flag."""
        result = check_no_cross_case_ruling_text(
            ruling_text="The motion in 23stcv99999 is granted.",
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999"],
        )
        assert result.result == "flag"

    def test_flag_format_tolerant(self) -> None:
        """Different formatting (hyphens removed / spacing) must still flag.

        The normalisation layer strips spaces and hyphens and uppercases, so
        ``"37 2023 99999"`` in text should match ``"37-2023-99999"`` in the
        other-cases list once both normalise to ``"37202399999"``.  The
        regex itself requires a dashed form to recognise the candidate, so
        we use a dashed form in the text and a non-dashed form in the list.
        """
        result = check_no_cross_case_ruling_text(
            ruling_text="See case 37-2023-99999 for prior order.",
            own_case_number="37-2023-12345",
            other_case_numbers=["37202399999"],
        )
        assert result.result == "flag"

    def test_pass_substring_of_own(self) -> None:
        """If an "other" number is a substring of own (weird edge), must not false-flag.

        This can happen when case-number lists are constructed sloppily.
        The rule deliberately filters out any sibling entry that normalises
        to the own number; it should also not flag pure substrings in the
        text (regex uses word boundaries, so this is mostly a sanity check).
        """
        result = check_no_cross_case_ruling_text(
            ruling_text="Case 30-2024-12345678 is continued.",
            own_case_number="30-2024-12345678",
            # Deliberately sloppy: sibling list contains own number.
            other_case_numbers=["30-2024-12345678"],
        )
        assert result.result == "pass"

    def test_pass_other_number_not_in_text(self) -> None:
        """Other list has entries but none appear in the text — pass."""
        result = check_no_cross_case_ruling_text(
            ruling_text="Case 23STCV12345: motion granted.",
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999", "24STCV88888"],
        )
        assert result.result == "pass"

    def test_flag_multiple_other_cases(self) -> None:
        """Two siblings referenced — reason should mention count and both numbers."""
        result = check_no_cross_case_ruling_text(
            ruling_text="See 23STCV99999 and also 24STCV88888 for related.",
            own_case_number="23STCV12345",
            other_case_numbers=["23STCV99999", "24STCV88888"],
        )
        assert result.result == "flag"
        reason = result.reason or ""
        assert "2 other case" in reason
        assert "23STCV99999" in reason
        assert "24STCV88888" in reason

    def test_pass_none_own_case_number(self) -> None:
        """If own case number is None but text cleanly contains a sibling — flag.

        None own_case_number is not a blocker; the check still scans for
        sibling numbers.  This guards against defensive callers.
        """
        result = check_no_cross_case_ruling_text(
            ruling_text="The motion in 23STCV99999 is granted.",
            own_case_number=None,
            other_case_numbers=["23STCV99999"],
        )
        assert result.result == "flag"

    def test_pass_other_with_empty_and_none(self) -> None:
        """Other list containing empty strings is handled gracefully."""
        result = check_no_cross_case_ruling_text(
            ruling_text="Case 23STCV12345 matter resolved.",
            own_case_number="23STCV12345",
            other_case_numbers=["", "   "],
        )
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

    def test_cross_case_kwarg_omitted_backward_compat(self) -> None:
        """Existing callers that omit other_case_numbers must still pass (#2371).

        The default count of rules is 6 without the cross-case check.
        """
        result = run_deterministic_rules(
            ruling_text="The motion in 23STCV99999 is granted.",
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
        )
        # Without other_case_numbers, no cross-case rule added.
        assert len(result.rules) == 6
        assert not any(r.rule == "no_cross_case_ruling_text" for r in result.rules)
        assert result.overall == "pass"

    def test_cross_case_kwarg_empty_list_backward_compat(self) -> None:
        """Empty other_case_numbers list is treated the same as omission (#2371)."""
        result = run_deterministic_rules(
            ruling_text="The motion in 23STCV99999 is granted.",
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
            other_case_numbers=[],
        )
        assert len(result.rules) == 6
        assert not any(r.rule == "no_cross_case_ruling_text" for r in result.rules)
        assert result.overall == "pass"

    def test_cross_case_flag_via_aggregated_runner(self) -> None:
        """run_deterministic_rules flags cross-case contamination when siblings given (#2371)."""
        result = run_deterministic_rules(
            ruling_text="The motion in 23STCV99999 is granted.",
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
            other_case_numbers=["23STCV99999", "23STCV88888"],
        )
        assert len(result.rules) == 7
        assert result.overall == "flag"
        cross_rules = [r for r in result.rules if r.rule == "no_cross_case_ruling_text"]
        assert len(cross_rules) == 1
        assert cross_rules[0].result == "flag"
        assert "23STCV99999" in (cross_rules[0].reason or "")

    def test_cross_case_pass_own_case_only_in_text(self) -> None:
        """run_deterministic_rules passes when ruling text only mentions its own case (#2371)."""
        result = run_deterministic_rules(
            ruling_text="Case 23STCV12345: motion granted.",
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            hearing_date=date(2026, 3, 5),
            captured_at=date(2026, 3, 4),
            other_case_numbers=["23STCV99999"],
        )
        assert len(result.rules) == 7
        assert result.overall == "pass"
        cross_rules = [r for r in result.rules if r.rule == "no_cross_case_ruling_text"]
        assert cross_rules[0].result == "pass"
