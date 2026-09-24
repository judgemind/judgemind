"""Tests for the Contra Costa tentative rulings portal scraper (Phase 1).

Fixtures in tests/fixtures/cc_portal/:
  form.html              — /tentative-rulings judge dropdown (now on the live listing page, #4591)
  listing_devine.html    — /tentative-rulings?field_judge_target_id=238 (7 rows, 3 test entries)
  listing_reyes.html     — /tentative-rulings?field_judge_target_id=245 (L24-04564)
  listing_weil.html      — /tentative-rulings?field_judge_target_id=280 (MSN23-2201)
  listing_empty.html     — empty table / no-results page
  detail_l24-04564.html  — detail page for L24-04564 (current jcc-body__main-text
                           structure: ruling content under an <h2>Tentative Ruling</h2>
                           heading inside <div class="jcc-body__main-text">, plus a
                           boilerplate /system/files/traffic/ PDF in a footer aside that
                           the parser must NOT select; #4598)
  detail_l24-04564_legacy.html — back-compat fixture preserving the OLD
                           <div class="field--name-body"> structure, exercising the
                           parser's legacy fallback path (#4598)
  sample.pdf             — minimal PDF bytes for HTTP stubbing
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
import structlog.testing
from helpers.reingest import make_reingest_cap_doc

from courts.ca.cc_tentatives_portal import (
    BASE_URL,
    FORM_URL,
    LISTING_URL,
    CCTentativesPortalScraper,
    _cc_dept_from_filename,
    _coerce_hearing_date,
    _is_test_entry,
    _parse_detail_page,
    _parse_judge_dropdown,
    _parse_listing_table,
)
from courts.ca.cc_tentatives_portal import default_config as portal_default_config
from framework import ContentFormat

# Identifier-shape defaults shared across this module's reingest-path
# regression tests.  Kept as module constants so each call to the shared
# ``make_reingest_cap_doc`` helper pins the same scraper identity.
_CC_SCRAPER_ID = "ca-cc-tentatives-portal"
_CC_STATE = "CA"
_CC_COUNTY = "Contra Costa"
_CC_COURT = "Superior Court"
_CC_SOURCE_URL = "https://contracosta.courts.ca.gov/tentative-ruling/l24-04564"
_CC_CAPTURE_TS = datetime(2025, 1, 28, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "cc_portal"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# 1. test_parse_judge_dropdown_returns_known_ids
# ---------------------------------------------------------------------------


def test_parse_judge_dropdown_returns_known_ids() -> None:
    html = _load_html("form.html")
    judges = _parse_judge_dropdown(html)
    ids = [j[0] for j in judges]
    names = [j[1] for j in judges]

    assert "238" in ids
    assert "242" in ids
    assert "245" in ids
    assert "276" in ids
    assert "278" in ids
    assert "280" in ids

    # Verify name content
    assert any("DEVINE" in n for n in names)
    assert any("TREAT" in n for n in names)
    assert any("REYES" in n for n in names)
    assert any("DOUGLAS" in n for n in names)
    assert any("BELTRAMO" in n for n in names)
    assert any("WEIL" in n for n in names)

    # "All" option should be excluded
    assert "All" not in ids
    assert "all" not in [v.lower() for v in ids]


def test_parse_judge_dropdown_missing_select_returns_empty() -> None:
    result = _parse_judge_dropdown("<html><body><p>No form here</p></body></html>")
    assert result == []


# ---------------------------------------------------------------------------
# 2. test_parse_listing_table_extracts_all_fields
# ---------------------------------------------------------------------------


def test_parse_listing_table_extracts_all_fields() -> None:
    html = _load_html("listing_reyes.html")
    rows = _parse_listing_table(html)

    assert len(rows) == 1
    row = rows[0]

    assert row["slug"] == "l24-04564"
    assert row["case_number"] == "L24-04564"
    assert row["case_title"] is not None
    assert "FUGERE" in row["case_title"] or "CONTRA COSTA" in row["case_title"]
    assert row["case_type"] == "Civil"
    assert row["motion_type"] == "CASE MANAGEMENT CONFERENCE"

    # hearing_date should be parsed as UTC datetime
    assert row["hearing_date"] is not None
    hd: datetime = row["hearing_date"]
    assert hd.year == 2025
    assert hd.month == 1
    assert hd.day == 29
    assert hd.hour == 16
    assert hd.minute == 31
    assert hd.tzinfo is not None

    # detail_url should be an absolute URL
    assert row["detail_url"].startswith("https://")
    assert "l24-04564" in row["detail_url"]


# ---------------------------------------------------------------------------
# 3. test_parse_listing_table_weil_msn_case_number
# ---------------------------------------------------------------------------


def test_parse_listing_table_weil_msn_case_number() -> None:
    html = _load_html("listing_weil.html")
    rows = _parse_listing_table(html)

    assert len(rows) == 1
    row = rows[0]
    case_number = row["case_number"]
    assert case_number == "MSN23-2201"

    # Must pass the case number regex (not a test entry)
    assert not _is_test_entry(row["slug"], case_number)


# ---------------------------------------------------------------------------
# 4. test_parse_listing_table_empty_returns_empty_list
# ---------------------------------------------------------------------------


def test_parse_listing_table_empty_returns_empty_list() -> None:
    html = _load_html("listing_empty.html")
    rows = _parse_listing_table(html)
    assert rows == []


def test_parse_listing_table_no_table_returns_empty_list() -> None:
    rows = _parse_listing_table("<html><body><p>no table here</p></body></html>")
    assert rows == []


# ---------------------------------------------------------------------------
# 5. test_parse_detail_page_extracts_ruling_and_pdf
# ---------------------------------------------------------------------------


def test_parse_detail_page_extracts_ruling_and_pdf() -> None:
    html = _load_html("detail_l24-04564.html")
    detail = _parse_detail_page(html)

    assert detail["ruling_text"] is not None
    assert "Before the Court are a demurrer" in detail["ruling_text"]

    assert detail["pdf_url"] is not None
    assert detail["pdf_url"].endswith("/system/files/general/16_012925.pdf")

    assert detail["judge_name"] == "BENJAMIN REYES"

    # ruling_text_html should be present
    assert detail["ruling_text_html"] is not None


def test_parse_detail_page_new_jcc_body_structure() -> None:
    """Regression (#4598): parse the current jcc-body__main-text detail structure.

    The portal moved ruling content out of <div class="field--name-body"> and
    into <div class="jcc-body__main-text"> under an <h2>Tentative Ruling</h2>
    heading, with the body <p> nested inside an outer <p>.  Every detail page
    also carries a boilerplate /system/files/traffic/ PDF in a footer aside that
    must NOT be selected — the real ruling PDF lives under /system/files/general/.
    """
    html = _load_html("detail_l24-04564.html")
    detail = _parse_detail_page(html)

    assert detail["pdf_url"] is not None
    assert detail["pdf_url"].endswith("/system/files/general/16_012925.pdf")
    assert "/traffic/" not in detail["pdf_url"]

    assert detail["ruling_text"] is not None
    assert "Before the Court are a demurrer" in detail["ruling_text"]
    # The PDF-link paragraph text must be excluded from the ruling body.
    assert "Tentative Ruling PDF" not in detail["ruling_text"]

    assert detail["ruling_text_html"] is not None
    assert detail["judge_name"] == "BENJAMIN REYES"


def test_parse_detail_page_traffic_only_falls_back_excluding_traffic() -> None:
    """Regression (#4598): with no /general/ PDF, never select the traffic PDF.

    Exercises the fallback path in _select_pdf_url: a non-PDF anchor is skipped,
    the /system/files/traffic/ link is excluded, and a different .pdf link is
    chosen as the fallback.  Also exercises the next-<h2> boundary in
    _ruling_section_paragraphs (a trailing <h2> after the ruling section).
    """
    html = (
        '<html><body><div class="jcc-body__main-text">'
        "<h2>Case Number</h2><p><span>L24-09999</span></p>"
        "<h2>Tentative Ruling</h2>"
        "<p>"
        '<p><a href="/about">About</a></p>'
        '<p><a href="/system/files/traffic/tr-320-info.pdf">Traffic info</a></p>'
        '<p><a href="/system/files/other/99_010125.pdf">Ruling PDF</a></p>'
        "<p>The motion is DENIED.</p>"
        "</p>"
        "<h2>Footnotes</h2><p>Not part of the ruling body.</p>"
        "</div></body></html>"
    )
    detail = _parse_detail_page(html)

    assert detail["pdf_url"] is not None
    assert detail["pdf_url"].endswith("/system/files/other/99_010125.pdf")
    assert "/traffic/" not in detail["pdf_url"]

    assert detail["ruling_text"] is not None
    assert "The motion is DENIED." in detail["ruling_text"]
    # The trailing Footnotes <h2> section is outside the ruling section.
    assert "Not part of the ruling body." not in detail["ruling_text"]


def test_parse_detail_page_jcc_body_without_tentative_ruling_heading() -> None:
    """Regression (#4598): a jcc-body__main-text div lacking the ruling heading.

    When the <h2>Tentative Ruling</h2> heading is absent the ruling section is
    empty, so pdf_url / ruling_text / ruling_text_html stay None.  Exercises the
    no-heading return in _ruling_section_paragraphs.
    """
    html = (
        '<html><body><div class="jcc-body__main-text">'
        "<h2>Case Number</h2><p><span>L24-08888</span></p>"
        "<h2>Case Type</h2><p>Civil</p>"
        "</div></body></html>"
    )
    detail = _parse_detail_page(html)

    assert detail["pdf_url"] is None
    assert detail["ruling_text"] is None
    assert detail["ruling_text_html"] is None


def test_parse_detail_page_legacy_field_name_body_fallback() -> None:
    """Regression (#4598): archived pages still use the old field--name-body div.

    The parser must fall back to <div class="field--name-body"> when
    jcc-body__main-text is absent, so older S3-archived detail pages reingest
    correctly.
    """
    html = _load_html("detail_l24-04564_legacy.html")
    detail = _parse_detail_page(html)

    assert detail["pdf_url"] is not None
    assert detail["pdf_url"].endswith("/system/files/general/16_012925.pdf")

    assert detail["ruling_text"] is not None
    assert "Before the Court are a demurrer" in detail["ruling_text"]
    assert "Tentative Ruling PDF" not in detail["ruling_text"]

    assert detail["ruling_text_html"] is not None
    assert detail["judge_name"] == "BENJAMIN REYES"


def test_parse_detail_page_paragraphs_wrapped_in_container() -> None:
    """Robustness (#4598): ruling <p> wrapped in a container <div>.

    If a future portal theming change nests the ruling paragraphs inside a
    container element (e.g. a styling <div>) after the <h2>Tentative Ruling</h2>
    heading, _ruling_section_paragraphs must still descend into the container to
    find the PDF link and body text — capture reliability is the top priority,
    so a container change must not silently drop the ruling.
    """
    html = (
        "<html><body><article>"
        '<div class="jcc-body__main-text usa-prose clearfix">'
        "<h2>Case Number</h2><p><span>L24-04564</span></p>"
        "<h2>Tentative Ruling</h2>"
        '<div class="ruling-wrapper">'
        '<p><a href="/system/files/general/16_012925.pdf">Tentative Ruling PDF</a></p>'
        "<p>Before the Court are a demurrer and motion to strike.</p>"
        "<p>The motion to strike is GRANTED.</p>"
        "</div>"
        "</div>"
        '<aside class="jcc-body__aside"><h4>BENJAMIN REYES</h4></aside>'
        '<aside class="usa-footer">'
        '<a href="/system/files/traffic/tr-320-info.pdf">Traffic</a></aside>'
        "</article></body></html>"
    )
    detail = _parse_detail_page(html)

    assert detail["pdf_url"] is not None
    assert detail["pdf_url"].endswith("/system/files/general/16_012925.pdf")
    assert "/traffic/" not in detail["pdf_url"]

    assert detail["ruling_text"] is not None
    assert "Before the Court are a demurrer" in detail["ruling_text"]
    # The PDF-link paragraph text must be excluded from the ruling body.
    assert "Tentative Ruling PDF" not in detail["ruling_text"]

    assert detail["ruling_text_html"] is not None
    assert detail["judge_name"] == "BENJAMIN REYES"


def test_parse_detail_page_nested_h2_boundary_inside_wrapper() -> None:
    """Robustness (#4598): the next <h2> ends the section even when nested.

    If a future portal theming change wraps a *section* in a container <div>
    (``<h2>Tentative Ruling</h2><div>...<h2>Footnotes</h2>...</div>``), the
    document-order traversal in _ruling_section_paragraphs must stop at that
    inner <h2> rather than leaking the following section's paragraphs into the
    ruling body.  Uses find_all_next() (not find_next_siblings()) so the
    boundary is detected regardless of nesting depth.
    """
    html = (
        "<html><body><article>"
        '<div class="jcc-body__main-text usa-prose clearfix">'
        "<h2>Case Number</h2><p><span>L24-04564</span></p>"
        "<h2>Tentative Ruling</h2>"
        '<div class="section-wrapper">'
        '<p><a href="/system/files/general/16_012925.pdf">Tentative Ruling PDF</a></p>'
        "<p>Before the Court are a demurrer and motion to strike.</p>"
        "<h2>Footnotes</h2>"
        "<p>This footnote text should NOT be captured.</p>"
        "</div>"
        "</div>"
        '<aside class="jcc-body__aside"><h4>BENJAMIN REYES</h4></aside>'
        '<aside class="usa-footer">'
        '<a href="/system/files/traffic/tr-320-info.pdf">Traffic</a></aside>'
        "</article></body></html>"
    )
    detail = _parse_detail_page(html)

    assert detail["pdf_url"] is not None
    assert detail["pdf_url"].endswith("/system/files/general/16_012925.pdf")
    assert "/traffic/" not in detail["pdf_url"]

    assert detail["ruling_text"] is not None
    assert "Before the Court are a demurrer" in detail["ruling_text"]
    # The nested <h2>Footnotes</h2> ends the ruling section — its paragraph
    # must not leak into the ruling body.
    assert "This footnote text should NOT be captured." not in detail["ruling_text"]
    assert "Tentative Ruling PDF" not in detail["ruling_text"]

    assert detail["ruling_text_html"] is not None
    assert detail["judge_name"] == "BENJAMIN REYES"


# ---------------------------------------------------------------------------
# 6. test_cc_dept_from_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pdf_url", "expected"),
    [
        ("/system/files/general/16_012925.pdf", "16"),
        ("https://contracosta.courts.ca.gov/system/files/general/09_031126.pdf", "09"),
        ("/garbage.pdf", None),
        ("https://example.com/notmatching.pdf", None),
        (None, None),
    ],
)
def test_cc_dept_from_filename(pdf_url: str | None, expected: str | None) -> None:
    assert _cc_dept_from_filename(pdf_url) == expected


# ---------------------------------------------------------------------------
# 7. test_is_test_entry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "case_number", "expected"),
    [
        # Test slug → always filtered
        ("test-case", "C22-01620", True),
        ("test-1", "L24-04564", True),
        ("TEST-SOMETHING", "N25-1234", True),
        # Valid real entries
        ("l24-04564", "L24-04564", False),
        ("c22-01620", "C22-01620", False),
        ("n25-1234", "N25-1234", False),
        ("msn23-2201", "MSN23-2201", False),
        # Invalid case number → filtered
        ("foo", "badnumber", True),
        ("foo", "NOTACASE", True),
        ("foo", None, True),
        # Probate variants pass
        ("p24-1234", "P24-1234", False),
        ("p24-12345", "P24-12345", False),
        # MSN variant passes
        ("msn23-2201", "MSN23-2201", False),
        # 5-digit variants pass
        ("c24-02490", "C24-02490", False),
        ("l23-06679", "L23-06679", False),
    ],
)
def test_is_test_entry(slug: str, case_number: str | None, expected: bool) -> None:
    assert _is_test_entry(slug, case_number) == expected


# ---------------------------------------------------------------------------
# 8. test_fetch_documents_filters_test_entries_and_emits_skip_log
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_documents_filters_test_entries_and_emits_skip_log() -> None:
    """Scraper should skip test entries and log scraper.test_entry_skipped per skip."""
    form_html = _load_html("form.html")
    # Use a form with only Devine (id=238) to limit the test scope
    devine_only_form = (
        form_html.replace('<option value="242">CHARLES S TREAT</option>', "")
        .replace('<option value="245">BENJAMIN REYES</option>', "")
        .replace('<option value="276">DANIELLE K DOUGLAS</option>', "")
        .replace('<option value="278">SHARA E BELTRAMO</option>', "")
        .replace('<option value="280">EDWARD G WEIL</option>', "")
    )
    listing_html = _load_html("listing_devine.html")
    pdf_bytes = _load_bytes("sample.pdf")
    detail_html = _load_html("detail_l24-04564.html")

    # Stub the per-judge listing FIRST so the params-specific route claims
    # the listing fetch before the no-param form route (FORM_URL == LISTING_URL
    # since #4591 — respx matches routes in registration order, and a
    # params-less route matches any request to the same path).
    respx.get(LISTING_URL, params={"field_judge_target_id": "238"}).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
    # Stub the form (judge dropdown) — the params-less fetch falls through here.
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=devine_only_form))
    # Stub detail pages for valid entries
    for slug in ["c22-01620", "c24-00123", "l23-05678", "n25-1234"]:
        respx.get(f"{BASE_URL}/tentative-ruling/{slug}").mock(
            return_value=httpx.Response(200, text=detail_html)
        )
    # Stub PDF
    respx.get(f"{BASE_URL}/system/files/general/16_012925.pdf").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    config = portal_default_config()
    config = config.model_copy(update={"request_delay_seconds": 0.0})
    scraper = CCTentativesPortalScraper(config=config)

    with structlog.testing.capture_logs() as cap:
        docs = scraper.fetch_documents()

    # Devine listing has 7 rows: 3 test entries (test-case, test-1, bad-number)
    # and 4 valid (c22-01620, c24-00123, l23-05678, n25-1234).
    # bad-number has case_number "bad-number" which fails the regex → filtered.
    skip_events = [e for e in cap if e.get("event") == "scraper.test_entry_skipped"]
    assert len(skip_events) == 3, f"Expected 3 skip events, got {len(skip_events)}: {skip_events}"

    # Docs count should be 4 (the 4 valid entries)
    assert len(docs) == 4


# ---------------------------------------------------------------------------
# 9. test_fetch_documents_downloads_pdf_and_keeps_detail_html
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_documents_archives_envelope_with_pdf_and_detail_html() -> None:
    """raw_content is a JSON envelope (#4133); PDF and detail HTML round-trip through it.

    Pre-#4133 ``raw_content`` was the raw PDF bytes and the detail HTML
    lived only in ``doc.extra["detail_html"]`` — neither shape survived
    reingest because reingest builds a fresh ``CapturedDocument``
    carrying only ``raw_content``.  Post-#4133 the envelope holds both
    artifacts plus the listing-row dict, so ``parse_document`` can
    re-derive every field on the reingest path.

    ``doc.extra["detail_html"]`` is preserved on the live path for
    backwards compatibility with consumers that read it (currently only
    these tests).
    """
    # Use a form with only Reyes (id=245)
    reyes_only_form = (
        '<html><body><form><select name="field_judge_target_id">'
        '<option value="All">- Any -</option>'
        '<option value="245">BENJAMIN REYES</option>'
        "</select></form></body></html>"
    )
    listing_html = _load_html("listing_reyes.html")
    detail_html_bytes = _load_bytes("detail_l24-04564.html")
    pdf_bytes = _load_bytes("sample.pdf")

    # Params-specific listing route first (FORM_URL == LISTING_URL since #4591).
    respx.get(LISTING_URL, params={"field_judge_target_id": "245"}).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=reyes_only_form))
    respx.get(f"{BASE_URL}/tentative-ruling/l24-04564").mock(
        return_value=httpx.Response(200, content=detail_html_bytes)
    )
    respx.get(f"{BASE_URL}/system/files/general/16_012925.pdf").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    config = portal_default_config()
    config = config.model_copy(update={"request_delay_seconds": 0.0})
    scraper = CCTentativesPortalScraper(config=config)

    docs = scraper.fetch_documents()

    assert len(docs) == 1
    doc = docs[0]

    # raw_content is now the JSON envelope (TEXT format), not raw PDF bytes
    assert doc.content_format == ContentFormat.TEXT
    payload = json.loads(doc.raw_content)
    assert isinstance(payload, dict)
    assert "row" in payload and isinstance(payload["row"], dict)
    assert payload["row"]["case_number"] == "L24-04564"

    # Both byte-streams round-trip through the envelope as base64.
    assert base64.b64decode(payload["pdf_bytes_b64"]) == pdf_bytes
    assert base64.b64decode(payload["detail_html_b64"]) == detail_html_bytes

    # Backwards-compatible extras still populated on the live path.
    assert doc.extra["detail_html"] == detail_html_bytes
    assert doc.extra["pdf_url"].endswith("/system/files/general/16_012925.pdf")
    assert doc.extra["slug"] == "l24-04564"

    # Both requests were made
    assert respx.calls.call_count >= 4  # form + listing + detail + pdf


# ---------------------------------------------------------------------------
# 10. test_fetch_documents_handles_empty_judge_listing
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_documents_handles_empty_judge_listing() -> None:
    """Scraper should return 0 docs and not raise when listing is empty."""
    weil_only_form = (
        '<html><body><form><select name="field_judge_target_id">'
        '<option value="All">- Any -</option>'
        '<option value="280">EDWARD G WEIL</option>'
        "</select></form></body></html>"
    )
    empty_listing_html = _load_html("listing_empty.html")

    # Params-specific listing route first (FORM_URL == LISTING_URL since #4591).
    respx.get(LISTING_URL, params={"field_judge_target_id": "280"}).mock(
        return_value=httpx.Response(200, text=empty_listing_html)
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=weil_only_form))

    config = portal_default_config()
    config = config.model_copy(update={"request_delay_seconds": 0.0})
    scraper = CCTentativesPortalScraper(config=config)

    docs = scraper.fetch_documents()
    assert docs == []


# ---------------------------------------------------------------------------
# 11. test_fetch_documents_handles_new_judge_id_in_dropdown
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_documents_handles_new_judge_id_in_dropdown() -> None:
    """When a previously unknown judge ID appears in the dropdown, iterate it."""
    # Form with a fake "999" judge ID not in the known list
    form_with_new_judge = (
        '<html><body><form><select name="field_judge_target_id">'
        '<option value="All">- Any -</option>'
        '<option value="999">TEST JUDGE NEW</option>'
        "</select></form></body></html>"
    )
    empty_listing_html = _load_html("listing_empty.html")

    # Params-specific listing route first (FORM_URL == LISTING_URL since #4591).
    respx.get(LISTING_URL, params={"field_judge_target_id": "999"}).mock(
        return_value=httpx.Response(200, text=empty_listing_html)
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=form_with_new_judge))

    config = portal_default_config()
    config = config.model_copy(update={"request_delay_seconds": 0.0})
    scraper = CCTentativesPortalScraper(config=config)

    # Should not raise — just returns empty list
    docs = scraper.fetch_documents()
    assert docs == []

    # The new judge ID was actually fetched
    listing_calls = [c for c in respx.calls if "field_judge_target_id=999" in str(c.request.url)]
    assert len(listing_calls) == 1


# ---------------------------------------------------------------------------
# 12. test_scraper_extracts_fields_civil_limited_probate
# ---------------------------------------------------------------------------


@respx.mock
def test_scraper_extracts_fields_civil_limited_probate() -> None:
    """Verify all key fields are populated on a real-style end-to-end doc."""
    reyes_only_form = (
        '<html><body><form><select name="field_judge_target_id">'
        '<option value="All">- Any -</option>'
        '<option value="245">BENJAMIN REYES</option>'
        "</select></form></body></html>"
    )
    listing_html = _load_html("listing_reyes.html")
    detail_html_bytes = _load_bytes("detail_l24-04564.html")
    pdf_bytes = _load_bytes("sample.pdf")

    # Params-specific listing route first (FORM_URL == LISTING_URL since #4591).
    respx.get(LISTING_URL, params={"field_judge_target_id": "245"}).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=reyes_only_form))
    respx.get(f"{BASE_URL}/tentative-ruling/l24-04564").mock(
        return_value=httpx.Response(200, content=detail_html_bytes)
    )
    respx.get(f"{BASE_URL}/system/files/general/16_012925.pdf").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    config = portal_default_config()
    config = config.model_copy(update={"request_delay_seconds": 0.0})
    scraper = CCTentativesPortalScraper(config=config)
    docs = scraper.fetch_documents()

    assert len(docs) == 1
    doc = docs[0]

    assert doc.case_number == "L24-04564"
    assert doc.case_title is not None
    assert doc.hearing_date is not None
    assert doc.hearing_date.year == 2025
    assert doc.judge_name == "BENJAMIN REYES"
    assert doc.motion_type == "CASE MANAGEMENT CONFERENCE"
    assert doc.ruling_text is not None
    assert "Before the Court are a demurrer" in doc.ruling_text

    # State/county/court populated from config
    assert doc.state == "CA"
    assert doc.county == "Contra Costa"
    assert doc.court == "Superior Court"

    # scraper_id from config
    assert doc.scraper_id == "ca-cc-tentatives-portal"


# ---------------------------------------------------------------------------
# 13. test_default_config_registered
# ---------------------------------------------------------------------------


def test_default_config_registered() -> None:
    """The scraper ID 'ca-cc-tentatives-portal' must appear in the runner registry."""
    from framework.runner import get_scraper_ids

    ids = get_scraper_ids()
    assert "ca-cc-tentatives-portal" in ids


# ---------------------------------------------------------------------------
# 14. parse_document reingest-safety regression tests (#4133)
# ---------------------------------------------------------------------------


def _make_reingest_scraper() -> CCTentativesPortalScraper:
    """Build a scraper instance suitable for parse_document-only tests."""
    config = portal_default_config()
    config = config.model_copy(update={"request_delay_seconds": 0.0})
    return CCTentativesPortalScraper(config=config)


def _build_envelope_bytes(
    *,
    detail_html: bytes,
    pdf_bytes: bytes,
    pdf_url: str = "https://contracosta.courts.ca.gov/system/files/general/16_012925.pdf",
    case_number: str = "L24-04564",
    case_title: str = "SCOTT FUGERE VS. THE COUNTY OF CONTRA COSTA",
    motion_type: str = "CASE MANAGEMENT CONFERENCE",
    case_type: str = "Civil",
    slug: str = "l24-04564",
    judge_id: str = "245",
    judge_name_dropdown: str = "BENJAMIN REYES",
    hearing_date_iso: str = "2025-01-29T16:31:00+00:00",
) -> bytes:
    """Build a JSON envelope mirroring what _fetch_single_ruling writes.

    Used by the reingest regression tests below to exercise
    ``parse_document`` without going through the live HTTP path.  The
    ``hearing_date`` field is intentionally an ISO-8601 string (not a
    ``datetime``) — that's what reingest sees after the envelope
    round-trips through ``json.dumps(default=str)`` and S3.
    """
    envelope = {
        "row": {
            "slug": slug,
            "detail_url": f"{BASE_URL}/tentative-ruling/{slug}",
            "case_number": case_number,
            "case_title": case_title,
            "case_type": case_type,
            "motion_type": motion_type,
            "hearing_date": hearing_date_iso,
        },
        "detail_html_b64": base64.b64encode(detail_html).decode("ascii"),
        "pdf_url": pdf_url,
        "pdf_bytes_b64": base64.b64encode(pdf_bytes).decode("ascii"),
        "judge_id": judge_id,
        "judge_name_dropdown": judge_name_dropdown,
    }
    return json.dumps(envelope).encode("utf-8")


def test_parse_document_reingest_populates_fields_from_envelope() -> None:
    """Acceptance criterion #3 (#4133) — parse_document populates structured fields
    from raw_content alone, with no live-capture state available.

    Constructs a fresh ``CapturedDocument`` carrying only the JSON
    envelope as ``raw_content`` (the exact shape reingest hands to
    ``parse_document``) and asserts that every field that
    ``_fetch_single_ruling`` populates on the live path is recovered.
    This is the test that would have caught #3986 and would have
    flagged this scraper in the audit if it had existed.
    """
    detail_html = _load_bytes("detail_l24-04564.html")
    pdf_bytes = _load_bytes("sample.pdf")

    raw = _build_envelope_bytes(detail_html=detail_html, pdf_bytes=pdf_bytes)
    doc = make_reingest_cap_doc(
        raw_content=raw,
        scraper_id=_CC_SCRAPER_ID,
        state=_CC_STATE,
        county=_CC_COUNTY,
        court=_CC_COURT,
        source_url=_CC_SOURCE_URL,
        capture_timestamp=_CC_CAPTURE_TS,
    )
    assert doc.case_number is None
    assert doc.judge_name is None
    assert doc.department is None
    assert doc.ruling_text is None
    assert doc.ruling_text_html is None

    scraper = _make_reingest_scraper()
    parsed = scraper.parse_document(doc)

    # Listing-row-derived fields recovered from the envelope.
    assert parsed.case_number == "L24-04564"
    assert parsed.case_title is not None
    assert "FUGERE" in parsed.case_title or "CONTRA COSTA" in parsed.case_title
    assert parsed.motion_type == "CASE MANAGEMENT CONFERENCE"

    # hearing_date round-trips through ISO-8601 and lands as a datetime.
    assert isinstance(parsed.hearing_date, datetime)
    assert parsed.hearing_date.year == 2025
    assert parsed.hearing_date.month == 1
    assert parsed.hearing_date.day == 29

    # Detail-page-derived fields re-derived from the embedded HTML —
    # ruling_text_html is the field that was permanently lost pre-#4133.
    assert parsed.ruling_text is not None
    assert "Before the Court are a demurrer" in parsed.ruling_text
    assert parsed.ruling_text_html is not None
    assert parsed.judge_name == "BENJAMIN REYES"

    # PDF-URL-derived fields.
    assert parsed.department == "16"
    assert parsed.courthouse == "Martinez Courthouse"

    # Extras keep the same shape as the live path.
    assert parsed.extra["pdf_url"].endswith("/system/files/general/16_012925.pdf")
    assert parsed.extra["pdf_filename"] == "16_012925.pdf"
    assert parsed.extra["slug"] == "l24-04564"
    assert parsed.extra["judge_id"] == "245"


def test_parse_document_reingest_pre_4133_pdf_bytes_returns_unchanged() -> None:
    """Pre-#4133 captures archived raw PDF bytes (not a JSON envelope).

    On the reingest path those archived docs hand ``parse_document`` raw
    PDF bytes that fail JSON decode.  The method MUST tolerate this and
    return the doc unchanged — reingest's ``_extract_text_from_content``
    + DB-seeded fields handle the recovery for those legacy captures
    (the symmetric merge in #4142 keeps the DB seeds intact).
    """
    pdf_bytes = _load_bytes("sample.pdf")
    doc = make_reingest_cap_doc(
        raw_content=pdf_bytes,
        scraper_id=_CC_SCRAPER_ID,
        state=_CC_STATE,
        county=_CC_COUNTY,
        court=_CC_COURT,
        source_url=_CC_SOURCE_URL,
        capture_timestamp=_CC_CAPTURE_TS,
    )

    scraper = _make_reingest_scraper()
    parsed = scraper.parse_document(doc)

    # All structured fields stay at their defaults — the merge in
    # _reparse_document then falls back to DB seeds (#4142).
    assert parsed.case_number is None
    assert parsed.judge_name is None
    assert parsed.department is None
    assert parsed.ruling_text is None
    assert parsed.ruling_text_html is None
    # raw_content is preserved unchanged for downstream pdfplumber.
    assert parsed.raw_content == pdf_bytes


def test_parse_document_reingest_invalid_envelope_shape_returns_unchanged() -> None:
    """A JSON-decodable but wrong-shaped envelope must not populate garbage fields."""
    raw = json.dumps({"unrelated": "payload"}).encode("utf-8")
    doc = make_reingest_cap_doc(
        raw_content=raw,
        scraper_id=_CC_SCRAPER_ID,
        state=_CC_STATE,
        county=_CC_COUNTY,
        court=_CC_COURT,
        source_url=_CC_SOURCE_URL,
        capture_timestamp=_CC_CAPTURE_TS,
    )

    scraper = _make_reingest_scraper()
    parsed = scraper.parse_document(doc)

    assert parsed.case_number is None
    assert parsed.judge_name is None
    assert parsed.ruling_text is None
    # raw_content is preserved.
    assert parsed.raw_content == raw


def test_parse_document_reingest_empty_raw_content_returns_unchanged() -> None:
    """Empty raw_content (defensive case) must not crash."""
    doc = make_reingest_cap_doc(
        raw_content=b"",
        scraper_id=_CC_SCRAPER_ID,
        state=_CC_STATE,
        county=_CC_COUNTY,
        court=_CC_COURT,
        source_url=_CC_SOURCE_URL,
        capture_timestamp=_CC_CAPTURE_TS,
    )
    scraper = _make_reingest_scraper()
    parsed = scraper.parse_document(doc)
    assert parsed.case_number is None
    assert parsed.ruling_text is None


def test_parse_document_reingest_envelope_with_unknown_pdf_filename_skips_dept() -> None:
    """An envelope whose pdf_url does not match the dept regex must not crash.

    Reproduces the case where a non-standard PDF filename appears
    (e.g. a one-off uploaded with a different naming convention).
    The dept/courthouse fields stay None; everything else still
    populates.
    """
    detail_html = _load_bytes("detail_l24-04564.html")
    pdf_bytes = _load_bytes("sample.pdf")

    raw = _build_envelope_bytes(
        detail_html=detail_html,
        pdf_bytes=pdf_bytes,
        pdf_url="https://example.com/some-other-name.pdf",
    )
    doc = make_reingest_cap_doc(
        raw_content=raw,
        scraper_id=_CC_SCRAPER_ID,
        state=_CC_STATE,
        county=_CC_COUNTY,
        court=_CC_COURT,
        source_url=_CC_SOURCE_URL,
        capture_timestamp=_CC_CAPTURE_TS,
    )

    scraper = _make_reingest_scraper()
    parsed = scraper.parse_document(doc)

    assert parsed.case_number == "L24-04564"
    assert parsed.judge_name == "BENJAMIN REYES"
    assert parsed.department is None
    assert parsed.courthouse is None


# ---------------------------------------------------------------------------
# 15. _coerce_hearing_date — accepts datetime, ISO string, or None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_year", "expected_month", "expected_day"),
    [
        (datetime(2025, 1, 29, tzinfo=UTC), 2025, 1, 29),
        ("2025-01-29T16:31:00+00:00", 2025, 1, 29),
        ("2025-01-29 16:31:00+00:00", 2025, 1, 29),
        ("2025-01-29T16:31:00", 2025, 1, 29),
    ],
)
def test_coerce_hearing_date_round_trips(
    value: datetime | str,
    expected_year: int,
    expected_month: int,
    expected_day: int,
) -> None:
    coerced = _coerce_hearing_date(value)
    assert coerced is not None
    assert coerced.year == expected_year
    assert coerced.month == expected_month
    assert coerced.day == expected_day


def test_coerce_hearing_date_none_returns_none() -> None:
    assert _coerce_hearing_date(None) is None


def test_coerce_hearing_date_garbage_string_returns_none() -> None:
    assert _coerce_hearing_date("not a date") is None


# ---------------------------------------------------------------------------
# 16. Judge-discovery URL contract + loud-failure regression tests (#4591)
# ---------------------------------------------------------------------------


def test_form_url_points_at_live_listing_page() -> None:
    """FORM_URL must target the live listing page that carries the dropdown (#4591).

    The judge dropdown migrated off the now-restricted
    ``/test-page-tentative-rulings`` page (which returns an access-denied
    200 body with no ``<select>``) onto the live ``/tentative-rulings``
    page.  Pinning ``FORM_URL == LISTING_URL`` is the test that would have
    caught the all-time-zero-capture regression; the prior tests mocked
    ``FORM_URL`` so they passed regardless of its value.
    """
    assert FORM_URL != f"{BASE_URL}/test-page-tentative-rulings"
    assert FORM_URL == LISTING_URL


@respx.mock
def test_fetch_documents_logs_error_when_no_judges() -> None:
    """An access-denied-style page (no dropdown) returns [] and logs at error level (#4591).

    Pre-#4591 this logged a ``warning`` and returned ``[]`` silently, so
    the zero-record / scraper-health alerting never fired and the total
    coverage failure went undetected for the whole dual-run period.
    Upgrading ``cc_portal.no_judges_found`` to ``error`` makes the
    breakage loud while still not raising (an exception would abort the
    whole 17-scraper run).
    """
    access_denied_html = (
        "<html><body><h1>Access Denied</h1>"
        "<p>This page requires authorization to access.</p>"
        "</body></html>"
    )

    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=access_denied_html))

    config = portal_default_config()
    config = config.model_copy(update={"request_delay_seconds": 0.0})
    scraper = CCTentativesPortalScraper(config=config)

    with structlog.testing.capture_logs() as cap:
        docs = scraper.fetch_documents()

    assert docs == []

    no_judges_events = [e for e in cap if e.get("event") == "cc_portal.no_judges_found"]
    assert len(no_judges_events) == 1, (
        f"Expected 1 cc_portal.no_judges_found event, got {len(no_judges_events)}: "
        f"{no_judges_events}"
    )
    assert no_judges_events[0].get("log_level") == "error"
