"""Tests for the ingestion worker — Postgres and OpenSearch writes.

All external dependencies (Postgres, Redis, OpenSearch, S3) are mocked so
these tests run offline in CI without any infrastructure.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import psycopg
import psycopg.errors
import pytest

from ingestion.db import (
    _derive_court_code,
    insert_document,
    insert_document_and_ruling,
    insert_ruling,
    normalize_judge_name,
    normalize_party_name,
    upsert_case,
    upsert_case_party,
    upsert_party,
)
from ingestion.worker import (
    CONSUMER_NAME,
    DEFAULT_HEARTBEAT_INTERVAL,
    PENDING_RECLAIM_INTERVAL,
    PENDING_RECLAIM_MIN_IDLE_MS,
    STALE_CONSUMER_IDLE_MS,
    InfrastructureError,
    IngestionWorker,
    _parse_date,
    _parse_datetime,
    is_infrastructure_error,
    is_schema_constraint_error,
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
        "s3_key": "ca/los_angeles/superior_court/raw/abc123.html",
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_happy_path(mock_psycopg: MagicMock, mock_resolve_judge: MagicMock) -> None:
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

    # Verify judge was resolved and linked to case
    mock_resolve_judge.assert_called_once()
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO case_judges" in all_sql


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_indexes_new_fields_in_opensearch(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """motion_type, outcome, case_title, and summary are passed to OpenSearch."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # Use a case_title without "v." to avoid party extraction which needs more mock calls
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        outcome="granted",
        motion_type="demurrer",
        case_title="In re Marriage of Smith",
    )
    worker.process_event(event)

    os_mock.index.assert_called_once()
    indexed_doc = os_mock.index.call_args.kwargs["body"]
    assert indexed_doc["motion_type"] == "demurrer"
    assert indexed_doc["outcome"] == "granted"
    assert indexed_doc["case_title"] == "In re Marriage of Smith"
    # summary is a truncated version of the cleaned ruling text
    assert indexed_doc["summary"] is not None
    assert len(indexed_doc["summary"]) <= 500


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_passes_outcome_and_motion_type_from_event(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """When event carries outcome/motion_type, they are passed to insert_ruling."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_normalizes_title_case_outcome(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """Title-case scraper outcomes are normalized to lowercase before DB insert (#1878)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    # Pass title-case outcome as scrapers produce (e.g. Riverside, CC, Ventura)
    event = _make_event(outcome="Granted", motion_type="demurrer")
    worker.process_event(event)

    # Find the INSERT INTO rulings call
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]  # positional args tuple
    # outcome should be normalized to lowercase (not title-case "Granted")
    assert "granted" in sql_args
    assert "Granted" not in sql_args


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_event_fields_override_regex(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """Event-level outcome/motion_type take precedence over regex extraction."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_no_case_number_falls_back_to_unknown(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """Events without case_number AND no extractable case number in ruling_text
    use a synthetic UNKNOWN- case number."""
    worker, _ = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_extracts_case_number_from_ruling_text(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """When case_number is None but ruling_text contains a case number,
    the fallback extraction should capture it."""
    worker, _ = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_extracts_judge_name_from_ruling_text(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """When judge_name is None but ruling_text contains a judge name,
    the fallback extraction should capture it (#401)."""
    worker, _ = _make_worker()

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
        ruling_text=(
            "DEPARTMENT 56 JUDGE STEVEN A. ELLIS\nCase Number: 24NNCV02551\nThe motion is GRANTED."
        ),
    )
    worker.process_event(event)

    # resolve_judge should have been called — judge name extracted from text
    mock_resolve_judge.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_no_hearing_date_inserts_ruling_with_null_date(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """Events without hearing_date should insert both document and ruling (#2215).

    A missing hearing_date should never cause a ruling to be silently dropped.
    The ruling is inserted with NULL hearing_date instead.
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

    event = _make_event(hearing_date=None)
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # insert_ruling SHOULD be called even without hearing_date (#2215)
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1

    # The hearing_date parameter should be None
    ruling_args = ruling_calls[0][0][1]  # positional args tuple
    # hearing_date is the 5th positional arg in the INSERT INTO rulings call
    # (document_id, case_id, court_id, judge_id, hearing_date, ...)
    assert ruling_args[4] is None

    # case_judges should still be populated since judge was resolved
    case_judge_calls = [
        c for c in mock_cur.execute.call_args_list if "INSERT INTO case_judges" in str(c)
    ]
    assert len(case_judge_calls) == 1


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_process_event_enrichment_runs_without_hearing_date(
    mock_psycopg: MagicMock,
    mock_enrichment_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Enrichment should run when case_number is present, even if hearing_date is None (#2215).

    Prior to #2215, enrichment was skipped when hearing_date was None.  This
    caused rulings with valid case_numbers but no hearing_date to miss judge
    resolution and case matching.
    """
    worker, os_mock = _make_worker()

    # Set up the mock enrichment engine
    mock_engine = MagicMock()
    mock_enrichment_cls.return_value = mock_engine
    # Return a result with no corrections (exact match)
    from framework.enrichment import CaseMatch, EnrichmentResult, JudgeResolution

    mock_engine.enrich.return_value = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-1",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id="judge-uuid-1",
            canonical_name="Smith, John A.",
            match_type="alias",
            confidence=1.0,
        ),
    )

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case_returning_title
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(hearing_date=None)
    worker.process_event(event)

    # Enrichment should have been called despite hearing_date=None
    mock_engine.enrich.assert_called_once()
    enrich_kwargs = mock_engine.enrich.call_args.kwargs
    assert enrich_kwargs["case_number"] == "23STCV12345"
    assert enrich_kwargs["hearing_date"] is None


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_duplicate_skips_opensearch(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """If document_id already in Postgres, OpenSearch indexing is skipped."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (False,),  # insert_document: RETURNING is_new = False (existing doc, upsert updated)
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
# Schema constraint error classification tests
# ---------------------------------------------------------------------------


def test_is_schema_constraint_error_string_data_right_truncation() -> None:
    """StringDataRightTruncation (value too long) is a schema constraint error."""
    exc = psycopg.errors.StringDataRightTruncation("value too long for type character(2)")
    assert is_schema_constraint_error(exc) is True


def test_is_schema_constraint_error_numeric_value_out_of_range() -> None:
    """NumericValueOutOfRange is a schema constraint error."""
    exc = psycopg.errors.NumericValueOutOfRange("numeric value out of range")
    assert is_schema_constraint_error(exc) is True


def test_is_schema_constraint_error_check_violation() -> None:
    """CheckViolation is a schema constraint error."""
    exc = psycopg.errors.CheckViolation("check constraint violated")
    assert is_schema_constraint_error(exc) is True


def test_is_schema_constraint_error_not_null_violation() -> None:
    """NotNullViolation is a schema constraint error."""
    exc = psycopg.errors.NotNullViolation("null value in column")
    assert is_schema_constraint_error(exc) is True


def test_is_schema_constraint_error_not_infra() -> None:
    """Schema constraint errors should NOT be classified as infrastructure errors."""
    exc = psycopg.errors.StringDataRightTruncation("value too long for type character(2)")
    assert is_infrastructure_error(exc) is False


def test_is_schema_constraint_error_not_value_error() -> None:
    """ValueError is not a schema constraint error."""
    exc = ValueError("bad data")
    assert is_schema_constraint_error(exc) is False


def test_is_schema_constraint_error_not_operational_error() -> None:
    """OperationalError (infra) is not a schema constraint error."""
    exc = psycopg.OperationalError("connection refused")
    assert is_schema_constraint_error(exc) is False


def test_is_schema_constraint_error_not_unique_violation() -> None:
    """UniqueViolation is not a schema constraint error (it's handled separately)."""
    exc = psycopg.errors.UniqueViolation("duplicate key")
    assert is_schema_constraint_error(exc) is False


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
# Schema constraint error dead-letter (no retry)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_process_message_schema_error_dead_letters_immediately(
    mock_psycopg: MagicMock,
) -> None:
    """Schema constraint errors should dead-letter immediately without retry."""
    worker, _ = _make_worker()
    worker._max_retries = 5
    worker.process_event = MagicMock(
        side_effect=psycopg.errors.StringDataRightTruncation(
            "value too long for type character(2)"
        ),
    )

    msg_id = b"schema-0"
    data = {b"data": json.dumps(_make_event()).encode()}
    worker._process_message(msg_id, data)

    # Should only attempt once — no retry for deterministic schema errors
    assert worker.process_event.call_count == 1
    # Message SHOULD be acknowledged (dead-lettered)
    worker._redis.xack.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_process_message_schema_error_does_not_raise(
    mock_psycopg: MagicMock,
) -> None:
    """Schema constraint errors should NOT raise InfrastructureError (worker stays up)."""
    worker, _ = _make_worker()
    worker._max_retries = 3
    worker.process_event = MagicMock(
        side_effect=psycopg.errors.NumericValueOutOfRange("numeric value out of range"),
    )

    msg_id = b"schema-1"
    data = {b"data": json.dumps(_make_event()).encode()}

    # Should NOT raise — the worker continues processing other messages
    worker._process_message(msg_id, data)

    assert worker.process_event.call_count == 1
    worker._redis.xack.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_process_message_schema_error_not_null_violation(
    mock_psycopg: MagicMock,
) -> None:
    """NotNullViolation dead-letters immediately without retry."""
    worker, _ = _make_worker()
    worker._max_retries = 3
    worker.process_event = MagicMock(
        side_effect=psycopg.errors.NotNullViolation("null value in column 'state_code'"),
    )

    msg_id = b"schema-2"
    data = {b"data": json.dumps(_make_event()).encode()}
    worker._process_message(msg_id, data)

    assert worker.process_event.call_count == 1
    worker._redis.xack.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_process_message_schema_error_check_violation(
    mock_psycopg: MagicMock,
) -> None:
    """CheckViolation dead-letters immediately without retry."""
    worker, _ = _make_worker()
    worker._max_retries = 3
    worker.process_event = MagicMock(
        side_effect=psycopg.errors.CheckViolation("check constraint failed"),
    )

    msg_id = b"schema-3"
    data = {b"data": json.dumps(_make_event()).encode()}
    worker._process_message(msg_id, data)

    assert worker.process_event.call_count == 1
    worker._redis.xack.assert_called_once()


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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_reuses_connection(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """Multiple process_event calls reuse the same DB connection."""
    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        # First event
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
        # Second event
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
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

    # No existing alias, no canonical match, no roster match,
    # then INSERT returns new judge id.
    # Calls: (1) alias lookup, (2) canonical match, (3) roster court_code
    # (returns None — no court found, skips roster), (4) INSERT judge.
    mock_cur.fetchone.side_effect = [None, None, None, ("new-judge-uuid",)]
    mock_cur.fetchall.return_value = []  # no near-duplicates

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


@patch("ingestion.worker.resolve_judge_from_department", return_value=None)
@patch("ingestion.worker.fetch_department_judge_mapping", return_value={})
@patch("ingestion.worker.psycopg")
def test_process_event_no_judge_name_leaves_judge_id_null(
    mock_psycopg: MagicMock,
    _mock_fetch_dept: MagicMock,
    _mock_roster_dept: MagicMock,
) -> None:
    """Events without judge_name should not resolve a judge — judge_id stays NULL.

    The LA dept-to-judge mapping returns empty, so no dept lookup resolves either.
    """
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (roster dept lookup)
        ("court-uuid-1",),  # upsert_court (DB write section)
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


@patch("ingestion.worker.resolve_judge", return_value="existing-judge-uuid")
@patch("ingestion.worker.psycopg")
def test_process_event_with_existing_judge_alias(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """When judge alias already exists, reuse the existing judge_id."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # resolve_judge was called and returned existing UUID
    mock_resolve_judge.assert_called_once()
    # Should still insert ruling and case_judges
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_passes_case_title_to_upsert_case(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """When event carries case_title, it is passed to upsert_case."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_without_case_title_passes_none(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """When event has no case_title, None is passed to upsert_case."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql


# ---------------------------------------------------------------------------
# process_event — party processing
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_with_parties(mock_psycopg: MagicMock, mock_resolve_judge: MagicMock) -> None:
    """When event carries parties, party records and case_party links are created."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_without_parties_no_party_calls(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """When event has no parties, no party DB calls are made."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event()  # no parties key
    worker.process_event(event)

    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO case_parties" not in all_sql


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

    # Find the INSERT INTO rulings call (not SAVEPOINT/RELEASE)
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(insert_calls) == 1
    sql = str(insert_calls[0])
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

    # Find the INSERT INTO rulings call and check its args
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(insert_calls) == 1
    sql_args = insert_calls[0][0][1]
    doc_id_count = sum(1 for a in sql_args if a == "doc-uuid-1")
    assert doc_id_count == 1


# ---------------------------------------------------------------------------
# LLM extraction integration — mock LLM response, verify fields populated
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_extraction_populates_missing_fields(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
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
    mock_resolve_judge.assert_called_once()

    # Parties from LLM were written via batch_upsert_parties
    all_executemany_sql = " ".join(str(c) for c in mock_cur.executemany.call_args_list)
    assert "INSERT INTO parties" in all_executemany_sql
    assert "case_parties" in all_executemany_sql


@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_passes_county_max_output_tokens_to_llm(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
) -> None:
    """Worker passes county-configured max_output_tokens to extract_fields_llm (#2355).

    Santa Clara is configured with max_output_tokens=32768.  Verify that the
    worker looks up the county config and passes max_tokens to the LLM call.
    """
    worker, _os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # Return enough values for the full processing pipeline
    mock_cur.fetchone.return_value = ("uuid-1",)
    mock_cur.fetchall.return_value = []
    mock_cur.rowcount = 1

    # Mock _llm_split_document to return False so we reach extract_fields_llm
    with patch.object(worker, "_llm_split_document", return_value=False):
        mock_llm.return_value = None

        # Event for Santa Clara — should get max_tokens=32768
        event = _make_event(
            scraper_id="ca-sc-tentatives-civil",
            state="CA",
            county="Santa Clara",
            ruling_text="Some ruling text",
            case_number="24CV443183",
        )
        worker.process_event(event)

        # Verify extract_fields_llm was called with max_tokens=32768
        mock_llm.assert_called_once()
        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["max_tokens"] == 32768


@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_default_max_tokens_for_unconfigured_county(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
) -> None:
    """Worker uses default max_tokens=4096 for counties without config (#2355)."""
    worker, _os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # Return enough values for the full processing pipeline
    mock_cur.fetchone.return_value = ("uuid-1",)
    mock_cur.fetchall.return_value = []
    mock_cur.rowcount = 1

    # Mock _llm_split_document to return False so we reach extract_fields_llm
    with patch.object(worker, "_llm_split_document", return_value=False):
        mock_llm.return_value = None

        # Event for an unconfigured county — should get default max_tokens=4096
        event = _make_event(
            state="CA",
            county="Unknown County",
            ruling_text="Some ruling text",
        )
        worker.process_event(event)

        # Verify extract_fields_llm was called with max_tokens=4096
        mock_llm.assert_called_once()
        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["max_tokens"] == 4096


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_matches_ruling_by_case_number(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
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


@patch("ingestion.worker.resolve_judge", return_value="existing-judge-uuid")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_scraper_fields_take_precedence_over_llm(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
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
# LLM invalid case_number redirect (#1524)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_invalid_case_number_redirected_to_title(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When LLM returns a case title as case_number, it should be redirected
    to case_title and not used as case_number (#1524)."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        # batch_upsert_parties: executemany RETURNING ids for caption parties
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    # batch_upsert_parties SELECT returns no existing aliases
    mock_cur.fetchall.return_value = []
    # nextset for executemany returning
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    # LLM returns a case title ("Smith v. Kia") as case_number
    mock_llm.return_value = LLMExtractionResult(
        judge_name="Ellis",
        hearing_date=date(2026, 3, 10),
        department="56",
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="Smith v. Kia",  # Invalid — this is a title
                case_title=None,
                outcome="granted",
                motion_type="msj",
                parties=[],
            )
        ],
    )

    event = _make_event(
        case_number=None,
        case_title=None,
        outcome=None,
        motion_type=None,
        ruling_text="The motion is granted.",
    )
    worker.process_event(event)

    # The invalid case_number should NOT appear as case_number in the SQL
    case_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO cases" in str(c)]
    assert len(case_calls) == 1
    case_args = case_calls[0][0][1]
    # "Smith v. Kia" should be used as case_title, not as case_number
    # The case_number should be UNKNOWN-<doc_id> (from the fallback)
    assert "Smith v. Kia" not in str(case_args[0])  # case_number is first arg
    # The case_title should contain the redirected value
    assert "Smith v. Kia" in str(case_args)


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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-dept")
@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe", "52": "John Smith"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_resolves_judge_when_name_missing(
    mock_psycopg: MagicMock, _mock_fetch_dept: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """LA event with department but no judge_name resolves judge via dept map."""
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

    mock_conn.commit.assert_called_once()

    # Judge resolution should have happened via dept lookup
    mock_resolve_judge.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_skipped_when_judge_name_present(
    mock_psycopg: MagicMock, mock_fetch_dept: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """LA event with judge_name already present skips dept lookup entirely."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(judge_name="Already Known", department="1")
    worker.process_event(event)

    # fetch_department_judge_mapping should NOT have been called
    mock_fetch_dept.assert_not_called()


@patch("ingestion.worker.resolve_judge_from_department", return_value=None)
@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_skipped_for_non_la_county(
    mock_psycopg: MagicMock,
    _mock_roster_dept: MagicMock,
) -> None:
    """Non-LA county events should not trigger the LA-specific dept lookup."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (roster dept lookup)
        ("court-uuid-1",),  # upsert_court (DB write section)
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

    # _la_dept_map should remain None (LA-specific lookup never triggered)
    assert worker._la_dept_map is None


@patch("ingestion.worker.resolve_judge_from_department", return_value=None)
@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_lookup_unmapped_dept_leaves_judge_null(
    mock_psycopg: MagicMock,
    _mock_fetch_dept: MagicMock,
    _mock_roster_dept: MagicMock,
) -> None:
    """LA event with department not in mapping — judge stays NULL."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (roster dept lookup)
        ("court-uuid-1",),  # upsert_court (DB write section)
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={"1": "Jane Doe"},
)
@patch("ingestion.worker.psycopg")
def test_la_dept_map_cached_across_events(
    mock_psycopg: MagicMock, mock_fetch_dept: MagicMock, mock_resolve_judge: MagicMock
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
        ]
        mock_cur.rowcount = 1
        event = _make_event(document_id=doc_id, judge_name=None, department="1")
        worker.process_event(event)
        mock_conn.reset_mock()
        mock_cur.reset_mock()

    # fetch_department_judge_mapping should have been called exactly once
    mock_fetch_dept.assert_called_once()


@patch("ingestion.worker.resolve_judge_from_department", return_value=None)
@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    side_effect=Exception("Network error"),
)
@patch("ingestion.worker.psycopg")
def test_la_dept_map_fetch_failure_degrades_gracefully(
    mock_psycopg: MagicMock,
    _mock_fetch_dept: MagicMock,
    _mock_roster_dept: MagicMock,
) -> None:
    """If dept map fetch fails, worker continues without dept lookup."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (roster dept lookup)
        ("court-uuid-1",),  # upsert_court (DB write section)
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
# Universal dept-to-judge fallback via court directory snapshots (#2269)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-dept")
@patch("ingestion.worker.resolve_judge_from_department", return_value="Jane Doe")
@patch(
    "ingestion.worker.fetch_department_judge_mapping",
    return_value={},
)
@patch("ingestion.worker.psycopg")
def test_roster_dept_lookup_fires_for_llm_extracted_events(
    mock_psycopg: MagicMock,
    _mock_fetch_dept: MagicMock,
    mock_roster_dept: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """LLM-extracted event with no judge_name should resolve via roster dept lookup (#2269)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (from roster dept lookup)
        ("court-uuid-1",),  # upsert_court (from DB write section)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        judge_name=None,
        department="3",
        _llm_extracted=True,
        _split_processed=True,
    )
    worker.process_event(event)

    # resolve_judge_from_department should have been called
    mock_roster_dept.assert_called_once()

    # resolve_judge should have been called with the roster-resolved name
    mock_resolve_judge.assert_called_once()
    assert mock_resolve_judge.call_args[0][1] == "Jane Doe"


@patch("ingestion.worker.resolve_judge_from_department", return_value="John Smith")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_roster_dept_lookup_fires_for_ventura(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_roster_dept: MagicMock,
) -> None:
    """Ventura event with no judge_name should resolve via roster dept lookup (#2269)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (from roster dept lookup)
        ("court-uuid-1",),  # upsert_court (from DB write section)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        judge_name=None,
        department="J6",
        state="CA",
        county="Ventura",
        ruling_text="Motion to compel is GRANTED.",
        _llm_extracted=True,
        _split_processed=True,
    )
    worker.process_event(event)

    # resolve_judge_from_department should have been called
    mock_roster_dept.assert_called_once()

    # resolve_judge should have been called with the roster name
    mock_resolve_judge.assert_called_once()
    assert mock_resolve_judge.call_args[0][1] == "John Smith"


@patch("ingestion.worker.resolve_judge_from_department", return_value=None)
@patch("ingestion.worker.psycopg")
def test_roster_dept_lookup_no_match_leaves_judge_null(
    mock_psycopg: MagicMock,
    mock_roster_dept: MagicMock,
) -> None:
    """When roster dept lookup returns None, judge stays NULL."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (from roster dept lookup)
        ("court-uuid-1",),  # upsert_court (from DB write section)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        judge_name=None,
        department="999",
        _llm_extracted=True,
        _split_processed=True,
    )
    worker.process_event(event)

    mock_conn.commit.assert_called_once()

    # No judge resolution should happen (no INSERT INTO judges)
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO judges" not in all_sql


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.resolve_judge_from_department", return_value="Jane Doe")
@patch("ingestion.worker.psycopg")
def test_roster_dept_lookup_skipped_when_judge_already_set(
    mock_psycopg: MagicMock,
    mock_roster_dept: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When judge_name is already set, roster dept lookup should not fire."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        judge_name="Already Known",
        department="3",
        _llm_extracted=True,
        _split_processed=True,
    )
    worker.process_event(event)

    # resolve_judge_from_department should NOT have been called
    mock_roster_dept.assert_not_called()


# ---------------------------------------------------------------------------
# PDF binary preprocessing in process_event
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_text_from_pdf")
@patch("ingestion.worker.is_pdf_binary")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_extracts_text_from_pdf_binary(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_is_pdf: MagicMock,
    mock_extract_pdf: MagicMock,
    mock_resolve_judge: MagicMock,
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_text_from_pdf")
@patch("ingestion.worker.is_pdf_binary")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_pdf_extraction_failure_continues(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_is_pdf: MagicMock,
    mock_extract_pdf: MagicMock,
    mock_resolve_judge: MagicMock,
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_text_from_pdf")
@patch("ingestion.worker.is_pdf_binary")
@patch("ingestion.worker.psycopg")
def test_process_event_non_pdf_skips_pdf_extraction(
    mock_psycopg: MagicMock,
    mock_is_pdf: MagicMock,
    mock_extract_pdf: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """HTML content does not trigger PDF extraction preprocessing."""
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


# ---------------------------------------------------------------------------
# Stale consumer cleanup tests
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_cleanup_stale_consumers_deletes_idle_consumers(mock_psycopg: MagicMock) -> None:
    """_cleanup_stale_consumers removes consumers idle > threshold with 0 pending."""
    worker, _ = _make_worker()

    worker._redis.xinfo_consumers.return_value = [
        {"name": CONSUMER_NAME.encode(), "idle": 0, "pending": 0},
        {"name": b"stale-worker-1", "idle": STALE_CONSUMER_IDLE_MS + 1, "pending": 0},
        {"name": b"stale-worker-2", "idle": STALE_CONSUMER_IDLE_MS * 2, "pending": 0},
    ]

    worker._cleanup_stale_consumers()

    assert worker._redis.xgroup_delconsumer.call_count == 2
    worker._redis.xgroup_delconsumer.assert_any_call(
        "document.captured", "ingestion-workers", "stale-worker-1"
    )
    worker._redis.xgroup_delconsumer.assert_any_call(
        "document.captured", "ingestion-workers", "stale-worker-2"
    )


@patch("ingestion.worker.psycopg")
def test_cleanup_stale_consumers_preserves_current_consumer(mock_psycopg: MagicMock) -> None:
    """_cleanup_stale_consumers never deletes the current process's consumer."""
    worker, _ = _make_worker()

    # Current consumer appears idle and with 0 pending — should still be kept.
    worker._redis.xinfo_consumers.return_value = [
        {"name": CONSUMER_NAME.encode(), "idle": STALE_CONSUMER_IDLE_MS + 1, "pending": 0},
    ]

    worker._cleanup_stale_consumers()

    worker._redis.xgroup_delconsumer.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_cleanup_stale_consumers_keeps_consumers_with_pending(mock_psycopg: MagicMock) -> None:
    """_cleanup_stale_consumers keeps consumers that have pending messages."""
    worker, _ = _make_worker()

    worker._redis.xinfo_consumers.return_value = [
        {"name": b"busy-worker", "idle": STALE_CONSUMER_IDLE_MS + 1, "pending": 3},
    ]

    worker._cleanup_stale_consumers()

    worker._redis.xgroup_delconsumer.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_cleanup_stale_consumers_keeps_recently_active(mock_psycopg: MagicMock) -> None:
    """_cleanup_stale_consumers keeps consumers that are not idle long enough."""
    worker, _ = _make_worker()

    worker._redis.xinfo_consumers.return_value = [
        {"name": b"active-worker", "idle": STALE_CONSUMER_IDLE_MS - 1, "pending": 0},
    ]

    worker._cleanup_stale_consumers()

    worker._redis.xgroup_delconsumer.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_cleanup_stale_consumers_handles_xinfo_failure(mock_psycopg: MagicMock) -> None:
    """_cleanup_stale_consumers logs a warning and continues if xinfo_consumers fails."""
    worker, _ = _make_worker()
    worker._redis.xinfo_consumers.side_effect = Exception("stream not found")

    # Should not raise — cleanup is best-effort
    worker._cleanup_stale_consumers()

    worker._redis.xgroup_delconsumer.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_cleanup_stale_consumers_handles_delconsumer_failure(mock_psycopg: MagicMock) -> None:
    """_cleanup_stale_consumers continues if deleting one consumer fails."""
    worker, _ = _make_worker()

    worker._redis.xinfo_consumers.return_value = [
        {"name": b"fail-worker", "idle": STALE_CONSUMER_IDLE_MS + 1, "pending": 0},
        {"name": b"ok-worker", "idle": STALE_CONSUMER_IDLE_MS + 1, "pending": 0},
    ]
    worker._redis.xgroup_delconsumer.side_effect = [
        Exception("permission denied"),
        None,
    ]

    # Should not raise
    worker._cleanup_stale_consumers()

    assert worker._redis.xgroup_delconsumer.call_count == 2


@patch("ingestion.worker.psycopg")
def test_cleanup_stale_consumers_called_during_run(mock_psycopg: MagicMock) -> None:
    """run() calls _cleanup_stale_consumers after _ensure_consumer_group."""
    worker, _ = _make_worker()
    worker._ensure_consumer_group = MagicMock()
    worker._cleanup_stale_consumers = MagicMock()
    worker.health_check = MagicMock()
    worker._process_batch = MagicMock(side_effect=KeyboardInterrupt)

    worker.run()

    worker._cleanup_stale_consumers.assert_called_once()


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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_llm_extraction_populates_case_type(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
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


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_hearing_date")
@patch("ingestion.worker.psycopg")
def test_process_event_regex_hearing_date_extraction(
    mock_psycopg: MagicMock,
    mock_extract_hd: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When hearing_date is missing and regex extracts it, tracks it (lines 470-471)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
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


@patch("ingestion.worker.psycopg")
def test_heartbeat_resets_after_message_received(mock_psycopg: MagicMock) -> None:
    """Empty poll counter resets when a message is processed."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()

    # Accumulate some empty polls
    worker._redis.xreadgroup.return_value = None
    for _ in range(DEFAULT_HEARTBEAT_INTERVAL - 1):
        worker._process_batch(batch_size=10, block_ms=5000)

    assert worker._empty_polls == DEFAULT_HEARTBEAT_INTERVAL - 1

    # Now process a message — counter should reset
    event_data = _make_event()
    worker._redis.xreadgroup.return_value = [
        (b"document.captured", [(b"1234-0", {b"data": json.dumps(event_data).encode()})])
    ]
    worker._process_batch(batch_size=10, block_ms=5000)

    assert worker._empty_polls == 0


@patch("ingestion.worker.psycopg")
def test_heartbeat_repeats_periodically(mock_psycopg: MagicMock) -> None:
    """Heartbeat fires every HEARTBEAT_INTERVAL empty polls, not just once."""
    worker, _ = _make_worker()
    worker._redis.xreadgroup.return_value = None

    with patch("ingestion.worker.logger") as mock_logger:
        # Run for 2x the interval
        for _ in range(DEFAULT_HEARTBEAT_INTERVAL * 2):
            worker._process_batch(batch_size=10, block_ms=5000)
        assert mock_logger.log.call_count == 2


@patch("ingestion.worker.psycopg")
def test_heartbeat_log_level_default_is_info(mock_psycopg: MagicMock) -> None:
    """By default heartbeat logs at INFO level."""
    import logging

    worker, _ = _make_worker()
    assert worker._heartbeat_log_level == logging.INFO


@patch.dict("os.environ", {"HEARTBEAT_LOG_LEVEL": "DEBUG"})
@patch("ingestion.worker.psycopg")
def test_heartbeat_log_level_configurable(mock_psycopg: MagicMock) -> None:
    """HEARTBEAT_LOG_LEVEL env var changes the heartbeat log level."""
    import logging

    worker, _ = _make_worker()
    assert worker._heartbeat_log_level == logging.DEBUG


@patch.dict("os.environ", {"HEARTBEAT_INTERVAL": "10"})
@patch("ingestion.worker.psycopg")
def test_heartbeat_interval_configurable(mock_psycopg: MagicMock) -> None:
    """HEARTBEAT_INTERVAL env var changes the heartbeat frequency."""
    worker, _ = _make_worker()
    worker._redis.xreadgroup.return_value = None

    with patch("ingestion.worker.logger") as mock_logger:
        for _ in range(10):
            worker._process_batch(batch_size=10, block_ms=5000)
        assert mock_logger.log.call_count == 1


@patch("ingestion.worker.psycopg")
def test_heartbeat_includes_idle_duration(mock_psycopg: MagicMock) -> None:
    """Heartbeat reports the time since the last event was processed."""
    worker, _ = _make_worker()
    worker._redis.xreadgroup.return_value = None

    # Set last_event_time to a known past value
    worker._last_event_time = time.monotonic() - 300  # 5 min ago

    with patch("ingestion.worker.logger") as mock_logger:
        for _ in range(DEFAULT_HEARTBEAT_INTERVAL):
            worker._process_batch(batch_size=10, block_ms=5000)

        call_args = mock_logger.log.call_args
        idle_seconds = call_args[1]["extra"]["idle_seconds"]
        # Should be approximately 300 seconds (5 minutes), allow some tolerance
        assert idle_seconds >= 299


# ---------------------------------------------------------------------------
# insert_ruling with ruling_text_html
# ---------------------------------------------------------------------------


def test_insert_ruling_with_ruling_text_html() -> None:
    """insert_ruling stores ruling_text_html and includes it in ON CONFLICT."""
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
        ruling_text_html='<div class="ruling"><p>Motion <strong>GRANTED</strong>.</p></div>',
    )

    # Find the INSERT INTO rulings call (not SAVEPOINT/RELEASE)
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(insert_calls) == 1
    sql = str(insert_calls[0])
    assert "ruling_text_html" in sql
    assert "EXCLUDED.ruling_text_html" in sql
    assert "rulings.ruling_text_html" in sql


def test_insert_ruling_with_ruling_text_html_none() -> None:
    """insert_ruling works when ruling_text_html is None (default)."""
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

    # Should have: SAVEPOINT, INSERT, RELEASE SAVEPOINT = 3 execute calls
    assert mock_cur.execute.call_count == 3


def test_insert_document_and_ruling_always_inserts_ruling() -> None:
    """insert_document_and_ruling inserts ruling even when hearing_date is None (#2215).

    Before #2215, the function silently skipped the ruling when hearing_date
    was None.  Now it always inserts the ruling, passing hearing_date through
    (which may be None).
    """
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.return_value = (True,)  # insert_document: is_new = True
    mock_cur.rowcount = 1

    insert_document_and_ruling(
        mock_conn,
        document_id="doc-uuid-1",
        case_id="case-uuid-1",
        court_id="court-uuid-1",
        content_format="html",
        content_hash="abc123",
        s3_key="s3/key",
        s3_bucket="bucket",
        source_url="https://example.com",
        scraper_id="test-scraper",
        captured_at=datetime(2026, 3, 5),
        hearing_date=None,
        ruling_text="Motion GRANTED.",
    )

    # Both INSERT INTO documents AND INSERT INTO rulings should be present
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "INSERT INTO documents" in all_sql
    assert "INSERT INTO rulings" in all_sql


def test_insert_ruling_with_null_hearing_date() -> None:
    """insert_ruling accepts None hearing_date after #2215 schema change."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    insert_ruling(
        mock_conn,
        document_id="doc-uuid-1",
        case_id="case-uuid-1",
        court_id="court-uuid-1",
        hearing_date=None,
        ruling_text="Motion GRANTED.",
        department="Dept. 1",
    )

    # The INSERT INTO rulings call should contain None for hearing_date
    insert_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(insert_calls) == 1
    sql_args = insert_calls[0][0][1]
    # hearing_date is the 5th positional arg
    # (document_id, case_id, court_id, judge_id, hearing_date)
    assert sql_args[4] is None


# ---------------------------------------------------------------------------
# Ruling formatting integration tests
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.format_ruling_text")
@patch("ingestion.worker.psycopg")
def test_process_event_formats_ruling_when_enabled(
    mock_psycopg: MagicMock,
    mock_format: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When ENABLE_RULING_FORMATTING is set, format_ruling_text is called."""
    worker, os_mock = _make_worker()
    worker._formatting_enabled = True
    worker._formatting_client = MagicMock()
    formatted_html = '<div class="ruling"><p>Motion GRANTED.</p></div>'
    mock_format.return_value = formatted_html

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event()
    worker.process_event(event)

    # Verify format_ruling_text was called
    mock_format.assert_called_once()

    # Verify insert_ruling received the formatted HTML
    sql = str(mock_cur.execute.call_args_list)
    assert "ruling_text_html" in sql


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.format_ruling_text")
@patch("ingestion.worker.psycopg")
def test_process_event_formatting_disabled_by_default(
    mock_psycopg: MagicMock,
    mock_format: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When ENABLE_RULING_FORMATTING is not set, format_ruling_text is not called."""
    worker, os_mock = _make_worker()
    # _formatting_enabled defaults to False
    assert not worker._formatting_enabled

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event()
    worker.process_event(event)

    mock_format.assert_not_called()


# ---------------------------------------------------------------------------
# PEL reclamation tests (#1044)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.psycopg")
def test_reclaim_pending_processes_claimed_messages(mock_psycopg: MagicMock) -> None:
    """_reclaim_pending_messages processes messages returned by XAUTOCLAIM."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()

    event_data = _make_event()
    msg_id = b"1234-0"

    # XAUTOCLAIM returns (next_cursor, [(msg_id, data)], [deleted_ids])
    worker._redis.xautoclaim.return_value = (
        b"0-0",
        [(msg_id, {b"data": json.dumps(event_data).encode()})],
        [],
    )

    result = worker._reclaim_pending_messages()

    assert result == 1
    worker._redis.xautoclaim.assert_called_once_with(
        "document.captured",
        "ingestion-workers",
        CONSUMER_NAME,
        min_idle_time=PENDING_RECLAIM_MIN_IDLE_MS,
        start_id=b"0-0",
        count=100,
    )
    worker.process_event.assert_called_once()
    worker._redis.xack.assert_called_once()


@patch("ingestion.worker.psycopg")
def test_reclaim_pending_no_messages(mock_psycopg: MagicMock) -> None:
    """_reclaim_pending_messages returns 0 when no messages are pending."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()

    worker._redis.xautoclaim.return_value = (b"0-0", [], [])

    result = worker._reclaim_pending_messages()

    assert result == 0
    worker.process_event.assert_not_called()


@patch("ingestion.worker.psycopg")
def test_reclaim_pending_handles_xautoclaim_failure(mock_psycopg: MagicMock) -> None:
    """_reclaim_pending_messages returns 0 when XAUTOCLAIM raises."""
    worker, _ = _make_worker()
    worker._redis.xautoclaim.side_effect = Exception("Redis error")

    result = worker._reclaim_pending_messages()

    assert result == 0


@patch("ingestion.worker.psycopg")
def test_reclaim_pending_skips_deleted_entries(mock_psycopg: MagicMock) -> None:
    """_reclaim_pending_messages acknowledges entries with None data (deleted from stream)."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()

    # Simulate a deleted message: msg_id present but data is None
    worker._redis.xautoclaim.return_value = (
        b"0-0",
        [(b"9999-0", None)],
        [],
    )

    result = worker._reclaim_pending_messages()

    assert result == 0
    worker.process_event.assert_not_called()
    # The deleted entry should still be acknowledged to clear the PEL
    worker._redis.xack.assert_called_once_with("document.captured", "ingestion-workers", b"9999-0")


@patch("ingestion.worker.psycopg")
def test_reclaim_pending_called_on_startup(mock_psycopg: MagicMock) -> None:
    """run() calls _reclaim_pending_messages after _cleanup_stale_consumers."""
    worker, _ = _make_worker()
    worker._ensure_consumer_group = MagicMock()
    worker._cleanup_stale_consumers = MagicMock()
    worker._reclaim_pending_messages = MagicMock(return_value=0)
    worker.health_check = MagicMock()
    worker._process_batch = MagicMock(side_effect=KeyboardInterrupt)

    worker.run()

    worker._reclaim_pending_messages.assert_called()


@patch("ingestion.worker.psycopg")
def test_reclaim_pending_called_periodically(mock_psycopg: MagicMock) -> None:
    """_reclaim_pending_messages is called every PENDING_RECLAIM_INTERVAL empty polls."""
    worker, _ = _make_worker()
    worker._ensure_consumer_group = MagicMock()
    worker._cleanup_stale_consumers = MagicMock()
    worker._reclaim_pending_messages = MagicMock(return_value=0)
    worker.health_check = MagicMock()

    # Simulate empty polls: xreadgroup returns None, then after enough
    # cycles (PENDING_RECLAIM_INTERVAL), reclaim should be called.
    call_count = 0

    def fake_process_batch(batch_size: int, block_ms: int) -> None:
        nonlocal call_count
        call_count += 1
        # Simulate empty poll by incrementing the counter
        worker._empty_polls = call_count
        if call_count > PENDING_RECLAIM_INTERVAL:
            raise KeyboardInterrupt

    worker._process_batch = MagicMock(side_effect=fake_process_batch)

    worker.run()

    # Should have been called on startup + at least once during the loop
    assert worker._reclaim_pending_messages.call_count >= 2


@patch("ingestion.worker.psycopg")
def test_reclaim_pending_processes_multiple_messages(mock_psycopg: MagicMock) -> None:
    """_reclaim_pending_messages handles multiple messages in one batch."""
    worker, _ = _make_worker()
    worker.process_event = MagicMock()

    event1 = _make_event(document_id="doc-1")
    event2 = _make_event(document_id="doc-2")

    worker._redis.xautoclaim.return_value = (
        b"0-0",
        [
            (b"1000-0", {b"data": json.dumps(event1).encode()}),
            (b"1001-0", {b"data": json.dumps(event2).encode()}),
        ],
        [],
    )

    result = worker._reclaim_pending_messages()

    assert result == 2
    assert worker.process_event.call_count == 2


# ---------------------------------------------------------------------------
# Document splitting integration tests
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_already_split_no_re_split(
    mock_psycopg: MagicMock, mock_resolve_judge: MagicMock
) -> None:
    """Events with _split_processed=True should NOT be split again."""
    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]

    event = _make_event(
        _split_processed=True,
        _original_document_id="aaaaaaaa-0000-0000-0000-000000000001",
    )
    worker.process_event(event)

    # Should process normally without trying to split again
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.batch_upsert_parties")
@patch("ingestion.worker.insert_document_and_ruling", return_value=True)
@patch("ingestion.worker.upsert_case_returning_title", return_value=("case-uuid-1", None))
@patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_split_event_insert_document_and_ruling_uses_split_id(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_upsert_court: MagicMock,
    mock_upsert_case: MagicMock,
    mock_insert_doc_and_ruling: MagicMock,
    mock_batch_upsert: MagicMock,
) -> None:
    """Split events must pass the split document_id to insert_document_and_ruling.

    Regression test for #1775/#1790: the shared helper guarantees the same
    document_id is used for both insert_document and insert_ruling internally.
    """
    import hashlib

    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    original_doc_id = "aaaaaaaa-0000-0000-0000-000000000001"
    split_doc_id = "bbbbbbbb-1111-1111-1111-222222222222"
    parent_hash = "abc123"
    split_index = 2

    event = _make_event(
        document_id=split_doc_id,
        content_hash=parent_hash,
        _split_processed=True,
        _original_document_id=original_doc_id,
        _split_index=split_index,
    )
    worker.process_event(event)

    # insert_document_and_ruling must receive the split doc ID
    mock_insert_doc_and_ruling.assert_called_once()
    call_kwargs = mock_insert_doc_and_ruling.call_args
    assert call_kwargs.kwargs["document_id"] == split_doc_id

    # content_hash must be a synthetic per-split hash, not the parent hash
    expected_hash = hashlib.sha256(f"{parent_hash}:ruling:{split_index}".encode()).hexdigest()
    assert call_kwargs.kwargs["content_hash"] == expected_hash
    assert call_kwargs.kwargs["content_hash"] != parent_hash


@patch("ingestion.worker.batch_upsert_parties")
@patch("ingestion.worker.insert_document_and_ruling", return_value=True)
@patch("ingestion.worker.upsert_case_returning_title", return_value=("case-uuid-1", None))
@patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_non_split_event_uses_original_document_id(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_upsert_court: MagicMock,
    mock_upsert_case: MagicMock,
    mock_insert_doc_and_ruling: MagicMock,
    mock_batch_upsert: MagicMock,
) -> None:
    """Non-split events should still use the original document_id and content_hash unchanged."""
    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    doc_id = "aaaaaaaa-0000-0000-0000-000000000001"
    original_hash = "abc123"

    event = _make_event(document_id=doc_id, content_hash=original_hash)
    worker.process_event(event)

    # insert_document_and_ruling must receive the original document_id
    mock_insert_doc_and_ruling.assert_called_once()
    assert mock_insert_doc_and_ruling.call_args.kwargs["document_id"] == doc_id

    # content_hash must be unchanged (no synthetic hash for non-splits)
    assert mock_insert_doc_and_ruling.call_args.kwargs["content_hash"] == original_hash


def test_make_split_document_id_deterministic() -> None:
    """make_split_document_id produces the same ID for the same inputs."""
    from ingestion.split_ids import make_split_document_id

    doc_id = "aaaaaaaa-0000-0000-0000-000000000001"
    assert make_split_document_id(doc_id, 0) == make_split_document_id(doc_id, 0)
    assert make_split_document_id(doc_id, 1) == make_split_document_id(doc_id, 1)
    assert make_split_document_id(doc_id, 0) != make_split_document_id(doc_id, 1)


# ---------------------------------------------------------------------------
# Ruling summarization integration tests (#1099)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.summarize_ruling")
@patch("ingestion.worker.psycopg")
def test_process_event_summarizes_ruling_when_enabled(
    mock_psycopg: MagicMock,
    mock_summarize: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When ENABLE_RULING_SUMMARIZATION is set, summarize_ruling is called."""
    worker, os_mock = _make_worker()
    worker._summarization_enabled = True
    worker._summarization_client = MagicMock()
    mock_summarize.return_value = ("The court granted the motion.", "claude-haiku-4-5-20251001")

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event()
    worker.process_event(event)

    # Verify summarize_ruling was called with cleaned ruling text
    mock_summarize.assert_called_once()
    call_kwargs = mock_summarize.call_args
    assert call_kwargs.kwargs["client"] is worker._summarization_client

    # Verify insert_ruling received summary fields — check the SQL contains
    # the summary columns
    sql_calls = str(mock_cur.execute.call_args_list)
    assert "summary" in sql_calls
    assert "summary_model" in sql_calls
    assert "summary_generated_at" in sql_calls


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.summarize_ruling")
@patch("ingestion.worker.psycopg")
def test_process_event_opensearch_uses_llm_summary_when_available(
    mock_psycopg: MagicMock,
    mock_summarize: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """OpenSearch indexed doc uses the LLM summary instead of truncated text (#1183)."""
    worker, os_mock = _make_worker()
    worker._summarization_enabled = True
    worker._summarization_client = MagicMock()
    llm_summary = "The court granted the motion for summary judgment."
    mock_summarize.return_value = (llm_summary, "claude-haiku-4-5-20251001")

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event()
    worker.process_event(event)

    os_mock.index.assert_called_once()
    indexed_doc = os_mock.index.call_args.kwargs["body"]
    assert indexed_doc["summary"] == llm_summary


@patch("ingestion.worker.summarize_ruling")
@patch("ingestion.worker.psycopg")
def test_process_event_opensearch_falls_back_to_truncated_text_without_summary(
    mock_psycopg: MagicMock,
    mock_summarize: MagicMock,
) -> None:
    """OpenSearch uses truncated text when summarization is disabled (#1183)."""
    worker, os_mock = _make_worker()
    # summarization disabled by default

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        None,  # resolve_judge: no existing alias
        None,  # resolve_judge: no canonical name match
        None,  # resolve_judge: roster court_code lookup (no court found)
        ("judge-uuid-1",),  # resolve_judge: INSERT INTO judges
        (True,),  # insert_document (via insert_document_and_ruling)
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.rowcount = 1

    event = _make_event(
        outcome="granted",
        motion_type="demurrer",
        case_title="In re Marriage of Smith",
    )
    worker.process_event(event)

    os_mock.index.assert_called_once()
    indexed_doc = os_mock.index.call_args.kwargs["body"]
    # Should fall back to truncated cleaned_ruling_text
    assert indexed_doc["summary"] is not None
    assert len(indexed_doc["summary"]) <= 500
    mock_summarize.assert_not_called()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.summarize_ruling")
@patch("ingestion.worker.psycopg")
def test_process_event_summarization_disabled_by_default(
    mock_psycopg: MagicMock,
    mock_summarize: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When ENABLE_RULING_SUMMARIZATION is not set, summarize_ruling is not called."""
    worker, os_mock = _make_worker()
    # _summarization_enabled defaults to False
    assert not worker._summarization_enabled

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event()
    worker.process_event(event)

    mock_summarize.assert_not_called()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.summarize_ruling")
@patch("ingestion.worker.psycopg")
def test_process_event_summarization_failure_does_not_block_ingestion(
    mock_psycopg: MagicMock,
    mock_summarize: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Summary generation failure should not prevent ruling insertion."""
    worker, os_mock = _make_worker()
    worker._summarization_enabled = True
    worker._summarization_client = MagicMock()
    # Simulate summarization returning None (failure)
    mock_summarize.return_value = (None, "claude-haiku-4-5-20251001")

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event()
    worker.process_event(event)

    # Ruling should still be committed even though summary failed
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.summarize_ruling")
@patch("ingestion.worker.psycopg")
def test_process_event_summarization_includes_case_context(
    mock_psycopg: MagicMock,
    mock_summarize: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Case context (case_title, motion_type) is passed to summarize_ruling."""
    worker, os_mock = _make_worker()
    worker._summarization_enabled = True
    worker._summarization_client = MagicMock()
    mock_summarize.return_value = ("Summary text.", "claude-haiku-4-5-20251001")

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event(
        case_title="In re: Estate of Smith",
        motion_type="Demurrer",
        outcome="granted",
    )
    worker.process_event(event)

    mock_summarize.assert_called_once()
    call_kwargs = mock_summarize.call_args
    case_context = call_kwargs.kwargs["case_context"]
    assert case_context is not None
    assert case_context["case_title"] == "In re: Estate of Smith"
    # "Demurrer" (title-case from scraper) is normalized to "demurrer" (#1712)
    assert case_context["motion_type"] == "demurrer"


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.summarize_ruling")
@patch("ingestion.worker.psycopg")
def test_process_event_summarization_no_case_context_when_missing(
    mock_psycopg: MagicMock,
    mock_summarize: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When case_title and motion_type are both None/absent, case_context is None."""
    worker, os_mock = _make_worker()
    worker._summarization_enabled = True
    worker._summarization_client = MagicMock()
    mock_summarize.return_value = ("Summary text.", "claude-haiku-4-5-20251001")

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    # Use ruling text that won't trigger regex motion_type extraction
    event = _make_event(
        case_title=None,
        ruling_text="The court has reviewed the matter and issues this order.",
    )
    # Remove motion_type and outcome from the event
    event.pop("motion_type", None)
    event.pop("outcome", None)
    worker.process_event(event)

    mock_summarize.assert_called_once()
    call_kwargs = mock_summarize.call_args
    # No case_title or motion_type available, so context should be None
    assert call_kwargs.kwargs["case_context"] is None


# ---------------------------------------------------------------------------
# PDF empty ruling_text warning (#1335)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_warns_on_pdf_with_no_ruling_text(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: object,
) -> None:
    """When a PDF document has no ruling_text after all extraction attempts,
    the worker should log a warning (#1335)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        ruling_text=None,
        content_format="pdf",
    )
    worker.process_event(event)

    import logging as _logging

    warning_records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if r.levelno >= _logging.WARNING and "no ruling text after all extraction" in r.getMessage()
    ]
    assert len(warning_records) >= 1, (
        f"Expected warning about PDF with no ruling text, got: "
        f"{[r.getMessage() for r in caplog.records]}"  # type: ignore[attr-defined]
    )


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_no_warning_when_pdf_has_ruling_text(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: object,
) -> None:
    """When a PDF document has ruling_text, no empty-text warning should fire."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        ruling_text="The motion for summary judgment is GRANTED.",
        content_format="pdf",
    )
    worker.process_event(event)

    import logging as _logging

    warning_records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if r.levelno >= _logging.WARNING and "no ruling text after all extraction" in r.getMessage()
    ]
    assert len(warning_records) == 0, "No warning expected when PDF has ruling text"


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_no_pdf_warning_for_html_without_ruling_text(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: object,
) -> None:
    """When an HTML document has no ruling_text, the PDF-specific warning should NOT fire."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        ruling_text=None,
        content_format="html",
    )
    worker.process_event(event)

    import logging as _logging

    warning_records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if r.levelno >= _logging.WARNING and "no ruling text after all extraction" in r.getMessage()
    ]
    assert len(warning_records) == 0, "No PDF warning expected for HTML documents"


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.extract_text_from_pdf", return_value="")
@patch("ingestion.worker.is_pdf_binary", return_value=True)
@patch("ingestion.worker.extract_fields_llm", return_value=None)
@patch("ingestion.worker.psycopg")
def test_process_event_warns_on_raw_pdf_binary_with_empty_extraction(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_is_pdf: MagicMock,
    mock_extract_pdf: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: object,
) -> None:
    """When raw PDF binary is sent and text extraction returns empty string,
    the 'no ruling text' warning should still fire (#1335)."""
    worker, os_mock = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        ruling_text="%PDF-1.4 image-only binary",
        content_format="pdf",
        outcome=None,
        motion_type=None,
    )
    worker.process_event(event)

    import logging as _logging

    warning_records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if r.levelno >= _logging.WARNING and "no ruling text after all extraction" in r.getMessage()
    ]
    assert len(warning_records) >= 1, (
        f"Expected warning about PDF with no ruling text after raw binary extraction, "
        f"got: {[r.getMessage() for r in caplog.records]}"  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Cross-case title lookup (#2006)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(
    "ingestion.worker.upsert_case_returning_title",
    return_value=("case-uuid-1", "Smith v. Jones"),
)
@patch("ingestion.worker.psycopg")
def test_process_event_cross_case_title_lookup(
    mock_psycopg: MagicMock,
    mock_upsert: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: object,
) -> None:
    """When event has no case_title but DB already has one for this case,
    the worker populates case_title from the existing DB record (#2006)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    import logging as _logging

    with caplog.at_level(_logging.INFO, logger="ingestion.worker"):  # type: ignore[attr-defined]
        # Event with no case_title and no hearing_date — enrichment still
        # runs (#2215) but the DB already knows this case's title from
        # a prior ruling
        event = _make_event(
            case_title=None, hearing_date=None, ruling_text="The motion is GRANTED."
        )
        worker.process_event(event)

    # Verify upsert_case_returning_title was called
    mock_upsert.assert_called_once()

    # Check all log messages to understand flow
    all_msgs = [(r.levelno, r.getMessage()) for r in caplog.records]  # type: ignore[attr-defined]

    # The cross-case title lookup should fire since case_title=None and
    # effective_title="Smith v. Jones"
    info_records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if r.levelno >= _logging.INFO
        and "Populated case_title from existing DB record" in r.getMessage()
    ]
    assert len(info_records) >= 1, f"Expected info about cross-case title lookup, got: {all_msgs}"


# ---------------------------------------------------------------------------
# Null case_title warning (#1359)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_warns_on_null_case_title(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: object,
) -> None:
    """When case_title remains None after all extraction attempts,
    the worker should log a warning (#1359)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    # Ruling text that does NOT contain any parseable party names or title
    event = _make_event(
        case_title=None,
        ruling_text="This is a ruling with no party names or case title.",
    )
    worker.process_event(event)

    import logging as _logging

    warning_records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if r.levelno >= _logging.WARNING and "NULL title" in r.getMessage()
    ]
    assert len(warning_records) >= 1, (
        f"Expected warning about NULL case_title, got: {[r.getMessage() for r in caplog.records]}"  # type: ignore[attr-defined]
    )


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_no_warning_when_case_title_present(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: object,
) -> None:
    """When case_title is provided, no null-title warning should fire."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1

    event = _make_event(
        case_title="Smith v. Jones",
        ruling_text="The motion for summary judgment is GRANTED.",
    )
    # "Smith v. Jones" triggers party extraction from caption -> batch_upsert_parties
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
        ("party-uuid-1",),  # batch_upsert_parties: Smith
        ("party-uuid-2",),  # batch_upsert_parties: Jones
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]
    worker.process_event(event)

    import logging as _logging

    warning_records = [
        r
        for r in caplog.records  # type: ignore[attr-defined]
        if r.levelno >= _logging.WARNING and "NULL title" in r.getMessage()
    ]
    assert len(warning_records) == 0, "No warning expected when case_title is present"


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_motion_type_fallback_populates_case_type(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When case number has no type code and scraper_id is generic,
    motion_type fallback infers case_type (#1702)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    # Ventura-like event: all-digit case number, generic scraper_id,
    # but with a civil motion_type.
    event = _make_event(
        scraper_id="ca-ventura-tentatives",
        case_number="202300574258",
        motion_type="motion_to_compel",
        case_type=None,
        ruling_text="The motion to compel is GRANTED.",
    )
    worker.process_event(event)

    # The upsert_case call should include case_type='civil'
    all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "civil" in all_sql


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_process_event_normalizes_title_case_motion_type(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Title-case motion_type from scraper is normalized to snake_case (#1712)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
    ]
    mock_cur.rowcount = 1

    # SD-like event with un-normalized motion_type from scraper
    event = _make_event(
        motion_type="Motion to Compel Further Responses",
        ruling_text="The motion to compel further responses is GRANTED.",
    )
    worker.process_event(event)

    # The normalized "motion_to_compel" should be in the ruling insert, not the raw value
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    assert "motion_to_compel" in sql_args
    # The raw title-case value should NOT be stored
    assert "Motion to Compel Further Responses" not in sql_args


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.format_ruling_text")
@patch("ingestion.worker.psycopg")
def test_process_event_uses_scraper_ruling_text_html(
    mock_psycopg: MagicMock,
    mock_format: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When event has ruling_text_html, LLM formatting is skipped."""
    worker, os_mock = _make_worker()
    worker._formatting_enabled = True
    worker._formatting_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    scraper_html = "<p>The motion is <strong>GRANTED</strong>.</p>"
    event = _make_event(ruling_text_html=scraper_html)
    worker.process_event(event)

    # LLM format_ruling_text should NOT be called when scraper provides HTML
    mock_format.assert_not_called()

    # Verify insert_ruling received the scraper-provided HTML
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args_str = str(ruling_calls[0])
    assert "GRANTED" in sql_args_str


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.format_ruling_text")
@patch("ingestion.worker.psycopg")
def test_process_event_falls_back_to_llm_when_no_scraper_html(
    mock_psycopg: MagicMock,
    mock_format: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When event has no ruling_text_html and formatting is enabled, LLM is used."""
    worker, os_mock = _make_worker()
    worker._formatting_enabled = True
    worker._formatting_client = MagicMock()
    mock_format.return_value = "<p>LLM formatted</p>"

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event()  # No ruling_text_html
    worker.process_event(event)

    # LLM format_ruling_text should be called
    mock_format.assert_called_once()


# ---------------------------------------------------------------------------
# Deterministic case_title enrichment (#2212)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.psycopg")
def test_deterministic_case_title_keeps_good_llm_title(
    mock_psycopg: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the LLM title is already clean and plausible, it is preserved (#2212)."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    # Good LLM title -- should NOT be overridden.
    # Use a title that won't trigger party extraction (no "v.").
    event = _make_event(
        _llm_extracted=True,
        _split_processed=True,
        case_title="In the Matter of the Estate of Williams",
        ruling_text="The motion is GRANTED.",
    )

    import logging

    with caplog.at_level(logging.INFO):
        worker.process_event(event)

    # No override should have happened
    assert not any("Deterministic case_title overrides" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# LLM enrichment integration (#2176)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("framework.llm_enrichment.enrich_ruling")
@patch("ingestion.worker.psycopg")
def test_llm_enrichment_fills_missing_fields(
    mock_psycopg: MagicMock,
    mock_enrich_ruling: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """LLM enrichment populates outcome, motion_type, case_title, and parties."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    mock_enrich_ruling.return_value = LlmEnrichmentResult(
        case_title="Garcia v. State Farm",
        motion_type="motion_to_compel",
        outcome="granted_in_part",
        parties=EnrichmentParties(
            plaintiffs=["Garcia"],
            defendants=["State Farm"],
        ),
    )

    worker, os_mock = _make_worker()
    # Enable enrichment (default is enabled)
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: RETURNING is_new = True
        # batch_upsert_parties: RETURNING ids for extracted parties
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]

    event = _make_event(
        outcome=None,
        motion_type=None,
        case_title=None,
        ruling_text="The motion to compel further discovery is GRANTED IN PART.",
    )
    # Remove parties from event
    event.pop("parties", None)

    worker.process_event(event)

    mock_enrich_ruling.assert_called_once()
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("framework.llm_enrichment.enrich_ruling")
@patch("ingestion.worker.psycopg")
def test_llm_enrichment_disabled_falls_through_to_regex(
    mock_psycopg: MagicMock,
    mock_enrich_ruling: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When USE_LLM_ENRICHMENT=false, enrichment is skipped and regex runs."""
    worker, os_mock = _make_worker()
    worker._llm_enrichment_enabled = False

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event(
        outcome=None,
        motion_type=None,
        ruling_text="The motion for summary judgment is GRANTED.",
    )

    worker.process_event(event)

    # enrich_ruling should NOT have been called
    mock_enrich_ruling.assert_not_called()
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("framework.llm_enrichment.enrich_ruling")
@patch("ingestion.worker.psycopg")
def test_llm_enrichment_does_not_override_existing_fields(
    mock_psycopg: MagicMock,
    mock_enrich_ruling: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """LLM enrichment does not overwrite fields already provided by the scraper."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    mock_enrich_ruling.return_value = LlmEnrichmentResult(
        case_title="Different Title",
        motion_type="demurrer",
        outcome="denied",
        parties=EnrichmentParties(
            plaintiffs=["Different Plaintiff"],
            defendants=["Different Defendant"],
        ),
    )

    worker, os_mock = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
        # batch_upsert_parties: RETURNING id for the party
        ("party-uuid-1",),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [False]

    # All enrichment fields are already provided
    event = _make_event(
        outcome="granted",
        motion_type="msj",
        case_title="Original Title",
        ruling_text="The motion for summary judgment is GRANTED.",
        parties=[{"name": "Original Plaintiff", "role": "plaintiff"}],
    )

    worker.process_event(event)

    # enrich_ruling should NOT have been called since all fields are present
    mock_enrich_ruling.assert_not_called()
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("framework.llm_enrichment.enrich_ruling")
@patch("ingestion.worker.psycopg")
def test_llm_enrichment_failure_falls_back_to_regex(
    mock_psycopg: MagicMock,
    mock_enrich_ruling: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """When LLM enrichment returns empty result, regex fallback still runs."""
    from framework.llm_enrichment import LlmEnrichmentResult

    # LLM enrichment returns empty result (all fields None)
    mock_enrich_ruling.return_value = LlmEnrichmentResult()

    worker, os_mock = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]

    event = _make_event(
        outcome=None,
        motion_type=None,
        ruling_text="The motion for summary judgment is GRANTED.",
    )

    worker.process_event(event)

    # enrich_ruling was called but returned empty — regex should fill the gaps
    mock_enrich_ruling.assert_called_once()
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("framework.llm_enrichment.enrich_ruling")
@patch("ingestion.worker.psycopg")
def test_llm_enrichment_skipped_for_llm_extracted_events_with_fields(
    mock_psycopg: MagicMock,
    mock_enrich_ruling: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Events from the LLM split path with all fields populated skip enrichment."""
    worker, os_mock = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
        # batch_upsert_parties: RETURNING id for the party
        ("party-uuid-1",),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [False]

    event = _make_event(
        _llm_extracted=True,
        _split_processed=True,
        outcome="granted",
        motion_type="msj",
        case_title="Smith v. Jones",
        ruling_text="The motion is GRANTED.",
        parties=[{"name": "Smith", "role": "plaintiff"}],
    )

    worker.process_event(event)

    # Should NOT call enrichment — all fields are already populated
    mock_enrich_ruling.assert_not_called()
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("framework.llm_enrichment.enrich_ruling")
@patch("ingestion.worker.psycopg")
def test_llm_enrichment_runs_for_multimodal_events_missing_fields(
    mock_psycopg: MagicMock,
    mock_enrich_ruling: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Multimodal events missing enrichment fields get LLM enrichment."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    mock_enrich_ruling.return_value = LlmEnrichmentResult(
        case_title="Davis v. Metro",
        motion_type="msj",
        outcome="denied",
        parties=EnrichmentParties(
            plaintiffs=["Davis"],
            defendants=["Metro"],
        ),
    )

    worker, os_mock = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
        # batch_upsert_parties
        ("party-uuid-1",),
        ("party-uuid-2",),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]

    event = _make_event(
        _llm_extracted=True,
        _split_processed=True,
        outcome=None,
        motion_type=None,
        case_title=None,
        ruling_text="The motion for summary judgment filed by defendant is DENIED.",
    )
    event.pop("parties", None)

    worker.process_event(event)

    mock_enrich_ruling.assert_called_once()
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("framework.llm_enrichment.enrich_ruling")
@patch("ingestion.worker.psycopg")
def test_llm_enrichment_parties_converted_to_dicts(
    mock_psycopg: MagicMock,
    mock_enrich_ruling: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Enrichment party names are converted to list[dict] format."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    mock_enrich_ruling.return_value = LlmEnrichmentResult(
        case_title="Alpha v. Beta Corp",
        motion_type="mtd",
        outcome="granted",
        parties=EnrichmentParties(
            plaintiffs=["Alpha LLC"],
            defendants=["Beta Corp", "Gamma Inc"],
        ),
    )

    worker, os_mock = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.rowcount = 1
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
        # batch_upsert_parties: 3 parties
        ("party-uuid-1",),
        ("party-uuid-2",),
        ("party-uuid-3",),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, True, False]

    event = _make_event(
        outcome=None,
        motion_type=None,
        case_title=None,
        ruling_text="The motion to dismiss is GRANTED.",
    )
    event.pop("parties", None)

    worker.process_event(event)

    mock_enrich_ruling.assert_called_once()
    mock_conn.commit.assert_called_once()


def test_llm_enrich_fields_returns_none_when_disabled() -> None:
    """_llm_enrich_fields returns None when enrichment is disabled."""
    worker, _ = _make_worker()
    worker._llm_enrichment_enabled = False

    result = worker._llm_enrich_fields("Some ruling text.", "doc-1")
    assert result is None


def test_llm_enrich_fields_returns_none_when_no_client() -> None:
    """_llm_enrich_fields returns None when no client is available."""
    worker, _ = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = None

    result = worker._llm_enrich_fields("Some ruling text.", "doc-1")
    assert result is None


def test_llm_enrich_fields_returns_none_for_empty_text() -> None:
    """_llm_enrich_fields returns None for empty ruling text."""
    worker, _ = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    result = worker._llm_enrich_fields("", "doc-1")
    assert result is None

    result = worker._llm_enrich_fields("   \n  ", "doc-1")
    assert result is None


@patch("framework.llm_enrichment.enrich_ruling")
def test_llm_enrich_fields_returns_result_with_data(
    mock_enrich_ruling: MagicMock,
) -> None:
    """_llm_enrich_fields returns the result when enrichment produces data."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    mock_enrich_ruling.return_value = LlmEnrichmentResult(
        case_title="Test v. Case",
        motion_type="msj",
        outcome="granted",
        parties=EnrichmentParties(plaintiffs=["Test"], defendants=["Case"]),
    )

    worker, _ = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    result = worker._llm_enrich_fields("The motion is GRANTED.", "doc-1")
    assert result is not None
    assert result.case_title == "Test v. Case"
    assert result.motion_type == "msj"
    assert result.outcome == "granted"


@patch("framework.llm_enrichment.enrich_ruling")
def test_llm_enrich_fields_returns_none_for_empty_result(
    mock_enrich_ruling: MagicMock,
) -> None:
    """_llm_enrich_fields returns None when enrichment produces empty result."""
    from framework.llm_enrichment import LlmEnrichmentResult

    mock_enrich_ruling.return_value = LlmEnrichmentResult()

    worker, _ = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    result = worker._llm_enrich_fields("Some ruling text.", "doc-1")
    assert result is None


@patch("framework.llm_enrichment.enrich_ruling")
def test_llm_enrich_fields_returns_none_for_api_failure(
    mock_enrich_ruling: MagicMock,
) -> None:
    """_llm_enrich_fields returns None when LLM API call fails (returns None)."""
    mock_enrich_ruling.return_value = None

    worker, _ = _make_worker()
    worker._llm_enrichment_enabled = True
    worker._enrichment_client = MagicMock()

    result = worker._llm_enrich_fields("Some ruling text.", "doc-1")
    assert result is None


def test_enrichment_enabled_by_default() -> None:
    """LLM enrichment is enabled by default when no env var is set."""
    worker, _ = _make_worker()
    assert worker._llm_enrichment_enabled is True
    assert worker._llm_enrichment_enabled is True


# ---------------------------------------------------------------------------
# IngestionWorker._apply_enrichment_result — shared helper for merging an
# LlmEnrichmentResult into the current field values (#2286).
# ---------------------------------------------------------------------------


def test_apply_enrichment_result_returns_inputs_when_result_is_none() -> None:
    """None enrichment_result returns inputs unchanged and empty methods dict."""
    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            None,
            outcome=None,
            motion_type=None,
            case_title=None,
            parties_data=[],
        )
    )
    assert outcome is None
    assert motion_type is None
    assert case_title is None
    assert parties_data == []
    assert methods == {}


def test_apply_enrichment_result_returns_inputs_when_result_is_none_with_values() -> None:
    """None enrichment_result preserves any existing values."""
    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            None,
            outcome="granted",
            motion_type="msj",
            case_title="Existing v. Case",
            parties_data=[{"name": "Alpha", "role": "plaintiff"}],
        )
    )
    assert outcome == "granted"
    assert motion_type == "msj"
    assert case_title == "Existing v. Case"
    assert parties_data == [{"name": "Alpha", "role": "plaintiff"}]
    assert methods == {}


def test_apply_enrichment_result_fills_all_missing_fields() -> None:
    """All four fields are populated when currently missing and result has them."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    result = LlmEnrichmentResult(
        case_title="Alpha v. Beta",
        motion_type="msj",
        outcome="granted",
        parties=EnrichmentParties(plaintiffs=["Alpha"], defendants=["Beta"]),
    )

    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            result,
            outcome=None,
            motion_type=None,
            case_title=None,
            parties_data=[],
        )
    )

    assert outcome == "granted"
    assert motion_type == "msj"
    assert case_title == "Alpha v. Beta"
    assert parties_data == [
        {"name": "Alpha", "role": "plaintiff"},
        {"name": "Beta", "role": "defendant"},
    ]
    assert methods == {
        "outcome": "llm_enrichment",
        "motion_type": "llm_enrichment",
        "case_title": "llm_enrichment",
        "parties": "llm_enrichment",
    }


def test_apply_enrichment_result_does_not_overwrite_existing_fields() -> None:
    """Existing values are preserved — helper only fills missing fields."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    result = LlmEnrichmentResult(
        case_title="New Title",
        motion_type="mtd",
        outcome="denied",
        parties=EnrichmentParties(plaintiffs=["NewP"], defendants=["NewD"]),
    )

    existing_parties = [{"name": "ExistingP", "role": "plaintiff"}]
    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            result,
            outcome="granted",
            motion_type="msj",
            case_title="Existing Title",
            parties_data=existing_parties,
        )
    )

    assert outcome == "granted"
    assert motion_type == "msj"
    assert case_title == "Existing Title"
    assert parties_data == existing_parties
    assert methods == {}


def test_apply_enrichment_result_partial_fill() -> None:
    """Only missing fields are filled; already-set fields are untouched."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    result = LlmEnrichmentResult(
        case_title="From LLM",
        motion_type="msj",
        outcome="granted",
        parties=EnrichmentParties(plaintiffs=["Alpha"], defendants=["Beta"]),
    )

    # outcome + parties already set; motion_type + case_title missing.
    existing_parties = [{"name": "Existing", "role": "plaintiff"}]
    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            result,
            outcome="denied",
            motion_type=None,
            case_title=None,
            parties_data=existing_parties,
        )
    )

    assert outcome == "denied"  # preserved
    assert motion_type == "msj"  # filled
    assert case_title == "From LLM"  # filled
    assert parties_data == existing_parties  # preserved
    assert methods == {
        "motion_type": "llm_enrichment",
        "case_title": "llm_enrichment",
    }


def test_apply_enrichment_result_parties_conversion_multiple() -> None:
    """EnrichmentParties with multiple plaintiffs/defendants converts correctly."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    result = LlmEnrichmentResult(
        parties=EnrichmentParties(
            plaintiffs=["A1", "A2"],
            defendants=["B1", "B2", "B3"],
        ),
    )

    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            result,
            outcome=None,
            motion_type=None,
            case_title=None,
            parties_data=[],
        )
    )

    assert parties_data == [
        {"name": "A1", "role": "plaintiff"},
        {"name": "A2", "role": "plaintiff"},
        {"name": "B1", "role": "defendant"},
        {"name": "B2", "role": "defendant"},
        {"name": "B3", "role": "defendant"},
    ]
    assert methods == {"parties": "llm_enrichment"}


def test_apply_enrichment_result_no_parties_when_both_lists_empty() -> None:
    """Parties field is NOT filled when both plaintiffs and defendants are empty."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    result = LlmEnrichmentResult(
        case_title="Alpha v. Beta",
        parties=EnrichmentParties(plaintiffs=[], defendants=[]),
    )

    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            result,
            outcome=None,
            motion_type=None,
            case_title=None,
            parties_data=[],
        )
    )

    assert case_title == "Alpha v. Beta"
    assert parties_data == []
    assert "parties" not in methods
    assert methods == {"case_title": "llm_enrichment"}


def test_apply_enrichment_result_parties_plaintiffs_only() -> None:
    """Parties populated when plaintiffs list is non-empty but defendants is empty."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    result = LlmEnrichmentResult(
        parties=EnrichmentParties(plaintiffs=["OnlyP"], defendants=[]),
    )

    _, _, _, parties_data, methods = IngestionWorker._apply_enrichment_result(
        result,
        outcome=None,
        motion_type=None,
        case_title=None,
        parties_data=[],
    )

    assert parties_data == [{"name": "OnlyP", "role": "plaintiff"}]
    assert methods == {"parties": "llm_enrichment"}


def test_apply_enrichment_result_parties_defendants_only() -> None:
    """Parties populated when defendants list is non-empty but plaintiffs is empty."""
    from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

    result = LlmEnrichmentResult(
        parties=EnrichmentParties(plaintiffs=[], defendants=["OnlyD"]),
    )

    _, _, _, parties_data, methods = IngestionWorker._apply_enrichment_result(
        result,
        outcome=None,
        motion_type=None,
        case_title=None,
        parties_data=[],
    )

    assert parties_data == [{"name": "OnlyD", "role": "defendant"}]
    assert methods == {"parties": "llm_enrichment"}


def test_apply_enrichment_result_empty_result_yields_no_changes() -> None:
    """An LlmEnrichmentResult with all fields None/empty produces no changes."""
    from framework.llm_enrichment import LlmEnrichmentResult

    result = LlmEnrichmentResult()  # all fields default: None / empty lists

    outcome, motion_type, case_title, parties_data, methods = (
        IngestionWorker._apply_enrichment_result(
            result,
            outcome=None,
            motion_type=None,
            case_title=None,
            parties_data=[],
        )
    )

    assert outcome is None
    assert motion_type is None
    assert case_title is None
    assert parties_data == []
    assert methods == {}


def test_apply_enrichment_result_case_title_empty_string_treated_as_missing() -> None:
    """An empty string case_title is treated as missing and overwritten (matches original)."""
    from framework.llm_enrichment import LlmEnrichmentResult

    result = LlmEnrichmentResult(case_title="Filled Title")

    _, _, case_title, _, methods = IngestionWorker._apply_enrichment_result(
        result,
        outcome=None,
        motion_type=None,
        case_title="",  # falsy — original code uses `not case_title`
        parties_data=[],
    )

    assert case_title == "Filled Title"
    assert methods == {"case_title": "llm_enrichment"}


# ---------------------------------------------------------------------------
# Worker-level guards for Ventura (#2370): implausible hearing date, repeated
# title segments, trailing case number, probate decedent as judge.
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value=None)
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_rejects_implausible_llm_hearing_date(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Worker rejects LLM-returned hearing_date that is far from capture_ts (#2370).

    Covers worker.py line 1037 — the "Rejected LLM hearing_date as implausible"
    log branch of the plausibility guard.
    """
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, _ = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    # Capture timestamp March 4 2026; LLM returns hearing_date 6 months later.
    mock_llm.return_value = LLMExtractionResult(
        hearing_date=date(2026, 9, 15),
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="24NNCV02551",
                case_title="Smith v. Jones",
                outcome="granted",
            )
        ],
    )

    event = _make_event(
        case_number=None,
        case_title=None,
        hearing_date=None,
        ruling_text="Some ruling text",
        capture_timestamp="2026-03-04T23:00:00",
    )
    # Skip the multi-ruling split path so we reach the single-doc per-field
    # extraction path where the guards live.
    with patch.object(worker, "_llm_split_document", return_value=False):
        worker.process_event(event)

    mock_llm.assert_called_once()
    # The ruling INSERT should NOT contain 2026-09-15 (rejected as implausible).
    ruling_calls = [c for c in mock_cur.execute.call_args_list if "INSERT INTO rulings" in str(c)]
    assert len(ruling_calls) == 1
    sql_args = ruling_calls[0][0][1]
    # No hearing_date 2026-09-15 should appear in bound args
    for arg in sql_args:
        assert arg != date(2026, 9, 15)


@patch("ingestion.worker.resolve_judge", return_value=None)
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_dedupes_repeated_title_segments(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Worker dedupes repeated case_title segments (#2370).

    Covers worker.py lines 1195, 1203 — the "Removed repeated title segments"
    log and `case_title = deduped` assignment.
    """
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, _ = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    # LLM returns a title with the same 30-char segment repeated twice
    mock_llm.return_value = LLMExtractionResult(
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="56-2024-00123456-CU-BC-VTA",
                case_title=("CITY OF THOUSAND OAKS vs PACKARD CITY OF THOUSAND OAKS vs PACKARD"),
                outcome="granted",
            )
        ],
    )

    event = _make_event(
        case_number=None,
        case_title=None,
        hearing_date=None,
        ruling_text="Some ruling text",
    )
    with patch.object(worker, "_llm_split_document", return_value=False):
        worker.process_event(event)

    mock_llm.assert_called_once()
    # The title stored must NOT be the doubled/repeated one.
    all_sql_args = []
    for call in mock_cur.execute.call_args_list:
        args = call[0]
        if len(args) > 1 and args[1] is not None:
            all_sql_args.extend(args[1] if isinstance(args[1], (list, tuple)) else [])
    # None of the bound args should contain the doubled sequence.
    doubled_fragment = "PACKARD CITY OF THOUSAND OAKS"
    for a in all_sql_args:
        if isinstance(a, str):
            assert doubled_fragment not in a, f"Doubled title leaked into SQL: {a!r}"


@patch("ingestion.worker.resolve_judge", return_value=None)
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_strips_trailing_case_number_from_title(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Worker strips trailing case numbers from case_title (#2370).

    Covers worker.py lines 1206, 1214 — the "Stripped trailing case number
    from title" log and `case_title = without_cn` assignment.
    """
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, _ = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    # LLM returns a title with a Ventura probate case number appended.
    mock_llm.return_value = LLMExtractionResult(
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="202200570654PRLP",
                case_title="In the Matter of Denise Guadalupe Mejia 202200570654PRL",
                outcome="granted",
            )
        ],
    )

    event = _make_event(
        case_number=None,
        case_title=None,
        hearing_date=None,
        ruling_text="Some ruling text",
    )
    with patch.object(worker, "_llm_split_document", return_value=False):
        worker.process_event(event)

    mock_llm.assert_called_once()
    # Gather all bound args
    all_sql_args = []
    for call in mock_cur.execute.call_args_list:
        args = call[0]
        if len(args) > 1 and args[1] is not None:
            all_sql_args.extend(args[1] if isinstance(args[1], (list, tuple)) else [])
    # The title without the trailing case number should appear
    assert "In the Matter of Denise Guadalupe Mejia" in all_sql_args


@patch("ingestion.worker.resolve_judge", return_value=None)
@patch("ingestion.worker.extract_fields_llm")
@patch("ingestion.worker.psycopg")
def test_process_event_rejects_probate_decedent_as_judge(
    mock_psycopg: MagicMock,
    mock_llm: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Worker rejects LLM judge_name that matches the probate decedent (#2370).

    Covers worker.py lines 1240, 1248-1249 — the "Rejected LLM judge_name as
    probate decedent" log, ``judge_name = None``, and the
    ``extraction_methods.pop("judge_name", None)`` cleanup.
    """
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, _ = _make_worker()
    worker._llm_client = MagicMock()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    # Use return_value rather than limited side_effect so extra fetchone calls
    # (e.g. dept-judge fallback lookups after judge_name is discarded) don't
    # raise StopIteration.
    mock_cur.fetchone.return_value = ("uuid-1",)
    mock_cur.fetchall.return_value = []
    mock_cur.rowcount = 1

    # LLM returns the decedent's name as the judge_name
    mock_llm.return_value = LLMExtractionResult(
        judge_name="Delbert L. Webb",
        case_count=1,
        rulings=[
            LLMRulingResult(
                case_number="202200570654PRLP",
                case_title="Estate of Delbert L. Webb",
                outcome="granted",
            )
        ],
    )

    event = _make_event(
        case_number=None,
        case_title=None,
        judge_name=None,
        hearing_date=None,
        ruling_text="Some ruling text",
    )
    with patch.object(worker, "_llm_split_document", return_value=False):
        worker.process_event(event)

    mock_llm.assert_called_once()
    # resolve_judge should NOT be called because the decedent name was rejected.
    mock_resolve_judge.assert_not_called()


# ---------------------------------------------------------------------------
# Ruling guards — orphan check (#1337)
# ---------------------------------------------------------------------------


def test_check_no_orphan_rulings_zero_count_returns_orphan() -> None:
    """Zero rulings => OrphanCheckResult(is_orphan=True, reason=<non-empty>)."""
    from ingestion.ruling_guards import check_no_orphan_rulings

    result = check_no_orphan_rulings(0)
    assert result.is_orphan is True
    assert result.reason is not None
    assert result.reason  # non-empty


def test_check_no_orphan_rulings_one_count_not_orphan() -> None:
    """One ruling => OrphanCheckResult(is_orphan=False, reason=None)."""
    from ingestion.ruling_guards import check_no_orphan_rulings

    result = check_no_orphan_rulings(1)
    assert result.is_orphan is False
    assert result.reason is None


def test_check_no_orphan_rulings_many_count_not_orphan() -> None:
    """Five rulings => OrphanCheckResult(is_orphan=False, reason=None)."""
    from ingestion.ruling_guards import check_no_orphan_rulings

    result = check_no_orphan_rulings(5)
    assert result.is_orphan is False
    assert result.reason is None


def test_llm_split_logs_orphan_warning_when_no_rulings_extracted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Orphan integrity check logs structured warning when LLM returns empty list (#1337).

    When ``_llm_split_document`` invokes the LLM extractor and it returns
    an empty list of rulings (successful extraction, zero cases found), the
    worker must emit a structured warning ``"Orphan document: no rulings
    extracted"`` with ``document_id`` in ``extra`` so operators can surface
    probate PDFs and other problematic content in the log stream.
    """
    worker, _ = _make_worker()
    worker._llm_client = MagicMock()

    # Mock the framework text-based extractor so we stay on the text path
    # and skip multimodal initialization entirely.
    mock_extractor = MagicMock()
    mock_extractor.extract.return_value = []  # LLM returns NO rulings (orphan case).

    event = _make_event(
        document_id="orphan-doc-id-0000-0000-000000000001",
        scraper_id="ca-oc-tentatives-probate",
        state="CA",
        county="Orange",
        s3_key="ca/orange/superior_court/raw/probate-orphan.pdf",
        ruling_text="Some text that the LLM cannot classify as a ruling",
    )

    with (
        patch.object(worker, "_get_framework_extractor", return_value=mock_extractor),
        patch.object(worker, "_get_multimodal_extractor", return_value=None),
        caplog.at_level("WARNING", logger="ingestion.worker"),
    ):
        result = worker._llm_split_document(
            event,
            event["document_id"],
            event["ruling_text"],
            event["state"],
            event["county"],
            raw_pdf_bytes=None,
        )

    assert result is False
    orphan_records = [
        r for r in caplog.records if r.message == "Orphan document: no rulings extracted"
    ]
    assert len(orphan_records) == 1, (
        f"Expected exactly one orphan warning; got {len(orphan_records)}: "
        f"{[r.message for r in caplog.records]}"
    )
    record = orphan_records[0]
    assert record.levelname == "WARNING"
    # Verify the structured extra fields are attached to the LogRecord.
    assert getattr(record, "document_id", None) == "orphan-doc-id-0000-0000-000000000001"
    assert getattr(record, "original_document_id", None) == ("orphan-doc-id-0000-0000-000000000001")
    assert getattr(record, "s3_key", None) == ("ca/orange/superior_court/raw/probate-orphan.pdf")
    assert getattr(record, "county", None) == "Orange"
    assert getattr(record, "state", None) == "CA"
    assert getattr(record, "scraper_id", None) == "ca-oc-tentatives-probate"
    assert getattr(record, "reason", None)  # non-empty


def test_llm_split_does_not_log_orphan_warning_when_rulings_extracted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No orphan warning should fire when the LLM extractor returns >=1 ruling (#1337).

    DB dependencies (``psycopg``, ``delete_stale_split_children``) and the
    recursive ``process_event`` call for each split are all mocked so
    ``_llm_split_document`` completes cleanly end-to-end.  This makes the
    assertion stronger: if a future change causes the function to raise
    before reaching the post-extraction branch, the test fails — rather
    than silently asserting on log records that were never emitted (#2443).
    """
    from framework.llm_schema import ExtractedRuling

    worker, _ = _make_worker()
    worker._llm_client = MagicMock()

    # Mock the framework text-based extractor returning one ruling.
    mock_extractor = MagicMock()
    mock_extractor.extract.return_value = [
        ExtractedRuling(
            extracted_case_number="24STCV00001",
            extracted_case_title="Foo v. Bar",
            extracted_judge_name="Smith, John",
            department="Dept. 1",
            motion_type="demurrer",
            outcome=None,
            hearing_date="2026-03-05",
            extracted_parties=[],
            ruling_text="The motion is GRANTED.",
            case_type=None,
        )
    ]

    event = _make_event(ruling_text="Some ruling text")

    mock_conn, _ = _make_mock_conn()

    with (
        patch.object(worker, "_get_framework_extractor", return_value=mock_extractor),
        patch.object(worker, "_get_multimodal_extractor", return_value=None),
        # Mock DB writes so the happy path completes without broad try/except.
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch("ingestion.worker.delete_stale_split_children", return_value=0),
        # Stub the recursive dispatch of the per-ruling split event so we
        # don't drag in the entire process_event code path (which would
        # require many more mocks and is covered by other tests).
        patch.object(worker, "process_event") as mock_process_event,
        caplog.at_level("WARNING", logger="ingestion.worker"),
    ):
        mock_psycopg.connect.return_value = mock_conn
        result = worker._llm_split_document(
            event,
            event["document_id"],
            event["ruling_text"],
            event["state"],
            event["county"],
            raw_pdf_bytes=None,
        )

    # Sanity check: the method reached the split-dispatch step and
    # returned True — i.e. the orphan branch was not taken.
    assert result is True
    assert mock_process_event.call_count == 1

    orphan_records = [
        r for r in caplog.records if r.message == "Orphan document: no rulings extracted"
    ]
    assert len(orphan_records) == 0, "Orphan warning should not fire when rulings are extracted"


# ---------------------------------------------------------------------------
# #2405 — force_update path on insert_ruling / insert_document /
# insert_document_and_ruling.  Reingest needs to overwrite bad historical
# values (case_id, judge_id, hearing_date, outcome, motion_type,
# department) with EXCLUDED.* unconditionally — including NULL — so
# post-extraction guards can actually clear bad data.  Live ingestion
# keeps the default COALESCE semantics so a missed re-extraction does
# not erase a previously-good value.
# ---------------------------------------------------------------------------


def _make_ruling_upsert_conn() -> MagicMock:
    """Return a MagicMock psycopg.Connection for insert_ruling tests."""
    conn = MagicMock(spec=psycopg.Connection)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    return conn


def _rulings_insert_sql(conn: MagicMock) -> str:
    """Return the SQL text of the INSERT INTO rulings call made on *conn*."""
    cur = conn.cursor.return_value
    for call in cur.execute.call_args_list:
        sql = call.args[0] if call.args else ""
        if "INSERT INTO rulings" in sql:
            return sql
    raise AssertionError("No INSERT INTO rulings call was made on this cursor")


def test_insert_ruling_default_is_coalesce() -> None:
    """Default insert_ruling uses COALESCE — does not erase existing values.

    Identity anchors (case_id, judge_id) use PRESERVE-FIRST COALESCE
    ``COALESCE(rulings.col, EXCLUDED.col)`` so a live re-ingest cannot
    silently relink the ruling to a different case or judge (#2475 /
    sibling of #2468).  Correctable facts (hearing_date, outcome,
    motion_type, department) keep INCOMING-WINS COALESCE
    ``COALESCE(EXCLUDED.col, rulings.col)`` so a later, higher-quality
    extraction legitimately replaces them while an incoming NULL preserves
    the good existing value (#2405 semantics).
    """
    conn = _make_ruling_upsert_conn()

    insert_ruling(
        conn,
        document_id="11111111-1111-1111-1111-111111111111",
        case_id="22222222-2222-2222-2222-222222222222",
        court_id="33333333-3333-3333-3333-333333333333",
        hearing_date=date(2026, 4, 1),
        ruling_text="Motion granted",
        department="C-10",
        judge_id="44444444-4444-4444-4444-444444444444",
        outcome="granted",
        motion_type="Motion to Compel",
    )

    sql = _rulings_insert_sql(conn)
    # Identity anchors: preserve-first COALESCE.  The existing column wins
    # when it is non-NULL; EXCLUDED only wins when the existing column is
    # NULL (e.g. first-time judge fill-in).
    assert "case_id = COALESCE(rulings.case_id, EXCLUDED.case_id)" in sql
    assert "judge_id = COALESCE(rulings.judge_id, EXCLUDED.judge_id)" in sql
    # Correctable facts: incoming-wins COALESCE.  A non-NULL incoming value
    # legitimately replaces the stored value, but an incoming NULL
    # preserves what's there.
    assert "hearing_date = COALESCE(EXCLUDED.hearing_date, rulings.hearing_date)" in sql
    assert "outcome = COALESCE(EXCLUDED.outcome, rulings.outcome)" in sql
    assert "motion_type = COALESCE(EXCLUDED.motion_type, rulings.motion_type)" in sql
    assert "department = COALESCE(EXCLUDED.department, rulings.department)" in sql


def test_insert_ruling_force_update_overrides_coalesce() -> None:
    """force_update=True switches structured fields to direct EXCLUDED.* overwrite."""
    conn = _make_ruling_upsert_conn()

    insert_ruling(
        conn,
        document_id="11111111-1111-1111-1111-111111111111",
        case_id="22222222-2222-2222-2222-222222222222",
        court_id="33333333-3333-3333-3333-333333333333",
        hearing_date=None,  # reingest wants to clear a bad hearing_date
        ruling_text="Motion granted",
        department=None,
        judge_id=None,  # reingest wants to clear a hallucinated judge
        outcome="granted",
        motion_type="Motion to Compel",
        force_update=True,
    )

    sql = _rulings_insert_sql(conn)
    # Structured fields use direct EXCLUDED.* (no COALESCE wrapper).
    assert "case_id = EXCLUDED.case_id" in sql
    assert "COALESCE(EXCLUDED.case_id" not in sql
    assert "judge_id = EXCLUDED.judge_id" in sql
    assert "COALESCE(EXCLUDED.judge_id" not in sql
    assert "hearing_date = EXCLUDED.hearing_date" in sql
    assert "COALESCE(EXCLUDED.hearing_date" not in sql
    assert "outcome = EXCLUDED.outcome" in sql
    assert "COALESCE(EXCLUDED.outcome" not in sql
    assert "motion_type = EXCLUDED.motion_type" in sql
    assert "COALESCE(EXCLUDED.motion_type" not in sql
    assert "department = EXCLUDED.department" in sql
    assert "COALESCE(EXCLUDED.department" not in sql


def test_insert_ruling_force_update_preserves_ruling_text_coalesce() -> None:
    """Even under force_update, ruling_text/html/summary keep COALESCE.

    A failed re-extraction must never erase a good ruling text — that
    would be destructive without any way to recover.  force_update only
    overrides structured fields (case_id, judge_id, hearing_date,
    outcome, motion_type, department).
    """
    conn = _make_ruling_upsert_conn()

    insert_ruling(
        conn,
        document_id="11111111-1111-1111-1111-111111111111",
        case_id="22222222-2222-2222-2222-222222222222",
        court_id="33333333-3333-3333-3333-333333333333",
        hearing_date=None,
        ruling_text=None,
        department=None,
        force_update=True,
    )

    sql = _rulings_insert_sql(conn)
    assert "ruling_text = COALESCE(EXCLUDED.ruling_text, rulings.ruling_text)" in sql
    assert "ruling_text_html = COALESCE(" in sql
    assert "summary = COALESCE(EXCLUDED.summary, rulings.summary)" in sql


def test_insert_document_default_hearing_date_uses_coalesce() -> None:
    """Default insert_document keeps COALESCE on hearing_date."""
    conn = MagicMock(spec=psycopg.Connection)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)
    conn.cursor.return_value = cur

    insert_document(
        conn,
        document_id="11111111-1111-1111-1111-111111111111",
        case_id="22222222-2222-2222-2222-222222222222",
        court_id="33333333-3333-3333-3333-333333333333",
        content_format="pdf",
        content_hash="abc123",
        s3_key="s3/key.pdf",
        s3_bucket="bucket",
        source_url="https://example.com/doc.pdf",
        scraper_id="test.scraper",
        captured_at=datetime(2026, 4, 1, 12, 0, 0),
        hearing_date=date(2026, 4, 15),
    )

    sql = cur.execute.call_args.args[0]
    assert "INSERT INTO documents" in sql
    assert "hearing_date = COALESCE(EXCLUDED.hearing_date, documents.hearing_date)" in sql


def test_insert_document_force_update_overrides_hearing_date_coalesce() -> None:
    """force_update=True switches hearing_date to direct EXCLUDED assignment."""
    conn = MagicMock(spec=psycopg.Connection)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)
    conn.cursor.return_value = cur

    insert_document(
        conn,
        document_id="11111111-1111-1111-1111-111111111111",
        case_id="22222222-2222-2222-2222-222222222222",
        court_id="33333333-3333-3333-3333-333333333333",
        content_format="pdf",
        content_hash="abc123",
        s3_key="s3/key.pdf",
        s3_bucket="bucket",
        source_url="https://example.com/doc.pdf",
        scraper_id="test.scraper",
        captured_at=datetime(2026, 4, 1, 12, 0, 0),
        hearing_date=None,  # reingest wants to clear a bad hearing_date
        force_update=True,
    )

    sql = cur.execute.call_args.args[0]
    assert "INSERT INTO documents" in sql
    assert "hearing_date = EXCLUDED.hearing_date" in sql
    assert "COALESCE(EXCLUDED.hearing_date" not in sql


def test_insert_document_and_ruling_force_update_threads_through() -> None:
    """force_update=True passed to insert_document_and_ruling threads to both calls."""
    with (
        patch("ingestion.db.insert_document") as mock_doc,
        patch("ingestion.db.insert_ruling") as mock_ruling,
    ):
        mock_doc.return_value = True
        conn = MagicMock(spec=psycopg.Connection)
        insert_document_and_ruling(
            conn,
            document_id="11111111-1111-1111-1111-111111111111",
            case_id="22222222-2222-2222-2222-222222222222",
            court_id="33333333-3333-3333-3333-333333333333",
            content_format="pdf",
            content_hash="abc123",
            s3_key="s3/key.pdf",
            s3_bucket="bucket",
            source_url="https://example.com/doc.pdf",
            scraper_id="test.scraper",
            captured_at=datetime(2026, 4, 1, 12, 0, 0),
            hearing_date=None,
            force_update=True,
        )

    doc_kwargs = mock_doc.call_args.kwargs
    ruling_kwargs = mock_ruling.call_args.kwargs
    assert doc_kwargs.get("force_update") is True
    assert ruling_kwargs.get("force_update") is True


def test_insert_document_and_ruling_default_keeps_coalesce() -> None:
    """Default insert_document_and_ruling passes force_update=False to both."""
    with (
        patch("ingestion.db.insert_document") as mock_doc,
        patch("ingestion.db.insert_ruling") as mock_ruling,
    ):
        mock_doc.return_value = True
        conn = MagicMock(spec=psycopg.Connection)
        insert_document_and_ruling(
            conn,
            document_id="11111111-1111-1111-1111-111111111111",
            case_id="22222222-2222-2222-2222-222222222222",
            court_id="33333333-3333-3333-3333-333333333333",
            content_format="pdf",
            content_hash="abc123",
            s3_key="s3/key.pdf",
            s3_bucket="bucket",
            source_url="https://example.com/doc.pdf",
            scraper_id="test.scraper",
            captured_at=datetime(2026, 4, 1, 12, 0, 0),
            hearing_date=date(2026, 4, 15),
        )

    doc_kwargs = mock_doc.call_args.kwargs
    ruling_kwargs = mock_ruling.call_args.kwargs
    assert doc_kwargs.get("force_update") is False
    assert ruling_kwargs.get("force_update") is False


def _make_content_hash_fallback_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (conn, cursor) mock that raises UniqueViolation on the INSERT.

    The raised UniqueViolation mimics the ``uq_rulings_case_text_hash``
    constraint violation that triggers the supersede-the-loser path (#2458).
    """
    conn = MagicMock(spec=psycopg.Connection)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    # Raise UniqueViolation on the INSERT (second execute — first is SAVEPOINT).
    call_index = [0]

    def execute_side_effect(sql: str, *args: object, **kwargs: object) -> None:
        call_index[0] += 1
        # SAVEPOINT -> INSERT -> ROLLBACK -> DELETE FROM rulings -> UPDATE documents
        if call_index[0] == 2 and "INSERT INTO rulings" in sql:
            exc = psycopg.errors.UniqueViolation("duplicate key")
            raise exc

    cur.execute.side_effect = execute_side_effect
    cur.rowcount = 1
    conn.cursor.return_value = cur
    return conn, cur


def _supersede_stmts(cur: MagicMock) -> list[tuple[str, tuple[object, ...]]]:
    """Return (sql, params) tuples for the supersede-path calls.

    The supersede path runs exactly two statements after ROLLBACK TO
    SAVEPOINT: a ``DELETE FROM rulings WHERE document_id = %s::uuid`` and
    an ``UPDATE documents SET status = 'superseded' WHERE id = %s::uuid``.
    """
    stmts: list[tuple[str, tuple[object, ...]]] = []
    for call in cur.execute.call_args_list:
        sql = call.args[0] if call.args else ""
        params = call.args[1] if len(call.args) > 1 else ()
        if "DELETE FROM rulings WHERE document_id" in sql:
            stmts.append((sql, params))
        elif "UPDATE documents SET status = 'superseded'" in sql:
            stmts.append((sql, params))
    return stmts


def test_insert_ruling_content_hash_collision_supersedes_loser_default() -> None:
    """On ``uq_rulings_case_text_hash`` violation, the losing document is
    superseded — no content-hash UPDATE runs on the winner (#2458)."""
    conn, cur = _make_content_hash_fallback_conn()

    losing_doc_id = "11111111-1111-1111-1111-111111111111"
    insert_ruling(
        conn,
        document_id=losing_doc_id,
        case_id="22222222-2222-2222-2222-222222222222",
        court_id="33333333-3333-3333-3333-333333333333",
        hearing_date=date(2026, 4, 1),
        ruling_text="Motion granted.",
        department="C-10",
        judge_id="44444444-4444-4444-4444-444444444444",
        outcome="granted",
        motion_type="Motion to Compel",
    )

    supersede_stmts = _supersede_stmts(cur)
    # Exactly one DELETE + one UPDATE, both scoped to the losing document_id.
    assert len(supersede_stmts) == 2
    delete_sql, delete_params = supersede_stmts[0]
    update_sql, update_params = supersede_stmts[1]
    assert "DELETE FROM rulings WHERE document_id" in delete_sql
    assert delete_params == (losing_doc_id,)
    assert "UPDATE documents SET status = 'superseded'" in update_sql
    assert update_params == (losing_doc_id,)
    # The old buggy fallback UPDATE that targeted (case_id, ruling_text_hash)
    # must not run — it silently mutated the winner's row (#2458).
    all_sql = [call.args[0] if call.args else "" for call in cur.execute.call_args_list]
    assert not any("UPDATE rulings SET" in s and "ruling_text_hash" in s for s in all_sql)


def test_insert_ruling_content_hash_collision_supersedes_loser_force_update() -> None:
    """``force_update=True`` (reingest path) also supersedes the loser —
    the supersede decision does not depend on ``force_update`` (#2458)."""
    conn, cur = _make_content_hash_fallback_conn()

    losing_doc_id = "11111111-1111-1111-1111-111111111111"
    insert_ruling(
        conn,
        document_id=losing_doc_id,
        case_id="22222222-2222-2222-2222-222222222222",
        court_id="33333333-3333-3333-3333-333333333333",
        hearing_date=None,
        ruling_text="Motion granted.",
        department=None,
        judge_id=None,
        outcome="granted",
        motion_type="Motion to Compel",
        force_update=True,
    )

    supersede_stmts = _supersede_stmts(cur)
    assert len(supersede_stmts) == 2
    _, delete_params = supersede_stmts[0]
    _, update_params = supersede_stmts[1]
    assert delete_params == (losing_doc_id,)
    assert update_params == (losing_doc_id,)
    # No UPDATE rulings SET of any form should run — the supersede path
    # must not touch the winner's ruling row.
    all_sql = [call.args[0] if call.args else "" for call in cur.execute.call_args_list]
    assert not any("UPDATE rulings SET" in s for s in all_sql)
