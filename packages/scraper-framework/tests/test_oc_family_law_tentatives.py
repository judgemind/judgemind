"""Tests for Orange County Family Law tentative rulings scraper.

Fixtures captured from live site 2026-03-07:
  oc_family_law_page.html         — index page with 2 PDF links
  oc_family_law_claustro_c22.pdf  — Dept C22, Judge Israel Claustro (1 page, 3 cases)
  oc_family_law_kohler_l69.pdf    — Dept L69, Commissioner Robert Kohler (1 page, empty)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.oc_family_law_tentatives import (
    INDEX_URL,
    OCFamilyLawTentativeRulingsScraper,
    _oc_fl_case_title_from_text,
    _oc_fl_courthouse,
    _oc_fl_hearing_date_from_text,
    _oc_fl_motion_type_from_text,
    _oc_fl_outcome_from_text,
)
from courts.ca.oc_family_law_tentatives import default_config as fl_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Courthouse mapping
# ---------------------------------------------------------------------------


def test_oc_fl_courthouse_central() -> None:
    assert _oc_fl_courthouse("C22") == "Central Justice Center"


def test_oc_fl_courthouse_lamoreaux() -> None:
    assert _oc_fl_courthouse("L69") == "Lamoreaux Justice Center"


def test_oc_fl_courthouse_north() -> None:
    assert _oc_fl_courthouse("N6") == "North Justice Center"


def test_oc_fl_courthouse_west() -> None:
    assert _oc_fl_courthouse("W15") == "West Justice Center"


# ---------------------------------------------------------------------------
# Hearing date extraction — unit tests
# ---------------------------------------------------------------------------


def test_oc_fl_hearing_date_standard() -> None:
    text = "Date: December 5, 2025\n# Case Name"
    assert _oc_fl_hearing_date_from_text(text) == datetime(2025, 12, 5)


def test_oc_fl_hearing_date_no_comma() -> None:
    text = "February 24 2026\nSome text"
    assert _oc_fl_hearing_date_from_text(text) == datetime(2026, 2, 24)


def test_oc_fl_hearing_date_none() -> None:
    assert _oc_fl_hearing_date_from_text("No date here") is None
    assert _oc_fl_hearing_date_from_text("") is None


# ---------------------------------------------------------------------------
# Hearing date extraction — against real PDF fixtures
# ---------------------------------------------------------------------------


def test_oc_fl_hearing_date_claustro_c22() -> None:
    text = _extract_pdf_text(_load_bytes("oc_family_law_claustro_c22.pdf"))
    dt = _oc_fl_hearing_date_from_text(text)
    assert dt == datetime(2025, 12, 5)


def test_oc_fl_hearing_date_kohler_l69_empty() -> None:
    """Kohler L69 PDF has boilerplate only — no hearing date."""
    text = _extract_pdf_text(_load_bytes("oc_family_law_kohler_l69.pdf"))
    dt = _oc_fl_hearing_date_from_text(text)
    # Kohler's PDF says "Date:" with nothing after — no hearing date
    assert dt is None


# ---------------------------------------------------------------------------
# Case title extraction
# ---------------------------------------------------------------------------


def test_oc_fl_case_title_from_text() -> None:
    text = "5 BAEZ V. BAEZ Court will continue this matter"
    assert _oc_fl_case_title_from_text(text) == "BAEZ V. BAEZ"


def test_oc_fl_case_title_lowercase_v() -> None:
    text = "10 FORBES v. FORBES No Tentative Ruling"
    assert _oc_fl_case_title_from_text(text) == "FORBES v. FORBES"


def test_oc_fl_case_title_none() -> None:
    assert _oc_fl_case_title_from_text("No cases here") is None


def test_oc_fl_case_title_claustro() -> None:
    text = _extract_pdf_text(_load_bytes("oc_family_law_claustro_c22.pdf"))
    title = _oc_fl_case_title_from_text(text)
    assert title is not None
    assert "BAEZ" in title


# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------


def test_oc_fl_outcome_granted() -> None:
    assert _oc_fl_outcome_from_text("Motion is GRANTED") == "GRANTED"


def test_oc_fl_outcome_denied() -> None:
    assert _oc_fl_outcome_from_text("Request is DENIED") == "DENIED"


def test_oc_fl_outcome_off_calendar() -> None:
    assert _oc_fl_outcome_from_text("taken off calendar") == "OFF CALENDAR"


def test_oc_fl_outcome_claustro() -> None:
    text = _extract_pdf_text(_load_bytes("oc_family_law_claustro_c22.pdf"))
    outcome = _oc_fl_outcome_from_text(text)
    assert outcome is not None


# ---------------------------------------------------------------------------
# Motion type extraction
# ---------------------------------------------------------------------------


def test_oc_fl_motion_type_rfo() -> None:
    text = "continue this RFO with to allow MP to amend"
    assert _oc_fl_motion_type_from_text(text) == "Request for Order"


def test_oc_fl_motion_type_motion() -> None:
    text = "Motion for Summary Judgment is granted"
    result = _oc_fl_motion_type_from_text(text)
    assert result is not None
    assert "Motion for Summary Judgment" in result


def test_oc_fl_motion_type_none() -> None:
    assert _oc_fl_motion_type_from_text("No motion references") is None


def test_oc_fl_motion_type_claustro() -> None:
    text = _extract_pdf_text(_load_bytes("oc_family_law_claustro_c22.pdf"))
    motion = _oc_fl_motion_type_from_text(text)
    # Claustro PDF has "RFO" reference
    assert motion is not None


# ---------------------------------------------------------------------------
# Full scraper run — index page parsing + PDF fetching
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_fl_run_fetches_documents() -> None:
    """Full run: fetch index page, discover 2 PDF links, fetch each PDF."""
    html = _load_html("oc_family_law_page.html")
    pdf_bytes = _load_bytes("oc_family_law_claustro_c22.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = fl_default_config()
    config.request_delay_seconds = 0
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) == 2

    # First doc: Claustro
    doc = docs[0]
    assert doc.judge_name == "Israel Claustro"
    assert doc.department == "C22"
    assert doc.courthouse == "Central Justice Center"

    # Second doc: Kohler
    doc = docs[1]
    assert doc.judge_name == "Robert Kohler"
    assert doc.department == "L69"
    assert doc.courthouse == "Lamoreaux Justice Center"


@respx.mock
def test_oc_fl_run_populates_all_fields() -> None:
    """Full run with parse: all extractable fields populated."""
    html = _load_html("oc_family_law_page.html")
    pdf_bytes = _load_bytes("oc_family_law_claustro_c22.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = fl_default_config()
    config.request_delay_seconds = 0
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Verify first parsed doc has all fields
    doc = parsed[0]
    assert doc.judge_name == "Israel Claustro"
    assert doc.department == "C22"
    assert doc.courthouse == "Central Justice Center"
    assert doc.hearing_date == datetime(2025, 12, 5)
    assert doc.case_number == "25D006297"
    assert doc.case_title is not None
    assert doc.outcome is not None
    assert doc.motion_type is not None
    assert doc.ruling_text is not None


@respx.mock
def test_oc_fl_run_with_empty_rulings_pdf() -> None:
    """Kohler L69 PDF is empty — scraper handles gracefully."""
    html = _load_html("oc_family_law_page.html")
    pdf_bytes = _load_bytes("oc_family_law_kohler_l69.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = fl_default_config()
    config.request_delay_seconds = 0
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Judge name still extracted from link text
    assert parsed[0].judge_name == "Israel Claustro"
    # Kohler's empty PDF: no case number, no hearing date
    doc = parsed[1]
    assert doc.judge_name == "Robert Kohler"
    assert doc.case_number is None
    # hearing_date may or may not be None depending on boilerplate


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------


def test_oc_fl_default_config() -> None:
    config = fl_default_config()
    assert config.scraper_id == "ca-oc-tentatives-family-law"
    assert config.state == "CA"
    assert config.county == "Orange"
    assert len(config.schedule_windows) == 2


def test_oc_fl_default_config_with_bucket() -> None:
    config = fl_default_config(s3_bucket="test-bucket")
    assert config.s3_bucket == "test-bucket"
