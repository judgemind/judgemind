"""Rebuild / reingest events route by the capturing scraper (#5145).

``extraction_config._SCRAPER_CONFIGS`` overrides the county extraction
method per scraper: San Diego's ``ca-sd-calendar`` HTML is fully parsed by
the scraper (``NONE``), while ``ca-sd-tentatives`` ROA text needs the
county LLM.  The worker looked the override up on the event's
``scraper_id``, which ``rebuild_db`` and ``reingest_from_s3`` rewrite to
``rebuild-ca-san_diego`` / ``reingest-ca-san_diego``.  So a rebuild sent
every SD raw down the NONE route (tentatives included) and a reingest sent
every SD raw, calendar included, to the LLM.  The live scraper id reaches
the worker as ``capture_scraper_id`` (from the raw object's S3 metadata).

These tests build events through each script's real event builder.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from framework.extraction_config import (
    SAN_DIEGO_FRAMEWORK_PROMPT,
    extraction_strategy_for_event,
)
from framework.llm_schema import ExtractedRuling, ExtractionOutcome
from framework.storage import capturing_scraper_id
from ingestion.worker import IngestionWorker

_SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "scripts")
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

rebuild_db = importlib.import_module("rebuild_db")
reingest = importlib.import_module("reingest_from_s3")

_HASH = "b" * 64
_KEY = f"ca/san_diego/superior_court/raw/{_HASH}.html"
_PARSED = {
    "state": "ca",
    "county": "san_diego",
    "court": "superior_court",
    "content_hash": _HASH,
    "ext": "html",
}

#: A calendar page the deterministic splitter finds no hearings in, so the
#: worker's routing falls to the extraction strategy.
_CALENDAR_HTML = (
    b"<!DOCTYPE html><html><body>"
    b"<h1>CIVIL CALENDAR For Friday, 03/13/2026</h1>"
    b"<h3>CENTRAL DIVISION, CENTRAL COURTHOUSE</h3>"
    b"<p>No hearings are scheduled.</p>"
    b"</body></html>"
)
#: ROA tentative ruling text, as ``ca-sd-tentatives`` archives it.
_TENTATIVES_HTML = (
    b"<html><body><p>Case Number: 24CU016153C  Smith vs Jones</p>"
    b"<p>Tentative Ruling: The Court GRANTS Defendant's Motion to Compel "
    b"Further Responses.</p></body></html>"
)

_Builder = Callable[..., dict[str, Any]]


def _rebuild(content: bytes, provenance: dict[str, str] | None) -> dict[str, Any]:
    return rebuild_db.build_event(
        _KEY, content, dict(_PARSED), "test-bucket", provenance=provenance
    )


def _reingest(content: bytes, provenance: dict[str, str] | None) -> dict[str, Any]:
    return reingest._build_prefix_event(
        _KEY, content, dict(_PARSED), "test-bucket", provenance=provenance
    )


_BUILDERS = [
    pytest.param(_rebuild, "rebuild-ca-san_diego", id="rebuild"),
    pytest.param(_reingest, "reingest-ca-san_diego", id="reingest"),
]


def _provenance(scraper_id: str) -> dict[str, str]:
    """What ``capture_provenance_from_s3_object`` restores from the object."""
    return {"source_url": "https://example.test/sd", "capture_scraper_id": scraper_id}


# ---------------------------------------------------------------------------
# The strategy the worker resolves for the event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("build", "synthetic_id"), _BUILDERS)
def test_extraction_strategy_sd_calendar_rebuild_or_reingest_is_none(
    build: _Builder, synthetic_id: str
) -> None:
    event = build(_CALENDAR_HTML, _provenance("ca-sd-calendar"))
    assert event["scraper_id"] == synthetic_id
    strategy = extraction_strategy_for_event(event)
    assert strategy.skip_llm is True


@pytest.mark.parametrize(("build", "synthetic_id"), _BUILDERS)
def test_extraction_strategy_sd_tentatives_rebuild_or_reingest_is_county_llm(
    build: _Builder, synthetic_id: str
) -> None:
    event = build(_TENTATIVES_HTML, _provenance("ca-sd-tentatives"))
    assert event["scraper_id"] == synthetic_id
    strategy = extraction_strategy_for_event(event)
    assert strategy.skip_llm is False
    assert strategy.use_multimodal is False
    assert strategy.system_prompt == SAN_DIEGO_FRAMEWORK_PROMPT
    assert strategy.provider == "google"


@pytest.mark.parametrize(("build", "synthetic_id"), _BUILDERS)
def test_extraction_strategy_sd_rebuild_or_reingest_without_metadata_takes_calendar_route(
    build: _Builder, synthetic_id: str
) -> None:
    """A metadata-less raw (local-cache rebuild, pre-metadata object) cannot
    name its scraper; every SD raw archived so far is calendar HTML."""
    event = build(_CALENDAR_HTML, None)
    assert "capture_scraper_id" not in event
    assert capturing_scraper_id(event) == synthetic_id
    assert extraction_strategy_for_event(event).skip_llm is True


def test_extraction_strategy_live_events_unchanged() -> None:
    calendar = {"state": "CA", "county": "San Diego", "scraper_id": "ca-sd-calendar"}
    tentatives = {"state": "CA", "county": "San Diego", "scraper_id": "ca-sd-tentatives"}
    assert extraction_strategy_for_event(calendar).skip_llm is True
    assert extraction_strategy_for_event(tentatives).skip_llm is False


# ---------------------------------------------------------------------------
# The worker's split routing for the same events
# ---------------------------------------------------------------------------


def _make_worker() -> IngestionWorker:
    os_mock = MagicMock()
    os_mock.indices.exists.return_value = False
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=os_mock,
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    return worker


def _split(event: dict[str, Any]) -> MagicMock:
    """Run the worker's split on *event*; return the county extractor mock."""
    worker = _make_worker()
    extractor = MagicMock()
    extractor.extract.return_value = [
        ExtractedRuling(
            extracted_case_number="24CU016153C",
            extracted_case_title="Smith vs Jones",
            ruling_text="The Court GRANTS Defendant's Motion to Compel Further Responses.",
            outcome=ExtractionOutcome.GRANTED,
            motion_type="Motion to Compel",
            extracted_parties=[],
        )
    ]
    with (
        patch.object(worker, "_get_county_extractor", return_value=extractor),
        patch.object(worker, "process_event"),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True),
    ):
        worker._llm_split_document(
            event, event["document_id"], event["ruling_text"], event["state"], event["county"]
        )
    return extractor


@pytest.mark.parametrize(("build", "synthetic_id"), _BUILDERS)
def test_extraction_strategy_worker_sends_sd_tentatives_rebuild_or_reingest_to_llm(
    build: _Builder, synthetic_id: str
) -> None:
    extractor = _split(build(_TENTATIVES_HTML, _provenance("ca-sd-tentatives")))
    extractor.extract.assert_called_once()
    assert extractor.extract.call_args.kwargs["system_prompt"] == SAN_DIEGO_FRAMEWORK_PROMPT


@pytest.mark.parametrize(("build", "synthetic_id"), _BUILDERS)
def test_extraction_strategy_worker_keeps_sd_calendar_rebuild_or_reingest_off_llm(
    build: _Builder, synthetic_id: str
) -> None:
    extractor = _split(build(_CALENDAR_HTML, _provenance("ca-sd-calendar")))
    extractor.extract.assert_not_called()


# ---------------------------------------------------------------------------
# The case-type fallback reads the same resolver
# ---------------------------------------------------------------------------


class _StopError(Exception):
    pass


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(rebuild_db.build_event, id="rebuild"),
        pytest.param(reingest._build_prefix_event, id="reingest"),
    ],
)
def test_case_type_fallback_rebuild_or_reingest_reads_capturing_scraper(build: _Builder) -> None:
    """``resolve_case_type``'s scraper-id step (``ca-oc-tentatives-probate``
    -> probate) saw ``rebuild-ca-orange`` on a rebuild, which names no
    case category."""
    content_hash = "c" * 64
    event = build(
        f"ca/orange/superior_court/raw/{content_hash}.html",
        b"<p>The petition is GRANTED.</p>",
        {
            "state": "ca",
            "county": "orange",
            "court": "superior_court",
            "content_hash": content_hash,
            "ext": "html",
        },
        "test-bucket",
        provenance={"capture_scraper_id": "ca-oc-tentatives-probate"},
    )
    # One already-extracted split child, so no extractor runs.
    event.update(
        {
            "_split_processed": True,
            "_llm_extracted": True,
            "case_number": "UNKNOWN-1",
            "hearing_date": "2026-03-05",
            "ruling_text": "The petition is GRANTED.",
        }
    )
    seen: list[str | None] = []

    def fake_resolve(**kwargs: Any) -> tuple[None, None]:
        seen.append(kwargs["scraper_id"])
        if kwargs["scraper_id"] is None:  # the case-number-only call
            return None, None
        raise _StopError

    worker = _make_worker()
    with (
        patch("ingestion.worker.resolve_case_type", side_effect=fake_resolve),
        patch.object(worker, "_get_connection"),
        patch("ingestion.worker.psycopg"),
        pytest.raises(_StopError),
    ):
        worker._process_event_timed(event, MagicMock())
    assert seen[-1] == "ca-oc-tentatives-probate"


# ---------------------------------------------------------------------------
# The shared resolver
# ---------------------------------------------------------------------------


class TestCapturingScraperId:
    def test_live_event(self) -> None:
        assert capturing_scraper_id({"scraper_id": "ca-sd-calendar"}) == "ca-sd-calendar"

    def test_capture_id_wins_over_synthetic(self) -> None:
        event = {"scraper_id": "rebuild-ca-orange", "capture_scraper_id": "ca-oc-tentatives-civil"}
        assert capturing_scraper_id(event) == "ca-oc-tentatives-civil"

    def test_metadata_less_keeps_synthetic(self) -> None:
        assert capturing_scraper_id({"scraper_id": "rebuild-ca-orange"}) == "rebuild-ca-orange"

    @pytest.mark.parametrize("event", [{}, {"scraper_id": None, "capture_scraper_id": ""}])
    def test_no_scraper(self, event: dict[str, Any]) -> None:
        assert capturing_scraper_id(event) == ""
