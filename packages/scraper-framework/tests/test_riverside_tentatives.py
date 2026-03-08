"""Tests for Riverside County tentative rulings scraper.

Covers:
  - hearing_date extraction
  - multi-ruling PDF splitting (issue #295)
  - per-ruling field extraction: case number, case title, motion type, outcome

Fixtures captured from live site 2026-03-02:
  riv_page.html            — index page with 17 PDF links
  riv_ps1.pdf              — Dept PS1, Judge Arthur Hester III (4 pages, 4 rulings)
  riv_hall_of_justice.pdf   — Dept 260, no rulings placeholder (1 page)
  riv_murrieta.pdf         — Dept M205, Judge Belinda Handy, no rulings (1 page)
  riv_moreno_valley.pdf    — Dept MV1, Judge David E. Gregory (2 pages, 3 rulings)
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
    _extract_case_title_from_ruling,
    _extract_motion_type,
    _extract_outcome,
    _riv_hearing_date_from_text,
    _split_rulings,
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


# ---------------------------------------------------------------------------
# Multi-ruling PDF splitting — _split_rulings (issue #295)
# ---------------------------------------------------------------------------


def test_split_rulings_ps1_count() -> None:
    """PS1 fixture (4 pages) contains 4 distinct rulings."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) == 4


def test_split_rulings_moreno_valley_count() -> None:
    """MV1 fixture (2 pages) contains 3 distinct rulings."""
    text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) == 3


def test_split_rulings_no_rulings_placeholder() -> None:
    """Murrieta 'No Tentative Rulings' PDF produces 0 rulings."""
    text = _extract_pdf_text(_load_bytes("riv_murrieta.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) == 0


def test_split_rulings_hall_of_justice_no_rulings() -> None:
    """Hall of Justice placeholder PDF produces 0 rulings."""
    text = _extract_pdf_text(_load_bytes("riv_hall_of_justice.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) == 0


# ---------------------------------------------------------------------------
# Per-ruling case numbers from PS1 fixture
# ---------------------------------------------------------------------------


def test_split_rulings_ps1_case_numbers() -> None:
    """Each PS1 ruling has a distinct case number."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)
    case_numbers = [r.case_number for r in rulings]
    assert case_numbers == [
        "CVPS2306157",
        "CVPS2306202",
        "CVPS2403119",
        "CVPS2404518",
    ]


def test_split_rulings_moreno_valley_case_numbers() -> None:
    """Each MV1 ruling has a distinct case number."""
    text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    rulings = _split_rulings(text)
    case_numbers = [r.case_number for r in rulings]
    assert case_numbers == [
        "CVMV2507098",
        "CVMV2510261",
        "CVMV2510403",
    ]


# ---------------------------------------------------------------------------
# Per-ruling case titles from PS1 fixture
# ---------------------------------------------------------------------------


def test_split_rulings_ps1_case_titles() -> None:
    """PS1 rulings with clear 'X vs Y' on the case number line get titles."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)
    # First 3 have clear single-line "PLAINTIFF vs DEFENDANT" format
    assert rulings[0].case_title == "Yeldell v. Henss"
    assert rulings[1].case_title == "Crump v. Irwin"
    assert rulings[2].case_title == "Garcia v. Fca Us, Llc"
    # #4 has a multi-line/columnar layout — title is None (acceptable)
    assert rulings[3].case_title is None


# ---------------------------------------------------------------------------
# Per-ruling motion types
# ---------------------------------------------------------------------------


def test_split_rulings_ps1_motion_types() -> None:
    """PS1 rulings have extractable motion types."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)
    assert rulings[0].motion_type == "Demurrer"
    assert rulings[1].motion_type == "Terminating Sanctions"
    assert rulings[2].motion_type == "Motion to Compel"
    assert rulings[3].motion_type == "Production of Documents"


def test_split_rulings_moreno_valley_motion_types() -> None:
    """MV1 rulings have extractable motion types."""
    text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    rulings = _split_rulings(text)
    assert rulings[0].motion_type == "Deem Admissions Admitted"
    assert rulings[1].motion_type == "Judgment on the Pleadings"
    assert rulings[2].motion_type == "Judgment on the Pleadings"


# ---------------------------------------------------------------------------
# Per-ruling outcomes
# ---------------------------------------------------------------------------


def test_split_rulings_ps1_outcomes() -> None:
    """PS1 rulings have extractable outcomes."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)
    assert rulings[0].outcome == "Overruled"
    assert rulings[1].outcome == "No Tentative Ruling"
    assert rulings[2].outcome == "Continued"
    assert rulings[3].outcome == "Denied"


def test_split_rulings_moreno_valley_outcomes() -> None:
    """MV1 rulings are all Granted."""
    text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    rulings = _split_rulings(text)
    assert all(r.outcome == "Granted" for r in rulings)


# ---------------------------------------------------------------------------
# Each ruling gets its own ruling text
# ---------------------------------------------------------------------------


def test_split_rulings_ps1_distinct_text() -> None:
    """Each PS1 ruling has its own ruling text, not the full PDF."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)

    # No two rulings share the same text
    texts = [r.ruling_text for r in rulings]
    assert len(set(texts)) == len(texts)

    # Each ruling text contains its own case number
    for ruling in rulings:
        assert ruling.case_number is not None
        assert ruling.case_number in ruling.ruling_text

    # Ruling text does NOT contain other rulings' case numbers
    for i, ruling in enumerate(rulings):
        for j, other in enumerate(rulings):
            if i != j:
                assert other.case_number not in ruling.ruling_text


# ---------------------------------------------------------------------------
# Ruling index
# ---------------------------------------------------------------------------


def test_split_rulings_ps1_indices() -> None:
    """Ruling indices match the numbered entries in the PDF."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)
    assert [r.ruling_index for r in rulings] == [1, 2, 3, 4]


def test_split_rulings_moreno_valley_indices() -> None:
    text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    rulings = _split_rulings(text)
    assert [r.ruling_index for r in rulings] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Unit tests for extraction helpers
# ---------------------------------------------------------------------------


def test_extract_outcome_granted() -> None:
    text = "Tentative Ruling: Granted.\nAll Requests for Admissions are deemed admitted."
    assert _extract_outcome(text) == "Granted"


def test_extract_outcome_denied() -> None:
    text = "Motion for Production of Documents Regarding Defendant's Financial Condition DENIED"
    assert _extract_outcome(text) == "Denied"


def test_extract_outcome_overruled() -> None:
    text = "Demurrer is OVERRULED, Defendant to file an answer within 10 days."
    assert _extract_outcome(text) == "Overruled"


def test_extract_outcome_continued() -> None:
    text = "Tentative Ruling: No tentative ruling, matter is continued to 3.23.26."
    assert _extract_outcome(text) == "Continued"


def test_extract_outcome_no_tentative_ruling() -> None:
    text = "Tentative Ruling: No tentative ruling, a hearing will be conducted."
    assert _extract_outcome(text) == "No Tentative Ruling"


def test_extract_motion_type_demurrer() -> None:
    text = "Hearing re: Demurrer on 1st Amended\nCVPS2306157 YELDELL vs HENSS"
    assert _extract_motion_type(text) == "Demurrer"


def test_extract_motion_type_motion_to_compel() -> None:
    text = "Motion to Compel Plaintiff's Responses\nto Request for Production"
    assert _extract_motion_type(text) == "Motion to Compel"


def test_extract_case_title_simple() -> None:
    text = "CVPS2306157 YELDELL vs HENSS Complaint of LACHON YELDELL"
    assert _extract_case_title_from_ruling(text) == "Yeldell v. Henss"


def test_extract_case_title_with_llc() -> None:
    text = "CVPS2403119 GARCIA vs FCA US, LLC Requests for Monetary Sanctions"
    assert _extract_case_title_from_ruling(text) == "Garcia v. Fca Us, Llc"


def test_extract_case_title_no_vs() -> None:
    """When there's no 'vs' pattern, returns None."""
    text = "CVPS2404518 LEGACY, INC., A CALIFORNIA CORPORATION"
    assert _extract_case_title_from_ruling(text) is None


# ---------------------------------------------------------------------------
# Full integration: scraper splits multi-ruling PDFs
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_run_splits_multi_ruling_pdfs() -> None:
    """Multi-ruling PDFs are split into individual CapturedDocument records."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")  # 4 rulings

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # 17 PDF links on the index page, each mocked with the 4-ruling PS1 PDF
    assert len(docs) == 17 * 4

    # All split docs have pre_split flag
    assert all(d.extra.get("pre_split") for d in docs)

    # Each doc has a distinct case number (within its batch of 4)
    batch = docs[:4]
    case_nums = [d.case_number for d in batch]
    assert case_nums == ["CVPS2306157", "CVPS2306202", "CVPS2403119", "CVPS2404518"]


@respx.mock
def test_riv_run_split_docs_inherit_judge_and_department() -> None:
    """Split documents inherit judge name and department from the parent PDF."""
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

    # Check one batch (first 4 docs from the PS1-mocked PDF)
    # The first PDF on the index page is PS1
    ps1_batch = [d for d in parsed if d.department == "PS1"]
    assert len(ps1_batch) > 0
    for doc in ps1_batch:
        assert doc.judge_name == "Arthur Hester III"
        assert doc.department == "PS1"
        assert doc.courthouse == "Palm Springs Courthouse"


@respx.mock
def test_riv_run_split_docs_have_hearing_date() -> None:
    """Split documents get the hearing date from the PDF header."""
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

    # All split docs should have the hearing date from the PS1 header
    assert all(d.hearing_date == datetime(2026, 3, 2) for d in parsed)


@respx.mock
def test_riv_run_split_docs_have_individual_ruling_text() -> None:
    """Each split document has ruling text for only its own case."""
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

    # Take a batch from PS1 department
    ps1_docs = [d for d in parsed if d.department == "PS1"][:4]
    assert len(ps1_docs) == 4

    # Each doc's ruling text contains its own case number
    for doc in ps1_docs:
        assert doc.case_number is not None
        assert doc.ruling_text is not None
        assert doc.case_number in doc.ruling_text

    # No doc's ruling text contains another doc's case number
    for i, doc in enumerate(ps1_docs):
        for j, other in enumerate(ps1_docs):
            if i != j and other.case_number is not None:
                assert other.case_number not in doc.ruling_text


@respx.mock
def test_riv_run_no_split_for_single_ruling_pdf() -> None:
    """PDFs with no numbered rulings are kept as-is (not split)."""
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
    # 17 PDFs, each has 0 numbered rulings -> kept as original single doc
    assert len(docs) == 17
    # No pre_split flag
    assert all(not d.extra.get("pre_split") for d in docs)
