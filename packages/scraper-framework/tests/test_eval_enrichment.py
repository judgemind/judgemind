"""Tests for the enrichment eval harness scoring functions.

Tests validate:
- Exact match scoring for motion_type and outcome
- Fuzzy match scoring for case_title and parties
- Fixture loading and scoring pipeline
- Aggregation and threshold checking
- Report formatting
- CLI interface (help, cached mode, threshold exit codes)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add the eval scripts directory to path
EVAL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "eval"
sys.path.insert(0, str(EVAL_DIR))

from eval_enrichment import (  # noqa: E402
    DEFAULT_FIXTURES_DIR,
    CountyScore,
    EvalSummary,
    FieldScore,
    FixtureScore,
    _check_thresholds,
    aggregate_scores,
    format_json_report,
    format_text_report,
    load_cached_results,
    load_fixtures,
    normalize_case_title,
    normalize_for_exact_match,
    save_cached_results,
    score_case_title,
    score_fixture,
    score_motion_type,
    score_outcome,
    score_parties,
    score_results,
)

# ---------------------------------------------------------------------------
# normalize_for_exact_match
# ---------------------------------------------------------------------------


class TestNormalizeForExactMatch:
    """Tests for the normalize_for_exact_match helper."""

    def test_none_returns_none(self) -> None:
        assert normalize_for_exact_match(None) is None

    def test_empty_returns_none(self) -> None:
        assert normalize_for_exact_match("") is None

    def test_whitespace_returns_none(self) -> None:
        assert normalize_for_exact_match("   ") is None

    def test_strips_and_lowercases(self) -> None:
        assert normalize_for_exact_match("  MSJ  ") == "msj"

    def test_preserves_underscores(self) -> None:
        assert normalize_for_exact_match("motion_to_compel") == "motion_to_compel"


# ---------------------------------------------------------------------------
# score_motion_type
# ---------------------------------------------------------------------------


class TestScoreMotionType:
    """Tests for motion_type exact match scoring."""

    def test_exact_match(self) -> None:
        assert score_motion_type("msj", "msj") is True

    def test_case_insensitive_match(self) -> None:
        assert score_motion_type("MSJ", "msj") is True

    def test_mismatch(self) -> None:
        assert score_motion_type("msj", "demurrer") is False

    def test_both_none(self) -> None:
        assert score_motion_type(None, None) is True

    def test_expected_none_extracted_present(self) -> None:
        assert score_motion_type(None, "msj") is False

    def test_expected_present_extracted_none(self) -> None:
        assert score_motion_type("msj", None) is False

    def test_out_of_taxonomy_exact_match(self) -> None:
        """Fixtures may have motion types not in the taxonomy."""
        assert (
            score_motion_type(
                "motion_for_judgment_on_the_pleadings",
                "motion_for_judgment_on_the_pleadings",
            )
            is True
        )

    def test_whitespace_trimmed(self) -> None:
        assert score_motion_type("  msj  ", "msj") is True


# ---------------------------------------------------------------------------
# score_outcome
# ---------------------------------------------------------------------------


class TestScoreOutcome:
    """Tests for outcome exact match scoring."""

    def test_exact_match(self) -> None:
        assert score_outcome("granted", "granted") is True

    def test_case_insensitive(self) -> None:
        assert score_outcome("GRANTED", "granted") is True

    def test_mismatch(self) -> None:
        assert score_outcome("granted", "denied") is False

    def test_both_none(self) -> None:
        assert score_outcome(None, None) is True

    def test_off_calendar(self) -> None:
        assert score_outcome("off_calendar", "off_calendar") is True

    def test_granted_in_part(self) -> None:
        assert score_outcome("granted_in_part", "granted_in_part") is True


# ---------------------------------------------------------------------------
# normalize_case_title
# ---------------------------------------------------------------------------


class TestNormalizeCaseTitle:
    """Tests for case title normalization."""

    def test_none_returns_none(self) -> None:
        assert normalize_case_title(None) is None

    def test_empty_returns_none(self) -> None:
        assert normalize_case_title("") is None

    def test_lowercases(self) -> None:
        assert normalize_case_title("Smith v. Jones") == "smith v. jones"

    def test_normalizes_vs_to_v(self) -> None:
        assert normalize_case_title("Smith vs. Jones") == "smith v. jones"
        assert normalize_case_title("Smith vs Jones") == "smith v. jones"

    def test_normalizes_versus_to_v(self) -> None:
        assert normalize_case_title("Smith versus Jones") == "smith v. jones"

    def test_strips_et_al(self) -> None:
        assert normalize_case_title("Smith v. Jones, et al.") == "smith v. jones"
        assert normalize_case_title("Smith v. Jones et al.") == "smith v. jones"
        assert normalize_case_title("Smith v. Jones et al") == "smith v. jones"

    def test_collapses_whitespace(self) -> None:
        assert normalize_case_title("Smith  v.   Jones") == "smith v. jones"


# ---------------------------------------------------------------------------
# score_case_title
# ---------------------------------------------------------------------------


class TestScoreCaseTitle:
    """Tests for case_title fuzzy match scoring."""

    def test_exact_match(self) -> None:
        assert score_case_title("Smith v. Jones", "Smith v. Jones") is True

    def test_case_insensitive(self) -> None:
        assert score_case_title("Smith v. Jones", "smith v. jones") is True

    def test_vs_normalization(self) -> None:
        assert score_case_title("Smith v. Jones", "Smith vs. Jones") is True

    def test_et_al_stripped(self) -> None:
        assert score_case_title("Smith v. Jones, et al.", "Smith v. Jones") is True

    def test_fuzzy_match_similar(self) -> None:
        """Names with minor differences should match at >= 0.85."""
        assert score_case_title("Smith v Jones", "Smith v. Jones") is True

    def test_mismatch(self) -> None:
        assert score_case_title("Smith v. Jones", "Garcia v. Martinez") is False

    def test_both_none(self) -> None:
        assert score_case_title(None, None) is True

    def test_one_none(self) -> None:
        assert score_case_title("Smith v. Jones", None) is False
        assert score_case_title(None, "Smith v. Jones") is False

    def test_token_overlap_match(self) -> None:
        """Token overlap >= 80% should match even if Levenshtein is lower."""
        # These share 4/5 tokens = 80% overlap
        assert (
            score_case_title(
                "John Smith v. Jane Jones Corp",
                "John Smith v. Jane Jones LLC",
            )
            is True
        )


# ---------------------------------------------------------------------------
# score_parties
# ---------------------------------------------------------------------------


class TestScoreParties:
    """Tests for parties fuzzy recall scoring."""

    def test_exact_match(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        extracted = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        assert score_parties(expected, extracted) == 1.0

    def test_case_insensitive(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        extracted = {"plaintiffs": ["SMITH"], "defendants": ["JONES"]}
        assert score_parties(expected, extracted) == 1.0

    def test_partial_recall(self) -> None:
        expected = {"plaintiffs": ["Smith", "Brown"], "defendants": ["Jones"]}
        extracted = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        # 2 of 3 expected found
        result = score_parties(expected, extracted)
        assert abs(result - 2.0 / 3.0) < 0.01

    def test_no_match(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        extracted = {"plaintiffs": ["Garcia"], "defendants": ["Martinez"]}
        assert score_parties(expected, extracted) == 0.0

    def test_fuzzy_match(self) -> None:
        """Party names with minor differences should match at >= 0.85."""
        expected = {"plaintiffs": ["John Smith"], "defendants": ["Jane Jones"]}
        extracted = {"plaintiffs": ["John Smth"], "defendants": ["Jane Jones"]}
        # "John Smth" vs "John Smith" should be close enough
        result = score_parties(expected, extracted)
        assert result >= 0.5  # At least one should match

    def test_empty_expected(self) -> None:
        """No expected parties — vacuously true."""
        expected: dict[str, list[str]] = {"plaintiffs": [], "defendants": []}
        extracted = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        assert score_parties(expected, extracted) == 1.0

    def test_none_expected(self) -> None:
        assert score_parties(None, {"plaintiffs": ["Smith"], "defendants": []}) == 1.0

    def test_none_extracted(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        assert score_parties(expected, None) == 0.0

    def test_empty_extracted(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        extracted: dict[str, list[str]] = {"plaintiffs": [], "defendants": []}
        assert score_parties(expected, extracted) == 0.0


# ---------------------------------------------------------------------------
# score_fixture
# ---------------------------------------------------------------------------


class TestScoreFixture:
    """Tests for scoring a single fixture."""

    def test_all_match(self) -> None:
        fixture = {
            "fixture_path": "la/test.json",
            "county": "los_angeles",
            "ruling_text": "test",
            "expected": {
                "motion_type": "msj",
                "outcome": "granted",
                "case_title": "Smith v. Jones",
                "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
            },
        }
        extraction = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Smith v. Jones",
            "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
        }
        score = score_fixture(fixture, extraction)
        assert score.county == "los_angeles"
        assert all(f.match for f in score.field_scores)
        assert score.party_recall == 1.0
        assert score.error is None

    def test_none_extraction(self) -> None:
        fixture = {
            "fixture_path": "la/test.json",
            "county": "los_angeles",
            "ruling_text": "test",
            "expected": {"motion_type": "msj", "outcome": "granted"},
        }
        score = score_fixture(fixture, None)
        assert score.error == "No extraction result"

    def test_mismatched_fields(self) -> None:
        fixture = {
            "fixture_path": "la/test.json",
            "county": "los_angeles",
            "ruling_text": "test",
            "expected": {
                "motion_type": "msj",
                "outcome": "granted",
                "case_title": "Smith v. Jones",
                "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
            },
        }
        extraction = {
            "motion_type": "demurrer",  # wrong
            "outcome": "denied",  # wrong
            "case_title": "Garcia v. Martinez",  # wrong
            "parties": {"plaintiffs": ["Garcia"], "defendants": ["Martinez"]},  # wrong
        }
        score = score_fixture(fixture, extraction)
        assert not any(f.match for f in score.field_scores)
        assert score.party_recall == 0.0


# ---------------------------------------------------------------------------
# aggregate_scores
# ---------------------------------------------------------------------------


class TestAggregateScores:
    """Tests for score aggregation."""

    def _make_fixture_score(
        self,
        *,
        county: str = "los_angeles",
        mt_match: bool = True,
        oc_match: bool = True,
        ct_match: bool = True,
        party_recall: float = 1.0,
    ) -> FixtureScore:
        return FixtureScore(
            fixture_path=f"{county}/test.json",
            county=county,
            field_scores=[
                FieldScore("motion_type", "msj", "msj", mt_match),
                FieldScore("outcome", "granted", "granted", oc_match),
                FieldScore("case_title", "Smith v. Jones", "Smith v. Jones", ct_match),
            ],
            party_recall=party_recall,
        )

    def test_perfect_scores(self) -> None:
        scores = [self._make_fixture_score() for _ in range(10)]
        summary = aggregate_scores(scores, "test-model")
        assert summary.total_fixtures == 10
        assert summary.field_accuracy["motion_type"] == 1.0
        assert summary.field_accuracy["outcome"] == 1.0
        assert summary.field_accuracy["case_title"] == 1.0
        assert summary.avg_party_recall == 1.0
        assert summary.thresholds_passed is True

    def test_below_threshold(self) -> None:
        # 9 correct, 1 wrong for motion_type (90% < 95% threshold)
        scores = [self._make_fixture_score() for _ in range(9)]
        scores.append(self._make_fixture_score(mt_match=False))
        summary = aggregate_scores(scores, "test-model")
        assert summary.field_accuracy["motion_type"] == 0.9
        assert summary.thresholds_passed is False
        assert any("motion_type" in f for f in summary.threshold_failures)

    def test_per_county(self) -> None:
        scores = [
            self._make_fixture_score(county="los_angeles"),
            self._make_fixture_score(county="los_angeles"),
            self._make_fixture_score(county="orange"),
        ]
        summary = aggregate_scores(scores, "test-model")
        assert "los_angeles" in summary.county_scores
        assert "orange" in summary.county_scores
        assert summary.county_scores["los_angeles"].total_fixtures == 2
        assert summary.county_scores["orange"].total_fixtures == 1


# ---------------------------------------------------------------------------
# _check_thresholds
# ---------------------------------------------------------------------------


class TestCheckThresholds:
    """Tests for threshold checking."""

    def test_all_pass(self) -> None:
        summary = EvalSummary(
            model="test",
            field_accuracy={
                "motion_type": 0.96,
                "outcome": 0.96,
                "case_title": 0.91,
            },
            avg_party_recall=0.86,
        )
        _check_thresholds(summary)
        assert summary.thresholds_passed is True
        assert summary.threshold_failures == []

    def test_motion_type_fails(self) -> None:
        summary = EvalSummary(
            model="test",
            field_accuracy={
                "motion_type": 0.90,
                "outcome": 0.96,
                "case_title": 0.91,
            },
            avg_party_recall=0.86,
        )
        _check_thresholds(summary)
        assert summary.thresholds_passed is False
        assert any("motion_type" in f for f in summary.threshold_failures)

    def test_parties_fails(self) -> None:
        summary = EvalSummary(
            model="test",
            field_accuracy={
                "motion_type": 0.96,
                "outcome": 0.96,
                "case_title": 0.91,
            },
            avg_party_recall=0.80,
        )
        _check_thresholds(summary)
        assert summary.thresholds_passed is False
        assert any("parties" in f for f in summary.threshold_failures)


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


class TestLoadFixtures:
    """Tests for load_fixtures function."""

    def test_load_real_fixtures(self) -> None:
        """Load the actual enrichment fixtures."""
        fixtures = load_fixtures(DEFAULT_FIXTURES_DIR)
        assert len(fixtures) > 0
        # Should have county subdirectories
        counties = {f["county"] for f in fixtures}
        assert len(counties) >= 9  # 9 active counties

    def test_county_filter(self) -> None:
        """County filter returns only matching fixtures."""
        fixtures = load_fixtures(DEFAULT_FIXTURES_DIR, county_filter="los_angeles")
        assert all(f["county"] == "los_angeles" for f in fixtures)
        assert len(fixtures) > 0

    def test_nonexistent_dir(self) -> None:
        """Nonexistent directory returns empty list."""
        fixtures = load_fixtures(Path("/nonexistent/path"))
        assert fixtures == []

    def test_fixture_structure(self) -> None:
        """Each fixture has required fields."""
        fixtures = load_fixtures(DEFAULT_FIXTURES_DIR)
        if fixtures:
            f = fixtures[0]
            assert "fixture_path" in f
            assert "county" in f
            assert "ruling_text" in f
            assert "expected" in f


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


class TestCacheManagement:
    """Tests for save/load cached results."""

    def test_round_trip(self, tmp_path: Path) -> None:
        """Save and load cached results."""
        results = [
            {
                "fixture_path": "la/test.json",
                "county": "los_angeles",
                "extraction_result": {"motion_type": "msj", "outcome": "granted"},
                "latency_ms": 100.5,
                "error": None,
            }
        ]

        with patch("eval_enrichment.RESULTS_DIR", tmp_path):
            save_cached_results(results, "test-model")
            loaded = load_cached_results("test-model")

        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0]["fixture_path"] == "la/test.json"

    def test_load_nonexistent(self) -> None:
        """Load returns None for nonexistent model."""
        with patch("eval_enrichment.RESULTS_DIR", Path("/nonexistent")):
            result = load_cached_results("nonexistent-model")
        assert result is None


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


class TestReportFormatting:
    """Tests for report formatting functions."""

    def _make_summary(self, *, passed: bool = True) -> EvalSummary:
        fs = FixtureScore(
            fixture_path="la/test.json",
            county="los_angeles",
            field_scores=[
                FieldScore("motion_type", "msj", "msj", True),
                FieldScore("outcome", "granted", "granted", True),
                FieldScore("case_title", "Smith v. Jones", "Smith v. Jones", True),
            ],
            party_recall=1.0,
        )
        summary = EvalSummary(
            model="test-model",
            total_fixtures=1,
            fixture_scores=[fs],
            field_accuracy={"motion_type": 1.0, "outcome": 1.0, "case_title": 1.0},
            avg_party_recall=1.0,
            county_scores={
                "los_angeles": CountyScore(
                    county="los_angeles",
                    total_fixtures=1,
                    field_correct={"motion_type": 1, "outcome": 1, "case_title": 1},
                    field_total={"motion_type": 1, "outcome": 1, "case_title": 1},
                    avg_party_recall=1.0,
                ),
            },
        )
        if not passed:
            summary.thresholds_passed = False
            summary.threshold_failures = ["motion_type accuracy 90.0% < 95%"]
        return summary

    def test_text_report_contains_header(self) -> None:
        report = format_text_report(self._make_summary())
        assert "=== Enrichment Eval Results ===" in report
        assert "test-model" in report

    def test_text_report_contains_fields(self) -> None:
        report = format_text_report(self._make_summary())
        assert "motion_type" in report
        assert "outcome" in report
        assert "case_title" in report
        assert "parties" in report

    def test_text_report_pass_status(self) -> None:
        report = format_text_report(self._make_summary(passed=True))
        assert "Threshold check PASSED" in report

    def test_text_report_fail_status(self) -> None:
        report = format_text_report(self._make_summary(passed=False))
        assert "Threshold check FAILED" in report

    def test_text_report_county_breakdown(self) -> None:
        report = format_text_report(self._make_summary())
        assert "los_angeles" in report

    def test_json_report_valid_json(self) -> None:
        report = format_json_report(self._make_summary())
        data = json.loads(report)
        assert data["model"] == "test-model"
        assert "field_accuracy" in data
        assert "county_scores" in data
        assert "fixture_scores" in data

    def test_json_report_threshold_info(self) -> None:
        report = format_json_report(self._make_summary(passed=False))
        data = json.loads(report)
        assert data["thresholds_passed"] is False
        assert len(data["threshold_failures"]) > 0


# ---------------------------------------------------------------------------
# score_results integration
# ---------------------------------------------------------------------------


class TestScoreResults:
    """Integration tests for the full scoring pipeline."""

    def test_matching_results(self) -> None:
        fixtures = [
            {
                "fixture_path": "la/test.json",
                "county": "los_angeles",
                "ruling_text": "test",
                "expected": {
                    "motion_type": "msj",
                    "outcome": "granted",
                    "case_title": "Smith v. Jones",
                    "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
                },
            }
        ]
        results = [
            {
                "fixture_path": "la/test.json",
                "county": "los_angeles",
                "extraction_result": {
                    "motion_type": "msj",
                    "outcome": "granted",
                    "case_title": "Smith v. Jones",
                    "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
                },
                "latency_ms": 100,
                "error": None,
            }
        ]
        summary = score_results(fixtures, results, "test-model")
        assert summary.total_fixtures == 1
        assert summary.field_accuracy.get("motion_type") == 1.0
        assert summary.field_accuracy.get("outcome") == 1.0
        assert summary.field_accuracy.get("case_title") == 1.0
        assert summary.avg_party_recall == 1.0

    def test_missing_result(self) -> None:
        """Fixture without a corresponding result gets error score."""
        fixtures = [
            {
                "fixture_path": "la/test.json",
                "county": "los_angeles",
                "ruling_text": "test",
                "expected": {"motion_type": "msj"},
            }
        ]
        results: list[dict] = []  # No results
        summary = score_results(fixtures, results, "test-model")
        assert summary.total_fixtures == 1
        assert summary.fixture_scores[0].error is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    """Tests for the CLI entry point."""

    def test_help_flag(self) -> None:
        """--help should print usage and exit 0."""
        from eval_enrichment import main

        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["eval_enrichment.py", "--help"]):
                main()
        assert exc_info.value.code == 0

    def test_no_args_prints_help(self) -> None:
        """No args should print help and exit 1."""
        from eval_enrichment import main

        with patch("sys.argv", ["eval_enrichment.py"]):
            result = main()
        assert result == 1

    def test_cached_no_results(self) -> None:
        """--cached with no cached data should exit 1."""
        from eval_enrichment import main

        with patch("sys.argv", ["eval_enrichment.py", "--cached", "--model", "nonexistent"]):
            with patch("eval_enrichment.RESULTS_DIR", Path("/nonexistent")):
                result = main()
        assert result == 1

    def test_check_thresholds_exit_code(self) -> None:
        """--check-thresholds with failing thresholds should exit 1."""
        from eval_enrichment import main

        # Create cached results with wrong values
        fake_results = [
            {
                "fixture_path": "la/test.json",
                "county": "los_angeles",
                "extraction_result": {
                    "motion_type": "wrong",
                    "outcome": "wrong",
                    "case_title": "Wrong v. Wrong",
                    "parties": {"plaintiffs": ["Wrong"], "defendants": ["Wrong"]},
                },
                "latency_ms": 100,
                "error": None,
            }
        ]

        with patch("sys.argv", ["eval_enrichment.py", "--cached", "--check-thresholds"]):
            with patch("eval_enrichment.load_cached_results", return_value=fake_results):
                result = main()
        assert result == 1
