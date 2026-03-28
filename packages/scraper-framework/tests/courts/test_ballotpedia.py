"""Tests for Ballotpedia judge biographical data scraper.

Fixtures:
    ballotpedia_judge.html — representative Ballotpedia page for a CA trial
        court judge with Education, Career, Elections, and Biography sections.
    ballotpedia_judge_minimal.html — minimal page with only Education section.
    ballotpedia_not_found.html — search results page for a non-existent article.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.ballotpedia import (
    BASE_URL,
    BallotpediaBioEntry,
    CareerEntry,
    EducationEntry,
    ElectionEntry,
    _parse_single_career_entry,
    _parse_single_education_entry,
    build_candidate_urls,
    fetch_judge_bio,
    is_article_not_found,
    parse_ballotpedia_page,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"

FIXED_TIME = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# build_candidate_urls — unit tests
# ---------------------------------------------------------------------------


class TestBuildCandidateUrls:
    def test_with_middle_initial(self) -> None:
        urls = build_candidate_urls("Maria", "Rodriguez", "T")
        assert urls == [
            f"{BASE_URL}/Maria_T._Rodriguez",
            f"{BASE_URL}/Maria_Rodriguez",
        ]

    def test_without_middle_initial(self) -> None:
        urls = build_candidate_urls("John", "Smith")
        assert urls == [f"{BASE_URL}/John_Smith"]

    def test_middle_initial_with_period(self) -> None:
        """Period on middle initial is normalized — only one period in URL."""
        urls = build_candidate_urls("Maria", "Rodriguez", "T.")
        assert urls[0] == f"{BASE_URL}/Maria_T._Rodriguez"

    def test_whitespace_stripped(self) -> None:
        urls = build_candidate_urls(" Maria ", " Rodriguez ", " T ")
        assert urls[0] == f"{BASE_URL}/Maria_T._Rodriguez"
        assert urls[1] == f"{BASE_URL}/Maria_Rodriguez"

    def test_hyphenated_last_name(self) -> None:
        urls = build_candidate_urls("Kerry", "Duffy-Lewis")
        assert urls == [f"{BASE_URL}/Kerry_Duffy-Lewis"]

    def test_empty_middle_initial_treated_as_none(self) -> None:
        urls = build_candidate_urls("John", "Smith", "")
        # Empty string is falsy, so no middle initial URL
        assert urls == [f"{BASE_URL}/John_Smith"]

    def test_multi_word_first_name(self) -> None:
        """First names with spaces get underscores in URL."""
        urls = build_candidate_urls("Mary Ann", "Jones")
        assert urls == [f"{BASE_URL}/Mary_Ann_Jones"]

    def test_period_only_middle_initial(self) -> None:
        """A period-only middle initial should not produce a malformed URL."""
        urls = build_candidate_urls("John", "Smith", ".")
        # After stripping the period, mid is empty — should skip middle initial URL
        assert urls == [f"{BASE_URL}/John_Smith"]

    def test_whitespace_only_middle_initial(self) -> None:
        """Whitespace-only middle initial should not produce a malformed URL."""
        urls = build_candidate_urls("John", "Smith", "   ")
        assert urls == [f"{BASE_URL}/John_Smith"]


# ---------------------------------------------------------------------------
# _parse_single_education_entry — unit tests
# ---------------------------------------------------------------------------


class TestParseSingleEducationEntry:
    def test_full_entry(self) -> None:
        entry = _parse_single_education_entry("B.A., University of California, Berkeley, 2005")
        assert entry is not None
        assert entry.degree == "B.A."
        assert entry.institution == "University of California, Berkeley"
        assert entry.year == "2005"

    def test_jd_entry(self) -> None:
        entry = _parse_single_education_entry("J.D., Stanford Law School, 2008")
        assert entry is not None
        assert entry.degree == "J.D."
        assert entry.institution == "Stanford Law School"
        assert entry.year == "2008"

    def test_no_year(self) -> None:
        entry = _parse_single_education_entry("B.A., UCLA")
        assert entry is not None
        assert entry.degree == "B.A."
        assert entry.institution == "UCLA"
        assert entry.year is None

    def test_no_degree(self) -> None:
        entry = _parse_single_education_entry("University of Southern California, 2001")
        assert entry is not None
        assert entry.degree is None
        assert entry.institution == "University of Southern California"
        assert entry.year == "2001"

    def test_empty_string_returns_none(self) -> None:
        assert _parse_single_education_entry("") is None
        assert _parse_single_education_entry("   ") is None

    def test_bs_degree(self) -> None:
        entry = _parse_single_education_entry("B.S., University of Southern California, 2001")
        assert entry is not None
        assert entry.degree == "B.S."
        assert entry.institution == "University of Southern California"
        assert entry.year == "2001"


# ---------------------------------------------------------------------------
# _parse_single_career_entry — unit tests
# ---------------------------------------------------------------------------


class TestParseSingleCareerEntry:
    def test_full_entry(self) -> None:
        entry = _parse_single_career_entry("Associate, Morrison & Foerster LLP, 2008-2012")
        assert entry is not None
        assert entry.position == "Associate"
        assert entry.employer == "Morrison & Foerster LLP"
        assert entry.years == "2008-2012"

    def test_no_years(self) -> None:
        entry = _parse_single_career_entry("Associate, Morrison & Foerster LLP")
        assert entry is not None
        assert entry.position == "Associate"
        assert entry.employer == "Morrison & Foerster LLP"
        assert entry.years is None

    def test_no_employer(self) -> None:
        entry = _parse_single_career_entry("Private practice")
        assert entry is not None
        assert entry.position == "Private practice"
        assert entry.employer is None
        assert entry.years is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_single_career_entry("") is None

    def test_year_range_with_present(self) -> None:
        entry = _parse_single_career_entry("Senior Partner, Smith & Associates, 2018-present")
        assert entry is not None
        assert entry.years == "2018-present"

    def test_single_year(self) -> None:
        entry = _parse_single_career_entry("Clerk, Judge Anderson, 2007")
        assert entry is not None
        assert entry.years == "2007"


# ---------------------------------------------------------------------------
# parse_ballotpedia_page — fixture tests
# ---------------------------------------------------------------------------


class TestParseBallotpediaPage:
    def test_parses_full_page(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert result.source_url == f"{BASE_URL}/Maria_T._Rodriguez"
        assert result.fetched_at == FIXED_TIME

    def test_extracts_biography_text(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert result.biography_text is not None
        assert "Maria T. Rodriguez" in result.biography_text
        assert "Los Angeles County Superior Court" in result.biography_text

    def test_extracts_education(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert len(result.education) == 2

        ba = result.education[0]
        assert ba.degree == "B.A."
        assert "University of California, Berkeley" in ba.institution
        assert ba.year == "2005"

        jd = result.education[1]
        assert jd.degree == "J.D."
        assert "Stanford Law School" in jd.institution
        assert jd.year == "2008"

    def test_extracts_career(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert len(result.career) == 3
        assert result.career[0].position == "Associate"
        assert result.career[0].employer == "Morrison & Foerster LLP"
        assert result.career[0].years == "2008-2012"

    def test_extracts_elections(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        # Fixture has primary + general election tables under 2024 heading
        assert len(result.elections) == 2

        primary = result.elections[0]
        assert primary.year == "2024"
        assert primary.race is not None
        assert "Primary" in primary.race
        assert len(primary.candidates) == 3  # 3 candidates in primary

        general = result.elections[1]
        assert general.year == "2024"
        assert general.race is not None
        assert "General" in general.race
        assert len(general.candidates) == 2  # 2 candidates in general
        assert "Maria T. Rodriguez" in general.candidates[0]["name"]

    def test_minimal_page_education_only(self) -> None:
        html = _load("ballotpedia_judge_minimal.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/John_Smith", FIXED_TIME)

        assert len(result.education) == 2
        assert result.education[0].institution == "University of Southern California"
        assert result.education[1].institution == "UCLA School of Law"
        assert result.career == []
        assert result.elections == []

    def test_empty_html_returns_empty_entry(self) -> None:
        result = parse_ballotpedia_page(
            "<html><body></body></html>",
            f"{BASE_URL}/test",
            FIXED_TIME,
        )
        assert result.education == []
        assert result.career == []
        assert result.elections == []
        assert result.biography_text is None

    def test_raw_html_stored(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)
        assert result.raw_html == html


# ---------------------------------------------------------------------------
# is_article_not_found — unit tests
# ---------------------------------------------------------------------------


class TestIsArticleNotFound:
    def test_not_found_page(self) -> None:
        html = _load("ballotpedia_not_found.html")
        assert is_article_not_found(html) is True

    def test_normal_page(self) -> None:
        html = _load("ballotpedia_judge.html")
        assert is_article_not_found(html) is False

    def test_empty_html(self) -> None:
        assert is_article_not_found("<html><body></body></html>") is False


# ---------------------------------------------------------------------------
# BallotpediaBioEntry.to_bio_source_dict — unit tests
# ---------------------------------------------------------------------------


class TestBioSourceDict:
    def test_includes_required_fields(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/Maria_T._Rodriguez",
            fetched_at=FIXED_TIME,
            biography_text="Some bio text",
            education=[
                EducationEntry(institution="Stanford Law School", degree="J.D.", year="2008"),
            ],
            career=[
                CareerEntry(position="Associate", employer="Big Law LLP", years="2008-2012"),
            ],
            elections=[
                ElectionEntry(year="2024", race="Office 97", candidates=[]),
            ],
        )
        result = entry.to_bio_source_dict()

        assert result["source"] == "ballotpedia"
        assert result["url"] == f"{BASE_URL}/Maria_T._Rodriguez"
        assert result["fetched_at"] == FIXED_TIME.isoformat()
        assert isinstance(result["content"], dict)

    def test_content_structure(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/test",
            fetched_at=FIXED_TIME,
            education=[
                EducationEntry(institution="Harvard Law", degree="J.D.", year="2010"),
            ],
            career=[],
            elections=[],
        )
        content = entry.to_bio_source_dict()["content"]

        assert isinstance(content, dict)
        assert len(content["education"]) == 1
        assert content["education"][0]["institution"] == "Harvard Law"
        assert content["education"][0]["degree"] == "J.D."
        assert content["education"][0]["year"] == "2010"
        assert content["career"] == []
        assert content["elections"] == []

    def test_empty_entry(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/test",
            fetched_at=FIXED_TIME,
        )
        result = entry.to_bio_source_dict()
        assert result["content"]["education"] == []
        assert result["content"]["career"] == []
        assert result["content"]["elections"] == []
        assert result["content"]["biography_text"] is None


# ---------------------------------------------------------------------------
# fetch_judge_bio — mocked HTTP tests
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_bio_with_middle_initial() -> None:
    """fetch_judge_bio with middle initial tries that URL first."""
    html = _load("ballotpedia_judge.html")
    respx.get(f"{BASE_URL}/Maria_T._Rodriguez").mock(return_value=httpx.Response(200, text=html))

    result = fetch_judge_bio("Maria", "Rodriguez", "T", request_delay=0)
    assert result is not None
    assert result.source_url == f"{BASE_URL}/Maria_T._Rodriguez"
    assert len(result.education) == 2
    assert len(result.career) == 3
    assert result.raw_html is not None
    assert "Maria T. Rodriguez" in result.raw_html


@respx.mock
def test_fetch_bio_falls_back_to_no_middle_initial() -> None:
    """When the middle initial URL 404s, try without middle initial."""
    html = _load("ballotpedia_judge.html")
    respx.get(f"{BASE_URL}/Maria_T._Rodriguez").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}/Maria_Rodriguez").mock(return_value=httpx.Response(200, text=html))

    result = fetch_judge_bio("Maria", "Rodriguez", "T", request_delay=0)
    assert result is not None
    assert result.source_url == f"{BASE_URL}/Maria_Rodriguez"


@respx.mock
def test_fetch_bio_returns_none_for_nonexistent_judge() -> None:
    """fetch_judge_bio returns None when no page exists."""
    not_found_html = _load("ballotpedia_not_found.html")
    respx.get(f"{BASE_URL}/Zzzzz_Q._Fakename").mock(
        return_value=httpx.Response(200, text=not_found_html)
    )
    respx.get(f"{BASE_URL}/Zzzzz_Fakename").mock(
        return_value=httpx.Response(200, text=not_found_html)
    )

    result = fetch_judge_bio("Zzzzz", "Fakename", "Q", request_delay=0)
    assert result is None


@respx.mock
def test_fetch_bio_without_middle_initial() -> None:
    """fetch_judge_bio without middle initial only tries one URL."""
    html = _load("ballotpedia_judge_minimal.html")
    respx.get(f"{BASE_URL}/John_Smith").mock(return_value=httpx.Response(200, text=html))

    result = fetch_judge_bio("John", "Smith", request_delay=0)
    assert result is not None
    assert result.source_url == f"{BASE_URL}/John_Smith"


@respx.mock
def test_fetch_bio_handles_404_gracefully() -> None:
    """404 responses don't raise — just return None."""
    respx.get(f"{BASE_URL}/Nobody_Here").mock(return_value=httpx.Response(404))

    result = fetch_judge_bio("Nobody", "Here", request_delay=0)
    assert result is None


@respx.mock
def test_fetch_bio_handles_http_error() -> None:
    """HTTP errors (network failures) don't raise — just return None."""
    respx.get(f"{BASE_URL}/Network_Fail").mock(side_effect=httpx.ConnectError("Connection refused"))

    result = fetch_judge_bio("Network", "Fail", request_delay=0)
    assert result is None


@respx.mock
def test_fetch_bio_handles_server_error() -> None:
    """500 errors are handled gracefully (no exceptions)."""
    respx.get(f"{BASE_URL}/Server_Error").mock(return_value=httpx.Response(500))

    result = fetch_judge_bio("Server", "Error", request_delay=0)
    assert result is None
