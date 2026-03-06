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
from ingestion.db import _derive_court_code, normalize_judge_name
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
        None,  # resolve_judge: no existing alias found
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges RETURNING id
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
        ("case-uuid-1",),   # upsert_case
        None,                # resolve_judge: no existing alias
        ("judge-uuid-1",),   # resolve_judge: INSERT INTO judges
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
        ("case-uuid-1",),   # upsert_case
        None,                # resolve_judge: no existing alias
        ("judge-uuid-1",),   # resolve_judge: INSERT INTO judges
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
        ("case-uuid-1",),   # upsert_case
        None,                # resolve_judge: no existing alias
        ("judge-uuid-1",),   # resolve_judge: INSERT INTO judges
    ]
    mock_cur.rowcount = 1

    # ruling_text says "GRANTED" but event says "denied"
    event = _make_event(
        ruling_text="The motion is GRANTED.",
        outcome="denied",
        motion_type="demurrer",
    )
    worker.process_event(event)

    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "denied" in sql_args
    assert "demurrer" in sql_args


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
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
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
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
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
        None,  # resolve_judge: no existing alias
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
    ]
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
    assert "INSERT INTO rulings" in all_sql
    assert "INSERT INTO case_judges" in all_sql
