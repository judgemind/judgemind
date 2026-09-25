"""The ingestion worker writes a CC portal inline ruling (#4749).

The portal posts some rulings inline on the detail page with no PDF link.
This test runs the real capture path (scraper -> document.captured event
payload) and hands that payload to ``IngestionWorker.process_event``, then
checks that the ruling row is written with the inline ruling text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from courts.ca.cc_tentatives_portal import (
    BASE_URL,
    FORM_URL,
    LISTING_URL,
    CCTentativesPortalScraper,
)
from courts.ca.cc_tentatives_portal import default_config as portal_default_config
from framework.events import EventBus

pytestmark = pytest.mark.regression

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "cc_portal" / "detail_c22-01746_no_pdf.html"

_FORM = (
    '<html><body><form><select name="field_judge_target_id">'
    '<option value="All">- Any -</option>'
    '<option value="238">JOHN P DEVINE</option>'
    "</select></form></body></html>"
)


def _listing(hearing: datetime) -> str:
    return (
        "<html><body><table><tbody><tr>"
        f'<td><time datetime="{hearing.strftime("%Y-%m-%dT%H:%M:%S")}Z">x</time></td>'
        '<td><a href="/tentative-ruling/c22-01746">C22-01746</a>'
        "<p>JANE DOE VS. WALNUT CREEK PRESBYTERIAN CHURCH</p>"
        "Civil<p>HEARING ON SUMMARY MOTION</p></td>"
        "</tr></tbody></table></body></html>"
    )


@respx.mock
def _capture_event_payload() -> dict[str, Any]:
    """Run the portal scraper against the inline fixture; return the event."""
    # A hearing a few days out so deterministic validation's date-range
    # rule accepts it, as it would for a freshly posted ruling.
    hearing = (datetime.now(UTC) + timedelta(days=3)).replace(
        hour=16, minute=0, second=0, microsecond=0
    )
    respx.get(LISTING_URL, params={"field_judge_target_id": "238"}).mock(
        return_value=httpx.Response(200, text=_listing(hearing))
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=_FORM))
    respx.get(f"{BASE_URL}/tentative-ruling/c22-01746").mock(
        return_value=httpx.Response(200, content=_FIXTURE.read_bytes())
    )

    redis_client = MagicMock()
    archiver = MagicMock()
    archiver.archive.return_value = "ca/contra_costa/superior_court/raw/abc.txt"
    archiver.bucket = "test-bucket"
    config = portal_default_config().model_copy(
        update={"request_delay_seconds": 0.0, "max_retries": 1}
    )
    scraper = CCTentativesPortalScraper(
        config=config, archiver=archiver, event_bus=EventBus(redis_client)
    )

    health = scraper.run()
    assert health.success is True
    assert health.records_captured == 1

    captured = [c for c in redis_client.xadd.call_args_list if c.args[0] == "document.captured"]
    assert len(captured) == 1
    return json.loads(captured[0].args[1]["data"])


def _worker() -> Any:
    from ingestion.worker import IngestionWorker

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


def _conn() -> MagicMock:
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",)] + [None] * 20
    return mock_conn


def test_worker_writes_inline_portal_ruling_text() -> None:
    payload = _capture_event_payload()

    assert payload["scraper_id"] == "ca-cc-tentatives-portal"
    assert payload["s3_key"] == "ca/contra_costa/superior_court/raw/abc.txt"
    assert payload["ruling_text"].startswith(
        "Defendant Walnut Creek Presbyterian Church’s Motion for Summary Judgment"
    )

    worker = _worker()
    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch("ingestion.worker.resolve_judge", return_value=None),
        patch("ingestion.worker.batch_upsert_parties"),
        patch("ingestion.worker.insert_document_and_ruling", return_value=True) as mock_ins,
    ):
        mock_psycopg.connect.return_value = _conn()
        worker.process_event(payload)

    mock_ins.assert_called_once()
    kwargs = mock_ins.call_args.kwargs
    assert kwargs["scraper_id"] == "ca-cc-tentatives-portal"
    assert kwargs["s3_key"] == "ca/contra_costa/superior_court/raw/abc.txt"
    assert "Walnut Creek Presbyterian Church" in kwargs["ruling_text"]
    assert "continued to give plaintiffs additional time" in kwargs["ruling_text"]
