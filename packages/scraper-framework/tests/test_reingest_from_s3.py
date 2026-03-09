"""Tests for the reingest_from_s3 script.

Verifies keyset (cursor-based) pagination replaces LIMIT/OFFSET,
ensuring constant per-batch performance and correct CLI flag behavior.
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
    s3_key: str = "docs/test.html",
    s3_bucket: str = "test-bucket",
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
# reingest_batch tests
# ---------------------------------------------------------------------------


class TestReingestBatch:
    """Tests for reingest_batch() with cursor-based pagination."""

    def test_no_rows_returns_zero_and_same_cursor(self) -> None:
        """Empty batch returns zero counts and unchanged cursor."""
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn.cursor.return_value = _mock_cursor_context(cur)

        initial_cursor = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)
        processed, updated, next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=initial_cursor,
            filters="",
            filter_params=[],
        )

        assert processed == 0
        assert updated == 0
        assert next_cursor == initial_cursor

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

        # Verify the execute call has cursor params + batch_size
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

        # Use a single cursor context for the fetch, then additional for inserts
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

        initial_cursor = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)
        processed, updated, next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=initial_cursor,
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

        initial_cursor = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)
        processed, updated, next_cursor = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=initial_cursor,
            filters="",
            filter_params=[],
            dry_run=True,
        )

        assert processed == 1
        assert updated == 0
        # Cursor still advances past the skipped row
        assert next_cursor == (_CAPTURED_AT_1, str(_DOC_ID_1))
        mock_fetch_s3.assert_not_called()


# ---------------------------------------------------------------------------
# run_reingest tests
# ---------------------------------------------------------------------------


class TestRunReingest:
    """Tests for run_reingest() end-to-end flow with keyset pagination."""

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

        cursor_min = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)
        mock_batch.return_value = (0, 0, cursor_min)

        reingest.run_reingest("postgresql://test", batch_size=50)

        # First call should use minimum cursor
        first_call = mock_batch.call_args_list[0]
        cursor_arg = first_call[0][3]  # 4th positional arg is cursor
        assert cursor_arg == (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)

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

        # Two full batches, then a partial batch
        mock_batch.side_effect = [
            (50, 40, cursor_1),
            (50, 30, cursor_2),
            (10, 5, cursor_2),
        ]

        reingest.run_reingest("postgresql://test", batch_size=50)

        # Verify cursor progression
        calls = mock_batch.call_args_list
        assert calls[0][0][3] == (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)
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

        # effective_batch should be capped to 30
        call_args = mock_batch.call_args_list[0]
        assert call_args[0][2] == 30  # batch_size arg
        assert stats["total_processed"] == 30

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

        cursor_min = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)
        mock_batch.return_value = (0, 0, cursor_min)

        reingest.run_reingest("postgresql://test", county="Orange")

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]  # filters string
        filter_params_arg = call_args[0][5]  # filter_params list
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

        cursor_min = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)
        mock_batch.return_value = (0, 0, cursor_min)

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
