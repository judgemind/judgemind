"""Tests for the reingest_from_s3 script.

Verifies keyset (cursor-based) pagination, parallel S3 fetching,
worker delegation via IngestionWorker.process_event(), error handling,
and CLI flag behavior. All database, S3, and worker access is mocked.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)

# Ensure the scraper-framework src is importable (needed for auto-discovery)
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC_DIR))

reingest = importlib.import_module("reingest_from_s3")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COURT_ID = uuid.uuid4()
_CASE_ID = uuid.uuid4()
_DOC_ID_1 = uuid.uuid4()
_DOC_ID_2 = uuid.uuid4()
_HEARING_DATE = date(2026, 3, 5)
_CAPTURED_AT_1 = datetime(2026, 3, 1, 10, 0, 0)
_CAPTURED_AT_2 = datetime(2026, 3, 2, 12, 0, 0)


def _make_document_row(
    doc_id: uuid.UUID = _DOC_ID_1,
    captured_at: datetime = _CAPTURED_AT_1,
    *,
    s3_key: str | None = "docs/test.html",
    s3_bucket: str | None = "test-bucket",
    scraper_id: str = "ca-la-tentatives-civil",
    case_number: str = "24STCV12345",
    case_title: str = "Smith v. Jones",
    hearing_date: date | None = _HEARING_DATE,
    ruling_hearing_date: date | None = _HEARING_DATE,
    stored_ruling_text: str | None = None,
) -> tuple:
    """Return a tuple matching the FETCH_DOCUMENTS_QUERY columns."""
    return (
        doc_id,  # d.id
        _CASE_ID,  # d.case_id
        _COURT_ID,  # d.court_id
        s3_key,  # d.s3_key
        s3_bucket,  # d.s3_bucket
        "abc123",  # d.content_hash
        "https://court.example.com/ruling",  # d.source_url
        scraper_id,  # d.scraper_id
        captured_at,  # d.captured_at
        hearing_date,  # d.hearing_date
        "html",  # d.format
        "CA",  # ct.state
        "Los Angeles",  # ct.county
        "Los Angeles Superior Court",  # ct.court_name
        case_number,  # c.case_number
        case_title,  # c.case_title
        ruling_hearing_date,  # ruling_hearing_date (subquery)
        stored_ruling_text,  # stored_ruling_text (subquery)
    )


def _mock_cursor_context(cur: MagicMock) -> MagicMock:
    """Create a context manager mock wrapping the given cursor."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _mock_conn_returning(rows: list) -> MagicMock:
    """Create a mock connection whose first cursor returns the given rows."""
    conn = MagicMock()
    cur_fetch = MagicMock()
    cur_fetch.fetchall.return_value = rows

    # All subsequent cursors are generic mocks
    call_count = 0

    def cursor_ctx() -> MagicMock:
        nonlocal call_count
        ctx = MagicMock()
        if call_count == 0:
            ctx.__enter__ = MagicMock(return_value=cur_fetch)
        else:
            cur = MagicMock()
            ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        call_count += 1
        return ctx

    conn.cursor.side_effect = cursor_ctx
    return conn


def _mock_s3_client(content: bytes = b"<html>ruling text</html>") -> MagicMock:
    """Create a mock S3 client that returns the given content."""
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = content
    s3.get_object.return_value = {"Body": body}
    return s3


_DEFAULT_CURSOR = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)


def _make_batch_result(
    processed: int = 0,
    updated: int = 0,
    next_cursor: tuple[datetime, str] = _DEFAULT_CURSOR,
    failed: int = 0,
    skipped: int = 0,
    batch_number: int = 0,
) -> dict:
    """Create a mock reingest_batch return dict."""
    return {
        "processed": processed,
        "updated": updated,
        "next_cursor": next_cursor,
        "failed": failed,
        "skipped": skipped,
        "batch_number": batch_number,
    }


# ---------------------------------------------------------------------------
# FETCH_DOCUMENTS_QUERY tests
# ---------------------------------------------------------------------------


class TestFetchDocumentsQuery:
    """Verify the FETCH_DOCUMENTS_QUERY constant exists and has expected shape."""

    def test_query_has_filters_placeholder(self) -> None:
        assert "{filters}" in reingest.FETCH_DOCUMENTS_QUERY

    def test_query_has_keyset_pagination(self) -> None:
        assert "(d.captured_at, d.id) >" in reingest.FETCH_DOCUMENTS_QUERY

    def test_query_has_order_by(self) -> None:
        assert "ORDER BY d.captured_at, d.id" in reingest.FETCH_DOCUMENTS_QUERY


# ---------------------------------------------------------------------------
# _fetch_s3_content tests
# ---------------------------------------------------------------------------


class TestFetchS3Content:
    """Tests for _fetch_s3_content."""

    def test_fetches_content(self) -> None:
        s3 = _mock_s3_client(b"ruling text")
        content = reingest._fetch_s3_content(s3, "bucket", "key")
        assert content == b"ruling text"
        s3.get_object.assert_called_once_with(Bucket="bucket", Key="key")

    def test_raises_on_error(self) -> None:
        s3 = MagicMock()
        s3.get_object.side_effect = Exception("S3 error")
        with pytest.raises(Exception, match="S3 error"):
            reingest._fetch_s3_content(s3, "bucket", "key")


# ---------------------------------------------------------------------------
# _build_reingest_event tests
# ---------------------------------------------------------------------------


class TestBuildReingestEvent:
    """Tests for _build_reingest_event."""

    def test_builds_event_for_html_content(self) -> None:
        doc_meta = {
            "document_id": "doc-123",
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": datetime(2026, 3, 1, 10, 0, 0),
            "content_hash": "abc123",
            "format": "html",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "hearing_date": date(2026, 3, 5),
            "ruling_hearing_date": date(2026, 3, 5),
            "scraper_id": "ca-la-tentatives-civil",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }
        raw_content = b"<html>ruling text</html>"

        event = reingest._build_reingest_event(doc_meta, raw_content)

        assert event["document_id"] == "doc-123"
        assert event["state"] == "CA"
        assert event["county"] == "Los Angeles"
        assert event["court"] == "Los Angeles Superior Court"
        assert event["case_number"] == "24STCV12345"
        assert event["case_title"] == "Smith v. Jones"
        assert event["content_format"] == "html"
        assert event["content_hash"] == "abc123"
        assert event["s3_key"] == "docs/test.html"
        assert event["s3_bucket"] == "test-bucket"
        assert event["scraper_id"] == "ca-la-tentatives-civil"
        assert event["ruling_text"] == "<html>ruling text</html>"
        assert event["hearing_date"] == "2026-03-05"
        assert "capture_timestamp" in event

    def test_builds_event_for_pdf_content(self) -> None:
        doc_meta = {
            "document_id": "doc-456",
            "state": "CA",
            "county": "Orange",
            "court_name": "Orange County Superior Court",
            "source_url": "https://court.example.com/ruling.pdf",
            "captured_at": datetime(2026, 3, 1),
            "content_hash": "def456",
            "format": "pdf",
            "case_number": None,
            "case_title": None,
            "hearing_date": None,
            "ruling_hearing_date": date(2026, 3, 5),
            "scraper_id": "ca-oc-tentatives-civil",
            "s3_key": "docs/test.pdf",
            "s3_bucket": "test-bucket",
        }
        raw_content = b"%PDF-1.4 some binary"

        event = reingest._build_reingest_event(doc_meta, raw_content)

        assert event["content_format"] == "pdf"
        # PDF content is decoded as latin-1
        assert event["ruling_text"] == raw_content.decode("latin-1")
        # hearing_date should fall back to ruling_hearing_date
        assert event["hearing_date"] == "2026-03-05"

    def test_null_hearing_date_when_both_null(self) -> None:
        doc_meta = {
            "document_id": "doc-789",
            "state": "CA",
            "county": "Orange",
            "court_name": "Court",
            "source_url": "",
            "captured_at": datetime(2026, 3, 1),
            "content_hash": "abc",
            "format": "html",
            "case_number": None,
            "case_title": None,
            "hearing_date": None,
            "ruling_hearing_date": None,
            "scraper_id": "test",
            "s3_key": "key",
            "s3_bucket": "bucket",
        }
        event = reingest._build_reingest_event(doc_meta, b"text")
        assert event["hearing_date"] is None

    def test_uses_document_hearing_date_over_ruling(self) -> None:
        doc_meta = {
            "document_id": "doc-aaa",
            "state": "CA",
            "county": "LA",
            "court_name": "Court",
            "source_url": "",
            "captured_at": datetime(2026, 3, 1),
            "content_hash": "abc",
            "format": "html",
            "case_number": None,
            "case_title": None,
            "hearing_date": date(2026, 4, 1),
            "ruling_hearing_date": date(2026, 3, 1),
            "scraper_id": "test",
            "s3_key": "key",
            "s3_bucket": "bucket",
        }
        event = reingest._build_reingest_event(doc_meta, b"text")
        assert event["hearing_date"] == "2026-04-01"


# ---------------------------------------------------------------------------
# reingest_batch tests
# ---------------------------------------------------------------------------


class TestReingestBatchCursor:
    """Test keyset cursor pagination."""

    def test_empty_batch_returns_original_cursor(self) -> None:
        conn = _mock_conn_returning([])
        worker = MagicMock()
        result = reingest.reingest_batch(conn, MagicMock(), worker, 10, _DEFAULT_CURSOR, "", [])
        assert result["processed"] == 0
        assert result["next_cursor"] == _DEFAULT_CURSOR

    def test_cursor_advances_to_last_row(self) -> None:
        rows = [
            _make_document_row(_DOC_ID_1, _CAPTURED_AT_1),
            _make_document_row(_DOC_ID_2, _CAPTURED_AT_2),
        ]
        conn = _mock_conn_returning(rows)
        s3 = _mock_s3_client()
        worker = MagicMock()

        result = reingest.reingest_batch(conn, s3, worker, 10, _DEFAULT_CURSOR, "", [])
        assert result["processed"] == 2
        assert result["next_cursor"] == (_CAPTURED_AT_2, str(_DOC_ID_2))


class TestReingestBatchCallsProcessEvent:
    """Test that reingest_batch delegates to worker.process_event."""

    def test_calls_process_event_per_document(self) -> None:
        rows = [
            _make_document_row(_DOC_ID_1, _CAPTURED_AT_1),
            _make_document_row(_DOC_ID_2, _CAPTURED_AT_2),
        ]
        conn = _mock_conn_returning(rows)
        s3 = _mock_s3_client(b"<html>ruling text</html>")
        worker = MagicMock()

        result = reingest.reingest_batch(conn, s3, worker, 10, _DEFAULT_CURSOR, "", [])
        assert result["processed"] == 2
        assert result["updated"] == 2
        assert worker.process_event.call_count == 2

        # Verify each call gets a dict with the right document_id
        call_args = [c[0][0] for c in worker.process_event.call_args_list]
        doc_ids = {args["document_id"] for args in call_args}
        assert str(_DOC_ID_1) in doc_ids
        assert str(_DOC_ID_2) in doc_ids

    def test_event_has_correct_fields(self) -> None:
        rows = [_make_document_row()]
        conn = _mock_conn_returning(rows)
        s3 = _mock_s3_client(b"<html>ruling text</html>")
        worker = MagicMock()

        reingest.reingest_batch(conn, s3, worker, 10, _DEFAULT_CURSOR, "", [])

        event = worker.process_event.call_args[0][0]
        assert event["document_id"] == str(_DOC_ID_1)
        assert event["state"] == "CA"
        assert event["county"] == "Los Angeles"
        assert event["court"] == "Los Angeles Superior Court"
        assert event["case_number"] == "24STCV12345"
        assert event["case_title"] == "Smith v. Jones"
        assert event["content_format"] == "html"
        assert event["content_hash"] == "abc123"
        assert event["ruling_text"] == "<html>ruling text</html>"
        assert event["hearing_date"] == "2026-03-05"

    def test_dry_run_skips_process_event(self) -> None:
        rows = [_make_document_row()]
        conn = _mock_conn_returning(rows)
        s3 = _mock_s3_client()
        worker = MagicMock()

        result = reingest.reingest_batch(
            conn,
            s3,
            worker,
            10,
            _DEFAULT_CURSOR,
            "",
            [],
            dry_run=True,
        )
        assert result["processed"] == 1
        assert result["updated"] == 0
        worker.process_event.assert_not_called()


class TestReingestBatchErrorHandling:
    """Test error handling in reingest_batch."""

    def test_skips_document_on_s3_fetch_failure(self) -> None:
        rows = [_make_document_row()]
        conn = _mock_conn_returning(rows)
        s3 = MagicMock()
        s3.get_object.side_effect = Exception("S3 down")
        worker = MagicMock()

        result = reingest.reingest_batch(conn, s3, worker, 10, _DEFAULT_CURSOR, "", [])
        assert result["processed"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 1
        worker.process_event.assert_not_called()

    def test_skips_document_with_no_s3_key(self) -> None:
        rows = [_make_document_row(s3_key=None)]
        conn = _mock_conn_returning(rows)
        s3 = MagicMock()
        worker = MagicMock()

        result = reingest.reingest_batch(conn, s3, worker, 10, _DEFAULT_CURSOR, "", [])
        assert result["skipped"] == 1
        worker.process_event.assert_not_called()

    def test_continues_after_process_event_failure(self) -> None:
        rows = [
            _make_document_row(_DOC_ID_1, _CAPTURED_AT_1),
            _make_document_row(_DOC_ID_2, _CAPTURED_AT_2),
        ]
        conn = _mock_conn_returning(rows)
        s3 = _mock_s3_client()
        worker = MagicMock()
        # First call fails, second succeeds
        worker.process_event.side_effect = [Exception("DB error"), None]

        result = reingest.reingest_batch(conn, s3, worker, 10, _DEFAULT_CURSOR, "", [])
        assert result["processed"] == 2
        assert result["updated"] == 1
        assert result["failed"] == 1

    def test_running_totals_in_logging(self) -> None:
        rows = [_make_document_row()]
        conn = _mock_conn_returning(rows)
        s3 = _mock_s3_client()
        worker = MagicMock()

        result = reingest.reingest_batch(
            conn,
            s3,
            worker,
            10,
            _DEFAULT_CURSOR,
            "",
            [],
            running_processed=5,
            running_updated=3,
        )
        assert result["processed"] == 1
        assert result["updated"] == 1


class TestReingestBatchParallel:
    """Test parallel S3 fetching."""

    def test_fetches_s3_content_in_parallel(self) -> None:
        rows = [
            _make_document_row(_DOC_ID_1, _CAPTURED_AT_1),
            _make_document_row(_DOC_ID_2, _CAPTURED_AT_2, s3_key="docs/test2.html"),
        ]
        conn = _mock_conn_returning(rows)
        s3 = _mock_s3_client(b"content")
        worker = MagicMock()

        result = reingest.reingest_batch(
            conn,
            s3,
            worker,
            10,
            _DEFAULT_CURSOR,
            "",
            [],
            concurrency=4,
        )
        assert result["processed"] == 2
        assert result["updated"] == 2
        # S3 should have been called for each document
        assert s3.get_object.call_count == 2


# ---------------------------------------------------------------------------
# _build_filters tests
# ---------------------------------------------------------------------------


class TestBuildFilters:
    """Tests for _build_filters."""

    def test_no_filters(self) -> None:
        clause, params = reingest._build_filters(None, None, None)
        assert clause == ""
        assert params == []

    def test_county_filter(self) -> None:
        clause, params = reingest._build_filters("Los Angeles", None, None)
        assert "ct.county = %s" in clause
        assert "Los Angeles" in params

    def test_date_from_filter(self) -> None:
        clause, params = reingest._build_filters(None, date(2026, 1, 1), None)
        assert "d.captured_at >= %s" in clause
        assert len(params) == 1

    def test_date_to_filter(self) -> None:
        clause, params = reingest._build_filters(None, None, date(2026, 3, 31))
        assert "d.captured_at <= %s" in clause
        assert len(params) == 1

    def test_case_number_like_filter(self) -> None:
        clause, params = reingest._build_filters(None, None, None, case_number_like="UNKNOWN-%%")
        assert "c.case_number LIKE %s" in clause
        assert "UNKNOWN-%%" in params

    def test_case_title_regex_filter(self) -> None:
        clause, params = reingest._build_filters(None, None, None, case_title_regex="vs\\.?\\s*$")
        assert "c.case_title ~ %s" in clause
        assert "vs\\.?\\s*$" in params

    def test_null_motion_type_filter(self) -> None:
        clause, params = reingest._build_filters(None, None, None, null_motion_type=True)
        assert "r.motion_type IS NULL" in clause

    def test_orphaned_only_filter(self) -> None:
        clause, params = reingest._build_filters(None, None, None, orphaned_only=True)
        assert "NOT EXISTS" in clause

    def test_all_filters_combined(self) -> None:
        clause, params = reingest._build_filters(
            "Los Angeles",
            date(2026, 1, 1),
            date(2026, 3, 31),
            case_title_regex="vs",
            null_motion_type=True,
            orphaned_only=True,
            case_number_like="UNKNOWN-%",
        )
        assert len(params) == 5  # county, date_from, date_to, case_number_like, regex


# ---------------------------------------------------------------------------
# run_reingest tests
# ---------------------------------------------------------------------------


class TestRunReingest:
    """Tests for run_reingest."""

    @patch("reingest_from_s3._create_worker")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_runs_batches_until_exhausted(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_worker: MagicMock,
    ) -> None:
        """run_reingest loops batches until batch returns fewer than batch_size."""
        mock_conn = _mock_conn_returning([])
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_create_worker.return_value = MagicMock()

        stats = reingest.run_reingest("postgresql://test", batch_size=10)
        assert stats["total_processed"] == 0
        assert stats["total_updated"] == 0
        assert stats["total_batches"] == 1

    @patch("reingest_from_s3._create_worker")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_passes_no_llm_to_create_worker(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_worker: MagicMock,
    ) -> None:
        mock_conn = _mock_conn_returning([])
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_create_worker.return_value = MagicMock()

        reingest.run_reingest("postgresql://test", no_llm=True)
        mock_create_worker.assert_called_once_with("postgresql://test", no_llm=True)

    @patch("reingest_from_s3._create_worker")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_closes_worker_on_completion(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_worker: MagicMock,
    ) -> None:
        mock_conn = _mock_conn_returning([])
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_worker = MagicMock()
        mock_create_worker.return_value = mock_worker

        reingest.run_reingest("postgresql://test")
        mock_worker.close.assert_called_once()

    @patch("reingest_from_s3._create_worker")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_limit_caps_processing(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_worker: MagicMock,
    ) -> None:
        # Return one row, then empty
        rows = [_make_document_row()]
        call_count = 0

        def mock_cursor_factory() -> MagicMock:
            nonlocal call_count
            ctx = MagicMock()
            cur = MagicMock()
            if call_count == 0:
                cur.fetchall.return_value = rows
            else:
                cur.fetchall.return_value = []
            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            call_count += 1
            return ctx

        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = mock_cursor_factory
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_worker = MagicMock()
        mock_create_worker.return_value = mock_worker

        # S3 client
        mock_boto3.client.return_value = _mock_s3_client()

        stats = reingest.run_reingest("postgresql://test", limit=1, batch_size=10)
        assert stats["total_processed"] <= 1


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLIConcurrencyFlag:
    """Test --concurrency CLI flag."""

    @patch("reingest_from_s3.run_reingest")
    @patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})
    def test_concurrency_passed_through(self, mock_run: MagicMock) -> None:
        mock_run.return_value = {"total_processed": 0, "total_updated": 0}
        with patch("sys.argv", ["reingest_from_s3.py", "--concurrency", "20"]):
            reingest.main()
        _, kwargs = mock_run.call_args
        assert kwargs["concurrency"] == 20


class TestCLINoLLMFlag:
    """Test --no-llm CLI flag."""

    @patch("reingest_from_s3.run_reingest")
    @patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})
    def test_no_llm_passed_through(self, mock_run: MagicMock) -> None:
        mock_run.return_value = {"total_processed": 0, "total_updated": 0}
        with patch("sys.argv", ["reingest_from_s3.py", "--no-llm"]):
            reingest.main()
        _, kwargs = mock_run.call_args
        assert kwargs["no_llm"] is True

    @patch("reingest_from_s3.run_reingest")
    @patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})
    def test_no_llm_default_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = {"total_processed": 0, "total_updated": 0}
        with patch("sys.argv", ["reingest_from_s3.py"]):
            reingest.main()
        _, kwargs = mock_run.call_args
        assert kwargs["no_llm"] is False


# ---------------------------------------------------------------------------
# Cursor min values tests
# ---------------------------------------------------------------------------


class TestCursorMinValues:
    """Verify the minimum cursor values for keyset pagination."""

    def test_cursor_min_timestamp(self) -> None:
        assert reingest._CURSOR_MIN_TIMESTAMP == datetime(1970, 1, 1)

    def test_cursor_min_uuid(self) -> None:
        assert reingest._CURSOR_MIN_UUID == "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Quality metrics tests
# ---------------------------------------------------------------------------


class TestQualityMetrics:
    """Tests for _run_quality_queries and report_metrics integration."""

    def test_run_quality_queries_returns_all_metrics(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (42,)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_cur)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = ctx

        result = reingest._run_quality_queries(mock_conn)

        expected_keys = {
            "truncated_vs_titles",
            "header_merge_titles",
            "null_case_titles",
            "missing_parties",
            "all_caps_titles",
            "short_ruling_text",
            "long_ruling_text",
            "total_rulings",
        }
        assert set(result.keys()) == expected_keys
        for val in result.values():
            assert val == 42

    def test_run_quality_queries_with_county_filter(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (5,)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_cur)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = ctx

        reingest._run_quality_queries(mock_conn, county="Los Angeles")

        for c in mock_cur.execute.call_args_list:
            args = c[0]
            assert len(args) == 2
            assert args[1] == ["Los Angeles"]

    def test_quality_queries_dict_has_correct_keys(self) -> None:
        expected_keys = {
            "truncated_vs_titles",
            "header_merge_titles",
            "null_case_titles",
            "missing_parties",
            "all_caps_titles",
            "short_ruling_text",
            "long_ruling_text",
            "total_rulings",
        }
        assert set(reingest._QUALITY_QUERIES.keys()) == expected_keys

    @patch("reingest_from_s3._create_worker")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_report_metrics_includes_quality_data(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_worker: MagicMock,
    ) -> None:
        """When report_metrics=True, stats include quality_before/after/delta."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_cur.fetchone.return_value = (0,)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_cur)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = ctx

        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_create_worker.return_value = MagicMock()

        stats = reingest.run_reingest(
            "postgresql://test",
            report_metrics=True,
        )

        assert "quality_before" in stats
        assert "quality_after" in stats
        assert "quality_delta" in stats
        assert isinstance(stats["quality_before"], dict)
        assert isinstance(stats["quality_after"], dict)
        assert isinstance(stats["quality_delta"], dict)


# ---------------------------------------------------------------------------
# Checkpoint tests
# ---------------------------------------------------------------------------


class TestWriteCheckpoint:
    """Tests for _write_checkpoint."""

    def test_writes_checkpoint_file(self, tmp_path: Any) -> None:
        cp_path = tmp_path / "checkpoint.json"
        cursor = (datetime(2026, 3, 1, 10, 0, 0), "doc-123")
        stats = {"total_processed": 42, "total_updated": 40}

        reingest._write_checkpoint(cp_path, cursor, stats)

        data = json.loads(cp_path.read_text())
        assert data["version"] == 1
        assert data["cursor"]["captured_at"] == "2026-03-01T10:00:00"
        assert data["cursor"]["document_id"] == "doc-123"
        assert data["stats"]["total_processed"] == 42


class TestReadCheckpoint:
    """Tests for _read_checkpoint."""

    def test_reads_checkpoint_file(self, tmp_path: Any) -> None:
        cp_path = tmp_path / "checkpoint.json"
        cp_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cursor": {
                        "captured_at": "2026-03-01T10:00:00",
                        "document_id": "doc-123",
                    },
                    "stats": {"total_processed": 42},
                }
            )
        )

        cursor, stats = reingest._read_checkpoint(cp_path)
        assert cursor[0] == datetime(2026, 3, 1, 10, 0, 0)
        assert cursor[1] == "doc-123"
        assert stats["total_processed"] == 42

    def test_rejects_wrong_version(self, tmp_path: Any) -> None:
        cp_path = tmp_path / "checkpoint.json"
        cp_path.write_text(
            json.dumps(
                {
                    "version": 999,
                    "cursor": {"captured_at": "2026-03-01", "document_id": "x"},
                    "stats": {},
                }
            )
        )

        with pytest.raises(ValueError, match="Unsupported checkpoint version"):
            reingest._read_checkpoint(cp_path)


class TestRunReingestCheckpoint:
    """Tests for checkpoint integration in run_reingest."""

    @patch("reingest_from_s3._create_worker")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_writes_checkpoint_after_batch(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_worker: MagicMock,
        tmp_path: Any,
    ) -> None:
        mock_conn = _mock_conn_returning([])
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_create_worker.return_value = MagicMock()

        cp_path = str(tmp_path / "checkpoint.json")
        reingest.run_reingest(
            "postgresql://test",
            checkpoint_file=cp_path,
        )
        # Checkpoint should have been written
        from pathlib import Path

        assert Path(cp_path).exists()

    @patch("reingest_from_s3._create_worker")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_dry_run_does_not_write_checkpoint(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_worker: MagicMock,
        tmp_path: Any,
    ) -> None:
        mock_conn = _mock_conn_returning([])
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_create_worker.return_value = MagicMock()

        cp_path = str(tmp_path / "checkpoint.json")
        reingest.run_reingest(
            "postgresql://test",
            checkpoint_file=cp_path,
            dry_run=True,
        )
        from pathlib import Path

        assert not Path(cp_path).exists()


# ---------------------------------------------------------------------------
# Prefix mode tests
# ---------------------------------------------------------------------------


class TestParseS3Key:
    """Tests for _parse_s3_key."""

    def test_valid_key(self) -> None:
        key = "federal/federal/courtlistener/raw/abc123def456.html"
        result = reingest._parse_s3_key(key)
        assert result is not None
        assert result["state"] == "federal"
        assert result["county"] == "federal"
        assert result["court"] == "courtlistener"
        assert result["content_hash"] == "abc123def456"
        assert result["ext"] == "html"

    def test_valid_key_with_underscores(self) -> None:
        key = "ca/los_angeles/los_angeles_superior_court/raw/deadbeef.pdf"
        result = reingest._parse_s3_key(key)
        assert result is not None
        assert result["state"] == "ca"
        assert result["county"] == "los_angeles"
        assert result["court"] == "los_angeles_superior_court"
        assert result["ext"] == "pdf"

    def test_invalid_key_returns_none(self) -> None:
        assert reingest._parse_s3_key("not/a/valid/key") is None
        assert reingest._parse_s3_key("") is None
        assert reingest._parse_s3_key("federal/federal/raw/abc.html") is None

    def test_non_hex_hash_rejected(self) -> None:
        assert reingest._parse_s3_key("ca/la/court/raw/NOTAHEX.html") is None


class TestUnsluggify:
    """Tests for _unsluggify."""

    def test_short_code_uppercased(self) -> None:
        assert reingest._unsluggify("ca") == "CA"
        assert reingest._unsluggify("ny") == "NY"

    def test_slug_to_title_case(self) -> None:
        assert reingest._unsluggify("los_angeles") == "Los Angeles"
        assert reingest._unsluggify("federal") == "Federal"

    def test_single_char(self) -> None:
        assert reingest._unsluggify("a") == "A"


class TestDeriveCourtCode:
    """Tests for _derive_court_code."""

    def test_basic(self) -> None:
        assert reingest._derive_court_code("CA", "Orange") == "ca-orange"

    def test_multi_word(self) -> None:
        assert reingest._derive_court_code("CA", "Los Angeles") == "ca-los-angeles"

    def test_federal(self) -> None:
        assert reingest._derive_court_code("Federal", "Federal") == "federal-federal"


class TestDiscoverCourts:
    """Tests for _discover_courts."""

    def test_discovers_unique_courts(self) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
            "ca/los_angeles/superior/raw/ccc333.pdf",
        ]
        courts = reingest._discover_courts(keys)
        assert len(courts) == 2
        codes = {c["court_code"] for c in courts}
        assert "federal-federal" in codes
        assert "ca-los-angeles" in codes

    def test_deduplicates_by_court_code(self) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        courts = reingest._discover_courts(keys)
        assert len(courts) == 1

    def test_empty_keys(self) -> None:
        assert reingest._discover_courts([]) == []


class TestListS3Keys:
    """Tests for _list_s3_keys."""

    def test_lists_matching_keys(self) -> None:
        s3 = MagicMock()
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "federal/federal/courtlistener/raw/aaa111.html"},
                    {"Key": "federal/federal/courtlistener/raw/bbb222.pdf"},
                    {"Key": "federal/federal/courtlistener/metadata.json"},
                ]
            }
        ]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert len(keys) == 2

    def test_handles_empty_pages(self) -> None:
        s3 = MagicMock()
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert keys == []


class TestBuildPrefixEvent:
    """Tests for _build_prefix_event."""

    def test_builds_event_for_html(self) -> None:
        parsed = {
            "state": "federal",
            "county": "federal",
            "court": "courtlistener",
            "content_hash": "abc123",
            "ext": "html",
        }
        content = b"<html>ruling text</html>"
        event = reingest._build_prefix_event(
            "federal/federal/courtlistener/raw/abc123.html",
            content,
            parsed,
            "test-bucket",
        )
        assert event["state"] == "Federal"
        assert event["county"] == "Federal"
        assert event["content_format"] == "html"
        assert event["ruling_text"] == "<html>ruling text</html>"

    def test_builds_event_for_pdf(self) -> None:
        parsed = {
            "state": "ca",
            "county": "los_angeles",
            "court": "superior",
            "content_hash": "def456",
            "ext": "pdf",
        }
        content = b"%PDF-1.4 some binary"
        event = reingest._build_prefix_event(
            "ca/los_angeles/superior/raw/def456.pdf",
            content,
            parsed,
            "test-bucket",
        )
        assert event["content_format"] == "pdf"
        assert event["ruling_text"] == content.decode("latin-1")


class TestSeedCourts:
    """Tests for _seed_courts."""

    def test_seeds_courts_and_returns_ids(self) -> None:
        court_id = uuid.uuid4()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (court_id,)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        courts = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        result = reingest._seed_courts(conn, courts)
        assert result == {"federal-federal": str(court_id)}
        conn.commit.assert_called_once()


class TestRunReingestFromPrefix:
    """Tests for run_reingest_from_prefix."""

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3.psycopg")
    def test_empty_prefix_returns_zeros(
        self,
        mock_psycopg: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_list.return_value = []
        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="nonexistent/",
        )
        assert stats["total_keys"] == 0
        assert stats["processed"] == 0

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_dry_run_skips_processing(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_list.return_value = [
            "federal/federal/courtlistener/raw/aaa111.html",
        ]
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="federal/",
            dry_run=True,
        )
        assert stats["total_keys"] == 1
        assert stats["processed"] == 0


class TestProcessPrefixDocument:
    """Tests for _process_prefix_document."""

    @patch("reingest_from_s3.hashlib")
    def test_skips_invalid_key(self, mock_hashlib: MagicMock) -> None:
        result = reingest._process_prefix_document(
            "invalid/key",
            "test-bucket",
            "postgresql://test",
            "redis://localhost:6379",
            "",
        )
        assert result == "skip"

    def test_hash_mismatch_returns_error(self) -> None:
        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b"<html>content</html>"
        mock_s3.get_object.return_value = {"Body": body}

        with patch("framework.s3_cache.make_s3_client", return_value=mock_s3):
            result = reingest._process_prefix_document(
                "federal/federal/courtlistener/raw/abc123.html",
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )
        assert result == "error"


# ---------------------------------------------------------------------------
# Schema validation for _QUALITY_QUERIES
# ---------------------------------------------------------------------------

_SCHEMA_SQL_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "api",
    "src",
    "data-access",
    "schema.sql",
)


def _parse_schema_tables(schema_path: str) -> dict[str, set[str]]:
    """Parse schema.sql to extract {table_name: {column_names}}."""
    with open(schema_path, encoding="utf-8") as f:
        sql = f.read()

    tables: dict[str, set[str]] = {}
    create_re = re.compile(
        r"CREATE\s+TABLE\s+(?!staging\.)(?:(?:public|derived|telemetry)\.)?(\w+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE,
    )

    for match in create_re.finditer(sql):
        table_name = match.group(1).lower()
        body = match.group(2)
        columns: set[str] = set()

        for line in body.split("\n"):
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if re.match(r"(?i)(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY)\b", line):
                continue
            col_match = re.match(r"(\w+)\s+", line)
            if col_match:
                col_name = col_match.group(1).lower()
                if col_name in {"constraint", "primary", "unique", "check", "foreign"}:
                    continue
                columns.add(col_name)

        if columns:
            tables[table_name] = columns

    return tables


def _parse_query_references(
    query: str,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Parse a SQL query to extract table aliases and column references."""
    query = " ".join(query.split())
    query = query.replace("{county_filter}", "")

    aliases: dict[str, str] = {}
    clause_keywords = {
        "on",
        "where",
        "left",
        "right",
        "inner",
        "outer",
        "cross",
        "join",
        "group",
        "order",
        "having",
        "limit",
        "and",
        "or",
        "not",
        "set",
    }

    table_re = re.compile(
        r"(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?",
        re.IGNORECASE,
    )
    for m in table_re.finditer(query):
        table_name = m.group(1).lower()
        potential_alias = m.group(2)
        if potential_alias and potential_alias.lower() not in clause_keywords:
            aliases[potential_alias.lower()] = table_name
        else:
            aliases[table_name] = table_name

    col_ref_re = re.compile(r"\b(\w+)\.(\w+)\b")
    column_refs: list[tuple[str, str]] = []
    for m in col_ref_re.finditer(query):
        alias = m.group(1).lower()
        column = m.group(2).lower()
        if alias in aliases:
            column_refs.append((alias, column))

    return aliases, column_refs


class TestQualityQueriesSchemaValidation:
    """Validate that _QUALITY_QUERIES reference real schema objects."""

    @pytest.mark.skipif(
        not os.path.exists(_SCHEMA_SQL_PATH),
        reason="schema.sql not found",
    )
    def test_quality_queries_reference_valid_columns(self) -> None:
        tables = _parse_schema_tables(_SCHEMA_SQL_PATH)
        all_queries = {**reingest._QUALITY_QUERIES}

        errors: list[str] = []
        for name, query in all_queries.items():
            aliases, column_refs = _parse_query_references(query)
            for alias, column in column_refs:
                table_name = aliases.get(alias)
                if table_name and table_name in tables:
                    if column not in tables[table_name]:
                        errors.append(
                            f"Query '{name}': {alias}.{column} "
                            f"not in {table_name} columns: "
                            f"{sorted(tables[table_name])}"
                        )

        if errors:
            pytest.fail("\n".join(errors))
