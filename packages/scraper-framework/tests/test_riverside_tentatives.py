"""Tests for Riverside County tentative rulings scraper — hearing_date extraction.

Fixtures captured from live site 2026-03-02:
  riv_page.html            — index page with 17 PDF links
  riv_ps1.pdf              — Dept PS1, Judge Arthur Hester III (4 pages)
  riv_hall_of_justice.pdf   — Dept 260, no rulings placeholder (1 page)
  riv_murrieta.pdf         — Dept M205, Judge Belinda Handy, no rulings (1 page)
  riv_moreno_valley.pdf    — Dept MV1, Judge David E. Gregory (2 pages)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.pdf_link_scraper import _extract_pdf_text
from courts.ca.riverside_tentatives import (
    INDEX_URL,
    RiversideTentativeRulingsScraper,
    _riv_hearing_date_from_text,
)
from courts.ca.riverside_tentatives import default_config as riv_default_config

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


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
def test_riv_run_no_date_when_pdf_has_none() -> None:
    """When a PDF has no date (like hall_of_justice), hearing_date is None."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_hall_of_justice.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Hall of Justice fixture has no date
    assert all(d.hearing_date is None for d in parsed)
