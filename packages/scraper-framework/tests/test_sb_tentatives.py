"""Tests for San Bernardino County tentative rulings scraper.

Fixtures captured from live site 2026-03-02:
  sb_iframe_page.html          — GET https://old.sb-court.org/GeneralInfo/TentativeRulings.aspx
                                  (52 PDF links)
  sb_r12_20260303_0df41117.pdf — CVR12030326.pdf  Dept R12, Judge Kory Mathewson
  sb_r17_20260302_817a3a84.pdf — CVR17030226.pdf  Dept R17, Judge Gilbert G. Ochoa
  sb_s22_20260302_6849aa12.pdf — CVS22030226.pdf  Dept S22, Judge David Driscoll (em-dash)
  sb_s24_20260304_a1f33a43.pdf — CVS24030426.pdf  Dept S24, Judge Carlos M. Cabrera
  sb_s36_20260303_a6da3fb7.pdf — CVS36030326.pdf  Dept S36, Judge Joseph Widman (HONORABLE fmt)
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.pdf_link_scraper import _extract_pdf_links, _extract_pdf_text
from courts.ca.sb_tentatives import (
    BASE_URL,
    INDEX_URL,
    SBTentativeRulingsScraper,
    _sb_courthouse,
    _sb_judge_from_pdf_text,
)
from courts.ca.sb_tentatives import default_config as sb_default_config

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _extract_pdf_links — against real SB index page
# ---------------------------------------------------------------------------


def test_sb_extract_pdf_links_count() -> None:
    html = _load_html("sb_iframe_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    assert len(links) == 52


def test_sb_extract_pdf_links_absolute_urls() -> None:
    html = _load_html("sb_iframe_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    for url, _ in links:
        assert url.startswith("http"), f"Expected absolute URL, got {url!r}"
        assert ".pdf" in url.lower()


def test_sb_extract_pdf_links_no_duplicates() -> None:
    html = _load_html("sb_iframe_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    urls = [u for u, _ in links]
    assert len(urls) == len(set(urls))


def test_sb_extract_pdf_links_first_entry() -> None:
    html = _load_html("sb_iframe_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    url, text = links[0]
    assert "CVS24030426" in url
    assert text == "CVS24030426.pdf"


def test_sb_extract_pdf_links_r12_present() -> None:
    html = _load_html("sb_iframe_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    texts = [t for _, t in links]
    assert any("CVR12" in t for t in texts)


# ---------------------------------------------------------------------------
# _extract_pdf_text — against real SB fixture PDFs
# ---------------------------------------------------------------------------


def test_sb_r12_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("sb_r12_20260303_0df41117.pdf"))
    assert "R12" in text
    assert "Mathewson" in text


def test_sb_r17_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("sb_r17_20260302_817a3a84.pdf"))
    assert "R17" in text
    assert "Ochoa" in text


def test_sb_s36_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("sb_s36_20260303_a6da3fb7.pdf"))
    assert "S36" in text
    assert "WIDMAN" in text


# ---------------------------------------------------------------------------
# _sb_judge_from_pdf_text — all five fixture PDFs
# ---------------------------------------------------------------------------


def test_sb_judge_r12_primary_format() -> None:
    text = _extract_pdf_text(_load_bytes("sb_r12_20260303_0df41117.pdf"))
    assert _sb_judge_from_pdf_text(text) == "Kory Mathewson"


def test_sb_judge_r17_no_space_before_dash() -> None:
    # "Department R17- Judge Gilbert G. Ochoa" — no space before dash
    text = _extract_pdf_text(_load_bytes("sb_r17_20260302_817a3a84.pdf"))
    assert _sb_judge_from_pdf_text(text) == "Gilbert G. Ochoa"


def test_sb_judge_s22_em_dash() -> None:
    # "Department S22 – Judge David Driscoll" — Unicode en-dash
    text = _extract_pdf_text(_load_bytes("sb_s22_20260302_6849aa12.pdf"))
    assert _sb_judge_from_pdf_text(text) == "David Driscoll"


def test_sb_judge_s24_primary_format() -> None:
    text = _extract_pdf_text(_load_bytes("sb_s24_20260304_a1f33a43.pdf"))
    assert _sb_judge_from_pdf_text(text) == "Carlos M. Cabrera"


def test_sb_judge_s36_honorable_fallback() -> None:
    # "BEFORE THE HONORABLE JOSEPH WIDMAN" — no "Department X - Judge" line
    text = _extract_pdf_text(_load_bytes("sb_s36_20260303_a6da3fb7.pdf"))
    assert _sb_judge_from_pdf_text(text) == "Joseph Widman"


def test_sb_judge_em_dash_format() -> None:
    # Defensive: em-dash (U+2014) should also be handled even though only en-dash
    # has been observed so far in live PDFs.
    text = "Department S99 \u2014 Judge Jane Doe\nSome ruling text"
    assert _sb_judge_from_pdf_text(text) == "Jane Doe"


def test_sb_judge_returns_none_for_empty_text() -> None:
    assert _sb_judge_from_pdf_text("") is None
    assert _sb_judge_from_pdf_text("No header here") is None


# ---------------------------------------------------------------------------
# Case number extraction — against real PDFs
# ---------------------------------------------------------------------------


def test_sb_r12_case_number() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sb_r12_20260303_0df41117.pdf"))
    matches = re.findall(r"\bCIV[A-Z]{2}\d{5,8}\b", text)
    assert "CIVRS2502080" in matches


def test_sb_r17_case_number() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sb_r17_20260302_817a3a84.pdf"))
    matches = re.findall(r"\bCIV[A-Z]{2}\d{5,8}\b", text)
    assert len(matches) >= 1


def test_sb_s22_multiple_case_numbers() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sb_s22_20260302_6849aa12.pdf"))
    matches = re.findall(r"\bCIV[A-Z]{2}\d{5,8}\b", text)
    assert len(matches) >= 2


def test_sb_s36_no_case_numbers() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sb_s36_20260303_a6da3fb7.pdf"))
    matches = re.findall(r"\bCIV[A-Z]{2}\d{5,8}\b", text)
    assert len(matches) == 0


# ---------------------------------------------------------------------------
# Courthouse mapping
# ---------------------------------------------------------------------------


def test_sb_courthouse_san_bernardino() -> None:
    assert _sb_courthouse("S22") == "San Bernardino Justice Center"
    assert _sb_courthouse("S36") == "San Bernardino Justice Center"
    assert _sb_courthouse("S14") == "San Bernardino Justice Center"


def test_sb_courthouse_rancho_cucamonga() -> None:
    assert _sb_courthouse("R12") == "Rancho Cucamonga Justice Center"
    assert _sb_courthouse("R17") == "Rancho Cucamonga Justice Center"
    assert _sb_courthouse("R14") == "Rancho Cucamonga Justice Center"


def test_sb_courthouse_unknown_returns_none() -> None:
    assert _sb_courthouse("V10") is None
    assert _sb_courthouse("B5") is None


# ---------------------------------------------------------------------------
# Full scraper run — mocked HTTP using real fixtures
# ---------------------------------------------------------------------------


@respx.mock
def test_sb_full_run() -> None:
    html = _load_html("sb_iframe_page.html")
    pdf_bytes = _load_bytes("sb_r12_20260303_0df41117.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sb_default_config()
    config.request_delay_seconds = 0
    scraper = SBTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 52


@respx.mock
def test_sb_run_populates_dept_from_filename() -> None:
    html = _load_html("sb_iframe_page.html")
    pdf_bytes = _load_bytes("sb_r12_20260303_0df41117.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sb_default_config()
    config.request_delay_seconds = 0
    scraper = SBTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) == 52

    # First link is CVS24030426 → dept S24, courthouse San Bernardino
    first = docs[0]
    assert first.department == "S24"
    assert first.courthouse == "San Bernardino Justice Center"


@respx.mock
def test_sb_run_populates_judge_from_pdf_text() -> None:
    html = _load_html("sb_iframe_page.html")
    pdf_bytes = _load_bytes("sb_r12_20260303_0df41117.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sb_default_config()
    config.request_delay_seconds = 0
    scraper = SBTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Every doc should have judge name extracted from the r12 PDF fixture
    has_judge = [d for d in parsed if d.judge_name]
    assert len(has_judge) == 52
    assert has_judge[0].judge_name == "Kory Mathewson"


@respx.mock
def test_sb_run_extracts_case_numbers() -> None:
    html = _load_html("sb_iframe_page.html")
    pdf_bytes = _load_bytes("sb_r12_20260303_0df41117.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sb_default_config()
    config.request_delay_seconds = 0
    scraper = SBTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]
    has_case = [d for d in parsed if d.case_number]
    assert len(has_case) > 0
    assert has_case[0].case_number == "CIVRS2502080"


@respx.mock
def test_sb_run_handles_get_failure() -> None:
    respx.get(INDEX_URL).mock(return_value=httpx.Response(503))

    config = sb_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = SBTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_sb_run_continues_when_pdf_fails() -> None:
    html = _load_html("sb_iframe_page.html")
    pdf_bytes = _load_bytes("sb_r12_20260303_0df41117.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))

    call_count = 0

    def pdf_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(404)
        return httpx.Response(200, content=pdf_bytes)

    respx.get(url__regex=r"\.pdf$").mock(side_effect=pdf_side_effect)

    config = sb_default_config()
    config.request_delay_seconds = 0
    scraper = SBTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 51  # 52 - 1 failed


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def test_sb_default_config() -> None:
    config = sb_default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-sb-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "San Bernardino"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2
