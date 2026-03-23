"""Tests for Orange County tentative rulings scraper.

Covers capture logic (link discovery, judge name reconstruction, boilerplate
detection) and the case_number_re regex. Field extraction (case_number,
hearing_date, case_title, etc.) is handled by the multimodal LLM ingestion
pipeline and tested separately.

Fixtures captured from live site 2026-03-02:
  oc_civil_page.html     -- index page with 33 PDF links
  oc_apkarian_c25.pdf    -- Dept C25, Judge Gassia Apkarian (36 pages)
  oc_central_c34.pdf     -- Dept C34, Judge H. Shaina Colover (27 pages)
  oc_complex_cx.pdf      -- Dept CX101, Judge William D. Claster (2 pages)
  oc_costa_mesa_cm.pdf   -- Dept CM02, Judge Andre De La Cruz (33 pages)
  oc_north_n.pdf         -- Dept N6, Judge Julianne S. Bancroft (26 pages)
  oc_west_w.pdf          -- Dept W15, Judge Richard Y. Lee (38 pages)
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.oc_tentatives import (
    INDEX_URL,
    OCTentativeRulingsScraper,
)
from courts.ca.oc_tentatives import default_config as oc_default_config
from framework import ContentFormat

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# case_number_re — regex unit tests
# ---------------------------------------------------------------------------


def test_seven_digit_case_number_matched() -> None:
    """Case number with 7 digits after dash (C20 format) is captured."""
    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    regex = scraper._pdf_config.case_number_re

    assert regex.search("24-1377364") is not None
    assert regex.search("24-1377364").group() == "24-1377364"


def test_seven_digit_case_number_in_context() -> None:
    """Seven-digit case number extracted from realistic PDF text."""
    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    regex = scraper._pdf_config.case_number_re

    text = "1. Smith v. Jones   Motion for Summary Judgment\n24-1377364          GRANTED."
    matches = regex.findall(text)
    assert "24-1377364" in matches


def test_eight_digit_case_number_still_matched() -> None:
    """Standard 8-digit case numbers still work after regex relaxation."""
    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    regex = scraper._pdf_config.case_number_re

    assert regex.search("25-01455183") is not None
    assert regex.search("2024-01437598") is not None


# ---------------------------------------------------------------------------
# Full scraper run — capture logic (link discovery, fetch, judge name)
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_fetch_discovers_pdf_links() -> None:
    """Scraper discovers PDF links from the index page."""
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()

    # The fixture has 33 PDF links
    assert len(docs) > 0


@respx.mock
def test_oc_fetch_reconstructs_judge_name() -> None:
    """Judge name is reconstructed as 'Firstname Lastname' from link text."""
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()

    # Find a doc for Apkarian
    apkarian_docs = [d for d in docs if d.judge_name and "Apkarian" in d.judge_name]
    assert len(apkarian_docs) > 0
    # Name should be "Firstname Lastname" not "LASTNAME, Firstname"
    assert apkarian_docs[0].judge_name == "Gassia Apkarian"


@respx.mock
def test_oc_fetch_populates_department() -> None:
    """Department is extracted from link text."""
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()

    # Most docs should have a department (some link text may not match regex)
    with_dept = [d for d in docs if d.department]
    assert len(with_dept) >= len(docs) - 1


# ---------------------------------------------------------------------------
# parse_document — no-op (field extraction handled by LLM pipeline)
# ---------------------------------------------------------------------------


def test_oc_parse_document_is_noop() -> None:
    """parse_document returns the doc unchanged (no field extraction at scrape time)."""
    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=b"fake-pdf-bytes",
        content_format=ContentFormat.PDF,
    )
    doc.department = "C25"
    doc.judge_name = "Gassia Apkarian"

    parsed = scraper.parse_document(doc)

    # parse_document should return the same doc object unchanged
    assert parsed is doc
    assert parsed.ruling_text is None
    assert parsed.case_number is None
    assert parsed.hearing_date is None
    assert parsed.case_title is None
