"""Tests for validation gate integration in the ingestion worker.

Verifies that the IngestionWorker correctly calls the validation gate
between enrichment and DB write, handles all validation outcomes
(pass/flag/fail/error), and files GitHub issues for flag/fail results.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ingestion.worker import IngestionWorker
from validation.gate import ValidationResult

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
        "ruling_text": "The motion for summary judgment is GRANTED. " * 5,
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (mock_conn, mock_cur) pair."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def _make_worker(
    validation_enabled: bool = True,
) -> tuple[IngestionWorker, MagicMock]:
    """Return a worker with mocked dependencies and validation optionally enabled.

    The LLM client is set to None to disable per-field LLM extraction
    (tested separately). Only the validation gate is exercised.
    """
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False

    env_vars = {}
    if validation_enabled:
        env_vars["ENABLE_INGESTION_VALIDATION"] = "true"

    with patch.dict("os.environ", env_vars):
        with patch("ingestion.worker.create_llm_client") as mock_create:
            mock_create.return_value = MagicMock()
            worker = IngestionWorker(
                redis_client=redis_mock,
                pg_dsn="postgresql://localhost/test",
                opensearch_client=os_mock,
                s3_client=s3_mock,
                archive_bucket="test-bucket",
                llm_client=None,
            )

    return worker, os_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# All tests mock _llm_split_document and extract_fields_llm to prevent
# the framework LlmExtractor and per-field LLM extraction from being
# invoked (they use actual LLM clients).  These are tested separately
# in test_llm_extraction_path.py and test_ingestion.py.
_SPLIT_MOCK = "ingestion.worker.IngestionWorker._llm_split_document"
_EXTRACT_LLM_MOCK = "ingestion.worker.extract_fields_llm"


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.validate_document")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_pass_writes_to_db(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_validate: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """On validation pass, the document is written to DB normally."""
    mock_validate.return_value = ValidationResult(
        result="pass",
        reason=None,
        model="test-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=50,
    )

    worker, os_mock = _make_worker(validation_enabled=True)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    worker.process_event(event)

    # validate_document was called
    mock_validate.assert_called_once()
    # Document was committed to DB
    mock_conn.commit.assert_called()
    # Validation result was logged to DB
    mock_insert_validation.assert_called_once()
    call_kwargs = mock_insert_validation.call_args
    assert call_kwargs.kwargs["document_id"] == event["document_id"]
    result = call_kwargs.kwargs["result"]
    assert result.result == "pass"


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.IngestionWorker._file_validation_issue")
@patch("ingestion.worker.validate_document")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_flag_writes_to_db_and_files_issue(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_validate: MagicMock,
    mock_file_issue: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """On validation flag, the document is written to DB AND an issue is filed."""
    mock_validate.return_value = ValidationResult(
        result="flag",
        reason="Case title mismatch",
        model="test-model",
        input_tokens=100,
        output_tokens=30,
        latency_ms=75,
    )

    worker, os_mock = _make_worker(validation_enabled=True)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    worker.process_event(event)

    # Document was still committed to DB (flag doesn't block)
    mock_conn.commit.assert_called()
    # Issue was filed
    mock_file_issue.assert_called_once()
    call_kwargs = mock_file_issue.call_args
    assert call_kwargs.kwargs["result"] == "flag"
    assert call_kwargs.kwargs["reason"] == "Case title mismatch"
    assert call_kwargs.kwargs["county"] == "Los Angeles"


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.IngestionWorker._file_validation_issue")
@patch("ingestion.worker.validate_document")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_fail_skips_db_write(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_validate: MagicMock,
    mock_file_issue: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """On validation fail, the document is NOT written to the main tables."""
    mock_validate.return_value = ValidationResult(
        result="fail",
        reason="Wrong case content assigned",
        model="test-model",
        input_tokens=100,
        output_tokens=30,
        latency_ms=75,
    )

    worker, os_mock = _make_worker(validation_enabled=True)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event()
    worker.process_event(event)

    # The validation result insert should be called (for logging the fail)
    mock_insert_validation.assert_called_once()
    result = mock_insert_validation.call_args.kwargs["result"]
    assert result.result == "fail"

    # Issue was filed
    mock_file_issue.assert_called_once()
    call_kwargs = mock_file_issue.call_args
    assert call_kwargs.kwargs["result"] == "fail"

    # OpenSearch indexing should NOT have happened (write path was skipped)
    os_mock.index.assert_not_called()


@patch("ingestion.worker.validate_document")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_error_treats_as_pass(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_validate: MagicMock,
) -> None:
    """On validation error, the document is written to DB normally."""
    mock_validate.return_value = ValidationResult(
        result="error",
        reason="LLM call returned None",
        model="test-model",
        input_tokens=0,
        output_tokens=0,
        latency_ms=50,
    )

    worker, os_mock = _make_worker(validation_enabled=True)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    worker.process_event(event)

    # Document was still committed to DB (error doesn't block)
    mock_conn.commit.assert_called()


@patch("ingestion.worker.validate_document")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_disabled_skips_validation(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_validate: MagicMock,
) -> None:
    """When ENABLE_INGESTION_VALIDATION is not set, validation is skipped."""
    worker, os_mock = _make_worker(validation_enabled=False)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    worker.process_event(event)

    # validate_document should NOT have been called
    mock_validate.assert_not_called()
    # Document was still committed
    mock_conn.commit.assert_called()


@patch("ingestion.worker.IngestionWorker._file_validation_issue")
@patch("ingestion.worker.validate_document")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_issue_filing_failure_does_not_block_ingestion(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_validate: MagicMock,
    mock_file_issue: MagicMock,
) -> None:
    """Issue filing failures should not block the ingestion pipeline."""
    mock_validate.return_value = ValidationResult(
        result="flag",
        reason="test issue",
        model="test-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=50,
    )
    # Issue filing raises an exception
    mock_file_issue.side_effect = RuntimeError("GitHub API error")

    worker, os_mock = _make_worker(validation_enabled=True)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.rowcount = 1

    event = _make_event()
    # Should not raise despite issue filing failure
    worker.process_event(event)

    # Document was still committed to DB
    mock_conn.commit.assert_called()


@patch("ingestion.worker.batch_upsert_parties")
@patch("ingestion.worker.validate_document")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_called_with_correct_fields(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_validate: MagicMock,
    mock_batch_upsert: MagicMock,
) -> None:
    """validate_document receives the correct extracted fields."""
    mock_validate.return_value = ValidationResult(
        result="pass",
        reason=None,
        model="test-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=50,
    )

    worker, os_mock = _make_worker(validation_enabled=True)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),
        ("case-uuid-1",),
        (True,),
    ]
    mock_cur.rowcount = 1

    event = _make_event(
        case_number="23STCV99999",
        case_title="Doe v. Roe",
        judge_name="Jones, Mary",
        motion_type="demurrer",
        outcome="sustained",
    )
    worker.process_event(event)

    mock_validate.assert_called_once()
    call_kwargs = mock_validate.call_args.kwargs
    assert call_kwargs["case_number"] == "23STCV99999"
    assert call_kwargs["case_title"] == "Doe v. Roe"
    assert call_kwargs["judge_name"] == "Jones, Mary"
    assert call_kwargs["county"] == "Los Angeles"


def test_validation_enabled_no_api_key_disables_validation() -> None:
    """When ENABLE_INGESTION_VALIDATION is set but no API key, validation is disabled."""
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False

    env_vars = {"ENABLE_INGESTION_VALIDATION": "true"}

    with patch.dict("os.environ", env_vars):
        with patch("ingestion.worker.create_llm_client") as mock_create:
            # Return None for all create_llm_client calls (simulating no API key)
            mock_create.return_value = None
            worker = IngestionWorker(
                redis_client=redis_mock,
                pg_dsn="postgresql://localhost/test",
                opensearch_client=os_mock,
                s3_client=s3_mock,
                archive_bucket="test-bucket",
                llm_client=None,
            )

    # Validation should be disabled since create_llm_client returned None
    assert not worker._validation_enabled


@patch("ingestion.worker.file_validation_issue")
def test_file_validation_issue_method_calls_module_function(
    mock_fvi: MagicMock,
) -> None:
    """_file_validation_issue method delegates to the module-level function."""
    mock_fvi.return_value = "https://github.com/issues/1"

    worker, _ = _make_worker(validation_enabled=True)

    worker._file_validation_issue(
        result="flag",
        reason="test reason",
        county="Los Angeles",
        case_number="23STCV12345",
        document_id="doc-123",
        s3_key=None,
        ruling_text_excerpt=None,
        extracted_fields=None,
    )

    mock_fvi.assert_called_once()
    call_kwargs = mock_fvi.call_args.kwargs
    assert call_kwargs["result"] == "flag"
    assert call_kwargs["reason"] == "test reason"
    assert call_kwargs["county"] == "Los Angeles"


@patch("ingestion.worker.file_validation_issue")
def test_file_validation_issue_method_catches_exceptions(
    mock_fvi: MagicMock,
) -> None:
    """_file_validation_issue method catches exceptions from the module function."""
    mock_fvi.side_effect = RuntimeError("API error")

    worker, _ = _make_worker(validation_enabled=True)

    # Should not raise
    worker._file_validation_issue(
        result="flag",
        reason="test reason",
        county="Los Angeles",
        case_number=None,
        document_id="doc-456",
        s3_key=None,
        ruling_text_excerpt=None,
        extracted_fields=None,
    )


def test_close_closes_github_client() -> None:
    """close() should close the httpx github client if it exists."""
    import httpx

    worker, _ = _make_worker(validation_enabled=True)

    # Simulate having created a github client
    mock_client = MagicMock(spec=httpx.Client)
    worker._github_client = mock_client

    worker.close()

    mock_client.close.assert_called_once()
    assert worker._github_client is None


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.IngestionWorker._file_validation_issue")
@patch("ingestion.worker.validate_document")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_validation_fail_db_write_error_still_returns(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_validate: MagicMock,
    mock_file_issue: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """On validation fail, if insert_validation_result raises, the worker still returns."""
    mock_validate.return_value = ValidationResult(
        result="fail",
        reason="Wrong content",
        model="test-model",
        input_tokens=100,
        output_tokens=30,
        latency_ms=75,
    )
    # Make insert_validation_result raise an exception
    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker(validation_enabled=True)

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event()
    # Should not raise despite DB error during validation result insert
    worker.process_event(event)

    # OpenSearch indexing should NOT have happened (fail path)
    os_mock.index.assert_not_called()
