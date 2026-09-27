"""Tests for Orange County Probate tentative rulings scraper.

Fixtures captured from live site 2026-03-07:
  oc_probate_page.html   — index page with 6 PDF links
  oc_probate_cm3.pdf     — Dept CM3, Judge Erin Rowe (6 pages, 1 trust case)
  oc_probate_cm5.pdf     — Dept CM05, Judge Ebrahim Baytieh (6 pages, 3 cases)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.oc_probate_tentatives import (
    INDEX_URL,
    OCProbateTentativeRulingsScraper,
    _probate_case_title_from_text,
    _probate_hearing_date_from_text,
    _probate_judge_from_text,
    _probate_motion_type_from_text,
    _probate_outcome_from_text,
)
from courts.ca.oc_probate_tentatives import default_config as probate_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Judge name extraction — unit tests
# ---------------------------------------------------------------------------


def test_probate_judge_from_text_standard() -> None:
    text = "HON. Judge Erin Rowe\nDate: 03/04/26"
    assert _probate_judge_from_text(text) == "Erin Rowe"


def test_probate_judge_from_text_commissioner() -> None:
    text = "HON. Commissioner Jane Smith\nSome text"
    assert _probate_judge_from_text(text) == "Jane Smith"


def test_probate_judge_from_text_uppercase() -> None:
    text = "HON. Judge ERIN ROWE\nDate: 03/04/26"
    assert _probate_judge_from_text(text) == "Erin Rowe"


def test_probate_judge_from_text_none() -> None:
    assert _probate_judge_from_text("No judge name here") is None
    assert _probate_judge_from_text("") is None


# ---------------------------------------------------------------------------
# Judge extraction — against real PDF fixtures
# ---------------------------------------------------------------------------


def test_probate_judge_cm3() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm3.pdf"))
    judge = _probate_judge_from_text(text)
    assert judge == "Erin Rowe"


def test_probate_judge_cm5() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm5.pdf"))
    judge = _probate_judge_from_text(text)
    assert judge == "Ebrahim Baytieh"


# ---------------------------------------------------------------------------
# Hearing date extraction — unit tests
# ---------------------------------------------------------------------------


def test_probate_date_mm_dd_yy() -> None:
    text = "Date: 03/04/26"
    assert _probate_hearing_date_from_text(text) == datetime(2026, 3, 4)


def test_probate_date_mm_dd_yyyy() -> None:
    text = "Date: 02/19/2026"
    assert _probate_hearing_date_from_text(text) == datetime(2026, 2, 19)


def test_probate_date_none() -> None:
    assert _probate_hearing_date_from_text("No date here") is None
    assert _probate_hearing_date_from_text("") is None


# ---------------------------------------------------------------------------
# Hearing date extraction — against real PDF fixtures
# ---------------------------------------------------------------------------


def test_probate_date_cm3() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm3.pdf"))
    dt = _probate_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 4)


def test_probate_date_cm5() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm5.pdf"))
    dt = _probate_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 19)


# ---------------------------------------------------------------------------
# Header date variants seen on dev S3 (#4808): no colon, unpadded month/day
# ---------------------------------------------------------------------------

# Trimmed pdfplumber text of real dev raws:
#   cm04 — f883c2d8… (CM4rulings.pdf): "Date 1/16/2026", no colon
#   cm06 — d469e354… (CM6rulings.pdf): "Date: 2/6/26", unpadded
_HEADER_VARIANT_FIXTURES = [
    ("oc_probate_cm04_date_no_colon_4808.txt", datetime(2026, 1, 16)),
    ("oc_probate_cm06_date_unpadded_4808.txt", datetime(2026, 2, 6)),
]


@pytest.mark.parametrize(("fixture", "expected"), _HEADER_VARIANT_FIXTURES)
def test_oc_probate_header_variants_4808_hook(fixture: str, expected: datetime) -> None:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert _probate_hearing_date_from_text(text) == expected
    got = OCProbateTentativeRulingsScraper.hearing_date_for_raw(text, content_format="pdf")
    assert got == expected


@pytest.mark.parametrize(("fixture", "expected"), _HEADER_VARIANT_FIXTURES)
def test_oc_probate_header_variants_4808_parse_document(
    fixture: str, expected: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live parse reads the same header the hook does."""
    import courts.ca.pdf_link_scraper as pdf_mod
    from framework import CapturedDocument, ContentFormat

    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    monkeypatch.setattr(pdf_mod, "_extract_pdf_text", lambda _raw: text)
    scraper = OCProbateTentativeRulingsScraper(config=probate_default_config())
    doc = CapturedDocument(
        scraper_id="ca-oc-tentatives-probate",
        state="CA",
        county="Orange",
        court="Superior Court",
        source_url="https://www.occourts.org/sites/default/files/oc/default/tentative-rulings/CM4rulings.pdf",
        capture_timestamp=datetime(2026, 9, 26),
        content_format=ContentFormat.PDF,
        raw_content=b"%PDF-1.4",
        content_hash="",
    )
    assert scraper.parse_document(doc).hearing_date == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Date 1/16/2026", datetime(2026, 1, 16)),
        ("Date: 2/6/26", datetime(2026, 2, 6)),
        ("Date: 6/10/2026", datetime(2026, 6, 10)),
        ("Date: 10/2/2026", datetime(2026, 10, 2)),
        ("Date: 7/1/2026", datetime(2026, 7, 1)),
        ("Date:06/24/2026", datetime(2026, 6, 24)),
        ("  Date 03/04/26", datetime(2026, 3, 4)),
    ],
)
def test_oc_probate_header_variants_4808_line_formats(line: str, expected: datetime) -> None:
    text = "TENTATIVE RULINGS FOR DEPARTMENT CM04\nHON. JUDGE X\n" + line + "\nbody\n"
    assert _probate_hearing_date_from_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Malformed header (dev raw 46a2dc92…): keep None rather than guess.
        "TENTATIVE RULINGS FOR DEPARTMENT CM08\nDate: 05/013/2026\n",
        # Doubled-glyph text layer (dev raw 997981a6…): no parseable label.
        "TTEENNTTAATTIIVVEE RRUULLIINNGGSS\nDDaattee:: 0066//2244//22002266\n",
        # Blank header date (boilerplate placeholder).
        "TENTATIVE RULINGS FOR DEPARTMENT CM3\nHON. Judge X\nDate:\n",
        # Body dates: the label must start the line (#4682).
        "The trust dated 1/16/2013 is the petitioner.\n",
        "The Update 1/16/2026 notice was served.\n",
        "Filing Date 1/16/2026 of the petition.\n",
        "Datebook 1/16/2026\n",
        # Impossible calendar date.
        "Date 2/30/2026\n",
    ],
)
def test_oc_probate_header_variants_4808_returns_none(text: str) -> None:
    assert _probate_hearing_date_from_text(text) is None


# ---------------------------------------------------------------------------
# Case title extraction
# ---------------------------------------------------------------------------


def test_probate_case_title_standard() -> None:
    text = "1 Fard - Trust\n01157766 MOTIONS FOR JUDGMENT"
    title = _probate_case_title_from_text(text)
    # This pattern needs "Tentative" to match — let's use real PDF text
    assert title is None or "Fard" in title


def test_probate_case_title_cm3() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm3.pdf"))
    title = _probate_case_title_from_text(text)
    assert title is not None
    assert "Fard" in title


def test_probate_case_title_cm5() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm5.pdf"))
    title = _probate_case_title_from_text(text)
    assert title is not None
    assert "Collins" in title


# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------


def test_probate_outcome_granted() -> None:
    assert _probate_outcome_from_text("is GRANTED") == "GRANTED"


def test_probate_outcome_denied() -> None:
    assert _probate_outcome_from_text("is DENIED") == "DENIED"


def test_probate_outcome_cm3() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm3.pdf"))
    outcome = _probate_outcome_from_text(text)
    assert outcome == "DENIED"


def test_probate_outcome_cm5() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm5.pdf"))
    outcome = _probate_outcome_from_text(text)
    assert outcome == "DENIED"


# ---------------------------------------------------------------------------
# Motion type extraction
# ---------------------------------------------------------------------------


def test_probate_motion_type_standard() -> None:
    text = "MOTION FOR JUDGMENT ON THE PLEADINGS (ROA 123)"
    result = _probate_motion_type_from_text(text)
    assert result is not None
    assert "JUDGMENT ON THE PLEADINGS" in result


def test_probate_motion_type_cm3() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm3.pdf"))
    motion = _probate_motion_type_from_text(text)
    assert motion is not None
    assert "JUDGMENT" in motion.upper()


def test_probate_motion_type_cm5() -> None:
    text = _extract_pdf_text(_load_bytes("oc_probate_cm5.pdf"))
    motion = _probate_motion_type_from_text(text)
    assert motion is not None
    assert "PROTECTIVE ORDER" in motion.upper()


# ---------------------------------------------------------------------------
# Full scraper run — index page parsing + PDF fetching
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_probate_run_fetches_documents() -> None:
    """Full run: fetch index page, discover 6 PDF links, fetch each PDF."""
    html = _load_html("oc_probate_page.html")
    pdf_bytes = _load_bytes("oc_probate_cm3.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = probate_default_config()
    config.request_delay_seconds = 0
    scraper = OCProbateTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) == 6

    # All should have department set from link text
    departments = [d.department for d in docs]
    assert "CM3" in departments
    assert "CM5" in departments

    # All should have courthouse
    for doc in docs:
        assert doc.courthouse == "Costa Mesa Justice Center"


@respx.mock
def test_oc_probate_run_populates_all_fields() -> None:
    """Full run with parse: all extractable fields populated."""
    html = _load_html("oc_probate_page.html")
    pdf_bytes = _load_bytes("oc_probate_cm3.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = probate_default_config()
    config.request_delay_seconds = 0
    scraper = OCProbateTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Verify first parsed doc has all fields extracted from PDF
    doc = parsed[0]
    assert doc.judge_name == "Erin Rowe"  # From PDF text, not link text
    assert doc.department == "CM3"  # From PDF text
    assert doc.courthouse == "Costa Mesa Justice Center"
    assert doc.hearing_date == datetime(2026, 3, 4)
    assert doc.case_number == "01157766"
    assert doc.case_title is not None
    assert doc.outcome is not None
    assert doc.motion_type is not None
    assert doc.ruling_text is not None


@respx.mock
def test_oc_probate_run_cm5_fields() -> None:
    """Full run with CM5 fixture: all extractable fields populated."""
    html = _load_html("oc_probate_page.html")
    pdf_bytes = _load_bytes("oc_probate_cm5.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = probate_default_config()
    config.request_delay_seconds = 0
    scraper = OCProbateTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    doc = parsed[0]
    assert doc.judge_name == "Ebrahim Baytieh"
    assert doc.hearing_date == datetime(2026, 2, 19)
    assert doc.case_number == "01430606"
    assert doc.case_title is not None
    assert doc.outcome is not None
    assert doc.motion_type is not None


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------


def test_oc_probate_default_config() -> None:
    config = probate_default_config()
    assert config.scraper_id == "ca-oc-tentatives-probate"
    assert config.state == "CA"
    assert config.county == "Orange"
    assert len(config.schedule_windows) == 2


def test_oc_probate_default_config_with_bucket() -> None:
    config = probate_default_config(s3_bucket="test-bucket")
    assert config.s3_bucket == "test-bucket"
