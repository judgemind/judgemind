"""Governor raws built by rebuild / reingest take the bio route (#5016).

The worker's governor branch (#3688) used to match only
``event_data["scraper_id"] == "ca-governor-appointments"``.  ``rebuild_db``
and ``reingest_from_s3`` build their events from S3 with
``scraper_id = "rebuild-ca-statewide"`` / ``"reingest-ca-statewide"``; the
live scraper id reaches the worker only as ``capture_scraper_id`` (from the
object's metadata), and a local-cache rebuild carries no metadata at all.
So a statewide rebuild or a ``ca/statewide/`` reingest sent every governor
press release through LLM extraction and wrote UNKNOWN-* rulings.

These tests build the governor event through each script's real event
builder and run it through ``IngestionWorker.process_event``.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ingestion import worker as worker_mod
from ingestion.worker import IngestionWorker

_SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "scripts")
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

rebuild_db = importlib.import_module("rebuild_db")
reingest = importlib.import_module("reingest_from_s3")

_HASH = "a" * 64
_KEY = f"ca/statewide/governor/raw/{_HASH}.html"
_PARSED = {
    "state": "ca",
    "county": "statewide",
    "court": "governor",
    "content_hash": _HASH,
    "ext": "html",
}
_HTML = (
    b"<html><body><p>Governor Gavin Newsom today announced the appointment of "
    b"Jane Doe, of Los Angeles, to serve as a Judge in the Los Angeles County "
    b"Superior Court. Hearing Date: April 28, 2026.</p></body></html>"
)
#: What ``capture_provenance_from_s3_object`` restores from the live object.
_PROVENANCE = {
    "source_url": "https://www.gov.ca.gov/2026/04/28/governor-newsom-appoints-judges/",
    "capture_scraper_id": "ca-governor-appointments",
}


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
    worker._get_framework_extractor = lambda: None  # type: ignore[method-assign]
    return worker


def _rebuild_event(provenance: dict[str, str] | None) -> dict[str, Any]:
    return rebuild_db.build_event(_KEY, _HTML, dict(_PARSED), "test-bucket", provenance=provenance)


def _reingest_event(provenance: dict[str, str] | None) -> dict[str, Any]:
    return reingest._build_prefix_event(
        _KEY, _HTML, dict(_PARSED), "test-bucket", provenance=provenance
    )


def _assert_bio_route(event: dict[str, Any]) -> None:
    """The event ends on the bio route: one skip record, no document/ruling write."""
    worker = _make_worker()
    with (
        patch.object(worker, "_get_connection") as mock_get_conn,
        patch.object(worker, "_process_event_timed") as mock_timed,
        patch("ingestion.worker.psycopg") as mock_psycopg,
    ):
        assert worker.process_event(event) is None

    # Ruling extraction never ran.
    mock_timed.assert_not_called()
    mock_psycopg.connect.assert_not_called()
    cur = mock_get_conn.return_value.cursor.return_value.__enter__.return_value
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("INSERT INTO telemetry.ingest_skips" in s for s in sqls), sqls
    assert not any("documents" in s or "rulings" in s for s in sqls), sqls
    upsert = next(c for c in cur.execute.call_args_list if "ingest_skips" in c.args[0])
    assert upsert.args[1][0] == _KEY
    assert upsert.args[1][1] == "governor_bio_route"


@pytest.mark.parametrize(
    "provenance",
    [_PROVENANCE, None],
    ids=["s3-metadata", "local-cache-no-metadata"],
)
def test_governor_raw_from_rebuild_event_takes_bio_route(provenance: dict[str, str] | None) -> None:
    event = _rebuild_event(provenance)
    assert event["scraper_id"] == "rebuild-ca-statewide"
    _assert_bio_route(event)


@pytest.mark.parametrize(
    "provenance",
    [_PROVENANCE, None],
    ids=["s3-metadata", "no-metadata"],
)
def test_governor_raw_from_reingest_event_takes_bio_route(
    provenance: dict[str, str] | None,
) -> None:
    event = _reingest_event(provenance)
    assert event["scraper_id"] == "reingest-ca-statewide"
    _assert_bio_route(event)


class TestIsGovernorAppointmentEvent:
    def test_live_scraper_id(self) -> None:
        assert worker_mod.is_governor_appointment_event({"scraper_id": "ca-governor-appointments"})

    def test_capture_scraper_id_from_rebuild_or_reingest(self) -> None:
        assert worker_mod.is_governor_appointment_event(
            {"scraper_id": "rebuild-ca-statewide", "capture_scraper_id": "ca-governor-appointments"}
        )

    def test_governor_key_prefix_alone(self) -> None:
        assert worker_mod.is_governor_appointment_event(
            {"scraper_id": "reingest-ca-statewide", "s3_key": _KEY}
        )

    @pytest.mark.parametrize(
        "event",
        [
            {
                "scraper_id": "ca-la-tentatives-civil",
                "s3_key": "ca/los_angeles/superior_court/raw/x.html",
            },
            {"scraper_id": "rebuild-ca-orange", "capture_scraper_id": "ca-orange-tentatives"},
            {
                "scraper_id": "reingest-ca-statewide",
                "s3_key": "ca/statewide/other_court/raw/x.html",
            },
            {"s3_key": None},
            {},
        ],
    )
    def test_other_events_are_not_governor(self, event: dict[str, Any]) -> None:
        assert not worker_mod.is_governor_appointment_event(event)
