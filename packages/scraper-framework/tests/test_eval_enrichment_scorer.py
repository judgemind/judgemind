"""Tests for the enrichment eval scoring module.

Tests scoring functions including exact match, fuzzy match,
party recall, aggregation, threshold checking, and report formatting.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts/eval to path for imports
SCRIPTS_EVAL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "eval"
sys.path.insert(0, str(SCRIPTS_EVAL_DIR))

from eval_enrichment_scorer import (  # noqa: E402
    VALID_MOTION_TYPES,
    VALID_OUTCOMES,
    EnrichmentEvalSummary,
    FieldScore,
    FixtureScore,
    PartyScore,
    aggregate_scores,
    compare_case_title,
    compare_exact,
    compare_with_alternatives,
    compute_party_recall,
    format_json_report,
    format_text_report,
    normalize_to_taxonomy,
    score_fixture,
)

# ---------------------------------------------------------------------------
# compare_exact tests
# ---------------------------------------------------------------------------


class TestCompareExact:
    """Tests for exact match comparison (motion_type, outcome)."""

    def test_exact_match_same_case(self) -> None:
        assert compare_exact("msj", "msj") is True

    def test_exact_match_case_insensitive(self) -> None:
        assert compare_exact("MSJ", "msj") is True
        assert compare_exact("Granted", "granted") is True

    def test_exact_match_with_whitespace(self) -> None:
        assert compare_exact("  msj  ", "msj") is True

    def test_exact_mismatch(self) -> None:
        assert compare_exact("msj", "mtd") is False
        assert compare_exact("granted", "denied") is False

    def test_exact_none_both(self) -> None:
        assert compare_exact(None, None) is True

    def test_exact_none_one(self) -> None:
        assert compare_exact("msj", None) is False
        assert compare_exact(None, "msj") is False


# ---------------------------------------------------------------------------
# normalize_to_taxonomy tests
# ---------------------------------------------------------------------------


class TestNormalizeToTaxonomy:
    """Tests for taxonomy normalization."""

    def test_valid_motion_type_unchanged(self) -> None:
        assert normalize_to_taxonomy("msj", VALID_MOTION_TYPES) == "msj"
        assert normalize_to_taxonomy("demurrer", VALID_MOTION_TYPES) == "demurrer"
        assert normalize_to_taxonomy("other", VALID_MOTION_TYPES) == "other"

    def test_invalid_motion_type_maps_to_other(self) -> None:
        assert normalize_to_taxonomy("anti_slapp", VALID_MOTION_TYPES) == "other"
        assert normalize_to_taxonomy("motion_to_consolidate", VALID_MOTION_TYPES) == "other"
        assert normalize_to_taxonomy("default_judgment", VALID_MOTION_TYPES) == "other"
        assert normalize_to_taxonomy("ex_parte_application", VALID_MOTION_TYPES) == "other"

    def test_case_insensitive(self) -> None:
        assert normalize_to_taxonomy("MSJ", VALID_MOTION_TYPES) == "msj"
        assert normalize_to_taxonomy("Demurrer", VALID_MOTION_TYPES) == "demurrer"

    def test_whitespace_stripped(self) -> None:
        assert normalize_to_taxonomy("  msj  ", VALID_MOTION_TYPES) == "msj"

    def test_none_returns_none(self) -> None:
        assert normalize_to_taxonomy(None, VALID_MOTION_TYPES) is None

    def test_valid_outcome_unchanged(self) -> None:
        assert normalize_to_taxonomy("granted", VALID_OUTCOMES) == "granted"
        assert normalize_to_taxonomy("denied", VALID_OUTCOMES) == "denied"
        assert normalize_to_taxonomy("off_calendar", VALID_OUTCOMES) == "off_calendar"

    def test_invalid_outcome_maps_to_other(self) -> None:
        assert normalize_to_taxonomy("sustained", VALID_OUTCOMES) == "other"
        assert normalize_to_taxonomy("overruled", VALID_OUTCOMES) == "other"


# ---------------------------------------------------------------------------
# compare_with_alternatives tests
# ---------------------------------------------------------------------------


class TestCompareWithAlternatives:
    """Tests for comparison with taxonomy normalization and alternatives."""

    def test_direct_match(self) -> None:
        assert compare_with_alternatives("msj", "msj", None, VALID_MOTION_TYPES) is True

    def test_non_taxonomy_expected_matches_other(self) -> None:
        """If expected is 'anti_slapp' (not in taxonomy), LLM returning 'other' is correct."""
        assert compare_with_alternatives("anti_slapp", "other", None, VALID_MOTION_TYPES) is True

    def test_non_taxonomy_expected_vs_taxonomy_mismatch(self) -> None:
        """If expected is 'anti_slapp' (-> other) but LLM returns 'msj', it's wrong."""
        assert compare_with_alternatives("anti_slapp", "msj", None, VALID_MOTION_TYPES) is False

    def test_alternative_match(self) -> None:
        """If expected is 'msj' but LLM returns 'motion_for_summary_adjudication',
        and alternatives include it, it should match."""
        assert (
            compare_with_alternatives(
                "msj",
                "motion_for_summary_adjudication",
                ["motion_for_summary_adjudication"],
                VALID_MOTION_TYPES,
            )
            is True
        )

    def test_alternative_with_taxonomy_normalization(self) -> None:
        """Alternatives that are outside taxonomy should be normalized to 'other'."""
        # expected='msj', alternatives=['default_judgment'] (-> other)
        # LLM returns 'other' -> matches the normalized alternative
        assert (
            compare_with_alternatives(
                "msj",
                "other",
                ["default_judgment"],
                VALID_MOTION_TYPES,
            )
            is True
        )

    def test_no_match_without_alternatives(self) -> None:
        assert compare_with_alternatives("msj", "mtd", None, VALID_MOTION_TYPES) is False

    def test_none_values(self) -> None:
        assert compare_with_alternatives(None, None, None, VALID_MOTION_TYPES) is True
        assert compare_with_alternatives("msj", None, None, VALID_MOTION_TYPES) is False

    def test_without_taxonomy(self) -> None:
        """When no valid_values provided, does plain lowercase comparison."""
        assert compare_with_alternatives("abc", "abc", None) is True
        assert compare_with_alternatives("abc", "def", ["def"]) is True


# ---------------------------------------------------------------------------
# compare_case_title tests
# ---------------------------------------------------------------------------


class TestCompareCaseTitle:
    """Tests for fuzzy case title matching."""

    def test_exact_match(self) -> None:
        assert compare_case_title("Smith v. Jones", "Smith v. Jones") is True

    def test_case_insensitive(self) -> None:
        assert compare_case_title("Smith v. Jones", "smith v. jones") is True

    def test_without_period(self) -> None:
        """'Smith v Jones' should match 'Smith v. Jones' at >= 0.85."""
        assert compare_case_title("Smith v Jones", "Smith v. Jones") is True

    def test_et_al_stripped(self) -> None:
        assert (
            compare_case_title(
                "Smith v. Jones, et al.",
                "Smith v. Jones",
            )
            is True
        )

    def test_et_al_variation(self) -> None:
        assert (
            compare_case_title(
                "Smith v. Jones et al",
                "Smith v. Jones",
            )
            is True
        )

    def test_whitespace_normalization(self) -> None:
        assert (
            compare_case_title(
                "Smith  v.   Jones",
                "Smith v. Jones",
            )
            is True
        )

    def test_vs_normalized_to_v(self) -> None:
        assert (
            compare_case_title(
                "Smith vs. Jones",
                "Smith v. Jones",
            )
            is True
        )

    def test_versus_normalized_to_v(self) -> None:
        assert (
            compare_case_title(
                "Smith versus Jones",
                "Smith v. Jones",
            )
            is True
        )

    def test_different_parties(self) -> None:
        """Completely different case titles should not match."""
        assert (
            compare_case_title(
                "Smith v. Jones",
                "Williams v. Anderson",
            )
            is False
        )

    def test_none_both(self) -> None:
        assert compare_case_title(None, None) is True

    def test_none_one(self) -> None:
        assert compare_case_title("Smith v. Jones", None) is False
        assert compare_case_title(None, "Smith v. Jones") is False

    def test_similar_title_levenshtein(self) -> None:
        """Titles with minor differences should match via Levenshtein."""
        assert (
            compare_case_title(
                "Hernandez v. Pacific Coast Properties",
                "Hernandez v. Pacific Coast Properties LLC",
            )
            is True
        )

    def test_token_overlap_match(self) -> None:
        """Titles with >80% token overlap should match."""
        assert (
            compare_case_title(
                "Garcia v. State Farm Insurance Company",
                "Garcia v. State Farm Insurance",
            )
            is True
        )


# ---------------------------------------------------------------------------
# compute_party_recall tests
# ---------------------------------------------------------------------------


class TestComputePartyRecall:
    """Tests for party recall computation."""

    def test_perfect_recall(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        extracted = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        score = compute_party_recall(expected, extracted)
        assert score.recall == 1.0
        assert score.found_count == 2
        assert score.expected_count == 2
        assert score.missing == []

    def test_partial_recall(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones", "Acme Corp."]}
        extracted = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        score = compute_party_recall(expected, extracted)
        assert score.recall == pytest.approx(2.0 / 3.0)
        assert score.found_count == 2
        assert score.expected_count == 3
        assert "Acme Corp." in score.missing

    def test_zero_recall(self) -> None:
        expected = {"plaintiffs": ["Smith"], "defendants": ["Jones"]}
        extracted = {"plaintiffs": [], "defendants": []}
        score = compute_party_recall(expected, extracted)
        assert score.recall == 0.0
        assert score.found_count == 0

    def test_fuzzy_match(self) -> None:
        """Party names with minor differences should match via Levenshtein."""
        expected = {"plaintiffs": ["John Smith"], "defendants": ["Jane Jones"]}
        extracted = {"plaintiffs": ["John  Smith"], "defendants": ["Jane Jones"]}
        score = compute_party_recall(expected, extracted)
        assert score.recall == 1.0

    def test_empty_expected(self) -> None:
        expected: dict[str, list[str]] = {"plaintiffs": [], "defendants": []}
        extracted = {"plaintiffs": ["Smith"], "defendants": []}
        score = compute_party_recall(expected, extracted)
        assert score.recall == 1.0
        assert score.expected_count == 0

    def test_case_insensitive(self) -> None:
        expected = {"plaintiffs": ["SMITH"], "defendants": ["jones"]}
        extracted = {"plaintiffs": ["smith"], "defendants": ["Jones"]}
        score = compute_party_recall(expected, extracted)
        assert score.recall == 1.0

    def test_cross_role_matching(self) -> None:
        """Expected party in plaintiff should match even if extracted as defendant."""
        expected = {"plaintiffs": ["Smith"], "defendants": []}
        extracted = {"plaintiffs": [], "defendants": ["Smith"]}
        score = compute_party_recall(expected, extracted)
        assert score.recall == 1.0


# ---------------------------------------------------------------------------
# score_fixture tests
# ---------------------------------------------------------------------------


class TestScoreFixture:
    """Tests for scoring a single fixture."""

    def test_all_correct(self) -> None:
        expected = {
            "case_title": "Smith v. Jones",
            "motion_type": "msj",
            "outcome": "granted",
            "parties": {
                "plaintiffs": ["Smith"],
                "defendants": ["Jones"],
            },
        }
        extraction = {
            "case_title": "Smith v. Jones",
            "motion_type": "msj",
            "outcome": "granted",
            "parties": {
                "plaintiffs": ["Smith"],
                "defendants": ["Jones"],
            },
        }
        score = score_fixture("la/test-1", "la", expected, extraction)
        assert score.error is None
        for fs in score.field_scores:
            assert fs.match is True, f"{fs.field_name} should match"
        assert score.party_score is not None
        assert score.party_score.recall == 1.0

    def test_motion_type_mismatch(self) -> None:
        expected = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        extraction = {
            "motion_type": "motion_for_summary_adjudication",
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        score = score_fixture("oc/test-1", "oc", expected, extraction)
        motion_score = next(f for f in score.field_scores if f.field_name == "motion_type")
        assert motion_score.match is False

    def test_none_extraction_result(self) -> None:
        expected = {"motion_type": "msj"}
        score = score_fixture("la/test-1", "la", expected, None)
        assert score.error == "No extraction result"

    def test_missing_field_in_extraction(self) -> None:
        """If extraction result is missing a field, it should not match."""
        expected = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        extraction = {
            "motion_type": None,
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        score = score_fixture("la/test-1", "la", expected, extraction)
        motion_score = next(f for f in score.field_scores if f.field_name == "motion_type")
        assert motion_score.match is False

    def test_non_taxonomy_motion_type_normalized_to_other(self) -> None:
        """If fixture expects 'anti_slapp' and LLM returns 'other', it matches."""
        expected = {
            "motion_type": "anti_slapp",
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        extraction = {
            "motion_type": "other",
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        score = score_fixture("la/test-1", "la", expected, extraction)
        motion_score = next(f for f in score.field_scores if f.field_name == "motion_type")
        assert motion_score.match is True

    def test_acceptable_alternatives_used(self) -> None:
        """Acceptable alternatives should be checked when primary doesn't match."""
        expected = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        extraction = {
            "motion_type": "motion_for_summary_adjudication",
            "outcome": "granted",
            "case_title": "Test v. Case",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        alternatives = {"motion_type": ["motion_for_summary_adjudication"]}
        score = score_fixture(
            "la/test-1",
            "la",
            expected,
            extraction,
            acceptable_alternatives=alternatives,
        )
        motion_score = next(f for f in score.field_scores if f.field_name == "motion_type")
        assert motion_score.match is True

    def test_case_title_alternatives(self) -> None:
        """Case title alternatives should be checked with fuzzy matching."""
        expected = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Smith v. Jones",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        extraction = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Jane Smith v. Robert Jones",
            "parties": {"plaintiffs": [], "defendants": []},
        }
        alternatives = {"case_title": ["Jane Smith v. Robert Jones"]}
        score = score_fixture(
            "la/test-1",
            "la",
            expected,
            extraction,
            acceptable_alternatives=alternatives,
        )
        title_score = next(f for f in score.field_scores if f.field_name == "case_title")
        assert title_score.match is True

    def test_case_title_in_text_false_skips_case_title(self) -> None:
        """When case_title_in_text=False, case_title should not be scored."""
        expected = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Smith v. Jones",
            "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
        }
        extraction = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": None,  # LLM couldn't extract — but that's expected
            "parties": {"plaintiffs": [], "defendants": []},
        }
        score = score_fixture(
            "la/test-1",
            "la",
            expected,
            extraction,
            case_title_in_text=False,
        )
        # case_title should NOT appear in field_scores
        field_names = [f.field_name for f in score.field_scores]
        assert "case_title" not in field_names
        # parties should also be skipped
        assert score.party_score is None
        # motion_type and outcome should still be scored
        assert "motion_type" in field_names
        assert "outcome" in field_names

    def test_case_title_in_text_true_default_scores_normally(self) -> None:
        """Default behavior (case_title_in_text=True) scores case_title normally."""
        expected = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Smith v. Jones",
            "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
        }
        extraction = {
            "motion_type": "msj",
            "outcome": "granted",
            "case_title": "Smith v. Jones",
            "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
        }
        score = score_fixture("la/test-1", "la", expected, extraction)
        field_names = [f.field_name for f in score.field_scores]
        assert "case_title" in field_names
        assert score.party_score is not None


# ---------------------------------------------------------------------------
# aggregate_scores tests
# ---------------------------------------------------------------------------


class TestAggregateScores:
    """Tests for score aggregation."""

    def _make_perfect_fixture(self, fixture_id: str, county: str) -> FixtureScore:
        return FixtureScore(
            fixture_id=fixture_id,
            county=county,
            field_scores=[
                FieldScore("motion_type", "msj", "msj", True),
                FieldScore("outcome", "granted", "granted", True),
                FieldScore("case_title", "A v. B", "A v. B", True),
            ],
            party_score=PartyScore(2, 2, 1.0),
        )

    def _make_failing_fixture(self, fixture_id: str, county: str) -> FixtureScore:
        return FixtureScore(
            fixture_id=fixture_id,
            county=county,
            field_scores=[
                FieldScore("motion_type", "msj", "mtd", False),
                FieldScore("outcome", "granted", "denied", False),
                FieldScore("case_title", "A v. B", "C v. D", False),
            ],
            party_score=PartyScore(2, 0, 0.0, ["A", "B"]),
        )

    def test_all_passing(self) -> None:
        fixtures = [
            self._make_perfect_fixture("la/1", "la"),
            self._make_perfect_fixture("la/2", "la"),
            self._make_perfect_fixture("oc/1", "oc"),
        ]
        summary = aggregate_scores(fixtures, "test-model")
        assert summary.total_fixtures == 3
        assert summary.field_accuracy["motion_type"] == 1.0
        assert summary.field_accuracy["outcome"] == 1.0
        assert summary.field_accuracy["case_title"] == 1.0
        assert summary.avg_party_recall == 1.0
        assert summary.thresholds_passed is True

    def test_below_threshold(self) -> None:
        """If accuracy is below threshold, thresholds_passed should be False."""
        fixtures = [
            self._make_perfect_fixture("la/1", "la"),
            self._make_failing_fixture("la/2", "la"),
        ]
        summary = aggregate_scores(fixtures, "test-model")
        assert summary.thresholds_passed is False
        assert len(summary.threshold_failures) > 0

    def test_per_county_breakdown(self) -> None:
        fixtures = [
            self._make_perfect_fixture("la/1", "la"),
            self._make_failing_fixture("oc/1", "oc"),
        ]
        summary = aggregate_scores(fixtures, "test-model")
        assert "la" in summary.county_scores
        assert "oc" in summary.county_scores
        assert summary.county_scores["la"].total_fixtures == 1
        assert summary.county_scores["oc"].total_fixtures == 1

        la_mt_acc = summary.county_scores["la"].field_correct.get(
            "motion_type", 0
        ) / summary.county_scores["la"].field_total.get("motion_type", 1)
        assert la_mt_acc == 1.0

        oc_mt_acc = summary.county_scores["oc"].field_correct.get(
            "motion_type", 0
        ) / summary.county_scores["oc"].field_total.get("motion_type", 1)
        assert oc_mt_acc == 0.0


# ---------------------------------------------------------------------------
# Threshold checking tests
# ---------------------------------------------------------------------------


class TestThresholdChecking:
    """Tests for threshold pass/fail logic."""

    def _make_summary_with_accuracy(
        self,
        motion_type_acc: float,
        outcome_acc: float,
        case_title_acc: float,
        party_recall: float,
    ) -> EnrichmentEvalSummary:
        """Create a summary with specific accuracy values."""
        summary = EnrichmentEvalSummary(model="test")
        summary.field_accuracy = {
            "motion_type": motion_type_acc,
            "outcome": outcome_acc,
            "case_title": case_title_acc,
        }
        summary.avg_party_recall = party_recall
        # Re-run threshold check
        from eval_enrichment_scorer import _check_thresholds

        _check_thresholds(summary)
        return summary

    def test_all_above_threshold(self) -> None:
        summary = self._make_summary_with_accuracy(0.96, 0.96, 0.91, 0.86)
        assert summary.thresholds_passed is True
        assert summary.threshold_failures == []

    def test_motion_type_below(self) -> None:
        summary = self._make_summary_with_accuracy(0.90, 0.96, 0.91, 0.86)
        assert summary.thresholds_passed is False
        assert any("motion_type" in f for f in summary.threshold_failures)

    def test_outcome_below(self) -> None:
        summary = self._make_summary_with_accuracy(0.96, 0.90, 0.91, 0.86)
        assert summary.thresholds_passed is False
        assert any("outcome" in f for f in summary.threshold_failures)

    def test_case_title_below(self) -> None:
        summary = self._make_summary_with_accuracy(0.96, 0.96, 0.85, 0.86)
        assert summary.thresholds_passed is False
        assert any("case_title" in f for f in summary.threshold_failures)

    def test_parties_below(self) -> None:
        summary = self._make_summary_with_accuracy(0.96, 0.96, 0.91, 0.80)
        assert summary.thresholds_passed is False
        assert any("parties" in f for f in summary.threshold_failures)


# ---------------------------------------------------------------------------
# Report formatting tests
# ---------------------------------------------------------------------------


class TestFormatTextReport:
    """Tests for text report formatting."""

    def test_report_contains_header(self) -> None:
        fixtures = [
            FixtureScore(
                fixture_id="la/test-1",
                county="la",
                field_scores=[
                    FieldScore("motion_type", "msj", "msj", True),
                    FieldScore("outcome", "granted", "granted", True),
                    FieldScore("case_title", "A v. B", "A v. B", True),
                ],
                party_score=PartyScore(2, 2, 1.0),
            ),
        ]
        summary = aggregate_scores(fixtures, "test-model")
        report = format_text_report(summary)
        assert "=== Enrichment Eval Results ===" in report
        assert "Model: test-model" in report
        assert "Fixtures: 1" in report

    def test_report_per_county(self) -> None:
        fixtures = [
            FixtureScore(
                fixture_id="la/test-1",
                county="la",
                field_scores=[
                    FieldScore("motion_type", "msj", "msj", True),
                    FieldScore("outcome", "granted", "granted", True),
                    FieldScore("case_title", "A v. B", "A v. B", True),
                ],
                party_score=PartyScore(2, 2, 1.0),
            ),
            FixtureScore(
                fixture_id="oc/test-1",
                county="oc",
                field_scores=[
                    FieldScore("motion_type", "msj", "msj", True),
                    FieldScore("outcome", "granted", "granted", True),
                    FieldScore("case_title", "C v. D", "C v. D", True),
                ],
                party_score=PartyScore(2, 2, 1.0),
            ),
        ]
        summary = aggregate_scores(fixtures, "test-model")
        report = format_text_report(summary)
        assert "Per-county breakdown:" in report
        assert "la:" in report
        assert "oc:" in report

    def test_report_shows_failures(self) -> None:
        fixtures = [
            FixtureScore(
                fixture_id="oc/test-1",
                county="oc",
                field_scores=[
                    FieldScore("motion_type", "msj", "mtd", False),
                    FieldScore("outcome", "granted", "granted", True),
                    FieldScore("case_title", "A v. B", "A v. B", True),
                ],
                party_score=PartyScore(2, 2, 1.0),
            ),
        ]
        summary = aggregate_scores(fixtures, "test-model")
        report = format_text_report(summary)
        assert "Failures:" in report
        assert 'expected="msj"' in report
        assert 'got="mtd"' in report


class TestFormatJsonReport:
    """Tests for JSON report formatting."""

    def test_json_has_required_keys(self) -> None:
        fixtures = [
            FixtureScore(
                fixture_id="la/test-1",
                county="la",
                field_scores=[
                    FieldScore("motion_type", "msj", "msj", True),
                    FieldScore("outcome", "granted", "granted", True),
                    FieldScore("case_title", "A v. B", "A v. B", True),
                ],
                party_score=PartyScore(2, 2, 1.0),
            ),
        ]
        summary = aggregate_scores(fixtures, "test-model")
        result = format_json_report(summary)
        assert "model" in result
        assert "total_fixtures" in result
        assert "field_accuracy" in result
        assert "avg_party_recall" in result
        assert "thresholds_passed" in result
        assert "county_scores" in result
        assert "fixture_scores" in result


# ---------------------------------------------------------------------------
# Integration-style test with fixture loading
# ---------------------------------------------------------------------------


class TestFixtureLoading:
    """Tests for loading and scoring enrichment fixtures end-to-end."""

    def test_load_and_score_real_fixtures(self) -> None:
        """Integration test: load real fixtures and score a perfect extraction."""
        fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "enrichment"
        if not fixtures_dir.exists():
            pytest.skip("Enrichment fixtures not found")

        # Import the CLI module's load function
        sys.path.insert(0, str(SCRIPTS_EVAL_DIR))
        from eval_enrichment import load_fixtures

        fixtures = load_fixtures(fixtures_dir)
        assert len(fixtures) >= 100, f"Expected 100+ fixtures, found {len(fixtures)}"

        # Each fixture should have the required keys
        for f in fixtures:
            assert "fixture_id" in f
            assert "county" in f
            assert "ruling_text" in f
            assert "expected" in f
            # acceptable_alternatives may or may not be present
            assert "acceptable_alternatives" in f  # key exists (may be None)

        # Simulate extraction results (perfect match) and score.
        # When expected values are outside the taxonomy, the "perfect"
        # extraction should return the normalized value ("other").
        from eval_enrichment_scorer import VALID_MOTION_TYPES, VALID_OUTCOMES

        results = []
        for f in fixtures:
            # Build a "perfect" extraction by normalizing expected values
            expected = f["expected"]
            mt = expected.get("motion_type")
            if mt and mt.strip().lower() not in VALID_MOTION_TYPES:
                mt = "other"
            oc = expected.get("outcome")
            if oc and oc.strip().lower() not in VALID_OUTCOMES:
                oc = "other"
            perfect_extraction = {
                "case_title": expected.get("case_title"),
                "motion_type": mt,
                "outcome": oc,
                "parties": expected.get("parties", {}),
            }
            results.append(
                {
                    "fixture_id": f["fixture_id"],
                    "county": f["county"],
                    "extraction_result": perfect_extraction,
                    "latency_ms": 100,
                    "error": None,
                }
            )

        from eval_enrichment import score_results

        fixture_scores = score_results(results, fixtures)
        assert len(fixture_scores) == len(fixtures)

        summary = aggregate_scores(fixture_scores, "test-perfect")
        assert summary.thresholds_passed is True

    def test_load_fixtures_county_filter(self) -> None:
        """Test that county filter works."""
        fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "enrichment"
        if not fixtures_dir.exists():
            pytest.skip("Enrichment fixtures not found")

        sys.path.insert(0, str(SCRIPTS_EVAL_DIR))
        from eval_enrichment import load_fixtures

        la_fixtures = load_fixtures(fixtures_dir, county_filter="los_angeles")
        for f in la_fixtures:
            assert f["county"] == "los_angeles"

        oc_fixtures = load_fixtures(fixtures_dir, county_filter="orange")
        for f in oc_fixtures:
            assert f["county"] == "orange"

    def test_load_fixtures_includes_acceptable_alternatives(self) -> None:
        """Verify that acceptable_alternatives are loaded from fixture JSON."""
        fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "enrichment"
        if not fixtures_dir.exists():
            pytest.skip("Enrichment fixtures not found")

        sys.path.insert(0, str(SCRIPTS_EVAL_DIR))
        from eval_enrichment import load_fixtures

        fixtures = load_fixtures(fixtures_dir)
        # At least some fixtures should have acceptable_alternatives
        with_alts = [f for f in fixtures if f.get("acceptable_alternatives")]
        assert len(with_alts) >= 1, "Expected at least 1 fixture with acceptable_alternatives"

    def test_load_fixtures_includes_case_title_in_text(self) -> None:
        """Verify that case_title_in_text flag is loaded from fixture JSON."""
        fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "enrichment"
        if not fixtures_dir.exists():
            pytest.skip("Enrichment fixtures not found")

        sys.path.insert(0, str(SCRIPTS_EVAL_DIR))
        from eval_enrichment import load_fixtures

        fixtures = load_fixtures(fixtures_dir)
        # All fixtures should have the case_title_in_text key
        for f in fixtures:
            assert "case_title_in_text" in f, (
                f"Fixture {f['fixture_id']} missing case_title_in_text"
            )
            assert isinstance(f["case_title_in_text"], bool)

        # At least some fixtures should have case_title_in_text=False
        # (the 4 annotated fixtures)
        without_title = [f for f in fixtures if not f["case_title_in_text"]]
        assert len(without_title) >= 4, (
            f"Expected at least 4 fixtures with case_title_in_text=False, "
            f"found {len(without_title)}"
        )

    def test_score_results_passes_case_title_in_text(self) -> None:
        """Verify that score_results passes case_title_in_text to score_fixture."""
        fixtures = [
            {
                "fixture_id": "test/1",
                "county": "test",
                "ruling_text": "some text",
                "expected": {
                    "motion_type": "msj",
                    "outcome": "granted",
                    "case_title": "Smith v. Jones",
                    "parties": {"plaintiffs": ["Smith"], "defendants": ["Jones"]},
                },
                "source_doc": "",
                "notes": "",
                "acceptable_alternatives": None,
                "case_title_in_text": False,
            },
        ]
        results = [
            {
                "fixture_id": "test/1",
                "county": "test",
                "extraction_result": {
                    "motion_type": "msj",
                    "outcome": "granted",
                    "case_title": None,
                    "parties": {"plaintiffs": [], "defendants": []},
                },
                "latency_ms": 100,
                "error": None,
            },
        ]

        sys.path.insert(0, str(SCRIPTS_EVAL_DIR))
        from eval_enrichment import score_results

        fixture_scores = score_results(results, fixtures)
        assert len(fixture_scores) == 1
        # case_title should be skipped due to case_title_in_text=False
        field_names = [f.field_name for f in fixture_scores[0].field_scores]
        assert "case_title" not in field_names
        assert fixture_scores[0].party_score is None
