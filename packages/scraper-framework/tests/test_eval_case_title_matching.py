"""Tests for case_title matching in county-specific LLM extraction evals.

Validates that the case_title_accuracy metric works correctly across
eval_sf_extraction.py, eval_riverside_llm.py, and eval_sb_extraction.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add eval scripts to path
EVAL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "eval"
sys.path.insert(0, str(EVAL_DIR))


# ---------------------------------------------------------------------------
# Shared helper tests (match_case_title)
# ---------------------------------------------------------------------------


class TestMatchCaseTitle:
    """Tests for the fuzzy case title matching function."""

    def test_exact_match(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title("Graves v. Long", "Graves v. Long") is True

    def test_case_insensitive(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title("GRAVES V. LONG", "Graves v. Long") is True

    def test_vs_vs_v_dot(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title("Graves vs Long", "Graves v. Long") is True
        assert match_case_title("Graves vs. Long", "Graves v. Long") is True

    def test_full_name_vs_last_name(self) -> None:
        """Full name should still match via last-name containment."""
        from eval_sf_extraction import match_case_title

        assert match_case_title("Michael Edward Graves v. Ranjie Long", "Graves v. Long") is True

    def test_completely_different(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title("Smith v. Jones", "Graves v. Long") is False

    def test_none_extracted(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title(None, "Graves v. Long") is False

    def test_none_expected(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title("Graves v. Long", None) is False

    def test_both_none(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title(None, None) is True

    def test_empty_strings(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title("", "") is True
        assert match_case_title("", "Graves v. Long") is False

    def test_last_name_containment_both_sides(self) -> None:
        """Both last names from expected must appear in extracted."""
        from eval_sf_extraction import match_case_title

        # Only one last name present — should fail
        assert match_case_title("Graves v. Smith", "Graves v. Long") is False

    def test_corporate_names(self) -> None:
        """Corporate party names should match."""
        from eval_sf_extraction import match_case_title

        assert match_case_title("Garcia v. FCA US, LLC", "Garcia v. FCA US, LLC") is True

    def test_corporate_name_minor_variation(self) -> None:
        from eval_sf_extraction import match_case_title

        assert match_case_title("Garcia v. FCA US LLC", "Garcia v. FCA US, LLC") is True

    def test_bank_names(self) -> None:
        from eval_sf_extraction import match_case_title

        assert (
            match_case_title(
                "Wells Fargo Bank, N.A. v. Perini",
                "Wells Fargo Bank, N.A. v. Perini",
            )
            is True
        )

    def test_long_corporate_name(self) -> None:
        from eval_sf_extraction import match_case_title

        assert (
            match_case_title(
                "Nieto v. Creating a Legacy Inc",
                "Nieto v. Creating A Legacy, Inc.",
            )
            is True
        )


# ---------------------------------------------------------------------------
# SF score_fixture case_title tests
# ---------------------------------------------------------------------------


class TestSFScoreFixtureCaseTitle:
    """Tests for case_title_accuracy in SF eval's score_fixture."""

    def test_score_fixture_includes_case_title_metrics(self) -> None:
        from eval_sf_extraction import FixtureResult, score_fixture

        result = FixtureResult(
            fixture_name="test.pdf",
            model="test-model",
            expected={
                "expected_cases": [
                    {"case_number": "FPT-25-378624", "case_title": "Graves v. Long"},
                ]
            },
            expected_case_count=1,
            actual_case_count=1,
            rulings=[
                {
                    "extracted_case_number": "FPT-25-378624",
                    "extracted_case_title": "Graves v. Long",
                    "outcome": "granted",
                    "motion_type": "test",
                    "ruling_text": "Some ruling text here.",
                    "extracted_parties": [{"name": "Graves", "role": "petitioner"}],
                }
            ],
        )

        scores = score_fixture(result)
        assert "case_titles_correct" in scores
        assert "case_titles_total" in scores
        assert scores["case_titles_correct"] == 1
        assert scores["case_titles_total"] == 1

    def test_score_fixture_case_title_mismatch(self) -> None:
        from eval_sf_extraction import FixtureResult, score_fixture

        result = FixtureResult(
            fixture_name="test.pdf",
            model="test-model",
            expected={
                "expected_cases": [
                    {"case_number": "FPT-25-378624", "case_title": "Graves v. Long"},
                ]
            },
            expected_case_count=1,
            actual_case_count=1,
            rulings=[
                {
                    "extracted_case_number": "FPT-25-378624",
                    "extracted_case_title": "Smith v. Jones",
                    "outcome": "granted",
                    "motion_type": "test",
                    "ruling_text": "Some ruling text here.",
                    "extracted_parties": [{"name": "Smith", "role": "petitioner"}],
                }
            ],
        )

        scores = score_fixture(result)
        assert scores["case_titles_correct"] == 0
        assert scores["case_titles_total"] == 1

    def test_score_fixture_no_expected_cases(self) -> None:
        from eval_sf_extraction import FixtureResult, score_fixture

        result = FixtureResult(
            fixture_name="test.pdf",
            model="test-model",
            expected={},
            expected_case_count=0,
            actual_case_count=0,
            rulings=[],
        )

        scores = score_fixture(result)
        assert scores["case_titles_correct"] == 0
        assert scores["case_titles_total"] == 0


# ---------------------------------------------------------------------------
# SB score_fixture case_title tests
# ---------------------------------------------------------------------------


class TestSBScoreFixtureCaseTitle:
    """Tests for case_title_accuracy in SB eval's score_fixture."""

    def test_score_fixture_includes_case_title_metrics(self) -> None:
        from eval_sb_extraction import FixtureResult, score_fixture

        result = FixtureResult(
            fixture_name="test.pdf",
            model="test-model",
            expected={
                "expected_cases": [
                    {
                        "case_number": "CIVRS2502080",
                        "case_title": "Angela Carmell v. Kathleen Janet Genus-Robinson-Haywood",
                    },
                ]
            },
            expected_case_count=1,
            actual_case_count=1,
            rulings=[
                {
                    "extracted_case_number": "CIVRS2502080",
                    "extracted_case_title": (
                        "Angela Carmell v. Kathleen Janet Genus-Robinson-Haywood"
                    ),
                    "outcome": "moot",
                    "motion_type": "compel",
                    "ruling_text": "Some ruling text.",
                    "extracted_parties": [],
                }
            ],
        )

        scores = score_fixture(result)
        assert "case_titles_correct" in scores
        assert "case_titles_total" in scores
        assert scores["case_titles_correct"] == 1
        assert scores["case_titles_total"] == 1


# ---------------------------------------------------------------------------
# Riverside score_fixture case_title tests
# ---------------------------------------------------------------------------


class TestRiversideScoreFixtureCaseTitle:
    """Tests for case_title_accuracy in Riverside eval's score_fixture."""

    def test_score_fixture_includes_case_title_metrics(self) -> None:
        from eval_riverside_llm import FixtureResult, score_fixture

        result = FixtureResult(
            fixture_name="test.pdf",
            model="test-model",
            expected={
                "expected_cases": [
                    {"case_number": "CVPS2306157", "case_title": "Lachon Yeldell v. Henss"},
                ]
            },
            expected_case_count=1,
            actual_case_count=1,
            rulings=[
                {
                    "extracted_case_number": "CVPS2306157",
                    "extracted_case_title": "Lachon Yeldell v. Henss",
                    "outcome": "denied",
                    "motion_type": "demurrer",
                    "ruling_text": "Ruling text here.",
                }
            ],
        )

        scores = score_fixture(result)
        assert "case_titles_correct" in scores
        assert "case_titles_total" in scores
        assert scores["case_titles_correct"] == 1
        assert scores["case_titles_total"] == 1

    def test_score_fixture_zero_case_fixture(self) -> None:
        """Fixtures with 0 cases should have 0 case_titles_total."""
        from eval_riverside_llm import FixtureResult, score_fixture

        result = FixtureResult(
            fixture_name="test.pdf",
            model="test-model",
            expected={},
            expected_case_count=0,
            actual_case_count=0,
            rulings=[],
        )

        scores = score_fixture(result)
        assert scores["case_titles_correct"] == 0
        assert scores["case_titles_total"] == 0

    def test_score_fixture_includes_case_number_metrics(self) -> None:
        """Riverside should now also track case_number accuracy."""
        from eval_riverside_llm import FixtureResult, score_fixture

        result = FixtureResult(
            fixture_name="test.pdf",
            model="test-model",
            expected={
                "expected_cases": [
                    {"case_number": "CVPS2306157", "case_title": "Lachon Yeldell v. Henss"},
                ]
            },
            expected_case_count=1,
            actual_case_count=1,
            rulings=[
                {
                    "extracted_case_number": "CVPS2306157",
                    "extracted_case_title": "Lachon Yeldell v. Henss",
                    "outcome": "denied",
                    "motion_type": "demurrer",
                    "ruling_text": "Some text.",
                }
            ],
        )

        scores = score_fixture(result)
        assert "case_numbers_correct" in scores
        assert "case_numbers_total" in scores
        assert scores["case_numbers_correct"] == 1
        assert scores["case_numbers_total"] == 1


# ---------------------------------------------------------------------------
# ModelSummary aggregation tests
# ---------------------------------------------------------------------------


class TestModelSummaryCaseTitleAggregation:
    """Tests for case_title aggregation in ModelSummary across all eval scripts."""

    def test_sf_model_summary_aggregates_case_titles(self) -> None:
        from eval_sf_extraction import FixtureResult, analyze_model

        results = [
            FixtureResult(
                fixture_name="test1.pdf",
                model="test",
                expected={
                    "expected_cases": [
                        {"case_number": "FPT-25-378624", "case_title": "Graves v. Long"},
                    ]
                },
                expected_case_count=1,
                actual_case_count=1,
                rulings=[
                    {
                        "extracted_case_number": "FPT-25-378624",
                        "extracted_case_title": "Graves v. Long",
                        "outcome": "granted",
                        "motion_type": "test",
                        "ruling_text": "Text.",
                        "extracted_parties": [],
                    }
                ],
            ),
        ]

        summary = analyze_model(results, "test", {"input": 0.1, "output": 0.1})
        assert summary.case_titles_correct == 1
        assert summary.case_titles_total == 1

    def test_riverside_model_summary_aggregates_case_titles(self) -> None:
        from eval_riverside_llm import FixtureResult, analyze_model

        results = [
            FixtureResult(
                fixture_name="test1.pdf",
                model="test",
                expected={
                    "expected_cases": [
                        {"case_number": "CVPS2306157", "case_title": "Lachon Yeldell v. Henss"},
                    ]
                },
                expected_case_count=1,
                actual_case_count=1,
                rulings=[
                    {
                        "extracted_case_number": "CVPS2306157",
                        "extracted_case_title": "Lachon Yeldell v. Henss",
                        "outcome": "denied",
                        "motion_type": "demurrer",
                        "ruling_text": "Text.",
                    }
                ],
            ),
        ]

        summary = analyze_model(results, "test", {"input": 0.1, "output": 0.1})
        assert summary.case_titles_correct == 1
        assert summary.case_titles_total == 1

    def test_sb_model_summary_aggregates_case_titles(self) -> None:
        from eval_sb_extraction import FixtureResult, analyze_model

        results = [
            FixtureResult(
                fixture_name="test1.pdf",
                model="test",
                expected={
                    "expected_cases": [
                        {"case_number": "CIVRS2502080", "case_title": "Angela Carmell v. Test"},
                    ]
                },
                expected_case_count=1,
                actual_case_count=1,
                rulings=[
                    {
                        "extracted_case_number": "CIVRS2502080",
                        "extracted_case_title": "Angela Carmell v. Test",
                        "outcome": "moot",
                        "motion_type": "compel",
                        "ruling_text": "Text.",
                        "extracted_parties": [],
                    }
                ],
            ),
        ]

        summary = analyze_model(results, "test", {"input": 0.1, "output": 0.1})
        assert summary.case_titles_correct == 1
        assert summary.case_titles_total == 1
