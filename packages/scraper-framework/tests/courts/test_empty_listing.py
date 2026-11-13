"""Empty index / listing pages fail the run unless legitimately empty (#4736).

When a court changes its HTML, the index selector matches nothing, the run
makes zero fetch attempts and, before #4736, recorded ``success=True`` with
0 records.  ``BaseScraper._check_listing`` turns that into ``success=False``
with an ``empty listing:`` error, unless the page carries the court's
recognized "no rulings posted" marker.

Every test starts from a real capture under ``tests/fixtures/`` (#4800).
The layout-change cases apply one targeted edit to it: the selector the
scraper keys on is renamed, which is exactly what a court redesign does.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.cc_tentatives import INDEX_URL as CC_INDEX_URL
from courts.ca.cc_tentatives import CCTentativeRulingsScraper
from courts.ca.cc_tentatives import default_config as cc_default_config
from courts.ca.la_tentatives import (
    APPELLATE_URL,
    CIVIL_URL,
    LAAppellateTentativeRulingsScraper,
    LATentativeRulingsScraper,
    default_config_appellate,
)
from courts.ca.la_tentatives import default_config as la_default_config
from courts.ca.sc_tentatives import LANDING_URL, SCTentativeRulingsScraper
from courts.ca.sc_tentatives import default_config as sc_default_config
from courts.ca.ventura_tentatives import SEARCH_URL, VenturaTentativeRulingsScraper
from courts.ca.ventura_tentatives import default_config as ventura_default_config
from framework import ScraperConfig

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mutate(html: str, old: str, new: str) -> str:
    """Apply one layout-change edit, asserting it actually changed the page."""
    assert old in html, f"fixture no longer contains {old!r}"
    return html.replace(old, new)


def _fast(config: ScraperConfig) -> ScraperConfig:
    config.request_delay_seconds = 0
    return config


# ---------------------------------------------------------------------------
# LA civil
# ---------------------------------------------------------------------------


@respx.mock
def test_la_civil_empty_listing_fails_run() -> None:
    """The ruling dropdown is renamed: no options, no empty-state marker."""
    html = _mutate(_load("la_main_page.html"), "List2DeptDate", "List2DeptDateNew")
    route = respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=html))

    health = LATentativeRulingsScraper(config=_fast(la_default_config())).run()

    assert health.success is False
    assert health.records_captured == 0
    assert "empty listing: LA civil ruling dropdown options" in (health.error_message or "")
    # A layout change is not retried within the run (#4736).
    assert route.call_count == 1


@respx.mock
def test_la_civil_empty_listing_with_no_rulings_marker_succeeds() -> None:
    """The portal's "No rulings have been published" table is a legit empty."""
    html = _mutate(_load("la_main_page.html"), "List2DeptDate", "List2DeptDateNew")
    html = html.replace("</body>", _no_ruling_table() + "</body>")
    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=html))

    health = LATentativeRulingsScraper(config=_fast(la_default_config())).run()

    assert health.success is True
    assert health.records_captured == 0


def _no_ruling_table() -> str:
    """The NoRuling table, copied verbatim from la_appellate_main_empty.html."""
    html = _load("la_appellate_main_empty.html")
    start = html.index('<table id="siteMasterHolder_basicBodyHolder_NoRuling"')
    end = html.index("</table>", start) + len("</table>")
    return html[start:end]


# ---------------------------------------------------------------------------
# LA appellate
# ---------------------------------------------------------------------------


@respx.mock
def test_la_appellate_empty_state_page_stays_successful() -> None:
    """The live empty-state page is the common case and stays green."""
    respx.get(APPELLATE_URL).mock(
        return_value=httpx.Response(200, text=_load("la_appellate_main_empty.html"))
    )

    health = LAAppellateTentativeRulingsScraper(config=_fast(default_config_appellate())).run()

    assert health.success is True
    assert health.records_captured == 0


@respx.mock
def test_la_appellate_empty_listing_without_marker_fails_run() -> None:
    """The empty-state text is gone and there is still no dropdown."""
    html = _mutate(
        _load("la_appellate_main_empty.html"),
        "No rulings have been published at this time.",
        "Please check back later.",
    )
    respx.get(APPELLATE_URL).mock(return_value=httpx.Response(200, text=html))

    health = LAAppellateTentativeRulingsScraper(config=_fast(default_config_appellate())).run()

    assert health.success is False
    assert "empty listing: LA appellate ruling dropdown options" in (health.error_message or "")


# ---------------------------------------------------------------------------
# Santa Clara
# ---------------------------------------------------------------------------


@respx.mock
def test_sc_empty_listing_fails_run() -> None:
    """Department links are relabelled, so none match ``Dept. N``."""
    html = _mutate(_load("sc_landing_page.html"), ">Dept. ", ">Department ")
    respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=html))

    health = SCTentativeRulingsScraper(config=_fast(sc_default_config())).run()

    assert health.success is False
    assert health.records_captured == 0
    assert "empty listing: SC landing page department links" in (health.error_message or "")


# ---------------------------------------------------------------------------
# Contra Costa PDF
# ---------------------------------------------------------------------------


@respx.mock
def test_cc_empty_listing_fails_run() -> None:
    """The ``tentative-ruling`` link class is renamed: no PDF links found."""
    html = _mutate(_load("cc_index_page_mini.html"), "tentative-ruling", "ruling-link")
    respx.get(CC_INDEX_URL).mock(return_value=httpx.Response(200, text=html))

    health = CCTentativeRulingsScraper(config=_fast(cc_default_config())).run()

    assert health.success is False
    assert health.records_captured == 0
    assert "empty listing: CC tentative-ruling PDF links" in (health.error_message or "")


# ---------------------------------------------------------------------------
# Ventura
# ---------------------------------------------------------------------------


@respx.mock
def test_ventura_empty_listing_fails_run() -> None:
    """The "No results found" marker class is renamed: no table, no marker."""
    page = _mutate(_load("ventura_no_results_live_4748.html"), 'class="noresults"', 'class="empty"')
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, text=_load("ventura_search_page.html"))
    )
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=page))

    health = VenturaTentativeRulingsScraper(
        config=_fast(ventura_default_config()), search_dates=[datetime(2026, 9, 27)]
    ).run()

    assert health.success is False
    assert health.records_captured == 0
    assert "empty listing: Ventura search results for 9/27/2026" in (health.error_message or "")


@respx.mock
def test_ventura_empty_listing_on_one_date_keeps_other_dates_green() -> None:
    """Per-date listings go on the tally: one live empty date plus one
    unrecognized page is not an all-failed run."""
    unrecognized = _mutate(
        _load("ventura_no_results_live_4748.html"), 'class="noresults"', 'class="empty"'
    )
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, text=_load("ventura_search_page.html"))
    )
    respx.post(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, text=_load("ventura_no_results_live_4748.html")),
            httpx.Response(200, text=unrecognized),
        ]
    )

    health = VenturaTentativeRulingsScraper(
        config=_fast(ventura_default_config()),
        search_dates=[datetime(2026, 9, 25), datetime(2026, 9, 27)],
    ).run()

    assert health.success is True


@pytest.mark.parametrize(
    "fixture",
    ["la_main_page.html", "sc_landing_page.html", "cc_index_page_mini.html"],
)
def test_unmodified_fixtures_are_not_empty_listings(fixture: str) -> None:
    """Guard against the mutations above passing vacuously: the real pages
    parse to a non-empty listing."""
    from courts.ca.cc_tentatives import BASE_URL as CC_BASE_URL
    from courts.ca.cc_tentatives import _cc_extract_links
    from courts.ca.la_tentatives import _parse_dropdown_options
    from courts.ca.sc_tentatives import extract_departments

    html = _load(fixture)
    parsers = {
        "la_main_page.html": _parse_dropdown_options,
        "sc_landing_page.html": extract_departments,
        "cc_index_page_mini.html": lambda h: _cc_extract_links(h, CC_BASE_URL),
    }
    assert len(parsers[fixture](html)) > 0
