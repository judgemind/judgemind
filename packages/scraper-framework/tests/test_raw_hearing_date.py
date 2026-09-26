"""Hearing-date parity for events built straight from S3 (#4774).

``rebuild_db`` and prefix-mode ``reingest_from_s3`` build ingestion events
from the raw S3 object: no ``hearing_date``, and a ``source_url`` only when
the object's capture metadata has one.  The live scrapers set the date from
a filename or a labelled header, so before this fix a rebuilt or reingested
document got NULL or body-text dates where the live capture of the same
bytes was right (#4667 Santa Clara, #4769 Contra Costa).

Each CA scraper now owns its derivation through
``BaseScraper.hearing_date_for_raw``; the worker calls it before any split.
The ``test_prefix_event_header_hearing_date*`` tests drive a prefix-style
event per scraper through ``IngestionWorker.process_event`` and check the
date handed to the split.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from courts.ca.pdf_link_scraper import _extract_pdf_text, source_url_filename
from framework.base import BaseScraper
from ingestion.raw_hearing_date import _scraper_index, raw_hearing_date
from ingestion.worker import IngestionWorker

FIXTURES = Path(__file__).parent / "fixtures"


def _pdf_text(name: str) -> str:
    return _extract_pdf_text((FIXTURES / name).read_bytes())


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _prefix_event(county: str, text: str, **overrides: Any) -> dict[str, Any]:
    """An event as prefix-mode reingest builds it: synthetic scraper_id, no
    hearing_date, and whatever capture metadata the S3 object carried."""
    slug = county.lower().replace(" ", "_")
    event: dict[str, Any] = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000004774",
        "scraper_id": f"reingest-ca-{slug}",
        "state": "CA",
        "county": county,
        "court": "Superior Court",
        "source_url": "",
        "content_format": "pdf",
        "content_hash": "h4774",
        "s3_key": None,
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": text,
        "hearing_date": None,
        "capture_timestamp": "2026-03-01T12:00:00+00:00",
    }
    event.update(overrides)
    return event


def _split_input(event: dict[str, Any]) -> dict[str, Any]:
    """Run ``process_event`` and return the event handed to the split."""
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    seen: list[dict[str, Any]] = []

    def fake_split(event_data: dict[str, Any], *_a: object, **_k: object) -> bool:
        seen.append(event_data)
        return True

    worker._llm_split_document = fake_split  # type: ignore[method-assign]
    worker.process_event(event)
    assert len(seen) == 1
    return seen[0]


_CC_HEADER = (
    "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\nDEPARTMENT 16\nHEARING DATE: 04/09/2026\n"
)
_VENTURA_PROBATE_NOTES = (
    "SUPERIOR COURT OF CALIFORNIA\n"
    "COUNTY OF VENTURA\n"
    "Probate Notes\n"
    "2024PRCE029972: IN THE MATTER OF DOROTHY MARWICK\n"
    "05/05/2026 in Department J6\n"
    "Hearing on Petition for Authority to Sell\n"
    "This was last heard on 04/14/26.\n"
)
_SF_URL_ENCODED = (
    "https://webapps.sftc.org/ufctr/files/Dept%20403/Tentative%20Rulings/"
    "403%20Tentative%20Rulings%203.03.2026.pdf"
)
_SF_URL_RAW = (
    "https://webapps.sftc.org/ufctr/files/Dept 414/Tentative Rulings/Thursday/"
    "414 Tentative Rulings 9.24.2026.pdf"
)


# (id, county, text factory, event overrides, expected date)
_CASES: list[tuple[str, str, Any, dict[str, Any], str]] = [
    ("santa_clara-header", "Santa Clara", lambda: _pdf_text("sc_dept16_wed.pdf"), {}, "2026-03-04"),
    (
        "santa_clara-stale_year",
        "Santa Clara",
        lambda: "Department 16\nDATE: 9/16/2025 TIME: 9:00 A.M.\nbody\n",
        {"capture_timestamp": "2026-09-15T01:43:00Z"},
        "2026-09-16",
    ),
    (
        "contra_costa-header",
        "Contra Costa",
        lambda: _pdf_text("cc_dept16_031126.pdf"),
        {},
        "2026-03-11",
    ),
    (
        "contra_costa-filename_first",
        "Contra Costa",
        lambda: _CC_HEADER,
        {
            "source_url": "https://retired.cc-courts.org/civil/TR/Department 16/16_041026.pdf",
            "capture_scraper_id": "ca-cc-tentatives",
        },
        "2026-04-10",
    ),
    (
        "fresno-header",
        "Fresno",
        lambda: _pdf_text("fresno_403_20260310_d019042f.pdf"),
        {},
        "2026-03-10",
    ),
    (
        "fresno-filename",
        "Fresno",
        lambda: "Department 403\nbody without a date label\n",
        {
            "source_url": "https://www.fresno.courts.ca.gov/system/files/tentative-rulings/"
            "03-10-26-dept-403-hbv.pdf",
            "capture_scraper_id": "ca-fresno-tentatives-civil",
        },
        "2026-03-10",
    ),
    ("riverside-header", "Riverside", lambda: _pdf_text("riv_ps1.pdf"), {}, "2026-03-02"),
    (
        "san_bernardino-filename",
        "San Bernardino",
        lambda: _pdf_text("sb_s36_20260303_a6da3fb7.pdf"),
        {
            "source_url": "https://old.sb-court.org/DesktopModules/TentativeRulings/"
            "TentativeRulings/CVS36030326.pdf",
            "capture_scraper_id": "ca-sb-tentatives-civil",
        },
        "2026-03-03",
    ),
    (
        "san_bernardino-title_line",
        "San Bernardino",
        lambda: "TENTATIVE RULINGS 6-1-26\nDepartment R17- Judge Gilbert G. Ochoa\nbody\n",
        {},
        "2026-06-01",
    ),
    (
        "san_francisco-filename_encoded",
        "San Francisco",
        lambda: _pdf_text("sf_dept403_ruling.pdf"),
        {"source_url": _SF_URL_ENCODED, "capture_scraper_id": "ca-sf-tentatives-family-law"},
        "2026-03-03",
    ),
    (
        "san_francisco-filename_raw",
        "San Francisco",
        lambda: _pdf_text("sf_dept403_ruling.pdf"),
        {"source_url": _SF_URL_RAW, "capture_scraper_id": "ca-sf-tentatives-family-law"},
        "2026-09-24",
    ),
    (
        "san_francisco-civil_envelope",
        "San Francisco",
        lambda: json.dumps(
            {"ruling_id": "r1", "department": "302", "court_date": "2026-03-23 09:00 AM"}
        ),
        {"content_format": "html", "capture_scraper_id": "ca-sf-tentatives-civil"},
        "2026-03-23",
    ),
    (
        "orange-family_law",
        "Orange",
        lambda: _pdf_text("oc_family_law_claustro_c22.pdf"),
        {"capture_scraper_id": "ca-oc-tentatives-family-law"},
        "2025-12-05",
    ),
    (
        "orange-probate",
        "Orange",
        lambda: _pdf_text("oc_probate_cm3.pdf"),
        {"capture_scraper_id": "ca-oc-tentatives-probate"},
        "2026-03-04",
    ),
    (
        "los_angeles-html_header",
        "Los Angeles",
        lambda: _html("la_ruling_response.html"),
        {"content_format": "html"},
        "2026-03-02",
    ),
    (
        "san_diego-calendar",
        "San Diego",
        lambda: _html("sd_calendar_full.html"),
        {"content_format": "html", "capture_scraper_id": "ca-sd-calendar"},
        "2026-03-13",
    ),
    (
        "san_diego-roa",
        "San Diego",
        lambda: _html("sd_roa_demurrer.html"),
        {"content_format": "html", "capture_scraper_id": "ca-sd-tentatives"},
        "2026-03-13",
    ),
    ("ventura-probate_notes", "Ventura", lambda: _VENTURA_PROBATE_NOTES, {}, "2026-05-05"),
    (
        "ventura-html",
        "Ventura",
        lambda: _html("ventura_ruling_html_msj.html"),
        {"content_format": "html", "capture_scraper_id": "ca-ventura-tentatives"},
        "2026-03-11",
    ),
]


@pytest.mark.parametrize(
    ("county", "text_factory", "overrides", "expected"),
    [pytest.param(c, t, o, e, id=i) for i, c, t, o, e in _CASES],
)
def test_prefix_event_header_hearing_date(
    county: str, text_factory: Any, overrides: dict[str, Any], expected: str
) -> None:
    """A prefix-style event gets the live scraper's hearing date before the split."""
    event = _split_input(_prefix_event(county, text_factory(), **overrides))
    assert event["hearing_date"] == expected


# (id, county, text, event overrides) — each must yield no hearing date.
_NONE_CASES: list[tuple[str, str, str, dict[str, Any]]] = [
    (
        "orange_civil-live_sets_none",
        "Orange",
        "TENTATIVE RULINGS\nDate: March 12, 2026\n",
        {"capture_scraper_id": "ca-oc-tentatives-civil"},
    ),
    (
        "ventura-body_date_only",
        "Ventura",
        "SUPERIOR COURT OF CALIFORNIA\nCOUNTY OF VENTURA\nThis was last heard on 04/14/2026.\n",
        {},
    ),
    (
        "san_francisco-no_filename",
        "San Francisco",
        "Case Number: FPT-25-378624\nHearing Date: March 3, 2026\n",
        {"capture_scraper_id": "ca-sf-tentatives-family-law"},
    ),
    (
        "san_bernardino-body_date_only",
        "San Bernardino",
        "Department R17- Judge Gilbert G. Ochoa\nThe hearing is continued to June 9, 2026.\n",
        {},
    ),
    (
        "contra_costa-body_date_only",
        "Contra Costa",
        "DEPARTMENT 10\nThe hearing is continued to May 4, 2026 at 9:00 a.m.\n",
        {},
    ),
    (
        "santa_clara-body_date_only",
        "Santa Clara",
        "Department 12\nThe hearing on August 27, 2026.\n",
        {},
    ),
    ("unknown_county", "Nowhere", _CC_HEADER, {}),
]


@pytest.mark.parametrize(
    ("county", "text", "overrides"),
    [pytest.param(c, t, o, id=i) for i, c, t, o in _NONE_CASES],
)
def test_prefix_event_header_hearing_date_none_over_guess(
    county: str, text: str, overrides: dict[str, Any]
) -> None:
    """No filename or labelled header: None, never a body date (#4682)."""
    event = _split_input(_prefix_event(county, text, **overrides))
    assert event["hearing_date"] is None


def test_prefix_event_header_hearing_date_live_date_still_wins() -> None:
    event = _split_input(
        _prefix_event(
            "Contra Costa",
            _CC_HEADER,
            hearing_date="2026-04-11",
            scraper_id="ca-cc-tentatives",
        )
    )
    assert event["hearing_date"] == "2026-04-11"


# ---------------------------------------------------------------------------
# Resolver behaviour
# ---------------------------------------------------------------------------


class _FakeA(BaseScraper):
    @classmethod
    def hearing_date_for_raw(cls, text: str, **_kw: Any) -> datetime | None:
        return datetime(2026, 1, 2)


class _FakeB(BaseScraper):
    @classmethod
    def hearing_date_for_raw(cls, text: str, **_kw: Any) -> datetime | None:
        return datetime(2026, 1, 3)


class _FakeNone(BaseScraper):
    pass


class _FakeRaises(BaseScraper):
    @classmethod
    def hearing_date_for_raw(cls, text: str, **_kw: Any) -> datetime | None:
        raise ValueError("boom")


class _FakeDate(BaseScraper):
    @classmethod
    def hearing_date_for_raw(cls, text: str, **_kw: Any) -> Any:
        return date(2026, 2, 3)


class _FakeEcho(BaseScraper):
    """Returns the capture timestamp it was handed."""

    @classmethod
    def hearing_date_for_raw(
        cls, text: str, *, capture_timestamp: datetime | None = None, **_kw: Any
    ) -> datetime | None:
        return capture_timestamp


def _patched_index(
    by_id: dict[str, type], by_county: dict[tuple[str, str], tuple[type, ...]]
) -> Any:
    return patch("ingestion.raw_hearing_date._scraper_index", return_value=(by_id, by_county))


class TestResolver:
    _EV = {"state": "CA", "county": "Test", "scraper_id": "rebuild-ca-test"}

    def test_county_candidates_disagreeing_yield_none(self) -> None:
        with _patched_index({}, {("CA", "TEST"): (_FakeA, _FakeB)}):
            assert raw_hearing_date(dict(self._EV), "x") is None

    def test_county_candidates_agreeing_yield_the_date(self) -> None:
        with _patched_index({}, {("CA", "TEST"): (_FakeA, _FakeA)}):
            assert raw_hearing_date(dict(self._EV), "x") == "2026-01-02"

    def test_county_scraper_without_hook_vetoes_fallback(self) -> None:
        """Orange civil reads no date from its raws; without capture metadata a
        raw may be one of its files, so another scraper's parser must not date
        it (#4682)."""
        with _patched_index({}, {("CA", "TEST"): (_FakeA, _FakeNone)}):
            assert raw_hearing_date(dict(self._EV), "x") is None

    def test_hook_returning_a_date_object(self) -> None:
        with _patched_index({"ca-d": _FakeDate}, {}):
            assert raw_hearing_date({**self._EV, "capture_scraper_id": "ca-d"}, "x") == (
                "2026-02-03"
            )

    @pytest.mark.parametrize(
        "capture_timestamp",
        [datetime(2026, 9, 15, 1, 43), "not-a-date", None, "2026-09-15T01:43:00Z"],
    )
    def test_capture_timestamp_shapes_reach_the_hook(self, capture_timestamp: Any) -> None:
        with _patched_index({"ca-e": _FakeEcho}, {}):
            event = {
                **self._EV,
                "capture_scraper_id": "ca-e",
                "capture_timestamp": capture_timestamp,
            }
            result = raw_hearing_date(event, "x")
        if capture_timestamp in (None, "not-a-date"):
            assert result is None
        else:
            assert result == "2026-09-15"

    def test_capture_scraper_id_is_authoritative_even_when_none(self) -> None:
        by_id = {"ca-none": _FakeNone, "ca-a": _FakeA}
        with _patched_index(by_id, {("CA", "TEST"): (_FakeA,)}):
            event = {**self._EV, "capture_scraper_id": "ca-none"}
            assert raw_hearing_date(event, "x") is None

    def test_live_scraper_id_resolves_without_capture_id(self) -> None:
        with _patched_index({"ca-b": _FakeB}, {("CA", "TEST"): (_FakeA,)}):
            assert raw_hearing_date({**self._EV, "scraper_id": "ca-b"}, "x") == "2026-01-03"

    def test_hook_error_is_no_date(self) -> None:
        with _patched_index({"ca-r": _FakeRaises}, {}):
            assert raw_hearing_date({**self._EV, "capture_scraper_id": "ca-r"}, "x") is None

    def test_empty_text_is_no_date(self) -> None:
        with _patched_index({}, {("CA", "TEST"): (_FakeA,)}):
            assert raw_hearing_date(dict(self._EV), "") is None


# Scrapers whose live capture reads no hearing date from the raw file, so
# the default ``hearing_date_for_raw`` (None) is parity.  A new CA scraper
# must either override the hook or be listed here with the reason.
_NO_RAW_HEARING_DATE = {
    # Leaves hearing_date to the multimodal LLM; sets none itself.
    "OCTentativeRulingsScraper",
    # Governor appointment press releases, not rulings.
    "GovernorAppointmentsScraper",
}


def test_every_ca_scraper_declares_raw_hearing_date() -> None:
    by_id, _ = _scraper_index()
    base = BaseScraper.hearing_date_for_raw.__func__  # type: ignore[attr-defined]
    ca_classes = {cls for sid, cls in by_id.items() if sid.startswith("ca-")}
    no_override = {cls.__name__ for cls in ca_classes if cls.hearing_date_for_raw.__func__ is base}
    assert no_override == _NO_RAW_HEARING_DATE


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", ""),
        ("https://x/a/b/16_031126.pdf", "16_031126.pdf"),
        (_SF_URL_ENCODED, "403 Tentative Rulings 3.03.2026.pdf"),
        (_SF_URL_RAW, "414 Tentative Rulings 9.24.2026.pdf"),
        ("https://x/f.pdf?download=1", "f.pdf"),
    ],
)
def test_source_url_filename(url: str, expected: str) -> None:
    assert source_url_filename(url) == expected


# ---------------------------------------------------------------------------
# Hook guards and edge cases
# ---------------------------------------------------------------------------


def _hook(module: str, cls_name: str) -> Any:
    import importlib

    return getattr(importlib.import_module(module), cls_name).hearing_date_for_raw


# (module, class, a content_format the live scraper never archives)
_WRONG_FORMAT = [
    ("courts.ca.cc_tentatives", "CCTentativeRulingsScraper", "html"),
    ("courts.ca.fresno_tentatives", "FresnoTentativeRulingsScraper", "html"),
    ("courts.ca.riverside_tentatives", "RiversideTentativeRulingsScraper", "html"),
    ("courts.ca.sb_tentatives", "SBTentativeRulingsScraper", "html"),
    ("courts.ca.sf_tentatives", "SFTentativeRulingsScraper", "html"),
    ("courts.ca.sc_tentatives", "SCTentativeRulingsScraper", "html"),
    ("courts.ca.oc_family_law_tentatives", "OCFamilyLawTentativeRulingsScraper", "html"),
    ("courts.ca.oc_probate_tentatives", "OCProbateTentativeRulingsScraper", "html"),
    ("courts.ca.la_tentatives", "LATentativeRulingsScraper", "pdf"),
    ("courts.ca.la_tentatives", "LAAppellateTentativeRulingsScraper", "pdf"),
    ("courts.ca.sd_calendar", "SDCalendarScraper", "pdf"),
    ("courts.ca.sd_tentatives", "SDTentativeRulingsScraper", "pdf"),
    ("courts.ca.sd_pipeline", "SDPipelineScraper", "pdf"),
]


@pytest.mark.parametrize(("module", "cls_name", "fmt"), _WRONG_FORMAT)
def test_hook_ignores_other_content_formats(module: str, cls_name: str, fmt: str) -> None:
    text = (
        "HEARING DATE: 04/09/2026\nTentative Rulings for March 2, 2026\n"
        "Hearing Date: March 2, 2026\nDATE: 03/02/2026\n"
    )
    hook = _hook(module, cls_name)
    assert hook(text, source_url="https://x/16_031126.pdf", content_format=fmt) is None


@pytest.mark.parametrize(
    ("module", "cls_name"),
    [
        ("courts.ca.riverside_tentatives", "RiversideTentativeRulingsScraper"),
        ("courts.ca.sd_calendar", "SDCalendarScraper"),
        ("courts.ca.sd_tentatives", "SDTentativeRulingsScraper"),
        ("courts.ca.sf_civil_tentatives", "SFCivilTentativeRulingsScraper"),
        ("courts.ca.ventura_tentatives", "VenturaTentativeRulingsScraper"),
        ("courts.ca.cc_tentatives_portal", "CCTentativesPortalScraper"),
        ("courts.ca.la_tentatives", "LATentativeRulingsScraper"),
    ],
)
def test_hook_empty_text_is_none(module: str, cls_name: str) -> None:
    assert _hook(module, cls_name)("", content_format="") is None


def test_la_hook_needs_the_ruling_block() -> None:
    """Like ``_extract_ruling_fields``: no ``div#speechSynthesis``, no date."""
    hook = _hook("courts.ca.la_tentatives", "LATentativeRulingsScraper")
    assert hook("<html><b>Hearing Date:</b> March 2, 2026</html>", content_format="html") is None


def test_sd_pipeline_hook_delegates_to_phase_two() -> None:
    hook = _hook("courts.ca.sd_pipeline", "SDPipelineScraper")
    assert hook(_html("sd_roa_demurrer.html"), content_format="html") == datetime(2026, 3, 13)


@pytest.mark.parametrize(
    "text",
    ["<html>legacy bare body</html>", "[1, 2]", json.dumps({"ruling_id": "r1"})],
)
def test_sf_civil_hook_non_envelope_is_none(text: str) -> None:
    hook = _hook("courts.ca.sf_civil_tentatives", "SFCivilTentativeRulingsScraper")
    assert hook(text, content_format="html") is None


def test_cc_portal_hook_reads_envelope_row_date() -> None:
    hook = _hook("courts.ca.cc_tentatives_portal", "CCTentativesPortalScraper")
    envelope = {
        "row": {"case_number": "C22-01081", "hearing_date": "2025-02-28 09:00:00"},
        "detail_html_b64": "",
    }
    assert hook(json.dumps(envelope), content_format="txt") == datetime(2025, 2, 28, 9, 0)
    assert hook("plain calendar text", content_format="txt") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("TENTATIVE RULINGS 6-1-26\n", datetime(2026, 6, 1)),
        ("TENTATIVE RULINGS FOR 06/01/2026\n", datetime(2026, 6, 1)),
        ("TENTATIVE RULINGS 13-45-26\n", None),
        ("Department S36\nTENTATIVE RULINGS 6-1-26 continued from 5-1-26\n", None),
    ],
)
def test_sb_header_title_line(text: str, expected: datetime | None) -> None:
    from courts.ca.sb_tentatives import _sb_hearing_date_from_text

    assert _sb_hearing_date_from_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_VENTURA_PROBATE_NOTES, datetime(2026, 5, 5)),
        ("COUNTY OF VENTURA\n13/45/2026 in Department J6\n", None),
        ("COUNTY OF VENTURA\n" + "x" * 1200 + "\n05/05/2026 in Department J6\n", None),
    ],
)
def test_ventura_header_hearing_date(text: str, expected: datetime | None) -> None:
    from courts.ca.ventura_tentatives import ventura_header_hearing_date

    assert ventura_header_hearing_date(text) == expected


def test_scraper_index_skips_broken_modules_and_factories() -> None:
    import importlib

    from ingestion import raw_hearing_date as rhd

    real_import = importlib.import_module

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "courts.ca.sb_tentatives":
            raise ImportError("missing optional dependency")
        return real_import(name, *args, **kwargs)

    def broken_factory() -> Any:
        raise RuntimeError("config needs env")

    broken_factory.__module__ = "courts.ca.ventura_tentatives"
    ventura = real_import("courts.ca.ventura_tentatives")
    with (
        patch.object(rhd.importlib, "import_module", side_effect=fake_import),
        patch.object(ventura, "default_config", broken_factory),
    ):
        by_id, by_county = rhd._scraper_index.__wrapped__()
    assert "ca-sb-tentatives-civil" not in by_id
    assert "ca-ventura-tentatives" not in by_id
    assert ("CA", "SAN BERNARDINO") not in by_county
    assert by_id["ca-sc-tentatives-civil"].__name__ == "SCTentativeRulingsScraper"
