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

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from courts.ca.pdf_link_scraper import PdfLinkScraper, _extract_pdf_text
from courts.ca.riverside_tentatives import (
    _CASE_NUMBER_RE,
    INDEX_URL,
    RiversideTentativeRulingsScraper,
    _is_no_tentative_rulings,
    _riv_courthouse,
    _riv_hearing_date_from_text,
)
from courts.ca.riverside_tentatives import default_config as riv_default_config
from framework import CapturedDocument, ContentFormat

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"

# Expected number of PDF links processed from riv_page.html after filtering.
# The fixture has 17 links; 1 is excluded by ``link_text_re`` (#1845), leaving 16.
_RIV_EXPECTED_PROCESSED_PDFS = 16


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
    assert _riv_courthouse("M205") == "Murrieta Courthouse"
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
    # (1 of 17 links filtered by link_text_re, #1845)
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
    # (1 of 17 links filtered by link_text_re, #1845)
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


# ---------------------------------------------------------------------------
# Riverside system prompt — now in extraction_config (#1728)
# ---------------------------------------------------------------------------


def test_riverside_system_prompt_in_extraction_config() -> None:
    """The Riverside system prompt is in extraction_config, not the scraper (#1728)."""
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
