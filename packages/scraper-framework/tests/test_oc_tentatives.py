"""Tests for Orange County tentative rulings scraper — hearing_date extraction.

Fixtures captured from live site 2026-03-02:
  oc_civil_page.html     — index page with 33 PDF links
  oc_apkarian_c25.pdf    — Dept C25, Judge Gassia Apkarian (36 pages)
  oc_central_c34.pdf     — Dept C34, Judge H. Shaina Colover (27 pages)
  oc_complex_cx.pdf      — Dept CX101, Judge William D. Claster (2 pages)
  oc_costa_mesa_cm.pdf   — Dept CM02, Judge Andre De La Cruz (33 pages)
  oc_north_n.pdf         — Dept N6, Judge Julianne S. Bancroft (26 pages)
  oc_west_w.pdf          — Dept W15, Judge Richard Y. Lee (38 pages)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.oc_tentatives import (
    INDEX_URL,
    OCTentativeRulingsScraper,
    _oc_hearing_date_from_text,
)
from courts.ca.oc_tentatives import default_config as oc_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _oc_hearing_date_from_text — unit tests
# ---------------------------------------------------------------------------


def test_oc_hearing_date_standard_format() -> None:
    text = "TENTATIVE RULINGS\nDEPT C25\nDate: February 24, 2026\nSome ruling"
    assert _oc_hearing_date_from_text(text) == datetime(2026, 2, 24)


def test_oc_hearing_date_leading_zero_day() -> None:
    text = "March 06, 2026\n09:00 a.m.\nCX-101"
    assert _oc_hearing_date_from_text(text) == datetime(2026, 3, 6)


def test_oc_hearing_date_no_comma() -> None:
    text = "February 24 2026\nSome text"
    assert _oc_hearing_date_from_text(text) == datetime(2026, 2, 24)


def test_oc_hearing_date_returns_none_for_no_date() -> None:
    assert _oc_hearing_date_from_text("") is None
    assert _oc_hearing_date_from_text("No dates here at all") is None


# ---------------------------------------------------------------------------
# _oc_hearing_date_from_text — against real PDF fixtures
# ---------------------------------------------------------------------------


def test_oc_hearing_date_apkarian_c25() -> None:
    text = _extract_pdf_text(_load_bytes("oc_apkarian_c25.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 24)


def test_oc_hearing_date_central_c34() -> None:
    text = _extract_pdf_text(_load_bytes("oc_central_c34.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 26)


def test_oc_hearing_date_complex_cx() -> None:
    text = _extract_pdf_text(_load_bytes("oc_complex_cx.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 6)


def test_oc_hearing_date_costa_mesa_cm() -> None:
    text = _extract_pdf_text(_load_bytes("oc_costa_mesa_cm.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 19)


def test_oc_hearing_date_north_n() -> None:
    text = _extract_pdf_text(_load_bytes("oc_north_n.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 2)


def test_oc_hearing_date_west_w() -> None:
    text = _extract_pdf_text(_load_bytes("oc_west_w.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 26)


# ---------------------------------------------------------------------------
# Full scraper run — hearing_date populated
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_run_populates_hearing_date() -> None:
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # All docs use the same PDF fixture, so all should have a hearing date
    has_date = [d for d in parsed if d.hearing_date]
    assert len(has_date) == len(parsed)
    assert has_date[0].hearing_date == datetime(2026, 2, 24)
