"""Tests for the extraction eval scorer module."""

from __future__ import annotations

import sys
from pathlib import Path

# Add eval scripts to path
EVAL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "eval"
sys.path.insert(0, str(EVAL_DIR))

from eval_scorer import (  # noqa: E402
    EvalSummary,
    FieldScore,
    FixtureScore,
    aggregate_scores,
    check_hallucination,
    check_ruling_text_contains,
    compare_field,
    compare_models,
    compute_text_similarity,
    format_report,
    get_county_from_fixture,
    normalize_case_number,
    normalize_case_title,
    normalize_date,
    normalize_department,
    normalize_judge,
    normalize_outcome,
    normalize_unicode,
    normalize_value,
    score_fixture,
    should_skip_fixture,
)

# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


class TestNormalizeUnicode:
    """Tests for Unicode normalization."""

    def test_en_dash_to_hyphen(self) -> None:
        assert normalize_unicode("2024\u20132025") == "2024-2025"

    def test_em_dash_to_hyphen(self) -> None:
        assert normalize_unicode("A\u2014B") == "A-B"

    def test_smart_quotes(self) -> None:
        assert normalize_unicode("\u201cHello\u201d") == '"Hello"'

    def test_plain_ascii_unchanged(self) -> None:
        assert normalize_unicode("hello world") == "hello world"


class TestNormalizeValue:
    """Tests for value normalization."""

    def test_none_returns_none(self) -> None:
        assert normalize_value(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_value("") is None

    def test_null_string_returns_none(self) -> None:
        assert normalize_value("null") is None

    def test_strips_whitespace(self) -> None:
        assert normalize_value("  hello  ") == "hello"


class TestNormalizeCaseNumber:
    """Tests for case number normalization."""

    def test_strips_county_prefix_2_digit(self) -> None:
        assert normalize_case_number("30-2024-01393434") == "2024-01393434"

    def test_keeps_no_prefix(self) -> None:
        assert normalize_case_number("22SMCV01940") == "22SMCV01940"

    def test_none_returns_none(self) -> None:
        assert normalize_case_number(None) is None

    def test_strips_spaces(self) -> None:
        assert normalize_case_number("22 SMCV 01940") == "22SMCV01940"


class TestNormalizeDepartment:
    """Tests for department normalization."""

    def test_uppercases(self) -> None:
        assert normalize_department("c25") == "C25"

    def test_preserves_leading_zeros(self) -> None:
        assert normalize_department("01") == "01"

    def test_none_returns_none(self) -> None:
        assert normalize_department(None) is None


class TestNormalizeDate:
    """Tests for date normalization."""

    def test_iso_format(self) -> None:
        assert normalize_date("2026-03-02") == "2026-03-02"

    def test_month_day_year(self) -> None:
        assert normalize_date("March 2, 2026") == "2026-03-02"

    def test_slash_format(self) -> None:
        assert normalize_date("03/02/2026") == "2026-03-02"

    def test_none_returns_none(self) -> None:
        assert normalize_date(None) is None

    def test_null_string_returns_none(self) -> None:
        assert normalize_date("null") is None


class TestNormalizeJudge:
    """Tests for judge name normalization."""

    def test_strips_honorable(self) -> None:
        assert normalize_judge("Honorable John Smith") == "john smith"

    def test_strips_judge_prefix(self) -> None:
        assert normalize_judge("Judge Jane Doe") == "jane doe"

    def test_strips_hon_prefix(self) -> None:
        assert normalize_judge("Hon. Bob Jones") == "bob jones"

    def test_none_returns_none(self) -> None:
        assert normalize_judge(None) is None

    def test_plain_name(self) -> None:
        assert normalize_judge("John Smith") == "john smith"


class TestNormalizeOutcome:
    """Tests for outcome normalization."""

    def test_granted(self) -> None:
        assert normalize_outcome("GRANTED") == "granted"

    def test_granted_in_part_underscore(self) -> None:
        assert normalize_outcome("granted_in_part") == "granted in part"

    def test_off_calendar(self) -> None:
        assert normalize_outcome("Off-Calendar") == "off calendar"

    def test_none_returns_none(self) -> None:
        assert normalize_outcome(None) is None


class TestNormalizeCaseTitle:
    """Tests for case title normalization."""

    def test_vs_to_v_dot(self) -> None:
        assert normalize_case_title("Smith vs. Jones") == "smith v. jones"

    def test_vs_space_to_v_dot(self) -> None:
        assert normalize_case_title("Smith vs Jones") == "smith v. jones"

    def test_collapses_spaces(self) -> None:
        assert normalize_case_title("Smith  v.  Jones") == "smith v. jones"

    def test_none_returns_none(self) -> None:
        assert normalize_case_title(None) is None


# ---------------------------------------------------------------------------
# Field comparison tests
# ---------------------------------------------------------------------------


class TestCompareField:
    """Tests for field comparison logic."""

    def test_both_none(self) -> None:
        assert compare_field("judge_name", None, None) is True

    def test_one_none(self) -> None:
        assert compare_field("judge_name", None, "Smith") is False

    def test_judge_name_with_prefix(self) -> None:
        assert compare_field("judge_name", "Hon. John Smith", "John Smith") is True

    def test_hearing_date_different_formats(self) -> None:
        assert compare_field("hearing_date", "2026-03-02", "March 2, 2026") is True

    def test_department_case_insensitive(self) -> None:
        assert compare_field("department", "c25", "C25") is True

    def test_case_number_with_prefix(self) -> None:
        assert compare_field("case_number", "30-2024-01393434", "2024-01393434") is True

    def test_case_count_integer(self) -> None:
        assert compare_field("case_count", "14", 14) is True

    def test_outcome_normalization(self) -> None:
        assert compare_field("outcome", "granted_in_part", "GRANTED IN PART") is True

    def test_case_title_fuzzy_substring(self) -> None:
        assert (
            compare_field(
                "case_title",
                "Martinez v. ABC Manufacturing Inc.",
                "Martinez v. ABC Manufacturing Inc. et al.",
            )
            is True
        )

    def test_case_title_mismatch(self) -> None:
        assert compare_field("case_title", "Smith v. Jones", "Williams v. Brown") is False


# ---------------------------------------------------------------------------
# Ruling text scoring tests
# ---------------------------------------------------------------------------


class TestComputeTextSimilarity:
    """Tests for text similarity computation."""

    def test_identical(self) -> None:
        assert compute_text_similarity("hello world", "hello world") == 1.0

    def test_none_input(self) -> None:
        assert compute_text_similarity(None, "hello") == 0.0

    def test_empty_input(self) -> None:
        assert compute_text_similarity("", "hello") == 0.0

    def test_similar_texts(self) -> None:
        sim = compute_text_similarity("the quick brown fox", "the quick brown dog")
        assert 0.5 < sim < 1.0

    def test_different_texts(self) -> None:
        sim = compute_text_similarity("hello world", "completely different text")
        assert sim < 0.5


class TestCheckRulingTextContains:
    """Tests for ruling text contains check."""

    def test_all_found(self) -> None:
        found, missing = check_ruling_text_contains(
            "The motion is granted. Fees are awarded.",
            ["motion is granted", "fees are awarded"],
        )
        assert found is True
        assert missing == []

    def test_some_missing(self) -> None:
        found, missing = check_ruling_text_contains(
            "The motion is granted.",
            ["motion is granted", "fees are awarded"],
        )
        assert found is False
        assert "fees are awarded" in missing

    def test_none_text(self) -> None:
        found, missing = check_ruling_text_contains(None, ["hello"])
        assert found is False

    def test_case_insensitive(self) -> None:
        found, _ = check_ruling_text_contains("MOTION IS GRANTED", ["motion is granted"])
        assert found is True


class TestCheckHallucination:
    """Tests for hallucination detection."""

    def test_no_hallucination(self) -> None:
        source = "The court grants the motion for summary judgment. Fees are awarded."
        ruling = "The court grants the motion for summary judgment."
        assert check_hallucination(ruling, source) is False

    def test_hallucination_detected(self) -> None:
        source = "The court grants the motion."
        ruling = (
            "The court hereby declares total global victory for the plaintiff "
            "in all jurisdictions worldwide and orders immediate payment of "
            "one billion dollars in damages. Furthermore, the defendant shall "
            "be permanently barred from all courts everywhere."
        )
        assert check_hallucination(ruling, source) is True

    def test_none_inputs(self) -> None:
        assert check_hallucination(None, "source") is False
        assert check_hallucination("ruling", None) is False

    def test_empty_ruling(self) -> None:
        assert check_hallucination("", "source") is False


# ---------------------------------------------------------------------------
# Fixture scoring tests
# ---------------------------------------------------------------------------


class TestScoreFixture:
    """Tests for fixture scoring."""

    def test_perfect_score(self) -> None:
        expected = {
            "judge_name": "John Smith",
            "hearing_date": "2026-03-02",
            "department": "C25",
            "case_count": 3,
        }
        extraction = {
            "extracted_judge_name": "John Smith",
            "hearing_date": "2026-03-02",
            "department": "C25",
            "case_count": 3,
            "rulings": [],
        }
        score = score_fixture("oc_test.json", extraction, expected)
        assert all(fs.match for fs in score.field_scores)

    def test_mismatch_case_count(self) -> None:
        expected = {"case_count": 5}
        extraction = {"case_count": 3, "rulings": []}
        score = score_fixture("oc_test.json", extraction, expected)
        case_count_scores = [fs for fs in score.field_scores if fs.field_name == "case_count"]
        assert len(case_count_scores) == 1
        assert case_count_scores[0].match is False

    def test_known_fixture_issue_noted(self) -> None:
        expected = {"judge_name": "Smith"}
        extraction = {"extracted_judge_name": "Jones", "rulings": []}
        score = score_fixture("oc_north_n.pdf", extraction, expected)
        judge_scores = [fs for fs in score.field_scores if fs.field_name == "judge_name"]
        assert len(judge_scores) == 1
        assert judge_scores[0].notes == "known_fixture_issue"


class TestGetCountyFromFixture:
    """Tests for county extraction from fixture names."""

    def test_la(self) -> None:
        assert get_county_from_fixture("la_ruling_bh205.json") == "Los Angeles"

    def test_oc(self) -> None:
        assert get_county_from_fixture("oc_apkarian_c25.json") == "Orange"

    def test_riv(self) -> None:
        assert get_county_from_fixture("riv_ps1.json") == "Riverside"

    def test_unknown(self) -> None:
        assert get_county_from_fixture("xyz_test.json") == "Unknown"


class TestShouldSkipFixture:
    """Tests for fixture skipping logic."""

    def test_stale_viewstate(self) -> None:
        assert should_skip_fixture("la_main_page.json") is True

    def test_access_denied(self) -> None:
        assert should_skip_fixture("sf_captcha_gate.json") is True

    def test_normal_fixture(self) -> None:
        assert should_skip_fixture("oc_apkarian_c25.json") is False


# ---------------------------------------------------------------------------
# Aggregation tests
# ---------------------------------------------------------------------------


class TestAggregateScores:
    """Tests for score aggregation."""

    def test_basic_aggregation(self) -> None:
        scores = [
            FixtureScore(
                fixture_name="oc_test.json",
                county="Orange",
                field_scores=[
                    FieldScore("judge_name", "Smith", "Smith", True),
                    FieldScore("case_number", "2024-123", "2024-123", True),
                ],
                ruling_text_similarity=0.95,
            ),
            FixtureScore(
                fixture_name="la_test.json",
                county="Los Angeles",
                field_scores=[
                    FieldScore("judge_name", "Jones", "Wrong", False),
                    FieldScore("case_number", "2024-456", "2024-456", True),
                ],
                ruling_text_similarity=0.85,
            ),
        ]
        summary = aggregate_scores(scores, "haiku")
        assert summary.total_fixtures == 2
        assert summary.field_accuracy["judge_name"] == 0.5
        assert summary.field_accuracy["case_number"] == 1.0
        assert 0.89 < summary.avg_ruling_text_similarity < 0.91

    def test_known_fixture_issues_excluded(self) -> None:
        scores = [
            FixtureScore(
                fixture_name="test.json",
                county="Orange",
                field_scores=[
                    FieldScore("judge_name", "A", "B", False, "known_fixture_issue"),
                    FieldScore("case_number", "123", "123", True),
                ],
            ),
        ]
        summary = aggregate_scores(scores, "haiku")
        # judge_name should not be counted (known issue)
        assert "judge_name" not in summary.field_accuracy
        assert summary.field_accuracy["case_number"] == 1.0


# ---------------------------------------------------------------------------
# Comparison tests
# ---------------------------------------------------------------------------


class TestCompareModels:
    """Tests for model comparison report."""

    def test_comparison_output(self) -> None:
        summary_a = EvalSummary(
            model="haiku",
            total_fixtures=5,
            field_accuracy={"case_number": 0.95},
            avg_ruling_text_similarity=0.9,
        )
        summary_b = EvalSummary(
            model="sonnet",
            total_fixtures=5,
            field_accuracy={"case_number": 1.0},
            avg_ruling_text_similarity=0.95,
        )
        report = compare_models(summary_a, summary_b)
        assert "haiku" in report
        assert "sonnet" in report


# ---------------------------------------------------------------------------
# Report formatting tests
# ---------------------------------------------------------------------------


class TestFormatReport:
    """Tests for report formatting."""

    def test_basic_report(self) -> None:
        summary = EvalSummary(
            model="haiku",
            total_fixtures=10,
            field_accuracy={"case_number": 0.95, "judge_name": 0.90},
            avg_ruling_text_similarity=0.92,
            thresholds_passed=True,
        )
        report = format_report(summary)
        assert "haiku" in report
        assert "case_number" in report
        assert "95.0%" in report
