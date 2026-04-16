"""Integration tests for deterministic validation in the ingestion worker.

Verifies that the IngestionWorker correctly runs deterministic validation
rules between enrichment and DB write, handles fail/flag/pass results,
and logs validation results to the DB.

Covers the three spotcheck findings from issue #2329:
- LA ruling with raw HTML (no_html_in_ruling_text -> fail)
- SD ruling with wrong hearing date (hearing_date_in_range -> fail)
- Fresno ruling with concatenated titles (no_concatenated_titles -> flag)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ingestion.worker import IngestionWorker

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


def _make_worker() -> tuple[IngestionWorker, MagicMock]:
    """Return a worker with mocked dependencies.

    The LLM client is set to None to disable per-field LLM extraction.
    LLM validation gate is also disabled so we only test deterministic rules.
    """
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False

    with patch.dict("os.environ", {}):
        with patch("ingestion.worker.create_llm_client") as mock_create:
            mock_create.return_value = None
            worker = IngestionWorker(
                redis_client=redis_mock,
                pg_dsn="postgresql://localhost/test",
                opensearch_client=os_mock,
                s3_client=s3_mock,
                archive_bucket="test-bucket",
                llm_client=None,
            )

    return worker, os_mock


# Mocks to prevent LLM extraction and document splitting paths
_SPLIT_MOCK = "ingestion.worker.IngestionWorker._llm_split_document"
_EXTRACT_LLM_MOCK = "ingestion.worker.extract_fields_llm"


# ---------------------------------------------------------------------------
# Tests: deterministic validation — pass (normal document)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_pass_allows_db_write(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """A normal document passes all deterministic rules and is written to DB."""
    worker, os_mock = _make_worker()

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

    # Document was committed to DB (pass = no blocking)
    mock_conn.commit.assert_called()
    # No deterministic validation result logged (only logged for flag/fail)
    # The insert_validation_result may not be called at all for pass
    # (it depends on whether LLM validation is enabled)


# ---------------------------------------------------------------------------
# Tests: deterministic validation — fail (raw HTML)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_html_skips_db_write(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """LA ruling 65932ec6 — raw HTML triggers no_html_in_ruling_text fail, skipping DB write."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event(
        ruling_text="<html><head><title>LASC</title></head><body><div>ruling</div></body></html>",
    )
    worker.process_event(event)

    # insert_validation_result should be called with a fail result
    mock_insert_validation.assert_called_once()
    call_kwargs = mock_insert_validation.call_args
    result = call_kwargs.kwargs["result"]
    assert result.result == "fail"
    assert result.model == "deterministic"
    assert "raw HTML detected" in (result.reason or "")

    # OpenSearch indexing should NOT have happened (write path was skipped)
    os_mock.index.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: deterministic validation — fail (wrong hearing date)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_wrong_date_skips_db_write(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """SD ruling 5eac1c2d — hearing_date=2003 triggers hearing_date_in_range fail."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event(
        county="San Diego",
        hearing_date="2003-07-15",
        capture_timestamp="2026-03-04T23:00:00",
    )
    worker.process_event(event)

    # insert_validation_result should be called with a fail result
    mock_insert_validation.assert_called_once()
    result = mock_insert_validation.call_args.kwargs["result"]
    assert result.result == "fail"
    assert result.model == "deterministic"
    assert "exceeds" in (result.reason or "")
    assert "2003-07-15" in (result.reason or "")

    # OpenSearch indexing should NOT have happened
    os_mock.index.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: deterministic validation — flag (concatenated titles)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_concatenated_title_still_writes(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Fresno ruling 4647a509 — concatenated titles trigger a flag but still write to DB."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (deterministic flag logging)
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    concatenated_title = (
        "Alpha v. Beta; Gamma v. Delta; Epsilon v. Zeta; Eta v. Theta; Iota v. Kappa"
    )
    event = _make_event(
        county="Fresno",
        case_title=concatenated_title,
    )
    worker.process_event(event)

    # Document was still committed to DB (flag doesn't block)
    assert mock_conn.commit.call_count >= 1

    # insert_validation_result should be called with a flag result
    mock_insert_validation.assert_called()
    # Find the deterministic validation call
    det_calls = [
        c
        for c in mock_insert_validation.call_args_list
        if c.kwargs.get("result") and c.kwargs["result"].model == "deterministic"
    ]
    assert len(det_calls) >= 1
    det_result = det_calls[0].kwargs["result"]
    assert det_result.result == "flag"
    assert "multi-case contamination" in (det_result.reason or "")


# ---------------------------------------------------------------------------
# Tests: deterministic validation — flag (empty ruling text)
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_empty_ruling_text_still_writes(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """Empty ruling text triggers a flag but still writes to DB."""
    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (flag logging)
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(ruling_text="")
    worker.process_event(event)

    # Document was still committed to DB (flag doesn't block)
    assert mock_conn.commit.call_count >= 1


# ---------------------------------------------------------------------------
# Tests: deterministic validation — fail DB logging error does not crash
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_fail_db_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If insert_validation_result raises on a fail, the worker still returns cleanly."""
    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    event = _make_event(
        ruling_text="<html><body>raw html</body></html>",
    )
    # Should not raise despite DB error during validation result insert
    worker.process_event(event)

    # OpenSearch indexing should NOT have happened (fail path)
    os_mock.index.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: deterministic validation — flag DB logging error does not crash
# ---------------------------------------------------------------------------


@patch("ingestion.worker.insert_validation_result")
@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch(_EXTRACT_LLM_MOCK, return_value=None)
@patch(_SPLIT_MOCK, return_value=False)
@patch("ingestion.worker.psycopg")
def test_det_validation_flag_db_error_does_not_crash(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    mock_extract_llm: MagicMock,
    mock_resolve_judge: MagicMock,
    mock_insert_validation: MagicMock,
) -> None:
    """If insert_validation_result raises on a flag, the worker continues to DB write."""
    mock_insert_validation.side_effect = RuntimeError("DB connection lost")

    worker, os_mock = _make_worker()

    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court (flag logging attempt — fails)
        ("court-uuid-1",),  # upsert_court (main flow)
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document: is_new = True
    ]
    mock_cur.rowcount = 1

    event = _make_event(ruling_text="")  # triggers flag for empty text
    # Should not raise despite DB error during flag logging
    worker.process_event(event)
