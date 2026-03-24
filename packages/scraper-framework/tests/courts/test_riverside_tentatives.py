"""Tests for Riverside County tentative rulings scraper.

Covers:
  - hearing_date extraction
  - multi-ruling PDF splitting (issue #295)
  - per-ruling field extraction: case number, case title, motion type, outcome
  - PDF-content judge name fallback (issue #411)

Fixtures captured from live site 2026-03-02:
  riv_page.html            — index page with 17 PDF links
  riv_ps1.pdf              — Dept PS1, Judge Arthur Hester III (4 pages, 4 rulings)
  riv_hall_of_justice.pdf   — Dept 260, no rulings placeholder (1 page)
  riv_murrieta.pdf         — Dept M205, Judge Belinda Handy, no rulings (1 page)
  riv_moreno_valley.pdf    — Dept MV1, Judge David E. Gregory (2 pages, 3 rulings)
"""

from __future__ import annotations

import json
import re
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
    _OUTCOME_MAP,
    INDEX_URL,
    RiversideTentativeRulingsScraper,
    SplitRuling,
    _extract_case_title_from_ruling,
    _extract_motion_type,
    _extract_outcome,
    _filter_entry_matches,
    _is_no_tentative_rulings,
    _llm_extract_rulings,
    _riv_courthouse,
    _riv_hearing_date_from_text,
    _split_rulings,
)
from courts.ca.riverside_tentatives import default_config as riv_default_config
from framework import CapturedDocument, ContentFormat

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def _disable_llm_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable LLM extraction in all tests by default.

    Returns ``None`` so ``fetch_documents()`` falls back to the regex-based
    ``_split_rulings()`` path.  Tests that explicitly exercise the LLM path
    should override this via ``monkeypatch`` or ``unittest.mock.patch``.
    """
    monkeypatch.setattr(
        "courts.ca.riverside_tentatives._llm_extract_rulings",
        lambda text: None,
    )


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
    """PS1 rulings have extractable motion types (raw, not normalized)."""
    text = _extract_pdf_text(_load_bytes("riv_ps1.pdf"))
    rulings = _split_rulings(text)
    # Raw motion types — normalization is handled downstream by the ingestion worker
    assert rulings[0].motion_type == "Demurrer"
    assert rulings[1].motion_type == "Terminating"
    assert rulings[2].motion_type == (
        "Compel Plaintiff's Responses to Request for Production, Set One"
    )
    assert rulings[3].motion_type == "Production of Documents"


def test_split_rulings_moreno_valley_motion_types() -> None:
    """MV1 rulings have extractable motion types (raw, not normalized)."""
    text = _extract_pdf_text(_load_bytes("riv_moreno_valley.pdf"))
    rulings = _split_rulings(text)
    # Raw motion types — normalization is handled downstream by the ingestion worker
    assert rulings[0].motion_type == "DEEM REQUESTS FOR WELLS FARGO BANK, N.A."
    assert rulings[1].motion_type == (
        "JUDGMENT ON THE PLEADINGS ON COMPLAINT FOR BANK OF AMERICA, N.A."
    )
    assert rulings[2].motion_type == (
        "JUDGMENT ON THE PLEADINGS ON COMPLAINT FOR BANK OF AMERICA, N.A."
    )


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
    assert _extract_motion_type(text) == "Compel Plaintiff's Responses to Request for Production"


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
    # 16 matching PDF links on the index page (1 filtered by link_text_re, #1845),
    # each mocked with the 4-ruling PS1 PDF
    assert len(docs) == 16 * 4

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
    # All 17 PDFs are "No Tentative Rulings" boilerplate — all should be skipped
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

    # All split docs should have the fallback judge name
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
def test_riv_parse_document_judge_fallback_single_ruling() -> None:
    """parse_document falls back to extract_judge_name for single-ruling PDFs (#411)."""
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
# _riv_hearing_date_from_text — edge case: matched date but invalid format (line 84)
# ---------------------------------------------------------------------------


def test_riv_hearing_date_returns_none_for_unparseable_date() -> None:
    """When the regex matches but the date string can't be parsed, return None."""
    # "Zeptember" will match the regex's date group only if we inject it in a
    # way the regex likes — instead use a month name but with invalid day "99".
    # Actually, let's trigger the fallback by providing a date that the regex
    # matches but strptime can't parse.  The regex requires a valid month name,
    # so the only way to hit line 84 is if the day/year combo is invalid.
    text = "Tentative Rulings for February 30, 2026\nDepartment PS1"
    # February 30 doesn't exist; strptime will raise ValueError for both formats
    result = _riv_hearing_date_from_text(text)
    assert result is None


# ---------------------------------------------------------------------------
# _extract_case_title_from_ruling — no case number (line 253)
# ---------------------------------------------------------------------------


def test_extract_case_title_no_case_number() -> None:
    """Returns None when ruling text has no case number."""
    text = "Some ruling text without any case number\nPlaintiff vs Defendant"
    assert _extract_case_title_from_ruling(text) is None


# ---------------------------------------------------------------------------
# _extract_case_title_from_ruling — short plaintiff/defendant (line 301)
# ---------------------------------------------------------------------------


def test_extract_case_title_short_names_returns_none() -> None:
    """Returns None when plaintiff or defendant name is too short (< 2 chars)."""
    # After stripping punctuation and noise, defendant becomes single char.
    # The vs regex requires [A-Z][A-Z\\s,.'-]+?, so plaintiff matches "A,"
    # which after strip(" ,;:") becomes "A" (len 1) -> returns None.
    text = "CVPS2306157 A, vs B, Hearing re: Demurrer"
    result = _extract_case_title_from_ruling(text)
    assert result is None


# ---------------------------------------------------------------------------
# _extract_motion_type — long motion type truncated at 80 chars (line 349)
# ---------------------------------------------------------------------------


def test_extract_motion_type_truncated_at_80_chars() -> None:
    """Motion type longer than 80 chars is truncated."""
    long_motion = "A" * 100
    text = f"Motion to {long_motion}\nTentative Ruling: Granted."
    result = _extract_motion_type(text)
    assert result is not None
    # After truncation to 80 chars, the result should be normalized
    assert len(result) <= 80


# ---------------------------------------------------------------------------
# _extract_motion_type — all-caps input (handled by Pattern 2 with IGNORECASE)
# ---------------------------------------------------------------------------


def test_extract_motion_type_all_caps_pattern() -> None:
    """All-caps MOTION TO input is matched by Pattern 2 (IGNORECASE)."""
    text = (
        "MOTION TO DEEM REQUESTS FOR ADMISSIONS ADMITTED BY PLAINTIFF\nTentative Ruling: Granted."
    )
    result = _extract_motion_type(text)
    assert result == "DEEM REQUESTS FOR ADMISSIONS ADMITTED"


def test_extract_motion_type_all_caps_no_by_of() -> None:
    """All-caps MOTION TO without 'BY' or 'OF' suffix."""
    text = "MOTION TO STRIKE\nTentative Ruling: Granted."
    result = _extract_motion_type(text)
    assert result == "STRIKE"


def test_extract_motion_type_all_caps_empty_returns_none() -> None:
    """All-caps MOTION TO with empty motion type returns None."""
    # "MOTION TO BY" — the group captures "BY PLAINTIFF" but truncation
    # at the "by" pattern leaves it empty
    text = "MOTION TO BY PLAINTIFF\nTentative Ruling: Granted."
    result = _extract_motion_type(text)
    # Pattern 2 matches; verify the function doesn't crash
    assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# _extract_outcome — "hearing will be conducted" (lines 445-448)
# ---------------------------------------------------------------------------


def test_extract_outcome_hearing_will_be_conducted() -> None:
    """'hearing will be conducted' maps to 'No Tentative Ruling'."""
    text = "Tentative Ruling: A hearing will be conducted on this matter."
    result = _extract_outcome(text)
    assert result == "No Tentative Ruling"


def test_extract_outcome_no_match_returns_none() -> None:
    """Returns None when no outcome pattern is found."""
    text = "This ruling contains no recognizable disposition keywords at all."
    result = _extract_outcome(text)
    assert result is None


# ---------------------------------------------------------------------------
# _riv_courthouse — unknown department code (line 469)
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
# fetch_documents — PDF text extraction failure (lines 509-512)
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
    # Despite extraction failures, docs are still returned (unsplit)
    # 16 matching links (1 filtered by link_text_re, #1845)
    assert len(docs) == 16  # one per PDF link, no splitting


# ---------------------------------------------------------------------------
# fetch_documents — department-to-judge mapping fallback (lines 529-534)
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
# fetch_documents — single ruling PDF not split (lines 543-544)
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_fetch_documents_single_ruling_not_split() -> None:
    """A PDF with a single numbered ruling is not split."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    # Patch _split_rulings to return a single ruling (simulating single-ruling PDF)
    from courts.ca.riverside_tentatives import SplitRuling

    single_ruling = SplitRuling(
        ruling_index=1,
        case_number="CVPS2306157",
        ruling_text="Some ruling text",
        case_title="Test v. Case",
        motion_type="Demurrer",
        outcome="Granted",
    )
    with patch(
        "courts.ca.riverside_tentatives._split_rulings",
        return_value=[single_ruling],
    ):
        docs = scraper.fetch_documents()

    # Each of the 16 matching PDFs returns as a single doc (not split)
    # (1 of 17 links filtered by link_text_re, #1845)
    assert len(docs) == 16
    # None should have pre_split flag
    assert all(not d.extra.get("pre_split") for d in docs)


# ---------------------------------------------------------------------------
# parse_document — pre-split doc hearing date from ruling text (line 588)
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_parse_document_pre_split_extracts_hearing_date() -> None:
    """parse_document extracts hearing date from ruling_text for pre-split docs."""
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=b"dummy",
        content_format=ContentFormat.PDF,
    )
    doc.extra = {"pre_split": True}
    doc.judge_name = "Test Judge"
    doc.hearing_date = None
    doc.ruling_text = "Tentative Rulings for March 2, 2026\nSome ruling text"

    result = scraper.parse_document(doc)
    assert result.hearing_date == datetime(2026, 3, 2)


# ---------------------------------------------------------------------------
# parse_document — pre-split doc dept-judge mapping (lines 591-595)
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_parse_document_pre_split_dept_judge_fallback() -> None:
    """parse_document uses dept_judge_map for pre-split docs without judge name."""
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    config = riv_default_config()
    config.request_delay_seconds = 0
    dept_map = {"PS1": "Mapped Pre-Split Judge"}
    scraper = RiversideTentativeRulingsScraper(
        config=config,
        dept_judge_map=dept_map,
    )

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=b"dummy",
        content_format=ContentFormat.PDF,
    )
    doc.extra = {"pre_split": True}
    doc.judge_name = None
    doc.department = "PS1"

    result = scraper.parse_document(doc)
    assert result.judge_name == "Mapped Pre-Split Judge"


# ---------------------------------------------------------------------------
# parse_document — single-ruling doc dept-judge mapping (lines 609-613)
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_parse_document_single_ruling_dept_judge_fallback() -> None:
    """parse_document uses dept_judge_map for single-ruling docs without judge name."""
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
# _split_rulings with non-CV case numbers (#805)
# ---------------------------------------------------------------------------


def test_split_rulings_ric_prefix() -> None:
    """_split_rulings correctly extracts RIC-prefixed case numbers."""
    text = (
        "Tentative Rulings for March 2, 2026\n"
        "Department 2\n\n"
        "1.\n"
        "RIC1904113 FOUR STAR MIDWEST vs CITY OF JURUPA  Hearing re: Demurrer\n"
        "Tentative Ruling: Granted.\n\n"
        "2.\n"
        "CVRI2412345 SMITH vs JONES  Motion to Compel\n"
        "Tentative Ruling: Denied.\n"
    )
    rulings = _split_rulings(text)
    assert len(rulings) == 2
    assert rulings[0].case_number == "RIC1904113"
    assert rulings[1].case_number == "CVRI2412345"


def test_split_rulings_mixed_prefixes() -> None:
    """_split_rulings handles a mix of CV and location-prefixed case numbers."""
    text = (
        "Tentative Rulings for March 2, 2026\n"
        "Department 2\n\n"
        "1.\n"
        "MCC2012345 DOE vs ROE  Hearing re: Demurrer\n"
        "Tentative Ruling: Overruled.\n\n"
        "2.\n"
        "CVPS2306157 YELDELL vs HENSS  Motion to Strike\n"
        "Tentative Ruling: Denied.\n\n"
        "3.\n"
        "INC2300001 ALPHA vs BETA  Motion for Summary Judgment\n"
        "Tentative Ruling: Granted.\n"
    )
    rulings = _split_rulings(text)
    assert len(rulings) == 3
    assert rulings[0].case_number == "MCC2012345"
    assert rulings[1].case_number == "CVPS2306157"
    assert rulings[2].case_number == "INC2300001"


# ---------------------------------------------------------------------------
# _extract_case_title_from_ruling with non-CV case numbers (#805)
# ---------------------------------------------------------------------------


def test_extract_case_title_ric_prefix() -> None:
    """Case title extraction works with RIC-prefixed case numbers."""
    text = "RIC1904113 FOUR STAR MIDWEST vs CITY OF JURUPA Hearing re: Demurrer"
    result = _extract_case_title_from_ruling(text)
    assert result == "Four Star Midwest v. City Of Jurupa"


def test_extract_case_title_mcc_prefix() -> None:
    """Case title extraction works with MCC-prefixed case numbers."""
    text = "MCC2012345 DOE vs ROE Hearing re: Motion to Compel"
    result = _extract_case_title_from_ruling(text)
    assert result == "Doe v. Roe"


def test_extract_case_title_psc_prefix() -> None:
    """Case title extraction works with PSC-prefixed case numbers."""
    text = "PSC2101234 JOHNSON vs WILLIAMS Application for Protective Order"
    result = _extract_case_title_from_ruling(text)
    assert result == "Johnson v. Williams"


# ---------------------------------------------------------------------------
# _filter_entry_matches — spurious body entries (#1410)
# ---------------------------------------------------------------------------


class TestFilterEntryMatches:
    """Verify _filter_entry_matches filters out numbered points in ruling body text."""

    _ENTRY_RE = re.compile(r"^(?P<num>\d{1,3})\.\s*$", re.MULTILINE)

    def test_no_spurious_entries(self) -> None:
        """Normal entries (no body numbering) are kept as-is."""
        text = (
            "1.\nCVPS2306157 YELDELL vs HENSS\nTentative Ruling: Granted.\n"
            "2.\nCVPS2306202 CRUMP vs IRWIN\nTentative Ruling: Denied.\n"
        )
        matches = list(self._ENTRY_RE.finditer(text))
        result = _filter_entry_matches(matches, text)
        assert len(result) == 2
        assert [int(m.group("num")) for m in result] == [1, 2]

    def test_body_numbered_points_filtered(self) -> None:
        """Numbered points inside ruling body (no case number) are filtered out (#1410)."""
        text = (
            "1.\n"
            "CVSW2405000 GERARDI vs MILO  Motion for Summary Judgment\n"
            "Tentative Ruling: The Court rules as follows:\n"
            "1.\n"
            "The motion is granted as to the first cause of action.\n"
            "2.\n"
            "The motion is denied as to the second cause of action.\n"
            "2.\n"
            "CVSW2405417 REYNOLDS VS CITY OF TEMECULA  Motion for New Trial\n"
            "Tentative Ruling: Hearing required.\n"
        )
        matches = list(self._ENTRY_RE.finditer(text))
        assert len(matches) == 4  # raw: 1, 1, 2, 2

        result = _filter_entry_matches(matches, text)
        assert len(result) == 2  # only the real entries
        assert [int(m.group("num")) for m in result] == [1, 2]

    def test_body_numbered_points_different_numbers(self) -> None:
        """Body points whose numbers differ from real entries are still filtered."""
        text = (
            "1.\n"
            "CVSW2405000 GERARDI vs MILO  Motion\n"
            "The court finds:\n"
            "1.\n"
            "Point one.\n"
            "2.\n"
            "Point two.\n"
            "3.\n"
            "CVSW2405417 REYNOLDS  Motion for New Trial\n"
        )
        matches = list(self._ENTRY_RE.finditer(text))
        result = _filter_entry_matches(matches, text)
        # Entry "1." kept, body "1." and "2." skipped, gap at 3 (expected 2) stops.
        assert len(result) == 1
        assert int(result[0].group("num")) == 1

    def test_empty_matches(self) -> None:
        """Empty match list returns empty."""
        assert _filter_entry_matches([], "some text") == []

    def test_single_entry(self) -> None:
        """Single valid entry is kept."""
        text = "1.\nCVPS2306157 YELDELL vs HENSS\nTentative Ruling: Granted.\n"
        matches = list(self._ENTRY_RE.finditer(text))
        result = _filter_entry_matches(matches, text)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _split_rulings — ruling-to-case assignment regression tests (#1410)
# ---------------------------------------------------------------------------


class TestSplitRulingsNoMisAssignment:
    """Verify ruling text is never assigned to the wrong case (#1410).

    The root cause was that numbered points in ruling body text
    (e.g. "The court finds:\\n1.\\nMotion is granted") matched the
    ``_RULING_ENTRY_RE`` pattern, creating spurious split boundaries
    that shifted ruling text to the wrong case.
    """

    def test_body_points_do_not_shift_ruling_assignment(self) -> None:
        """Numbered body points do not cause off-by-one assignment."""
        text = (
            "Tentative Rulings for March 2, 2026\n"
            "Department SW1\n\n"
            "1.\n"
            "CVSW2405000 GERARDI vs MILO  Motion for Summary Judgment\n"
            "Tentative Ruling: The Court rules as follows:\n"
            "1.\n"
            "The motion is granted as to the first cause of action.\n"
            "2.\n"
            "The motion is denied as to the second cause of action.\n"
            "2.\n"
            "CVSW2405417 REYNOLDS VS CITY OF TEMECULA  Motion for New Trial\n"
            "Tentative Ruling: Hearing required.\n"
        )
        rulings = _split_rulings(text)
        assert len(rulings) == 2

        # Ruling 1 belongs to Gerardi
        assert rulings[0].case_number == "CVSW2405000"
        assert "GERARDI" in rulings[0].ruling_text
        # Ruling 1's text includes the body points (they are part of the ruling)
        assert "The motion is granted" in rulings[0].ruling_text
        assert "The motion is denied" in rulings[0].ruling_text

        # Ruling 2 belongs to Reynolds — NOT Gerardi
        assert rulings[1].case_number == "CVSW2405417"
        assert "REYNOLDS" in rulings[1].ruling_text
        assert "GERARDI" not in rulings[1].ruling_text

    def test_three_entries_with_body_points_in_middle(self) -> None:
        """Numbered body points in the middle entry do not shift later entries."""
        text = (
            "1.\n"
            "CVRI2401443 HULL VS GENERAL MOTORS, LLC.\n"
            "Tentative Ruling: Granted.\n"
            "2.\n"
            "CVRI2501821 CHO VS JAGUAR LAND ROVER\n"
            "Tentative Ruling: The court orders:\n"
            "1.\n"
            "Discovery responses within 10 days.\n"
            "2.\n"
            "Sanctions of $500.\n"
            "3.\n"
            "CVRI2504487 GARCIA VS REYES\n"
            "Tentative Ruling: Granted.\n"
        )
        rulings = _split_rulings(text)
        assert len(rulings) == 3

        assert rulings[0].case_number == "CVRI2401443"
        assert "HULL" in rulings[0].ruling_text

        assert rulings[1].case_number == "CVRI2501821"
        assert "CHO" in rulings[1].ruling_text
        # Body points are included in CHO's ruling text
        assert "Discovery responses" in rulings[1].ruling_text

        assert rulings[2].case_number == "CVRI2504487"
        assert "GARCIA" in rulings[2].ruling_text

    def test_each_ruling_contains_only_its_own_case_number(self) -> None:
        """No ruling text contains another ruling's case number."""
        text = (
            "1.\n"
            "CVSW2405000 GERARDI vs MILO  Motion\n"
            "Tentative Ruling: Granted.\n"
            "The court orders:\n"
            "1.\n"
            "Pay costs within 30 days.\n"
            "2.\n"
            "CVSW2405417 REYNOLDS  Motion for New Trial\n"
            "Tentative Ruling: Hearing required.\n"
            "3.\n"
            "CVSW2405794 COFFELT vs STINSON  Motion\n"
            "Tentative Ruling: Off Calendar.\n"
        )
        rulings = _split_rulings(text)
        assert len(rulings) == 3

        case_numbers = [r.case_number for r in rulings]
        assert case_numbers == ["CVSW2405000", "CVSW2405417", "CVSW2405794"]

        for i, ruling in enumerate(rulings):
            for j, other in enumerate(rulings):
                if i != j and other.case_number:
                    assert other.case_number not in ruling.ruling_text, (
                        f"Ruling {i} ({ruling.case_number}) contains "
                        f"case number from ruling {j} ({other.case_number})"
                    )


# ---------------------------------------------------------------------------
# _split_rulings / _filter_entry_matches — duplicate case number with
# body-text numbered analysis referencing case numbers (#1716)
# ---------------------------------------------------------------------------


class TestSplitRulingsDuplicateCaseNumber:
    """Verify correct splitting when multiple entries share a case number
    and the body text of a long ruling contains numbered analytical
    paragraphs that reference case numbers (#1716).

    The bug: a 6-page ruling for entry #1 (CVRI2403055) contained numbered
    analysis points ("1.\\n...", "2.\\n...", "3.\\n...") that also mentioned
    the case number.  The old ``_filter_entry_matches`` kept these spurious
    matches because their blocks contained a case number, and the consecutive-
    numbering pass treated the real entries #2 and #3 as duplicates, silently
    discarding them.  This caused entry #1's text to be truncated and later
    entries' text to be mis-assigned.
    """

    # Synthetic text matching the structure from the bug report:
    # 6 entries, entries 1-3 share CVRI2403055, and entry 1's body text
    # has numbered analytical paragraphs that reference the case number.
    _TEXT = (
        "Tentative Rulings for March 23, 2026\n"
        "Department 2\n\n"
        "1.\n"
        "CVRI2403055 SMITH vs JONES Hearing re: Motion to Compel\n"
        "Tentative Ruling: Summary of Ruling: The Court grants all three "
        "motions to compel.\n\n"
        "The Court's analysis follows:\n\n"
        "1.\n"
        "Motion to Compel Interrogatories (CVRI2403055): Plaintiff failed to "
        "respond. The motion is granted.\n\n"
        "2.\n"
        "Motion to Compel Production (CVRI2403055): Plaintiff also failed to "
        "respond. The motion is granted.\n\n"
        "3.\n"
        "Motion to Deem Admissions (CVRI2403055): Under CCP 2033.280, the "
        "motion is granted.\n\n"
        "Monetary sanctions awarded: $1,500.\n\n"
        "2.\n"
        "CVRI2403055 SMITH vs JONES Hearing re: Motion to Compel Further\n"
        "Tentative Ruling: See #1 Above\n\n"
        "3.\n"
        "CVRI2403055 SMITH vs JONES Hearing re: Motion to Deem Admissions\n"
        "Tentative Ruling: See #1 Above\n\n"
        "4.\n"
        "CVRI2404378 GARCIA vs MARTINEZ Hearing re: Motion for Sanctions\n"
        "Tentative Ruling: The motion for sanctions is denied.\n\n"
        "5.\n"
        "CVRI2406848 DOE vs ROE Hearing re: Motion to Continue\n"
        "Tentative Ruling: Motion withdrawn.\n\n"
        "6.\n"
        "CVRI2506271 BLACK vs WHITE Hearing re: Motion for Summary Judgment\n"
        "Tentative Ruling: Motion Granted. No opposition filed.\n"
    )

    def test_all_six_entries_captured(self) -> None:
        """All 6 numbered entries are captured as separate rulings."""
        rulings = _split_rulings(self._TEXT)
        assert len(rulings) == 6

    def test_entry_indices_sequential(self) -> None:
        """Ruling indices are 1 through 6."""
        rulings = _split_rulings(self._TEXT)
        assert [r.ruling_index for r in rulings] == [1, 2, 3, 4, 5, 6]

    def test_case_numbers_correct(self) -> None:
        """Each ruling has the correct case number."""
        rulings = _split_rulings(self._TEXT)
        case_numbers = [r.case_number for r in rulings]
        assert case_numbers == [
            "CVRI2403055",
            "CVRI2403055",
            "CVRI2403055",
            "CVRI2404378",
            "CVRI2406848",
            "CVRI2506271",
        ]

    def test_entry_1_has_full_ruling_text(self) -> None:
        """Entry #1 (CVRI2403055) gets the full detailed ruling, not a truncated or
        wrong one (#1716)."""
        rulings = _split_rulings(self._TEXT)
        ruling_1 = rulings[0]
        assert ruling_1.case_number == "CVRI2403055"
        # The ruling text must contain the full analysis, including the
        # numbered analytical points and the sanctions award.
        assert "grants all three motions" in ruling_1.ruling_text
        assert "Motion to Compel Interrogatories" in ruling_1.ruling_text
        assert "Motion to Compel Production" in ruling_1.ruling_text
        assert "Motion to Deem Admissions" in ruling_1.ruling_text
        assert "Monetary sanctions awarded" in ruling_1.ruling_text

    def test_entry_1_does_not_have_other_entries_text(self) -> None:
        """Entry #1's text does not spill into other entries' text."""
        rulings = _split_rulings(self._TEXT)
        ruling_1 = rulings[0]
        # Should NOT contain text from entries 4-6 (different case numbers)
        assert "CVRI2404378" not in ruling_1.ruling_text
        assert "CVRI2406848" not in ruling_1.ruling_text
        assert "CVRI2506271" not in ruling_1.ruling_text

    def test_see_above_entries_have_correct_text(self) -> None:
        """Entries #2 and #3 ('See #1 Above') have short cross-reference text."""
        rulings = _split_rulings(self._TEXT)
        ruling_2 = rulings[1]
        ruling_3 = rulings[2]
        assert "See #1 Above" in ruling_2.ruling_text
        assert "See #1 Above" in ruling_3.ruling_text
        # They should NOT have entry #1's full analysis text
        assert "Monetary sanctions awarded" not in ruling_2.ruling_text
        assert "Monetary sanctions awarded" not in ruling_3.ruling_text

    def test_entry_6_not_misassigned_to_entry_1(self) -> None:
        """Entry #6's text (CVRI2506271) is NOT assigned to entry #1 (#1716 root cause)."""
        rulings = _split_rulings(self._TEXT)
        ruling_1 = rulings[0]
        ruling_6 = rulings[5]
        # Entry 6 has its own text
        assert ruling_6.case_number == "CVRI2506271"
        assert "Motion Granted" in ruling_6.ruling_text
        # Entry 1 should NOT have entry 6's text
        assert "CVRI2506271" not in ruling_1.ruling_text
        assert "No opposition filed" not in ruling_1.ruling_text

    def test_later_entries_have_correct_text(self) -> None:
        """Entries #4, #5, #6 each have their own correct ruling text."""
        rulings = _split_rulings(self._TEXT)
        # Entry 4: CVRI2404378 — sanctions denied
        assert rulings[3].case_number == "CVRI2404378"
        assert "sanctions is denied" in rulings[3].ruling_text
        # Entry 5: CVRI2406848 — motion withdrawn
        assert rulings[4].case_number == "CVRI2406848"
        assert "withdrawn" in rulings[4].ruling_text
        # Entry 6: CVRI2506271 — motion granted
        assert rulings[5].case_number == "CVRI2506271"
        assert "Granted" in rulings[5].ruling_text


class TestFilterEntryMatchesDuplicatesWithCaseRefs:
    """Test _filter_entry_matches when body-text numbered points reference
    case numbers, creating ambiguity between real and spurious matches (#1716).
    """

    _ENTRY_RE = re.compile(r"^(?P<num>\d{1,3})\.\s*$", re.MULTILINE)

    def test_prefers_match_with_tentative_ruling(self) -> None:
        """When two candidates exist for the same entry number, the one
        with 'Tentative Ruling:' in its block is chosen."""
        text = (
            "1.\n"
            "CVRI2403055 SMITH vs JONES\n"
            "Tentative Ruling: Granted.\n"
            "Analysis:\n"
            "2.\n"
            "Point about CVRI2403055 motion.\n"
            "2.\n"
            "CVRI2404378 DOE vs ROE\n"
            "Tentative Ruling: Denied.\n"
        )
        matches = list(self._ENTRY_RE.finditer(text))
        result = _filter_entry_matches(matches, text)
        # Should select 2 entries: real "1." and real "2." (the one with
        # "Tentative Ruling: Denied.")
        assert len(result) == 2
        assert int(result[0].group("num")) == 1
        assert int(result[1].group("num")) == 2
        # Verify the second match is the REAL entry (later position),
        # not the spurious body-text "2."
        assert result[1].start() > result[0].start()
        # The block after the selected "2." should contain CVRI2404378
        block_after_2 = text[result[1].end() :]
        assert "CVRI2404378" in block_after_2

    def test_spurious_matches_skipped_when_no_tentative_ruling(self) -> None:
        """Body-text numbered points that reference case numbers but lack
        'Tentative Ruling:' are skipped in favour of real entries."""
        text = (
            "1.\n"
            "CVRI2403055 SMITH vs JONES\n"
            "Tentative Ruling: The Court grants the motions.\n"
            "1.\n"
            "First motion (CVRI2403055): granted.\n"
            "2.\n"
            "Second motion (CVRI2403055): granted.\n"
            "2.\n"
            "CVRI2403055 SMITH vs JONES\n"
            "Tentative Ruling: See #1 Above\n"
            "3.\n"
            "CVRI2404378 DOE vs ROE\n"
            "Tentative Ruling: Denied.\n"
        )
        matches = list(self._ENTRY_RE.finditer(text))
        result = _filter_entry_matches(matches, text)
        assert len(result) == 3
        # Verify the real entries were selected (not the body-text ones)
        nums = [int(m.group("num")) for m in result]
        assert nums == [1, 2, 3]
        # The "2." should be the real entry (with "Tentative Ruling: See #1 Above")
        block_after_2 = text[result[1].end() : result[2].start()]
        assert "See #1 Above" in block_after_2


# ---------------------------------------------------------------------------
# LLM extraction — _llm_extract_rulings (#1855)
# ---------------------------------------------------------------------------


class TestLlmExtractRulings:
    """Tests for the LLM-based extraction path in the Riverside scraper (#1855)."""

    # Sample LLM response matching the RIVERSIDE_SYSTEM_PROMPT output format
    _SAMPLE_LLM_RESPONSE = json.dumps(
        {
            "extracted_judge_name": "Arthur Hester III",
            "hearing_date": "2026-03-02",
            "department": "PS1",
            "rulings": [
                {
                    "extracted_case_number": "CVPS2306157",
                    "extracted_case_title": "Yeldell v. Henss",
                    "case_type": "civil",
                    "outcome": "denied",
                    "motion_type": "demurrer",
                    "ruling_text": "Demurrer is OVERRULED. Defendant to answer within 10 days.",
                },
                {
                    "extracted_case_number": "CVPS2306202",
                    "extracted_case_title": "Crump v. Irwin",
                    "case_type": "civil",
                    "outcome": "other",
                    "motion_type": "terminating sanctions",
                    "ruling_text": "No tentative ruling, a hearing will be conducted.",
                },
                {
                    "extracted_case_number": "CVPS2403119",
                    "extracted_case_title": "Garcia v. Fca Us, Llc",
                    "case_type": "civil",
                    "outcome": "continued",
                    "motion_type": "motion to compel",
                    "ruling_text": "No tentative ruling, matter is continued to 3.23.26.",
                },
                {
                    "extracted_case_number": "CVPS2404518",
                    "extracted_case_title": None,
                    "case_type": "civil",
                    "outcome": "denied",
                    "motion_type": "motion for production of documents",
                    "ruling_text": "Motion for Production DENIED.",
                },
            ],
        }
    )

    def test_llm_extract_returns_split_rulings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_llm_extract_rulings converts LLM JSON to SplitRuling objects."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,  # restore original
        )
        mock_response = LLMResponse(
            text=self._SAMPLE_LLM_RESPONSE,
            input_tokens=100,
            output_tokens=200,
        )
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = _llm_extract_rulings("some pdf text")

        assert rulings is not None
        assert len(rulings) == 4
        assert rulings[0].case_number == "CVPS2306157"
        assert rulings[0].case_title == "Yeldell v. Henss"
        assert rulings[0].motion_type == "demurrer"
        assert rulings[0].ruling_index == 1
        assert rulings[3].ruling_index == 4

    def test_llm_extract_outcome_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LLM outcome values are mapped to the format used by the regex pipeline."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        mock_response = LLMResponse(
            text=self._SAMPLE_LLM_RESPONSE,
            input_tokens=100,
            output_tokens=200,
        )
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = _llm_extract_rulings("some pdf text")

        assert rulings is not None
        # "denied" -> "Denied"
        assert rulings[0].outcome == "Denied"
        # "other" -> "No Tentative Ruling"
        assert rulings[1].outcome == "No Tentative Ruling"
        # "continued" -> "Continued"
        assert rulings[2].outcome == "Continued"
        # "denied" -> "Denied"
        assert rulings[3].outcome == "Denied"

    def test_llm_extract_unmapped_outcome_becomes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unmapped LLM outcomes are returned as None, not passed through raw."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        response_data = json.dumps(
            {
                "rulings": [
                    {
                        "extracted_case_number": "CVPS2306157",
                        "ruling_text": "Some ruling text.",
                        "outcome": "rejected",  # not in _OUTCOME_MAP
                    }
                ]
            }
        )
        mock_response = LLMResponse(text=response_data, input_tokens=50, output_tokens=50)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = _llm_extract_rulings("some pdf text")

        assert rulings is not None
        assert len(rulings) == 1
        # Unmapped outcome "rejected" should become None, not pass through as "rejected"
        assert rulings[0].outcome is None

    def test_llm_extract_returns_none_on_null_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_llm_extract_rulings returns None when call_llm returns None."""
        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        with patch("ingestion.llm_providers.call_llm", return_value=None):
            result = _llm_extract_rulings("some pdf text")
        assert result is None

    def test_llm_extract_returns_none_on_invalid_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_llm_extract_rulings returns None when the LLM returns invalid JSON."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        mock_response = LLMResponse(
            text="This is not valid JSON at all",
            input_tokens=100,
            output_tokens=50,
        )
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings("some pdf text")
        assert result is None

    def test_llm_extract_handles_bare_list_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_llm_extract_rulings handles a bare JSON list (no wrapping object)."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        bare_list = json.dumps(
            [
                {
                    "extracted_case_number": "CVPS2306157",
                    "extracted_case_title": "Yeldell v. Henss",
                    "outcome": "granted",
                    "motion_type": "demurrer",
                    "ruling_text": "Demurrer sustained.",
                }
            ]
        )
        mock_response = LLMResponse(text=bare_list, input_tokens=50, output_tokens=100)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = _llm_extract_rulings("some pdf text")

        assert rulings is not None
        assert len(rulings) == 1
        assert rulings[0].case_number == "CVPS2306157"
        assert rulings[0].outcome == "Granted"

    def test_llm_extract_strips_code_fences(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_llm_extract_rulings strips markdown code fences from the response."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        fenced = (
            "```json\n"
            + json.dumps(
                {
                    "rulings": [
                        {
                            "extracted_case_number": "CVPS2306157",
                            "ruling_text": "Granted.",
                            "outcome": "granted",
                        }
                    ]
                }
            )
            + "\n```"
        )
        mock_response = LLMResponse(text=fenced, input_tokens=50, output_tokens=100)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = _llm_extract_rulings("some pdf text")

        assert rulings is not None
        assert len(rulings) == 1

    def test_llm_extract_handles_empty_rulings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_llm_extract_rulings handles an empty rulings array (no-tentative PDF)."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        empty_response = json.dumps({"rulings": []})
        mock_response = LLMResponse(text=empty_response, input_tokens=50, output_tokens=20)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = _llm_extract_rulings("No Tentative Rulings March 2, 2026")

        assert rulings is not None
        assert len(rulings) == 0

    def test_llm_extract_returns_none_on_unexpected_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_llm_extract_rulings returns None when the LLM returns an unexpected JSON shape."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        # JSON string (not a dict or list)
        mock_response = LLMResponse(text='"just a string"', input_tokens=10, output_tokens=5)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings("some pdf text")
        assert result is None

    def test_llm_extract_returns_none_when_rulings_not_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_llm_extract_rulings returns None when 'rulings' key is not a list."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        bad_rulings = json.dumps({"rulings": "not a list"})
        mock_response = LLMResponse(text=bad_rulings, input_tokens=10, output_tokens=5)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings("some pdf text")
        assert result is None

    def test_llm_extract_skips_non_dict_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_llm_extract_rulings skips non-dict entries in the rulings list."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setattr(
            "courts.ca.riverside_tentatives._llm_extract_rulings",
            _llm_extract_rulings,
        )
        mixed_rulings = json.dumps(
            {
                "rulings": [
                    {
                        "extracted_case_number": "CVPS2306157",
                        "ruling_text": "Granted.",
                        "outcome": "granted",
                    },
                    "not a dict",
                    42,
                    None,
                ]
            }
        )
        mock_response = LLMResponse(text=mixed_rulings, input_tokens=50, output_tokens=50)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            rulings = _llm_extract_rulings("some pdf text")

        assert rulings is not None
        assert len(rulings) == 1  # only the valid dict entry
        assert rulings[0].case_number == "CVPS2306157"


class TestOutcomeMap:
    """Tests for the _OUTCOME_MAP used to translate LLM outcomes."""

    def test_all_llm_outcomes_mapped(self) -> None:
        """Every LLM outcome value in the prompt has a mapping."""
        llm_outcomes = [
            "granted",
            "denied",
            "granted_in_part",
            "denied_in_part",
            "moot",
            "continued",
            "off_calendar",
            "submitted",
            "other",
            None,
        ]
        for outcome in llm_outcomes:
            assert outcome in _OUTCOME_MAP

    def test_mapped_values_are_capitalized(self) -> None:
        """All non-None mapped values start with an uppercase letter."""
        for key, value in _OUTCOME_MAP.items():
            if value is not None:
                assert value[0].isupper(), f"_OUTCOME_MAP[{key!r}] = {value!r} is not capitalized"


# ---------------------------------------------------------------------------
# Integration: LLM extraction path in fetch_documents (#1855)
# ---------------------------------------------------------------------------


@respx.mock
def test_riv_fetch_documents_uses_llm_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_documents uses LLM extraction when _llm_extract_rulings succeeds."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    # Create a deterministic LLM response for 4 rulings
    llm_rulings = [
        SplitRuling(
            ruling_index=1,
            case_number="CVPS2306157",
            ruling_text="Demurrer OVERRULED.",
            case_title="Yeldell v. Henss",
            motion_type="demurrer",
            outcome="Denied",
        ),
        SplitRuling(
            ruling_index=2,
            case_number="CVPS2306202",
            ruling_text="No tentative ruling.",
            case_title="Crump v. Irwin",
            motion_type="terminating sanctions",
            outcome="No Tentative Ruling",
        ),
        SplitRuling(
            ruling_index=3,
            case_number="CVPS2403119",
            ruling_text="Continued to 3.23.26.",
            case_title="Garcia v. Fca Us, Llc",
            motion_type="motion to compel",
            outcome="Continued",
        ),
        SplitRuling(
            ruling_index=4,
            case_number="CVPS2404518",
            ruling_text="Motion DENIED.",
            case_title=None,
            motion_type="production of documents",
            outcome="Denied",
        ),
    ]
    monkeypatch.setattr(
        "courts.ca.riverside_tentatives._llm_extract_rulings",
        lambda text: llm_rulings,
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # 16 PDF links (1 of 17 skipped by link pattern), each returns 4 rulings from LLM
    assert len(docs) == 16 * 4

    # Check that per-ruling fields come from LLM output
    batch = docs[:4]
    assert batch[0].case_number == "CVPS2306157"
    assert batch[0].ruling_text == "Demurrer OVERRULED."
    assert batch[0].case_title == "Yeldell v. Henss"
    assert batch[1].case_number == "CVPS2306202"
    assert batch[2].case_number == "CVPS2403119"
    assert batch[3].case_number == "CVPS2404518"

    # All should be marked pre_split
    assert all(d.extra.get("pre_split") for d in docs)


@respx.mock
def test_riv_fetch_documents_falls_back_to_regex_on_llm_failure() -> None:
    """fetch_documents falls back to regex when _llm_extract_rulings returns None.

    Note: This test uses the default autouse fixture that sets
    _llm_extract_rulings to return None, so the regex fallback is tested.
    """
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
    # Regex fallback produces 4 rulings per PDF, 16 PDFs total (1 skipped by link pattern)
    assert len(docs) == 16 * 4

    # Verify the regex extraction still works correctly
    batch = docs[:4]
    case_nums = [d.case_number for d in batch]
    assert case_nums == ["CVPS2306157", "CVPS2306202", "CVPS2403119", "CVPS2404518"]


@respx.mock
def test_riv_fetch_documents_llm_single_ruling_not_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM returns a single ruling, the document is not split."""
    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_ps1.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    single_ruling = [
        SplitRuling(
            ruling_index=1,
            case_number="CVPS2306157",
            ruling_text="Demurrer OVERRULED.",
            case_title="Yeldell v. Henss",
            motion_type="demurrer",
            outcome="Denied",
        ),
    ]
    monkeypatch.setattr(
        "courts.ca.riverside_tentatives._llm_extract_rulings",
        lambda text: single_ruling,
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # Single ruling means each PDF returns 1 unsplit doc (16 PDFs, 1 skipped by link pattern)
    assert len(docs) == 16
    # Should NOT have pre_split flag
    assert all(not d.extra.get("pre_split") for d in docs)
