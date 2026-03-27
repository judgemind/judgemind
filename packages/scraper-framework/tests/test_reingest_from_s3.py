"""Tests for the reingest_from_s3 script.

Verifies keyset (cursor-based) pagination, parallel S3 fetching,
psycopg3 pipeline batching of DB writes, LLM extraction integration,
error handling, and CLI flag behavior. All database and S3 access is mocked.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

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

from ingestion.split_ids import make_split_document_id  # noqa: E402

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

    # All subsequent cursors are generic mocks (for DB writes)
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


def _mock_conn_with_rows(rows: list[tuple]) -> MagicMock:
    """Create a mock connection that returns rows and supports transaction context."""
    conn = _mock_conn_returning(rows)

    # Transaction context manager (savepoints — conn.transaction())
    txn = MagicMock()
    txn.__enter__ = MagicMock(return_value=txn)
    txn.__exit__ = MagicMock(return_value=False)
    conn.transaction.return_value = txn

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
    llm_skipped: int = 0,
    next_cursor: tuple[datetime, str] = _DEFAULT_CURSOR,
    failed: int = 0,
    skipped: int = 0,
    llm_success: int = 0,
    llm_failure: int = 0,
    batch_number: int = 0,
) -> dict:
    """Create a mock reingest_batch return dict."""
    return {
        "processed": processed,
        "updated": updated,
        "llm_skipped": llm_skipped,
        "next_cursor": next_cursor,
        "failed": failed,
        "skipped": skipped,
        "llm_success": llm_success,
        "llm_failure": llm_failure,
        "batch_number": batch_number,
    }


# ---------------------------------------------------------------------------
# FETCH_DOCUMENTS_QUERY tests
# ---------------------------------------------------------------------------


class TestFetchDocumentsQuery:
    """Verify the SQL query uses keyset pagination."""

    def test_query_uses_cursor_not_offset(self) -> None:
        """The query must use (captured_at, id) > (%s, %s), not OFFSET."""
        assert "OFFSET" not in reingest.FETCH_DOCUMENTS_QUERY
        assert "(d.captured_at, d.id) > (%s, %s)" in reingest.FETCH_DOCUMENTS_QUERY

    def test_query_orders_by_captured_at_and_id(self) -> None:
        """ORDER BY must include both captured_at and id for stable pagination."""
        assert "ORDER BY d.captured_at, d.id" in reingest.FETCH_DOCUMENTS_QUERY

    def test_query_has_limit(self) -> None:
        """The query must still use LIMIT for batch sizing."""
        assert "LIMIT %s" in reingest.FETCH_DOCUMENTS_QUERY


# ---------------------------------------------------------------------------
# _fetch_s3_content tests
# ---------------------------------------------------------------------------


class TestFetchS3Content:
    """Tests for the _fetch_s3_content helper."""

    def test_returns_body_bytes(self) -> None:
        s3 = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = b"<html>ruling</html>"
        s3.get_object.return_value = {"Body": body_mock}

        result = reingest._fetch_s3_content(s3, "my-bucket", "my-key")

        s3.get_object.assert_called_once_with(Bucket="my-bucket", Key="my-key")
        assert result == b"<html>ruling</html>"


# ---------------------------------------------------------------------------
# _reparse_document tests — NUL byte stripping
# ---------------------------------------------------------------------------


class TestReparseDocumentNulBytes:
    """Verify that NUL (0x00) bytes are stripped from ruling text."""

    def _doc_meta(self) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-la-tentatives-civil",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }

    @patch.object(reingest, "_load_scraper_registry")
    def test_nul_bytes_stripped_from_raw_decode(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """NUL bytes in raw content are removed after UTF-8 decode."""
        raw = b"ruling\x00text\x00here"
        result = reingest._reparse_document(raw, "unknown-scraper", self._doc_meta())
        assert "\x00" not in result["ruling_text"]
        assert "rulingtexthere" in result["ruling_text"]

    @patch.object(reingest, "_load_scraper_registry")
    def test_nul_bytes_stripped_from_scraper_parse(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """NUL bytes in scraper-parsed ruling text are also removed."""
        raw = b"<html>ruling</html>"
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "parsed\x00ruling\x00text"
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = "Judge Test"
        mock_parsed.outcome = "granted"
        mock_parsed.motion_type = "demurrer"
        mock_parsed.department = "1"
        mock_parsed.parties = []
        mock_parsed.hearing_date = None
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-scraper"] = mock_scraper_cls

        try:
            result = reingest._reparse_document(raw, "test-scraper", self._doc_meta())
            assert "\x00" not in result["ruling_text"]
            assert "parsedrulingtext" in result["ruling_text"]
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-scraper", None)


# ---------------------------------------------------------------------------
# _reparse_document tests — motion_type normalization (#1849)
# ---------------------------------------------------------------------------


class TestReparseDocumentMotionTypeNormalization:
    """Verify that scraper-provided motion_type is normalized via
    normalize_motion_type() to match the behavior of worker.py (#1849)."""

    def _doc_meta(self) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-la-tentatives-civil",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }

    @patch.object(reingest, "_load_scraper_registry")
    def test_title_case_motion_type_normalized(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """A scraper returning 'Motion to Compel' gets normalized to
        'motion_to_compel'."""
        raw = b"<html>ruling</html>"
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "The motion is granted."
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = "Judge Test"
        mock_parsed.outcome = "granted"
        mock_parsed.motion_type = "Motion to Compel"
        mock_parsed.department = "1"
        mock_parsed.parties = []
        mock_parsed.hearing_date = None
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-scraper"] = mock_scraper_cls

        try:
            result = reingest._reparse_document(raw, "test-scraper", self._doc_meta())
            assert result["motion_type"] == "motion_to_compel"
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-scraper", None)

    @patch.object(reingest, "_load_scraper_registry")
    def test_already_normalized_motion_type_passes_through(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """A scraper returning already-normalized 'demurrer' passes through."""
        raw = b"<html>ruling</html>"
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "The demurrer is sustained."
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = "Judge Test"
        mock_parsed.outcome = "sustained"
        mock_parsed.motion_type = "demurrer"
        mock_parsed.department = "1"
        mock_parsed.parties = []
        mock_parsed.hearing_date = None
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-scraper"] = mock_scraper_cls

        try:
            result = reingest._reparse_document(raw, "test-scraper", self._doc_meta())
            assert result["motion_type"] == "demurrer"
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-scraper", None)

    @patch.object(reingest, "_load_scraper_registry")
    def test_unmappable_motion_type_returns_none(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """A scraper returning an unmappable motion type gets normalized to
        None, allowing regex fallback to extract from ruling text."""
        raw = b"<html>ruling</html>"
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "The motion is granted."
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = "Judge Test"
        mock_parsed.outcome = "granted"
        mock_parsed.motion_type = "Some Random Hearing Type"
        mock_parsed.department = "1"
        mock_parsed.parties = []
        mock_parsed.hearing_date = None
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-scraper"] = mock_scraper_cls

        try:
            result = reingest._reparse_document(raw, "test-scraper", self._doc_meta())
            # normalize_motion_type returns None for unmappable values
            assert result["motion_type"] is None
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-scraper", None)

    @patch.object(reingest, "_load_scraper_registry")
    def test_none_motion_type_stays_none(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """A scraper returning None motion_type keeps it as None."""
        raw = b"<html>ruling</html>"
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "The motion is granted."
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = "Judge Test"
        mock_parsed.outcome = "granted"
        mock_parsed.motion_type = None
        mock_parsed.department = "1"
        mock_parsed.parties = []
        mock_parsed.hearing_date = None
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-scraper"] = mock_scraper_cls

        try:
            result = reingest._reparse_document(raw, "test-scraper", self._doc_meta())
            assert result["motion_type"] is None
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-scraper", None)


# ---------------------------------------------------------------------------
# reingest_batch tests — cursor pagination
# ---------------------------------------------------------------------------


class TestReingestBatchCursor:
    """Tests for reingest_batch() cursor-based pagination."""

    def test_no_rows_returns_zero_and_same_cursor(self) -> None:
        """Empty batch returns zero counts and unchanged cursor."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn.cursor.return_value = _mock_cursor_context(cur)

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["processed"] == 0
        assert result["updated"] == 0
        assert result["next_cursor"] == _DEFAULT_CURSOR

    def test_cursor_passed_to_query(self) -> None:
        """The cursor values are passed as parameters to the SQL query."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn.cursor.return_value = _mock_cursor_context(cur)

        cursor_ts = datetime(2026, 3, 1, 10, 0, 0)
        cursor_id = str(uuid.uuid4())
        batch_size = 25

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=batch_size,
            cursor=(cursor_ts, cursor_id),
            filters="",
            filter_params=[],
        )

        call_args = cur.execute.call_args[0]
        params = call_args[1]
        assert params == [cursor_ts, cursor_id, batch_size]

    def test_cursor_with_filters_preserves_filter_params(self) -> None:
        """Filter params come before cursor params in the parameter list."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn.cursor.return_value = _mock_cursor_context(cur)

        cursor_ts = datetime(2026, 3, 1)
        cursor_id = "some-uuid"
        county_param = "Los Angeles"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=(cursor_ts, cursor_id),
            filters="AND ct.county = %s",
            filter_params=[county_param],
        )

        call_args = cur.execute.call_args[0]
        params = call_args[1]
        assert params == [county_param, cursor_ts, cursor_id, 10]

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_returns_last_row_cursor(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """Cursor advances to the last row's (captured_at, id)."""
        row1 = _make_document_row(_DOC_ID_1, _CAPTURED_AT_1)
        row2 = _make_document_row(_DOC_ID_2, _CAPTURED_AT_2)

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row1, row2]

        extra = [_mock_cursor_context(MagicMock()) for _ in range(10)]
        contexts = iter([_mock_cursor_context(cur_fetch)] + extra)
        conn.cursor.side_effect = lambda: next(contexts)

        mock_fetch_s3.return_value = b"ruling text"
        mock_reparse.return_value = {
            "ruling_text": "ruling text",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert result["processed"] == 2
        assert result["next_cursor"] == (_CAPTURED_AT_2, str(_DOC_ID_2))

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_skips_document_without_s3_key(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """Documents missing S3 key are skipped but cursor still advances."""
        row = _make_document_row(s3_key="", s3_bucket="test-bucket")

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]
        conn.cursor.return_value = _mock_cursor_context(cur_fetch)

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert result["processed"] == 1
        assert result["updated"] == 0
        assert result["next_cursor"] == (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_fetch_s3.assert_not_called()


# ---------------------------------------------------------------------------
# reingest_batch tests — per-document DB writes
# ---------------------------------------------------------------------------


class TestReingestBatchDBWrites:
    """Tests for per-document DB writes in reingest_batch()."""

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_dry_run_skips_db_writes(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """In dry-run mode, transaction context is not entered."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert result["processed"] == 1
        assert result["updated"] == 0
        conn.transaction.assert_not_called()

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_transaction_context_used_for_db_writes(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """DB writes happen inside a transaction (savepoint) context."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "The motion is granted.",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "John Smith",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "new-case-id"
        mock_resolve_judge.return_value = "judge-id"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["processed"] == 1
        assert result["updated"] == 1
        conn.transaction.assert_called_once()
        mock_upsert_case.assert_called_once()
        mock_insert_doc_and_ruling.assert_called_once()
        mock_resolve_judge.assert_called_once()
        mock_upsert_cj.assert_called_once()

    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_case_id_flows_through_pipeline(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
    ) -> None:
        """The case_id from upsert_case flows to insert_document_and_ruling."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "The motion is granted.",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "pipeline-case-id"
        mock_resolve_judge.return_value = "pipeline-judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        call_kwargs = mock_insert_doc_and_ruling.call_args[1]
        assert call_kwargs["case_id"] == "pipeline-case-id"
        assert call_kwargs["judge_id"] == "pipeline-judge-id"

        mock_upsert_cj.assert_called_once_with(
            conn,
            "pipeline-case-id",
            "pipeline-judge-id",
            _HEARING_DATE,
        )

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_motion_type_persisted_through_pipeline(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """Extracted motion_type flows to insert_document_and_ruling (#1834)."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>Motion to Compel is GRANTED</html>"
        mock_reparse.return_value = {
            "ruling_text": "Motion to Compel is GRANTED",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "motion_to_compel",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        call_kwargs = mock_insert_doc_and_ruling.call_args[1]
        assert call_kwargs["motion_type"] == "motion_to_compel"
        assert call_kwargs["outcome"] == "granted"

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_null_doc_hearing_date_falls_back_to_ruling_hearing_date(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """When documents.hearing_date is NULL, use ruling hearing_date (#1834).

        OC PDF documents may have NULL hearing_date on the document row
        (the scraper doesn't capture it) while the ruling row has a
        hearing_date from LLM/regex extraction.  Without the fallback,
        insert_document_and_ruling skips the ruling insert because
        hearing_date is None, silently losing extracted fields like
        motion_type.
        """
        row = _make_document_row(hearing_date=None, ruling_hearing_date=_HEARING_DATE)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>Motion to Compel is GRANTED</html>"
        mock_reparse.return_value = {
            "ruling_text": "Motion to Compel is GRANTED",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "motion_to_compel",
            "department": "1",
            "parties": [],
            "hearing_date": None,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["updated"] == 1
        mock_insert_doc_and_ruling.assert_called_once()
        call_kwargs = mock_insert_doc_and_ruling.call_args[1]
        # The ruling's hearing_date should be used as fallback
        assert call_kwargs["hearing_date"] == _HEARING_DATE
        # motion_type should flow through to the DB write
        assert call_kwargs["motion_type"] == "motion_to_compel"

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_null_doc_and_ruling_hearing_date_skips_ruling(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """When both document and ruling hearing_dates are NULL, ruling is still skipped."""
        row = _make_document_row(hearing_date=None, ruling_hearing_date=None)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": None,
        }
        mock_upsert_case.return_value = "case-id"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["updated"] == 1
        mock_insert_doc_and_ruling.assert_called_once()
        call_kwargs = mock_insert_doc_and_ruling.call_args[1]
        # Both are None — insert_document_and_ruling will skip the ruling insert
        assert call_kwargs["hearing_date"] is None


# ---------------------------------------------------------------------------
# reingest_batch tests — per-document commits
# ---------------------------------------------------------------------------


class TestReingestBatchPerDocumentCommit:
    """Tests that reingest_batch commits after each document."""

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_commit_called_per_document(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """conn.commit() is called once per successfully written document."""
        rows = [
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1),
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_2),
        ]
        conn = _mock_conn_with_rows(rows)

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "The motion is granted.",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "John Smith",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["processed"] == 2
        assert result["updated"] == 2
        # One commit per document, not one per batch
        assert conn.commit.call_count == 2

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_failed_document_does_not_commit(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """If a document's DB write fails, that document is not committed
        but prior successful documents remain committed."""
        rows = [
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1),
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_2),
        ]
        conn = _mock_conn_with_rows(rows)

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "The motion is granted.",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "John Smith",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        # Make the transaction context raise on the second document
        call_count = 0
        txn_ok = MagicMock()
        txn_ok.__enter__ = MagicMock(return_value=txn_ok)
        txn_ok.__exit__ = MagicMock(return_value=False)

        txn_fail = MagicMock()
        txn_fail.__enter__ = MagicMock(side_effect=RuntimeError("DB constraint violation"))
        txn_fail.__exit__ = MagicMock(return_value=False)

        def make_txn() -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return txn_fail
            return txn_ok

        conn.transaction.side_effect = make_txn

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["processed"] == 2
        assert result["updated"] == 1
        # Only one commit — the first document succeeded, the second failed
        assert conn.commit.call_count == 1

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_dry_run_does_not_commit(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """Dry-run mode does not call conn.commit() at all."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        conn.commit.assert_not_called()


class TestReingestBatchRunningTotals:
    """Tests that running totals are passed and forwarded correctly."""

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_running_totals_passed_to_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """run_reingest passes cumulative totals to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        cursor_2 = (_CAPTURED_AT_2, str(_DOC_ID_2))

        mock_batch.side_effect = [
            _make_batch_result(processed=10, updated=8, next_cursor=cursor_1),
            _make_batch_result(processed=10, updated=7, next_cursor=cursor_2),
            _make_batch_result(processed=5, updated=3, next_cursor=cursor_2),
        ]

        reingest.run_reingest("postgresql://test", batch_size=10)

        calls = mock_batch.call_args_list
        assert len(calls) == 3
        # First batch: running totals start at 0
        assert calls[0].kwargs.get("running_processed") == 0
        assert calls[0].kwargs.get("running_updated") == 0
        # Second batch: running totals reflect first batch
        assert calls[1].kwargs.get("running_processed") == 10
        assert calls[1].kwargs.get("running_updated") == 8
        # Third batch: running totals reflect first + second batch
        assert calls[2].kwargs.get("running_processed") == 20
        assert calls[2].kwargs.get("running_updated") == 15


# ---------------------------------------------------------------------------
# reingest_batch tests — parallel S3 fetches
# ---------------------------------------------------------------------------


class TestReingestBatchParallel:
    """Tests that reingest_batch fetches S3 objects in parallel."""

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_concurrent_s3_fetches(self, mock_fetch: MagicMock, mock_reparse: MagicMock) -> None:
        """Multiple documents in a batch trigger concurrent S3 fetches."""
        rows = [_make_document_row(uuid.uuid4(), _CAPTURED_AT_1) for _ in range(3)]
        conn = _mock_conn_returning(rows)

        mock_fetch.return_value = b"<html>content</html>"
        mock_reparse.return_value = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=3,
        )

        assert result["processed"] == 3
        assert mock_fetch.call_count == 3

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_failed_s3_fetch_skipped(self, mock_fetch: MagicMock, mock_reparse: MagicMock) -> None:
        """Documents with failed S3 fetches are skipped, others proceed."""
        rows = [_make_document_row(uuid.uuid4(), _CAPTURED_AT_1) for _ in range(3)]
        conn = _mock_conn_returning(rows)

        mock_fetch.side_effect = [
            b"<html>ok1</html>",
            Exception("S3 timeout"),
            b"<html>ok3</html>",
        ]
        mock_reparse.return_value = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=3,
        )

        assert result["processed"] == 3
        assert mock_reparse.call_count == 2

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_no_s3_key_skipped_before_fetch(
        self, mock_fetch: MagicMock, mock_reparse: MagicMock
    ) -> None:
        """Documents without s3_key or s3_bucket skip S3 fetch entirely."""
        rows = [
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1, s3_key=None),
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1, s3_bucket=None),
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1),  # valid
        ]
        conn = _mock_conn_returning(rows)

        mock_fetch.return_value = b"<html>content</html>"
        mock_reparse.return_value = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=3,
        )

        assert result["processed"] == 3
        assert mock_fetch.call_count == 1

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_concurrency_1_works(self, mock_fetch: MagicMock, mock_reparse: MagicMock) -> None:
        """Concurrency of 1 effectively disables parallelism but still works."""
        rows = [_make_document_row(uuid.uuid4(), _CAPTURED_AT_1) for _ in range(2)]
        conn = _mock_conn_returning(rows)

        mock_fetch.return_value = b"<html>content</html>"
        mock_reparse.return_value = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=1,
        )

        assert result["processed"] == 2
        assert mock_fetch.call_count == 2


# ---------------------------------------------------------------------------
# run_reingest tests
# ---------------------------------------------------------------------------


class TestRunReingest:
    """Tests for run_reingest() end-to-end flow."""

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_initial_cursor_uses_minimum_values(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """First call to reingest_batch uses epoch timestamp and nil UUID."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test", batch_size=50)

        first_call = mock_batch.call_args_list[0]
        cursor_arg = first_call[0][3]
        assert cursor_arg == _DEFAULT_CURSOR

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_cursor_advances_between_batches(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Each batch receives the cursor from the previous batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        cursor_2 = (_CAPTURED_AT_2, str(_DOC_ID_2))

        mock_batch.side_effect = [
            _make_batch_result(processed=50, updated=40, next_cursor=cursor_1),
            _make_batch_result(processed=50, updated=30, next_cursor=cursor_2),
            _make_batch_result(processed=10, updated=5, next_cursor=cursor_2),
        ]

        reingest.run_reingest("postgresql://test", batch_size=50)

        calls = mock_batch.call_args_list
        assert calls[0][0][3] == _DEFAULT_CURSOR
        assert calls[1][0][3] == cursor_1
        assert calls[2][0][3] == cursor_2

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_dry_run_rolls_back_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_batch.side_effect = [
            _make_batch_result(processed=50, updated=40, next_cursor=cursor_1),
            _make_batch_result(processed=10, updated=5, next_cursor=cursor_1),
        ]

        stats = reingest.run_reingest("postgresql://test", batch_size=50, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 60
        assert stats["total_updated"] == 45

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_no_batch_level_commit_in_non_dry_run(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """run_reingest no longer calls conn.commit() — per-document commits
        happen inside reingest_batch instead."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_batch.side_effect = [
            _make_batch_result(processed=50, updated=40, next_cursor=cursor_1),
            _make_batch_result(processed=30, updated=20, next_cursor=cursor_1),
        ]

        stats = reingest.run_reingest("postgresql://test", batch_size=50)

        # Commits now happen per-document inside reingest_batch, not here
        mock_conn.commit.assert_not_called()
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 80
        assert stats["total_updated"] == 60

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_batch.return_value = _make_batch_result(processed=30, updated=20, next_cursor=cursor_1)

        stats = reingest.run_reingest("postgresql://test", batch_size=100, limit=30)

        call_args = mock_batch.call_args_list[0]
        assert call_args[0][2] == 30
        assert stats["total_processed"] == 30

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_concurrency_passed_to_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result(processed=5, updated=3)

        reingest.run_reingest(
            "postgresql://test",
            batch_size=50,
            concurrency=20,
        )

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("concurrency") == 20

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_default_concurrency_is_10(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result(processed=5, updated=3)

        reingest.run_reingest("postgresql://test", batch_size=50)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("concurrency") == 10

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_county_filter_passed_through(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test", county="Orange")

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        filter_params_arg = call_args[0][5]
        assert "AND ct.county = %s" in filters_arg
        assert "Orange" in filter_params_arg

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_date_filters_passed_through(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest(
            "postgresql://test",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 3, 1),
        )

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        assert "AND d.captured_at >= %s" in filters_arg
        assert "AND d.captured_at <= %s" in filters_arg

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_case_title_regex_filter_passed_through(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        regex = r"vs\.?\s*$"
        reingest.run_reingest(
            "postgresql://test",
            county="Orange",
            case_title_regex=regex,
        )

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        filter_params_arg = call_args[0][5]
        assert "AND c.case_title ~ %s" in filters_arg
        assert regex in filter_params_arg

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_null_motion_type_filter_passed_through(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest(
            "postgresql://test",
            county="San Diego",
            null_motion_type=True,
        )

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        assert "AND ct.county = %s" in filters_arg
        assert "AND EXISTS" in filters_arg
        assert "r.motion_type IS NULL" in filters_arg

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_orphaned_only_filter_passed_through(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest(
            "postgresql://test",
            county="Riverside",
            orphaned_only=True,
        )

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        assert "AND ct.county = %s" in filters_arg
        assert "AND NOT EXISTS" in filters_arg
        assert "r.document_id = d.id" in filters_arg

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_case_number_like_filter_passed_through(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        pattern = "UNKNOWN-%"
        reingest.run_reingest(
            "postgresql://test",
            county="Orange",
            case_number_like=pattern,
        )

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        filter_params_arg = call_args[0][5]
        assert "AND c.case_number LIKE %s" in filters_arg
        assert pattern in filter_params_arg


# ---------------------------------------------------------------------------
# _build_filters
# ---------------------------------------------------------------------------


class TestBuildFilters:
    """Unit tests for the _build_filters helper."""

    def test_no_filters_returns_empty(self) -> None:
        clauses, params = reingest._build_filters(None, None, None)
        assert clauses == ""
        assert params == []

    def test_county_only(self) -> None:
        clauses, params = reingest._build_filters("Orange", None, None)
        assert "AND ct.county = %s" in clauses
        assert params == ["Orange"]

    def test_case_title_regex_adds_clause(self) -> None:
        regex = r"vs\.?\s*$"
        clauses, params = reingest._build_filters(None, None, None, case_title_regex=regex)
        assert "AND c.case_title ~ %s" in clauses
        assert regex in params

    def test_county_and_case_title_regex_combined(self) -> None:
        regex = r"(?i)(Before the Court|moves the)"
        clauses, params = reingest._build_filters("Orange", None, None, case_title_regex=regex)
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_title ~ %s" in clauses
        assert params == ["Orange", regex]

    def test_null_motion_type_adds_exists_subquery(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, null_motion_type=True)
        assert "AND EXISTS" in clauses
        assert "r.motion_type IS NULL" in clauses
        # No additional params needed for this filter
        assert params == []

    def test_null_motion_type_false_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, null_motion_type=False)
        assert clauses == ""
        assert params == []

    def test_null_motion_type_with_county(self) -> None:
        clauses, params = reingest._build_filters("San Diego", None, None, null_motion_type=True)
        assert "AND ct.county = %s" in clauses
        assert "AND EXISTS" in clauses
        assert "r.motion_type IS NULL" in clauses
        assert params == ["San Diego"]

    def test_null_motion_type_with_county_and_case_title_regex(self) -> None:
        regex = r"vs\.?\s*$"
        clauses, params = reingest._build_filters(
            "San Diego", None, None, case_title_regex=regex, null_motion_type=True
        )
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_title ~ %s" in clauses
        assert "AND EXISTS" in clauses
        assert "r.motion_type IS NULL" in clauses
        assert params == ["San Diego", regex]

    def test_orphaned_only_adds_not_exists_subquery(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, orphaned_only=True)
        assert "AND NOT EXISTS" in clauses
        assert "r.document_id = d.id" in clauses
        assert params == []

    def test_orphaned_only_false_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, orphaned_only=False)
        assert clauses == ""
        assert params == []

    def test_orphaned_only_with_county(self) -> None:
        clauses, params = reingest._build_filters("Riverside", None, None, orphaned_only=True)
        assert "AND ct.county = %s" in clauses
        assert "AND NOT EXISTS" in clauses
        assert "r.document_id = d.id" in clauses
        assert params == ["Riverside"]

    def test_orphaned_only_and_null_motion_type_mutually_exclusive_in_practice(
        self,
    ) -> None:
        """Both flags can be set but produce contradictory logic (no doc can
        have no rulings AND have a ruling with NULL motion_type). Verify both
        clauses are emitted so the query returns an empty set gracefully."""
        clauses, params = reingest._build_filters(
            None, None, None, null_motion_type=True, orphaned_only=True
        )
        assert "AND EXISTS" in clauses
        assert "AND NOT EXISTS" in clauses

    def test_case_number_like_adds_clause(self) -> None:
        pattern = "UNKNOWN-%"
        clauses, params = reingest._build_filters(None, None, None, case_number_like=pattern)
        assert "AND c.case_number LIKE %s" in clauses
        assert pattern in params

    def test_case_number_like_none_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, case_number_like=None)
        assert clauses == ""
        assert params == []

    def test_case_number_like_with_county(self) -> None:
        pattern = "UNKNOWN-%"
        clauses, params = reingest._build_filters("Orange", None, None, case_number_like=pattern)
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_number LIKE %s" in clauses
        assert params == ["Orange", pattern]

    def test_case_number_like_with_case_title_regex(self) -> None:
        """Both case_number_like and case_title_regex can be combined."""
        pattern = "UNKNOWN-%"
        regex = r"(?i)Before the Court"
        clauses, params = reingest._build_filters(
            "Orange",
            None,
            None,
            case_number_like=pattern,
            case_title_regex=regex,
        )
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_number LIKE %s" in clauses
        assert "AND c.case_title ~ %s" in clauses
        assert params == ["Orange", pattern, regex]

    def test_case_number_like_param_order_before_case_title_regex(self) -> None:
        """case_number_like param appears before case_title_regex in the
        param list, matching the clause order in the SQL query."""
        pattern = "UNKNOWN-%"
        regex = r"vs\.?\s*$"
        clauses, params = reingest._build_filters(
            None, None, None, case_number_like=pattern, case_title_regex=regex
        )
        assert params.index(pattern) < params.index(regex)


# ---------------------------------------------------------------------------
# Cursor minimum values
# ---------------------------------------------------------------------------


class TestCursorMinValues:
    """Verify cursor minimum values are defined and appropriate."""

    def test_min_timestamp_is_epoch(self) -> None:
        assert reingest._CURSOR_MIN_TIMESTAMP == datetime(1970, 1, 1)

    def test_min_uuid_is_nil(self) -> None:
        assert reingest._CURSOR_MIN_UUID == "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Parallel parse tests
# ---------------------------------------------------------------------------


class TestParallelParsing:
    """Tests that scraper parsing runs in parallel within a batch."""

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_parse_called_for_each_document(
        self,
        mock_fetch: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """Each document with fetched content gets parsed via _reparse_document."""
        rows = [_make_document_row(uuid.uuid4(), _CAPTURED_AT_1) for _ in range(3)]
        conn = _mock_conn_returning(rows)

        mock_fetch.return_value = b"<html>content</html>"
        mock_reparse.return_value = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            parse_workers=2,
        )

        assert result["processed"] == 3
        assert mock_reparse.call_count == 3

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_parse_failure_skips_document(
        self,
        mock_fetch: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """If _reparse_document raises, that document is skipped."""
        rows = [_make_document_row(uuid.uuid4(), _CAPTURED_AT_1) for _ in range(3)]
        conn = _mock_conn_with_rows(rows)

        mock_fetch.return_value = b"<html>content</html>"

        ok_result = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge X",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_reparse.side_effect = [
            ok_result,
            RuntimeError("pdfplumber crash"),
            ok_result,
        ]

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            parse_workers=1,
        )

        assert result["processed"] == 3
        # Only 2 succeed, so only 2 are written to DB
        assert result["updated"] == 2

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_parse_exception_skips_document(
        self,
        mock_fetch: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """If parsing raises any exception, the document is skipped."""
        rows = [_make_document_row(uuid.uuid4(), _CAPTURED_AT_1) for _ in range(2)]
        conn = _mock_conn_with_rows(rows)

        mock_fetch.return_value = b"<html>content</html>"

        ok_result = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge X",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_reparse.side_effect = [
            ok_result,
            RuntimeError("pdfplumber hung and was killed"),
        ]

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            parse_workers=1,
        )

        assert result["processed"] == 2
        assert result["updated"] == 1

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_parse_workers_passed_to_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """parse_workers is forwarded from run_reingest to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result(processed=5, updated=3)

        reingest.run_reingest(
            "postgresql://test",
            batch_size=50,
            parse_workers=6,
        )

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("parse_workers") == 6

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_parse_timeout_passed_to_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """parse_timeout is forwarded from run_reingest to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result(processed=5, updated=3)

        reingest.run_reingest(
            "postgresql://test",
            batch_size=50,
            parse_timeout=30.0,
        )

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("parse_timeout") == 30.0

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_default_parse_workers_is_4(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Default parse_workers is 4 when not specified."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result(processed=5, updated=3)

        reingest.run_reingest("postgresql://test", batch_size=50)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("parse_workers") == 4

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_default_batch_size_is_25(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Default batch_size is 25."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        # batch_size is the 3rd positional arg (conn, s3, batch_size, ...)
        assert batch_call[0][2] == 25


# ---------------------------------------------------------------------------
# _extract_pdf_text_subprocess tests
# ---------------------------------------------------------------------------


class TestExtractPdfTextSubprocess:
    """Tests for the subprocess-based PDF text extraction."""

    def test_extracts_text_from_real_pdf(self) -> None:
        """Subprocess extraction produces readable text from a real PDF."""
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        pdf_path = os.path.join(fixtures_dir, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        result = reingest._extract_pdf_text_subprocess(pdf_bytes, timeout=30.0)
        assert result is not None
        assert len(result) > 100
        assert "%PDF" not in result

    def test_returns_none_on_invalid_pdf(self) -> None:
        """Invalid PDF bytes return None (subprocess exits non-zero)."""
        result = reingest._extract_pdf_text_subprocess(b"not a real pdf", timeout=5.0)
        assert result is None

    def test_returns_none_on_timeout(self) -> None:
        """A very short timeout returns None without hanging."""
        # Use a tiny timeout that the subprocess can't possibly meet
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        pdf_path = os.path.join(fixtures_dir, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        # 0.001s timeout — subprocess won't even start pdfplumber in time
        result = reingest._extract_pdf_text_subprocess(pdf_bytes, timeout=0.001)
        # Should return None (timeout), not hang
        assert result is None


# ---------------------------------------------------------------------------
# CLI argument tests
# ---------------------------------------------------------------------------


class TestCLIConcurrencyFlag:
    """Tests that --concurrency CLI flag is properly parsed."""

    def test_parser_has_concurrency_arg(self) -> None:
        """The argument parser accepts --concurrency."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--county", type=str, default=None)
        parser.add_argument("--date-from", type=str, default=None)
        parser.add_argument("--date-to", type=str, default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--concurrency", type=int, default=10)

        args = parser.parse_args(["--concurrency", "20"])
        assert args.concurrency == 20

    def test_parser_default_concurrency(self) -> None:
        """Default concurrency is 10 when not specified."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--concurrency", type=int, default=10)
        args = parser.parse_args([])
        assert args.concurrency == 10


class TestCLIParseWorkersFlag:
    """Tests that --parse-workers and --parse-timeout CLI flags are parsed."""

    def test_parser_has_parse_workers_arg(self) -> None:
        """The argument parser accepts --parse-workers."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--parse-workers", type=int, default=4)
        args = parser.parse_args(["--parse-workers", "6"])
        assert args.parse_workers == 6

    def test_parser_default_parse_workers(self) -> None:
        """Default parse-workers is 4."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--parse-workers", type=int, default=4)
        args = parser.parse_args([])
        assert args.parse_workers == 4

    def test_parser_has_parse_timeout_arg(self) -> None:
        """The argument parser accepts --parse-timeout."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--parse-timeout", type=float, default=60.0)
        args = parser.parse_args(["--parse-timeout", "30"])
        assert args.parse_timeout == 30.0

    def test_parser_default_parse_timeout(self) -> None:
        """Default parse-timeout is 60.0."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--parse-timeout", type=float, default=60.0)
        args = parser.parse_args([])
        assert args.parse_timeout == 60.0


# ---------------------------------------------------------------------------
# Scraper registry auto-discovery tests
# ---------------------------------------------------------------------------


class TestScraperRegistryAutoDiscovery:
    """Tests that _load_scraper_registry() auto-discovers scrapers."""

    def setup_method(self) -> None:
        """Clear the registry before each test so _load_scraper_registry re-runs."""
        reingest._SCRAPER_REGISTRY.clear()

    def test_registry_is_non_empty(self) -> None:
        """Auto-discovery should find at least one scraper."""
        reingest._load_scraper_registry()
        assert len(reingest._SCRAPER_REGISTRY) > 0

    def test_registry_contains_all_known_scrapers(self) -> None:
        """Registry should contain all scraper modules that have default_config().

        Auto-discovers expected scraper IDs rather than maintaining a hardcoded
        set, so adding a new scraper never requires updating this test (#680).
        """
        import inspect
        import pkgutil

        import courts
        from framework.base import BaseScraper

        # Build expected set by walking the courts package — same discovery
        # strategy as _load_scraper_registry() itself.
        expected_ids: set[str] = set()
        for _importer, modname, ispkg in pkgutil.walk_packages(courts.__path__, prefix="courts."):
            if ispkg:
                continue
            try:
                mod = importlib.import_module(modname)
            except Exception:  # noqa: BLE001
                continue
            config_fn = getattr(mod, "default_config", None)
            if config_fn is None or not callable(config_fn):
                continue
            # Verify there is a concrete BaseScraper subclass in the module.
            has_scraper = any(
                inspect.isclass(obj)
                and issubclass(obj, BaseScraper)
                and obj is not BaseScraper
                and obj.__module__ == mod.__name__
                for _name, obj in inspect.getmembers(mod, inspect.isclass)
            )
            if not has_scraper:
                continue
            expected_ids.add(config_fn().scraper_id)

        reingest._load_scraper_registry()
        assert expected_ids == set(reingest._SCRAPER_REGISTRY.keys())


# ---------------------------------------------------------------------------
# _extract_text_from_content tests
# ---------------------------------------------------------------------------


class TestExtractTextFromContent:
    """Tests for the _extract_text_from_content helper."""

    def test_html_decoded_as_utf8(self) -> None:
        """HTML content is decoded as UTF-8."""
        html = b"<html><body>Hello world</body></html>"
        result = reingest._extract_text_from_content(html, "html")
        assert "Hello world" in result

    def test_pdf_extracted_via_subprocess(self) -> None:
        """PDF content is extracted via pdfplumber subprocess."""
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        pdf_path = os.path.join(fixtures_dir, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        result = reingest._extract_text_from_content(pdf_bytes, "pdf")
        # pdfplumber should extract readable text
        assert len(result) > 100
        # Should NOT contain PDF binary garbage
        assert "%PDF" not in result
        # Should contain actual ruling text
        assert "TENTATIVE" in result.upper() or "DEPT" in result.upper()

    def test_pdf_fallback_to_utf8_on_invalid_pdf(self) -> None:
        """Invalid PDF bytes fall back to UTF-8 decode."""
        fake_pdf = b"not a real pdf"
        result = reingest._extract_text_from_content(fake_pdf, "pdf")
        assert result == "not a real pdf"

    def test_pdf_ocr_fallback_for_image_only_pdf(self) -> None:
        """Image-only PDF uses OCR fallback instead of lossy UTF-8 decode."""
        # Simulate a valid PDF where pdfplumber returns no text (image-only).
        # The subprocess returns None, then extract_text_from_pdf (OCR) is called.
        fake_pdf = b"%PDF-1.4 fake image-only pdf content"
        ocr_text = "OCR extracted text from court document"

        with (
            patch.object(
                reingest,
                "_extract_pdf_text_subprocess",
                return_value=None,
            ),
            patch(
                "reingest_from_s3.extract_text_from_pdf",
                return_value=ocr_text,
            ) as mock_ocr,
        ):
            result = reingest._extract_text_from_content(fake_pdf, "pdf")

        # Should use OCR result, not garbled UTF-8 decode
        assert result == ocr_text
        mock_ocr.assert_called_once_with(fake_pdf)

    def test_pdf_falls_back_to_utf8_when_ocr_also_fails(self) -> None:
        """When both pdfplumber and OCR fail, falls back to UTF-8 decode."""
        fake_pdf = b"not a real pdf"

        with (
            patch.object(
                reingest,
                "_extract_pdf_text_subprocess",
                return_value=None,
            ),
            patch(
                "reingest_from_s3.extract_text_from_pdf",
                return_value=None,
            ),
        ):
            result = reingest._extract_text_from_content(fake_pdf, "pdf")

        # Should fall back to UTF-8 decode
        assert result == "not a real pdf"

    def test_unknown_format_decoded_as_utf8(self) -> None:
        """Unknown format is decoded as UTF-8."""
        content = b"some text content"
        result = reingest._extract_text_from_content(content, "text")
        assert result == "some text content"


class TestIsRealCaseNumber:
    """Tests for the _is_real_case_number helper."""

    def test_none_is_not_real(self) -> None:
        assert reingest._is_real_case_number(None) is False

    def test_empty_string_is_not_real(self) -> None:
        assert reingest._is_real_case_number("") is False

    def test_unknown_prefix_is_not_real(self) -> None:
        assert reingest._is_real_case_number("UNKNOWN-107555aa-80bf") is False

    def test_real_case_number(self) -> None:
        assert reingest._is_real_case_number("25PR199782") is True

    def test_cv_case_number(self) -> None:
        assert reingest._is_real_case_number("24CV123456") is True


# ---------------------------------------------------------------------------
# _reparse_document tests — PdfLinkScraper subclasses
# ---------------------------------------------------------------------------


class TestReparsePdfDocuments:
    """Tests that _reparse_document correctly handles PDF scraper types.

    PdfLinkScraper subclasses (OC, SB, Riverside, SF) must be instantiated
    correctly during reingest and produce proper parse output from real PDFs.
    """

    _FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

    @staticmethod
    def _make_pdf_doc_meta(
        scraper_id: str,
        county: str,
        doc_format: str = "pdf",
    ) -> dict:
        return {
            "document_id": "test-pdf-123",
            "state": "CA",
            "county": county,
            "court_name": "Superior Court",
            "source_url": "https://example.com/ruling.pdf",
            "captured_at": datetime(2026, 3, 5, 10, 0, 0),
            "content_hash": "abc123",
            "format": doc_format,
            "case_number": None,
            "case_title": None,
            "hearing_date": None,
        }

    def test_oc_civil_reparse_extracts_text(self) -> None:
        """OC civil PDF scraper extracts ruling text via pdfplumber."""
        pdf_path = os.path.join(self._FIXTURES_DIR, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        doc_meta = self._make_pdf_doc_meta("ca-oc-tentatives-civil", "Orange")
        result = reingest._reparse_document(pdf_bytes, "ca-oc-tentatives-civil", doc_meta)

        # Should have extracted meaningful text, not garbage
        assert result["ruling_text"] is not None
        assert len(result["ruling_text"]) > 100
        assert "%PDF" not in result["ruling_text"]

    def test_sb_reparse_extracts_judge(self) -> None:
        """SB PDF scraper extracts judge name from PDF text."""
        pdf_path = os.path.join(self._FIXTURES_DIR, "sb_r12_20260303_0df41117.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        doc_meta = self._make_pdf_doc_meta("ca-sb-tentatives-civil", "San Bernardino")
        result = reingest._reparse_document(pdf_bytes, "ca-sb-tentatives-civil", doc_meta)

        # SB scraper extracts judge name from PDF text header
        assert result["judge_name"] is not None
        assert len(result["judge_name"]) > 2

    def test_sf_reparse_extracts_text(self) -> None:
        """SF PDF scraper extracts ruling text."""
        pdf_path = os.path.join(self._FIXTURES_DIR, "sf_dept403_ruling.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        doc_meta = self._make_pdf_doc_meta("ca-sf-tentatives-family-law", "San Francisco")
        result = reingest._reparse_document(pdf_bytes, "ca-sf-tentatives-family-law", doc_meta)

        assert result["ruling_text"] is not None
        assert len(result["ruling_text"]) > 100

    def test_riverside_reparse_extracts_text(self) -> None:
        """Riverside PDF scraper extracts ruling text."""
        pdf_path = os.path.join(self._FIXTURES_DIR, "riv_ps1.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        doc_meta = self._make_pdf_doc_meta("ca-riverside-tentatives-civil", "Riverside")
        result = reingest._reparse_document(pdf_bytes, "ca-riverside-tentatives-civil", doc_meta)

        assert result["ruling_text"] is not None
        assert len(result["ruling_text"]) > 100

    def test_pdf_fallback_extracts_text_when_no_scraper(self) -> None:
        """When no scraper is registered, PDF text is still extracted via pdfplumber."""
        pdf_path = os.path.join(self._FIXTURES_DIR, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        doc_meta = self._make_pdf_doc_meta("unknown-scraper-id", "Test")
        result = reingest._reparse_document(pdf_bytes, "unknown-scraper-id", doc_meta)

        # Even without a scraper, the text should be properly extracted
        assert result["ruling_text"] is not None
        assert len(result["ruling_text"]) > 100
        assert "%PDF" not in result["ruling_text"]

    def test_non_pdf_scraper_still_works(self) -> None:
        """LA (non-PDF) scraper still works with HTML content."""
        html = b"<html><body>Motion is GRANTED. Judge Smith presiding.</body></html>"
        doc_meta = self._make_pdf_doc_meta(
            "ca-la-tentatives-civil", "Los Angeles", doc_format="html"
        )
        result = reingest._reparse_document(html, "ca-la-tentatives-civil", doc_meta)

        assert result["ruling_text"] is not None
        assert "GRANTED" in result["ruling_text"]

    def test_registry_values_are_basescraper_subclasses(self) -> None:
        """Every value in the registry should be a BaseScraper subclass."""
        from framework.base import BaseScraper

        reingest._load_scraper_registry()
        for scraper_id, cls in reingest._SCRAPER_REGISTRY.items():
            assert issubclass(cls, BaseScraper), (
                f"{scraper_id} maps to {cls} which is not a BaseScraper subclass"
            )

    def test_registry_keys_match_default_config_scraper_id(self) -> None:
        """Each registry key should match the scraper_id from default_config()."""
        reingest._load_scraper_registry()
        for scraper_id, cls in reingest._SCRAPER_REGISTRY.items():
            # Find the module that defines this class and call default_config()
            import importlib

            mod = importlib.import_module(cls.__module__)
            config = mod.default_config()
            assert config.scraper_id == scraper_id

    def test_idempotent_load(self) -> None:
        """Calling _load_scraper_registry() twice does not duplicate entries."""
        reingest._load_scraper_registry()
        count_first = len(reingest._SCRAPER_REGISTRY)
        reingest._load_scraper_registry()
        count_second = len(reingest._SCRAPER_REGISTRY)
        assert count_first == count_second

    def test_no_hardcoded_imports_in_load_function(self) -> None:
        """The _load_scraper_registry function should not contain hardcoded court imports."""
        import inspect

        source = inspect.getsource(reingest._load_scraper_registry)
        # Should not have direct imports like "from courts.ca.la_tentatives import ..."
        assert "from courts.ca." not in source
        # Should use auto-discovery via pkgutil or importlib
        assert "pkgutil" in source or "importlib" in source


# ---------------------------------------------------------------------------
# _match_ruling tests
# ---------------------------------------------------------------------------


class TestMatchRuling:
    """Tests for the _match_ruling helper."""

    def test_returns_none_for_empty_rulings(self) -> None:
        """Returns None when there are no rulings."""
        from ingestion.llm_extract import LLMExtractionResult

        result = LLMExtractionResult(rulings=[])
        assert reingest._match_ruling(result, "24STCV12345") is None

    def test_returns_matching_ruling_by_case_number(self) -> None:
        """Returns the ruling matching the given case number."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        r1 = LLMRulingResult(case_number="24STCV11111", outcome="granted")
        r2 = LLMRulingResult(case_number="24STCV22222", outcome="denied")
        result = LLMExtractionResult(rulings=[r1, r2])
        matched = reingest._match_ruling(result, "24STCV22222")
        assert matched is r2

    def test_returns_first_ruling_when_no_match(self) -> None:
        """Falls back to the first ruling when no case number matches."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        r1 = LLMRulingResult(case_number="24STCV11111", outcome="granted")
        r2 = LLMRulingResult(case_number="24STCV22222", outcome="denied")
        result = LLMExtractionResult(rulings=[r1, r2])
        matched = reingest._match_ruling(result, "NO-MATCH")
        assert matched is r1

    def test_returns_first_ruling_when_no_case_number(self) -> None:
        """Falls back to first ruling when case_number is None."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        r1 = LLMRulingResult(case_number="24STCV11111", outcome="granted")
        result = LLMExtractionResult(rulings=[r1])
        matched = reingest._match_ruling(result, None)
        assert matched is r1


# ---------------------------------------------------------------------------
# LLM extraction integration in _reparse_document
# ---------------------------------------------------------------------------


class TestReparseDocumentLLM:
    """Tests for LLM extraction integration in _reparse_document."""

    def _doc_meta(self) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": None,
            "case_title": None,
            "hearing_date": None,
            "court_id": str(_COURT_ID),
            "scraper_id": "unknown-scraper",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_fills_missing_fields(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """LLM extraction fills fields that scraper did not provide."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        mock_llm.return_value = LLMExtractionResult(
            judge_name="Jane Doe",
            hearing_date=date(2026, 3, 5),
            department="12",
            case_count=1,
            rulings=[
                LLMRulingResult(
                    case_number="24STCV99999",
                    case_title="Doe v. Smith",
                    outcome="granted",
                    motion_type="msj",
                    parties=[
                        {"name": "Jane Doe", "role": "plaintiff"},
                        {"name": "John Smith", "role": "defendant"},
                    ],
                )
            ],
        )

        raw = b"<html>ruling text with motion is granted</html>"
        client = MagicMock()
        result = reingest._reparse_document(
            raw, "unknown-scraper", self._doc_meta(), llm_client=client
        )

        assert result["judge_name"] == "Jane Doe"
        assert result["hearing_date"] == date(2026, 3, 5)
        assert result["case_number"] == "24STCV99999"
        assert result["case_title"] == "Doe v. Smith"
        assert result["outcome"] == "granted"
        assert result["motion_type"] == "msj"
        assert result["department"] == "12"
        assert len(result["parties"]) == 2
        assert result["extraction_methods"]["judge_name"] == "llm"
        assert result["extraction_methods"]["outcome"] == "llm"

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_motion_type_normalized(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """LLM-provided title-case motion_type is normalized to snake_case (#1849)."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        mock_llm.return_value = LLMExtractionResult(
            judge_name="Jane Doe",
            hearing_date=date(2026, 3, 5),
            department="12",
            case_count=1,
            rulings=[
                LLMRulingResult(
                    case_number="24STCV99999",
                    case_title="Doe v. Smith",
                    outcome="granted",
                    motion_type="Motion to Compel",  # title case from LLM
                    parties=[],
                )
            ],
        )

        raw = b"<html>ruling text with motion is granted</html>"
        client = MagicMock()
        result = reingest._reparse_document(
            raw, "unknown-scraper", self._doc_meta(), llm_client=client
        )

        assert result["motion_type"] == "motion_to_compel"
        assert result["extraction_methods"]["motion_type"] == "llm"

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_unmappable_motion_type_not_stored(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """LLM-provided unmappable motion_type is not stored (#1849).

        When normalize_motion_type returns None for an LLM value, the
        motion_type field should remain unfilled so regex fallback can
        attempt extraction from the ruling text.
        """
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        mock_llm.return_value = LLMExtractionResult(
            judge_name="Jane Doe",
            hearing_date=date(2026, 3, 5),
            department="12",
            case_count=1,
            rulings=[
                LLMRulingResult(
                    case_number="24STCV99999",
                    case_title="Doe v. Smith",
                    outcome="granted",
                    motion_type="Some Random Hearing Type",  # unmappable
                    parties=[],
                )
            ],
        )

        raw = b"<html>ruling text with motion is granted</html>"
        client = MagicMock()
        result = reingest._reparse_document(
            raw, "unknown-scraper", self._doc_meta(), llm_client=client
        )

        # normalize_motion_type returns None for unmappable values,
        # so the field should not be set by the LLM path.
        # It may still be filled by regex fallback from ruling text.
        assert (
            "motion_type" not in result.get("extraction_methods", {})
            or result["extraction_methods"].get("motion_type") != "llm"
        )

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_not_called_without_client(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """LLM extraction is skipped when llm_client is None."""
        raw = b"<html>ruling text</html>"
        reingest._reparse_document(raw, "unknown-scraper", self._doc_meta())

        mock_llm.assert_not_called()

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_not_called_when_all_fields_present(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """LLM is not called when scraper already filled all fields."""
        # Set up scraper to fill everything
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "ruling text"
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = "Judge Test"
        mock_parsed.outcome = "granted"
        mock_parsed.motion_type = "demurrer"
        mock_parsed.department = "1"
        mock_parsed.parties = [{"name": "Smith", "role": "plaintiff"}]
        mock_parsed.hearing_date = date(2026, 3, 5)
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-all-filled"] = mock_scraper_cls

        try:
            meta = self._doc_meta()
            meta["scraper_id"] = "test-all-filled"
            meta["case_type"] = "civil"
            raw = b"<html>ruling text</html>"
            client = MagicMock()
            reingest._reparse_document(raw, "test-all-filled", meta, llm_client=client)
            mock_llm.assert_not_called()
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-all-filled", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_failure_falls_through_to_regex(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When LLM returns None, regex fallback is still used."""
        mock_llm.return_value = None

        raw = b"<html>Motion for Summary Judgment is GRANTED. Judge John Smith presiding.</html>"
        client = MagicMock()
        result = reingest._reparse_document(
            raw, "unknown-scraper", self._doc_meta(), llm_client=client
        )

        # LLM was called but returned None — regex should have been tried
        mock_llm.assert_called_once()
        # extraction_methods should use regex for whatever regex found,
        # except case_type which may be derived from motion_type (#1731).
        methods = result["extraction_methods"]
        for field, method in methods.items():
            if field == "case_type":
                assert method in ("regex", "motion_type")
            else:
                assert method == "regex"

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_scraper_fields_not_overridden_by_llm(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Scraper-provided fields are NOT overridden by LLM extraction."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        # Set up scraper to fill judge_name
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "ruling text"
        mock_parsed.case_number = None
        mock_parsed.case_title = None
        mock_parsed.judge_name = "Scraper Judge"
        mock_parsed.outcome = None
        mock_parsed.motion_type = None
        mock_parsed.department = None
        mock_parsed.parties = []
        mock_parsed.hearing_date = None
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-partial"] = mock_scraper_cls

        # LLM would provide a different judge name
        mock_llm.return_value = LLMExtractionResult(
            judge_name="LLM Judge",
            hearing_date=date(2026, 3, 5),
            rulings=[
                LLMRulingResult(
                    case_number="24STCV99999",
                    outcome="denied",
                )
            ],
        )

        try:
            meta = self._doc_meta()
            meta["scraper_id"] = "test-partial"
            raw = b"<html>ruling text</html>"
            client = MagicMock()
            result = reingest._reparse_document(raw, "test-partial", meta, llm_client=client)

            # Scraper judge should be preserved, LLM should not override
            assert result["judge_name"] == "Scraper Judge"
            assert result["extraction_methods"]["judge_name"] == "scraper"
            # LLM should fill missing fields
            assert result["case_number"] == "24STCV99999"
            assert result["extraction_methods"]["case_number"] == "llm"
            assert result["hearing_date"] == date(2026, 3, 5)
            assert result["extraction_methods"]["hearing_date"] == "llm"
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-partial", None)

    @patch.object(reingest, "_load_scraper_registry")
    def test_extraction_methods_in_result(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """extraction_methods dict is included in the returned result."""
        raw = b"<html>Motion is GRANTED</html>"
        result = reingest._reparse_document(raw, "unknown-scraper", self._doc_meta())
        assert "extraction_methods" in result
        assert isinstance(result["extraction_methods"], dict)


# ---------------------------------------------------------------------------
# case_type from motion_type fallback (#1731)
# ---------------------------------------------------------------------------


class TestReparseDocumentCaseTypeFromMotionType:
    """Tests for the motion_type -> case_type fallback in _reparse_document."""

    def _doc_meta(
        self,
        case_number: str | None = None,
        case_type: str | None = None,
    ) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Ventura",
            "court_name": "Ventura Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": case_number,
            "case_title": None,
            "hearing_date": None,
            "court_id": str(_COURT_ID),
            "scraper_id": "unknown-scraper",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
            "case_type": case_type,
        }

    @patch.object(reingest, "_load_scraper_registry")
    def test_case_type_derived_from_motion_type(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """case_type is derived from motion_type when case number has no type prefix."""
        # Ventura all-digit case number has no type prefix, so
        # extract_case_type_from_number returns None.  The regex
        # extracts motion_to_compel from the text, and the new
        # fallback should derive case_type = "civil".
        raw = b"<html>Motion to Compel is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "unknown-scraper",
            self._doc_meta(case_number="202300574258"),
        )

        assert result["motion_type"] == "motion_to_compel"
        assert result["case_type"] == "civil"

    @patch.object(reingest, "_load_scraper_registry")
    def test_extraction_methods_records_motion_type(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """extraction_methods records 'motion_type' for case_type when fallback is used."""
        raw = b"<html>Motion to Compel is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "unknown-scraper",
            self._doc_meta(case_number="202300574258"),
        )

        assert result["extraction_methods"]["case_type"] == "motion_type"

    @patch.object(reingest, "_load_scraper_registry")
    def test_petition_derives_probate(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """petition_for_probate motion_type derives case_type = 'probate'."""
        raw = b"<html>Petition for letters of administration</html>"
        result = reingest._reparse_document(
            raw,
            "unknown-scraper",
            self._doc_meta(case_number="25HR054887C"),
        )

        assert result["motion_type"] == "petition_for_probate"
        assert result["case_type"] == "probate"
        assert result["extraction_methods"]["case_type"] == "motion_type"

    @patch.object(reingest, "_load_scraper_registry")
    def test_case_number_prefix_takes_priority(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """case_type from case number prefix is NOT overridden by motion_type fallback."""
        # CVRI2502741 has "CV" prefix -> "civil" from case number.
        # The motion_type fallback should not overwrite it.
        raw = b"<html>Motion to Compel is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "unknown-scraper",
            self._doc_meta(case_number="CVRI2502741"),
        )

        assert result["case_type"] == "civil"
        # Should be "regex" (from case number), not "motion_type"
        assert result["extraction_methods"]["case_type"] == "regex"

    @patch.object(reingest, "_load_scraper_registry")
    def test_no_fallback_when_motion_type_ambiguous(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """case_type remains None when motion_type is ambiguous (e.g. ex_parte)."""
        raw = b"<html>Ex Parte Application for temporary restraining order</html>"
        result = reingest._reparse_document(
            raw,
            "unknown-scraper",
            self._doc_meta(case_number="202300574258"),
        )

        assert result["motion_type"] == "ex_parte_application"
        assert result["case_type"] is None

    @patch.object(reingest, "_load_scraper_registry")
    def test_no_fallback_when_case_type_from_metadata(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """case_type from doc metadata is NOT overridden by motion_type fallback."""
        raw = b"<html>Motion to Compel is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "unknown-scraper",
            self._doc_meta(case_number="202300574258", case_type="family"),
        )

        # Metadata case_type should be preserved
        assert result["case_type"] == "family"
        # The extraction method should NOT be "motion_type" — it should
        # be "scraper" (from the doc metadata) or absent.
        assert result["extraction_methods"].get("case_type") != "motion_type"


# ---------------------------------------------------------------------------
# case_type fallback from scraper_id (#1836)
# ---------------------------------------------------------------------------


class TestReparseDocumentCaseTypeFromScraperId:
    """Tests for extract_case_type_from_scraper_id fallback in _apply_regex_fallbacks."""

    def _doc_meta(
        self,
        case_number: str | None = None,
        case_type: str | None = None,
        scraper_id: str = "ca-oc-tentatives-civil",
    ) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Orange",
            "court_name": "Orange County Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": case_number,
            "case_title": None,
            "hearing_date": None,
            "court_id": str(_COURT_ID),
            "scraper_id": scraper_id,
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
            "case_type": case_type,
        }

    @patch.object(reingest, "_load_scraper_registry")
    def test_case_type_from_scraper_id_civil(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """case_type derived from scraper_id suffix when case number has no type prefix."""
        # No case number at all — scraper_id suffix 'civil' should yield case_type.
        raw = b"<html>The motion is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-oc-tentatives-civil",
            self._doc_meta(case_number=None),
        )

        assert result["case_type"] == "civil"
        assert result["extraction_methods"]["case_type"] == "scraper_id"

    @patch.object(reingest, "_load_scraper_registry")
    def test_case_type_from_scraper_id_probate(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Scraper IDs with 'probate' suffix yield probate case_type."""
        raw = b"<html>Petition is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-oc-tentatives-probate",
            self._doc_meta(
                case_number=None,
                scraper_id="ca-oc-tentatives-probate",
            ),
        )

        assert result["case_type"] == "probate"
        assert result["extraction_methods"]["case_type"] == "scraper_id"

    @patch.object(reingest, "_load_scraper_registry")
    def test_case_number_prefix_takes_priority_over_scraper_id(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """case_type from case number prefix is NOT overridden by scraper_id fallback."""
        # CVPS prefix => "civil" from case number.
        # scraper_id is also civil, but the extraction method should be "regex"
        # (from case number), not "scraper_id".
        raw = b"<html>The motion is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-oc-tentatives-civil",
            self._doc_meta(case_number="CVPS2306157"),
        )

        assert result["case_type"] == "civil"
        assert result["extraction_methods"]["case_type"] == "regex"

    @patch.object(reingest, "_load_scraper_registry")
    def test_scraper_id_takes_priority_over_motion_type(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """scraper_id fallback fires before motion_type fallback."""
        # No case number, scraper_id suffix is "civil".
        # The text also contains a motion that would resolve to "civil" via
        # motion_type fallback — but scraper_id should fire first.
        raw = b"<html>Motion to Compel is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-oc-tentatives-civil",
            self._doc_meta(case_number=None),
        )

        assert result["case_type"] == "civil"
        # Should be "scraper_id", not "motion_type"
        assert result["extraction_methods"]["case_type"] == "scraper_id"

    @patch.object(reingest, "_load_scraper_registry")
    def test_no_fallback_when_case_type_from_metadata(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """case_type from doc metadata is NOT overridden by scraper_id fallback."""
        raw = b"<html>The motion is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-oc-tentatives-civil",
            self._doc_meta(case_number=None, case_type="family"),
        )

        assert result["case_type"] == "family"
        assert result["extraction_methods"].get("case_type") != "scraper_id"

    @patch.object(reingest, "_load_scraper_registry")
    def test_unknown_scraper_id_falls_through_to_motion_type(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Unknown scraper_id falls through to motion_type fallback."""
        raw = b"<html>Motion to Compel is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "unknown-scraper",
            self._doc_meta(
                case_number="202300574258",
                scraper_id="unknown-scraper",
            ),
        )

        assert result["case_type"] == "civil"
        assert result["extraction_methods"]["case_type"] == "motion_type"


# ---------------------------------------------------------------------------
# parties fallback from case title caption (#1836)
# ---------------------------------------------------------------------------


class TestReparseDocumentPartiesFromCaption:
    """Tests for extract_parties_from_caption fallback in _apply_regex_fallbacks."""

    def _doc_meta(
        self,
        case_title: str | None = None,
        case_number: str | None = "24STCV12345",
    ) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": case_number,
            "case_title": case_title,
            "hearing_date": None,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-la-tentatives-civil",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }

    @patch.object(reingest, "_load_scraper_registry")
    def test_parties_extracted_from_case_title(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Parties are extracted from case_title when none are provided."""
        raw = b"<html>The motion is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-la-tentatives-civil",
            self._doc_meta(case_title="Smith v. Jones"),
        )

        assert len(result["parties"]) >= 2
        names = {p["name"] for p in result["parties"]}
        assert "Smith" in names
        assert "Jones" in names
        roles = {p["role"] for p in result["parties"]}
        assert "plaintiff" in roles
        assert "defendant" in roles
        assert result["extraction_methods"]["parties"] == "regex"

    @patch.object(reingest, "_load_scraper_registry")
    def test_parties_not_extracted_when_already_present(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Parties from scraper are NOT overridden by caption fallback."""
        raw = b"<html>The motion is GRANTED</html>"
        meta = self._doc_meta(case_title="Smith v. Jones")

        # Create a mock scraper that provides parties
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "The motion is GRANTED"
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = None
        mock_parsed.outcome = None
        mock_parsed.motion_type = None
        mock_parsed.department = None
        mock_parsed.parties = [{"name": "Alpha Corp", "role": "plaintiff"}]
        mock_parsed.hearing_date = None
        mock_parsed.case_type = None
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["ca-la-tentatives-civil"] = mock_scraper_cls

        try:
            result = reingest._reparse_document(
                raw,
                "ca-la-tentatives-civil",
                meta,
            )
            # Scraper-provided parties should be preserved
            assert len(result["parties"]) == 1
            assert result["parties"][0]["name"] == "Alpha Corp"
        finally:
            reingest._SCRAPER_REGISTRY.pop("ca-la-tentatives-civil", None)

    @patch.object(reingest, "_load_scraper_registry")
    def test_parties_not_extracted_when_no_case_title(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """No parties are extracted when case_title is None."""
        raw = b"<html>The motion is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-la-tentatives-civil",
            self._doc_meta(case_title=None),
        )

        assert result["parties"] == []

    @patch.object(reingest, "_load_scraper_registry")
    def test_parties_from_regex_extracted_case_title(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Parties are extracted even when case_title comes from regex fallback."""
        # No case_title in metadata, but the text contains a parseable caption
        raw = b"<html>Case No. 24STCV99999\nAlpha Corp v. Beta LLC\nThe motion is GRANTED</html>"
        result = reingest._reparse_document(
            raw,
            "ca-la-tentatives-civil",
            self._doc_meta(case_title=None, case_number=None),
        )

        # case_title must have been extracted by regex first
        assert result["case_title"] is not None
        assert "Alpha Corp v. Beta LLC" in result["case_title"]
        assert result["extraction_methods"].get("case_title") == "regex"

        # Then parties should have been extracted from that title
        assert len(result["parties"]) >= 2
        assert result["extraction_methods"].get("parties") == "regex"
        names = {p["name"] for p in result["parties"]}
        assert "Alpha Corp" in names
        assert "Beta LLC" in names


# ---------------------------------------------------------------------------
# Title validation in _apply_regex_fallbacks (#1974)
# ---------------------------------------------------------------------------


class TestReparseDocumentTitleValidation:
    """Tests for is_plausible_case_title() integration in _apply_regex_fallbacks."""

    def _doc_meta(
        self,
        case_title: str | None = None,
        case_number: str | None = "24STCV12345",
    ) -> dict[str, Any]:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": case_number,
            "case_title": case_title,
            "hearing_date": None,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-la-tentatives-civil",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }

    @patch.object(reingest, "_load_scraper_registry")
    def test_implausible_title_rejected(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Titles extracted by regex that fail is_plausible_case_title() are not written."""
        mock_registry.return_value = {}
        # Construct text where extract_case_title would match "To Respond v. Without"
        # but is_plausible_case_title rejects it.  Use a mock to isolate.
        raw = b"<html>RULING: The motion is GRANTED</html>"
        meta = self._doc_meta(case_title=None)
        with patch.object(
            reingest, "extract_case_title", return_value="To Respond, Without Objections"
        ):
            result = reingest._reparse_document(raw, "ca-la-tentatives-civil", meta)
        assert result["case_title"] is None

    @patch.object(reingest, "_load_scraper_registry")
    def test_plausible_title_accepted(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Titles that pass is_plausible_case_title() are written normally."""
        mock_registry.return_value = {}
        raw = b"<html>Smith v. Jones\nThe motion is GRANTED</html>"
        meta = self._doc_meta(case_title=None)
        with patch.object(reingest, "extract_case_title", return_value="Smith v. Jones"):
            result = reingest._reparse_document(raw, "ca-la-tentatives-civil", meta)
        assert result["case_title"] == "Smith v. Jones"

    @patch.object(reingest, "_load_scraper_registry")
    def test_scraper_provided_title_not_revalidated(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Titles from the scraper (already in metadata) bypass regex fallback."""
        mock_registry.return_value = {}
        raw = b"<html>The motion is GRANTED</html>"
        # Scraper already set case_title — regex fallback should NOT overwrite it
        meta = self._doc_meta(case_title="Existing Title")
        result = reingest._reparse_document(raw, "ca-la-tentatives-civil", meta)
        assert result["case_title"] == "Existing Title"


# ---------------------------------------------------------------------------
# scraper_id fallback in split path (#1836)
# ---------------------------------------------------------------------------


class TestFullReparseDocumentScraperIdFallback:
    """Tests for scraper_id fallback in _full_reparse_document split path."""

    def _doc_meta(self, **overrides: Any) -> dict:
        meta = {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Orange",
            "court_name": "Orange County Superior Court",
            "source_url": "https://court.example.com/ruling.pdf",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "pdf",
            "case_number": None,
            "case_title": None,
            "case_type": None,
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-oc-tentatives-civil",
            "s3_key": "docs/test.pdf",
            "s3_bucket": "test-bucket",
        }
        meta.update(overrides)
        return meta

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_split_path_applies_case_type_from_scraper_id(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Split rulings with no case number get case_type from scraper_id (#1836)."""
        from courts.ca.fresno_tentatives import SplitRuling

        # No case numbers at all — scraper_id suffix should provide case_type
        rulings = [
            SplitRuling(1, None, "Ruling text", "Smith v. Jones", "Demurrer", "Granted", None),
            SplitRuling(2, None, "Other ruling", "Doe v. Roe", None, None, None),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["ca-oc-tentatives-civil"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("ca-oc-tentatives-civil", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf",
                "ca-oc-tentatives-civil",
                self._doc_meta(),
            )

            assert len(result) == 2
            # Both should get case_type from scraper_id
            assert result[0]["case_type"] == "civil"
            assert result[0]["extraction_methods"]["case_type"] == "scraper_id"
            assert result[1]["case_type"] == "civil"
            assert result[1]["extraction_methods"]["case_type"] == "scraper_id"
        finally:
            reingest._SPLIT_REGISTRY.pop("ca-oc-tentatives-civil", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_split_path_applies_parties_from_caption(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Split rulings with case titles get parties from caption fallback (#1836)."""
        from courts.ca.fresno_tentatives import SplitRuling

        rulings = [
            SplitRuling(
                1, "CVPS2306157", "Ruling text", "Smith v. Jones", "Demurrer", "Granted", None
            ),
            SplitRuling(2, "FL2301234", "Family ruling", "Doe v. Roe", None, None, None),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-parties-split"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-parties-split", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf",
                "test-parties-split",
                self._doc_meta(scraper_id="test-parties-split"),
            )

            assert len(result) == 2
            # First ruling: "Smith v. Jones" -> parties
            assert len(result[0]["parties"]) >= 2
            names_0 = {p["name"] for p in result[0]["parties"]}
            assert "Smith" in names_0
            assert "Jones" in names_0
            assert result[0]["extraction_methods"]["parties"] == "regex"

            # Second ruling: "Doe v. Roe" -> parties
            assert len(result[1]["parties"]) >= 2
            names_1 = {p["name"] for p in result[1]["parties"]}
            assert "Doe" in names_1
            assert "Roe" in names_1
            assert result[1]["extraction_methods"]["parties"] == "regex"
        finally:
            reingest._SPLIT_REGISTRY.pop("test-parties-split", None)


# ---------------------------------------------------------------------------
# LLM extraction in reingest_batch — llm_client passthrough
# ---------------------------------------------------------------------------


class TestReingestBatchLLM:
    """Tests that reingest_batch passes llm_client through to parsing."""

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_llm_client_passed_to_reparse(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """llm_client is forwarded from reingest_batch to _reparse_document."""
        row = _make_document_row()
        conn = _mock_conn_returning([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
            "extraction_methods": {},
        }

        mock_client = MagicMock()
        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            llm_client=mock_client,
        )

        # Verify _reparse_document was called with the anthropic_client
        call_args = mock_reparse.call_args[0]
        assert call_args[4] is mock_client

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_no_llm_client_by_default(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """By default, llm_client is None (no LLM extraction)."""
        row = _make_document_row()
        conn = _mock_conn_returning([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
            "extraction_methods": {},
        }

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        call_args = mock_reparse.call_args[0]
        assert call_args[4] is None


# ---------------------------------------------------------------------------
# run_reingest — LLM client creation
# ---------------------------------------------------------------------------


class TestRunReingestLLM:
    """Tests for LLM client creation in run_reingest."""

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_no_llm_flag_disables_llm(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """no_llm=True prevents LLM client creation even with API key set."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = _make_batch_result()

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            reingest.run_reingest("postgresql://test", no_llm=True)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("llm_client") is None

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_no_api_key_disables_llm(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Without any LLM API key, llm_client is None."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = _make_batch_result()

        with patch.dict(os.environ, {}, clear=True):
            # Ensure no LLM API keys are set
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("llm_client") is None

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.create_llm_client")
    def test_api_key_creates_client(
        self,
        mock_create_client: MagicMock,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """When create_llm_client() returns a client, it is passed to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = _make_batch_result()

        fake_client = MagicMock()
        mock_create_client.return_value = fake_client

        env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "test-key-123"}
        with patch.dict(os.environ, env):
            reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        client = batch_call.kwargs.get("llm_client")
        assert client is fake_client


# ---------------------------------------------------------------------------
# CLI --no-llm flag tests
# ---------------------------------------------------------------------------


class TestCLINoLLMFlag:
    """Tests that --no-llm CLI flag is properly parsed."""

    def test_parser_has_no_llm_arg(self) -> None:
        """The argument parser accepts --no-llm."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--no-llm", action="store_true")
        args = parser.parse_args(["--no-llm"])
        assert args.no_llm is True

    def test_parser_default_no_llm_is_false(self) -> None:
        """Default --no-llm is False (LLM enabled by default)."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--no-llm", action="store_true")
        args = parser.parse_args([])
        assert args.no_llm is False


# ---------------------------------------------------------------------------
# reingest_batch tests — per-document error handling
# ---------------------------------------------------------------------------


class TestReingestBatchPerDocumentErrorHandling:
    """Verify that a single bad document does not crash the entire batch."""

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_bad_document_skipped_others_succeed(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """When one document raises an exception during DB writes, the batch
        continues and processes the remaining documents."""
        doc_id_bad = uuid.uuid4()
        doc_id_good = uuid.uuid4()
        row_bad = _make_document_row(doc_id=doc_id_bad, captured_at=_CAPTURED_AT_1)
        row_good = _make_document_row(doc_id=doc_id_good, captured_at=_CAPTURED_AT_2)

        conn = _mock_conn_with_rows([row_bad, row_good])
        mock_fetch_s3.return_value = b"<html>text</html>"

        extracted_good = {
            "ruling_text": "Granted.",
            "case_number": "23STCV01234",
            "case_title": "A v. B",
            "judge_name": "Judge Good",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_reparse.return_value = extracted_good
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        # Make the transaction context manager raise for the first call (bad doc)
        # then succeed for the second (good doc)
        call_count = 0

        def transaction_side_effect() -> MagicMock:
            nonlocal call_count
            call_count += 1
            txn = MagicMock()
            if call_count == 1:
                # First doc: raise inside the context
                txn.__enter__ = MagicMock(side_effect=Exception("index row requires 9568 bytes"))
            else:
                txn.__enter__ = MagicMock(return_value=txn)
            txn.__exit__ = MagicMock(return_value=False)
            return txn

        conn.transaction.side_effect = transaction_side_effect

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        # Both documents were processed (fetched + parsed)
        assert result["processed"] == 2
        # Only one was successfully written (the second one)
        assert result["updated"] == 1

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_all_docs_fail_returns_zero_updated(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """When every document fails DB writes, updated count is zero."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])
        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "23STCV01234",
            "case_title": "A v. B",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        # Every transaction raises
        txn = MagicMock()
        txn.__enter__ = MagicMock(side_effect=Exception("DB error"))
        txn.__exit__ = MagicMock(return_value=False)
        conn.transaction.return_value = txn

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["processed"] == 1
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# LLM skip-if-complete and --force-llm tests
# ---------------------------------------------------------------------------


class TestLLMSkipIfComplete:
    """Tests for skipping LLM when all fields are present, and --force-llm."""

    def _doc_meta(self) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": None,
            "case_title": None,
            "hearing_date": None,
            "court_id": str(_COURT_ID),
            "scraper_id": "unknown-scraper",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }

    def _setup_all_fields_scraper(self) -> MagicMock:
        """Register a mock scraper that fills all fields."""
        mock_scraper_cls = MagicMock()
        mock_parsed = MagicMock()
        mock_parsed.ruling_text = "ruling text"
        mock_parsed.case_number = "24STCV12345"
        mock_parsed.case_title = "Smith v. Jones"
        mock_parsed.judge_name = "Judge Test"
        mock_parsed.outcome = "granted"
        mock_parsed.motion_type = "demurrer"
        mock_parsed.department = "1"
        mock_parsed.parties = [{"name": "Smith", "role": "plaintiff"}]
        mock_parsed.hearing_date = date(2026, 3, 5)
        mock_scraper_cls.return_value.parse_document.return_value = mock_parsed
        reingest._SCRAPER_REGISTRY["test-all-filled"] = mock_scraper_cls
        return mock_scraper_cls

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_skipped_flag_set_when_all_fields_present(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Result includes llm_skipped=True when all fields present."""
        self._setup_all_fields_scraper()
        try:
            meta = self._doc_meta()
            meta["scraper_id"] = "test-all-filled"
            meta["case_type"] = "civil"
            raw = b"<html>ruling text</html>"
            client = MagicMock()
            result = reingest._reparse_document(raw, "test-all-filled", meta, llm_client=client)
            assert result["llm_skipped"] is True
            mock_llm.assert_not_called()
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-all-filled", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_force_llm_overrides_skip(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """force_llm=True calls LLM even when all fields are present."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        mock_llm.return_value = LLMExtractionResult(
            judge_name="LLM Judge",
            hearing_date=date(2026, 3, 5),
            department="99",
            case_count=1,
            rulings=[
                LLMRulingResult(
                    case_number="24STCV12345",
                    case_title="LLM Title",
                    outcome="denied",
                    motion_type="summary judgment",
                    parties=[],
                )
            ],
        )
        self._setup_all_fields_scraper()
        try:
            meta = self._doc_meta()
            meta["scraper_id"] = "test-all-filled"
            meta["case_type"] = "civil"
            raw = b"<html>ruling text</html>"
            client = MagicMock()
            result = reingest._reparse_document(
                raw, "test-all-filled", meta, llm_client=client, force_llm=True
            )
            # LLM should have been called
            mock_llm.assert_called_once()
            # llm_skipped should be False
            assert result["llm_skipped"] is False
        finally:
            reingest._SCRAPER_REGISTRY.pop("test-all-filled", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_llm_skipped_false_when_fields_missing(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """llm_skipped is False when some fields are missing."""
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        mock_llm.return_value = LLMExtractionResult(
            judge_name="Jane Doe",
            hearing_date=date(2026, 3, 5),
            department="12",
            case_count=1,
            rulings=[
                LLMRulingResult(
                    case_number="24STCV12345",
                    case_title="A v. B",
                    outcome="granted",
                    motion_type="demurrer",
                    parties=[],
                )
            ],
        )
        raw = b"<html>some ruling text</html>"
        client = MagicMock()
        result = reingest._reparse_document(
            raw, "unknown-scraper", self._doc_meta(), llm_client=client
        )
        assert result["llm_skipped"] is False
        mock_llm.assert_called_once()

    @patch.object(reingest, "_load_scraper_registry")
    def test_llm_skipped_false_without_client(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """llm_skipped is False when no LLM client is provided."""
        raw = b"<html>text</html>"
        result = reingest._reparse_document(raw, "unknown-scraper", self._doc_meta())
        assert result["llm_skipped"] is False

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_batch_counts_llm_skipped(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """reingest_batch returns correct llm_skipped count."""
        row1 = _make_document_row(doc_id=_DOC_ID_1, captured_at=_CAPTURED_AT_1)
        row2 = _make_document_row(doc_id=_DOC_ID_2, captured_at=_CAPTURED_AT_2)
        conn = _mock_conn_returning([row1, row2])

        mock_fetch_s3.return_value = b"<html>text</html>"
        # First doc has all fields (skipped), second does not
        mock_reparse.side_effect = [
            {
                "ruling_text": "text",
                "case_number": "24STCV12345",
                "case_title": "Smith v. Jones",
                "judge_name": "Judge Test",
                "outcome": "granted",
                "motion_type": "demurrer",
                "department": "1",
                "parties": [{"name": "Smith", "role": "plaintiff"}],
                "hearing_date": _HEARING_DATE,
                "extraction_methods": {},
                "llm_skipped": True,
            },
            {
                "ruling_text": "text",
                "case_number": "24STCV99999",
                "case_title": "A v. B",
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "extraction_methods": {},
                "llm_skipped": False,
            },
        ]

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert result["processed"] == 2
        assert result["llm_skipped"] == 1

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.create_llm_client")
    def test_run_reingest_returns_llm_skipped_total(
        self,
        mock_create_client: MagicMock,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """run_reingest returns total_llm_skipped in stats."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        fake_client = MagicMock()
        mock_create_client.return_value = fake_client

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_batch.side_effect = [
            _make_batch_result(processed=50, updated=40, llm_skipped=15, next_cursor=cursor_1),
            _make_batch_result(processed=10, updated=5, llm_skipped=3, next_cursor=cursor_1),
        ]

        env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "test-key"}
        with patch.dict(os.environ, env):
            stats = reingest.run_reingest("postgresql://test", batch_size=50)

        assert stats["total_llm_skipped"] == 18

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.create_llm_client")
    def test_force_llm_passed_to_batch(
        self,
        mock_create_client: MagicMock,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """force_llm=True is forwarded to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        fake_client = MagicMock()
        mock_create_client.return_value = fake_client

        mock_batch.return_value = _make_batch_result()

        env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "test-key"}
        with patch.dict(os.environ, env):
            reingest.run_reingest("postgresql://test", force_llm=True)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("force_llm") is True


class TestCLIForceLLMFlag:
    """Tests that --force-llm CLI flag is properly parsed."""

    def test_parser_has_force_llm_arg(self) -> None:
        """The argument parser accepts --force-llm."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--force-llm", action="store_true")
        args = parser.parse_args(["--force-llm"])
        assert args.force_llm is True

    def test_parser_default_force_llm_is_false(self) -> None:
        """Default --force-llm is False."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--force-llm", action="store_true")
        args = parser.parse_args([])
        assert args.force_llm is False


# ---------------------------------------------------------------------------
# Progress logging tests
# ---------------------------------------------------------------------------


class TestProgressLogging:
    """Tests for structured progress logging features."""

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_summary_stats_aggregation(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """run_reingest aggregates all stats from batch results."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_batch.side_effect = [
            _make_batch_result(
                processed=50,
                updated=40,
                llm_skipped=5,
                failed=2,
                skipped=3,
                llm_success=10,
                llm_failure=1,
                next_cursor=cursor_1,
            ),
            _make_batch_result(
                processed=20,
                updated=15,
                llm_skipped=2,
                failed=1,
                skipped=2,
                llm_success=5,
                llm_failure=0,
                next_cursor=cursor_1,
            ),
        ]

        stats = reingest.run_reingest("postgresql://test", batch_size=50)

        assert stats["total_processed"] == 70
        assert stats["total_updated"] == 55
        assert stats["total_llm_skipped"] == 7
        assert stats["total_failed"] == 3
        assert stats["total_skipped"] == 5
        assert stats["total_batches"] == 2
        assert stats["llm_success"] == 15
        assert stats["llm_failure"] == 1

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_batch_stats_dict_keys(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """run_reingest return dict includes all expected keys."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        stats = reingest.run_reingest("postgresql://test")

        expected_keys = {
            "total_processed",
            "total_updated",
            "total_llm_skipped",
            "total_failed",
            "total_skipped",
            "total_batches",
            "llm_success",
            "llm_failure",
            "wall_time_seconds",
            "input_tokens",
            "output_tokens",
            "llm_api_calls",
            "estimated_cost_usd",
        }
        assert set(stats.keys()) == expected_keys

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_failed_count_tracking(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """Documents with DB write failures increment the failed counter."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])
        conn.transaction.return_value.__enter__.side_effect = RuntimeError("DB error")

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Test",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
            "llm_outcome": "not_attempted",
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["failed"] == 1
        assert result["updated"] == 0

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_skipped_count_tracking(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """Documents without S3 keys increment the skipped counter."""
        rows = [
            _make_document_row(_DOC_ID_1, _CAPTURED_AT_1, s3_key=None),
            _make_document_row(_DOC_ID_2, _CAPTURED_AT_2),
        ]
        conn = _mock_conn_returning(rows)

        mock_fetch_s3.return_value = b"<html>content</html>"
        mock_reparse.return_value = {
            "ruling_text": "content",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
            "llm_outcome": "not_attempted",
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert result["skipped"] == 1

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_llm_stats_tracking(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """LLM success/failure counts are tracked per document."""
        rows = [
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1),
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1),
            _make_document_row(uuid.uuid4(), _CAPTURED_AT_1),
        ]
        conn = _mock_conn_returning(rows)

        mock_fetch_s3.return_value = b"<html>content</html>"
        mock_reparse.side_effect = [
            {
                "ruling_text": "t",
                "case_number": "A",
                "case_title": "B",
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "llm_outcome": "success",
            },
            {
                "ruling_text": "t",
                "case_number": "A",
                "case_title": "B",
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "llm_outcome": "failure",
            },
            {
                "ruling_text": "t",
                "case_number": "A",
                "case_title": "B",
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "llm_outcome": "not_attempted",
            },
        ]

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert result["llm_success"] == 1
        assert result["llm_failure"] == 1

    @patch.object(reingest, "_load_scraper_registry")
    def test_llm_outcome_in_reparse(self, mock_registry: MagicMock) -> None:
        """_reparse_document includes llm_outcome in its return dict."""
        doc_meta = {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": "unknown",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }
        result = reingest._reparse_document(
            b"<html>ruling text</html>", "unknown-scraper", doc_meta
        )
        assert "llm_outcome" in result
        assert result["llm_outcome"] == "not_attempted"

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_batch_number_passthrough(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """run_reingest passes incrementing batch_number to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_batch.side_effect = [
            _make_batch_result(processed=50, updated=40, next_cursor=cursor_1),
            _make_batch_result(processed=10, updated=5, next_cursor=cursor_1),
        ]

        reingest.run_reingest("postgresql://test", batch_size=50)

        calls = mock_batch.call_args_list
        assert calls[0].kwargs.get("batch_number") == 1
        assert calls[1].kwargs.get("batch_number") == 2

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_wall_time(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """run_reingest includes wall_time_seconds in return stats."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        stats = reingest.run_reingest("postgresql://test")

        assert "wall_time_seconds" in stats
        assert isinstance(stats["wall_time_seconds"], float)
        assert stats["wall_time_seconds"] >= 0

    def test_structlog_logger(self) -> None:
        """The module-level logger is a structlog logger."""
        import structlog

        assert hasattr(reingest, "logger")
        assert isinstance(
            reingest.logger,
            (structlog.BoundLogger, structlog._config.BoundLoggerLazyProxy),
        )

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_batch_result_has_batch_number(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """reingest_batch returns batch_number in its result dict."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn.cursor.return_value = _mock_cursor_context(cur)

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            batch_number=42,
        )

        assert result["batch_number"] == 42


# ---------------------------------------------------------------------------
# split document ID unification tests
# ---------------------------------------------------------------------------


class TestSplitDocumentIdUnification:
    """Tests that reingest uses the canonical make_split_document_id from ingestion.split_ids."""

    def test_reingest_uses_split_ids_make_split_document_id(self) -> None:
        """reingest_from_s3 imports make_split_document_id from ingestion.split_ids."""
        assert reingest.make_split_document_id is make_split_document_id

    def test_no_local_make_split_document_id(self) -> None:
        """reingest_from_s3 does not define its own _make_split_document_id."""
        assert not hasattr(reingest, "_make_split_document_id")

    def test_reingest_produces_same_ids_as_worker(self) -> None:
        """IDs from reingest match what the ingestion worker would produce."""
        doc_id = str(uuid.uuid4())
        for idx in range(5):
            worker_id = make_split_document_id(doc_id, idx)
            reingest_id = reingest.make_split_document_id(doc_id, idx)
            assert worker_id == reingest_id


# ---------------------------------------------------------------------------
# _full_reparse_document tests
# ---------------------------------------------------------------------------


class TestFullReparseDocument:
    """Tests for _full_reparse_document()."""

    def _doc_meta(self, **overrides: Any) -> dict:
        meta = {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Riverside",
            "court_name": "Riverside Superior Court",
            "source_url": "https://court.example.com/ruling.pdf",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "pdf",
            "case_number": "CVPS2306157",
            "case_title": None,
            "case_type": None,
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-riverside-tentatives-civil",
            "s3_key": "docs/test.pdf",
            "s3_bucket": "test-bucket",
        }
        meta.update(overrides)
        return meta

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_reparse_document")
    def test_falls_back_to_reparse_without_split_registry(
        self,
        mock_reparse: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When no split function is registered, falls back to _reparse_document."""
        # Clear split registry
        reingest._SPLIT_REGISTRY.clear()
        mock_reparse.return_value = {
            "ruling_text": "some ruling",
            "case_number": "CVPS2306157",
            "case_title": None,
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        result = reingest._full_reparse_document(b"raw pdf", "unknown-scraper", self._doc_meta())

        assert len(result) == 1
        assert result[0]["is_split"] is False
        assert result[0]["ruling_index"] == 0
        assert result[0]["split_document_id"] == str(_DOC_ID_1)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_reparse_document")
    @patch.object(reingest, "_extract_text_from_content")
    def test_falls_back_when_split_returns_single_ruling(
        self,
        mock_extract: MagicMock,
        mock_reparse: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When split function returns 0 or 1 ruling, falls back to standard reparse."""
        mock_split = MagicMock(return_value=[])
        reingest._SPLIT_REGISTRY["test-scraper"] = mock_split
        mock_extract.return_value = "some text"
        mock_reparse.return_value = {
            "ruling_text": "some ruling",
            "case_number": "CVPS2306157",
            "case_title": None,
            "judge_name": None,
            "outcome": None,
            "motion_type": None,
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }

        try:
            result = reingest._full_reparse_document(
                b"raw pdf", "test-scraper", self._doc_meta(scraper_id="test-scraper")
            )
            assert len(result) == 1
            assert result[0]["is_split"] is False
        finally:
            reingest._SPLIT_REGISTRY.pop("test-scraper", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_splits_multi_ruling_document(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When split function returns multiple rulings, creates one dict per ruling."""
        from courts.ca.fresno_tentatives import SplitRuling

        rulings = [
            SplitRuling(
                ruling_index=1,
                case_number="CVPS2306157",
                ruling_text="Ruling 1 text",
                case_title="Yeldell V. Henss",
                motion_type="Demurrer",
                outcome="Granted",
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=2,
                case_number="CVPS2306202",
                ruling_text="Ruling 2 text",
                case_title="Crump V. Irwin",
                motion_type="Motion to Compel",
                outcome="Denied",
                hearing_date=None,
            ),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-split"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-split", None)
        mock_extract.return_value = "full pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf", "test-split", self._doc_meta(scraper_id="test-split")
            )

            assert len(result) == 2

            # First ruling
            assert result[0]["is_split"] is True
            assert result[0]["ruling_index"] == 1
            assert result[0]["case_number"] == "CVPS2306157"
            assert result[0]["ruling_text"] == "Ruling 1 text"
            assert result[0]["case_title"] == "Yeldell V. Henss"
            assert result[0]["motion_type"] == "demurrer"  # normalized (#1849)
            assert result[0]["outcome"] == "granted"

            # Second ruling
            assert result[1]["is_split"] is True
            assert result[1]["ruling_index"] == 2
            assert result[1]["case_number"] == "CVPS2306202"
            assert result[1]["ruling_text"] == "Ruling 2 text"

            # Split document IDs are deterministic and different
            assert result[0]["split_document_id"] != result[1]["split_document_id"]
            # Both are valid UUIDs
            uuid.UUID(result[0]["split_document_id"])
            uuid.UUID(result[1]["split_document_id"])
        finally:
            reingest._SPLIT_REGISTRY.pop("test-split", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_split_ids_are_idempotent(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Running full-reparse twice produces the same split document IDs."""
        from courts.ca.fresno_tentatives import SplitRuling

        rulings = [
            SplitRuling(1, "CVPS001", "text1", None, None, None, None),
            SplitRuling(2, "CVPS002", "text2", None, None, None, None),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-idem"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-idem", None)
        mock_extract.return_value = "pdf text"

        try:
            meta = self._doc_meta(scraper_id="test-idem")
            result1 = reingest._full_reparse_document(b"raw pdf", "test-idem", meta)
            result2 = reingest._full_reparse_document(b"raw pdf", "test-idem", meta)

            assert result1[0]["split_document_id"] == result2[0]["split_document_id"]
            assert result1[1]["split_document_id"] == result2[1]["split_document_id"]
        finally:
            reingest._SPLIT_REGISTRY.pop("test-idem", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_nul_bytes_stripped_from_split_ruling_text(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """NUL bytes in split ruling text are removed."""
        from courts.ca.fresno_tentatives import SplitRuling

        rulings = [
            SplitRuling(1, "CVPS001", "ruling\x00text", None, None, None, None),
            SplitRuling(2, "CVPS002", "clean text", None, None, None, None),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-nul"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-nul", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf", "test-nul", self._doc_meta(scraper_id="test-nul")
            )
            assert "\x00" not in result[0]["ruling_text"]
            assert result[0]["ruling_text"] == "rulingtext"
        finally:
            reingest._SPLIT_REGISTRY.pop("test-nul", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_split_path_applies_case_type_from_number_fallback(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Split rulings with recognisable case number prefix get case_type (#1749)."""
        from courts.ca.fresno_tentatives import SplitRuling

        # CVPS prefix => civil case type
        rulings = [
            SplitRuling(
                1, "CVPS2306157", "Ruling text", "Smith v. Jones", "Demurrer", "Granted", None
            ),
            SplitRuling(2, "FL2301234", "Family ruling", "Doe v. Doe", None, None, None),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-ct-num"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-ct-num", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf",
                "test-ct-num",
                self._doc_meta(scraper_id="test-ct-num", case_type=None),
            )

            assert len(result) == 2
            # CVPS => civil
            assert result[0]["case_type"] == "civil"
            assert result[0]["extraction_methods"]["case_type"] == "regex"
            # FL => family
            assert result[1]["case_type"] == "family"
            assert result[1]["extraction_methods"]["case_type"] == "regex"
        finally:
            reingest._SPLIT_REGISTRY.pop("test-ct-num", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_split_path_applies_case_type_from_motion_type_fallback(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Ventura-style all-digit case numbers fall back to motion_type for case_type (#1749).

        When the case number has no embedded type code (e.g. Ventura's
        ``202300574258``), the case_type should be derived from the
        motion_type via ``extract_case_type_from_motion_type()``.
        """
        from courts.ca.fresno_tentatives import SplitRuling

        # All-digit case numbers that won't match any prefix pattern
        rulings = [
            SplitRuling(1, "202300574258", "Ruling text", "Smith v. Jones", "demurrer", None, None),
            SplitRuling(2, "202300574259", "Other ruling", "Doe v. Roe", "petition", None, None),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-ct-mt"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-ct-mt", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf",
                "test-ct-mt",
                self._doc_meta(scraper_id="test-ct-mt", case_type=None),
            )

            assert len(result) == 2
            # demurrer => civil
            assert result[0]["case_type"] == "civil"
            assert result[0]["extraction_methods"]["case_type"] == "motion_type"
            # petition => probate
            assert result[1]["case_type"] == "probate"
            assert result[1]["extraction_methods"]["case_type"] == "motion_type"
        finally:
            reingest._SPLIT_REGISTRY.pop("test-ct-mt", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_split_path_applies_regex_fallbacks_for_missing_fields(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Split rulings with missing fields get regex fallback extraction (#1749).

        Verifies that outcome, case_title, and hearing_date regex
        fallbacks fire on the per-ruling text when the split result
        does not provide them.
        """
        from courts.ca.fresno_tentatives import SplitRuling

        ruling_text_with_fields = (
            "Case No. 24STCV99999\n"
            "Smith v. Jones\n"
            "Hearing Date: March 5, 2026\n"
            "Motion for Summary Judgment\n"
            "The motion is GRANTED.\n"
        )

        # Split result has case_number but is missing outcome, case_title, hearing_date
        rulings = [
            SplitRuling(
                ruling_index=1,
                case_number=None,
                ruling_text=ruling_text_with_fields,
                case_title=None,
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
        ]
        mock_split = MagicMock(
            return_value=[
                rulings[0],
                SplitRuling(2, "DUMMY", "dummy", None, None, None, None),
            ]
        )
        reingest._SPLIT_REGISTRY["test-regex"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-regex", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf",
                "test-regex",
                self._doc_meta(
                    scraper_id="test-regex",
                    case_number=None,
                    case_title=None,
                    case_type=None,
                    hearing_date=None,
                ),
            )

            first = result[0]
            # Regex should have extracted outcome from ruling text
            assert first["outcome"] is not None, "outcome should be extracted by regex"
            assert first["extraction_methods"].get("outcome") == "regex"
            # case_type should be derived from the case number prefix (24STCV => civil)
            assert first["case_type"] == "civil"
        finally:
            reingest._SPLIT_REGISTRY.pop("test-regex", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_split_path_does_not_overwrite_existing_fields_with_regex(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Regex fallback should NOT overwrite fields already set by split results (#1749)."""
        from courts.ca.fresno_tentatives import SplitRuling

        rulings = [
            SplitRuling(
                ruling_index=1,
                case_number="CVPS2306157",
                ruling_text="The motion is DENIED.\nSome judge ruling text.",
                case_title="Yeldell V. Henss",
                motion_type="Demurrer",
                outcome="Granted",  # split says Granted, text says DENIED
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=2,
                case_number="CVPS2306202",
                ruling_text="Another ruling",
                case_title=None,
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-no-overwrite"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-no-overwrite", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf",
                "test-no-overwrite",
                self._doc_meta(scraper_id="test-no-overwrite", case_type=None),
            )

            # First ruling: split-provided fields should NOT be overwritten
            assert result[0]["outcome"] == "granted"  # from split (normalized), not regex "denied"
            assert result[0]["case_title"] == "Yeldell V. Henss"
            assert result[0]["motion_type"] == "demurrer"  # normalized (#1849)
            assert result[0]["extraction_methods"]["outcome"] == "split"
        finally:
            reingest._SPLIT_REGISTRY.pop("test-no-overwrite", None)


# ---------------------------------------------------------------------------
# normalize_outcome tests — verifies reingest uses the shared function
# from ingestion.extract (#1878)
# ---------------------------------------------------------------------------


class TestNormalizeOutcome:
    """Tests that reingest uses the shared normalize_outcome from ingestion.extract.

    Comprehensive unit tests for normalize_outcome() live in test_extract.py.
    These tests verify the reingest module correctly imports and uses the shared
    function (not a local copy).
    """

    def test_reingest_uses_shared_normalize_outcome(self) -> None:
        """reingest.normalize_outcome should be the same function as
        ingestion.extract.normalize_outcome."""
        from ingestion.extract import normalize_outcome

        assert reingest.normalize_outcome is normalize_outcome

    def test_basic_normalization_via_reingest(self) -> None:
        """Spot-check that the shared function works when called via reingest."""
        assert reingest.normalize_outcome("Granted") == "granted"
        assert reingest.normalize_outcome("No Tentative Ruling") == "other"
        assert reingest.normalize_outcome(None) is None


# ---------------------------------------------------------------------------
# _supersede_document tests
# ---------------------------------------------------------------------------


class TestSupersedeDocument:
    """Tests for _supersede_document()."""

    def test_executes_delete_and_update_queries(self) -> None:
        """Deletes old rulings then marks document as superseded."""
        conn = MagicMock()
        cur = MagicMock()
        cur.rowcount = 1
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        reingest._supersede_document(conn, str(_DOC_ID_1))

        assert cur.execute.call_count == 2

        # First call: DELETE old rulings referencing the parent document
        delete_sql = cur.execute.call_args_list[0][0][0]
        assert "DELETE FROM rulings" in delete_sql
        delete_params = cur.execute.call_args_list[0][0][1]
        assert delete_params == (str(_DOC_ID_1),)

        # Second call: UPDATE document status to superseded
        update_sql = cur.execute.call_args_list[1][0][0]
        assert "UPDATE documents" in update_sql
        assert "superseded" in update_sql
        update_params = cur.execute.call_args_list[1][0][1]
        assert update_params == (str(_DOC_ID_1),)


# ---------------------------------------------------------------------------
# reingest_batch tests — full_reparse mode
# ---------------------------------------------------------------------------


class TestReingestBatchFullReparse:
    """Tests for reingest_batch() with full_reparse=True."""

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_full_reparse_calls_full_reparse_fn(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """full_reparse=True uses _full_reparse_document instead of _reparse_document."""
        row = _make_document_row(
            scraper_id="ca-riverside-tentatives-civil",
            case_number="CVPS2306157",
        )
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"pdf content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "Ruling 1",
                "case_number": "CVPS2306157",
                "case_title": "Yeldell v. Henss",
                "judge_name": "Arthur Hester III",
                "outcome": "granted",
                "motion_type": "demurrer",  # normalized (#1849)
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 1,
                "split_document_id": "split-id-1",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
            {
                "ruling_text": "Ruling 2",
                "case_number": "CVPS2306202",
                "case_title": "Crump v. Irwin",
                "judge_name": "Arthur Hester III",
                "outcome": "denied",
                "motion_type": "motion_to_compel",  # normalized (#1849)
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 2,
                "split_document_id": "split-id-2",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        assert result["processed"] == 1
        assert result["updated"] == 1
        # insert_document_and_ruling called twice — one per split ruling
        assert mock_insert_doc_and_ruling.call_count == 2
        # Original document superseded
        mock_supersede.assert_called_once()
        # upsert_case called twice (different case numbers)
        assert mock_upsert_case.call_count == 2

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_full_reparse_no_split_does_not_supersede(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """When full_reparse does not split, original document is not superseded."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"html content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "Single ruling",
                "case_number": "24STCV12345",
                "case_title": "Smith v. Jones",
                "judge_name": "John Smith",
                "outcome": "granted",
                "motion_type": "msj",
                "department": "1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 0,
                "split_document_id": str(_DOC_ID_1),
                "is_split": False,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        assert result["updated"] == 1
        mock_supersede.assert_not_called()

    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_full_reparse_dry_run_logs_split_info(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
    ) -> None:
        """Dry-run with full_reparse logs split ruling info."""
        row = _make_document_row()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = [row]
        conn.cursor.return_value = _mock_cursor_context(cur)

        mock_fetch_s3.return_value = b"pdf content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "Ruling 1",
                "case_number": "CVPS001",
                "case_title": None,
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 1,
                "split_document_id": "split-1",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
            {
                "ruling_text": "Ruling 2",
                "case_number": "CVPS002",
                "case_title": None,
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 2,
                "split_document_id": "split-2",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            full_reparse=True,
        )

        assert result["processed"] == 1
        assert result["updated"] == 0

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_split_document_ids_used_for_db_writes(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """Split doc IDs used for insert_document_and_ruling.

        Each split ruling gets its own document row (so the FK
        rulings.document_id -> documents.id is satisfied) and its own
        ruling row via the shared helper.  The original parent document
        is superseded.
        """
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"pdf content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "Ruling 1",
                "case_number": "CVPS001",
                "case_title": None,
                "judge_name": "Judge X",
                "outcome": "granted",
                "motion_type": "demurrer",  # normalized (#1849)
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 1,
                "split_document_id": "split-doc-aaa",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
            {
                "ruling_text": "Ruling 2",
                "case_number": "CVPS002",
                "case_title": None,
                "judge_name": "Judge X",
                "outcome": "denied",
                "motion_type": "MSJ",
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 2,
                "split_document_id": "split-doc-bbb",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        # insert_document_and_ruling uses split document IDs so each ruling's
        # FK is satisfied (the helper passes the same document_id to both
        # insert_document and insert_ruling internally).
        doc_ids = [c[1]["document_id"] for c in mock_insert_doc_and_ruling.call_args_list]
        assert "split-doc-aaa" in doc_ids
        assert "split-doc-bbb" in doc_ids


# ---------------------------------------------------------------------------
# full_reparse split-child guard tests (#1919)
# ---------------------------------------------------------------------------


class TestFullReparseSplitChildGuard:
    """Tests for the split-child guard in reingest_batch() full_reparse mode.

    When full_reparse=True, documents whose IDs are UUID v5 (generated by
    make_split_document_id) must be skipped to prevent an infinite loop where
    split children are re-split, creating exponentially growing document rows.
    """

    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_split_child_documents_skipped_in_full_reparse(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
    ) -> None:
        """Documents with UUID v5 IDs are skipped in full_reparse mode."""
        # Create a split-child document ID (UUID v5)
        parent_id = str(uuid.uuid4())
        child_id = uuid.UUID(make_split_document_id(parent_id, 0))

        row = _make_document_row(doc_id=child_id)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"pdf content"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        # The split child should be skipped — not parsed or written
        mock_full_reparse.assert_not_called()
        assert result["processed"] == 1
        assert result["skipped"] == 1
        assert result["updated"] == 0

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_split_child_documents_not_skipped_without_full_reparse(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """The guard only activates in full_reparse mode; normal mode processes all docs."""
        parent_id = str(uuid.uuid4())
        child_id = uuid.UUID(make_split_document_id(parent_id, 0))

        row = _make_document_row(doc_id=child_id)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"html content"
        mock_reparse.return_value = {
            "ruling_text": "text",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge",
            "outcome": "granted",
            "motion_type": "demurrer",
            "department": None,
            "parties": [],
            "hearing_date": _HEARING_DATE,
            "llm_skipped": True,
            "llm_outcome": "not_attempted",
        }

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=False,
        )

        # Without full_reparse, split-child IDs are processed normally
        mock_reparse.assert_called_once()
        assert result["processed"] == 1

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_uuid4_documents_still_processed_in_full_reparse(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """Regular UUID v4 documents are still processed in full_reparse mode."""
        regular_id = uuid.uuid4()  # UUID v4 — not a split child
        row = _make_document_row(doc_id=regular_id)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"pdf content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "Ruling text",
                "case_number": "24STCV12345",
                "case_title": "Smith v. Jones",
                "judge_name": "Judge Smith",
                "outcome": "granted",
                "motion_type": "demurrer",
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 0,
                "split_document_id": str(regular_id),
                "is_split": False,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        mock_full_reparse.assert_called_once()
        assert result["processed"] == 1
        assert result["updated"] == 1

    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_mixed_batch_skips_only_split_children(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
    ) -> None:
        """A batch with both regular and split-child IDs skips only the children."""
        regular_id = uuid.uuid4()
        parent_id = str(uuid.uuid4())
        child_id = uuid.UUID(make_split_document_id(parent_id, 0))

        # Two rows: one regular (should be processed), one split child (should be skipped)
        row_regular = _make_document_row(
            doc_id=regular_id,
            captured_at=_CAPTURED_AT_1,
        )
        row_child = _make_document_row(
            doc_id=child_id,
            captured_at=_CAPTURED_AT_2,
        )
        conn = _mock_conn_with_rows([row_regular, row_child])

        mock_fetch_s3.return_value = b"pdf content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "text",
                "case_number": "24STCV12345",
                "case_title": "Smith v. Jones",
                "judge_name": "Judge",
                "outcome": "granted",
                "motion_type": "demurrer",
                "department": None,
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 0,
                "split_document_id": str(regular_id),
                "is_split": False,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
            dry_run=True,
        )

        assert result["processed"] == 2
        assert result["skipped"] == 1
        # Only the regular doc was parsed
        mock_full_reparse.assert_called_once()

    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_cursor_advances_past_skipped_split_children(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
    ) -> None:
        """Cursor advances past skipped split-child documents to avoid re-fetching them."""
        parent_id = str(uuid.uuid4())
        child_id = uuid.UUID(make_split_document_id(parent_id, 0))
        child_captured = datetime(2026, 3, 15, 8, 0, 0)

        row = _make_document_row(doc_id=child_id, captured_at=child_captured)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"pdf content"

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        # Cursor should have advanced past the skipped document
        assert result["next_cursor"] == (child_captured, str(child_id))


# ---------------------------------------------------------------------------
# _SPLIT_REGISTRY tests
# ---------------------------------------------------------------------------


class TestSplitRegistry:
    """Tests for the split function registry."""

    def test_split_registry_not_populated_for_riverside(self) -> None:
        """Riverside no longer has _split_rulings — splitting moved to ingestion worker (#1728)."""
        # Clear registries to force re-discovery
        reingest._SCRAPER_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()

        # Call the real auto-discovery function
        reingest._load_scraper_registry()

        scraper_id = "ca-riverside-tentatives-civil"
        assert scraper_id not in reingest._SPLIT_REGISTRY


# ---------------------------------------------------------------------------
# Cost tracking tests
# ---------------------------------------------------------------------------


class TestCostTracking:
    """Tests for token tracking and cost estimation in run_reingest."""

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.create_llm_client")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_run_reingest_returns_cost_fields(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """run_reingest() returns token counts and cost estimate."""
        # Mock empty result set so reingest completes immediately
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_cur)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = ctx
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_create_llm.return_value = MagicMock()

        stats = reingest.run_reingest("postgresql://test")

        assert "input_tokens" in stats
        assert "output_tokens" in stats
        assert "llm_api_calls" in stats
        assert "estimated_cost_usd" in stats
        # No documents processed, so all should be zero
        assert stats["input_tokens"] == 0
        assert stats["output_tokens"] == 0
        assert stats["llm_api_calls"] == 0
        assert stats["estimated_cost_usd"] == 0.0

    def test_token_tracker_passed_to_reparse(self) -> None:
        """_reparse_document forwards token_tracker to extract_fields_llm."""
        from ingestion.llm_extract import TokenTracker

        tracker = TokenTracker()
        raw_content = b"<html>Some ruling text about motion</html>"
        doc_meta = {
            "document_id": str(uuid.uuid4()),
            "state": "CA",
            "county": "Test",
            "court_name": "Test Court",
            "source_url": "https://test.example.com",
            "captured_at": datetime(2026, 3, 1),
            "content_hash": "abc123",
            "format": "html",
            "case_number": None,
            "case_title": None,
            "hearing_date": None,
            "court_id": str(uuid.uuid4()),
            "scraper_id": "test-scraper",
        }

        with (
            patch.object(reingest, "_load_scraper_registry"),
            patch(
                "reingest_from_s3.extract_fields_llm",
                return_value=None,
            ) as mock_extract,
        ):
            reingest._reparse_document(
                raw_content,
                "test-scraper",
                doc_meta,
                llm_client=MagicMock(),
                token_tracker=tracker,
            )

            # Verify extract_fields_llm was called with the tracker
            mock_extract.assert_called_once()
            call_kwargs = mock_extract.call_args
            assert call_kwargs.kwargs.get("token_tracker") is tracker


# ---------------------------------------------------------------------------
# Quality metrics tests
# ---------------------------------------------------------------------------


class TestQualityMetrics:
    """Tests for _run_quality_queries and report_metrics integration."""

    def test_run_quality_queries_returns_all_metrics(self) -> None:
        """_run_quality_queries returns a dict with all expected metric keys."""
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
        # All values should be 42 (from mock)
        for val in result.values():
            assert val == 42

    def test_run_quality_queries_with_county_filter(self) -> None:
        """_run_quality_queries applies county filter when specified."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (5,)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_cur)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = ctx

        reingest._run_quality_queries(mock_conn, county="Los Angeles")

        # Should have been called with county param
        for call in mock_cur.execute.call_args_list:
            args = call[0]
            assert len(args) == 2
            # The params list should contain the county name
            assert args[1] == ["Los Angeles"]

    def test_quality_queries_dict_has_correct_keys(self) -> None:
        """_QUALITY_QUERIES has the expected metric names."""
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

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.create_llm_client")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    def test_report_metrics_includes_quality_data(
        self,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
        mock_create_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When report_metrics=True, stats include quality_before/after/delta."""
        # First connection: quality_before queries
        # Second connection: reingest (empty)
        # Third connection: quality_after queries
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

        mock_create_llm.return_value = MagicMock()

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
    """Parse schema.sql to extract {table_name: {column_names}}.

    Only parses public-schema ``CREATE TABLE`` blocks (skips staging.*).
    """
    import re

    with open(schema_path, encoding="utf-8") as f:
        sql = f.read()

    tables: dict[str, set[str]] = {}

    # Match CREATE TABLE <name> ( ... );  — skip staging schema tables
    create_re = re.compile(
        r"CREATE\s+TABLE\s+(?!staging\.)(\w+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE,
    )

    for match in create_re.finditer(sql):
        table_name = match.group(1).lower()
        body = match.group(2)
        columns: set[str] = set()

        for line in body.split("\n"):
            line = line.strip()
            # Skip empty lines, comments, constraints, and PRIMARY KEY lines
            if not line or line.startswith("--"):
                continue
            if re.match(r"(?i)(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY)\b", line):
                continue
            # First word is the column name (if it's a valid identifier)
            col_match = re.match(r"(\w+)\s+", line)
            if col_match:
                col_name = col_match.group(1).lower()
                # Skip SQL keywords that aren't column names
                if col_name in {"constraint", "primary", "unique", "check", "foreign"}:
                    continue
                columns.add(col_name)

        if columns:
            tables[table_name] = columns

    return tables


def _parse_query_references(
    query: str,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Parse a SQL query to extract table aliases and column references.

    Handles both aliased tables (``FROM cases c``) and unaliased tables
    (``FROM cases WHERE ...``).  When no alias is present, the table name
    itself is used as the lookup key so that ``cases.id`` is validated.

    Returns:
        aliases: {alias_or_table_name -> table_name}
        column_refs: [(alias_or_table_name, column_name), ...]
    """
    import re

    # Normalise whitespace (collapse newlines/tabs into spaces)
    query = " ".join(query.split())

    # Strip the {county_filter} placeholder so it doesn't confuse parsing
    query = query.replace("{county_filter}", "")

    aliases: dict[str, str] = {}

    # SQL clause keywords — if one of these follows a table name, it means
    # the table has no explicit alias.
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

    # Extract FROM/JOIN table references with an optional alias.
    # Group 1 = table name, Group 2 = next token (alias or keyword, optional).
    table_re = re.compile(
        r"(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?",
        re.IGNORECASE,
    )
    for m in table_re.finditer(query):
        table_name = m.group(1).lower()
        potential_alias = m.group(2)

        if potential_alias and potential_alias.lower() not in clause_keywords:
            # Explicit alias present (e.g. ``FROM cases c``)
            aliases[potential_alias.lower()] = table_name
        else:
            # No alias or next token is a keyword — use table name itself
            # so that ``table.column`` references are still validated.
            aliases[table_name] = table_name

    # Extract column references: alias.column patterns
    col_ref_re = re.compile(r"\b(\w+)\.(\w+)\b")
    column_refs: list[tuple[str, str]] = []
    for m in col_ref_re.finditer(query):
        alias = m.group(1).lower()
        column = m.group(2).lower()
        # Only include references whose prefix matches a known alias/table
        if alias in aliases:
            column_refs.append((alias, column))

    return aliases, column_refs


class TestQualityQueriesSchemaValidation:
    """Validate that _QUALITY_QUERIES reference only columns that exist in the schema.

    This test parses schema.sql to get the authoritative table/column definitions,
    then parses each SQL query in _QUALITY_QUERIES to extract table aliases and
    column references, and validates that every referenced column exists.

    This would have caught the parties.case_id bug: the parties table has no
    case_id column (the relationship goes through the case_parties junction table).
    """

    def test_schema_sql_exists(self) -> None:
        """schema.sql must exist for schema validation to work."""
        assert os.path.isfile(_SCHEMA_SQL_PATH), f"schema.sql not found at {_SCHEMA_SQL_PATH}"

    def test_schema_parser_extracts_known_tables(self) -> None:
        """The schema parser should find the key tables used by quality queries."""
        tables = _parse_schema_tables(_SCHEMA_SQL_PATH)
        for expected_table in (
            "cases",
            "documents",
            "courts",
            "rulings",
            "parties",
            "case_parties",
        ):
            assert expected_table in tables, (
                f"Expected table '{expected_table}' not found in parsed schema"
            )

    def test_schema_parser_extracts_columns(self) -> None:
        """The schema parser should extract correct columns for key tables."""
        tables = _parse_schema_tables(_SCHEMA_SQL_PATH)

        # Verify key columns exist where expected
        assert "case_title" in tables["cases"]
        assert "case_id" in tables["documents"]
        assert "county" in tables["courts"]
        assert "ruling_text" in tables["rulings"]
        assert "case_id" in tables["case_parties"]

        # The critical check: parties does NOT have case_id
        assert "case_id" not in tables["parties"], (
            "parties table should NOT have a case_id column — "
            "the relationship goes through case_parties"
        )

    def test_query_parser_extracts_aliases(self) -> None:
        """The query parser should correctly extract table aliases."""
        query = """
            SELECT COUNT(*) FROM cases c
            JOIN documents d ON d.case_id = c.id AND d.status = 'active'
            JOIN courts ct ON ct.id = d.court_id
            WHERE c.case_title IS NULL
        """
        aliases, _ = _parse_query_references(query)
        assert aliases == {"c": "cases", "d": "documents", "ct": "courts"}

    def test_query_parser_handles_unaliased_tables(self) -> None:
        """The query parser handles tables without an explicit alias."""
        query = """
            SELECT COUNT(*) FROM cases
            WHERE cases.case_title IS NULL
        """
        aliases, col_refs = _parse_query_references(query)
        # The table name itself should be used as the key
        assert aliases == {"cases": "cases"}
        assert ("cases", "case_title") in col_refs

    def test_query_parser_extracts_column_refs(self) -> None:
        """The query parser should correctly extract alias.column references."""
        query = """
            SELECT COUNT(*) FROM cases c
            JOIN documents d ON d.case_id = c.id AND d.status = 'active'
            WHERE c.case_title IS NULL
        """
        aliases, col_refs = _parse_query_references(query)
        # Check that key references are found
        assert ("d", "case_id") in col_refs
        assert ("c", "id") in col_refs
        assert ("d", "status") in col_refs
        assert ("c", "case_title") in col_refs

    def test_all_quality_queries_reference_valid_columns(self) -> None:
        """Every column reference in _QUALITY_QUERIES must exist in the schema.

        This is the primary regression test. If a query references a column
        that does not exist on the target table (like parties.case_id), this
        test fails with a clear error message.
        """
        tables = _parse_schema_tables(_SCHEMA_SQL_PATH)
        errors: list[str] = []

        for query_name, query_sql in reingest._QUALITY_QUERIES.items():
            aliases, col_refs = _parse_query_references(query_sql)

            for alias, column in col_refs:
                table_name = aliases.get(alias)
                if table_name is None:
                    # Alias not recognized — skip (could be a subquery alias)
                    continue
                if table_name not in tables:
                    errors.append(
                        f"Query '{query_name}': table '{table_name}' "
                        f"(alias '{alias}') not found in schema"
                    )
                    continue
                if column not in tables[table_name]:
                    errors.append(
                        f"Query '{query_name}': column '{table_name}.{column}' "
                        f"does not exist (alias '{alias}.{column}'). "
                        f"Available columns: {sorted(tables[table_name])}"
                    )

        assert not errors, "Quality queries reference invalid columns:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    def test_would_catch_parties_case_id_bug(self) -> None:
        """Verify that the validation approach catches the original bug.

        The original bug used ``parties p ... p.case_id`` but the parties
        table has no case_id column. This test constructs such a buggy query
        and confirms the validation catches it.
        """
        tables = _parse_schema_tables(_SCHEMA_SQL_PATH)

        # Simulate the original buggy query
        buggy_query = """
            SELECT COUNT(DISTINCT c.id) FROM cases c
            JOIN documents d ON d.case_id = c.id AND d.status = 'active'
            JOIN courts ct ON ct.id = d.court_id
            LEFT JOIN parties p ON p.case_id = c.id
            WHERE p.id IS NULL
        """  # sql-check:ignore — intentionally invalid SQL for testing validation
        aliases, col_refs = _parse_query_references(buggy_query)

        # The alias 'p' should map to 'parties'
        assert aliases.get("p") == "parties"

        # Validate: parties.case_id should be flagged as invalid
        invalid_refs = []
        for alias, column in col_refs:
            table_name = aliases.get(alias)
            if table_name and table_name in tables:
                if column not in tables[table_name]:
                    invalid_refs.append(f"{table_name}.{column}")

        assert "parties.case_id" in invalid_refs, (
            "The validation should catch 'parties.case_id' as invalid. "
            f"Invalid refs found: {invalid_refs}"
        )


# ---------------------------------------------------------------------------
# Multimodal extraction tests (#1719)
# ---------------------------------------------------------------------------


class TestReparseDocumentMultimodal:
    """Tests for ``_reparse_document_multimodal()``."""

    def _make_doc_meta(
        self,
        doc_id: str = "test-doc-id",
        doc_format: str = "pdf",
    ) -> dict:
        return {
            "document_id": doc_id,
            "state": "CA",
            "county": "Orange",
            "court_name": "Orange County Superior Court",
            "source_url": "https://court.example.com/ruling.pdf",
            "captured_at": datetime(2026, 3, 1, 10, 0, 0),
            "content_hash": "abc123hash",
            "format": doc_format,
            "case_number": None,
            "case_title": None,
            "hearing_date": date(2026, 3, 5),
            "court_id": "court-id-1",
            "scraper_id": "ca-oc-tentatives-civil",
            "s3_key": "docs/test.pdf",
            "s3_bucket": "test-bucket",
        }

    def test_non_pdf_falls_back_to_text_reparse(self) -> None:
        """Non-PDF documents should fall back to _reparse_document."""
        doc_meta = self._make_doc_meta(doc_format="html")
        mock_extractor = MagicMock()

        with (
            patch.object(reingest, "_reparse_document") as mock_reparse,
            patch.object(reingest, "_load_scraper_registry"),
        ):
            mock_reparse.return_value = {
                "ruling_text": "test ruling",
                "case_number": "24STCV12345",
                "case_title": "Smith v. Jones",
                "case_type": None,
                "judge_name": "Judge Smith",
                "outcome": "granted",
                "motion_type": "demurrer",  # normalized (#1849)
                "department": "D1",
                "parties": [],
                "hearing_date": date(2026, 3, 5),
                "extraction_methods": {},
                "llm_skipped": False,
                "llm_outcome": "success",
            }

            results = reingest._reparse_document_multimodal(
                b"<html>ruling</html>",
                "ca-la-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results) == 1
        assert results[0]["case_number"] == "24STCV12345"
        assert results[0]["ruling_index"] == 0
        assert results[0]["is_split"] is False
        mock_extractor.extract_from_pdf.assert_not_called()
        mock_reparse.assert_called_once()

    def test_pdf_uses_multimodal_extraction(self) -> None:
        """PDF documents should use extract_from_pdf on the multimodal extractor."""
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion is GRANTED.",
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks"):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results) == 1
        assert results[0]["case_number"] == "30-2024-01234567"
        assert results[0]["case_title"] == "Smith v. Jones"
        assert results[0]["ruling_text"] == "The motion is GRANTED."
        assert results[0]["llm_outcome"] == "multimodal_success"
        assert results[0]["is_split"] is False
        assert results[0]["ruling_index"] == 0
        mock_extractor.extract_from_pdf.assert_called_once()

    def test_pdf_multimodal_multiple_rulings(self) -> None:
        """Multi-ruling PDFs should produce split documents."""
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                extracted_case_title="Ruling One",
                ruling_text="First ruling text.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000002",
                extracted_case_title="Ruling Two",
                ruling_text="Second ruling text.",
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks"):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results) == 2
        assert results[0]["case_number"] == "30-2024-00000001"
        assert results[0]["is_split"] is True
        assert results[0]["ruling_index"] == 0
        assert results[1]["case_number"] == "30-2024-00000002"
        assert results[1]["is_split"] is True
        assert results[1]["ruling_index"] == 1
        # Split documents should have different split_document_ids
        assert results[0]["split_document_id"] != results[1]["split_document_id"]

    def test_pdf_multimodal_failure_falls_back(self) -> None:
        """Multimodal extraction failure should fall back to text-based."""
        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.side_effect = RuntimeError("API error")

        with (
            patch.object(reingest, "_reparse_document") as mock_reparse,
            patch.object(reingest, "_load_scraper_registry"),
        ):
            mock_reparse.return_value = {
                "ruling_text": "fallback text",
                "case_number": "UNKNOWN-test-doc-id",
                "case_title": None,
                "case_type": None,
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": date(2026, 3, 5),
                "extraction_methods": {},
                "llm_skipped": False,
                "llm_outcome": "failure",
            }

            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results) == 1
        assert results[0]["llm_outcome"] == "multimodal_fallback"
        mock_reparse.assert_called_once()

    def test_pdf_multimodal_empty_results_falls_back(self) -> None:
        """Empty multimodal results should fall back to text-based."""
        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = []

        with (
            patch.object(reingest, "_reparse_document") as mock_reparse,
            patch.object(reingest, "_load_scraper_registry"),
        ):
            mock_reparse.return_value = {
                "ruling_text": "fallback text",
                "case_number": "UNKNOWN-test-doc-id",
                "case_title": None,
                "case_type": None,
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": date(2026, 3, 5),
                "extraction_methods": {},
                "llm_skipped": False,
                "llm_outcome": "failure",
            }

            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results) == 1
        assert results[0]["llm_outcome"] == "multimodal_fallback"

    def test_metadata_passed_to_extractor(self) -> None:
        """Metadata (judge_name, department, hearing_date) should be passed."""
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        doc_meta["judge_name"] = "Judge Williams"
        doc_meta["department"] = "C32"
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                ruling_text="Granted.",
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks"):
            reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        call_kwargs = mock_extractor.extract_from_pdf.call_args
        metadata = call_kwargs.kwargs.get("metadata") or call_kwargs[1].get("metadata")
        assert metadata["judge_name"] == "Judge Williams"
        assert metadata["department"] == "C32"
        assert metadata["hearing_date"] == "2026-03-05"

    def test_outcome_enum_converted_to_string(self) -> None:
        """ExtractionOutcome enum values should be converted to strings."""
        from framework.llm_schema import ExtractedRuling, ExtractionOutcome

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                ruling_text="The motion is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks"):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert results[0]["outcome"] == "granted"

    def test_regex_fallbacks_applied(self) -> None:
        """Regex fallbacks should fill missing fields from ruling text."""
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                ruling_text="MOTION TO COMPEL is GRANTED. Judge Wilson presiding.",
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks") as mock_fallback:
            reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        mock_fallback.assert_called_once()
        call_args = mock_fallback.call_args
        assert call_args[0][0]["ruling_text"] == (
            "MOTION TO COMPEL is GRANTED. Judge Wilson presiding."
        )

    def test_parties_extracted(self) -> None:
        """Parties from ExtractedRuling should be converted to dict format."""
        from framework.llm_schema import ExtractedParty, ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                ruling_text="Granted.",
                extracted_parties=[
                    ExtractedParty(name="Smith", role="plaintiff"),
                    ExtractedParty(name="Jones", role="defendant"),
                ],
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks"):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results[0]["parties"]) == 2
        assert results[0]["parties"][0] == {"name": "Smith", "role": "plaintiff"}
        assert results[0]["parties"][1] == {"name": "Jones", "role": "defendant"}


class TestReingestBatchMultimodal:
    """Tests for reingest_batch with multimodal extraction enabled."""

    def test_multimodal_extractor_used_for_pdf_docs(self) -> None:
        """When multimodal_extractor is provided, it should be used for parsing."""
        from framework.llm_schema import ExtractedRuling

        row = _make_document_row(
            scraper_id="ca-oc-tentatives-civil",
        )
        # Override format to pdf
        row_list = list(row)
        row_list[10] = "pdf"  # format column
        row_list[11] = "CA"
        row_list[12] = "Orange"
        row = tuple(row_list)

        conn = _mock_conn_with_rows([row])
        s3 = _mock_s3_client(b"%PDF-1.4 fake pdf content")
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion is GRANTED.",
            ),
        ]

        with (
            patch.object(reingest, "_apply_regex_fallbacks"),
            patch.object(reingest, "upsert_case", return_value="case-id"),
            patch.object(reingest, "resolve_judge", return_value=None),
            patch.object(reingest, "insert_document_and_ruling"),
            patch.object(reingest, "batch_upsert_parties"),
        ):
            result = reingest.reingest_batch(
                conn,
                s3,
                25,
                (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID),
                "",
                [],
                multimodal_extractor=mock_extractor,
            )

        assert result["processed"] == 1
        mock_extractor.extract_from_pdf.assert_called_once()

    def test_multimodal_flag_not_present_uses_standard_path(self) -> None:
        """Without multimodal_extractor, standard text parsing should be used."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])
        s3 = _mock_s3_client()

        with (
            patch.object(reingest, "_reparse_document") as mock_reparse,
            patch.object(reingest, "_load_scraper_registry"),
            patch.object(reingest, "upsert_case", return_value="case-id"),
            patch.object(reingest, "resolve_judge", return_value=None),
            patch.object(reingest, "insert_document_and_ruling"),
            patch.object(reingest, "batch_upsert_parties"),
        ):
            mock_reparse.return_value = {
                "ruling_text": "test",
                "case_number": "24STCV12345",
                "case_title": "Smith v. Jones",
                "case_type": None,
                "judge_name": None,
                "outcome": None,
                "motion_type": None,
                "department": None,
                "parties": [],
                "hearing_date": date(2026, 3, 5),
                "extraction_methods": {},
                "llm_skipped": False,
                "llm_outcome": "not_attempted",
            }
            result = reingest.reingest_batch(
                conn,
                s3,
                25,
                (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID),
                "",
                [],
            )

        assert result["processed"] == 1
        mock_reparse.assert_called_once()


class TestCLIMultimodalFlag:
    """Verify --multimodal CLI flag is parsed correctly."""

    def test_multimodal_flag_parsed(self) -> None:
        """--multimodal should set args.multimodal to True."""
        import argparse

        # Build a minimal parser with just the flag
        parser = argparse.ArgumentParser()
        parser.add_argument("--multimodal", action="store_true")
        args = parser.parse_args(["--multimodal"])
        assert args.multimodal is True

    def test_multimodal_flag_default_false(self) -> None:
        """Without --multimodal, args.multimodal should be False."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--multimodal", action="store_true")
        args = parser.parse_args([])
        assert args.multimodal is False


# ---------------------------------------------------------------------------
# Stored ruling_text preservation tests (#1848)
# ---------------------------------------------------------------------------


class TestStoredRulingTextPreservation:
    """Verify that stored ruling_text is used for regex extraction and preserved
    during reingest, rather than being overwritten by full PDF text (#1848)."""

    def _doc_meta(self, *, stored_ruling_text: str | None = None) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "html",
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-la-tentatives-civil",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
            "stored_ruling_text": stored_ruling_text,
        }

    @patch.object(reingest, "_load_scraper_registry")
    def test_stored_ruling_text_used_for_regex_extraction(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """When stored ruling_text exists, regex extraction runs against it,
        not against the full document text from pdfplumber."""
        # Full PDF text contains "motion to compel" which is GRANTED — this
        # belongs to a different case in the same PDF.  The stored
        # ruling_text for THIS case contains "demurrer" which is DENIED.
        # If regex runs against the full text, motion_type would be
        # "motion_to_compel" and outcome "granted".  If regex runs against
        # stored text (correct), motion_type is "demurrer" and outcome
        # "denied".
        full_pdf_text = "Motion to Compel is GRANTED."
        stored_text = "Demurrer to the complaint is DENIED."

        raw = full_pdf_text.encode()
        meta = self._doc_meta(stored_ruling_text=stored_text)

        result = reingest._reparse_document(raw, "unknown-scraper", meta)

        # motion_type should come from the stored ruling text (demurrer),
        # NOT from the full PDF (which would yield motion_to_compel).
        assert result["motion_type"] == "demurrer"
        # outcome should come from the stored ruling text (denied),
        # NOT from the full PDF (which would yield granted).
        assert result["outcome"] == "denied"

    @patch.object(reingest, "_load_scraper_registry")
    def test_stored_ruling_text_preserved_in_output(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """When stored ruling_text exists, it is preserved as the output
        ruling_text instead of being replaced by the full PDF text."""
        full_pdf_text = "Full PDF with 77000 chars of multiple rulings..."
        stored_text = "Individual case ruling text (~1K chars)."

        raw = full_pdf_text.encode()
        meta = self._doc_meta(stored_ruling_text=stored_text)

        result = reingest._reparse_document(raw, "unknown-scraper", meta)

        assert result["ruling_text"] == stored_text

    @patch.object(reingest, "_load_scraper_registry")
    def test_full_text_used_when_no_stored_ruling_text(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """When no stored ruling_text exists, the full PDF text is used as
        before (backward compatible)."""
        full_text = "Full ruling text from pdfplumber. Demurrer is GRANTED."

        raw = full_text.encode()
        meta = self._doc_meta(stored_ruling_text=None)

        result = reingest._reparse_document(raw, "unknown-scraper", meta)

        assert result["ruling_text"] == full_text
        assert result["outcome"] == "granted"
        assert result["motion_type"] == "demurrer"

    @patch.object(reingest, "_load_scraper_registry")
    def test_stored_ruling_text_nul_bytes_stripped(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """NUL bytes in stored ruling_text are stripped."""
        stored_text = "Ruling\x00 text with NUL bytes"

        raw = b"<html>full doc text</html>"
        meta = self._doc_meta(stored_ruling_text=stored_text)

        result = reingest._reparse_document(raw, "unknown-scraper", meta)

        assert "\x00" not in result["ruling_text"]
        assert result["ruling_text"] == "Ruling text with NUL bytes"

    def test_fetch_query_includes_stored_ruling_text(self) -> None:
        """FETCH_DOCUMENTS_QUERY must include stored_ruling_text subquery."""
        assert "stored_ruling_text" in reingest.FETCH_DOCUMENTS_QUERY

    def test_make_document_row_includes_stored_ruling_text(self) -> None:
        """_make_document_row supports stored_ruling_text parameter."""
        row = _make_document_row(stored_ruling_text="test ruling")
        # stored_ruling_text is the last element in the tuple
        assert row[-1] == "test ruling"

    def test_make_document_row_default_stored_ruling_text_is_none(self) -> None:
        """_make_document_row defaults stored_ruling_text to None."""
        row = _make_document_row()
        assert row[-1] is None

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_stored_ruling_text_threaded_to_doc_meta(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """stored_ruling_text from the DB query flows into doc_meta."""
        stored_text = "Individual ruling for this case"
        row = _make_document_row(stored_ruling_text=stored_text)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>full PDF text</html>"
        mock_reparse.return_value = {
            "ruling_text": stored_text,
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "demurrer",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        # Verify _reparse_document received doc_meta with stored_ruling_text
        call_args = mock_reparse.call_args
        doc_meta_arg = call_args[0][2]  # Third positional arg is doc_meta
        assert doc_meta_arg["stored_ruling_text"] == stored_text

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_ruling_text_preserved_in_db_write(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """ruling_text passed to insert_document_and_ruling should be the
        stored (individual) text, not the full PDF text."""
        stored_text = "Individual ruling: Demurrer is SUSTAINED."
        row = _make_document_row(stored_ruling_text=stored_text)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>Full 77K PDF with many rulings</html>"
        # _reparse_document should return the stored text as ruling_text
        mock_reparse.return_value = {
            "ruling_text": stored_text,
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "sustained",
            "motion_type": "demurrer",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        call_kwargs = mock_insert_doc_and_ruling.call_args[1]
        assert call_kwargs["ruling_text"] == stored_text


# ---------------------------------------------------------------------------
# Checkpoint / Resume tests (#1925)
# ---------------------------------------------------------------------------


class TestWriteCheckpoint:
    """Tests for _write_checkpoint()."""

    def test_writes_valid_json(self, tmp_path: Any) -> None:
        """Checkpoint file contains valid JSON with expected keys."""
        import json as _json

        cp_path = tmp_path / "checkpoint.json"
        cursor = (datetime(2026, 3, 15, 10, 30, 0), "doc-uuid-123")
        stats = {"total_processed": 50, "total_updated": 45}

        reingest._write_checkpoint(cp_path, cursor, stats)

        data = _json.loads(cp_path.read_text(encoding="utf-8"))
        assert data["version"] == reingest._CHECKPOINT_VERSION
        assert data["cursor"]["captured_at"] == "2026-03-15T10:30:00"
        assert data["cursor"]["document_id"] == "doc-uuid-123"
        assert data["stats"]["total_processed"] == 50
        assert data["stats"]["total_updated"] == 45
        assert "updated_at" in data

    def test_atomic_write_via_rename(self, tmp_path: Any) -> None:
        """Checkpoint overwrites previous file atomically (no .tmp left)."""
        cp_path = tmp_path / "checkpoint.json"
        cursor1 = (datetime(2026, 3, 1), "uuid-1")
        cursor2 = (datetime(2026, 3, 2), "uuid-2")

        reingest._write_checkpoint(cp_path, cursor1, {"total_processed": 10})
        reingest._write_checkpoint(cp_path, cursor2, {"total_processed": 20})

        data = json.loads(cp_path.read_text(encoding="utf-8"))
        assert data["cursor"]["document_id"] == "uuid-2"
        assert data["stats"]["total_processed"] == 20
        # Temp file should not remain
        assert not (tmp_path / "checkpoint.tmp").exists()


class TestReadCheckpoint:
    """Tests for _read_checkpoint()."""

    def test_reads_valid_checkpoint(self, tmp_path: Any) -> None:
        """Round-trip: write then read returns same cursor and stats."""
        cp_path = tmp_path / "checkpoint.json"
        original_cursor = (datetime(2026, 3, 15, 10, 30, 0), "doc-uuid-456")
        original_stats = {
            "total_processed": 100,
            "total_updated": 90,
            "total_batches": 4,
        }

        reingest._write_checkpoint(cp_path, original_cursor, original_stats)
        cursor, stats = reingest._read_checkpoint(cp_path)

        assert cursor[0] == original_cursor[0]
        assert cursor[1] == original_cursor[1]
        assert stats["total_processed"] == 100
        assert stats["total_updated"] == 90
        assert stats["total_batches"] == 4

    def test_raises_on_missing_file(self, tmp_path: Any) -> None:
        """FileNotFoundError when checkpoint file does not exist."""
        import pytest

        cp_path = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            reingest._read_checkpoint(cp_path)

    def test_raises_on_bad_version(self, tmp_path: Any) -> None:
        """ValueError when checkpoint file has unsupported version."""
        import pytest

        cp_path = tmp_path / "checkpoint.json"
        cp_path.write_text(
            json.dumps(
                {
                    "version": 999,
                    "cursor": {
                        "captured_at": "2026-03-01T00:00:00",
                        "document_id": "x",
                    },
                    "stats": {},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Unsupported checkpoint version"):
            reingest._read_checkpoint(cp_path)

    def test_raises_on_invalid_json(self, tmp_path: Any) -> None:
        """ValueError when file contains invalid JSON."""
        import pytest

        cp_path = tmp_path / "checkpoint.json"
        cp_path.write_text("not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            reingest._read_checkpoint(cp_path)


class TestRunReingestCheckpoint:
    """Tests for checkpoint integration in run_reingest()."""

    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.psycopg")
    def test_checkpoint_written_after_each_batch(
        self,
        mock_psycopg: MagicMock,
        mock_batch: MagicMock,
        tmp_path: Any,
    ) -> None:
        """Checkpoint file is written after every batch when --checkpoint-file
        is provided."""
        cp_path = tmp_path / "cp.json"

        mock_batch.side_effect = [
            _make_batch_result(
                processed=25,
                updated=20,
                next_cursor=(_CAPTURED_AT_1, str(_DOC_ID_1)),
                batch_number=1,
            ),
            _make_batch_result(
                processed=10,
                updated=8,
                next_cursor=(_CAPTURED_AT_2, str(_DOC_ID_2)),
                batch_number=2,
            ),
        ]

        reingest.run_reingest(
            "postgresql://test",
            batch_size=25,
            checkpoint_file=str(cp_path),
        )

        assert cp_path.exists()
        data = json.loads(cp_path.read_text(encoding="utf-8"))
        # Should reflect cumulative stats from both batches
        assert data["stats"]["total_processed"] == 35
        assert data["stats"]["total_updated"] == 28
        assert data["stats"]["total_batches"] == 2
        assert data["cursor"]["document_id"] == str(_DOC_ID_2)

    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.psycopg")
    def test_no_checkpoint_without_flag(
        self,
        mock_psycopg: MagicMock,
        mock_batch: MagicMock,
        tmp_path: Any,
    ) -> None:
        """No checkpoint file is created when --checkpoint-file is not given."""
        cp_path = tmp_path / "cp.json"

        mock_batch.return_value = _make_batch_result(processed=0)

        reingest.run_reingest("postgresql://test", batch_size=25)

        assert not cp_path.exists()

    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.psycopg")
    def test_dry_run_does_not_write_checkpoint(
        self,
        mock_psycopg: MagicMock,
        mock_batch: MagicMock,
        tmp_path: Any,
    ) -> None:
        """Checkpoint file is NOT written during --dry-run, because a dry
        run should not produce side effects that could cause a subsequent
        real run with --resume to skip documents."""
        cp_path = tmp_path / "cp.json"

        mock_batch.side_effect = [
            _make_batch_result(
                processed=25,
                updated=0,
                next_cursor=(_CAPTURED_AT_1, str(_DOC_ID_1)),
                batch_number=1,
            ),
            _make_batch_result(processed=0, batch_number=2),
        ]

        reingest.run_reingest(
            "postgresql://test",
            batch_size=25,
            dry_run=True,
            checkpoint_file=str(cp_path),
        )

        assert not cp_path.exists()

    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.psycopg")
    def test_resume_restores_cursor_and_stats(
        self,
        mock_psycopg: MagicMock,
        mock_batch: MagicMock,
        tmp_path: Any,
    ) -> None:
        """When --resume is used, the cursor and stats are restored from the
        checkpoint file, and processing continues from the saved position."""
        cp_path = tmp_path / "cp.json"

        # Write a checkpoint as if 25 docs were already processed
        saved_cursor = (_CAPTURED_AT_1, str(_DOC_ID_1))
        reingest._write_checkpoint(
            cp_path,
            saved_cursor,
            {
                "total_processed": 25,
                "total_updated": 20,
                "total_llm_skipped": 5,
                "total_failed": 0,
                "total_skipped": 0,
                "total_batches": 1,
                "total_llm_success": 15,
                "total_llm_failure": 0,
            },
        )

        # The next batch returns 10 more docs, then no more
        mock_batch.side_effect = [
            _make_batch_result(
                processed=10,
                updated=8,
                next_cursor=(_CAPTURED_AT_2, str(_DOC_ID_2)),
                batch_number=2,
            ),
            _make_batch_result(processed=0, batch_number=3),
        ]

        stats = reingest.run_reingest(
            "postgresql://test",
            batch_size=25,
            checkpoint_file=str(cp_path),
            resume=True,
        )

        # reingest_batch should have been called with the restored cursor.
        # cursor is the 4th positional arg (conn, s3, batch_size, cursor, ...)
        first_call_args = mock_batch.call_args_list[0][0]
        assert first_call_args[3] == saved_cursor

        # Cumulative stats should include the restored stats + new batch
        assert stats["total_processed"] == 35  # 25 restored + 10 new
        assert stats["total_updated"] == 28  # 20 restored + 8 new

    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.psycopg")
    def test_resume_without_existing_checkpoint_starts_fresh(
        self,
        mock_psycopg: MagicMock,
        mock_batch: MagicMock,
        tmp_path: Any,
    ) -> None:
        """When --resume is used but no checkpoint file exists, processing
        starts from the beginning (no error)."""
        cp_path = tmp_path / "nonexistent.json"

        mock_batch.return_value = _make_batch_result(processed=0)

        reingest.run_reingest(
            "postgresql://test",
            batch_size=25,
            checkpoint_file=str(cp_path),
            resume=True,
        )

        # Should have been called with the default cursor (4th positional arg)
        first_call_args = mock_batch.call_args_list[0][0]
        assert first_call_args[3] == _DEFAULT_CURSOR

    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.psycopg")
    def test_checkpoint_updated_each_batch(
        self,
        mock_psycopg: MagicMock,
        mock_batch: MagicMock,
        tmp_path: Any,
    ) -> None:
        """Checkpoint is updated after each batch, not just at the end."""
        cp_path = tmp_path / "cp.json"
        checkpoint_snapshots: list[dict] = []

        original_write = reingest._write_checkpoint

        def capture_checkpoint(path: Any, cursor: Any, stats: Any) -> None:
            original_write(path, cursor, stats)
            data = json.loads(path.read_text(encoding="utf-8"))
            checkpoint_snapshots.append(data)

        mock_batch.side_effect = [
            _make_batch_result(
                processed=25,
                updated=20,
                next_cursor=(_CAPTURED_AT_1, str(_DOC_ID_1)),
                batch_number=1,
            ),
            _make_batch_result(
                processed=25,
                updated=22,
                next_cursor=(_CAPTURED_AT_2, str(_DOC_ID_2)),
                batch_number=2,
            ),
            _make_batch_result(processed=0, batch_number=3),
        ]

        with patch.object(reingest, "_write_checkpoint", side_effect=capture_checkpoint):
            reingest.run_reingest(
                "postgresql://test",
                batch_size=25,
                checkpoint_file=str(cp_path),
            )

        # Two checkpoints should have been written (batch 3 processed=0 exits
        # before checkpoint write because processed < effective_batch triggers
        # break, but checkpoint IS written before the break check)
        assert len(checkpoint_snapshots) >= 2
        assert checkpoint_snapshots[0]["stats"]["total_processed"] == 25
        assert checkpoint_snapshots[1]["stats"]["total_processed"] == 50


# ---------------------------------------------------------------------------
# _LLM_SPLIT_REGISTRY and _full_reparse_document LLM split preference (#1969)
# ---------------------------------------------------------------------------


class TestLlmSplitRegistry:
    """Tests for LLM-based split function registration and preference in
    _full_reparse_document (#1969).

    When a scraper module exports both _split_rulings (regex) and
    _llm_extract_rulings (LLM), _full_reparse_document should prefer the
    LLM-based function because it produces higher-quality ruling_text
    (e.g. full legal analyses instead of disposition summaries).
    """

    def _doc_meta(self, **overrides: Any) -> dict:
        meta = {
            "document_id": "test-llm-split-doc",
            "scraper_id": "test-llm-split",
            "state": "CA",
            "county": "TestCounty",
            "court_name": "TestCounty Superior Court",
            "source_url": "https://example.com/test.pdf",
            "captured_at": datetime(2026, 3, 25, 12, 0, 0),
            "hearing_date": date(2026, 3, 25),
            "format": "pdf",
            "content_hash": "abc123",
            "case_number": "TEST001",
            "case_title": "Smith v. Jones",
            "case_type": "civil",
            "stored_ruling_text": None,
        }
        meta.update(overrides)
        return meta

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_llm_split_preferred_over_regex(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When LLM split is registered, it should be used instead of regex."""
        from courts.ca.fresno_tentatives import SplitRuling

        # LLM returns richer ruling_text
        llm_rulings = [
            SplitRuling(
                1,
                "TEST001",
                "Full legal analysis with citations...",
                "Smith v. Jones",
                "msj",
                "denied",
                None,
            ),
            SplitRuling(
                2,
                "TEST002",
                "Detailed demurrer analysis...",
                "Doe v. Roe",
                "demurrer",
                "granted",
                None,
            ),
        ]
        # Regex returns truncated text
        regex_rulings = [
            SplitRuling(1, "TEST001", "DENY MSJ.", "Smith v. Jones", "msj", "denied", None),
            SplitRuling(2, "TEST002", "GRANT demurrer.", "Doe v. Roe", "demurrer", "granted", None),
        ]
        mock_llm_split = MagicMock(return_value=llm_rulings)
        mock_regex_split = MagicMock(return_value=regex_rulings)

        reingest._SPLIT_REGISTRY["test-llm-split"] = mock_regex_split
        reingest._LLM_SPLIT_REGISTRY["test-llm-split"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-split", None)
        mock_extract.return_value = "full pdf text"

        try:
            result = reingest._full_reparse_document(b"raw pdf", "test-llm-split", self._doc_meta())
            # LLM split should have been called
            mock_llm_split.assert_called_once_with("full pdf text")
            # Regex split should NOT have been called
            mock_regex_split.assert_not_called()
            # Ruling text should come from LLM (full analysis)
            assert len(result) == 2
            assert result[0]["ruling_text"] == "Full legal analysis with citations..."
            assert result[1]["ruling_text"] == "Detailed demurrer analysis..."
        finally:
            reingest._SPLIT_REGISTRY.pop("test-llm-split", None)
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-split", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_llm_split_failure_falls_back_to_regex(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When LLM split returns None, regex split should be used as fallback."""
        from courts.ca.fresno_tentatives import SplitRuling

        regex_rulings = [
            SplitRuling(1, "TEST001", "DENY MSJ.", "Smith v. Jones", "msj", "denied", None),
            SplitRuling(2, "TEST002", "GRANT demurrer.", "Doe v. Roe", "demurrer", "granted", None),
        ]
        mock_llm_split = MagicMock(return_value=None)
        mock_regex_split = MagicMock(return_value=regex_rulings)

        reingest._SPLIT_REGISTRY["test-llm-split"] = mock_regex_split
        reingest._LLM_SPLIT_REGISTRY["test-llm-split"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-split", None)
        mock_extract.return_value = "full pdf text"

        try:
            result = reingest._full_reparse_document(b"raw pdf", "test-llm-split", self._doc_meta())
            # Both should have been called (LLM first, then regex fallback)
            mock_llm_split.assert_called_once()
            mock_regex_split.assert_called_once()
            # Result should come from regex fallback
            assert len(result) == 2
            assert result[0]["ruling_text"] == "DENY MSJ."
        finally:
            reingest._SPLIT_REGISTRY.pop("test-llm-split", None)
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-split", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_no_llm_split_uses_regex_only(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When no LLM split is registered, regex split should be used directly."""
        from courts.ca.fresno_tentatives import SplitRuling

        regex_rulings = [
            SplitRuling(1, "TEST001", "DENY MSJ.", "Smith v. Jones", "msj", "denied", None),
            SplitRuling(2, "TEST002", "GRANT demurrer.", "Doe v. Roe", "demurrer", "granted", None),
        ]
        mock_regex_split = MagicMock(return_value=regex_rulings)

        reingest._SPLIT_REGISTRY["test-llm-split"] = mock_regex_split
        reingest._LLM_SPLIT_REGISTRY.pop("test-llm-split", None)
        reingest._SCRAPER_REGISTRY.pop("test-llm-split", None)
        mock_extract.return_value = "full pdf text"

        try:
            result = reingest._full_reparse_document(b"raw pdf", "test-llm-split", self._doc_meta())
            # Only regex should have been called
            mock_regex_split.assert_called_once()
            assert len(result) == 2
            assert result[0]["ruling_text"] == "DENY MSJ."
        finally:
            reingest._SPLIT_REGISTRY.pop("test-llm-split", None)


class TestLlmSplitRegistryAutoDiscovery:
    """Tests that _load_scraper_registry() correctly discovers and registers
    _llm_extract_rulings functions from scraper modules (#1969)."""

    def test_riverside_not_in_split_registries(self) -> None:
        """After #1728, Riverside LLM extraction moved to framework-level
        LlmExtractor via extraction_config.py.  The scraper module no longer
        exports _llm_extract_rulings or _split_rulings, so Riverside must NOT
        appear in _LLM_SPLIT_REGISTRY or _SPLIT_REGISTRY."""
        reingest._SCRAPER_REGISTRY.clear()
        reingest._LLM_SPLIT_REGISTRY.clear()
        reingest._SPLIT_REGISTRY.clear()
        reingest._load_scraper_registry()

        assert "ca-riverside-tentatives-civil" not in reingest._LLM_SPLIT_REGISTRY, (
            "Riverside should not be in _LLM_SPLIT_REGISTRY (LLM extraction moved to framework)"
        )
        assert "ca-riverside-tentatives-civil" not in reingest._SPLIT_REGISTRY, (
            "Riverside should not be in _SPLIT_REGISTRY (splitting moved to framework)"
        )


# ---------------------------------------------------------------------------
# S3-key deduplication in full-reparse mode (#1984)
# ---------------------------------------------------------------------------


class TestFullReparseS3KeyDedup:
    """Tests for S3-key-level deduplication in reingest_batch() full_reparse mode.

    Riverside calendar PDFs contain rulings for ~6 cases.  The scraper creates
    one document row per case, all pointing to the same S3 key.  Without dedup,
    ``--full-reparse`` would process the same PDF N times (once per document
    row), producing N * R ruling records instead of R — a Cartesian product.
    See #1984.
    """

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_full_reparse_deduplicates_by_s3_key(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """6 document rows sharing one S3 key produces N rulings, not 6*N."""
        shared_s3_key = "ca/riverside/calendar/2026-03-05.pdf"
        shared_s3_bucket = "judgemind-docs"

        # Create 6 document rows sharing the same S3 key (different doc IDs,
        # different case numbers, same PDF).
        rows = []
        for i in range(6):
            rows.append(
                _make_document_row(
                    doc_id=uuid.uuid4(),
                    scraper_id="ca-riverside-tentatives-civil",
                    case_number=f"CVPS230600{i}",
                    case_title=f"Case {i} v. Defendant {i}",
                    s3_key=shared_s3_key,
                    s3_bucket=shared_s3_bucket,
                )
            )

        conn = _mock_conn_with_rows(rows)
        mock_fetch_s3.return_value = b"shared pdf content"

        # The PDF contains 3 rulings when split
        mock_full_reparse.return_value = [
            {
                "ruling_text": f"Ruling {j}",
                "case_number": f"CVPS230600{j}",
                "case_title": f"Case {j} v. Defendant {j}",
                "judge_name": "Arthur Hester III",
                "outcome": "granted",
                "motion_type": "demurrer",
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": j,
                "split_document_id": f"split-id-{j}",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            }
            for j in range(1, 4)
        ]
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        processed_keys: set[tuple[str, str]] = set()

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=25,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
            processed_s3_keys=processed_keys,
        )

        # Only 1 document should be parsed (the first one with that S3 key).
        # The other 5 should be skipped as duplicates.
        mock_full_reparse.assert_called_once()

        # 3 rulings inserted (from the single parse), not 6*3 = 18
        assert mock_insert_doc_and_ruling.call_count == 3

        # All 6 rows are "processed" (iterated over), but 5 are skipped
        assert result["processed"] == 6
        assert result["skipped"] == 5
        assert result["updated"] == 1

        # The S3 key should be in the processed set
        assert (shared_s3_key, shared_s3_bucket) in processed_keys

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_s3_dedup_only_applies_in_full_reparse_mode(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """Without full_reparse, S3 key dedup does NOT apply — all rows processed."""
        shared_s3_key = "ca/riverside/calendar/2026-03-05.pdf"
        shared_s3_bucket = "judgemind-docs"

        rows = []
        for i in range(3):
            rows.append(
                _make_document_row(
                    doc_id=uuid.uuid4(),
                    scraper_id="ca-riverside-tentatives-civil",
                    case_number=f"CVPS230600{i}",
                    s3_key=shared_s3_key,
                    s3_bucket=shared_s3_bucket,
                )
            )

        conn = _mock_conn_with_rows(rows)
        mock_fetch_s3.return_value = b"shared pdf content"

        with patch("reingest_from_s3._reparse_document") as mock_reparse:
            mock_reparse.return_value = {
                "ruling_text": "Ruling text",
                "case_number": "CVPS2306000",
                "case_title": "Case v. Defendant",
                "judge_name": "Judge Name",
                "outcome": "granted",
                "motion_type": "demurrer",
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            }
            mock_upsert_case.return_value = "case-id"
            mock_resolve_judge.return_value = "judge-id"

            result = reingest.reingest_batch(
                conn,
                MagicMock(),
                batch_size=25,
                cursor=_DEFAULT_CURSOR,
                filters="",
                filter_params=[],
                full_reparse=False,
                processed_s3_keys=None,
            )

        # All 3 rows should be processed (no dedup in non-full-reparse mode)
        assert mock_reparse.call_count == 3
        assert result["processed"] == 3
        assert result["updated"] == 3
        assert result["skipped"] == 0

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_s3_dedup_across_batches(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """processed_s3_keys set carries dedup state across batch calls."""
        shared_s3_key = "ca/riverside/calendar/2026-03-05.pdf"
        shared_s3_bucket = "judgemind-docs"

        # Batch 1: one row with the shared S3 key
        row1 = _make_document_row(
            doc_id=uuid.uuid4(),
            scraper_id="ca-riverside-tentatives-civil",
            case_number="CVPS2306001",
            s3_key=shared_s3_key,
            s3_bucket=shared_s3_bucket,
        )
        conn1 = _mock_conn_with_rows([row1])
        mock_fetch_s3.return_value = b"shared pdf content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "Ruling 1",
                "case_number": "CVPS2306001",
                "case_title": "Case 1 v. Defendant",
                "judge_name": "Judge",
                "outcome": "granted",
                "motion_type": "demurrer",
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 1,
                "split_document_id": "split-1",
                "is_split": True,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"

        processed_keys: set[tuple[str, str]] = set()

        result1 = reingest.reingest_batch(
            conn1,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
            processed_s3_keys=processed_keys,
        )

        assert result1["processed"] == 1
        assert result1["updated"] == 1
        assert mock_full_reparse.call_count == 1

        # Batch 2: another row with the same S3 key (from cursor pagination)
        row2 = _make_document_row(
            doc_id=uuid.uuid4(),
            captured_at=_CAPTURED_AT_2,
            scraper_id="ca-riverside-tentatives-civil",
            case_number="CVPS2306002",
            s3_key=shared_s3_key,
            s3_bucket=shared_s3_bucket,
        )
        conn2 = _mock_conn_with_rows([row2])

        result2 = reingest.reingest_batch(
            conn2,
            MagicMock(),
            batch_size=10,
            cursor=result1["next_cursor"],
            filters="",
            filter_params=[],
            full_reparse=True,
            processed_s3_keys=processed_keys,
        )

        # Second batch should skip the duplicate S3 key
        assert result2["processed"] == 1
        assert result2["skipped"] == 1
        assert result2["updated"] == 0
        # _full_reparse_document should NOT have been called again
        assert mock_full_reparse.call_count == 1

    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_s3_dedup_different_keys_still_processed(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
    ) -> None:
        """Documents with different S3 keys are all processed normally."""
        rows = [
            _make_document_row(
                doc_id=uuid.uuid4(),
                scraper_id="ca-riverside-tentatives-civil",
                case_number=f"CVPS230600{i}",
                s3_key=f"ca/riverside/calendar/2026-03-0{i}.pdf",
                s3_bucket="judgemind-docs",
            )
            for i in range(3)
        ]

        conn = _mock_conn_with_rows(rows)
        mock_fetch_s3.return_value = b"pdf content"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "Ruling",
                "case_number": "CVPS2306000",
                "case_title": "Case v. Defendant",
                "judge_name": "Judge",
                "outcome": "granted",
                "motion_type": "demurrer",
                "department": "PS1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 0,
                "split_document_id": "doc-id",
                "is_split": False,
                "llm_skipped": True,
                "llm_outcome": "not_attempted",
            },
        ]

        processed_keys: set[tuple[str, str]] = set()

        with (
            patch("reingest_from_s3.upsert_case", return_value="case-id"),
            patch("reingest_from_s3.insert_document_and_ruling"),
            patch("reingest_from_s3.resolve_judge", return_value=None),
            patch("reingest_from_s3.batch_upsert_parties"),
        ):
            result = reingest.reingest_batch(
                conn,
                MagicMock(),
                batch_size=25,
                cursor=_DEFAULT_CURSOR,
                filters="",
                filter_params=[],
                full_reparse=True,
                processed_s3_keys=processed_keys,
            )

        # All 3 rows have different S3 keys — all should be processed
        assert mock_full_reparse.call_count == 3
        assert result["processed"] == 3
        assert result["skipped"] == 0
        assert len(processed_keys) == 3

    @patch("reingest_from_s3._fetch_s3_content")
    def test_s3_prefetch_deduplicates_fetch_calls(
        self,
        mock_fetch_s3: MagicMock,
    ) -> None:
        """S3 prefetch only fetches each unique (s3_key, s3_bucket) once."""
        shared_s3_key = "ca/riverside/calendar/2026-03-05.pdf"
        shared_s3_bucket = "judgemind-docs"

        rows = [
            _make_document_row(
                doc_id=uuid.uuid4(),
                scraper_id="ca-riverside-tentatives-civil",
                case_number=f"CVPS230600{i}",
                s3_key=shared_s3_key,
                s3_bucket=shared_s3_bucket,
            )
            for i in range(6)
        ]

        conn = _mock_conn_with_rows(rows)
        mock_fetch_s3.return_value = b"pdf content"

        processed_keys: set[tuple[str, str]] = set()

        with (
            patch("reingest_from_s3._full_reparse_document") as mock_reparse,
        ):
            mock_reparse.return_value = [
                {
                    "ruling_text": "Ruling",
                    "case_number": "CVPS2306000",
                    "case_title": "Case v. Defendant",
                    "judge_name": "Judge",
                    "outcome": "granted",
                    "motion_type": "demurrer",
                    "department": "PS1",
                    "parties": [],
                    "hearing_date": _HEARING_DATE,
                    "ruling_index": 0,
                    "split_document_id": "doc-id",
                    "is_split": False,
                    "llm_skipped": True,
                    "llm_outcome": "not_attempted",
                },
            ]
            with (
                patch("reingest_from_s3.upsert_case", return_value="case-id"),
                patch("reingest_from_s3.insert_document_and_ruling"),
                patch("reingest_from_s3.resolve_judge", return_value=None),
                patch("reingest_from_s3.batch_upsert_parties"),
            ):
                reingest.reingest_batch(
                    conn,
                    MagicMock(),
                    batch_size=25,
                    cursor=_DEFAULT_CURSOR,
                    filters="",
                    filter_params=[],
                    full_reparse=True,
                    processed_s3_keys=processed_keys,
                )

        # S3 fetch should only be called once despite 6 rows
        mock_fetch_s3.assert_called_once()


# ---------------------------------------------------------------------------
# LLM split exception handling (#1984)
# ---------------------------------------------------------------------------


class TestLlmSplitExceptionFallback:
    """Tests that LLM split exceptions fall back to regex instead of failing.

    When _LLM_SPLIT_REGISTRY contains a split function that raises an
    exception, _full_reparse_document should catch the exception, log a
    warning, and fall back to the regex-based _SPLIT_REGISTRY function.
    See #1984.
    """

    def _doc_meta(self, **overrides: Any) -> dict:
        meta = {
            "document_id": "test-llm-exception-doc",
            "scraper_id": "test-llm-exception",
            "state": "CA",
            "county": "TestCounty",
            "court_name": "TestCounty Superior Court",
            "source_url": "https://example.com/test.pdf",
            "captured_at": datetime(2026, 3, 25, 12, 0, 0),
            "hearing_date": date(2026, 3, 25),
            "format": "pdf",
            "content_hash": "abc123",
            "case_number": "TEST001",
            "case_title": "Smith v. Jones",
            "case_type": "civil",
            "stored_ruling_text": None,
        }
        meta.update(overrides)
        return meta

    @staticmethod
    def _make_split_ruling(
        ruling_index: int,
        case_number: str | None,
        ruling_text: str,
        case_title: str | None,
        motion_type: str | None,
        outcome: str | None,
    ) -> Any:
        """Create a SplitRuling-like object without importing from courts module.

        Uses types.SimpleNamespace to avoid transient CI import failures when
        courts.ca.riverside_tentatives has unresolvable dependencies in some
        test shards.
        """
        import types

        return types.SimpleNamespace(
            ruling_index=ruling_index,
            case_number=case_number,
            ruling_text=ruling_text,
            case_title=case_title,
            motion_type=motion_type,
            outcome=outcome,
        )

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_llm_split_exception_falls_back_to_regex(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """LLM function that raises RuntimeError falls back to regex split."""
        # LLM raises an exception
        mock_llm_split = MagicMock(side_effect=RuntimeError("LLM API error"))

        # Regex fallback returns valid results
        regex_rulings = [
            self._make_split_ruling(1, "TEST001", "DENY MSJ.", "Smith v. Jones", "msj", "denied"),
            self._make_split_ruling(
                2, "TEST002", "GRANT demurrer.", "Doe v. Roe", "demurrer", "granted"
            ),
        ]
        mock_regex_split = MagicMock(return_value=regex_rulings)

        reingest._SPLIT_REGISTRY["test-llm-exception"] = mock_regex_split
        reingest._LLM_SPLIT_REGISTRY["test-llm-exception"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-exception", None)
        mock_extract.return_value = "full pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf", "test-llm-exception", self._doc_meta()
            )
            # LLM was called but raised
            mock_llm_split.assert_called_once_with("full pdf text")
            # Regex fallback was called
            mock_regex_split.assert_called_once_with("full pdf text")
            # Results come from regex
            assert len(result) == 2
            assert result[0]["ruling_text"] == "DENY MSJ."
            assert result[1]["ruling_text"] == "GRANT demurrer."
        finally:
            reingest._SPLIT_REGISTRY.pop("test-llm-exception", None)
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-exception", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_llm_split_generic_exception_falls_back(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Any exception type (ValueError, TypeError, etc.) triggers regex fallback."""
        mock_llm_split = MagicMock(side_effect=ValueError("unexpected JSON"))

        regex_rulings = [
            self._make_split_ruling(
                1, "TEST001", "Ruling text.", "Smith v. Jones", "msj", "denied"
            ),
        ]
        mock_regex_split = MagicMock(return_value=regex_rulings)

        reingest._SPLIT_REGISTRY["test-llm-exception"] = mock_regex_split
        reingest._LLM_SPLIT_REGISTRY["test-llm-exception"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-exception", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf", "test-llm-exception", self._doc_meta()
            )
            mock_llm_split.assert_called_once()
            mock_regex_split.assert_called_once()
            assert len(result) == 1
        finally:
            reingest._SPLIT_REGISTRY.pop("test-llm-exception", None)
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-exception", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_llm_split_success_does_not_trigger_fallback(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When LLM succeeds with 2+ results, regex fallback is NOT called."""
        # Need 2+ rulings to trigger the multi-ruling split path
        # (1 ruling falls through to _reparse_document)
        llm_rulings = [
            self._make_split_ruling(
                1, "TEST001", "Full analysis.", "Smith v. Jones", "msj", "denied"
            ),
            self._make_split_ruling(
                2, "TEST002", "Detailed ruling.", "Doe v. Roe", "demurrer", "granted"
            ),
        ]
        mock_llm_split = MagicMock(return_value=llm_rulings)
        mock_regex_split = MagicMock()

        reingest._SPLIT_REGISTRY["test-llm-exception"] = mock_regex_split
        reingest._LLM_SPLIT_REGISTRY["test-llm-exception"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-exception", None)
        mock_extract.return_value = "pdf text"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf", "test-llm-exception", self._doc_meta()
            )
            mock_llm_split.assert_called_once()
            mock_regex_split.assert_not_called()
            assert len(result) == 2
            assert result[0]["ruling_text"] == "Full analysis."
            assert result[1]["ruling_text"] == "Detailed ruling."
        finally:
            reingest._SPLIT_REGISTRY.pop("test-llm-exception", None)
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-exception", None)


# ---------------------------------------------------------------------------
# LLM-only split registry — no regex fallback (#2007)
# ---------------------------------------------------------------------------


class TestLlmOnlySplitRegistry:
    """Tests for scrapers that only have an LLM split function (no regex).

    Before #2007, scrapers with entries only in _LLM_SPLIT_REGISTRY (and not
    in _SPLIT_REGISTRY) were silently skipped by _full_reparse_document,
    falling back to single-document reparse.  This meant multi-case LA
    documents were never split during --full-reparse reingest.
    """

    def _doc_meta(self, **overrides: Any) -> dict:
        meta = {
            "document_id": "test-llm-only-doc",
            "scraper_id": "test-llm-only",
            "state": "CA",
            "county": "LlmOnly",
            "court_name": "LlmOnly Superior Court",
            "source_url": "https://example.com/test.html",
            "captured_at": datetime(2026, 3, 26, 12, 0, 0),
            "hearing_date": date(2026, 3, 26),
            "format": "html",
            "content_hash": "def456",
            "case_number": "TEST001",
            "case_title": "Alpha v. Beta",
            "case_type": "civil",
            "stored_ruling_text": None,
        }
        meta.update(overrides)
        return meta

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    def test_llm_only_split_is_invoked(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Scraper with only LLM split (no regex) should have LLM invoked."""
        from courts.ca.fresno_tentatives import SplitRuling

        llm_rulings = [
            SplitRuling(1, "TEST001", "First ruling text", "Alpha v. Beta", "msj", "denied", None),
            SplitRuling(
                2, "TEST002", "Second ruling text", "Gamma v. Delta", "demurrer", "granted", None
            ),
        ]
        mock_llm_split = MagicMock(return_value=llm_rulings)

        # Only register in LLM registry — no regex fallback.
        reingest._SPLIT_REGISTRY.pop("test-llm-only", None)
        reingest._LLM_SPLIT_REGISTRY["test-llm-only"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-only", None)
        mock_extract.return_value = "full html text"

        try:
            result = reingest._full_reparse_document(b"raw html", "test-llm-only", self._doc_meta())
            mock_llm_split.assert_called_once_with("full html text")
            assert len(result) == 2
            assert result[0]["ruling_text"] == "First ruling text"
            assert result[1]["ruling_text"] == "Second ruling text"
        finally:
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-only", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    @patch.object(reingest, "_reparse_document")
    def test_llm_only_failure_falls_back_to_single_doc(
        self,
        mock_reparse: MagicMock,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When LLM split fails and no regex fallback, falls back to single doc."""
        mock_llm_split = MagicMock(return_value=None)

        reingest._SPLIT_REGISTRY.pop("test-llm-only", None)
        reingest._LLM_SPLIT_REGISTRY["test-llm-only"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-only", None)
        mock_extract.return_value = "full html text"

        # When split_results is empty, _full_reparse_document falls back
        # to _reparse_document for single-doc processing.
        mock_reparse.return_value = {
            "ruling_text": "single doc text",
            "case_number": "TEST001",
            "case_title": "Alpha v. Beta",
            "hearing_date": None,
            "judge_name": None,
            "case_type": None,
            "outcome": None,
            "motion_type": None,
        }

        try:
            result = reingest._full_reparse_document(b"raw html", "test-llm-only", self._doc_meta())
            mock_llm_split.assert_called_once()
            # Empty split_results -> len <= 1 -> standard reparse fallback.
            assert len(result) == 1
            assert result[0]["is_split"] is False
        finally:
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-only", None)

    @patch.object(reingest, "_load_scraper_registry")
    @patch.object(reingest, "_extract_text_from_content")
    @patch.object(reingest, "_reparse_document")
    def test_llm_only_exception_falls_back_to_single_doc(
        self,
        mock_reparse: MagicMock,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """When LLM split raises and no regex fallback, falls back to single doc."""
        mock_llm_split = MagicMock(side_effect=ValueError("LLM API error"))

        reingest._SPLIT_REGISTRY.pop("test-llm-only", None)
        reingest._LLM_SPLIT_REGISTRY["test-llm-only"] = mock_llm_split
        reingest._SCRAPER_REGISTRY.pop("test-llm-only", None)
        mock_extract.return_value = "full html text"

        mock_reparse.return_value = {
            "ruling_text": "single doc text",
            "case_number": "TEST001",
            "case_title": "Alpha v. Beta",
            "hearing_date": None,
            "judge_name": None,
            "case_type": None,
            "outcome": None,
            "motion_type": None,
        }

        try:
            result = reingest._full_reparse_document(b"raw html", "test-llm-only", self._doc_meta())
            mock_llm_split.assert_called_once()
            # Empty split_results -> len <= 1 -> standard reparse fallback.
            assert len(result) == 1
            assert result[0]["is_split"] is False
        finally:
            reingest._LLM_SPLIT_REGISTRY.pop("test-llm-only", None)
