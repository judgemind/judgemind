"""Tests for the Contra Costa tentative rulings portal scraper (Phase 1).

Fixtures in tests/fixtures/cc_portal/:
  form.html              — /test-page-tentative-rulings (judge dropdown)
  listing_devine.html    — /tentative-rulings?field_judge_target_id=238 (7 rows, 3 test entries)
  listing_reyes.html     — /tentative-rulings?field_judge_target_id=245 (L24-04564)
  listing_weil.html      — /tentative-rulings?field_judge_target_id=280 (MSN23-2201)
  listing_empty.html     — empty table / no-results page
  detail_l24-04564.html  — detail page for L24-04564
  sample.pdf             — minimal PDF bytes for HTTP stubbing
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx
import structlog.testing

from courts.ca.cc_tentatives_portal import (
    BASE_URL,
    FORM_URL,
    LISTING_URL,
    CCTentativesPortalScraper,
    _cc_dept_from_filename,
    _is_test_entry,
    _parse_detail_page,
    _parse_judge_dropdown,
    _parse_listing_table,
)
from courts.ca.cc_tentatives_portal import default_config as portal_default_config

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

    # Stub the form
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=devine_only_form))
    # Stub the listing
    respx.get(LISTING_URL, params={"field_judge_target_id": "238"}).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
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
def test_fetch_documents_downloads_pdf_and_keeps_detail_html() -> None:
    """PDF should be raw_content; detail HTML should be in doc.extra['detail_html']."""
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

    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=reyes_only_form))
    respx.get(LISTING_URL, params={"field_judge_target_id": "245"}).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
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

    # PDF is the primary raw content
    assert doc.raw_content == pdf_bytes

    # Detail HTML is in extra
    assert "detail_html" in doc.extra
    assert doc.extra["detail_html"] == detail_html_bytes

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

    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=weil_only_form))
    respx.get(LISTING_URL, params={"field_judge_target_id": "280"}).mock(
        return_value=httpx.Response(200, text=empty_listing_html)
    )

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

    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=form_with_new_judge))
    respx.get(LISTING_URL, params={"field_judge_target_id": "999"}).mock(
        return_value=httpx.Response(200, text=empty_listing_html)
    )

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

    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=reyes_only_form))
    respx.get(LISTING_URL, params={"field_judge_target_id": "245"}).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
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
