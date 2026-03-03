"""Tests for PDF-link scraper template and OC/Riverside court configs.

Fixtures captured from live sites 2026-03-02:
  oc_civil_page.html   — GET https://www.occourts.org/online-services/tentative-rulings/civil-tentative-rulings
  oc_apkarian_c25.pdf  — gapkarianrulings.pdf (Dept C25, 36 pages, 14 case numbers)
  riv_page.html        — GET https://www.riverside.courts.ca.gov/online-services/tentative-rulings
  riv_ps1.pdf          — PS1ruling030226.pdf (Dept PS1, 4 pages)
"""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from courts.ca.oc_tentatives import (
    BASE_URL as OC_BASE_URL,
)
from courts.ca.oc_tentatives import (
    INDEX_URL as OC_INDEX_URL,
)
from courts.ca.oc_tentatives import (
    OCTentativeRulingsScraper,
    _oc_courthouse,
)
from courts.ca.oc_tentatives import (
    default_config as oc_default_config,
)
from courts.ca.pdf_link_scraper import (
    _extract_pdf_links,
    _extract_pdf_text,
)
from courts.ca.riverside_tentatives import (
    BASE_URL as RIV_BASE_URL,
)
from courts.ca.riverside_tentatives import (
    INDEX_URL as RIV_INDEX_URL,
)
from courts.ca.riverside_tentatives import (
    RiversideTentativeRulingsScraper,
    _riv_courthouse,
)
from courts.ca.riverside_tentatives import (
    default_config as riv_default_config,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _extract_pdf_links — against real OC index page
# ---------------------------------------------------------------------------


def test_oc_extract_pdf_links_count() -> None:
    html = _load_html("oc_civil_page.html")
    links = _extract_pdf_links(html, OC_INDEX_URL, OC_BASE_URL)
    assert len(links) == 33


def test_oc_extract_pdf_links_absolute_urls() -> None:
    html = _load_html("oc_civil_page.html")
    links = _extract_pdf_links(html, OC_INDEX_URL, OC_BASE_URL)
    for url, _ in links:
        assert url.startswith("http"), f"Expected absolute URL, got {url!r}"
        assert ".pdf" in url.lower()


def test_oc_extract_pdf_links_no_duplicates() -> None:
    html = _load_html("oc_civil_page.html")
    links = _extract_pdf_links(html, OC_INDEX_URL, OC_BASE_URL)
    urls = [u for u, _ in links]
    assert len(urls) == len(set(urls))


def test_oc_extract_pdf_links_first_entry() -> None:
    html = _load_html("oc_civil_page.html")
    links = _extract_pdf_links(html, OC_INDEX_URL, OC_BASE_URL)
    # First link is CLASTER Dept CX101
    url, text = links[0]
    assert "claster" in url.lower() or "CX101" in text or "CLASTER" in text


def test_oc_extract_pdf_links_apkarian_present() -> None:
    html = _load_html("oc_civil_page.html")
    links = _extract_pdf_links(html, OC_INDEX_URL, OC_BASE_URL)
    texts = [t for _, t in links]
    assert any("APKARIAN" in t for t in texts)


# ---------------------------------------------------------------------------
# _extract_pdf_links — against real Riverside index page
# ---------------------------------------------------------------------------


def test_riv_extract_pdf_links_count() -> None:
    html = _load_html("riv_page.html")
    links = _extract_pdf_links(html, RIV_INDEX_URL, RIV_BASE_URL)
    assert len(links) == 17


def test_riv_extract_pdf_links_ps1_present() -> None:
    html = _load_html("riv_page.html")
    links = _extract_pdf_links(html, RIV_INDEX_URL, RIV_BASE_URL)
    texts = [t for _, t in links]
    assert any("PS1" in t for t in texts)


def test_riv_extract_pdf_links_judge_in_text() -> None:
    html = _load_html("riv_page.html")
    links = _extract_pdf_links(html, RIV_INDEX_URL, RIV_BASE_URL)
    texts = [t for _, t in links]
    assert any("Honorable" in t for t in texts)


# ---------------------------------------------------------------------------
# _extract_pdf_text — against real PDFs
# ---------------------------------------------------------------------------


def test_oc_pdf_text_extraction() -> None:
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")
    text = _extract_pdf_text(pdf_bytes)
    assert "Apkarian" in text
    assert "DEPT C25" in text or "C25" in text


def test_oc_pdf_text_contains_case_numbers() -> None:
    import re

    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")
    text = _extract_pdf_text(pdf_bytes)
    # OC case number format: DD-DDDDDDDD or DDDD-DDDDDDDD
    matches = re.findall(r"\b\d{2,4}-\d{8}\b", text)
    assert len(matches) >= 10  # real fixture has 14


def test_oc_costa_mesa_case_numbers() -> None:
    """Regression: Costa Mesa PDFs use DDDD-DDDDDDDD format (#47)."""
    import re

    pdf_bytes = _load_bytes("oc_costa_mesa_cm.pdf")
    text = _extract_pdf_text(pdf_bytes)
    # Costa Mesa uses 4-digit year prefix: e.g. 2024-01437598
    matches = re.findall(r"\b\d{2,4}-\d{8}\b", text)
    assert len(matches) >= 5, f"Expected >= 5 case numbers from Costa Mesa, got {len(matches)}"
    assert "2024-01437598" in matches


def test_oc_complex_civil_case_numbers() -> None:
    """Regression: Complex Civil (CX) PDFs use DDDD-DDDDDDDD format (#47)."""
    import re

    pdf_bytes = _load_bytes("oc_complex_cx.pdf")
    text = _extract_pdf_text(pdf_bytes)
    # Complex Civil uses 4-digit year prefix: e.g. 2023-01301305
    matches = re.findall(r"\b\d{2,4}-\d{8}\b", text)
    assert len(matches) >= 9, f"Expected >= 9 case numbers from Complex Civil, got {len(matches)}"
    assert "2023-01301305" in matches


def test_oc_north_has_no_case_numbers() -> None:
    """North Justice Center PDFs do not contain case numbers (#47)."""
    import re

    pdf_bytes = _load_bytes("oc_north_n.pdf")
    text = _extract_pdf_text(pdf_bytes)
    # North PDFs list cases by line number + name only — no case numbers
    matches = re.findall(r"\b\d{2,4}-\d{8}\b", text)
    assert len(matches) == 0, f"Expected 0 case numbers from North, got {len(matches)}"
    # But the PDF should still contain ruling text
    assert "TENTATIVE RULINGS" in text or "Tentative" in text


def test_oc_central_c34_mixed_formats() -> None:
    """C34 uses a mix of DD-DDDDDDDD and DDDD-DDDDDDDD case numbers (#47)."""
    import re

    pdf_bytes = _load_bytes("oc_central_c34.pdf")
    text = _extract_pdf_text(pdf_bytes)
    matches = re.findall(r"\b\d{2,4}-\d{8}\b", text)
    assert len(matches) >= 10, f"Expected >= 10 case numbers from C34, got {len(matches)}"
    # Should find both 2-digit and 4-digit year prefixes
    has_short = any(len(m.split("-")[0]) == 2 for m in matches)
    has_long = any(len(m.split("-")[0]) == 4 for m in matches)
    assert has_short, "C34 should have DD-DDDDDDDD format case numbers"
    assert has_long, "C34 should have DDDD-DDDDDDDD format case numbers"


def test_riv_pdf_text_extraction() -> None:
    pdf_bytes = _load_bytes("riv_ps1.pdf")
    text = _extract_pdf_text(pdf_bytes)
    assert "PS1" in text
    assert "Tentative Ruling" in text or "Tentative" in text


def test_riv_pdf_text_contains_case_number() -> None:
    import re

    pdf_bytes = _load_bytes("riv_ps1.pdf")
    text = _extract_pdf_text(pdf_bytes)
    # Riverside case format: CV + letters + digits
    matches = re.findall(r"\bCV[A-Z]{2,4}\d{6,8}\b", text)
    assert len(matches) >= 1


# ---------------------------------------------------------------------------
# Courthouse mapping helpers
# ---------------------------------------------------------------------------


def test_oc_courthouse_complex() -> None:
    assert _oc_courthouse("CX101") == "Complex Civil Department"
    assert _oc_courthouse("CX105") == "Complex Civil Department"


def test_oc_courthouse_central() -> None:
    assert _oc_courthouse("C25") == "Central Justice Center"
    assert _oc_courthouse("C11") == "Central Justice Center"


def test_oc_courthouse_north() -> None:
    assert _oc_courthouse("N15") == "North Justice Center"
    assert _oc_courthouse("N6") == "North Justice Center"


def test_oc_courthouse_west() -> None:
    assert _oc_courthouse("W8") == "West Justice Center"
    assert _oc_courthouse("W15") == "West Justice Center"


def test_oc_courthouse_costa_mesa() -> None:
    assert _oc_courthouse("CM02") == "Costa Mesa Justice Center"


def test_riv_courthouse_palm_springs() -> None:
    assert _riv_courthouse("PS1") == "Palm Springs Courthouse"
    assert _riv_courthouse("PS2") == "Palm Springs Courthouse"


def test_riv_courthouse_murrieta() -> None:
    assert _riv_courthouse("M205") == "Murrieta Courthouse"
    assert _riv_courthouse("M301") == "Murrieta Courthouse"


def test_riv_courthouse_moreno_valley() -> None:
    assert _riv_courthouse("MV1") == "Moreno Valley Courthouse"


def test_riv_courthouse_hall_of_justice() -> None:
    assert _riv_courthouse("01") == "Hall of Justice"
    assert _riv_courthouse("10") == "Hall of Justice"


# ---------------------------------------------------------------------------
# Full OC scraper run — mocked HTTP using real fixtures
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_full_run() -> None:
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(OC_INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    # All PDF GETs return the same sample PDF
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 33  # real fixture has 33 links


@respx.mock
def test_oc_run_populates_judge_and_dept() -> None:
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(OC_INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    # Call fetch_documents directly to inspect docs
    docs = scraper.fetch_documents()
    assert len(docs) == 33

    # Check first doc has judge and dept populated
    first = docs[0]
    assert first.department is not None
    assert first.judge_name is not None
    assert first.courthouse is not None


@respx.mock
def test_oc_run_extracts_case_numbers() -> None:
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(OC_INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # parse_document populates case numbers from PDF text
    parsed = [scraper.parse_document(d) for d in docs]
    # The Apkarian PDF has case numbers
    has_case_number = [d for d in parsed if d.case_number]
    assert len(has_case_number) > 0


@respx.mock
def test_oc_run_handles_get_failure() -> None:
    respx.get(OC_INDEX_URL).mock(return_value=httpx.Response(503))

    config = oc_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_oc_run_continues_when_pdf_fails() -> None:
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(OC_INDEX_URL).mock(return_value=httpx.Response(200, text=html))

    call_count = 0

    def pdf_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(404)
        return httpx.Response(200, content=pdf_bytes)

    respx.get(url__regex=r"\.pdf$").mock(side_effect=pdf_side_effect)

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 32  # 33 - 1 failed


# ---------------------------------------------------------------------------
# Full Riverside scraper run — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_full_run() -> None:
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(RIV_INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 17


@respx.mock
def test_riv_run_populates_judge_and_dept() -> None:
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(RIV_INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) == 17

    # PS1 doc should have judge Hester
    ps1_docs = [d for d in docs if d.department == "PS1"]
    assert len(ps1_docs) == 1
    assert "Hester" in (ps1_docs[0].judge_name or "")
    assert ps1_docs[0].courthouse == "Palm Springs Courthouse"


# ---------------------------------------------------------------------------
# Config factories
# ---------------------------------------------------------------------------


def test_oc_default_config() -> None:
    config = oc_default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-oc-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Orange"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2


def test_riv_default_config() -> None:
    config = riv_default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-riverside-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Riverside"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2
