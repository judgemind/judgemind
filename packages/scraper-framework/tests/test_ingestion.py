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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.execute.side_effect = psycopg.errors.UndefinedTable("relation 'courts' does not exist")

    with pytest.raises(InfrastructureError):
        worker.health_check()


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
    """Names with suffixes like 'III' or 'Jr.' preserve full length."""
    assert normalize_judge_name("Arthur Hester III") == "Arthur Hester Iii"
    # Note: .title() lowercases "III" to "Iii" — this is a known limitation
    # but the important thing is no truncation occurs.


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


@patch("ingestion.worker.psycopg")
def test_process_event_no_judge_name_leaves_judge_id_null(mock_psycopg: MagicMock) -> None:
    """Events without judge_name should not resolve a judge — judge_id stays NULL."""
    worker, os_mock = _make_worker()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # Caption fallback extracts parties from "Aasi v. American Honda":
        None,  # upsert_party for Aasi: no existing alias
        ("party-uuid-1",),  # upsert_party: INSERT INTO parties
        None,  # upsert_party for American Honda: no existing alias
        ("party-uuid-2",),  # upsert_party: INSERT INTO parties
    ]
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # upsert_party for first party: no existing alias, then INSERT
        None,
        ("party-uuid-1",),
        # upsert_party for second party: no existing alias, then INSERT
        None,
        ("party-uuid-2",),
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        parties=[
            {"name": "Sumayya Aasi", "role": "plaintiff"},
            {"name": "American Honda Motor Co.", "role": "defendant"},
        ]
    )
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO parties" in all_sql
    assert "INSERT INTO party_aliases" in all_sql
    assert "INSERT INTO case_parties" in all_sql


@patch("ingestion.worker.psycopg")
def test_process_event_without_parties_no_party_calls(mock_psycopg: MagicMock) -> None:
    """When event has no parties, no party DB calls are made."""
    worker, os_mock = _make_worker()

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
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

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        # upsert_party for plaintiff: no existing alias, then INSERT
        None,
        ("party-uuid-1",),
        # upsert_party for defendant: no existing alias, then INSERT
        None,
        ("party-uuid-2",),
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        case_title="Caldera v. Techno-Advanced, Inc.",
    )  # no parties key — fallback will extract from case_title
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO parties" in all_sql
    assert "INSERT INTO case_parties" in all_sql


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
