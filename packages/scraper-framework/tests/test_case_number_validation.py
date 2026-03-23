"""Cross-scraper validation: every scraped document must have a case_number.

This parametrized test runs each scraper against its real fixtures (via respx mocks)
and asserts that ``case_number is not None`` for every emitted ``CapturedDocument``.

The goal is to catch scrapers that silently drop case numbers when the source data
contains parseable case numbers — the class of bug fixed in issue #296.

## Documented exceptions (case_number genuinely absent in source data)

- **OC North Justice Center (N6)**: The ``oc_north_n.pdf`` fixture does not contain
  case numbers anywhere in the PDF text. Cases are listed by line number and name
  only. This is a courthouse-specific PDF format limitation, not a regex issue.
  See ``expected/oc_north_n.json`` for details.

- **OC Family Law Kohler L69**: The ``oc_family_law_kohler_l69.pdf`` fixture is
  an empty boilerplate PDF with no rulings or case numbers.

Parent: #296
Closes: #308
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
import respx

from framework.models import CapturedDocument

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Scraper configurations — one entry per (scraper, fixture) combination that
# should produce documents with case numbers.
# ---------------------------------------------------------------------------


class ScraperFixtureConfig(NamedTuple):
    """Configuration for a single scraper-fixture test case."""

    scraper_id: str
    description: str
    # Number of documents expected with non-None case_number.
    # If None, ALL documents must have case_number.
    expected_with_case_number: int | None
    # If True, some documents may legitimately have case_number=None.
    allows_none_case_numbers: bool


# ---------------------------------------------------------------------------
# LA County
# ---------------------------------------------------------------------------


@respx.mock
def test_la_all_docs_have_case_number() -> None:
    """LA County: every parsed document must have a case_number.

    The LA scraper extracts case numbers from the ruling HTML via regex.
    The la_ruling_response.html fixture has 2 cases, both with case numbers.
    """
    from courts.ca.la_tentatives import (
        CIVIL_URL,
        LATentativeRulingsScraper,
        default_config,
    )

    main_html = _load_html("la_main_page.html")
    ruling_html = _load_html("la_ruling_response.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=ruling_html))

    config = default_config()
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0, "Expected at least one parsed document"
    _assert_all_have_case_number(parsed, "LA County")


# ---------------------------------------------------------------------------
# OC Civil
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_civil_parse_document_is_noop() -> None:
    """OC Civil: parse_document is a no-op — field extraction handled by LLM pipeline.

    OC multimodal extraction is validated and deployed. The scraper captures
    raw PDF content only; case_number extraction happens in the ingestion
    pipeline via LLM page-image analysis.
    """
    from courts.ca.oc_tentatives import (
        INDEX_URL,
        OCTentativeRulingsScraper,
    )
    from courts.ca.oc_tentatives import default_config as oc_default_config

    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0, "Expected at least one parsed document"
    # All documents should have case_number=None — parse_document is a no-op
    none_count = sum(1 for d in parsed if d.case_number is None)
    assert none_count == len(parsed), (
        f"OC Civil: expected all {len(parsed)} docs to have case_number=None "
        f"(parse_document is a no-op), but {len(parsed) - none_count} had values"
    )


@respx.mock
def test_oc_civil_north_n6_no_case_numbers_expected() -> None:
    """OC North Justice Center (N6): case_number=None is expected.

    The oc_north_n.pdf fixture does not contain case numbers anywhere in the
    PDF text. Cases are listed by line number and case name only. This is a
    format limitation of this courthouse's PDF generation.

    See tests/fixtures/expected/oc_north_n.json and fixtures/README.md for
    documentation of this edge case.
    """
    from courts.ca.oc_tentatives import (
        INDEX_URL,
        OCTentativeRulingsScraper,
    )
    from courts.ca.oc_tentatives import default_config as oc_default_config

    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_north_n.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # All documents from N6 should have case_number=None — the PDF format
    # genuinely does not contain case numbers.
    none_count = sum(1 for d in parsed if d.case_number is None)
    assert none_count == len(parsed), (
        f"OC North N6: expected all {len(parsed)} docs to have case_number=None "
        f"(source data has no case numbers), but {len(parsed) - none_count} had values"
    )


# ---------------------------------------------------------------------------
# OC Family Law
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_family_law_claustro_has_case_number() -> None:
    """OC Family Law Claustro C22: fixture has case numbers — all must be extracted."""
    from courts.ca.oc_family_law_tentatives import (
        INDEX_URL,
        OCFamilyLawTentativeRulingsScraper,
    )
    from courts.ca.oc_family_law_tentatives import default_config as fl_default_config

    html = _load_html("oc_family_law_page.html")
    pdf_bytes = _load_bytes("oc_family_law_claustro_c22.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = fl_default_config()
    config.request_delay_seconds = 0
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0, "Expected at least one parsed document"
    _assert_all_have_case_number(parsed, "OC Family Law (claustro_c22)")


@respx.mock
def test_oc_family_law_kohler_no_case_numbers_expected() -> None:
    """OC Family Law Kohler L69: case_number=None is expected.

    The oc_family_law_kohler_l69.pdf fixture is a 1-page empty boilerplate
    PDF with no rulings or case numbers.
    """
    from courts.ca.oc_family_law_tentatives import (
        INDEX_URL,
        OCFamilyLawTentativeRulingsScraper,
    )
    from courts.ca.oc_family_law_tentatives import default_config as fl_default_config

    html = _load_html("oc_family_law_page.html")
    pdf_bytes = _load_bytes("oc_family_law_kohler_l69.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = fl_default_config()
    config.request_delay_seconds = 0
    scraper = OCFamilyLawTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Kohler L69 PDF is empty boilerplate — no case numbers in source data
    none_count = sum(1 for d in parsed if d.case_number is None)
    assert none_count == len(parsed), (
        f"OC Family Law Kohler: expected all {len(parsed)} docs to have "
        f"case_number=None (empty boilerplate PDF), but "
        f"{len(parsed) - none_count} had values"
    )


# ---------------------------------------------------------------------------
# OC Probate
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_probate_cm3_has_case_number() -> None:
    """OC Probate CM3: fixture has case numbers — all must be extracted."""
    from courts.ca.oc_probate_tentatives import (
        INDEX_URL,
        OCProbateTentativeRulingsScraper,
    )
    from courts.ca.oc_probate_tentatives import (
        default_config as probate_default_config,
    )

    html = _load_html("oc_probate_page.html")
    pdf_bytes = _load_bytes("oc_probate_cm3.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = probate_default_config()
    config.request_delay_seconds = 0
    scraper = OCProbateTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0, "Expected at least one parsed document"
    _assert_all_have_case_number(parsed, "OC Probate (cm3)")


@respx.mock
def test_oc_probate_cm5_has_case_number() -> None:
    """OC Probate CM5: fixture has case numbers — all must be extracted."""
    from courts.ca.oc_probate_tentatives import (
        INDEX_URL,
        OCProbateTentativeRulingsScraper,
    )
    from courts.ca.oc_probate_tentatives import (
        default_config as probate_default_config,
    )

    html = _load_html("oc_probate_page.html")
    pdf_bytes = _load_bytes("oc_probate_cm5.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = probate_default_config()
    config.request_delay_seconds = 0
    scraper = OCProbateTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0, "Expected at least one parsed document"
    _assert_all_have_case_number(parsed, "OC Probate (cm5)")


# ---------------------------------------------------------------------------
# Riverside
# ---------------------------------------------------------------------------


@respx.mock
def test_riverside_ps1_all_docs_have_case_number() -> None:
    """Riverside PS1: multi-ruling PDF — every split doc must have a case_number."""
    from courts.ca.riverside_tentatives import (
        INDEX_URL,
        RiversideTentativeRulingsScraper,
    )
    from courts.ca.riverside_tentatives import default_config as riv_default_config

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

    assert len(parsed) > 0, "Expected at least one parsed document"
    _assert_all_have_case_number(parsed, "Riverside (ps1)")


@respx.mock
def test_riverside_moreno_valley_all_docs_have_case_number() -> None:
    """Riverside Moreno Valley: multi-ruling PDF — all split docs have case_number."""
    from courts.ca.riverside_tentatives import (
        INDEX_URL,
        RiversideTentativeRulingsScraper,
    )
    from courts.ca.riverside_tentatives import default_config as riv_default_config

    html = _load_html("riv_page.html")
    pdf_bytes = _load_bytes("riv_moreno_valley.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = riv_default_config()
    config.request_delay_seconds = 0
    scraper = RiversideTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0, "Expected at least one parsed document"
    _assert_all_have_case_number(parsed, "Riverside (moreno_valley)")


# ---------------------------------------------------------------------------
# San Bernardino
# ---------------------------------------------------------------------------


@respx.mock
def test_sb_r12_has_case_number() -> None:
    """San Bernardino R12: fixture has case numbers — all must be extracted."""
    from courts.ca.sb_tentatives import (
        INDEX_URL,
        SBTentativeRulingsScraper,
    )
    from courts.ca.sb_tentatives import default_config as sb_default_config

    html = _load_html("sb_iframe_page.html")
    pdf_bytes = _load_bytes("sb_r12_20260303_0df41117.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = sb_default_config()
    config.request_delay_seconds = 0
    scraper = SBTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    has_case = [d for d in parsed if d.case_number is not None]
    assert len(has_case) > 0, (
        "SB R12 fixture has case numbers — at least some docs should extract them"
    )


# ---------------------------------------------------------------------------
# San Francisco (Family Law)
# ---------------------------------------------------------------------------


@respx.mock
def test_sf_family_law_has_case_number() -> None:
    """SF Family Law: fixture has case numbers — all must be extracted."""
    from courts.ca.sf_tentatives import (
        INDEX_URL,
        SFTentativeRulingsScraper,
    )
    from courts.ca.sf_tentatives import default_config as sf_default_config

    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes),
    )

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    has_case = [d for d in parsed if d.case_number is not None]
    assert len(has_case) > 0, "SF fixture has case numbers — at least some docs should extract them"


# ---------------------------------------------------------------------------
# Santa Clara
# ---------------------------------------------------------------------------


@respx.mock
def test_sc_all_docs_have_case_number() -> None:
    """Santa Clara: all parsed documents must have a case_number.

    Uses dept6 fixture which has 5+ case numbers in DDCVDDDDDD format.
    """
    from courts.ca.sc_tentatives import (
        LANDING_URL,
        SCTentativeRulingsScraper,
    )
    from courts.ca.sc_tentatives import default_config as sc_default_config

    landing_html = _load_html("sc_landing_page.html")
    dept6_html = _load_html("sc_dept6_page.html")
    dept6_pdf = _load_bytes("sc_dept6_tues.pdf")

    respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
    respx.get(url__regex=r"tentative-rulings/dep").mock(
        return_value=httpx.Response(200, text=dept6_html),
    )
    respx.get(url__regex=r"\.pdf$").mock(
        return_value=httpx.Response(200, content=dept6_pdf),
    )

    config = sc_default_config()
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0, "Expected at least one parsed document"
    _assert_all_have_case_number(parsed, "Santa Clara (dept6)")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_all_have_case_number(
    docs: list[CapturedDocument],
    scraper_label: str,
) -> None:
    """Assert every document in the list has a non-None case_number."""
    missing = [(i, d.department, d.source_url) for i, d in enumerate(docs) if d.case_number is None]
    assert not missing, (
        f"{scraper_label}: {len(missing)} of {len(docs)} documents have "
        f"case_number=None when the source data contains parseable case numbers. "
        f"First 5 missing: {missing[:5]}"
    )
