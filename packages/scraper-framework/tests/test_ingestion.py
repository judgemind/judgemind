"""Tests for the ingestion worker — Postgres and OpenSearch writes.

All external dependencies (Postgres, Redis, OpenSearch, S3) are mocked so
these tests run offline in CI without any infrastructure.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from ingestion.db import _derive_court_code
from ingestion.worker import IngestionWorker, _parse_date, _parse_datetime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> dict:
    """Return a minimal valid DocumentCapturedEvent payload."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "scraper_id": "ca-la-tentatives-civil",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": "https://www.lacourt.org/tentativerulings/1",
        "content_format": "html",
        "content_hash": "abc123",
        "s3_key": "ca/los_angeles/superior_court/raw/2026/03/05/aaaaaaaa.html",
        "s3_bucket": "judgemind-document-archive-dev",
        "case_number": "23STCV12345",
        "department": "Dept. 1",
        "judge_name": "Smith, John A.",
        "ruling_text": "The motion for summary judgment is GRANTED.",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_worker(pg_dsn: str = "postgresql://localhost/test") -> tuple[IngestionWorker, MagicMock]:
    """Return a worker with mocked OpenSearch and S3."""
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    # Simulate index doesn't exist so create_index runs without error
    os_mock.indices.exists.return_value = False

    worker = IngestionWorker(
        redis_client=redis_mock,
        pg_dsn=pg_dsn,
        opensearch_client=os_mock,
        s3_client=s3_mock,
        archive_bucket="test-bucket",
    )
    return worker, os_mock


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


def test_derive_court_code_multiword() -> None:
    assert _derive_court_code("CA", "Los Angeles") == "ca-los-angeles"


def test_derive_court_code_single_word() -> None:
    assert _derive_court_code("CA", "Orange") == "ca-orange"


def test_parse_datetime_valid() -> None:
    dt = _parse_datetime("2026-03-05T10:00:00")
    assert dt == datetime(2026, 3, 5, 10, 0, 0)


def test_parse_datetime_none() -> None:
    assert _parse_datetime(None) is None


def test_parse_datetime_invalid() -> None:
    assert _parse_datetime("not-a-date") is None


def test_parse_date_string() -> None:
    assert _parse_date("2026-03-05") == date(2026, 3, 5)


def test_parse_date_datetime() -> None:
    assert _parse_date(datetime(2026, 3, 5, 12, 0)) == date(2026, 3, 5)


def test_parse_date_none() -> None:
    assert _parse_date(None) is None


# ---------------------------------------------------------------------------
# Integration-style tests — IngestionWorker.process_event with mocked Postgres
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_process_event_happy_path(mock_psycopg: MagicMock) -> None:
    """Full happy-path: court, case, document, ruling all written; OS indexed."""
    worker, os_mock = _make_worker()

    # Set up mock connection and cursor
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn

    # upsert_court returns court_id
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
    ]
    mock_cur.rowcount = 1  # insert_document: new row

    event = _make_event()
    worker.process_event(event)

    # Postgres commit was called
    mock_conn.commit.assert_called_once()

    # OpenSearch indexed
    os_mock.index.assert_called_once()
    indexed_doc = os_mock.index.call_args.kwargs["body"]
    assert indexed_doc["document_id"] == event["document_id"]
    assert indexed_doc["state"] == "CA"
    assert indexed_doc["county"] == "Los Angeles"
    assert indexed_doc["ruling_text"] == "The motion for summary judgment is GRANTED."


@patch("ingestion.worker.psycopg")
def test_process_event_no_case_number(mock_psycopg: MagicMock) -> None:
    """Events without case_number use a synthetic UNKNOWN- case number."""
    worker, _ = _make_worker()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",)]
    mock_cur.rowcount = 1

    doc_id = "bbbbbbbb-0000-0000-0000-000000000002"
    event = _make_event(case_number=None, document_id=doc_id)
    worker.process_event(event)

    # Verify that a synthetic case number was upserted
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert f"UNKNOWN-{doc_id}" in all_sql


@patch("ingestion.worker.psycopg")
def test_process_event_no_hearing_date_skips_ruling(mock_psycopg: MagicMock) -> None:
    """Events without hearing_date should still insert document but skip ruling."""
    worker, os_mock = _make_worker()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",)]
    mock_cur.rowcount = 1

    event = _make_event(hearing_date=None)
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # insert_ruling uses a specific SQL pattern — check it was NOT called
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 0


@patch("ingestion.worker.psycopg")
def test_process_event_duplicate_skips_opensearch(mock_psycopg: MagicMock) -> None:
    """If document_id already in Postgres, OpenSearch indexing is skipped."""
    worker, os_mock = _make_worker()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",)]
    mock_cur.rowcount = 0  # document already exists — ON CONFLICT DO NOTHING

    worker.process_event(_make_event())

    # OpenSearch should NOT be called for duplicate
    os_mock.index.assert_not_called()


# ---------------------------------------------------------------------------
# Worker run loop — message processing
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_process_message_acks_on_success(mock_psycopg: MagicMock) -> None:
    """Successful processing results in XACK."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()  # skip actual DB work

    msg_id = b"1234-0"
    data = {b"data": json.dumps(_make_event()).encode()}
    worker._process_message(msg_id, data)

    worker._redis.xack.assert_called_once_with("document.captured", "ingestion-workers", msg_id)


@patch("ingestion.worker.psycopg")
def test_process_message_retries_then_dead_letters(mock_psycopg: MagicMock) -> None:
    """Failed events are retried max_retries times, then dead-lettered (XACK)."""
    worker, _ = _make_worker()
    worker._max_retries = 2
    worker.process_event = MagicMock(side_effect=RuntimeError("db down"))

    msg_id = b"9999-0"
    data = {b"data": json.dumps(_make_event()).encode()}
    worker._process_message(msg_id, data)

    assert worker.process_event.call_count == 2
    worker._redis.xack.assert_called_once()  # dead-letter ack


@patch("ingestion.worker.psycopg")
def test_process_message_dead_letters_malformed_json(mock_psycopg: MagicMock) -> None:
    """Malformed JSON events are dead-lettered immediately without retries."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()

    msg_id = b"bad-0"
    data = {b"data": b"not valid json"}
    worker._process_message(msg_id, data)

    worker.process_event.assert_not_called()
    worker._redis.xack.assert_called_once()
