"""Tests for the reingest_from_s3 script.

Verifies keyset (cursor-based) pagination, parallel S3 fetching,
psycopg3 pipeline batching of DB writes, LLM extraction integration,
error handling, and CLI flag behavior. All database and S3 access is mocked.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from datetime import date, datetime
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
        _HEARING_DATE,  # d.hearing_date
        "html",  # d.format
        "CA",  # ct.state
        "Los Angeles",  # ct.county
        "Los Angeles Superior Court",  # ct.court_name
        case_number,  # c.case_number
        case_title,  # c.case_title
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

        processed, updated, next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert processed == 0
        assert updated == 0
        assert next_cursor == _DEFAULT_CURSOR

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

        processed, updated, next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert processed == 2
        assert next_cursor == (_CAPTURED_AT_2, str(_DOC_ID_2))

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

        processed, updated, next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert processed == 1
        assert updated == 0
        assert next_cursor == (_CAPTURED_AT_1, str(_DOC_ID_1))
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

        processed, updated, _cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert processed == 1
        assert updated == 0
        conn.transaction.assert_not_called()

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.insert_ruling")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_transaction_context_used_for_db_writes(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_insert_ruling: MagicMock,
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

        processed, updated, _cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert processed == 1
        assert updated == 1
        conn.transaction.assert_called_once()
        mock_upsert_case.assert_called_once()
        mock_insert_doc.assert_called_once()
        mock_resolve_judge.assert_called_once()
        mock_insert_ruling.assert_called_once()
        mock_upsert_cj.assert_called_once()

    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.insert_ruling")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_case_id_flows_through_pipeline(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_insert_ruling: MagicMock,
        mock_upsert_cj: MagicMock,
    ) -> None:
        """The case_id from upsert_case flows to insert_document and insert_ruling."""
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

        insert_doc_kwargs = mock_insert_doc.call_args[1]
        assert insert_doc_kwargs["case_id"] == "pipeline-case-id"

        insert_ruling_kwargs = mock_insert_ruling.call_args[1]
        assert insert_ruling_kwargs["case_id"] == "pipeline-case-id"
        assert insert_ruling_kwargs["judge_id"] == "pipeline-judge-id"

        mock_upsert_cj.assert_called_once_with(
            conn,
            "pipeline-case-id",
            "pipeline-judge-id",
            _HEARING_DATE,
        )


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

        processed, updated, _next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=3,
        )

        assert processed == 3
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

        processed, updated, _next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=3,
        )

        assert processed == 3
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

        processed, updated, _next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=3,
        )

        assert processed == 3
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

        processed, updated, _next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            concurrency=1,
        )

        assert processed == 2
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

        mock_batch.return_value = (0, 0, _DEFAULT_CURSOR)

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
            (50, 40, cursor_1),
            (50, 30, cursor_2),
            (10, 5, cursor_2),
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
            (50, 40, cursor_1),
            (10, 5, cursor_1),
        ]

        stats = reingest.run_reingest("postgresql://test", batch_size=50, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 60
        assert stats["total_updated"] == 45

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_commits_per_batch(
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
            (50, 40, cursor_1),
            (30, 20, cursor_1),
        ]

        stats = reingest.run_reingest("postgresql://test", batch_size=50)

        assert mock_conn.commit.call_count == 2
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
        mock_batch.return_value = (30, 20, cursor_1)

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

        mock_batch.return_value = (5, 3, _DEFAULT_CURSOR)

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

        mock_batch.return_value = (5, 3, _DEFAULT_CURSOR)

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

        mock_batch.return_value = (0, 0, _DEFAULT_CURSOR)

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

        mock_batch.return_value = (0, 0, _DEFAULT_CURSOR)

        reingest.run_reingest(
            "postgresql://test",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 3, 1),
        )

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        assert "AND d.captured_at >= %s" in filters_arg
        assert "AND d.captured_at <= %s" in filters_arg


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

        processed, updated, _next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            dry_run=True,
            parse_workers=2,
        )

        assert processed == 3
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

        processed, updated, _next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            parse_workers=1,
        )

        assert processed == 3
        # Only 2 succeed, so only 2 are written to DB
        assert updated == 2

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

        processed, updated, _next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            parse_workers=1,
        )

        assert processed == 2
        assert updated == 1

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

        mock_batch.return_value = (5, 3, _DEFAULT_CURSOR)

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

        mock_batch.return_value = (5, 3, _DEFAULT_CURSOR)

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

        mock_batch.return_value = (5, 3, _DEFAULT_CURSOR)

        reingest.run_reingest("postgresql://test", batch_size=50)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("parse_workers") == 4

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_default_batch_size_is_200(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Default batch_size is 200."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = (0, 0, _DEFAULT_CURSOR)

        reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        # batch_size is the 3rd positional arg (conn, s3, batch_size, ...)
        assert batch_call[0][2] == 200


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
        """Registry should contain all scraper modules that have default_config()."""
        reingest._load_scraper_registry()

        # These are the scraper_ids from the known court modules
        expected_ids = {
            "ca-la-tentatives-civil",
            "ca-oc-tentatives-civil",
            "ca-oc-tentatives-family-law",
            "ca-oc-tentatives-probate",
            "ca-sb-tentatives-civil",
            "ca-sf-tentatives-family-law",
            "ca-sc-tentatives-civil",
            "ca-riverside-tentatives-civil",
        }
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

    def test_unknown_format_decoded_as_utf8(self) -> None:
        """Unknown format is decoded as UTF-8."""
        content = b"some text content"
        result = reingest._extract_text_from_content(content, "text")
        assert result == "some text content"


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
            raw, "unknown-scraper", self._doc_meta(), anthropic_client=client
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
    def test_llm_not_called_without_client(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """LLM extraction is skipped when anthropic_client is None."""
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
            raw = b"<html>ruling text</html>"
            client = MagicMock()
            reingest._reparse_document(raw, "test-all-filled", meta, anthropic_client=client)
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
            raw, "unknown-scraper", self._doc_meta(), anthropic_client=client
        )

        # LLM was called but returned None — regex should have been tried
        mock_llm.assert_called_once()
        # extraction_methods should use regex for whatever regex found
        methods = result["extraction_methods"]
        for field_method in methods.values():
            assert field_method == "regex"

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
            result = reingest._reparse_document(raw, "test-partial", meta, anthropic_client=client)

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
# LLM extraction in reingest_batch — anthropic_client passthrough
# ---------------------------------------------------------------------------


class TestReingestBatchLLM:
    """Tests that reingest_batch passes anthropic_client through to parsing."""

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_anthropic_client_passed_to_reparse(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """anthropic_client is forwarded from reingest_batch to _reparse_document."""
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
            anthropic_client=mock_client,
        )

        # Verify _reparse_document was called with the anthropic_client
        call_args = mock_reparse.call_args[0]
        assert call_args[4] is mock_client

    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_no_anthropic_client_by_default(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
    ) -> None:
        """By default, anthropic_client is None (no LLM extraction)."""
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
        mock_batch.return_value = (0, 0, _DEFAULT_CURSOR)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            reingest.run_reingest("postgresql://test", no_llm=True)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("anthropic_client") is None

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_no_api_key_disables_llm(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Without ANTHROPIC_API_KEY, anthropic_client is None."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = (0, 0, _DEFAULT_CURSOR)

        with patch.dict(os.environ, {}, clear=True):
            # Ensure ANTHROPIC_API_KEY is not set
            os.environ.pop("ANTHROPIC_API_KEY", None)
            reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("anthropic_client") is None

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_api_key_creates_client(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """With ANTHROPIC_API_KEY set, an Anthropic client is created and passed."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = (0, 0, _DEFAULT_CURSOR)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key-123"}):
            reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        client = batch_call.kwargs.get("anthropic_client")
        assert client is not None


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
    @patch("reingest_from_s3.insert_ruling")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_bad_document_skipped_others_succeed(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_insert_ruling: MagicMock,
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

        processed, updated, _cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        # Both documents were processed (fetched + parsed)
        assert processed == 2
        # Only one was successfully written (the second one)
        assert updated == 1

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.insert_ruling")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_all_docs_fail_returns_zero_updated(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_insert_ruling: MagicMock,
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

        processed, updated, _cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert processed == 1
        assert updated == 0
