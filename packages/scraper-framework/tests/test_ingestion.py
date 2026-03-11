"""Tests for the ingestion worker — Postgres and OpenSearch writes.

All external dependencies (Postgres, Redis, OpenSearch, S3) are mocked so
these tests run offline in CI without any infrastructure.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import psycopg
import psycopg.errors
import pytest

from ingestion.db import (
    _derive_court_code,
    insert_document,
    insert_ruling,
    normalize_judge_name,
    normalize_party_name,
    upsert_case,
    upsert_case_party,
    upsert_party,
)
from ingestion.worker import (
    InfrastructureError,
    IngestionWorker,
    _parse_date,
    _parse_datetime,
    is_infrastructure_error,
)

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


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (mock_conn, mock_cur) pair configured for the persistent connection pattern.

    The mock_conn has ``closed = False`` so ``_get_connection()`` reuses it,
    and cursor context-manager protocol is set up for db.py helper functions.
    """
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


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
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    # upsert_court returns court_id
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias found
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges RETURNING id
    ]
    mock_cur.rowcount = 1

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

    # Verify judge resolution and ruling insertion with judge_id
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" in all_sql
    assert "INSERT INTO judge_aliases" in all_sql
    assert "INSERT INTO case_judges" in all_sql


@patch("ingestion.worker.psycopg")
def test_process_event_passes_outcome_and_motion_type_from_event(mock_psycopg: MagicMock) -> None:
    """When event carries outcome/motion_type, they are passed to insert_ruling."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(outcome="denied", motion_type="demurrer")
    worker.process_event(event)

    # Find the INSERT INTO rulings call
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]  # positional args tuple
    # outcome and motion_type should be in the args
    assert "denied" in sql_args
    assert "demurrer" in sql_args


@patch("ingestion.worker.psycopg")
def test_process_event_extracts_outcome_from_ruling_text(mock_psycopg: MagicMock) -> None:
    """When event has no outcome/motion_type, regex extraction from ruling_text is used."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(ruling_text="The motion for summary judgment is GRANTED.")
    # No outcome/motion_type in event — should be extracted from text
    worker.process_event(event)

    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "granted" in sql_args
    assert "msj" in sql_args


@patch("ingestion.worker.psycopg")
def test_process_event_event_fields_override_regex(mock_psycopg: MagicMock) -> None:
    """Event-level outcome/motion_type take precedence over regex extraction."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # ruling_text says "GRANTED" but event says "denied"
    event = _make_event(
        ruling_text="The motion is GRANTED.",
        outcome="denied",
        motion_type="demurrer",
    )
    worker.process_event(event)

    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "denied" in sql_args
    assert "demurrer" in sql_args


@patch("ingestion.worker.psycopg")
def test_process_event_no_case_number_falls_back_to_unknown(mock_psycopg: MagicMock) -> None:
    """Events without case_number AND no extractable case number in ruling_text
    use a synthetic UNKNOWN- case number."""
    worker, _ = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    doc_id = "bbbbbbbb-0000-0000-0000-000000000002"
    # ruling_text has no case number patterns — should fall back to UNKNOWN
    event = _make_event(
        case_number=None,
        document_id=doc_id,
        ruling_text="The motion for summary judgment is GRANTED.",
    )
    worker.process_event(event)

    # Verify that a synthetic case number was upserted
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert f"UNKNOWN-{doc_id}" in all_sql


@patch("ingestion.worker.psycopg")
def test_process_event_extracts_case_number_from_ruling_text(mock_psycopg: MagicMock) -> None:
    """When case_number is None but ruling_text contains a case number,
    the fallback extraction should capture it."""
    worker, _ = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    doc_id = "cccccccc-0000-0000-0000-000000000003"
    event = _make_event(
        case_number=None,
        document_id=doc_id,
        ruling_text="Case Number: 24NNCV02551\nThe motion is GRANTED.",
    )
    worker.process_event(event)

    # Verify the extracted case number was used, NOT the UNKNOWN fallback
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "24NNCV02551" in all_sql
    assert f"UNKNOWN-{doc_id}" not in all_sql


@patch("ingestion.worker.psycopg")
def test_process_event_extracts_judge_name_from_ruling_text(mock_psycopg: MagicMock) -> None:
    """When judge_name is None but ruling_text contains a judge name,
    the fallback extraction should capture it (#401)."""
    worker, _ = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        judge_name=None,
        ruling_text=(
            "DEPARTMENT 56 JUDGE STEVEN A. ELLIS\nCase Number: 24NNCV02551\nThe motion is GRANTED."
        ),
    )
    worker.process_event(event)

    # resolve_judge should have been called — judge name extracted from text
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "judges" in all_sql.lower() or "judge_aliases" in all_sql.lower()


@patch("ingestion.worker.psycopg")
def test_process_event_no_hearing_date_skips_ruling(mock_psycopg: MagicMock) -> None:
    """Events without hearing_date should still insert document but skip ruling."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(hearing_date=None)
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # insert_ruling uses a specific SQL pattern — check it was NOT called
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 0

    # But case_judges should still be populated since judge was resolved
    case_judge_calls = [
        c for c in mock_cur.execute.call_args_list if "INSERT INTO case_judges" in str(c)
    ]
    assert len(case_judge_calls) == 1


@patch("ingestion.worker.psycopg")
def test_process_event_duplicate_skips_opensearch(mock_psycopg: MagicMock) -> None:
    """If document_id already in Postgres, OpenSearch indexing is skipped."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (False,),  # insert_document: RETURNING is_new = False (existing doc, upsert updated)
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1  # upsert always returns rowcount=1

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


# ---------------------------------------------------------------------------
# Error classification tests
# ---------------------------------------------------------------------------


def test_is_infrastructure_error_operational_error() -> None:
    """psycopg.OperationalError (e.g. connection refused) is infra."""
    exc = psycopg.OperationalError("connection refused")
    assert is_infrastructure_error(exc) is True


def test_is_infrastructure_error_undefined_table() -> None:
    """UndefinedTable (missing relation) is an infrastructure error."""
    exc = psycopg.errors.UndefinedTable("relation 'courts' does not exist")
    assert is_infrastructure_error(exc) is True


def test_is_infrastructure_error_undefined_column() -> None:
    """UndefinedColumn is an infrastructure error (schema mismatch)."""
    exc = psycopg.errors.UndefinedColumn("column 'foo' does not exist")
    assert is_infrastructure_error(exc) is True


def test_is_infrastructure_error_connection_error() -> None:
    """Generic ConnectionError is infra."""
    exc = ConnectionError("connection reset")
    assert is_infrastructure_error(exc) is True


def test_is_infrastructure_error_data_error() -> None:
    """psycopg.errors.DataError is a message-level error, not infra."""
    exc = psycopg.errors.DataError("invalid input syntax for type uuid")
    assert is_infrastructure_error(exc) is False


def test_is_infrastructure_error_value_error() -> None:
    """ValueError is a message-level error."""
    exc = ValueError("bad data")
    assert is_infrastructure_error(exc) is False


def test_is_infrastructure_error_key_error() -> None:
    """KeyError is a message-level error."""
    exc = KeyError("missing_field")
    assert is_infrastructure_error(exc) is False


def test_is_infrastructure_error_unique_violation() -> None:
    """UniqueViolation is a message-level error (duplicate data)."""
    exc = psycopg.errors.UniqueViolation("duplicate key")
    assert is_infrastructure_error(exc) is False


def test_is_infrastructure_error_interface_error() -> None:
    """InterfaceError (e.g. connection closed) is infra."""
    exc = psycopg.InterfaceError("connection is closed")
    assert is_infrastructure_error(exc) is True


# ---------------------------------------------------------------------------
# InfrastructureError wrapping
# ---------------------------------------------------------------------------


def test_infrastructure_error_wraps_original() -> None:
    """InfrastructureError should preserve the original exception."""
    original = psycopg.OperationalError("db down")
    wrapped = InfrastructureError(original)
    assert wrapped.__cause__ is original
    assert "db down" in str(wrapped)


# ---------------------------------------------------------------------------
# Worker dead-letter logic with error classification
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_process_message_infra_error_raises_instead_of_dead_letter(
    mock_psycopg: MagicMock,
) -> None:
    """Infrastructure errors should NOT dead-letter. They raise InfrastructureError."""
    worker, _ = _make_worker()
    worker._max_retries = 3
    worker.process_event = MagicMock(
        side_effect=psycopg.OperationalError("connection refused"),
    )

    msg_id = b"infra-0"
    data = {b"data": json.dumps(_make_event()).encode()}

    with pytest.raises(InfrastructureError):
        worker._process_message(msg_id, data)

    # Message must NOT be acknowledged — it stays in the stream for retry after restart
    worker._redis.xack.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_process_message_dead_letters_only_message_errors(
    mock_psycopg: MagicMock,
) -> None:
    """Message-level errors (e.g. ValueError) should still dead-letter after retries."""
    worker, _ = _make_worker()
    worker._max_retries = 2
    worker.process_event = MagicMock(side_effect=ValueError("bad field"))

    msg_id = b"msg-err-0"
    data = {b"data": json.dumps(_make_event()).encode()}
    worker._process_message(msg_id, data)

    assert worker.process_event.call_count == 2
    worker._redis.xack.assert_called_once()  # dead-lettered


@patch("ingestion.worker.psycopg")
def test_process_message_infra_error_on_first_attempt_raises_immediately(
    mock_psycopg: MagicMock,
) -> None:
    """Infra errors should raise on first attempt, not retry max_retries times."""
    worker, _ = _make_worker()
    worker._max_retries = 5
    worker.process_event = MagicMock(
        side_effect=psycopg.errors.UndefinedTable("relation 'courts' does not exist"),
    )

    msg_id = b"infra-1"
    data = {b"data": json.dumps(_make_event()).encode()}

    with pytest.raises(InfrastructureError):
        worker._process_message(msg_id, data)

    # Should only attempt once for infra errors (no point retrying immediately)
    assert worker.process_event.call_count == 1
    worker._redis.xack.assert_not_called()


# ---------------------------------------------------------------------------
# Health check on startup
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_health_check_success(mock_psycopg: MagicMock) -> None:
    """Health check passes when DB is reachable and tables exist."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    # Should not raise
    worker.health_check()


@patch("ingestion.worker.psycopg")
def test_health_check_raises_on_connection_failure(mock_psycopg: MagicMock) -> None:
    """Health check raises InfrastructureError if DB is unreachable."""
    worker, _ = _make_worker()
    mock_psycopg.connect.side_effect = psycopg.OperationalError("connection refused")

    with pytest.raises(InfrastructureError):
        worker.health_check()


@patch("ingestion.worker.psycopg")
def test_health_check_raises_on_missing_tables(mock_psycopg: MagicMock) -> None:
    """Health check raises InfrastructureError if required tables are missing."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.execute.side_effect = psycopg.errors.UndefinedTable("relation 'courts' does not exist")

    with pytest.raises(InfrastructureError):
        worker.health_check()


# ---------------------------------------------------------------------------
# Persistent connection — reuse and reconnection (#476)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_get_connection_creates_on_first_call(mock_psycopg: MagicMock) -> None:
    """_get_connection creates a connection on first call."""
    worker, _ = _make_worker()
    mock_conn, _ = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    conn = worker._get_connection()

    assert conn is mock_conn
    mock_psycopg.connect.assert_called_once_with(worker._pg_dsn, autocommit=False)


@patch("ingestion.worker.psycopg")
def test_get_connection_reuses_existing(mock_psycopg: MagicMock) -> None:
    """_get_connection reuses the same connection on subsequent calls."""
    worker, _ = _make_worker()
    mock_conn, _ = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    conn1 = worker._get_connection()
    conn2 = worker._get_connection()

    assert conn1 is conn2
    # Only one connect call — the connection is reused
    mock_psycopg.connect.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_get_connection_reconnects_when_closed(mock_psycopg: MagicMock) -> None:
    """_get_connection creates a new connection if the existing one is closed."""
    worker, _ = _make_worker()
    mock_conn_1, _ = _make_mock_conn()
    mock_conn_2, _ = _make_mock_conn()
    mock_psycopg.connect.side_effect = [mock_conn_1, mock_conn_2]

    conn1 = worker._get_connection()
    assert conn1 is mock_conn_1

    # Simulate connection being closed (e.g. server restart)
    mock_conn_1.closed = True

    conn2 = worker._get_connection()
    assert conn2 is mock_conn_2
    assert mock_psycopg.connect.call_count == 2


@patch("ingestion.worker.psycopg")
def test_process_event_reuses_connection(mock_psycopg: MagicMock) -> None:
    """Multiple process_event calls reuse the same DB connection."""
    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        # First event
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        None,
        ("judge-uuid-1",),
        # Second event
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        None,
        ("judge-uuid-2",),
    ]
    mock_cur.rowcount = 1

    worker.process_event(_make_event(document_id="doc-1"))
    worker.process_event(_make_event(document_id="doc-2"))

    # Connection was created only once, not per-event
    mock_psycopg.connect.assert_called_once()
    # Both events committed on the same connection
    assert mock_conn.commit.call_count == 2


@patch("ingestion.worker.psycopg")
def test_close_closes_connection(mock_psycopg: MagicMock) -> None:
    """close() closes the persistent connection."""
    worker, _ = _make_worker()
    mock_conn, _ = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    worker._get_connection()  # create the connection
    worker.close()

    mock_conn.close.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_close_idempotent(mock_psycopg: MagicMock) -> None:
    """close() is safe to call multiple times."""
    worker, _ = _make_worker()
    mock_conn, _ = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    worker._get_connection()
    worker.close()
    # After close, _conn is None — second close should not raise
    worker.close()

    mock_conn.close.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_process_event_rollback_on_error(mock_psycopg: MagicMock) -> None:
    """process_event rolls back the transaction on error."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    # Make the first DB call raise
    mock_cur.fetchone.side_effect = psycopg.errors.DataError("bad data")

    with pytest.raises(psycopg.errors.DataError):
        worker.process_event(_make_event())

    mock_conn.rollback.assert_called_once()
    mock_conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Run loop exits on infra errors
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_run_exits_on_infrastructure_error(mock_psycopg: MagicMock) -> None:
    """The run loop should exit (not swallow) InfrastructureError for ECS restart."""
    worker, _ = _make_worker()
    worker._ensure_consumer_group = MagicMock()
    worker.health_check = MagicMock()  # skip health check

    # Make _process_batch raise InfrastructureError
    worker._process_batch = MagicMock(
        side_effect=InfrastructureError(psycopg.OperationalError("db gone")),
    )

    with pytest.raises(InfrastructureError):
        worker.run()


@patch("ingestion.worker.psycopg")
def test_run_closes_connection_on_exit(mock_psycopg: MagicMock) -> None:
    """The run loop closes the persistent DB connection when it stops."""
    worker, _ = _make_worker()
    mock_conn, _ = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    worker._ensure_consumer_group = MagicMock()
    worker.health_check = MagicMock()

    # Simulate a single batch then KeyboardInterrupt to stop the loop
    worker._process_batch = MagicMock(side_effect=KeyboardInterrupt)

    # Establish a connection so close() has something to clean up
    worker._conn = mock_conn

    worker.run()

    mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# Judge name normalization
# ---------------------------------------------------------------------------


def test_normalize_judge_name_last_comma_first() -> None:
    """'LAST, FIRST M.' format is converted to 'First M. Last'."""
    assert normalize_judge_name("Smith, John A.") == "John A. Smith"


def test_normalize_judge_name_first_last() -> None:
    """'FIRST LAST' format is title-cased."""
    assert normalize_judge_name("john smith") == "John Smith"


def test_normalize_judge_name_all_caps_comma() -> None:
    """All-caps 'LUNA, BOBBY P.' format is normalized."""
    assert normalize_judge_name("LUNA, BOBBY P.") == "Bobby P. Luna"


def test_normalize_judge_name_extra_whitespace() -> None:
    """Extra whitespace is collapsed and stripped."""
    assert normalize_judge_name("  Smith ,  John   A. ") == "John A. Smith"


def test_normalize_judge_name_already_normal() -> None:
    """Already normalized name passes through."""
    assert normalize_judge_name("John A. Smith") == "John A. Smith"


def test_normalize_judge_name_single_name() -> None:
    """Single name is title-cased."""
    assert normalize_judge_name("SMITH") == "Smith"


# ---------------------------------------------------------------------------
# Judge name normalization — truncation regression tests (#327)
# ---------------------------------------------------------------------------


def test_normalize_judge_name_preserves_full_length() -> None:
    """Regression: normalize must not truncate names (issue #327).
    'James I. Montgomery' was being stored as 'James I. Montgomer'."""
    assert normalize_judge_name("James I. Montgomery") == "James I. Montgomery"


def test_normalize_judge_name_long_name_with_suffix() -> None:
    """Names with suffixes like 'III' or 'Jr.' preserve correct casing."""
    assert normalize_judge_name("Arthur Hester III") == "Arthur Hester III"
    assert normalize_judge_name("Arthur Hester Jr.") == "Arthur Hester Jr."
    assert normalize_judge_name("Arthur Hester Sr.") == "Arthur Hester Sr."
    assert normalize_judge_name("Arthur Hester II") == "Arthur Hester II"
    assert normalize_judge_name("Arthur Hester IV") == "Arthur Hester IV"


def test_normalize_judge_name_hyphenated_surname() -> None:
    """Hyphenated surnames are preserved at full length."""
    assert normalize_judge_name("Maria Santos-Rodriguez") == "Maria Santos-Rodriguez"


def test_normalize_judge_name_long_compound_name() -> None:
    """Long compound names with multiple parts are not truncated."""
    name = "Christopher Michael Alexander Van Der Berg"
    result = normalize_judge_name(name)
    assert result == "Christopher Michael Alexander Van Der Berg"
    assert len(result) == len(name)


def test_normalize_judge_name_preserves_periods() -> None:
    """Middle initials with periods are preserved without truncation."""
    assert normalize_judge_name("William A. Crowfoot") == "William A. Crowfoot"
    assert normalize_judge_name("H. Shaina Colover") == "H. Shaina Colover"


def test_normalize_judge_name_all_caps_long_name() -> None:
    """All-caps long name is title-cased without truncation."""
    name = "JAMES I. MONTGOMERY"
    result = normalize_judge_name(name)
    assert result == "James I. Montgomery"
    assert len(result) == len(name)


# ---------------------------------------------------------------------------
# Judge name normalization — honorific prefix stripping (#331)
# ---------------------------------------------------------------------------


def test_normalize_judge_name_strips_hon_dot() -> None:
    """'Hon. Joseph B. Widman' → 'Joseph B. Widman'."""
    assert normalize_judge_name("Hon. Joseph B. Widman") == "Joseph B. Widman"


def test_normalize_judge_name_strips_hon_no_dot() -> None:
    """'Hon Joseph B. Widman' → 'Joseph B. Widman'."""
    assert normalize_judge_name("Hon Joseph B. Widman") == "Joseph B. Widman"


def test_normalize_judge_name_strips_honorable() -> None:
    """'Honorable Jane Doe' → 'Jane Doe'."""
    assert normalize_judge_name("Honorable Jane Doe") == "Jane Doe"


def test_normalize_judge_name_strips_the_honorable() -> None:
    """'The Honorable Jane Doe' → 'Jane Doe'."""
    assert normalize_judge_name("The Honorable Jane Doe") == "Jane Doe"


def test_normalize_judge_name_strips_judge_prefix() -> None:
    """'Judge Bobby P. Luna' → 'Bobby P. Luna'."""
    assert normalize_judge_name("Judge Bobby P. Luna") == "Bobby P. Luna"


def test_normalize_judge_name_strips_judge_colon() -> None:
    """'Judge: Bobby P. Luna' → 'Bobby P. Luna'."""
    assert normalize_judge_name("Judge: Bobby P. Luna") == "Bobby P. Luna"


def test_normalize_judge_name_strips_hon_case_insensitive() -> None:
    """Honorific stripping is case-insensitive."""
    assert normalize_judge_name("HON. JOHN SMITH") == "John Smith"
    assert normalize_judge_name("the honorable john smith") == "John Smith"
    assert normalize_judge_name("JUDGE MARK MOONEY") == "Mark Mooney"


def test_normalize_judge_name_hon_with_comma_format() -> None:
    """'Hon. SMITH, JOHN A.' → comma-flip then title-case."""
    assert normalize_judge_name("Hon. Smith, John A.") == "John A. Smith"


def test_normalize_judge_name_honorable_with_allcaps() -> None:
    """'THE HONORABLE JOSEPH WIDMAN' → 'Joseph Widman'."""
    assert normalize_judge_name("THE HONORABLE JOSEPH WIDMAN") == "Joseph Widman"


def test_normalize_judge_name_consistency_with_without_hon() -> None:
    """Same judge with and without 'Hon.' should produce the same canonical name.

    This is the core bug from issue #331: 'Joseph Widman' and 'Hon. Joseph B. Widman'
    produce different canonical names because the prefix was not stripped.
    """
    # With middle initial, both variants should normalize the same way
    assert normalize_judge_name("Hon. Joseph B. Widman") == normalize_judge_name("Joseph B. Widman")
    # Plain name without prefix should still work
    assert normalize_judge_name("Joseph Widman") == "Joseph Widman"


# ---------------------------------------------------------------------------
# Judge name normalization — Arbitrator prefix stripping (#589)
# ---------------------------------------------------------------------------


def test_normalize_judge_name_strips_arbitrator() -> None:
    """'Arbitrator Howard B. Miller' -> 'Howard B. Miller'."""
    assert normalize_judge_name("Arbitrator Howard B. Miller") == "Howard B. Miller"


def test_normalize_judge_name_strips_arbitrator_case_insensitive() -> None:
    """Arbitrator stripping is case-insensitive."""
    assert normalize_judge_name("ARBITRATOR HOWARD MILLER") == "Howard Miller"
    assert normalize_judge_name("arbitrator Jane Doe") == "Jane Doe"


# ---------------------------------------------------------------------------
# Judge name normalization — suffix handling (#589)
# ---------------------------------------------------------------------------


def test_normalize_judge_name_suffix_at_beginning() -> None:
    """'Jr. Edward B. Moreton' -> 'Edward B. Moreton Jr.' — suffix moved to end."""
    assert normalize_judge_name("Jr. Edward B. Moreton") == "Edward B. Moreton Jr."


def test_normalize_judge_name_sr_suffix_at_beginning() -> None:
    """'Sr. John Smith' -> 'John Smith Sr.'."""
    assert normalize_judge_name("Sr. John Smith") == "John Smith Sr."


def test_normalize_judge_name_roman_numeral_suffix_at_beginning() -> None:
    """'III Robert Jones' -> 'Robert Jones III'."""
    assert normalize_judge_name("III Robert Jones") == "Robert Jones III"


def test_normalize_judge_name_suffix_at_end_unchanged() -> None:
    """Suffix already at end should stay there."""
    assert normalize_judge_name("Edward B. Moreton Jr.") == "Edward B. Moreton Jr."
    assert normalize_judge_name("Robert Jones III") == "Robert Jones III"


# ---------------------------------------------------------------------------
# Judge name normalization — garbage rejection (#589)
# ---------------------------------------------------------------------------


def test_normalize_judge_name_rejects_too_long() -> None:
    """Names longer than 80 chars are rejected as garbage."""
    long_name = "A" * 81
    assert normalize_judge_name(long_name) is None


def test_normalize_judge_name_rejects_unicode_junk() -> None:
    """Names with unicode replacement characters are cleaned; pure junk rejected."""
    # Pure replacement characters
    assert normalize_judge_name("\ufffd") is None
    assert normalize_judge_name("\ufffd \ufffd\ufffd \ufffd") is None
    # Mixed: replacement chars stripped, valid name remains
    assert normalize_judge_name("\ufffd John A. Smith\ufffd") == "John A. Smith"


def test_normalize_judge_name_rejects_paragraph_text() -> None:
    """Paragraph text captured as a name is rejected."""
    paragraph = "2026 ___ Hon. Tiana J. Murillo Moving Party Is Ordered to appear"
    assert normalize_judge_name(paragraph) is None


def test_normalize_judge_name_rejects_ruling_text_ordered() -> None:
    """Strings containing 'Ordered to' are rejected."""
    assert normalize_judge_name("Judge Smith Ordered to appear in court") is None


def test_normalize_judge_name_rejects_ruling_text_fragments() -> None:
    """Common ruling text fragments trigger rejection."""
    assert normalize_judge_name("Plaintiff John Smith") is None
    assert normalize_judge_name("Defendant Corp LLC") is None


def test_normalize_judge_name_valid_names_pass() -> None:
    """Ensure valid names are not rejected by garbage checks."""
    assert normalize_judge_name("John A. Smith") == "John A. Smith"
    assert normalize_judge_name("H. Shaina Colover") == "H. Shaina Colover"
    assert normalize_judge_name("Edward B. Moreton Jr.") == "Edward B. Moreton Jr."
    assert normalize_judge_name("Maria Santos-Rodriguez") == "Maria Santos-Rodriguez"


# ---------------------------------------------------------------------------
# Judge name validation — _looks_like_valid_judge_name (#589)
# ---------------------------------------------------------------------------


def test_looks_like_valid_judge_name_rejects_single_word() -> None:
    """Single-word names (last name only) are rejected."""
    from ingestion.db import _looks_like_valid_judge_name

    assert _looks_like_valid_judge_name("Bahadori") is False
    assert _looks_like_valid_judge_name("Crowfoot") is False
    assert _looks_like_valid_judge_name("Smith") is False


def test_looks_like_valid_judge_name_accepts_full_names() -> None:
    """Full names with first + last are accepted."""
    from ingestion.db import _looks_like_valid_judge_name

    assert _looks_like_valid_judge_name("John Smith") is True
    assert _looks_like_valid_judge_name("Bobby P. Luna") is True
    assert _looks_like_valid_judge_name("H. Shaina Colover") is True


def test_looks_like_valid_judge_name_rejects_empty() -> None:
    """Empty or whitespace-only strings are rejected."""
    from ingestion.db import _looks_like_valid_judge_name

    assert _looks_like_valid_judge_name("") is False
    assert _looks_like_valid_judge_name("   ") is False


# ---------------------------------------------------------------------------
# resolve_judge — invalid name rejection (#589)
# ---------------------------------------------------------------------------


def test_resolve_judge_rejects_garbage_name() -> None:
    """resolve_judge returns None for garbage names instead of creating records."""
    from ingestion.db import resolve_judge

    mock_conn = MagicMock()

    # Garbage name — should not touch the DB at all
    result = resolve_judge(
        mock_conn,
        "2026 ___ Hon. Tiana J. Murillo Moving Party Is Ordered to appear",
        "court-uuid-1",
    )
    assert result is None


def test_resolve_judge_rejects_single_word_name() -> None:
    """resolve_judge returns None for single-word (last-name-only) names."""
    from ingestion.db import resolve_judge

    mock_conn = MagicMock()
    result = resolve_judge(mock_conn, "Bahadori", "court-uuid-1")
    assert result is None


def test_resolve_judge_rejects_unicode_junk() -> None:
    """resolve_judge returns None for unicode junk names."""
    from ingestion.db import resolve_judge

    mock_conn = MagicMock()
    result = resolve_judge(mock_conn, "\ufffd\ufffd \ufffd", "court-uuid-1")
    assert result is None


# ---------------------------------------------------------------------------
# Judge resolution (resolve_judge)
# ---------------------------------------------------------------------------


def test_resolve_judge_existing_alias() -> None:
    """resolve_judge returns existing judge_id when alias matches."""
    from ingestion.db import resolve_judge

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # Simulate existing alias found
    mock_cur.fetchone.return_value = ("existing-judge-uuid",)

    result = resolve_judge(mock_conn, "Smith, John A.", "court-uuid-1")

    assert result == "existing-judge-uuid"
    # Should NOT insert a new judge
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" not in all_sql


def test_resolve_judge_creates_new() -> None:
    """resolve_judge creates a new judge and alias when no match exists."""
    from ingestion.db import resolve_judge

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # No existing alias, then INSERT returns new judge id
    mock_cur.fetchone.side_effect = [None, ("new-judge-uuid",)]

    result = resolve_judge(mock_conn, "Luna, Bobby P.", "court-uuid-1")

    assert result == "new-judge-uuid"
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" in all_sql
    assert "INSERT INTO judge_aliases" in all_sql
    # Verify the canonical name was normalized
    assert "Bobby P. Luna" in all_sql


# ---------------------------------------------------------------------------
# upsert_case_judge
# ---------------------------------------------------------------------------


def test_upsert_case_judge_inserts() -> None:
    """upsert_case_judge executes the INSERT INTO case_judges SQL."""
    from ingestion.db import upsert_case_judge

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    upsert_case_judge(mock_conn, "case-uuid-1", "judge-uuid-1", date(2026, 3, 5))

    mock_cur.execute.assert_called_once()
    sql = str(mock_cur.execute.call_args)
    assert "INSERT INTO case_judges" in sql
    assert "ON CONFLICT" in sql


# ---------------------------------------------------------------------------
# process_event — judge resolution integration
# ---------------------------------------------------------------------------


@patch("ingestion.worker.fetch_department_judge_mapping", return_value={})
@patch("ingestion.worker.psycopg")
def test_process_event_no_judge_name_leaves_judge_id_null(
    mock_psycopg: MagicMock, _mock_fetch_dept: MagicMock
) -> None:
    """Events without judge_name should not resolve a judge — judge_id stays NULL.

    The LA dept-to-judge mapping returns empty, so no dept lookup resolves either.
    """
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(judge_name=None)
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # No judge resolution should happen
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" not in all_sql
    assert "judge_aliases" not in all_sql
    assert "case_judges" not in all_sql


@patch("ingestion.worker.psycopg")
def test_process_event_with_existing_judge_alias(mock_psycopg: MagicMock) -> None:
    """When judge alias already exists, reuse the existing judge_id."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        ("existing-judge-uuid",),  # resolve_judge: found existing alias
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # Should not create a new judge
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" not in all_sql
    # But should still insert ruling and case_judges
    assert "INTO rulings" in all_sql
    assert "INSERT INTO case_judges" in all_sql


# ---------------------------------------------------------------------------
# upsert_case — case_title support
# ---------------------------------------------------------------------------


def test_upsert_case_passes_case_title_in_sql() -> None:
    """upsert_case includes case_title in INSERT and COALESCE on conflict."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.return_value = ("case-uuid-1",)

    result = upsert_case(mock_conn, "23STCV12345", "court-uuid-1", case_title="Smith v. Jones")

    assert result == "case-uuid-1"
    sql = str(mock_cur.execute.call_args)
    assert "case_title" in sql
    assert "COALESCE" in sql
    assert "Smith v. Jones" in sql


def test_upsert_case_none_title_still_works() -> None:
    """upsert_case with case_title=None passes NULL and uses COALESCE."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.return_value = ("case-uuid-1",)

    result = upsert_case(mock_conn, "23STCV12345", "court-uuid-1", case_title=None)

    assert result == "case-uuid-1"
    sql = str(mock_cur.execute.call_args)
    assert "COALESCE" in sql


# ---------------------------------------------------------------------------
# process_event — case_title pass-through
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_process_event_passes_case_title_to_upsert_case(mock_psycopg: MagicMock) -> None:
    """When event carries case_title, it is passed to upsert_case."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # batch_upsert_parties: executemany RETURNING ids for caption-extracted parties
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    # nextset for executemany returning
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    event = _make_event(case_title="Aasi v. American Honda")
    worker.process_event(event)

    # Find the INSERT INTO cases call and verify case_title is in the args
    case_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO cases" in str(c)]
    assert len(case_calls) == 1
    sql_args = case_calls[0][0][1]  # positional args tuple
    assert "Aasi v. American Honda" in sql_args


@patch("ingestion.worker.psycopg")
def test_process_event_without_case_title_passes_none(mock_psycopg: MagicMock) -> None:
    """When event has no case_title, None is passed to upsert_case."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event()  # no case_title key
    worker.process_event(event)

    # Verify case_title=None is in the SQL args
    case_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO cases" in str(c)]
    assert len(case_calls) == 1
    sql_args = case_calls[0][0][1]
    # None should be the 4th argument (case_title)
    assert None in sql_args


# ---------------------------------------------------------------------------
# Party name normalization
# ---------------------------------------------------------------------------


def test_normalize_party_name_basic() -> None:
    """Basic party name normalization."""
    assert normalize_party_name("  sumayya aasi  ") == "Sumayya Aasi"


def test_normalize_party_name_collapses_whitespace() -> None:
    """Extra whitespace is collapsed."""
    assert normalize_party_name("AMERICAN  HONDA  MOTOR  CO.") == "American Honda Motor Co."


def test_normalize_party_name_title_case() -> None:
    """Names are title-cased."""
    assert normalize_party_name("DAVID KEICHLINE") == "David Keichline"


# ---------------------------------------------------------------------------
# upsert_party
# ---------------------------------------------------------------------------


def test_upsert_party_existing_alias() -> None:
    """upsert_party returns existing party_id when alias matches."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # Simulate existing alias found
    mock_cur.fetchone.return_value = ("existing-party-uuid",)

    result = upsert_party(mock_conn, "Sumayya Aasi")

    assert result == "existing-party-uuid"
    # Should NOT insert a new party
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO parties" not in all_sql


def test_upsert_party_creates_new() -> None:
    """upsert_party creates a new party and alias when no match exists."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # No existing alias, then INSERT returns new party id
    mock_cur.fetchone.side_effect = [None, ("new-party-uuid",)]

    result = upsert_party(mock_conn, "Sumayya Aasi")

    assert result == "new-party-uuid"
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO parties" in all_sql
    assert "INSERT INTO party_aliases" in all_sql


def test_upsert_party_with_party_type() -> None:
    """upsert_party passes party_type to INSERT."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_cur.fetchone.side_effect = [None, ("new-party-uuid",)]

    result = upsert_party(mock_conn, "Acme Corp", party_type="corporation")

    assert result == "new-party-uuid"
    # Verify party_type was passed
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO parties" in str(c)]
    assert len(insert_calls) == 1
    sql_args = insert_calls[0][0][1]
    assert "corporation" in sql_args


# ---------------------------------------------------------------------------
# upsert_case_party
# ---------------------------------------------------------------------------


def test_upsert_case_party_executes_insert() -> None:
    """upsert_case_party executes the INSERT SQL."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    upsert_case_party(mock_conn, "case-uuid-1", "party-uuid-1", "plaintiff")

    mock_cur.execute.assert_called_once()
    sql = str(mock_cur.execute.call_args)
    assert "INSERT INTO case_parties" in sql
    assert "NOT EXISTS" in sql


# ---------------------------------------------------------------------------
# process_event — party processing
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_process_event_with_parties(mock_psycopg: MagicMock) -> None:
    """When event carries parties, party records and case_party links are created."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # batch_upsert_parties: executemany RETURNING ids
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    # nextset for executemany returning
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    event = _make_event(
        parties=[
            {"name": "Sumayya Aasi", "role": "plaintiff"},
            {"name": "American Honda Motor Co.", "role": "defendant"},
        ]
    )
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # batch_upsert_parties uses executemany for parties, aliases, and case_parties
    all_executemany_sql = " ".join(str(c) for c in mock_cur.executemany.call_args_list)
    assert "INSERT INTO parties" in all_executemany_sql
    assert "party_aliases" in all_executemany_sql
    assert "case_parties" in all_executemany_sql


@patch("ingestion.worker.psycopg")
def test_process_event_without_parties_no_party_calls(mock_psycopg: MagicMock) -> None:
    """When event has no parties, no party DB calls are made."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event()  # no parties key
    worker.process_event(event)

    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO case_parties" not in all_sql


@patch("ingestion.worker.psycopg")
def test_process_event_extracts_parties_from_case_title(mock_psycopg: MagicMock) -> None:
    """When event has no parties but has a case_title with 'v.', parties are
    extracted from the caption as a fallback (#328)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # batch_upsert_parties: executemany RETURNING ids
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    # nextset for executemany returning
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    event = _make_event(
        case_title="Caldera v. Techno-Advanced, Inc.",
    )  # no parties key — fallback will extract from case_title
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    all_executemany_sql = " ".join(str(c) for c in mock_cur.executemany.call_args_list)
    assert "INSERT INTO parties" in all_executemany_sql
    assert "case_parties" in all_executemany_sql


# ---------------------------------------------------------------------------
# Upsert behavior — insert_document and insert_ruling
# ---------------------------------------------------------------------------


def test_insert_document_upsert_updates_mutable_fields() -> None:
    """insert_document ON CONFLICT updates hearing_date and case_id."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # Simulate existing row (xmax != 0 → is_new = False)
    mock_cur.fetchone.return_value = (False,)

    result = insert_document(
        mock_conn,
        document_id="doc-uuid-1",
        case_id="case-uuid-1",
        court_id="court-uuid-1",
        content_format="html",
        content_hash="abc123",
        s3_key="key",
        s3_bucket="bucket",
        source_url="https://example.com",
        scraper_id="test-scraper",
        captured_at=datetime(2026, 3, 5),
        hearing_date=date(2026, 3, 5),
    )

    assert result is False  # not a new insert
    sql = str(mock_cur.execute.call_args)
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "hearing_date" in sql
    assert "case_id" in sql


def test_insert_document_upsert_new_row_returns_true() -> None:
    """insert_document returns True for genuinely new rows."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_cur.fetchone.return_value = (True,)

    result = insert_document(
        mock_conn,
        document_id="doc-uuid-2",
        case_id="case-uuid-1",
        court_id="court-uuid-1",
        content_format="html",
        content_hash="def456",
        s3_key="key2",
        s3_bucket="bucket",
        source_url="https://example.com/2",
        scraper_id="test-scraper",
        captured_at=datetime(2026, 3, 6),
        hearing_date=date(2026, 3, 6),
    )

    assert result is True


def test_insert_ruling_upsert_uses_on_conflict() -> None:
    """insert_ruling uses ON CONFLICT (document_id) DO UPDATE."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    insert_ruling(
        mock_conn,
        document_id="doc-uuid-1",
        case_id="case-uuid-1",
        court_id="court-uuid-1",
        hearing_date=date(2026, 3, 5),
        ruling_text="Motion GRANTED.",
        department="Dept. 1",
        judge_id="judge-uuid-1",
        outcome="granted",
        motion_type="msj",
    )

    sql = str(mock_cur.execute.call_args)
    assert "ON CONFLICT (document_id) DO UPDATE" in sql
    assert "COALESCE" in sql
    # Verify all updatable fields are in the ON CONFLICT clause
    assert "judge_id" in sql
    assert "outcome" in sql
    assert "motion_type" in sql
    assert "ruling_text" in sql
    assert "department" in sql


def test_insert_ruling_upsert_no_duplicate_document_id_param() -> None:
    """insert_ruling args should not contain duplicate document_id."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    insert_ruling(
        mock_conn,
        document_id="doc-uuid-1",
        case_id="case-uuid-1",
        court_id="court-uuid-1",
        hearing_date=date(2026, 3, 5),
        ruling_text="Motion GRANTED.",
        department="Dept. 1",
    )

    # The old pattern had document_id twice in the args tuple. New pattern has it once.
    sql_args = mock_cur.execute.call_args[0][1]
    doc_id_count = sum(1 for a in sql_args if a == "doc-uuid-1")
    assert doc_id_count == 1


# ---------------------------------------------------------------------------
# LLM extraction integration — mock LLM response, verify fields populated
# ---------------------------------------------------------------------------


@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_extraction_populates_missing_fields(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
) -> None:
    """When LLM extraction returns results, missing fields are populated from LLM."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, os_mock = _make_worker()
    # Simulate an anthropic client being configured
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # batch_upsert_parties: executemany RETURNING ids for LLM parties
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    # nextset for executemany returning
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    # Configure LLM to return structured results
    mock_llm.return_value = LLMExtractionResult(
        judge_name="Steven A. Ellis",
        hearing_date=date(2026, 3, 10),
        department="56",
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="24NNCV02551",
                case_title="Doe v. Roe",
                outcome="granted",
                motion_type="msj",
                parties=[
                    {"name": "John Doe", "role": "plaintiff"},
                    {"name": "Jane Roe", "role": "defendant"},
                ],
            )
        ],
    )

    # Event with minimal fields — LLM should fill in the rest
    event = _make_event(
        case_number=None,
        case_title=None,
        judge_name=None,
        department=None,
        hearing_date=None,
        outcome=None,
        motion_type=None,
        ruling_text="Some ruling text that LLM will parse",
    )
    worker.process_event(event)

    # Verify LLM was called
    mock_llm.assert_called_once()

    # Verify fields from LLM were used in DB writes
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)

    # Case number from LLM
    assert "24NNCV02551" in all_sql

    # Ruling should have been inserted (hearing_date from LLM)
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "granted" in sql_args
    assert "msj" in sql_args

    # Judge was resolved (from LLM result)
    assert "judges" in all_sql.lower()

    # Parties from LLM were written via batch_upsert_parties
    all_executemany_sql = " ".join(str(c) for c in mock_cur.executemany.call_args_list)
    assert "INSERT INTO parties" in all_executemany_sql
    assert "case_parties" in all_executemany_sql


@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_failure_falls_back_to_regex(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
) -> None:
    """When LLM extraction returns None, regex extraction is used as fallback."""
    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # LLM returns None (simulating API failure)
    mock_llm.return_value = None

    event = _make_event(
        outcome=None,
        motion_type=None,
        ruling_text="The motion for summary judgment is GRANTED.",
    )
    worker.process_event(event)

    # LLM was attempted
    mock_llm.assert_called_once()

    # Regex fallback should have extracted outcome and motion_type
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "granted" in sql_args
    assert "msj" in sql_args


@patch("ingestion.worker.psycopg")
def test_process_event_no_anthropic_client_uses_regex_only(
    mock_psycopg: MagicMock,
) -> None:
    """When anthropic client is None (no API key), regex-only mode is used."""
    worker, os_mock = _make_worker()
    # Ensure no anthropic client
    worker._llm_client = None

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        outcome=None,
        motion_type=None,
        ruling_text="The motion for summary judgment is GRANTED.",
    )
    worker.process_event(event)

    # Regex should still extract outcome and motion_type
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "granted" in sql_args
    assert "msj" in sql_args


@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_matches_ruling_by_case_number(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
) -> None:
    """When event has a case_number and LLM returns multiple rulings,
    the matching ruling is used."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # batch_upsert_parties: executemany RETURNING ids for caption-extracted parties
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    # nextset for executemany returning
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    # LLM returns two rulings — one matches the event's case_number
    mock_llm.return_value = LLMExtractionResult(
        judge_name="Ellis",
        hearing_date=date(2026, 3, 10),
        department="56",
        case_count=2,
        rulings=[
            LLMRulingResult(
                case_number="24NNCV99999",
                case_title="Other v. Case",
                outcome="denied",
                motion_type="demurrer",
                parties=[],
            ),
            LLMRulingResult(
                case_number="23STCV12345",
                case_title="Smith v. Jones",
                outcome="granted",
                motion_type="msj",
                parties=[],
            ),
        ],
    )

    # Event has case_number that matches the second ruling
    event = _make_event(
        case_number="23STCV12345",
        outcome=None,
        motion_type=None,
        case_title=None,
        ruling_text="Some ruling text",
    )
    worker.process_event(event)

    # Should use the second ruling (matching case_number), not the first
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "granted" in sql_args
    assert "msj" in sql_args

    # case_title from the matched ruling
    case_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO cases" in str(c)]
    assert len(case_calls) == 1
    case_args = case_calls[0][0][1]
    assert "Smith v. Jones" in case_args


@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_scraper_fields_take_precedence_over_llm(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
) -> None:
    """Scraper-provided fields are not overwritten by LLM results."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        ("existing-judge-uuid",),  # resolve_judge: existing alias
        # batch_upsert_parties: executemany RETURNING id for LLM party
        ("party-uuid-1",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    # nextset for executemany returning
    mock_cur.nextset.side_effect = [False]
    mock_cur.rowcount = 1

    # LLM returns different values than the scraper
    mock_llm.return_value = LLMExtractionResult(
        judge_name="Wrong Judge",
        hearing_date=date(2099, 1, 1),
        department="99",
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="WRONG-CASE",
                case_title="Wrong v. Title",
                outcome="denied",
                motion_type="demurrer",
                parties=[{"name": "Wrong", "role": "plaintiff"}],
            )
        ],
    )

    # Event has ALL fields populated by scraper — LLM should not override
    event = _make_event(
        case_number="23STCV12345",
        case_title="Correct v. Title",
        judge_name="Correct Judge",
        department="Dept. 1",
        hearing_date="2026-03-05",
        outcome="granted",
        motion_type="msj",
        ruling_text="Some ruling text",
    )
    worker.process_event(event)

    # Scraper values should be used, not LLM values
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "granted" in sql_args
    assert "msj" in sql_args
    # "denied" and "demurrer" from LLM should NOT appear
    assert "denied" not in sql_args
    assert "demurrer" not in sql_args

    case_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO cases" in str(c)]
    assert len(case_calls) == 1
    case_args = case_calls[0][0][1]
    assert "Correct v. Title" in case_args


# ---------------------------------------------------------------------------
# _match_ruling unit tests
# ---------------------------------------------------------------------------


def test_match_ruling_finds_by_case_number() -> None:
    """_match_ruling returns the ruling matching the event's case_number."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult
    from ingestion.worker import _match_ruling

    result = LLMExtractionResult(
        rulings=[
            LLMRulingResult(case_number="AAA"),
            LLMRulingResult(case_number="BBB"),
        ]
    )
    assert _match_ruling(result, "BBB").case_number == "BBB"


def test_match_ruling_falls_back_to_first() -> None:
    """_match_ruling returns the first ruling if no case_number match."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult
    from ingestion.worker import _match_ruling

    result = LLMExtractionResult(
        rulings=[
            LLMRulingResult(case_number="AAA"),
            LLMRulingResult(case_number="BBB"),
        ]
    )
    assert _match_ruling(result, "CCC").case_number == "AAA"


def test_match_ruling_no_case_number_returns_first() -> None:
    """_match_ruling returns the first ruling when event has no case_number."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult
    from ingestion.worker import _match_ruling

    result = LLMExtractionResult(rulings=[LLMRulingResult(case_number="AAA")])
    assert _match_ruling(result, None).case_number == "AAA"


def test_match_ruling_empty_rulings_returns_none() -> None:
    """_match_ruling returns None when LLM returned no rulings."""
    from ingestion.llm_extract import LLMExtractionResult
    from ingestion.worker import _match_ruling

    result = LLMExtractionResult(rulings=[])
    assert _match_ruling(result, "AAA") is None


# ---------------------------------------------------------------------------
# LA department-to-judge lookup tests (#584)
# ---------------------------------------------------------------------------


@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe", "52": "John Smith"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_resolves_judge_when_name_missing(
    mock_psycopg: MagicMock, _mock_fetch_dept: MagicMock
) -> None:
    """LA event with department but no judge_name resolves judge via dept map."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-dept",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(judge_name=None, department="1")
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # Judge resolution should have happened via dept lookup
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "judges" in all_sql.lower() or "judge_aliases" in all_sql.lower()


@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_skipped_when_judge_name_present(
    mock_psycopg: MagicMock, mock_fetch_dept: MagicMock
) -> None:
    """LA event with judge_name already present skips dept lookup entirely."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(judge_name="Already Known", department="1")
    worker.process_event(event)

    # fetch_department_judge_mapping should NOT have been called
    mock_fetch_dept.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_skipped_for_non_la_county(mock_psycopg: MagicMock) -> None:
    """Non-LA county events should not trigger the dept lookup."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        judge_name=None,
        department="1",
        state="CA",
        county="Orange",
        ruling_text="Short ruling text.",
    )
    worker.process_event(event)

    # _la_dept_map should remain None (never fetched)
    assert worker._la_dept_map is None


@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_unmapped_dept_leaves_judge_null(
    mock_psycopg: MagicMock, _mock_fetch_dept: MagicMock
) -> None:
    """LA event with department not in mapping — judge stays NULL."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(judge_name=None, department="999")
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # No judge resolution should happen
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" not in all_sql
    assert "case_judges" not in all_sql


@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_map_cached_across_events(
    mock_psycopg: MagicMock, mock_fetch_dept: MagicMock
) -> None:
    """The dept map should be fetched once and reused for subsequent events."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    # Process two LA events with no judge_name
    for doc_id in ("doc-1", "doc-2"):
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),  # upsert_court
            ("case-uuid-1",),  # upsert_case
            (True,),  # insert_document
            None,  # resolve_judge: no existing alias
            ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        ]
        mock_cur.rowcount = 1
        event = _make_event(document_id=doc_id, judge_name=None, department="1")
        worker.process_event(event)
        mock_conn.reset_mock()
        mock_cur.reset_mock()

    # fetch_department_judge_mapping should have been called exactly once
    mock_fetch_dept.assert_called_once()


@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    side_effect=Exception("Network error"),
)
@patch("ingestion.worker.psycopg")
def test_la_dept_map_fetch_failure_degrades_gracefully(
    mock_psycopg: MagicMock, _mock_fetch_dept: MagicMock
) -> None:
    """If dept map fetch fails, worker continues without dept lookup."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(judge_name=None, department="1")
    worker.process_event(event)

    # Worker should still commit — just without judge_id
    mock_conn.commit.assert_called_once()

    # Dept map should be set to empty dict (not None — fetch was attempted)
    assert worker._la_dept_map == {}

    # No judge resolution should happen
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" not in all_sql


# ---------------------------------------------------------------------------
# PDF binary preprocessing in process_event
# ---------------------------------------------------------------------------


@patch("ingestion.worker.extract_text_from_pdf")
@patch("ingestion.worker.is_pdf_binary")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_extracts_text_from_pdf_binary(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_is_pdf: MagicMock,
    mock_extract_pdf: MagicMock,
) -> None:
    """When ruling_text is raw PDF binary, text is extracted before LLM/regex processing."""
    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # Simulate raw PDF binary content
    mock_is_pdf.return_value = True
    mock_extract_pdf.return_value = "The motion for summary judgment is GRANTED."

    # LLM returns None so regex fallback kicks in
    mock_llm.return_value = None

    event = _make_event(
        ruling_text="%PDF-1.4 fake binary content",
        content_format="pdf",
        outcome=None,
        motion_type=None,
    )
    worker.process_event(event)

    # PDF binary should have been detected and text extracted
    mock_is_pdf.assert_called_once()
    mock_extract_pdf.assert_called_once_with("%PDF-1.4 fake binary content")

    # LLM should receive the extracted text, not raw binary
    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    assert call_kwargs[1]["document_text"] == "The motion for summary judgment is GRANTED."

    # Ruling should be inserted (regex fallback extracts outcome from extracted text)
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1


@patch("ingestion.worker.extract_text_from_pdf")
@patch("ingestion.worker.is_pdf_binary")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_pdf_extraction_failure_continues(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_is_pdf: MagicMock,
    mock_extract_pdf: MagicMock,
) -> None:
    """When PDF text extraction returns None, processing continues with original text."""
    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # Simulate PDF extraction failure
    mock_is_pdf.return_value = True
    mock_extract_pdf.return_value = None

    # LLM returns None (will fail on binary content anyway)
    mock_llm.return_value = None

    event = _make_event(
        ruling_text="%PDF-1.4 corrupt binary",
        content_format="pdf",
        outcome=None,
    )
    worker.process_event(event)

    # PDF extraction was attempted
    mock_extract_pdf.assert_called_once()

    # LLM is still called with the original (binary) text since extraction failed
    mock_llm.assert_called_once()

    # Worker should still commit — event processing doesn't fail
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.extract_text_from_pdf")
@patch("ingestion.worker.is_pdf_binary")
@patch("ingestion.worker.psycopg")
def test_process_event_non_pdf_skips_pdf_extraction(
    mock_psycopg: MagicMock,
    mock_is_pdf: MagicMock,
    mock_extract_pdf: MagicMock,
) -> None:
    """HTML content does not trigger PDF extraction preprocessing."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        ruling_text="<html><body>The motion is GRANTED.</body></html>",
        content_format="html",
    )
    worker.process_event(event)

    # PDF binary check should not be called for HTML content
    mock_is_pdf.assert_not_called()
    mock_extract_pdf.assert_not_called()

    mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Coverage gap tests — remaining uncovered lines in worker.py (#811)
# ---------------------------------------------------------------------------


def test_parse_date_date_object() -> None:
    """_parse_date with a date object returns it directly (line 783)."""
    d = date(2026, 3, 5)
    assert _parse_date(d) is d


def test_parse_date_invalid_string() -> None:
    """_parse_date with an invalid string returns None (lines 787-788)."""
    assert _parse_date("not-a-date") is None


@patch("ingestion.worker.psycopg")
def test_ensure_consumer_group_creates_group(mock_psycopg: MagicMock) -> None:
    """_ensure_consumer_group calls xgroup_create on first run (lines 661-670)."""
    worker, _ = _make_worker()

    worker._ensure_consumer_group()

    worker._redis.xgroup_create.assert_called_once_with(
        "document.captured", "ingestion-workers", id="0", mkstream=True
    )


@patch("ingestion.worker.psycopg")
def test_ensure_consumer_group_already_exists(mock_psycopg: MagicMock) -> None:
    """_ensure_consumer_group silently ignores 'group already exists' error."""
    worker, _ = _make_worker()
    worker._redis.xgroup_create.side_effect = Exception(
        "BUSYGROUP Consumer Group name already exists"
    )

    # Should not raise
    worker._ensure_consumer_group()


@patch("ingestion.worker.psycopg")
def test_process_batch_calls_xreadgroup(mock_psycopg: MagicMock) -> None:
    """_process_batch calls xreadgroup and processes returned messages (lines 673-685)."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()

    event_data = _make_event()
    msg_id = b"1234-0"
    worker._redis.xreadgroup.return_value = [
        (b"document.captured", [(msg_id, {b"data": json.dumps(event_data).encode()})])
    ]

    worker._process_batch(batch_size=10, block_ms=5000)

    worker._redis.xreadgroup.assert_called_once()
    worker.process_event.assert_called_once()
    worker._redis.xack.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_process_batch_no_messages(mock_psycopg: MagicMock) -> None:
    """_process_batch does nothing when no messages are returned."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()
    worker._redis.xreadgroup.return_value = None

    worker._process_batch(batch_size=10, block_ms=5000)

    worker.process_event.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_run_loop_continues_on_generic_exception(mock_psycopg: MagicMock) -> None:
    """The run loop logs and continues on generic exceptions (lines 317-318).

    After the generic exception, simulate KeyboardInterrupt to break the loop.
    """
    worker, _ = _make_worker()
    worker._ensure_consumer_group = MagicMock()
    worker.health_check = MagicMock()

    # First call raises generic exception, second call raises KeyboardInterrupt
    worker._process_batch = MagicMock(
        side_effect=[RuntimeError("unexpected"), KeyboardInterrupt],
    )

    # Should NOT raise — it catches the RuntimeError and continues,
    # then exits gracefully on KeyboardInterrupt
    worker.run()

    assert worker._process_batch.call_count == 2


@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_extraction_populates_case_type(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
) -> None:
    """When LLM extraction returns case_type, it populates the field (lines 440-441)."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # LLM returns case_type but nothing else new
    mock_llm.return_value = LLMExtractionResult(
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="23STCV12345",
                case_type="civil",
            )
        ],
    )

    event = _make_event(
        case_type=None,
        ruling_text="Some ruling text",
    )
    worker.process_event(event)

    mock_llm.assert_called_once()

    # Verify case_type was passed to upsert_case
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "civil" in all_sql


@patch("ingestion.worker.extract_hearing_date")
@patch("ingestion.worker.psycopg")
def test_process_event_regex_hearing_date_extraction(
    mock_psycopg: MagicMock,
    mock_extract_hd: MagicMock,
) -> None:
    """When hearing_date is missing and regex extracts it, tracks it (lines 470-471)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # Regex extraction returns a date
    mock_extract_hd.return_value = date(2026, 3, 10)

    event = _make_event(
        hearing_date=None,
        ruling_text="Hearing Date: March 10, 2026\nThe motion is GRANTED.",
    )
    worker.process_event(event)

    # Hearing date should have been extracted via regex
    mock_extract_hd.assert_called_once()

    # Ruling should have been inserted since hearing_date is now available
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1


@patch("ingestion.worker.extract_case_title")
@patch("ingestion.worker.psycopg")
def test_process_event_regex_case_title_extraction(
    mock_psycopg: MagicMock,
    mock_extract_title: MagicMock,
) -> None:
    """When case_title is missing and regex extracts it, extraction_methods tracks it (line 507)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # Regex returns a title
    mock_extract_title.return_value = "Smith v. Jones"

    # "Smith v. Jones" triggers extract_parties_from_caption which produces
    # party records that need additional fetchone calls for batch_upsert_parties
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # batch_upsert_parties: RETURNING ids for extracted parties
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]

    event = _make_event(
        case_title=None,
        ruling_text="Case: Smith v. Jones\nThe motion is GRANTED.",
    )
    worker.process_event(event)

    mock_extract_title.assert_called_once()
    mock_conn.commit.assert_called_once()
