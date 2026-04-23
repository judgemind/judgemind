"""Tests for the LA extraction eval script.

Tests the scoring, fixture loading, normalization, and contamination check
functions without requiring LLM API calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add eval scripts directory to path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
EVAL_DIR = REPO_ROOT / "scripts" / "eval"
sys.path.insert(0, str(EVAL_DIR))

from eval_la_extraction import (  # noqa: E402
    CaseResult,
    FixtureResult,
    extract_html_text,
    get_la_fixtures,
    is_party_contaminated,
    normalize_outcome,
    normalize_title,
    score_fixture,
    titles_match,
)

# ---------------------------------------------------------------------------
# Fixture loading tests
# ---------------------------------------------------------------------------


class TestGetLaFixtures:
    """Tests for get_la_fixtures."""

    def test_returns_fixtures_with_cases_field(self) -> None:
        """Only fixtures with 'cases' field should be returned."""
        fixtures = get_la_fixtures()
        assert len(fixtures) > 0
        for _path, expected in fixtures:
            assert "cases" in expected
            assert expected.get("case_count", 0) > 0

    def test_returns_html_files(self) -> None:
        """All returned fixtures must be .html files."""
        fixtures = get_la_fixtures()
        for path, _expected in fixtures:
            assert path.suffix == ".html"

    def test_excludes_error_pages(self) -> None:
        """Stale viewstate error pages (case_count=0) should be excluded."""
        fixtures = get_la_fixtures()
        fixture_names = [p.name for p, _ in fixtures]
        # These are known error page fixtures
        assert "la_ruling_van_a.html" not in fixture_names
        assert "la_ruling_ea_h.html" not in fixture_names
        assert "la_ruling_smc49.html" not in fixture_names
        assert "la_ruling_smc1.html" not in fixture_names


# ---------------------------------------------------------------------------
# HTML text extraction tests
# ---------------------------------------------------------------------------

FIXTURES_DIR = REPO_ROOT / "packages" / "scraper-framework" / "tests" / "fixtures"


class TestExtractHtmlText:
    """Tests for extract_html_text."""

    def test_extracts_text_from_valid_fixture(self) -> None:
        """Should extract text from fixture with speechSynthesis div."""
        fixture_path = FIXTURES_DIR / "la_ruling_pas_p.html"
        if not fixture_path.exists():
            pytest.skip("Fixture not found")
        text = extract_html_text(fixture_path)
        assert text is not None
        assert "DEPARTMENT P LAW AND MOTION RULINGS" in text
        assert "Case Number:" in text
        assert "25NNCV00140" in text

    def test_returns_none_for_error_page(self) -> None:
        """Error pages without speechSynthesis div return None."""
        fixture_path = FIXTURES_DIR / "la_ruling_van_a.html"
        if not fixture_path.exists():
            pytest.skip("Fixture not found")
        text = extract_html_text(fixture_path)
        assert text is None

    def test_multicase_fixture_contains_both_cases(self) -> None:
        """Multi-case fixtures should contain all case numbers."""
        fixture_path = FIXTURES_DIR / "la_ruling_response.html"
        if not fixture_path.exists():
            pytest.skip("Fixture not found")
        text = extract_html_text(fixture_path)
        assert text is not None
        assert "24NNCV02551" in text
        assert "26NNCP00062" in text


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


class TestNormalizeTitle:
    """Tests for normalize_title."""

    def test_basic_normalization(self) -> None:
        assert normalize_title("Smith v. Jones") == "smith v. jones"

    def test_vs_normalization(self) -> None:
        assert normalize_title("Smith vs. Jones") == "smith v. jones"
        assert normalize_title("Smith vs Jones") == "smith v. jones"

    def test_et_al_removal(self) -> None:
        assert normalize_title("Aasi, et al. v. Honda") == "aasi v. honda"

    def test_none_input(self) -> None:
        assert normalize_title(None) is None

    def test_trailing_punctuation(self) -> None:
        assert normalize_title("Smith v. Jones,") == "smith v. jones"


class TestTitlesMatch:
    """Tests for titles_match."""

    def test_exact_match(self) -> None:
        assert titles_match("Smith v. Jones", "Smith v. Jones")

    def test_case_insensitive(self) -> None:
        assert titles_match("SMITH V. JONES", "Smith v. Jones")

    def test_vs_variants(self) -> None:
        assert titles_match("Smith vs. Jones", "Smith v. Jones")

    def test_et_al_variants(self) -> None:
        assert titles_match("Aasi, et al. v. Honda", "Aasi v. Honda")

    def test_substring_match(self) -> None:
        assert titles_match(
            "Aasi v. American Honda Motor Co., Inc.",
            "Aasi v. American Honda Motor Co.",
        )

    def test_none_values(self) -> None:
        assert titles_match(None, None)
        assert not titles_match("Smith v. Jones", None)
        assert not titles_match(None, "Smith v. Jones")


class TestNormalizeOutcome:
    """Tests for normalize_outcome."""

    def test_basic(self) -> None:
        assert normalize_outcome("granted") == "granted"
        assert normalize_outcome("DENIED") == "denied"

    def test_underscore_normalization(self) -> None:
        assert normalize_outcome("granted_in_part") == "granted in part"
        assert normalize_outcome("off_calendar") == "off calendar"

    def test_none(self) -> None:
        assert normalize_outcome(None) is None


# ---------------------------------------------------------------------------
# Party contamination tests
# ---------------------------------------------------------------------------


class TestIsPartyContaminated:
    """Tests for is_party_contaminated."""

    def test_clean_name(self) -> None:
        assert not is_party_contaminated("John Smith")
        assert not is_party_contaminated("American Honda Motor Co., Inc.")

    def test_department_header(self) -> None:
        assert is_party_contaminated(
            "Department 50 Law And Motion Rulings Case Number: 20Stcv41848"
        )

    def test_case_number_in_name(self) -> None:
        assert is_party_contaminated("Case Number: 22STCV35574 Hearing Date: March 5, 2026")

    def test_superior_court(self) -> None:
        assert is_party_contaminated("Superior Court of California")

    def test_county_name(self) -> None:
        assert is_party_contaminated("County of Los Angeles")


# ---------------------------------------------------------------------------
# Scoring tests
# ---------------------------------------------------------------------------


class TestScoreFixture:
    """Tests for score_fixture."""

    def _make_result(
        self,
        cases: list[CaseResult] | None = None,
        expected_cases: list[dict] | None = None,
    ) -> FixtureResult:
        """Helper to create a FixtureResult for testing."""
        if cases is None:
            cases = []
        if expected_cases is None:
            expected_cases = []
        return FixtureResult(
            fixture_name="test.html",
            model="test-model",
            expected={"cases": expected_cases},
            expected_case_count=len(expected_cases),
            actual_case_count=len(cases),
            cases=cases,
        )

    def test_perfect_match(self) -> None:
        result = self._make_result(
            cases=[
                CaseResult(
                    case_number="24NNCV02551",
                    case_title="Aasi v. Honda",
                    outcome="granted",
                    parties=[
                        {"name": "Sumayya Aasi", "role": "plaintiff"},
                        {"name": "American Honda Motor Co.", "role": "defendant"},
                    ],
                ),
            ],
            expected_cases=[
                {
                    "case_number": "24NNCV02551",
                    "case_title": "Aasi v. Honda",
                    "outcome": "granted",
                },
            ],
        )
        scores = score_fixture(result)
        assert scores["count_exact"]
        assert scores["title_matches"] == 1
        assert scores["outcome_matches"] == 1
        assert scores["party_contaminated"] == 0

    def test_missing_case(self) -> None:
        result = self._make_result(
            cases=[],
            expected_cases=[
                {
                    "case_number": "24NNCV02551",
                    "case_title": "Aasi v. Honda",
                    "outcome": "granted",
                },
            ],
        )
        scores = score_fixture(result)
        assert not scores["count_exact"]
        assert scores["title_matches"] == 0

    def test_contaminated_party(self) -> None:
        result = self._make_result(
            cases=[
                CaseResult(
                    case_number="22STCV35574",
                    case_title="Dept 50 v. Someone",
                    outcome="granted",
                    parties=[
                        {
                            "name": "Department 50 Law And Motion Rulings Case Number: 22STCV35574",
                            "role": "plaintiff",
                        },
                    ],
                ),
            ],
            expected_cases=[
                {
                    "case_number": "22STCV35574",
                    "case_title": "Smith v. Someone",
                    "outcome": "granted",
                },
            ],
        )
        scores = score_fixture(result)
        assert scores["party_contaminated"] == 1
        assert scores["party_clean"] == 0

    def test_null_title_extraction(self) -> None:
        """LLM extracts title where regex would return null."""
        result = self._make_result(
            cases=[
                CaseResult(
                    case_number="22STCV35574",
                    case_title="Smith v. Jones",
                    outcome="granted",
                    parties=[],
                ),
            ],
            expected_cases=[
                {
                    "case_number": "22STCV35574",
                    "case_title": "Smith v. Jones",
                    "outcome": "granted",
                },
            ],
        )
        scores = score_fixture(result)
        assert scores["title_extracted"] == 1
        assert scores["title_matches"] == 1


# ---------------------------------------------------------------------------
# LA prompt tests
# ---------------------------------------------------------------------------


class TestLAPrompt:
    """Tests for the LA extraction prompt content."""

    def test_prompt_mentions_la_formats(self) -> None:
        from eval_la_extraction import LA_EXTRACTION_PROMPT

        assert "Los Angeles" in LA_EXTRACTION_PROMPT
        assert "HTML" in LA_EXTRACTION_PROMPT
        assert "Case Number:" in LA_EXTRACTION_PROMPT

    def test_prompt_includes_outcome_taxonomy(self) -> None:
        from eval_la_extraction import LA_EXTRACTION_PROMPT

        assert "granted" in LA_EXTRACTION_PROMPT
        assert "denied" in LA_EXTRACTION_PROMPT
        assert "off_calendar" in LA_EXTRACTION_PROMPT

    def test_prompt_warns_against_contamination(self) -> None:
        from eval_la_extraction import LA_EXTRACTION_PROMPT

        # The prompt should specifically warn against including
        # department headers in party names
        assert "NEVER include department headers" in LA_EXTRACTION_PROMPT
