"""Tests for enrichment eval fixture integrity and coverage.

Validates that the hand-verified enrichment eval fixture set meets
quality and coverage requirements:
- All fixtures have valid JSON with required fields
- Coverage spans all 9 active counties with 10+ fixtures each
- Motion type diversity includes required types
- Edge cases are documented
- Source documents are referenced
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "enrichment"

REQUIRED_COUNTIES = [
    "los_angeles",
    "orange",
    "riverside",
    "san_bernardino",
    "santa_clara",
    "ventura",
    "contra_costa",
    "fresno",
    "san_francisco",
]

REQUIRED_FIXTURE_FIELDS = {"ruling_text", "expected", "source_doc", "notes"}
REQUIRED_EXPECTED_FIELDS = {"case_title", "motion_type", "outcome", "parties"}
REQUIRED_PARTIES_FIELDS = {"plaintiffs", "defendants"}

VALID_OUTCOMES = {
    "granted",
    "denied",
    "granted_in_part",
    "denied_in_part",
    "moot",
    "continued",
    "off_calendar",
    "submitted",
    "other",
    None,
}

# Motion types that must have at least 2 fixtures each
REQUIRED_MOTION_TYPES = {"msj", "demurrer", "motion_to_compel", "motion_to_strike"}

MIN_FIXTURES_PER_COUNTY = 10
MIN_TOTAL_FIXTURES = 100


def _load_all_fixtures() -> list[tuple[str, str, dict]]:
    """Load all fixture files and return (county, filename, data) tuples."""
    fixtures: list[tuple[str, str, dict]] = []
    for county_dir in sorted(FIXTURES_DIR.iterdir()):
        if not county_dir.is_dir():
            continue
        county = county_dir.name
        for fixture_path in sorted(county_dir.glob("*.json")):
            with open(fixture_path) as f:
                data = json.load(f)
            fixtures.append((county, fixture_path.name, data))
    return fixtures


ALL_FIXTURES = _load_all_fixtures()


class TestFixtureDirectoryStructure:
    """Verify directory structure exists for all counties."""

    def test_enrichment_dir_exists(self) -> None:
        assert FIXTURES_DIR.is_dir(), f"Missing {FIXTURES_DIR}"

    @pytest.mark.parametrize("county", REQUIRED_COUNTIES)
    def test_county_dir_exists(self, county: str) -> None:
        county_dir = FIXTURES_DIR / county
        assert county_dir.is_dir(), f"Missing county dir: {county}"

    @pytest.mark.parametrize("county", REQUIRED_COUNTIES)
    def test_county_has_min_fixtures(self, county: str) -> None:
        county_dir = FIXTURES_DIR / county
        fixture_files = list(county_dir.glob("*.json"))
        assert len(fixture_files) >= MIN_FIXTURES_PER_COUNTY, (
            f"{county} has {len(fixture_files)} fixtures, need >= {MIN_FIXTURES_PER_COUNTY}"
        )


class TestFixtureSchema:
    """Verify each fixture has valid schema."""

    @pytest.mark.parametrize(
        ("county", "filename", "data"),
        ALL_FIXTURES,
        ids=[f"{c}/{f}" for c, f, _ in ALL_FIXTURES],
    )
    def test_has_required_fields(self, county: str, filename: str, data: dict) -> None:
        missing = REQUIRED_FIXTURE_FIELDS - set(data.keys())
        assert not missing, f"Missing fields in {county}/{filename}: {missing}"

    @pytest.mark.parametrize(
        ("county", "filename", "data"),
        ALL_FIXTURES,
        ids=[f"{c}/{f}" for c, f, _ in ALL_FIXTURES],
    )
    def test_expected_has_required_fields(self, county: str, filename: str, data: dict) -> None:
        expected = data.get("expected", {})
        missing = REQUIRED_EXPECTED_FIELDS - set(expected.keys())
        assert not missing, f"Missing expected fields in {county}/{filename}: {missing}"

    @pytest.mark.parametrize(
        ("county", "filename", "data"),
        ALL_FIXTURES,
        ids=[f"{c}/{f}" for c, f, _ in ALL_FIXTURES],
    )
    def test_parties_has_required_fields(self, county: str, filename: str, data: dict) -> None:
        parties = data.get("expected", {}).get("parties", {})
        missing = REQUIRED_PARTIES_FIELDS - set(parties.keys())
        assert not missing, f"Missing parties fields in {county}/{filename}: {missing}"

    @pytest.mark.parametrize(
        ("county", "filename", "data"),
        ALL_FIXTURES,
        ids=[f"{c}/{f}" for c, f, _ in ALL_FIXTURES],
    )
    def test_ruling_text_not_empty(self, county: str, filename: str, data: dict) -> None:
        ruling_text = data.get("ruling_text", "")
        assert len(ruling_text) > 50, (
            f"ruling_text too short in {county}/{filename}: {len(ruling_text)} chars"
        )

    @pytest.mark.parametrize(
        ("county", "filename", "data"),
        ALL_FIXTURES,
        ids=[f"{c}/{f}" for c, f, _ in ALL_FIXTURES],
    )
    def test_outcome_is_valid(self, county: str, filename: str, data: dict) -> None:
        outcome = data.get("expected", {}).get("outcome")
        assert outcome in VALID_OUTCOMES, f"Invalid outcome in {county}/{filename}: '{outcome}'"

    @pytest.mark.parametrize(
        ("county", "filename", "data"),
        ALL_FIXTURES,
        ids=[f"{c}/{f}" for c, f, _ in ALL_FIXTURES],
    )
    def test_source_doc_populated(self, county: str, filename: str, data: dict) -> None:
        source_doc = data.get("source_doc", "")
        assert source_doc, f"source_doc not populated in {county}/{filename}"


class TestFixtureCoverage:
    """Verify coverage requirements across all fixtures."""

    def test_total_fixture_count(self) -> None:
        assert len(ALL_FIXTURES) >= MIN_TOTAL_FIXTURES, (
            f"Only {len(ALL_FIXTURES)} total fixtures, need >= {MIN_TOTAL_FIXTURES}"
        )

    def test_motion_type_diversity(self) -> None:
        motion_counts: dict[str, int] = {}
        for _, _, data in ALL_FIXTURES:
            mt = data.get("expected", {}).get("motion_type")
            if mt:
                motion_counts[mt] = motion_counts.get(mt, 0) + 1

        for required_type in REQUIRED_MOTION_TYPES:
            count = motion_counts.get(required_type, 0)
            assert count >= 2, (
                f"Need >= 2 fixtures with motion_type='{required_type}', found {count}"
            )

    def test_edge_cases_documented(self) -> None:
        notes_count = sum(1 for _, _, data in ALL_FIXTURES if data.get("notes", "").strip())
        assert notes_count >= 2, (
            f"Need >= 2 fixtures with notes documenting ambiguity, found {notes_count}"
        )

    def test_all_source_docs_populated(self) -> None:
        missing = [
            f"{county}/{filename}"
            for county, filename, data in ALL_FIXTURES
            if not data.get("source_doc")
        ]
        assert not missing, f"Fixtures missing source_doc: {missing}"
