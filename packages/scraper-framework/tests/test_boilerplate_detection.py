"""Tests for boilerplate detection in PDF-link scrapers (#322).

Verifies that the base-class _is_boilerplate() hook and per-court
overrides correctly identify placeholder PDFs that should be skipped.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.oc_family_law_tentatives import (
    INDEX_URL as OC_FL_INDEX_URL,
)
from courts.ca.oc_family_law_tentatives import (
    OCFamilyLawTentativeRulingsScraper,
)
from courts.ca.oc_family_law_tentatives import (
    default_config as oc_fl_default_config,
)
from courts.ca.oc_tentatives import (
    OCTentativeRulingsScraper,
)
from courts.ca.oc_tentatives import (
    default_config as oc_default_config,
)
from courts.ca.pdf_link_scraper import PdfLinkConfig, PdfLinkScraper, _extract_pdf_text
from courts.ca.riverside_tentatives import (
    INDEX_URL as RIV_INDEX_URL,
)
from courts.ca.riverside_tentatives import (
    RiversideTentativeRulingsScraper,
)
from courts.ca.riverside_tentatives import (
    default_config as riv_default_config,
)
from framework import ScraperConfig

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Base-class _is_boilerplate — unit tests
# ---------------------------------------------------------------------------


def _make_base_scraper() -> PdfLinkScraper:
    """Create a PdfLinkScraper instance for testing _is_boilerplate."""
    import re

    config = ScraperConfig(
        scraper_id="test",
        state="CA",
        county="Test",
        court="Superior Court",
        target_urls=["http://example.com"],
        s3_bucket="",
    )
    pdf_config = PdfLinkConfig(
        index_url="http://example.com",
        pdf_base_url="http://example.com",
        link_text_re=re.compile(r"(?P<department>\S+)\s+(?P<judge_name>.+)"),
    )
    return PdfLinkScraper(config=config, pdf_config=pdf_config)


def test_base_is_boilerplate_no_tentative_rulings() -> None:
    """'No Tentative Rulings ...' at start of text is detected."""
    scraper = _make_base_scraper()
    assert scraper._is_boilerplate("No Tentative Rulings March 2, 2026") is True


def test_base_is_boilerplate_no_tentative_ruling_singular() -> None:
    """'No Tentative Ruling' (singular) is also detected."""
    scraper = _make_base_scraper()
    assert scraper._is_boilerplate("No Tentative Ruling for this department.") is True


def test_base_is_boilerplate_case_insensitive() -> None:
    """Detection is case-insensitive."""
    scraper = _make_base_scraper()
    assert scraper._is_boilerplate("NO TENTATIVE RULINGS March 2, 2026") is True
    assert scraper._is_boilerplate("no tentative rulings for\nDepartment 1") is True


def test_base_is_boilerplate_with_leading_whitespace() -> None:
    """Leading whitespace is tolerated."""
    scraper = _make_base_scraper()
    assert scraper._is_boilerplate("  No Tentative Rulings for March 2, 2026") is True


def test_base_is_boilerplate_false_for_real_rulings() -> None:
    """Real ruling text is NOT boilerplate."""
    scraper = _make_base_scraper()
    text = "Tentative Rulings for March 2, 2026\nDepartment PS1\n..."
    assert scraper._is_boilerplate(text) is False


def test_base_is_boilerplate_false_for_empty() -> None:
    """Empty string is NOT boilerplate."""
    scraper = _make_base_scraper()
    assert scraper._is_boilerplate("") is False


# ---------------------------------------------------------------------------
# Riverside — boilerplate detection via base class hook
# ---------------------------------------------------------------------------


def test_riv_is_boilerplate_murrieta_fixture() -> None:
    """Murrieta 'No Tentative Rulings' fixture is detected by base class."""
    text = _extract_pdf_text(_load_bytes("riv_murrieta.pdf"))
    config = riv_default_config()
    scraper = RiversideTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is True


def test_riv_is_boilerplate_hall_of_justice_fixture() -> None:
    """Hall of Justice 'No Tentative Rulings' fixture is detected by base class."""
    text = _extract_pdf_text(_load_bytes("riv_hall_of_justice.pdf"))
    config = riv_default_config()
    scraper = RiversideTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is True


def test_riv_is_boilerplate_false_for_real_rulings() -> None:
    """Real PS1 fixture is NOT boilerplate."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    config = riv_default_config()
    scraper = RiversideTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is False


@respx.mock
def test_riv_fetch_skips_boilerplate_pdfs() -> None:
    """Full Riverside run with boilerplate PDFs skips them via base class."""
    html = _load_html("riv_page.html")
    boilerplate_bytes = _load_bytes("riv_murrieta.pdf")

    respx.get(RIV_INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=boilerplate_bytes)
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)
    docs = scraper.fetch_documents()

    # All 17 PDFs are boilerplate — all should be skipped
    assert len(docs) == 0


# ---------------------------------------------------------------------------
# OC Family Law — boilerplate detection
# ---------------------------------------------------------------------------


def test_oc_fl_is_boilerplate_kohler_fixture() -> None:
    """Kohler L69 empty template PDF is detected as boilerplate."""
    text = _extract_pdf_text(_load_bytes("oc_family_law_kohler_l69.pdf"))
    config = oc_fl_default_config()
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is True


def test_oc_fl_is_boilerplate_false_for_claustro() -> None:
    """Claustro C22 with real rulings is NOT boilerplate."""
    text = _extract_pdf_text(_load_bytes("oc_family_law_claustro_c22.pdf"))
    config = oc_fl_default_config()
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is False


@respx.mock
def test_oc_fl_fetch_skips_boilerplate_pdf() -> None:
    """OC Family Law run skips Kohler boilerplate but keeps Claustro."""
    html = _load_html("oc_family_law_page.html")
    claustro_bytes = _load_bytes("oc_family_law_claustro_c22.pdf")
    kohler_bytes = _load_bytes("oc_family_law_kohler_l69.pdf")

    respx.get(OC_FL_INDEX_URL).mock(return_value=httpx.Response(200, text=html))

    # Map actual PDF URLs to appropriate fixtures
    # We need to serve different PDFs for different URLs.
    # The index page has 2 links — use side_effect to alternate.
    call_count = 0

    def pdf_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        url = str(request.url).lower()
        if "kohler" in url:
            return httpx.Response(200, content=kohler_bytes)
        return httpx.Response(200, content=claustro_bytes)

    respx.get(url__regex=r"\.pdf$").mock(side_effect=pdf_side_effect)

    config = oc_fl_default_config()
    config.request_delay_seconds = 0
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)
    docs = scraper.fetch_documents()

    # Kohler (boilerplate) should be skipped, Claustro should remain
    assert len(docs) == 1
    assert docs[0].department == "C22"


# ---------------------------------------------------------------------------
# OC Civil — boilerplate detection
# ---------------------------------------------------------------------------


def test_oc_civil_is_boilerplate_false_for_apkarian() -> None:
    """Real Apkarian C25 PDF is NOT boilerplate."""
    text = _extract_pdf_text(_load_bytes("oc_apkarian_c25.pdf"))
    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is False


def test_oc_civil_is_boilerplate_false_for_north() -> None:
    """North JC PDF (no case numbers but real rulings) is NOT boilerplate.

    North JC PDFs lack case numbers by design but contain actual rulings.
    The OC civil _is_boilerplate check uses the configured case_number_re
    which only matches civil case number patterns — this test ensures
    North JC PDFs (which have ruling text) are not falsely flagged.
    """
    text = _extract_pdf_text(_load_bytes("oc_north_n.pdf"))
    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is False


def test_oc_civil_is_boilerplate_synthetic_empty_page() -> None:
    """Synthetic empty OC civil page (TENTATIVE RULINGS + empty Date:) is boilerplate."""
    text = (
        "TENTATIVE RULINGS\n"
        "LAW & MOTION\n"
        "DEPT C99\n"
        "Judge Test Judge\n"
        "The court will hear oral argument on all matters.\n"
        "Date:\n"
        "# Case Name\n"
        "1\n"
        "2\n"
    )
    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    assert scraper._is_boilerplate(text) is True
