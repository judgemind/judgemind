"""Tests for the backfill_la_rulings script.

All database and S3 access is mocked — these tests verify the splitting,
cleanup, and field extraction logic without requiring live infrastructure.
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
backfill_la = importlib.import_module("backfill_la_rulings")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_COURT_ID = str(uuid.uuid4())
_CASE_ID = str(uuid.uuid4())
_RULING_ID = str(uuid.uuid4())
_DOCUMENT_ID = str(uuid.uuid4())
_HEARING_DATE = date(2026, 3, 5)
_S3_KEY = "ca/los_angeles/superior_court/raw/2026/03/05/test-doc.html"
_BUCKET = "judgemind-document-archive-dev"

# Single-case ruling HTML (wraps content in speechSynthesis div)
SINGLE_CASE_HTML = """<html><body>
<div id="speechSynthesis">
<B> Case Number: </B>24STCV01234
<P>
SUMAYYA AASI,
  Plaintiff(s),
  vs.
  AMERICAN HONDA MOTOR CO.,
  Defendant(s).
</P>
<P>The motion for summary judgment is GRANTED.</P>
<div>William A. Crowfoot Judge of the Superior Court</div>
</div>
</body></html>"""

# Multi-case ruling HTML (two cases separated by HR)
MULTI_CASE_HTML = """<html><body>
<div id="speechSynthesis">
<B> Case Number: </B>24STCV01234
<P>
JOHN DOE,
  Plaintiff(s),
  vs.
  ACME CORP.,
  Defendant(s).
</P>
<P>The demurrer is DENIED.</P>
<div>William A. Crowfoot Judge of the Superior Court</div>
<HR>
<P>
<B> Case Number: </B>24STCV05678
<P>
JANE SMITH,
  Plaintiff(s),
  vs.
  MEGACORP INC.,
  Defendant(s).
</P>
<P>The motion to compel is GRANTED.</P>
<div>William A. Crowfoot Judge of the Superior Court</div>
</div>
</body></html>"""


def _make_ruling_row(
    s3_key: str = _S3_KEY,
    s3_bucket: str | None = _BUCKET,
    case_number: str = "24STCV01234",
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        _RULING_ID,  # r.id
        _CASE_ID,  # r.case_id
        _COURT_ID,  # r.court_id
        _HEARING_DATE,  # r.hearing_date
        "1",  # r.department
        _DOCUMENT_ID,  # d.id
        s3_key,  # d.s3_key
        s3_bucket,  # d.s3_bucket
        "https://example.com",  # d.source_url
        "ca-la-tentatives",  # d.scraper_id
        datetime(2026, 3, 5, 18, 0),  # d.captured_at
        case_number,  # c.case_number
    )


def _mock_cursor_factory(fetch_rows: list[tuple]) -> MagicMock:
    """Create a mock connection with a cursor that returns fetch_rows on first call."""
    conn = MagicMock()
    cursors: list[MagicMock] = []

    # First cursor call returns the fetch cursor
    fetch_cur = MagicMock()
    fetch_cur.fetchall.return_value = fetch_rows
    cursors.append(fetch_cur)

    # Subsequent cursors are update cursors
    call_index = [0]

    def cursor_ctx() -> MagicMock:
        ctx = MagicMock()
        if call_index[0] < len(cursors):
            cur = cursors[call_index[0]]
        else:
            cur = MagicMock()
        call_index[0] += 1
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    conn.cursor.side_effect = cursor_ctx
    return conn


# ---------------------------------------------------------------------------
# _FieldBag tests
# ---------------------------------------------------------------------------


class TestFieldBag:
    """Tests for the _FieldBag helper class."""

    def test_default_values(self) -> None:
        bag = backfill_la._FieldBag()
        assert bag.ruling_text is None
        assert bag.case_number is None
        assert bag.case_title is None
        assert bag.judge_name is None
        assert bag.parties == []
        assert bag.extra == {}

    def test_writable_fields(self) -> None:
        bag = backfill_la._FieldBag()
        bag.ruling_text = "test text"
        bag.case_number = "24STCV01234"
        assert bag.ruling_text == "test text"
        assert bag.case_number == "24STCV01234"


# ---------------------------------------------------------------------------
# process_ruling tests
# ---------------------------------------------------------------------------


class TestProcessRuling:
    """Tests for process_ruling()."""

    @patch("backfill_la_rulings.upsert_case_party")
    @patch("backfill_la_rulings.upsert_party")
    @patch("backfill_la_rulings.upsert_case_judge")
    @patch("backfill_la_rulings.resolve_judge")
    @patch("backfill_la_rulings.fetch_s3_content")
    def test_single_case_updates_existing(
        self,
        mock_fetch: MagicMock,
        mock_resolve: MagicMock,
        mock_cj: MagicMock,
        mock_up: MagicMock,
        mock_ucp: MagicMock,
    ) -> None:
        """Single-case ruling re-cleans text and re-extracts fields."""
        mock_fetch.return_value = SINGLE_CASE_HTML.encode("utf-8")
        mock_resolve.return_value = "judge-uuid-123"
        mock_up.return_value = "party-uuid-456"

        conn = MagicMock()
        # Multiple cursor calls for UPDATE queries
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        result = backfill_la.process_ruling(
            conn,
            MagicMock(),
            _BUCKET,
            _RULING_ID,
            _CASE_ID,
            _COURT_ID,
            _HEARING_DATE,
            "1",
            _DOCUMENT_ID,
            _S3_KEY,
            _BUCKET,
            "https://example.com",
            "ca-la-tentatives",
            datetime(2026, 3, 5),
            "24STCV01234",
        )

        assert result["updated"] == 1
        assert result["split_created"] == 0
        assert result["skipped"] == 0

        # Judge should have been resolved
        mock_resolve.assert_called_once()

        # Case judge link should have been created
        mock_cj.assert_called_once()

    @patch("backfill_la_rulings.insert_ruling")
    @patch("backfill_la_rulings.insert_document")
    @patch("backfill_la_rulings.upsert_case")
    @patch("backfill_la_rulings.upsert_case_party")
    @patch("backfill_la_rulings.upsert_party")
    @patch("backfill_la_rulings.upsert_case_judge")
    @patch("backfill_la_rulings.resolve_judge")
    @patch("backfill_la_rulings.fetch_s3_content")
    def test_multi_case_creates_split_records(
        self,
        mock_fetch: MagicMock,
        mock_resolve: MagicMock,
        mock_cj: MagicMock,
        mock_up: MagicMock,
        mock_ucp: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc: MagicMock,
        mock_insert_ruling: MagicMock,
    ) -> None:
        """Multi-case ruling creates new records for additional cases."""
        mock_fetch.return_value = MULTI_CASE_HTML.encode("utf-8")
        mock_resolve.return_value = "judge-uuid-123"
        mock_up.return_value = "party-uuid-456"
        mock_upsert_case.return_value = str(uuid.uuid4())
        mock_insert_doc.return_value = True  # new document

        conn = MagicMock()
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        result = backfill_la.process_ruling(
            conn,
            MagicMock(),
            _BUCKET,
            _RULING_ID,
            _CASE_ID,
            _COURT_ID,
            _HEARING_DATE,
            "1",
            _DOCUMENT_ID,
            _S3_KEY,
            _BUCKET,
            "https://example.com",
            "ca-la-tentatives",
            datetime(2026, 3, 5),
            "24STCV01234",
        )

        assert result["updated"] == 1
        assert result["split_created"] == 1

        # upsert_case called for the split-out case
        mock_upsert_case.assert_called_once()

        # insert_document called for the new split document
        mock_insert_doc.assert_called_once()

        # insert_ruling called for the new split ruling
        mock_insert_ruling.assert_called_once()

    @patch("backfill_la_rulings.fetch_s3_content")
    def test_s3_fetch_failure_skips(self, mock_fetch: MagicMock) -> None:
        """When S3 fetch fails, the ruling is skipped."""
        mock_fetch.return_value = None

        conn = MagicMock()
        result = backfill_la.process_ruling(
            conn,
            MagicMock(),
            _BUCKET,
            _RULING_ID,
            _CASE_ID,
            _COURT_ID,
            _HEARING_DATE,
            "1",
            _DOCUMENT_ID,
            _S3_KEY,
            _BUCKET,
            "https://example.com",
            "ca-la-tentatives",
            datetime(2026, 3, 5),
            "24STCV01234",
        )

        assert result["skipped"] == 1
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# backfill_batch tests
# ---------------------------------------------------------------------------


class TestBackfillBatch:
    """Tests for backfill_batch()."""

    @patch("backfill_la_rulings.process_ruling")
    def test_empty_batch(self, mock_process: MagicMock) -> None:
        conn = _mock_cursor_factory([])
        stats = backfill_la.backfill_batch(conn, MagicMock(), _BUCKET, 50, 0)
        assert stats["processed"] == 0
        mock_process.assert_not_called()

    @patch("backfill_la_rulings.process_ruling")
    def test_processes_rows(self, mock_process: MagicMock) -> None:
        mock_process.return_value = {"updated": 1, "split_created": 0, "skipped": 0}
        row = _make_ruling_row()
        conn = _mock_cursor_factory([row])

        stats = backfill_la.backfill_batch(conn, MagicMock(), _BUCKET, 50, 0)
        assert stats["processed"] == 1
        assert stats["updated"] == 1
        mock_process.assert_called_once()

    @patch("backfill_la_rulings.process_ruling")
    def test_error_handling(self, mock_process: MagicMock) -> None:
        """Errors processing individual rulings are caught and counted as skips."""
        mock_process.side_effect = RuntimeError("test error")
        row = _make_ruling_row()
        conn = _mock_cursor_factory([row])

        stats = backfill_la.backfill_batch(conn, MagicMock(), _BUCKET, 50, 0)
        assert stats["processed"] == 1
        assert stats["skipped"] == 1


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill()."""

    @patch("backfill_la_rulings.boto3")
    @patch("backfill_la_rulings.psycopg")
    @patch("backfill_la_rulings.backfill_batch")
    def test_dry_run_rolls_back(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.side_effect = [
            {"processed": 50, "updated": 40, "split_created": 5, "skipped": 5},
            {"processed": 10, "updated": 8, "split_created": 1, "skipped": 1},
        ]

        stats = backfill_la.run_backfill("postgresql://test", dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 60
        assert stats["total_updated"] == 48
        assert stats["total_split_created"] == 6

    @patch("backfill_la_rulings.boto3")
    @patch("backfill_la_rulings.psycopg")
    @patch("backfill_la_rulings.backfill_batch")
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

        mock_batch.side_effect = [
            {"processed": 50, "updated": 45, "split_created": 3, "skipped": 2},
            {"processed": 20, "updated": 18, "split_created": 1, "skipped": 1},
        ]

        stats = backfill_la.run_backfill("postgresql://test")

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 70
        assert stats["total_updated"] == 63

    @patch("backfill_la_rulings.boto3")
    @patch("backfill_la_rulings.psycopg")
    @patch("backfill_la_rulings.backfill_batch")
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

        mock_batch.side_effect = [
            {"processed": 10, "updated": 8, "split_created": 1, "skipped": 1},
        ]

        stats = backfill_la.run_backfill(
            "postgresql://test",
            batch_size=50,
            limit=10,
        )

        call_args = mock_batch.call_args_list[0]
        assert call_args[0][3] == 10  # effective_batch = min(50, 10)
        assert stats["total_processed"] == 10
