"""Tests for Riverside County tentative rulings scraper.

Covers:
  - hearing_date extraction
  - boilerplate "No Tentative Rulings" filtering
  - courthouse mapping
  - case number regex
  - PDF-content judge name fallback (issue #411)
  - department-to-judge mapping fallback (#585)
  - scraper returns whole PDFs without splitting (#1728)

Fixtures captured from live site 2026-03-02:
  riv_page.html            — index page with 17 PDF links
  riv_ps1.pdf              — Dept PS1, Judge Arthur Hester III (4 pages, 4 rulings)
  riv_hall_of_justice.pdf   — Dept 260, no rulings placeholder (1 page)
  riv_murrieta.pdf         — Dept M205, Judge Belinda Handy, no rulings (1 page)
  riv_moreno_valley.pdf    — Dept MV1, Judge David E. Gregory (2 pages, 3 rulings)

Note: Multi-ruling PDF splitting was moved to the framework-level
``LlmExtractor`` in the ingestion worker using a Riverside-specific
system prompt configured in ``framework.extraction_config`` (#1728).
The scraper now passes whole PDFs through without splitting.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest
import respx

from courts.ca.pdf_link_scraper import PdfLinkScraper, _extract_pdf_text
from courts.ca.riverside_tentatives import (
    _CASE_NUMBER_RE,
    _LINK_TEXT_RE,
    _PLACEHOLDER_JUDGE_NAMES,
    INDEX_URL,
    RiversideTentativeRulingsScraper,
    SplitRuling,
    _is_no_tentative_rulings,
    _is_placeholder_judge,
    _riv_courthouse,
    _riv_hearing_date_from_text,
    _split_rulings,
)
from courts.ca.riverside_tentatives import default_config as riv_default_config
from framework import CapturedDocument, ContentFormat

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"

# Expected number of PDF links processed from riv_page.html after filtering.
# The fixture has 17 links; with the loosened regex (#2603), all 17 pass
# (Department 260 no longer filtered out).
_RIV_EXPECTED_PROCESSED_PDFS = 17


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _riv_hearing_date_from_text — unit tests
# ---------------------------------------------------------------------------


def test_riv_hearing_date_standard_format() -> None:
    text = "Tentative Rulings for March 2, 2026\nDepartment PS1\nSome ruling"
    assert _riv_hearing_date_from_text(text) == datetime(2026, 3, 2)


def test_riv_hearing_date_no_tentative_rulings() -> None:
    text = "No Tentative Rulings March 2, 2026\nDepartment M205"
    assert _riv_hearing_date_from_text(text) == datetime(2026, 3, 2)


def test_riv_hearing_date_no_comma() -> None:
    text = "Tentative Rulings for March 2 2026\nDepartment PS1"
    assert _riv_hearing_date_from_text(text) == datetime(2026, 3, 2)


def test_riv_hearing_date_returns_none_for_no_date() -> None:
    assert _riv_hearing_date_from_text("") is None
    assert _riv_hearing_date_from_text("No dates here") is None


def test_riv_hearing_date_returns_none_for_no_rulings_no_date() -> None:
    """Hall of Justice placeholder has no date at all."""
    text = "No Tentative Rulings for\nDepartment 260"
    assert _riv_hearing_date_from_text(text) is None


# ---------------------------------------------------------------------------
# _riv_hearing_date_from_text — against real PDF fixtures
# ---------------------------------------------------------------------------


def test_riv_hearing_date_ps1() -> None:
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    dt = _riv_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 2)


def test_riv_hearing_date_murrieta() -> None:
    text = _extract_pdf_text(_load_bytes("riv_murrieta.pdf"))
    dt = _riv_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 2)


def test_riv_hearing_date_moreno_valley() -> None:
    text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    dt = _riv_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 2)


def test_riv_hearing_date_hall_of_justice_no_date() -> None:
    """Stale 2023 placeholder PDF has no date — hearing_date should be None."""
    text = _extract_pdf_text(_load_bytes("riv_hall_of_justice.pdf"))
    dt = _riv_hearing_date_from_text(text)
    assert dt is None


# ---------------------------------------------------------------------------
# _riv_hearing_date_from_text — edge case: matched date but invalid format
# ---------------------------------------------------------------------------


def test_riv_hearing_date_returns_none_for_unparseable_date() -> None:
    """When the regex matches but the date string can't be parsed, return None."""
    text = "Tentative Rulings for February 30, 2026\nDepartment PS1"
    result = _riv_hearing_date_from_text(text)
    assert result is None


# ---------------------------------------------------------------------------
# _is_no_tentative_rulings — unit tests (#318)
# ---------------------------------------------------------------------------


def test_is_no_tentative_rulings_murrieta_text() -> None:
    """Murrieta boilerplate starts with 'No Tentative Rulings March 2, 2026'."""
    text = (
        "No Tentative Rulings March 2, 2026\n"
        "Department M205\n"
        "Riverside Superior Court provides official court reporters..."
    )
    assert _is_no_tentative_rulings(text) is True


def test_is_no_tentative_rulings_hall_of_justice_text() -> None:
    """Hall of Justice boilerplate starts with 'No Tentative Rulings for'."""
    text = (
        "No Tentative Rulings for\n"
        "Department 260\n"
        "Riverside Superior Court provides official court reporters..."
    )
    assert _is_no_tentative_rulings(text) is True


def test_is_no_tentative_rulings_murrieta_fixture() -> None:
    """Murrieta fixture PDF is detected as boilerplate."""
    text = _extract_pdf_text(_load_bytes("riv_murrieta.pdf"))
    assert _is_no_tentative_rulings(text) is True


def test_is_no_tentative_rulings_hall_of_justice_fixture() -> None:
    """Hall of Justice fixture PDF is detected as boilerplate."""
    text = _extract_pdf_text(_load_bytes("riv_hall_of_justice.pdf"))
    assert _is_no_tentative_rulings(text) is True


def test_is_no_tentative_rulings_false_for_real_rulings() -> None:
    """Real ruling PDFs (PS1, Moreno Valley) are NOT boilerplate."""
    ps1_text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    assert _is_no_tentative_rulings(ps1_text) is False

    mv_text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    assert _is_no_tentative_rulings(mv_text) is False


def test_is_no_tentative_rulings_false_for_empty() -> None:
    """Empty string is not boilerplate."""
    assert _is_no_tentative_rulings("") is False


def test_is_no_tentative_rulings_case_insensitive() -> None:
    """Detection is case-insensitive."""
    assert _is_no_tentative_rulings("NO TENTATIVE RULINGS March 2, 2026") is True
    assert _is_no_tentative_rulings("no tentative rulings for\nDepartment 1") is True


# ---------------------------------------------------------------------------
# _riv_courthouse — unit tests
# ---------------------------------------------------------------------------


def test_riv_courthouse_unknown_returns_none() -> None:
    """Unknown department prefix returns None."""
    assert _riv_courthouse("X99") is None
    assert _riv_courthouse("ZZZZ") is None


def test_riv_courthouse_known_prefixes() -> None:
    """Known department prefixes map to correct courthouses."""
    assert _riv_courthouse("PS1") == "Palm Springs Courthouse"
    assert _riv_courthouse("MV1") == "Moreno Valley Courthouse"
    assert _riv_courthouse("M205") == "Menifee Justice Center"
    assert _riv_courthouse("C1") == "Corona Courthouse"
    assert _riv_courthouse("05") == "Hall of Justice"


# ---------------------------------------------------------------------------
# Expanded case number regex — non-CV prefixes (#805)
# ---------------------------------------------------------------------------


class TestCaseNumberRegexExpanded:
    """Verify _CASE_NUMBER_RE matches both CV-prefixed and location-prefixed case numbers."""

    def test_cv_prefixed_case_numbers(self) -> None:
        """Existing CV-prefixed case numbers still match."""
        assert _CASE_NUMBER_RE.match("CVPS2306157")
        assert _CASE_NUMBER_RE.match("CVRI2412345")
        assert _CASE_NUMBER_RE.match("CVMV2507098")

    def test_ric_prefix(self) -> None:
        """RIC (Riverside - Hall of Justice) prefix matches."""
        assert _CASE_NUMBER_RE.match("RIC1904113")

    def test_mcc_prefix(self) -> None:
        """MCC (Murrieta) prefix matches."""
        assert _CASE_NUMBER_RE.match("MCC2012345")

    def test_psc_prefix(self) -> None:
        """PSC (Palm Springs) prefix matches."""
        assert _CASE_NUMBER_RE.match("PSC2101234")

    def test_swc_prefix(self) -> None:
        """SWC (Southwest) prefix matches."""
        assert _CASE_NUMBER_RE.match("SWC2200001")

    def test_inc_prefix(self) -> None:
        """INC (Indio) prefix matches."""
        assert _CASE_NUMBER_RE.match("INC2300001")

    def test_civ_prefix(self) -> None:
        """CIV (civil) prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("CIV208568")

    def test_mvc_prefix(self) -> None:
        """MVC (motor vehicle collision) prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("MVC1904273")

    def test_tec_prefix(self) -> None:
        """TEC (Temecula) prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("TEC1102057")

    def test_udps_prefix(self) -> None:
        """UDPS (unlawful detainer Palm Springs) prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("UDPS2500726")

    def test_mixed_case_civ(self) -> None:
        """Mixed-case CIV prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("Civ208568")

    def test_mixed_case_mvc(self) -> None:
        """Mixed-case MVC prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("Mvc1904273")

    def test_mixed_case_tec(self) -> None:
        """Mixed-case TEC prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("Tec1102057")

    def test_mixed_case_udps(self) -> None:
        """Mixed-case UDPS prefix matches (#2192)."""
        assert _CASE_NUMBER_RE.match("Udps2500726")

    def test_mixed_case_ric(self) -> None:
        """Mixed-case existing prefix matches (case-insensitive)."""
        assert _CASE_NUMBER_RE.match("ric1904113")

    def test_mixed_case_cvps(self) -> None:
        """Mixed-case CV-prefixed matches (case-insensitive)."""
        assert _CASE_NUMBER_RE.match("Cvps2306157")

    def test_no_match_random_prefix(self) -> None:
        """Unknown prefixes do not match."""
        assert _CASE_NUMBER_RE.match("XYZ1234567") is None
        assert _CASE_NUMBER_RE.match("ABC1234567") is None

    def test_no_match_too_few_digits(self) -> None:
        """Case numbers with fewer than 6 digits do not match."""
        assert _CASE_NUMBER_RE.match("RIC12345") is None

    def test_word_boundary(self) -> None:
        """Regex uses word boundaries to avoid partial matches."""
        text = "sometext RIC1904113 moretext"
        m = _CASE_NUMBER_RE.search(text)
        assert m is not None
        assert m.group(0) == "RIC1904113"


# ---------------------------------------------------------------------------
# _LINK_TEXT_RE — unit tests (#2603)
# ---------------------------------------------------------------------------


class TestLinkTextRe:
    """Verify _LINK_TEXT_RE matches all three real-world link-text shapes."""

    def test_standard_with_honorable(self) -> None:
        """Standard form: 'Department 04 - Honorable Daniel A. Ottolia'."""
        m = _LINK_TEXT_RE.search("Department 04 - Honorable Daniel A. Ottolia")
        assert m is not None
        assert m.group("department") == "04"
        assert m.group("judge_name") == "Daniel A. Ottolia"

    def test_without_honorable_prefix(self) -> None:
        """Non-standard judge label without 'Honorable': 'Department 01 - Assigned Judge'."""
        m = _LINK_TEXT_RE.search("Department 01 - Assigned Judge")
        assert m is not None
        assert m.group("department") == "01"
        assert m.group("judge_name") == "Assigned Judge"

    def test_no_dash_no_judge(self) -> None:
        """No judge suffix at all: 'Department 260'."""
        m = _LINK_TEXT_RE.search("Department 260")
        assert m is not None
        assert m.group("department") == "260"
        assert m.group("judge_name") is None

    def test_ps1_existing_passes(self) -> None:
        """Existing PS1 format still matches after regex loosening."""
        m = _LINK_TEXT_RE.search("Department PS1 - Honorable Arthur Hester III")
        assert m is not None
        assert m.group("department") == "PS1"
        assert m.group("judge_name") == "Arthur Hester III"


# ---------------------------------------------------------------------------
# _is_placeholder_judge — unit tests (#3785)
# ---------------------------------------------------------------------------


def test_placeholder_judge_names_constant() -> None:
    """_PLACEHOLDER_JUDGE_NAMES contains the expected placeholder strings."""
    assert "assigned judge" in _PLACEHOLDER_JUDGE_NAMES


def test_is_placeholder_judge_none_returns_false() -> None:
    """None is not a placeholder."""
    assert _is_placeholder_judge(None) is False


def test_is_placeholder_judge_empty_returns_false() -> None:
    """Empty string is not a placeholder."""
    assert _is_placeholder_judge("") is False


@pytest.mark.parametrize(
    "variant",
    ["Assigned Judge", "assigned judge", "ASSIGNED JUDGE", "  Assigned Judge  "],
)
def test_assigned_judge_placeholder_rejected(variant: str) -> None:
    """All case/whitespace variants of 'Assigned Judge' are detected as placeholders.

    Tests the full fetch_documents pipeline: a single-link synthetic HTML page
    with the given variant produces a document with judge_name=None (#3785).
    """
    html = (
        "<html><body>"
        f'<a href="/system/files/2026-02/01ruling030226.pdf">'
        f"Department 01 - {variant}</a>"
        "</body></html>"
    )
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    with respx.mock:
        respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
        respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

        config = riv_default_config()
        config.request_delay_seconds = 0
        scraper = RiversideTentativeRulingsScraper(config=config)

        # Patch extract_judge_name to return None so only the link-text path fires
        with patch(
            "courts.ca.riverside_tentatives.extract_judge_name",
            return_value=None,
        ):
            docs = scraper.fetch_documents()

    assert len(docs) == 1
    assert docs[0].judge_name is None, (
        f"Expected judge_name=None for placeholder variant {variant!r}, got {docs[0].judge_name!r}"
    )


@respx.mock
def test_assigned_judge_placeholder_falls_back_to_dept_map() -> None:
    """When 'Assigned Judge' placeholder is reset, the dept_judge_map fallback fires (#3785).

    A scraper constructed with dept_judge_map={"1": "Honorable Real Name"} should
    populate judge_name from the map after the placeholder is cleared.
    Department "01" normalizes to "1" via normalize_department().
    """
    html = (
        "<html><body>"
        '<a href="/system/files/2026-02/01ruling030226.pdf">'
        "Department 01 - Assigned Judge</a>"
        "</body></html>"
    )
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = riv_default_config()
    config.request_delay_seconds = 0
    # Department "01" normalizes to "1", so the key in the map must be "1"
    scraper = RiversideTentativeRulingsScraper(
        config=config,
        dept_judge_map={"1": "Honorable Real Name"},
    )

    # Patch extract_judge_name to return None so the dept-map is the only fallback
    with patch(
        "courts.ca.riverside_tentatives.extract_judge_name",
        return_value=None,
    ):
        docs = scraper.fetch_documents()

    assert len(docs) == 1
    assert docs[0].judge_name == "Honorable Real Name", (
        f"Expected judge_name='Honorable Real Name' from dept map fallback, "
        f"got {docs[0].judge_name!r}"
    )


# ---------------------------------------------------------------------------
# Regression test — all three link-text shapes flow through fetch_documents
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_all_link_text_shapes_processed() -> None:
    """Regression: all three link-text shapes produce documents (#2603).

    Verifies:
    - Standard form ('Department 04 - Honorable Daniel A. Ottolia') → judge populated
    - No-Honorable form ('Department 01 - Assigned Judge') → judge populated
    - No-dash form ('Department 260') → department='260', judge_name=None
    """
    html = (
        "<html><body>"
        '<a href="/system/files/2026-02/04ruling.pdf">'
        "Department 04 - Honorable Daniel A. Ottolia</a>"
        '<a href="/system/files/2026-02/01ruling.pdf">'
        "Department 01 - Assigned Judge</a>"
        '<a href="/system/files/2023-10/260ruling.pdf">'
        "Department 260</a>"
        "</body></html>"
    )
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()

    # All three links should produce documents (no filtering)
    assert len(docs) == 3

    dept_map = {d.department: d for d in docs}

    # Standard form — judge name populated from link text
    assert "04" in dept_map
    assert dept_map["04"].judge_name == "Daniel A. Ottolia"
    assert dept_map["04"].courthouse == "Hall of Justice"

    # No-Honorable form — placeholder "Assigned Judge" is reset to None (#3785).
    # The PDF fallback uses riv_ps1.pdf (Dept PS1, not 01), so it won't match
    # dept 01; no dept_judge_map passed either.  Final value must be None.
    assert "01" in dept_map
    assert dept_map["01"].judge_name is None

    # No-dash form — department set, judge_name is None (fallback may fill later)
    assert "260" in dept_map
    assert dept_map["260"].department == "260"
    assert dept_map["260"].judge_name is None or dept_map["260"].judge_name == ""


# ---------------------------------------------------------------------------
# Full scraper run — hearing_date populated
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_run_populates_hearing_date() -> None:
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # All docs use the ps1 fixture, so all should have a hearing date
    has_date = [d for d in parsed if d.hearing_date]
    assert len(has_date) == len(parsed)
    assert has_date[0].hearing_date == datetime(2026, 3, 2)


@respx.mock
def test_riv_run_no_date_boilerplate_pdf_skipped() -> None:
    """Hall of Justice 'No Tentative Rulings' PDFs are now skipped (#318)."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_hall_of_justice.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # All docs are boilerplate — none should be returned
    assert len(docs) == 0


# ---------------------------------------------------------------------------
# Scraper returns whole PDFs — no splitting (#1728)
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_run_returns_one_doc_per_pdf() -> None:
    """The scraper returns one CapturedDocument per PDF, no splitting (#1728).

    Multi-ruling PDF splitting is handled downstream by the ingestion worker
    using the framework ``LlmExtractor`` with a Riverside-specific system
    prompt configured in ``framework.extraction_config``.
    """
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")  # 4 rulings in the PDF

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # One doc per PDF link — no splitting
    # (all 17 links pass with loosened link_text_re, #2603)
    assert len(docs) == _RIV_EXPECTED_PROCESSED_PDFS

    # Documents should NOT have pre_split flag (scraper does not split)
    for doc in docs:
        assert not doc.extra.get("pre_split")


@respx.mock
def test_riv_run_docs_have_raw_pdf_content() -> None:
    """Returned documents contain raw PDF bytes, not extracted text (#1728)."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) > 0

    # Each doc should have the raw PDF bytes
    for doc in docs:
        assert doc.raw_content == pdf_bytes
        assert doc.content_format == ContentFormat.PDF


@respx.mock
def test_riv_run_docs_inherit_judge_and_department() -> None:
    """Documents inherit judge name and department from link text (#1728)."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Check PS1 department docs
    ps1_docs = [d for d in parsed if d.department == "PS1"]
    assert len(ps1_docs) > 0
    for doc in ps1_docs:
        assert doc.judge_name == "Arthur Hester III"
        assert doc.department == "PS1"
        assert doc.courthouse == "Palm Springs Courthouse"


@respx.mock
def test_riv_run_docs_have_hearing_date() -> None:
    """Parsed documents get the hearing date from the PDF header (#1728)."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # All docs should have the hearing date from the PS1 header
    assert all(d.hearing_date == datetime(2026, 3, 2) for d in parsed)


# ---------------------------------------------------------------------------
# Boilerplate PDFs — skipped by the scraper
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_run_skips_no_tentative_rulings_pdfs() -> None:
    """'No Tentative Rulings' boilerplate PDFs are skipped entirely (#318)."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_hall_of_justice.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) == 0


@respx.mock
def test_riv_run_skips_murrieta_no_tentative_rulings() -> None:
    """Murrieta 'No Tentative Rulings' PDFs are skipped entirely (#318)."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_murrieta.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) == 0


# ---------------------------------------------------------------------------
# PDF-content judge name fallback (#411)
# ---------------------------------------------------------------------------


def _html_with_single_matching_link(
    link_text: str = "Department PS1 - Honorable Arthur Hester III",
) -> str:
    """Return a minimal HTML page with a single matching PDF link.

    Used by judge-fallback tests that need precise control over link text
    without depending on the full riv_page.html fixture (#1845).
    """
    return (
        "<html><body>"
        f'<a href="/system/files/2026-02/PS1ruling030226.pdf">{link_text}</a>'
        "</body></html>"
    )


@respx.mock
def test_riv_pdf_judge_fallback_when_link_text_has_no_name() -> None:
    """When link text lacks judge name, extract_judge_name is called on PDF text (#411).

    Uses a single-link synthetic HTML page to isolate the fallback test from
    the link_text_re filter (#1845). The link text matches the filter pattern
    but we mock _fetch_one_pdf to return a doc with judge_name=None to
    exercise the PDF-text fallback path.
    """
    html = _html_with_single_matching_link("Department PS1 - Honorable Arthur Hester III")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    # Patch _fetch_one_pdf to clear the judge name (simulate missing link-text judge)
    original_fetch = PdfLinkScraper._fetch_one_pdf

    def _fetch_no_judge(self: PdfLinkScraper, client: Any, href: str, link_text: str) -> Any:
        doc = original_fetch(self, client, href, link_text)
        doc.judge_name = None
        return doc

    # Mock extract_judge_name to return a judge name from the PDF text
    with (
        patch.object(PdfLinkScraper, "_fetch_one_pdf", _fetch_no_judge),
        patch(
            "courts.ca.riverside_tentatives.extract_judge_name",
            return_value="Arthur Hester III",
        ) as mock_extract,
    ):
        docs = scraper.fetch_documents()
        # extract_judge_name should have been called
        assert mock_extract.call_count > 0

    # All docs should have the fallback judge name
    assert len(docs) > 0
    for doc in docs:
        assert doc.judge_name == "Arthur Hester III"


@respx.mock
def test_riv_pdf_judge_fallback_preserves_link_text_judge_name() -> None:
    """When link text has a judge name, it is preserved (PDF fallback not used) (#411)."""
    html = _load_html("riv_page.html")  # unmodified — has judge names
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    with patch(
        "courts.ca.riverside_tentatives.extract_judge_name",
        return_value="SHOULD NOT APPEAR",
    ):
        docs = scraper.fetch_documents()

    # Judge names from link text are preserved — the fallback value is NOT used
    ps1_docs = [d for d in docs if d.department == "PS1"]
    assert len(ps1_docs) > 0
    for doc in ps1_docs:
        assert doc.judge_name == "Arthur Hester III"


@respx.mock
def test_riv_pdf_judge_fallback_none_when_no_judge_in_pdf() -> None:
    """When neither link text nor PDF content has a judge name, judge_name stays None (#411).

    Uses a single-link synthetic HTML page to isolate the test from the
    link_text_re filter (#1845). Mocks _fetch_one_pdf to return a doc with
    judge_name=None, then verifies the fallback also returns None.
    """
    html = _html_with_single_matching_link("Department PS1 - Honorable Arthur Hester III")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    # Patch _fetch_one_pdf to clear the judge name
    original_fetch = PdfLinkScraper._fetch_one_pdf

    def _fetch_no_judge(self: PdfLinkScraper, client: Any, href: str, link_text: str) -> Any:
        doc = original_fetch(self, client, href, link_text)
        doc.judge_name = None
        return doc

    # extract_judge_name returns None (no judge found in PDF text)
    with (
        patch.object(PdfLinkScraper, "_fetch_one_pdf", _fetch_no_judge),
        patch(
            "courts.ca.riverside_tentatives.extract_judge_name",
            return_value=None,
        ),
    ):
        docs = scraper.fetch_documents()

    assert len(docs) > 0
    for doc in docs:
        assert doc.judge_name is None


@respx.mock
def test_riv_parse_document_judge_fallback() -> None:
    """parse_document falls back to extract_judge_name for PDFs without judge (#411)."""
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    # Dummy mock for the index page (not used by parse_document, but needed for respx)
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    # Use _make_base_doc to create a properly formed CapturedDocument
    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format=ContentFormat.PDF,
    )
    doc.judge_name = None
    doc.extra = {}

    with patch(
        "courts.ca.riverside_tentatives.extract_judge_name",
        return_value="Test Judge Name",
    ) as mock_extract:
        result = scraper.parse_document(doc)
        mock_extract.assert_called_once()
        assert result.judge_name == "Test Judge Name"


# ---------------------------------------------------------------------------
# fetch_documents — PDF text extraction failure
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_fetch_documents_pdf_extraction_failure() -> None:
    """When PDF text extraction fails, the original doc is kept as-is."""
    html = _load_html("riv_page.html")
    # Use invalid PDF content that will cause extraction to fail
    bad_pdf = b"This is not a valid PDF"

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=bad_pdf),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # Despite extraction failures, docs are still returned (one per PDF link)
    # (all 17 links pass with loosened link_text_re, #2603)
    assert len(docs) == _RIV_EXPECTED_PROCESSED_PDFS


# ---------------------------------------------------------------------------
# fetch_documents — department-to-judge mapping fallback (#585)
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_fetch_documents_dept_judge_map_fallback() -> None:
    """When both link text and PDF content lack a judge name, dept_judge_map is used."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    dept_map = {"PS1": "Mapped Judge Name"}
    scraper = RiversideTentativeRulingsScraper(
        config=config,
        dept_judge_map=dept_map,
    )

    # Patch _fetch_one_pdf to return docs with department set but no judge
    original_fetch = scraper._fetch_one_pdf.__func__

    def _fetch_no_judge(
        self: object,
        client: object,
        href: str,
        link_text: str,
    ) -> CapturedDocument:
        doc = original_fetch(self, client, href, link_text)
        doc.judge_name = None  # simulate missing judge from link text
        return doc

    with patch.object(type(scraper), "_fetch_one_pdf", _fetch_no_judge):
        with patch(
            "courts.ca.riverside_tentatives.extract_judge_name",
            return_value=None,
        ):
            docs = scraper.fetch_documents()

    ps1_docs = [d for d in docs if d.department == "PS1"]
    assert len(ps1_docs) > 0
    for doc in ps1_docs:
        assert doc.judge_name == "Mapped Judge Name"


# ---------------------------------------------------------------------------
# parse_document — hearing date extraction
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_parse_document_extracts_hearing_date() -> None:
    """parse_document extracts hearing date from ruling_text."""
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=b"dummy",
        content_format=ContentFormat.PDF,
    )
    doc.extra = {}
    doc.judge_name = "Test Judge"
    doc.hearing_date = None
    doc.ruling_text = "Tentative Rulings for March 2, 2026\nSome ruling text"

    result = scraper.parse_document(doc)
    assert result.hearing_date == datetime(2026, 3, 2)


# ---------------------------------------------------------------------------
# parse_document — dept-judge mapping fallback
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_parse_document_dept_judge_fallback() -> None:
    """parse_document uses dept_judge_map for docs without judge name."""
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    config = riv_default_config()
    config.request_delay_seconds = 0
    dept_map = {"PS1": "Mapped Single Judge"}
    scraper = RiversideTentativeRulingsScraper(
        config=config,
        dept_judge_map=dept_map,
    )

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format=ContentFormat.PDF,
    )
    doc.extra = {}
    doc.judge_name = None
    doc.department = "PS1"

    # Mock extract_judge_name to return None so dept map is tried
    with patch(
        "courts.ca.riverside_tentatives.extract_judge_name",
        return_value=None,
    ):
        result = scraper.parse_document(doc)

    assert result.judge_name == "Mapped Single Judge"


@respx.mock
def test_riv_parse_document_placeholder_judge_reset() -> None:
    """parse_document resets placeholder judge names to None (#3785)."""
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format=ContentFormat.PDF,
    )
    doc.extra = {}
    doc.judge_name = "Assigned Judge"

    with patch(
        "courts.ca.riverside_tentatives.extract_judge_name",
        return_value=None,
    ):
        result = scraper.parse_document(doc)

    assert result.judge_name is None


# ---------------------------------------------------------------------------
# Riverside system prompt — now in extraction_config (#1728)
# ---------------------------------------------------------------------------


def test_riverside_system_prompt_in_extraction_config() -> None:
    """The Riverside system prompt is in extraction_config, not the scraper (#1728, #2088)."""
    from framework.extraction_config import RIVERSIDE_SYSTEM_PROMPT

    # Prompt must mention the two-layer structure
    assert "two-layer" in RIVERSIDE_SYSTEM_PROMPT.lower()
    assert "detailed analysis" in RIVERSIDE_SYSTEM_PROMPT.lower()
    # Prompt must warn against truncation
    assert "truncat" in RIVERSIDE_SYSTEM_PROMPT.lower()
    # Prompt must mention that short ruling_text is wrong
    assert "200 characters" in RIVERSIDE_SYSTEM_PROMPT.lower()


def test_riverside_extraction_config_registered() -> None:
    """Riverside county has a registered extraction config (#1728)."""
    from framework.extraction_config import ExtractionMethod, get_county_extraction_config

    config = get_county_extraction_config("CA", "Riverside")
    assert config is not None
    assert config.method == ExtractionMethod.LLM
    assert config.provider == "google"
    assert config.model == "gemini-2.5-flash-lite"
    assert config.max_output_tokens == 32768
    assert config.system_prompt is not None
    assert len(config.system_prompt) > 100


def test_extraction_config_lookup_case_insensitive() -> None:
    """get_county_extraction_config is case-insensitive (#1728)."""
    from framework.extraction_config import get_county_extraction_config

    # All these should return the same config
    config_upper = get_county_extraction_config("CA", "RIVERSIDE")
    config_title = get_county_extraction_config("CA", "Riverside")
    config_lower = get_county_extraction_config("ca", "riverside")
    assert config_upper is not None
    assert config_upper == config_title
    assert config_upper == config_lower


def test_extraction_config_returns_none_for_unknown_county() -> None:
    """get_county_extraction_config returns None for unregistered counties (#1728)."""
    from framework.extraction_config import get_county_extraction_config

    assert get_county_extraction_config("CA", "Nonexistent") is None
    assert get_county_extraction_config("XX", "Riverside") is None


def test_extraction_method_enum_values() -> None:
    """ExtractionMethod enum has expected values (#1728)."""
    from framework.extraction_config import ExtractionMethod

    assert ExtractionMethod.LLM == "llm"
    assert ExtractionMethod.MULTIMODAL == "multimodal"
    assert ExtractionMethod.NONE == "none"


# ---------------------------------------------------------------------------
# default_config — basic sanity check
# ---------------------------------------------------------------------------


def test_riv_default_config() -> None:
    """default_config returns a valid ScraperConfig for Riverside."""
    config = riv_default_config()
    assert config.scraper_id == "ca-riverside-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Riverside"
    assert config.court == "Superior Court"
    assert len(config.schedule_windows) == 2


# ---------------------------------------------------------------------------
# AC#3: Multi-entry extraction drops "No tentative ruling" stub row (#3715)
# ---------------------------------------------------------------------------


def _make_anthropic_response(text: str) -> MagicMock:
    """Build a minimal mock Anthropic messages.create() response."""
    content_block = MagicMock()
    content_block.text = text

    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50

    response = MagicMock()
    response.content = [content_block]
    response.usage = usage
    return response


def test_extract_riverside_multi_entry_drops_no_tentative_row() -> None:
    """AC#3: A 3-entry PDF with one 'No tentative ruling.' stub produces N-1 rulings.

    Fixture: 3 numbered entries:
      1. CVRI2600001 — substantive ruling (MSJ, DENY, multi-paragraph)
      2. CVRI2600002 — bare "No tentative ruling." stub (< 200 chars)
      3. CVRI2600003 — off-calendar entry

    The post-processor must drop entry 2 (the stub), yielding 2 rulings, not 3.
    """
    from framework.llm_extractor import LlmExtractor
    from framework.prompts.riverside import RIVERSIDE_SYSTEM_PROMPT

    # The mock LLM returns all 3 entries — the sanitizer is responsible for
    # dropping the stub, so this tests the post-processing pipeline.
    llm_output = {
        "extracted_judge_name": "Arthur Hester III",
        "hearing_date": "2026-04-15",
        "department": "PS1",
        "rulings": [
            {
                "extracted_case_number": "CVRI2600001",
                "extracted_case_title": "Smith v. Jones",
                "case_type": "civil",
                "outcome": "denied",
                "motion_type": "summary_judgment",
                "ruling_text": (
                    "DENY Defendant's Motion for Summary Judgment.\n\n"
                    "The court has reviewed the moving papers, opposition, and reply. "
                    "Under CCP section 437c, the moving party bears the initial burden "
                    "of showing that one or more elements of the cause of action cannot "
                    "be established. Defendant has not met this burden. The motion is DENIED. "
                    "Trial is set for June 10, 2026."
                ),
                "extracted_parties": [],
                "confidence": {
                    "case_number": "high",
                    "case_title": "high",
                    "parties": "high",
                    "judge": "high",
                    "ruling_text": "high",
                    "outcome": "high",
                },
            },
            {
                "extracted_case_number": "CVRI2600002",
                "extracted_case_title": "Doe v. Roe",
                "case_type": "civil",
                "outcome": "other",
                "motion_type": None,
                "ruling_text": "No tentative ruling.",
                "extracted_parties": [],
                "confidence": {
                    "case_number": "high",
                    "case_title": "high",
                    "parties": "high",
                    "judge": "high",
                    "ruling_text": "high",
                    "outcome": "high",
                },
            },
            {
                "extracted_case_number": "CVRI2600003",
                "extracted_case_title": "Garcia v. Torres",
                "case_type": "civil",
                "outcome": "off_calendar",
                "motion_type": None,
                "ruling_text": "Off calendar.",
                "extracted_parties": [],
                "confidence": {
                    "case_number": "high",
                    "case_title": "high",
                    "parties": "high",
                    "judge": "high",
                    "ruling_text": "high",
                    "outcome": "high",
                },
            },
        ],
    }

    mock_response = _make_anthropic_response(json.dumps(llm_output))
    mock_client = MagicMock(spec=anthropic.Anthropic)
    mock_client.messages = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch.object(anthropic, "Anthropic", return_value=mock_client):
        extractor = LlmExtractor(api_key="test-key")
    extractor._client = mock_client
    extractor._cache = None  # Disable S3 cache for the test

    raw_text = (
        "Tentative Rulings for April 15, 2026\n"
        "Department PS1 - Honorable Arthur Hester III\n\n"
        "1.\nCVRI2600001\nSMITH VS JONES\n"
        "Hearing re: Motion for Summary Judgment\n"
        "Tentative Ruling: DENY.\n\n"
        "2.\nCVRI2600002\nDOE VS ROE\n"
        "Hearing re: Demurrer\n"
        "Tentative Ruling: No tentative ruling.\n\n"
        "3.\nCVRI2600003\nGARCIA VS TORRES\n"
        "Hearing re: Motion to Compel\n"
        "Tentative Ruling: Off calendar.\n"
    )

    rulings = extractor.extract(raw_text, system_prompt=RIVERSIDE_SYSTEM_PROMPT)

    # The stub (entry 2) must be dropped; entries 1 and 3 must be kept.
    assert len(rulings) == 2, (
        f"Expected 2 rulings after dropping the 'No tentative ruling' stub, "
        f"got {len(rulings)}: {[r.extracted_case_number for r in rulings]}"
    )

    case_numbers = {r.extracted_case_number for r in rulings}
    assert "CVRI2600001" in case_numbers, "Substantive ruling should be kept"
    assert "CVRI2600003" in case_numbers, "Off-calendar entry should be kept"
    assert "CVRI2600002" not in case_numbers, "Stub should be dropped"


# ---------------------------------------------------------------------------
# _split_rulings — Riverside multi-case PDF deterministic splitter (#3649)
# ---------------------------------------------------------------------------
#
# Regression coverage for the carry-forward bug documented in #3649.
# The Fresno splitter (#3534) eliminated this same class of bug for Fresno
# PDFs by splitting before LLM enrichment so each entry's enrichment runs
# against only its own text.  These tests pin the Riverside splitter to
# the same contract: each numbered entry is a separate SplitRuling, the
# preamble is dropped, and "No Tentative Rulings" placeholders return
# an empty list.


class TestRiversidePdfSplit:
    """Unit tests for _split_rulings against real Riverside fixture PDFs."""

    def test_riverside_pdf_split_ps1_fixture_returns_four_rulings(self) -> None:
        """riv_ps1.pdf has 4 numbered entries — splitter must return 4 rulings."""
        text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
        rulings = _split_rulings(text)
        assert len(rulings) == 4
        # Indices match the PDF's printed numbering 1..4.
        assert [r.ruling_index for r in rulings] == [1, 2, 3, 4]

    def test_riverside_pdf_split_ps1_case_numbers_match_pdf(self) -> None:
        """Each split must carry the case_number from its own entry header."""
        text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
        rulings = _split_rulings(text)
        case_numbers = [r.case_number for r in rulings]
        # Ground truth from the fixture PDF.
        assert case_numbers == [
            "CVPS2306157",
            "CVPS2306202",
            "CVPS2403119",
            "CVPS2404518",
        ]

    def test_riverside_pdf_split_ps1_entry_text_contains_only_own_body(self) -> None:
        """Entry N's ruling_text must contain only its own header + body — no
        cross-entry contamination from other entries' headers/bodies.

        This is the core invariant the splitter must guarantee: the LLM that
        runs against ``ruling_text`` should never see another entry's text,
        which is what enables it to violate the anti-carry-forward rule.
        """
        text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
        rulings = _split_rulings(text)
        # Entry 1 is YELDELL vs HENSS (CVPS2306157) — its ruling_text
        # must contain "YELDELL" and "HENSS" but must NOT contain
        # CRUMP/IRWIN (entry 2), GARCIA/FCA (entry 3), or NIETO (entry 4).
        r1 = rulings[0]
        assert "YELDELL" in r1.ruling_text
        assert "HENSS" in r1.ruling_text
        assert "CRUMP" not in r1.ruling_text
        assert "GARCIA vs FCA" not in r1.ruling_text
        assert "NIETO" not in r1.ruling_text
        # Entry 2 (CRUMP vs IRWIN) must not contain entry 1's case number.
        r2 = rulings[1]
        assert "CRUMP" in r2.ruling_text
        assert "IRWIN" in r2.ruling_text
        assert "CVPS2306157" not in r2.ruling_text

    def test_riverside_pdf_split_ps1_preamble_is_dropped(self) -> None:
        """Page-1 boilerplate (Zoom call-in, court reporter notice) must NOT
        appear in any per-entry ruling_text.  This guards against the
        ghost-ruling bug documented in the 2026-05-02 spotcheck comment
        on #3649 — the LLM was wrapping the procedural footer into a
        fake UNKNOWN-* ruling.
        """
        text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
        rulings = _split_rulings(text)
        for r in rulings:
            # The Zoom/oral-argument boilerplate from the page-1 preamble
            # should never appear inside a per-entry ruling_text.
            assert "Call-in Numbers" not in r.ruling_text, (
                f"Entry {r.ruling_index} contains preamble call-in text"
            )
            assert "Meeting Number:" not in r.ruling_text, (
                f"Entry {r.ruling_index} contains preamble meeting text"
            )

    def test_riverside_pdf_split_moreno_valley_fixture(self) -> None:
        """riv_moreno_valley.pdf has 3 numbered entries (CVMV2507098, CVMV2510261, CVMV2510403)."""
        text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
        rulings = _split_rulings(text)
        assert len(rulings) == 3
        case_numbers = [r.case_number for r in rulings]
        assert case_numbers == ["CVMV2507098", "CVMV2510261", "CVMV2510403"]

    def test_riverside_pdf_split_moreno_valley_outcome_isolation(self) -> None:
        """Each MV entry's ruling_text contains its own case_number only —
        regression test for the carry-forward shape from #3649: every
        ground-truth entry says 'Granted', so we assert each entry's text
        contains its own granted-disposition substring without leakage
        from others."""
        text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
        rulings = _split_rulings(text)
        all_case_numbers = {"CVMV2507098", "CVMV2510261", "CVMV2510403"}
        for r in rulings:
            others = all_case_numbers - {r.case_number}
            for other in others:
                assert other not in r.ruling_text, (
                    f"Entry {r.ruling_index} ({r.case_number}) contains other entry's "
                    f"case_number {other}"
                )

    def test_riverside_pdf_split_murrieta_no_tentative_rulings_returns_empty(
        self,
    ) -> None:
        """riv_murrieta.pdf is a 'No Tentative Rulings' boilerplate page —
        the splitter must return an empty list so the LLM path is bypassed
        entirely (no LLM call required for a placeholder page).
        """
        text = _extract_pdf_text(_load_bytes("riv_murrieta.pdf"))
        rulings = _split_rulings(text)
        assert rulings == []

    def test_riverside_pdf_split_hall_of_justice_no_rulings_returns_empty(
        self,
    ) -> None:
        """riv_hall_of_justice.pdf is also a 'No Tentative Rulings' placeholder."""
        text = _extract_pdf_text(_load_bytes("riv_hall_of_justice.pdf"))
        rulings = _split_rulings(text)
        assert rulings == []

    def test_riverside_pdf_split_empty_text_returns_empty(self) -> None:
        """Empty text returns an empty list (defensive)."""
        assert _split_rulings("") == []

    def test_riverside_pdf_split_no_numbered_entries_returns_empty(self) -> None:
        """Text without any numbered entries returns empty (defensive)."""
        text = "Some random text without any numbered entries"
        assert _split_rulings(text) == []

    def test_riverside_pdf_split_skips_entry_without_tentative_marker(
        self,
    ) -> None:
        """Entries with no 'Tentative Ruling:' marker are skipped — defends
        against the ghost-ruling shape from the 2026-05-02 spotcheck where
        the procedural footer was being wrapped as a fake ruling.
        """
        text = (
            "1.\n"
            "Some procedural footer text without a tentative ruling marker.\n"
            "Just rules and instructions on how to request oral argument.\n"
            "2.\n"
            "CVPS2400001 SMITH vs JONES\n"
            "Motion to Compel\n"
            "Tentative Ruling: Granted.\n"
            "The motion is granted.\n"
        )
        rulings = _split_rulings(text)
        # Only entry 2 has a tentative ruling marker — entry 1 must be skipped.
        assert len(rulings) == 1
        assert rulings[0].ruling_index == 2
        assert rulings[0].case_number == "CVPS2400001"

    def test_riverside_pdf_split_skips_too_short_entries(self) -> None:
        """Entries with <30 chars of body are skipped (defensive)."""
        text = "1.\nTentative Ruling.\n2.\n"
        rulings = _split_rulings(text)
        # Entry 1 is too short; entry 2 has empty body.
        assert rulings == []

    def test_riverside_pdf_split_two_digit_entry_numbers(self) -> None:
        """Entry indices can be 1-3 digits (defensive — some Riverside PDFs
        run >9 entries, e.g. department M302's 9-entry calendar called out
        in the issue body).
        """
        text = (
            "9.\n"
            "CVRI2400009 ALPHA vs BETA Motion 9\n"
            "Tentative Ruling: Granted.\nThe motion is granted in full.\n"
            "10.\n"
            "CVRI2400010 GAMMA vs DELTA Motion 10\n"
            "Tentative Ruling: Denied.\nThe motion is denied without prejudice.\n"
            "11.\n"
            "CVRI2400011 EPSILON vs ZETA Motion 11\n"
            "Tentative Ruling: Continue to next month.\nThis matter is continued.\n"
        )
        rulings = _split_rulings(text)
        assert [r.ruling_index for r in rulings] == [9, 10, 11]
        assert [r.case_number for r in rulings] == [
            "CVRI2400009",
            "CVRI2400010",
            "CVRI2400011",
        ]

    def test_riverside_pdf_split_does_not_match_inline_citations(self) -> None:
        """Numeric citations like '(2010) 48 Cal.4th 32' inside the body
        must NOT be treated as entry boundaries.

        The regex anchors on ``^\\d{1,3}\\.\\s*$`` (number + period on a
        line by itself), so an inline citation like ``v. Santa Clara County
        Bd. of Supervisors (2010) 48 Cal.4th 32, 42`` won't trip a false
        positive even though it contains digits adjacent to a period.
        """
        text = (
            "1.\n"
            "CVPS2400001 SMITH vs JONES\n"
            "Motion to Compel\n"
            "Tentative Ruling: See Foo v. Bar (2010) 48 Cal.4th 32, 42.\n"
            "The motion is GRANTED based on Section 998. The court relies\n"
            "on the rule from (2010) 48 Cal.4th at 50.\n"
            "2.\n"
            "CVPS2400002 ALPHA vs BETA\n"
            "Demurrer\n"
            "Tentative Ruling: Sustained.\nThe demurrer is sustained without leave.\n"
        )
        rulings = _split_rulings(text)
        # Despite the inline 32, 42, 998, 50 numerics, only 2 real entries.
        assert len(rulings) == 2
        assert [r.ruling_index for r in rulings] == [1, 2]

    def test_riverside_pdf_split_does_not_extract_motion_or_outcome(self) -> None:
        """The Riverside splitter intentionally leaves motion_type, outcome,
        and case_title as ``None`` — those are populated by per-entry LLM
        enrichment via ``_llm_enrich_fields``.

        This is the key behavioural difference from the Fresno splitter,
        which extracts those fields deterministically from the structured
        ``Motion:`` / ``Re:`` / ``Tentative Ruling:`` headers Riverside
        PDFs don't have.  Riverside relies on per-entry LLM enrichment
        (each entry processed individually) to fill those fields without
        any cross-entry carry-forward window.
        """
        text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
        rulings = _split_rulings(text)
        for r in rulings:
            assert r.motion_type is None, "splitter must not populate motion_type"
            assert r.outcome is None, "splitter must not populate outcome"
            assert r.case_title is None, "splitter must not populate case_title"

    def test_riverside_pdf_split_strips_page_footers(self) -> None:
        """Per-entry ruling_text must not contain 'Page N of M' footers."""
        text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
        rulings = _split_rulings(text)
        for r in rulings:
            assert "Page 1 of" not in r.ruling_text
            assert "Page 2 of" not in r.ruling_text
            assert "Page 3 of" not in r.ruling_text
            assert "Page 4 of" not in r.ruling_text


class TestRiversideSplitRuling:
    """Unit tests for the SplitRuling dataclass."""

    def test_split_ruling_default_optional_fields(self) -> None:
        """SplitRuling can be constructed with only required fields."""
        r = SplitRuling(
            ruling_index=1,
            case_number="CVPS2400001",
            ruling_text="Some ruling text",
        )
        assert r.ruling_index == 1
        assert r.case_number == "CVPS2400001"
        assert r.ruling_text == "Some ruling text"
        assert r.case_title is None
        assert r.motion_type is None
        assert r.outcome is None
        assert r.hearing_date is None
        assert r.department is None

    def test_split_ruling_with_all_fields(self) -> None:
        """SplitRuling carries all 8 fields in its slots."""
        r = SplitRuling(
            ruling_index=42,
            case_number="CVRI2412345",
            ruling_text="Ruling text",
            case_title="Smith v. Jones",
            motion_type="demurrer",
            outcome="granted",
            hearing_date=datetime(2026, 3, 2),
            department="PS1",
        )
        assert r.ruling_index == 42
        assert r.case_number == "CVRI2412345"
        assert r.case_title == "Smith v. Jones"
        assert r.motion_type == "demurrer"
        assert r.outcome == "granted"
        assert r.hearing_date == datetime(2026, 3, 2)
        assert r.department == "PS1"
