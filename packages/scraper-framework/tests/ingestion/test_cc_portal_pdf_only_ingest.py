"""The ingestion worker transcribes a CC portal PDF-only ruling (#4753).

Some portal detail pages carry only a link to the department's calendar PDF
and no inline ruling text.  The scraper archives the PDF inside the JSON
envelope and leaves ``ruling_text`` empty (archive-first: no pdfplumber
before the archive write).  The worker then reads the envelope back from
S3, transcribes the PDF with the same ``extract_text_from_pdf`` path other
PDF scrapers use, and keeps only this case's item of the calendar.

These tests run the real capture path (scraper -> archive -> document.captured
payload) and hand the payload to ``IngestionWorker.process_event``, with a
fake S3 that returns exactly the bytes the scraper archived.  The rebuild /
prefix-reingest path (the event carries the raw envelope as ``ruling_text``)
is covered too.

Fixture ``18_022825.pdf`` is the real Dept 18 calendar PDF from dev S3
(document 4c0d8d33-0f79-5bf6-be44-149a1d8137b9, case C22-01081).
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, date, datetime, timedelta
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

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "cc_portal"
_PDF = _FIXTURES / "18_022825.pdf"
_DETAIL = _FIXTURES / "detail_c22-01081_pdf_only.html"
_INLINE_DETAIL = _FIXTURES / "detail_c22-01746_no_pdf.html"
_PDF_URL = f"{BASE_URL}/system/files/general/18_022825.pdf"
_S3_KEY = "ca/contra_costa/superior_court/raw/pdfonly.txt"

_FORM = (
    '<html><body><form><select name="field_judge_target_id">'
    '<option value="All">- Any -</option>'
    '<option value="276">DANIELLE K DOUGLAS</option>'
    "</select></form></body></html>"
)


def _listing(hearing: datetime, slug: str = "c22-01081", case_number: str = "C22-01081") -> str:
    return (
        "<html><body><table><tbody><tr>"
        f'<td><time datetime="{hearing.strftime("%Y-%m-%dT%H:%M:%S")}Z">x</time></td>'
        f'<td><a href="/tentative-ruling/{slug}">{case_number}</a>'
        "<p>WINEHAVEN LEGACY LLC VS. CITY OF RICHMOND</p>"
        "Civil<p>HEARING ON MOTION IN RE:  JUDGMENT ON THE PLEADINGS</p></td>"
        "</tr></tbody></table></body></html>"
    )


@respx.mock
def _capture(hearing: datetime) -> tuple[dict[str, Any], bytes]:
    """Run the portal scraper on the PDF-only fixture.

    Returns the document.captured payload and the bytes that were archived.
    """
    respx.get(LISTING_URL, params={"field_judge_target_id": "276"}).mock(
        return_value=httpx.Response(200, text=_listing(hearing))
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=_FORM))
    respx.get(f"{BASE_URL}/tentative-ruling/c22-01081").mock(
        return_value=httpx.Response(200, content=_DETAIL.read_bytes())
    )
    respx.get(_PDF_URL).mock(return_value=httpx.Response(200, content=_PDF.read_bytes()))

    redis_client = MagicMock()
    archived: list[bytes] = []
    archiver = MagicMock()

    def _archive(doc: Any) -> str:
        archived.append(doc.raw_content)
        return _S3_KEY

    archiver.archive.side_effect = _archive
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
    assert len(archived) == 1

    captured = [c for c in redis_client.xadd.call_args_list if c.args[0] == "document.captured"]
    assert len(captured) == 1
    return json.loads(captured[0].args[1]["data"]), archived[0]


def _worker(s3_objects: dict[str, bytes] | None = None) -> Any:
    from ingestion.worker import IngestionWorker

    os_mock = MagicMock()
    os_mock.indices.exists.return_value = False
    s3 = MagicMock()
    objects = s3_objects or {}

    def _get_object(Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 — boto3 kwargs
        if Key not in objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(objects[Key])}

    s3.get_object.side_effect = _get_object
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=os_mock,
        s3_client=s3,
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


def _process(worker: Any, payload: dict[str, Any]) -> MagicMock:
    """Run process_event; return the insert mock with ``.case_number`` set.

    ``.case_number`` is the case number the worker upserted the case under
    (None when no case was upserted).
    """
    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch("ingestion.worker.resolve_judge", return_value=None),
        patch("ingestion.worker.batch_upsert_parties"),
        patch(
            "ingestion.worker.upsert_case_returning_title",
            return_value=("case-uuid-1", None),
        ) as mock_case,
        patch("ingestion.worker.insert_document_and_ruling", return_value=True) as mock_ins,
    ):
        mock_psycopg.connect.return_value = _conn()
        worker.process_event(payload)
    mock_ins.case_number = mock_case.call_args.args[1] if mock_case.call_args else None
    return mock_ins


def _assert_c22_01081_section(text: str) -> None:
    assert text.startswith("1. 9:00 AM CASE NUMBER: C22-01081")
    assert "Before the Court is Defendant City of Richmond" in text
    assert "Defendant’s MJOP is sustained without leave to amend." in text
    # Only this case's item of the 20-item department calendar.
    assert "C22-01706" not in text
    assert "JOHN DEERE FINANCIAL" not in text


def _recent_hearing() -> datetime:
    # A hearing a few days out so deterministic validation's date-range rule
    # accepts it, as it would for a freshly posted ruling.
    return (datetime.now(UTC) + timedelta(days=3)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )


# A capture the evening before C22-01081's 02/28/2025 hearing, so the
# deterministic date-range rule accepts the calendar date (#4762).
_CALENDAR_CAPTURE_TS = "2025-02-28T02:00:00+00:00"


def test_live_path_worker_transcribes_pdf_only_portal_ruling() -> None:
    payload, archived = _capture(_recent_hearing())

    # The event carries no ruling text: transcription is the worker's job.
    assert payload["scraper_id"] == "ca-cc-tentatives-portal"
    assert payload["content_format"] == "text"
    assert payload["s3_key"] == _S3_KEY
    assert not payload["ruling_text"]
    # The hearing date is the calendar's 02/28/2025, not the listing time.
    assert payload["hearing_date"].startswith("2025-02-28")
    payload["capture_timestamp"] = _CALENDAR_CAPTURE_TS

    worker = _worker({_S3_KEY: archived})
    mock_ins = _process(worker, payload)

    mock_ins.assert_called_once()
    kwargs = mock_ins.call_args.kwargs
    assert kwargs["scraper_id"] == "ca-cc-tentatives-portal"
    assert kwargs["s3_key"] == _S3_KEY
    assert mock_ins.case_number == "C22-01081"
    assert kwargs["hearing_date"] == date(2025, 2, 28)
    _assert_c22_01081_section(kwargs["ruling_text"])


def test_live_path_stale_pdf_only_ruling_fails_only_the_hearing_date_rule(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2025 portal rulings: text is present, only the date rule rejects them."""
    payload, archived = _capture(datetime(2025, 3, 10, 21, 21, 20, tzinfo=UTC))

    worker = _worker({_S3_KEY: archived})
    with caplog.at_level(logging.WARNING, logger="ingestion.worker"):
        mock_ins = _process(worker, payload)

    mock_ins.assert_not_called()
    fails = [r for r in caplog.records if "Deterministic validation FAIL" in r.getMessage()]
    assert len(fails) == 1
    assert fails[0].det_failed_rules == ["hearing_date_in_range"]


def test_live_path_s3_fetch_failure_leaves_text_empty_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload, _archived = _capture(_recent_hearing())

    worker = _worker({})  # the envelope is not in S3
    with caplog.at_level(logging.WARNING, logger="ingestion.worker"):
        mock_ins = _process(worker, payload)

    mock_ins.assert_not_called()
    messages = [r.getMessage() for r in caplog.records]
    assert any("CC portal envelope could not be read" in m for m in messages)
    fails = [r for r in caplog.records if "Deterministic validation FAIL" in r.getMessage()]
    assert fails and "ruling_text_not_empty" in fails[0].det_failed_rules


def _rebuild_event(envelope_bytes: bytes) -> dict[str, Any]:
    """The event rebuild_db / prefix-mode reingest builds for a .txt raw."""
    return {
        "document_id": "5a1f3d0e-0000-5000-8000-000000000001",
        "state": "CA",
        "county": "Contra Costa",
        "court": "Superior Court",
        "content_format": "txt",
        "content_hash": "0d4d4e8d",
        "s3_key": _S3_KEY,
        "s3_bucket": "test-bucket",
        "scraper_id": "rebuild-ca-contra_costa",
        "source_url": "",
        "capture_timestamp": None,
        "ruling_text": envelope_bytes.decode("utf-8"),
    }


def test_rebuild_path_worker_unwraps_pdf_only_envelope() -> None:
    _payload, archived = _capture(_recent_hearing())

    worker = _worker()
    mock_ins = _process(worker, _rebuild_event(archived))

    mock_ins.assert_called_once()
    kwargs = mock_ins.call_args.kwargs
    assert mock_ins.case_number == "C22-01081"
    # Fields from the envelope fill the bare rebuild event.
    # Dept 18 from the PDF filename (the worker then applies CC's
    # department reassignment table).
    assert kwargs["department"]
    assert kwargs["source_url"] == f"{BASE_URL}/tentative-ruling/c22-01081"
    # The calendar PDF's date, not the listing time (#4762).
    assert kwargs["hearing_date"] == date(2025, 2, 28)
    _assert_c22_01081_section(kwargs["ruling_text"])
    assert '"detail_html_b64"' not in kwargs["ruling_text"]
    # Nothing to fetch: the envelope was in the event.
    worker._s3_client.get_object.assert_not_called()


@respx.mock
def _capture_inline() -> bytes:
    respx.get(LISTING_URL, params={"field_judge_target_id": "276"}).mock(
        return_value=httpx.Response(
            200, text=_listing(_recent_hearing(), slug="c22-01746", case_number="C22-01746")
        )
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=_FORM))
    respx.get(f"{BASE_URL}/tentative-ruling/c22-01746").mock(
        return_value=httpx.Response(200, content=_INLINE_DETAIL.read_bytes())
    )
    config = portal_default_config().model_copy(update={"request_delay_seconds": 0.0})
    docs = CCTentativesPortalScraper(config=config).fetch_documents()
    assert len(docs) == 1
    return docs[0].raw_content


def test_rebuild_path_worker_unwraps_inline_envelope() -> None:
    """An inline envelope on the rebuild path yields the inline text, not JSON."""
    worker = _worker()
    mock_ins = _process(worker, _rebuild_event(_capture_inline()))

    mock_ins.assert_called_once()
    kwargs = mock_ins.call_args.kwargs
    assert mock_ins.case_number == "C22-01746"
    assert kwargs["ruling_text"].startswith(
        "Defendant Walnut Creek Presbyterian Church’s Motion for Summary Judgment"
    )


def test_non_envelope_text_event_is_untouched() -> None:
    """Plain text events from other scrapers do not go near the envelope path."""
    from ingestion.worker import IngestionWorker

    event = {
        "content_format": "text",
        "ruling_text": "{ not an envelope",
        "scraper_id": "ca-other",
        "extra": {},
    }
    worker = _worker()
    assert IngestionWorker._unwrap_cc_portal_envelope(worker, event) is event
    worker._s3_client.get_object.assert_not_called()


def test_malformed_envelope_is_processed_as_is_not_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unwrap error is logged and the event goes on unchanged."""
    event = {
        "document_id": "d1",
        "content_format": "text",
        "ruling_text": None,
        "extra": {"ruling_text_in_pdf": True},
        "s3_key": _S3_KEY,
    }
    worker = _worker()
    with (
        patch.object(worker, "_fetch_archived_bytes", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.WARNING, logger="ingestion.worker"),
    ):
        assert worker._unwrap_cc_portal_envelope(event) is event
    assert any("CC portal envelope unwrap failed" in r.getMessage() for r in caplog.records)


@respx.mock
def _capture_pdf_plus_inline() -> dict[str, Any]:
    """Live capture of a detail page with both a PDF link and inline text."""
    respx.get(LISTING_URL, params={"field_judge_target_id": "276"}).mock(
        return_value=httpx.Response(
            200, text=_listing(_recent_hearing(), slug="l24-04564", case_number="L24-04564")
        )
    )
    respx.get(FORM_URL).mock(return_value=httpx.Response(200, text=_FORM))
    respx.get(f"{BASE_URL}/tentative-ruling/l24-04564").mock(
        return_value=httpx.Response(200, content=(_FIXTURES / "detail_l24-04564.html").read_bytes())
    )
    respx.get(f"{BASE_URL}/system/files/general/16_012925.pdf").mock(
        return_value=httpx.Response(200, content=_PDF.read_bytes())
    )
    redis_client = MagicMock()
    archiver = MagicMock()
    archiver.archive.return_value = _S3_KEY
    archiver.bucket = "test-bucket"
    config = portal_default_config().model_copy(
        update={"request_delay_seconds": 0.0, "max_retries": 1}
    )
    CCTentativesPortalScraper(
        config=config, archiver=archiver, event_bus=EventBus(redis_client)
    ).run()
    captured = [c for c in redis_client.xadd.call_args_list if c.args[0] == "document.captured"]
    assert len(captured) == 1
    return json.loads(captured[0].args[1]["data"])


def test_live_path_pdf_plus_inline_ruling_is_untouched() -> None:
    """A page with a PDF and inline text keeps its inline text; S3 is not read."""
    payload = _capture_pdf_plus_inline()
    assert payload["ruling_text"]
    assert "ruling_text_in_pdf" not in payload["extra"]

    worker = _worker()
    mock_ins = _process(worker, payload)

    mock_ins.assert_called_once()
    assert mock_ins.call_args.kwargs["ruling_text"] == payload["ruling_text"]
    worker._s3_client.get_object.assert_not_called()


def _transcription_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if getattr(r, "telemetry_event", None) == "cc_portal_envelope_pdf_transcription"
    ]


def _pdf_text() -> str:
    from ingestion.llm_extract import extract_text_from_pdf

    text = extract_text_from_pdf(_PDF.read_bytes())
    assert text
    return text


def test_pdf_header_date_overrides_listing_time_on_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An event that still carries the listing time (captured before #4762)
    is stored under the calendar PDF's header date, and the worker logs it."""
    payload, archived = _capture(_recent_hearing())
    payload["hearing_date"] = "2025-03-10T21:21:20+00:00"
    payload["capture_timestamp"] = _CALENDAR_CAPTURE_TS

    worker = _worker({_S3_KEY: archived})
    with caplog.at_level(logging.INFO, logger="ingestion.worker"):
        mock_ins = _process(worker, payload)

    mock_ins.assert_called_once()
    assert mock_ins.call_args.kwargs["hearing_date"] == date(2025, 2, 28)
    logs = _transcription_logs(caplog)
    assert len(logs) == 1
    assert logs[0].hearing_date == "2025-02-28"
    assert logs[0].hearing_date_source == "pdf_header"
    assert logs[0].event_hearing_date == "2025-03-10T21:21:20+00:00"


def test_pdf_without_header_date_keeps_event_date(caplog: pytest.LogCaptureFixture) -> None:
    """No header date in the PDF preamble: the event's date stands.  The
    worker never takes a date from the calendar items' bodies."""
    payload, archived = _capture(_recent_hearing())
    payload["capture_timestamp"] = _CALENDAR_CAPTURE_TS
    headerless = "\n".join(
        line for line in _pdf_text().splitlines() if not line.startswith("HEARING DATE:")
    )

    worker = _worker({_S3_KEY: archived})
    with (
        patch("ingestion.worker.extract_text_from_pdf", return_value=headerless),
        caplog.at_level(logging.INFO, logger="ingestion.worker"),
    ):
        mock_ins = _process(worker, payload)

    mock_ins.assert_called_once()
    # 2025-02-28 here is the capture-time date from the PDF filename.
    assert mock_ins.call_args.kwargs["hearing_date"] == date(2025, 2, 28)
    logs = _transcription_logs(caplog)
    assert len(logs) == 1
    assert logs[0].hearing_date == "2025-02-28"
    assert logs[0].hearing_date_source == "event"
