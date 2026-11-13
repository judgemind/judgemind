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


# ---------------------------------------------------------------------------
# PdfLinkScraper courts: OC civil, OC probate, OC Family Law, Fresno,
# Riverside, San Bernardino, SF Family Law (#4912)
# ---------------------------------------------------------------------------
#
# The listing is the index PDF links that survive the court's link-text /
# filename filter, so each edit renames the part of the page that filter
# keys on: a court relabelling its links skips every one of them.


def _pdf_courts() -> dict[str, tuple[type, ScraperConfig, str, str, str]]:
    """court -> (scraper class, config, fixture, old, new) for one layout edit."""
    from courts.ca import (
        fresno_tentatives,
        oc_family_law_tentatives,
        oc_probate_tentatives,
        oc_tentatives,
        riverside_tentatives,
        sb_tentatives,
        sf_tentatives,
    )

    return {
        "oc-civil": (
            oc_tentatives.OCTentativeRulingsScraper,
            oc_tentatives.default_config(),
            "oc_civil_page.html",
            "Dept",
            "Courtroom",
        ),
        "oc-probate": (
            oc_probate_tentatives.OCProbateTentativeRulingsScraper,
            oc_probate_tentatives.default_config(),
            "oc_probate_page.html",
            "Law and Motion",
            "Probate Calendar",
        ),
        "oc-family-law": (
            oc_family_law_tentatives.OCFamilyLawTentativeRulingsScraper,
            oc_family_law_tentatives.default_config(),
            "oc_family_law_page.html",
            ".pdf",
            ".aspx",
        ),
        "fresno": (
            fresno_tentatives.FresnoTentativeRulingsScraper,
            fresno_tentatives.default_config(),
            "fresno_index_page.html",
            "-dept-",
            "-courtroom-",
        ),
        "riverside": (
            riverside_tentatives.RiversideTentativeRulingsScraper,
            riverside_tentatives.default_config(),
            "riv_page.html",
            ">Department ",
            ">Courtroom ",
        ),
        "sb": (
            sb_tentatives.SBTentativeRulingsScraper,
            sb_tentatives.default_config(),
            "sb_iframe_page.html",
            ">CV",
            ">CIV",
        ),
        "sf-family-law": (
            sf_tentatives.SFTentativeRulingsScraper,
            sf_tentatives.default_config(),
            "sf_family_law_page.html",
            " Tentative Rulings ",
            " Rulings ",
        ),
    }


@pytest.mark.parametrize("court", list(_pdf_courts()))
@respx.mock
def test_pdf_link_court_empty_listing_fails_run(court: str) -> None:
    """Every index link is relabelled, so none passes the court's filter."""
    cls, config, fixture, old, new = _pdf_courts()[court]
    scraper = cls(config=_fast(config))
    html = _mutate(_load(fixture), old, new)
    route = respx.get(scraper._pdf_config.index_url).mock(
        return_value=httpx.Response(200, text=html)
    )

    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0
    message = health.error_message or ""
    assert f"empty listing: {config.scraper_id} index PDF links" in message
    # The reason survives in full ahead of the URL (#4912).
    if court == "oc-family-law":
        # Its links stopped ending in .pdf; the panel still names 2 officers.
        assert "Family Law Panel lists 2 judicial officer entries" in message
    else:
        assert "PDF link(s), none matching the ruling-link filters" in message
    # A layout change is not retried within the run, and no PDF was fetched.
    assert route.call_count == 1
    assert len(respx.calls) == 1


@pytest.mark.parametrize("court", list(_pdf_courts()))
def test_pdf_link_court_unmodified_index_is_not_empty_listing(court: str) -> None:
    """Companion: the real index parses to a non-empty ruling-link listing,
    so the edit above is what empties it."""
    from courts.ca.pdf_link_scraper import _extract_pdf_links

    cls, config, fixture, _old, _new = _pdf_courts()[court]
    scraper = cls(config=config)
    pc = scraper._pdf_config
    links = _extract_pdf_links(_load(fixture), pc.index_url, pc.pdf_base_url)
    assert len(scraper._ruling_links(links)) > 0


@respx.mock
def test_oc_family_law_empty_panel_is_not_empty_listing_failure() -> None:
    """The live empty Family Law Panel (since 2026-07-21) stays green."""
    from courts.ca.oc_family_law_tentatives import (
        INDEX_URL as OC_FL_INDEX_URL,
    )
    from courts.ca.oc_family_law_tentatives import (
        OCFamilyLawTentativeRulingsScraper,
    )
    from courts.ca.oc_family_law_tentatives import default_config as oc_fl_config

    respx.get(OC_FL_INDEX_URL).mock(
        return_value=httpx.Response(200, text=_load("oc_family_law_page_empty_panel.html"))
    )

    health = OCFamilyLawTentativeRulingsScraper(config=_fast(oc_fl_config())).run()

    assert health.success is True
    assert health.records_captured == 0


# ---------------------------------------------------------------------------
# SF civil: one listing per RulingID, on the tally (#4912)
# ---------------------------------------------------------------------------


def _sf_civil_body() -> str:
    return _load("sf-human-captures/sf-civil-api-response-rid10-2026-03-23.json")


@respx.mock
def test_sf_civil_empty_listing_fails_run() -> None:
    """The "Case Number:" row label is renamed: the API still reports 12
    rulings but none parse, on every RulingID."""
    from courts.ca.sf_civil_tentatives import CIVIL_REST_BASE, SFCivilTentativeRulingsScraper
    from courts.ca.sf_civil_tentatives import default_config as sf_civil_config

    body = _mutate(_sf_civil_body(), "Case Number:", "Case No.")
    respx.get(url__startswith=CIVIL_REST_BASE).mock(return_value=httpx.Response(200, text=body))

    health = SFCivilTentativeRulingsScraper(
        config=_fast(sf_civil_config()), session_id="test-session-0123456789"
    ).run()

    assert health.success is False
    assert health.records_captured == 0
    message = health.error_message or ""
    assert "empty listing: SF civil rulings for RulingID" in message
    assert "unexpected result: API reports 12 rulings but none parsed" in message
    assert "test-session" not in message


@respx.mock
def test_sf_civil_documented_empty_answer_stays_green() -> None:
    """``{"result": [0, ""]}`` on every RulingID is a quiet day."""
    from courts.ca.sf_civil_tentatives import CIVIL_REST_BASE, SFCivilTentativeRulingsScraper
    from courts.ca.sf_civil_tentatives import default_config as sf_civil_config

    respx.get(url__startswith=CIVIL_REST_BASE).mock(
        return_value=httpx.Response(200, text='{"result": [0, ""]}')
    )

    health = SFCivilTentativeRulingsScraper(
        config=_fast(sf_civil_config()), session_id="test-session-0123456789"
    ).run()

    assert health.success is True


def test_sf_civil_unmodified_response_is_not_empty_listing() -> None:
    from courts.ca.sf_civil_tentatives import parse_api_response

    assert len(parse_api_response(_sf_civil_body())) > 0


# ---------------------------------------------------------------------------
# SD calendar: one listing per calendar page, on the tally (#4912)
# ---------------------------------------------------------------------------


@respx.mock
def test_sd_calendar_empty_listing_fails_run() -> None:
    """The hearing-table class is renamed on every page: departments render
    but no hearing rows parse."""
    from courts.ca.sd_calendar import SDCalendarScraper
    from courts.ca.sd_calendar import default_config as sd_cal_config

    html = _mutate(_load("sd_calendar_central.html"), 'class="tables"', 'class="calendar-table"')
    respx.get(url__regex=r"/calendar/").mock(return_value=httpx.Response(200, text=html))

    health = SDCalendarScraper(config=_fast(sd_cal_config()), day_numbers=[1]).run()

    assert health.success is False
    message = health.error_message or ""
    assert "empty listing: SD " in message
    assert "calendar page has departments but no hearing rows parsed" in message


def test_sd_calendar_unmodified_page_is_not_empty_listing() -> None:
    from courts.ca.sd_calendar import parse_calendar_page

    assert len(parse_calendar_page(_load("sd_calendar_central.html"))) > 0


# ---------------------------------------------------------------------------
# CC portal: judge dropdown (whole run) and per-judge listings (tally)
# ---------------------------------------------------------------------------


def _cc_portal_config() -> ScraperConfig:
    from courts.ca.cc_tentatives_portal import default_config as cc_portal_config

    return _fast(cc_portal_config())


@respx.mock
def test_cc_portal_judge_dropdown_empty_listing_fails_run() -> None:
    """The judge ``<select>`` is renamed on the form page."""
    from courts.ca.cc_tentatives_portal import FORM_URL, CCTentativesPortalScraper

    html = _mutate(_load("cc_portal/form.html"), 'name="field_judge_target_id"', 'name="judge"')
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=html))

    health = CCTentativesPortalScraper(config=_cc_portal_config()).run()

    assert health.success is False
    assert "empty listing: CC portal judge dropdown options" in (health.error_message or "")


@respx.mock
def test_cc_portal_listing_empty_listing_fails_run() -> None:
    """Every judge's results ``<table>`` becomes a ``<div>``: the page is
    populated but no rows parse, and there is no no-results marker."""
    from courts.ca.cc_tentatives_portal import (
        FORM_URL,
        LISTING_URL,
        CCTentativesPortalScraper,
        _parse_judge_dropdown,
    )

    form = _load("cc_portal/form.html")
    assert _parse_judge_dropdown(form)
    listing = _mutate(
        _load("cc_portal/listing_reyes_live.html"), '<table class="cols-2">', '<div class="cols-2">'
    )
    respx.get(LISTING_URL, params__contains={"field_judge_target_id": None}).mock(
        return_value=httpx.Response(200, text=listing)
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=form))

    health = CCTentativesPortalScraper(config=_cc_portal_config()).run()

    assert health.success is False
    assert health.records_captured == 0
    message = health.error_message or ""
    assert "empty listing: CC portal listing rows for judge" in message
    # The full reason survives FetchTally's 300-char truncation (#4912).
    assert "(no result rows, no no-results marker)" in message


def test_cc_portal_unmodified_pages_are_not_empty_listings() -> None:
    from courts.ca.cc_tentatives_portal import _parse_judge_dropdown, _parse_listing_table

    assert len(_parse_judge_dropdown(_load("cc_portal/form.html"))) > 0
    assert len(_parse_listing_table(_load("cc_portal/listing_reyes_live.html"))) > 0


# ---------------------------------------------------------------------------
# Governor appointments: first search page (#4912)
# ---------------------------------------------------------------------------


@respx.mock
def test_governor_search_empty_listing_fails_run() -> None:
    """Press releases move off gov.ca.gov, so no result passes the filter."""
    from courts.ca.governor_appointments import GovernorAppointmentsScraper
    from courts.ca.governor_appointments import default_config as gov_config

    html = _mutate(
        _load("governor_appointments/search_results_page1.html"),
        "https://www.gov.ca.gov/20",
        "https://www.ca.gov/newsroom/20",
    )
    respx.get(url__startswith="https://www.gov.ca.gov/").mock(
        return_value=httpx.Response(200, text=html)
    )

    health = GovernorAppointmentsScraper(config=_fast(gov_config()), max_pages=1).run()

    assert health.success is False
    assert "empty listing: governor judicial-appointment search results" in (
        health.error_message or ""
    )


def test_governor_unmodified_search_page_is_not_empty_listing() -> None:
    from courts.ca.governor_appointments import SEARCH_URL as GOV_SEARCH_URL
    from courts.ca.governor_appointments import extract_search_results

    html = _load("governor_appointments/search_results_page1.html")
    assert len(extract_search_results(html, GOV_SEARCH_URL)) > 0
