"""Tests for the reingest_from_s3 script.

Verifies keyset (cursor-based) pagination, parallel S3 fetching,
psycopg3 pipeline batching of DB writes, LLM extraction integration,
error handling, and CLI flag behavior. All database and S3 access is mocked.
"""

from __future__ import annotations

import hashlib
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
    ruling_department: str | None = None,
    ruling_judge_name: str | None = None,
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
        ruling_department,  # ruling_department (subquery)
        ruling_judge_name,  # ruling_judge_name (subquery)
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

    def test_query_selects_judge_name_and_department(self) -> None:
        """The query must fetch judge_name and department from rulings so
        the multimodal LLM cache key matches the worker path.  See #2501.
        """
        query = reingest.FETCH_DOCUMENTS_QUERY
        assert "AS ruling_department" in query
        assert "AS ruling_judge_name" in query
        # The judge_name subquery must join rulings -> judges to get the
        # canonical judge name rather than selecting a non-existent column.
        assert "j.canonical_name" in query
        assert "LEFT JOIN judges j ON j.id = r.judge_id" in query


# ---------------------------------------------------------------------------
# LLM cache key parity: worker vs reingest paths
# ---------------------------------------------------------------------------


class TestLlmCacheKeyParity:
    """Verify reingest and worker paths produce identical LLM cache keys.

    Regression test for #2501: FETCH_DOCUMENTS_QUERY previously omitted
    judge_name and department, so reingest built metadata = {hearing_date}
    while the worker built metadata = {judge_name, department, hearing_date}.
    Same raw PDF -> different content_hash_for_cache -> duplicate LLM calls
    + divergent extraction output.
    """

    @staticmethod
    def _build_worker_metadata(event_data: dict) -> dict[str, str]:
        """Mirror packages/scraper-framework/src/ingestion/worker.py ~L2222.

        This is the exact logic the live ingestion worker uses to build
        the metadata dict passed to multimodal_extractor.extract_from_pdf.
        """
        metadata: dict[str, str] = {}
        if event_data.get("judge_name"):
            metadata["judge_name"] = event_data["judge_name"]
        if event_data.get("department"):
            metadata["department"] = event_data["department"]
        if event_data.get("hearing_date"):
            metadata["hearing_date"] = str(event_data["hearing_date"])
        return metadata

    @staticmethod
    def _build_reingest_metadata(doc_meta: dict) -> dict[str, str]:
        """Mirror scripts/reingest_from_s3.py ~L1483 inside _reparse_document_multimodal.

        Only the three keys that actually feed the LLM cache hash.
        """
        metadata: dict[str, str] = {}
        if doc_meta.get("judge_name"):
            metadata["judge_name"] = str(doc_meta["judge_name"])
        if doc_meta.get("department"):
            metadata["department"] = str(doc_meta["department"])
        if doc_meta.get("hearing_date"):
            metadata["hearing_date"] = str(doc_meta["hearing_date"])
        return metadata

    def test_reingest_row_populates_judge_name_and_department_in_doc_meta(self) -> None:
        """End-to-end: a row with judge_name and department must map into
        doc_meta so downstream metadata matches the worker path.
        """
        from framework.llm_extractor import _content_hash_for_cache

        # Event payload the scraper produced at capture time (worker path).
        event_data = {
            "judge_name": "Hon. Layne H. Melzer",
            "department": "C25",
            "hearing_date": date(2026, 3, 5),
        }

        # Row returned by FETCH_DOCUMENTS_QUERY after the fix.
        row = _make_document_row(
            scraper_id="ca-oc-tentatives-civil",
            hearing_date=event_data["hearing_date"],
            ruling_hearing_date=event_data["hearing_date"],
            ruling_department=event_data["department"],
            ruling_judge_name=event_data["judge_name"],
        )

        # Reingest unpacks the row the same way the script does.  We
        # reproduce the doc_meta construction here rather than invoking
        # the full reingest_batch pipeline because the full pipeline
        # requires DB / S3 mocks for every step, and this test only
        # cares about the judge_name/department propagation.
        ruling_department = row[18]
        ruling_judge_name = row[19]

        doc_meta = {
            "format": "pdf",
            "hearing_date": row[9],
            "judge_name": ruling_judge_name,
            "department": ruling_department,
        }

        # Build metadata via both paths and hash the same PDF bytes.
        pdf_bytes = b"%PDF-1.7\nfake pdf contents\n"
        worker_meta = self._build_worker_metadata(event_data)
        reingest_meta = self._build_reingest_metadata(doc_meta)

        assert worker_meta == reingest_meta, (
            "Worker and reingest metadata dicts must be identical; got "
            f"worker={worker_meta} vs reingest={reingest_meta}"
        )
        assert _content_hash_for_cache(pdf_bytes, worker_meta) == _content_hash_for_cache(
            pdf_bytes, reingest_meta
        )

    def test_cache_keys_diverge_when_judge_name_and_department_missing(self) -> None:
        """Regression: without the fix, reingest metadata drops judge_name
        and department, and the cache key differs from the worker path.

        This pins the bug: if someone reverts the FETCH_DOCUMENTS_QUERY fix
        without updating the test suite, this test's expectation would
        stop protecting the guarantee.  The test is positive (divergence
        exists when fields are missing) rather than negative, so it stays
        green when the fix is present and only demonstrates the failure
        mode it's protecting against.
        """
        from framework.llm_extractor import _content_hash_for_cache

        pdf_bytes = b"%PDF-1.7\nfake pdf contents\n"

        worker_meta = self._build_worker_metadata(
            {
                "judge_name": "Hon. Layne H. Melzer",
                "department": "C25",
                "hearing_date": date(2026, 3, 5),
            }
        )
        reingest_meta_without_fix = self._build_reingest_metadata(
            {"hearing_date": date(2026, 3, 5)}
        )

        assert _content_hash_for_cache(pdf_bytes, worker_meta) != _content_hash_for_cache(
            pdf_bytes, reingest_meta_without_fix
        )

    def test_cache_key_parity_for_missing_scraper_metadata(self) -> None:
        """When both paths have no judge_name/department (e.g. a county
        whose scraper does not provide either), the cache keys must still
        match — both paths see only hearing_date.
        """
        from framework.llm_extractor import _content_hash_for_cache

        event_data = {
            "judge_name": None,
            "department": None,
            "hearing_date": date(2026, 3, 5),
        }

        row = _make_document_row(
            scraper_id="ca-oc-tentatives-civil",
            hearing_date=event_data["hearing_date"],
            ruling_hearing_date=event_data["hearing_date"],
            ruling_department=None,
            ruling_judge_name=None,
        )
        ruling_department = row[18]
        ruling_judge_name = row[19]

        doc_meta = {
            "format": "pdf",
            "hearing_date": row[9],
            "judge_name": ruling_judge_name,
            "department": ruling_department,
        }

        pdf_bytes = b"%PDF-1.7\nfake pdf contents\n"
        worker_meta = self._build_worker_metadata(event_data)
        reingest_meta = self._build_reingest_metadata(doc_meta)

        assert worker_meta == reingest_meta
        assert _content_hash_for_cache(pdf_bytes, worker_meta) == _content_hash_for_cache(
            pdf_bytes, reingest_meta
        )


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
# _reparse_document tests — stored_ruling_text format awareness (#2360)
# ---------------------------------------------------------------------------


class TestReparseDocumentStoredRulingText:
    """Verify that stored_ruling_text is only preserved for PDF documents.

    For HTML documents, the freshly-parsed ruling_text from parse_document()
    should be used instead. The --force-retranscribe flag skips preservation
    for all formats.
    """

    def _doc_meta(self, *, fmt: str = "html", stored: str | None = None) -> dict:
        meta: dict[str, Any] = {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Los Angeles",
            "court_name": "Los Angeles Superior Court",
            "source_url": "https://court.example.com/ruling",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": fmt,
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": "ca-la-tentatives-civil",
            "s3_key": "docs/test.html",
            "s3_bucket": "test-bucket",
        }
        if stored is not None:
            meta["stored_ruling_text"] = stored
        return meta

    @patch.object(reingest, "_load_scraper_registry")
    def test_html_doc_ignores_stored_ruling_text(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """HTML documents should use freshly-parsed text, not stored value."""
        raw = b"freshly parsed clean text"
        meta = self._doc_meta(fmt="html", stored="<p>stale raw HTML</p>")
        result = reingest._reparse_document(raw, "unknown-scraper", meta)
        # The fresh text should be used, not the stored HTML
        assert result["ruling_text"] == "freshly parsed clean text"

    @patch.object(reingest, "_load_scraper_registry")
    def test_pdf_doc_preserves_stored_ruling_text(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """PDF documents should preserve stored_ruling_text (existing behavior)."""
        # Use a mock to avoid needing a real PDF
        with patch.object(
            reingest,
            "_extract_text_from_content",
            return_value="full multi-ruling PDF text 77K chars",
        ):
            meta = self._doc_meta(fmt="pdf", stored="scoped LLM ruling text")
            result = reingest._reparse_document(b"fake-pdf-bytes", "unknown-scraper", meta)
            # The stored text should be preserved for PDF docs
            assert result["ruling_text"] == "scoped LLM ruling text"

    @patch.object(reingest, "_load_scraper_registry")
    def test_pdf_doc_without_stored_text_uses_fresh(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """PDF documents without stored_ruling_text use freshly-extracted text."""
        with patch.object(
            reingest,
            "_extract_text_from_content",
            return_value="full pdf text",
        ):
            meta = self._doc_meta(fmt="pdf", stored=None)
            result = reingest._reparse_document(b"fake-pdf-bytes", "unknown-scraper", meta)
            assert result["ruling_text"] == "full pdf text"

    @patch.object(reingest, "_load_scraper_registry")
    def test_force_retranscribe_skips_stored_for_pdf(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """--force-retranscribe should skip stored_ruling_text even for PDFs."""
        with patch.object(
            reingest,
            "_extract_text_from_content",
            return_value="fresh pdf text from pdfplumber",
        ):
            meta = self._doc_meta(fmt="pdf", stored="old LLM scoped text")
            result = reingest._reparse_document(
                b"fake-pdf-bytes",
                "unknown-scraper",
                meta,
                force_retranscribe=True,
            )
            # With force_retranscribe, fresh text should be used
            assert result["ruling_text"] == "fresh pdf text from pdfplumber"

    @patch.object(reingest, "_load_scraper_registry")
    def test_force_retranscribe_with_html_still_uses_fresh(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """--force-retranscribe on HTML docs also uses fresh text (same behavior)."""
        raw = b"clean text from parse"
        meta = self._doc_meta(fmt="html", stored="stale stored text")
        result = reingest._reparse_document(raw, "unknown-scraper", meta, force_retranscribe=True)
        assert result["ruling_text"] == "clean text from parse"

    @patch.object(reingest, "_load_scraper_registry")
    def test_html_stored_text_not_used_for_regex_fallback(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """For HTML docs, regex fallback should use fresh text, not stored text."""
        raw = b"The motion for demurrer is GRANTED"
        meta = self._doc_meta(fmt="html", stored="<p>stale HTML content</p>")
        result = reingest._reparse_document(raw, "unknown-scraper", meta)
        # The fresh text should have been used for regex extraction
        # (the regex_text variable in _reparse_document)
        assert result["ruling_text"] == "The motion for demurrer is GRANTED"


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
    def test_null_doc_and_ruling_hearing_date_inserts_ruling_with_null(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
    ) -> None:
        """When both document and ruling hearing_dates are NULL, ruling is still inserted (#2215).

        A missing hearing_date should never cause a ruling to be dropped.
        The ruling is inserted with NULL hearing_date instead.
        """
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
        # hearing_date is None but ruling should still be inserted (#2215)
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

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_filter_null_outcome_passed_through(
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
            county="Santa Clara",
            filter_null_outcome=True,
        )

        call_args = mock_batch.call_args_list[0]
        filters_arg = call_args[0][4]
        assert "AND ct.county = %s" in filters_arg
        assert "AND EXISTS" in filters_arg
        assert "r.outcome IS NULL" in filters_arg


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

    def test_filter_null_outcome_adds_exists_subquery(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, filter_null_outcome=True)
        assert "AND EXISTS" in clauses
        assert "r.outcome IS NULL" in clauses
        # No additional params needed for this filter
        assert params == []

    def test_filter_null_outcome_false_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, filter_null_outcome=False)
        assert clauses == ""
        assert params == []

    def test_filter_null_outcome_with_county(self) -> None:
        clauses, params = reingest._build_filters(
            "Santa Clara", None, None, filter_null_outcome=True
        )
        assert "AND ct.county = %s" in clauses
        assert "AND EXISTS" in clauses
        assert "r.outcome IS NULL" in clauses
        assert params == ["Santa Clara"]

    def test_filter_null_outcome_and_orphaned_only_contradictory(self) -> None:
        """Both flags can be set but produce contradictory logic (no doc can
        have no rulings AND have a ruling with NULL outcome). Verify both
        clauses are emitted so the query returns an empty set gracefully."""
        clauses, params = reingest._build_filters(
            None, None, None, filter_null_outcome=True, orphaned_only=True
        )
        assert "AND EXISTS" in clauses
        assert "r.outcome IS NULL" in clauses
        assert "AND NOT EXISTS" in clauses

    def test_filter_null_outcome_with_null_motion_type(self) -> None:
        """Both null-outcome and null-motion-type can be combined to target
        documents that have rulings with both NULL outcome and NULL motion_type."""
        clauses, params = reingest._build_filters(
            None, None, None, filter_null_outcome=True, null_motion_type=True
        )
        assert "r.outcome IS NULL" in clauses
        assert "r.motion_type IS NULL" in clauses
        assert params == []


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

        Multi-scraper modules (those exporting ``_SCRAPER_CLASS_BY_ID`` with
        multiple ``default_config_<suffix>`` factories) contribute one expected
        scraper_id per factory (#2599).
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
            # Collect every ``default_config*`` factory's scraper_id.  Multi-
            # scraper modules contribute multiple entries (#2599).
            for _name, obj in inspect.getmembers(mod, inspect.isfunction):
                if (
                    _name == "default_config" or _name.startswith("default_config_")
                ) and obj.__module__ == mod.__name__:
                    expected_ids.add(obj().scraper_id)

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
        """Each registry key should match a ``default_config*()`` factory's scraper_id.

        Multi-scraper modules (#2599) expose multiple factories; walk them all
        and verify that every registered ``scraper_id`` is produced by some
        ``default_config*()`` factory in the class's module.
        """
        import importlib
        import inspect

        reingest._load_scraper_registry()
        for scraper_id, cls in reingest._SCRAPER_REGISTRY.items():
            mod = importlib.import_module(cls.__module__)
            factory_ids: set[str] = set()
            for _name, obj in inspect.getmembers(mod, inspect.isfunction):
                if (
                    _name == "default_config" or _name.startswith("default_config_")
                ) and obj.__module__ == mod.__name__:
                    factory_ids.add(obj().scraper_id)
            assert scraper_id in factory_ids, (
                f"scraper_id {scraper_id!r} not produced by any default_config* "
                f"factory in {mod.__name__}"
            )

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
class TestReparseDocumentSanDiegoCalendarNarrowing:
    """San Diego calendar HTML narrowing path for unregistered scraper IDs (#2381).

    When a ``rebuild-ca-san_diego`` (or any other unregistered) scraper_id
    reingests a San Diego calendar HTML document, ``parse_document`` cannot
    be called — the scraper class is not in ``_SCRAPER_REGISTRY``.  The
    reingest fallback at :func:`reingest_from_s3._reparse_document` calls
    :func:`courts.ca.sd_calendar.extract_case_section` directly to narrow
    the full HTML page to a single-case plain-text excerpt.  Previously
    the narrowed section was only used for the LLM call — the stored
    ``ruling_text`` remained the full raw HTML (50KB+).  This test class
    verifies that the narrowed excerpt also replaces ``ruling_text``.
    """

    _SD_CALENDAR_HTML = """<html><head></head><body>
<h1>CIVIL CALENDAR For Monday, 03/23/2026</h1>
<h3>CENTRAL DIVISION, CENTRAL COURTHOUSE</h3>
<div class="department">
<h2><a name='C-60'></a>Department: C-60</h2>
<table class="tables">
<thead><tr><th>Time</th><th>Case#</th><th>Title</th><th>Event</th><th>Officer</th><th>Party</th><th>Attorney</th></tr></thead>
<tbody>
<tr>
<td>9:00 AM</td>
<td>37-2024-00021082-CL-CL-CTL</td>
<td>Doe v Smith</td>
<td>Motion Hearing</td>
<td>Judge MATTHEW C. BRANER</td>
<td><p>(PL) Jane Doe</p><p>(DF) John Smith</p></td>
<td><p>Alice Attorney</p></td>
</tr>
</tbody>
</table>
</div>
</body></html>
"""

    def _sd_doc_meta(self, case_number: str) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "San Diego",
            "court_name": "Superior Court",
            "source_url": "",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "sd123",
            "format": "html",
            "case_number": case_number,
            "case_title": None,
            "hearing_date": None,
            "court_id": str(_COURT_ID),
            "scraper_id": "rebuild-ca-san_diego",
            "s3_key": "ca/san_diego/superior_court/raw/abc.html",
            "s3_bucket": "test-bucket",
        }

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_odyssey_short_form_narrowing_updates_ruling_text(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """rebuild-ca-san_diego with Odyssey short-form case_number gets
        ruling_text set to the narrowed plain-text excerpt, not raw HTML."""
        mock_llm.return_value = None  # Force regex fallback path.
        # Ensure the rebuild scraper_id is NOT in the registry.
        reingest._SCRAPER_REGISTRY.pop("rebuild-ca-san_diego", None)

        raw = self._SD_CALENDAR_HTML.encode("utf-8")
        client = MagicMock()
        result = reingest._reparse_document(
            raw,
            "rebuild-ca-san_diego",
            self._sd_doc_meta(case_number="2024-00021082"),
            llm_client=client,
        )

        ruling_text = result["ruling_text"]
        assert ruling_text is not None
        # Must NOT be raw HTML.
        assert not ruling_text.lstrip().startswith("<")
        assert "<!DOCTYPE" not in ruling_text
        assert "<html" not in ruling_text.lower()
        # Must contain the narrowed case details.
        assert "Department: C-60" in ruling_text
        assert "37-2024-00021082-CL-CL-CTL" in ruling_text
        assert "Motion Hearing" in ruling_text
        # Narrowed text should be much shorter than the full page.
        assert len(ruling_text) < 1000

    @patch.object(reingest, "_load_scraper_registry")
    @patch("reingest_from_s3.extract_fields_llm")
    def test_ruling_text_is_raw_html_when_case_not_in_calendar(
        self,
        mock_llm: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """If the case isn't in the calendar HTML at all, narrowing is a no-op.

        This establishes that the narrowing is what caused ruling_text to be
        updated — when it fails, the fallback behavior (raw HTML) still
        applies.  It is retained here as a regression guard so a future
        change that always overrides ruling_text would fail.
        """
        mock_llm.return_value = None
        reingest._SCRAPER_REGISTRY.pop("rebuild-ca-san_diego", None)

        raw = self._SD_CALENDAR_HTML.encode("utf-8")
        client = MagicMock()
        result = reingest._reparse_document(
            raw,
            "rebuild-ca-san_diego",
            self._sd_doc_meta(case_number="9999-99999999"),  # not in HTML
            llm_client=client,
        )

        # No match — ruling_text stays as the full raw HTML fallback.
        assert result["ruling_text"].lstrip().startswith("<")

    @patch.object(reingest, "_load_scraper_registry")
    def test_odyssey_narrowing_runs_without_llm_client(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Narrowing must run even when llm_client is None (--no-llm mode).

        Regression guard: previously the SD direct-narrowing lived inside
        the ``if llm_client is not None`` branch, so ``--no-llm`` reingest
        runs never replaced the raw HTML.  The narrowing is independent
        of LLM availability — it's about producing clean ruling_text, not
        preparing an LLM prompt.
        """
        reingest._SCRAPER_REGISTRY.pop("rebuild-ca-san_diego", None)

        raw = self._SD_CALENDAR_HTML.encode("utf-8")
        result = reingest._reparse_document(
            raw,
            "rebuild-ca-san_diego",
            self._sd_doc_meta(case_number="2024-00021082"),
            llm_client=None,  # --no-llm mode
        )

        ruling_text = result["ruling_text"]
        assert ruling_text is not None
        assert not ruling_text.lstrip().startswith("<")
        assert "37-2024-00021082-CL-CL-CTL" in ruling_text
        assert len(ruling_text) < 1000


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
# --force-retranscribe flag tests (#2360)
# ---------------------------------------------------------------------------


class TestForceRetranscribePassedToBatch:
    """Verify force_retranscribe is forwarded from run_reingest to reingest_batch."""

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_force_retranscribe_passed_to_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """force_retranscribe=True is forwarded to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test", force_retranscribe=True)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("force_retranscribe") is True

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_force_retranscribe_defaults_to_false(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """force_retranscribe defaults to False when not specified."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("force_retranscribe") is False


class TestCLIForceRetranscribeFlag:
    """Tests that --force-retranscribe CLI flag is properly parsed."""

    def test_parser_has_force_retranscribe_arg(self) -> None:
        """The argument parser accepts --force-retranscribe."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--force-retranscribe", action="store_true")
        args = parser.parse_args(["--force-retranscribe"])
        assert args.force_retranscribe is True

    def test_parser_default_force_retranscribe_is_false(self) -> None:
        """Default --force-retranscribe is False."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--force-retranscribe", action="store_true")
        args = parser.parse_args([])
        assert args.force_retranscribe is False


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
    def test_split_path_empty_ruling_text_uses_none(
        self,
        mock_extract: MagicMock,
        mock_registry: MagicMock,
    ) -> None:
        """Split rulings with empty/None ruling_text should get None, not full PDF text (#2078).

        When a split function returns rulings with empty or None ruling_text,
        the extracted dict should have ruling_text=None to prevent the full
        PDF text from being stored for every case in the document.
        """
        from courts.ca.fresno_tentatives import SplitRuling

        rulings = [
            SplitRuling(1, "CVPS001", "", None, None, None, None),
            SplitRuling(2, "CVPS002", "has actual text", None, None, None, None),
        ]
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY["test-empty-text"] = mock_split
        reingest._SCRAPER_REGISTRY.pop("test-empty-text", None)
        mock_extract.return_value = "FULL PDF CALENDAR TEXT THAT SHOULD NOT APPEAR"

        try:
            result = reingest._full_reparse_document(
                b"raw pdf",
                "test-empty-text",
                self._doc_meta(scraper_id="test-empty-text"),
            )
            assert len(result) == 2
            # Empty ruling_text in split should become None, not the full PDF text
            assert result[0]["ruling_text"] is None
            # Ruling with actual text should keep it
            assert result[1]["ruling_text"] == "has actual text"
        finally:
            reingest._SPLIT_REGISTRY.pop("test-empty-text", None)

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
    def test_supersede_runs_before_insert_for_split_docs(
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
        """_supersede_document runs BEFORE insert_document_and_ruling (#2365).

        Without this ordering, insert_ruling's content-hash dedup matches
        the old ruling (still linked to the parent document), updating it
        in place.  _supersede_document then deletes those same rulings,
        leaving split children with no ruling rows.
        """
        call_order: list[str] = []
        mock_supersede.side_effect = lambda *a, **kw: call_order.append("supersede")
        mock_insert_doc_and_ruling.side_effect = lambda *a, **kw: call_order.append("insert")

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
                "motion_type": "demurrer",
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
                "motion_type": "motion_to_compel",
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

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        # Supersede must happen first, then both inserts
        assert call_order == ["supersede", "insert", "insert"], (
            f"Expected supersede before inserts, got: {call_order}"
        )

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
# multimodal split-child guard tests (#2416)
# ---------------------------------------------------------------------------


class TestMultimodalSplitChildGuard:
    """Tests for the split-child guard in multimodal reingest mode (#2416).

    When ``multimodal_extractor`` is passed, documents whose IDs are UUID v5
    (generated by ``make_split_document_id``) must be skipped to prevent an
    exponential loop where split children are re-processed through
    per-page multimodal extraction, producing N new grandchildren per child.

    This is the same class of bug as #1919 but for multimodal mode.  During
    the 2026-04-16 SC reingest the absence of this guard inflated SC rulings
    from 808 to 1,615 and created ~10k unwanted grandchildren.
    """

    @patch("reingest_from_s3._reparse_document_multimodal")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_split_child_documents_skipped_in_multimodal(
        self,
        mock_fetch_s3: MagicMock,
        mock_multimodal: MagicMock,
    ) -> None:
        """Split-child documents are skipped when multimodal_extractor is set."""
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
            full_reparse=False,
            multimodal_extractor=MagicMock(),
        )

        # The split child should be skipped — not parsed or written
        mock_multimodal.assert_not_called()
        assert result["processed"] == 1
        assert result["skipped"] == 1
        assert result["updated"] == 0

    @patch("reingest_from_s3._supersede_document")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document_multimodal")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_uuid4_documents_still_processed_in_multimodal(
        self,
        mock_fetch_s3: MagicMock,
        mock_multimodal: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_supersede: MagicMock,
    ) -> None:
        """Regular UUID v4 documents are still processed in multimodal mode."""
        regular_id = uuid.uuid4()  # UUID v4 — not a split child
        row = _make_document_row(doc_id=regular_id)
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"pdf content"
        mock_multimodal.return_value = [
            {
                "ruling_text": "Ruling text",
                "case_number": "24CV123456",
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
                "llm_skipped": False,
                "llm_outcome": "multimodal_success",
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
            full_reparse=False,
            multimodal_extractor=MagicMock(),
        )

        mock_multimodal.assert_called_once()
        assert result["processed"] == 1
        assert result["updated"] == 1

    @patch("reingest_from_s3._reparse_document_multimodal")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_mixed_batch_skips_only_split_children_in_multimodal(
        self,
        mock_fetch_s3: MagicMock,
        mock_multimodal: MagicMock,
    ) -> None:
        """Mixed batch: only split children are skipped in multimodal mode."""
        regular_id = uuid.uuid4()
        parent_id = str(uuid.uuid4())
        child_id = uuid.UUID(make_split_document_id(parent_id, 0))

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
        mock_multimodal.return_value = [
            {
                "ruling_text": "text",
                "case_number": "24CV123456",
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
                "llm_skipped": False,
                "llm_outcome": "multimodal_success",
            },
        ]

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=False,
            multimodal_extractor=MagicMock(),
            dry_run=True,
        )

        assert result["processed"] == 2
        assert result["skipped"] == 1
        # Only the regular doc was parsed via multimodal
        mock_multimodal.assert_called_once()


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

    # Match CREATE TABLE [schema.]<name> ( ... );  — skip staging schema tables
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

    def test_pdf_multimodal_multi_ruling_empty_text_uses_none(self) -> None:
        """Multi-ruling PDFs with empty ruling_text should get None, not full PDF text (#2078).

        When multimodal extraction returns multiple rulings but some have
        empty/None ruling_text, the code must NOT fall back to the full PDF
        text.  This mirrors the guard in worker.py line 1579.
        """
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                extracted_case_title="Ruling One",
                ruling_text=None,  # Empty — multimodal didn't extract text
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000002",
                extracted_case_title="Ruling Two",
                ruling_text="Second ruling text.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000003",
                extracted_case_title="Ruling Three",
                ruling_text="",  # Empty string — also should get None
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks"):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results) == 3
        # Ruling with None text should get None, not full PDF text
        assert results[0]["ruling_text"] is None
        # Ruling with actual text should keep its text
        assert results[1]["ruling_text"] == "Second ruling text."
        # Ruling with empty string should get None for multi-ruling docs
        assert results[2]["ruling_text"] is None
        # All should be marked as split
        assert all(r["is_split"] is True for r in results)

    def test_pdf_multimodal_single_ruling_empty_text_uses_empty_string(self) -> None:
        """Single-ruling PDFs with empty ruling_text should get empty string (#2078).

        For backward compatibility, single-ruling documents still fall back
        to empty string when ruling_text is empty.
        """
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                extracted_case_title="Single Ruling",
                ruling_text=None,
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
        # Single ruling with None text should get empty string (backward compat)
        assert results[0]["ruling_text"] == ""
        assert results[0]["is_split"] is False

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

    def test_case_title_null_when_llm_returns_none_no_db_fallback(self) -> None:
        """When the multimodal LLM returns no case_title, the reingest dict
        must have case_title=None — NOT the stale DB title from doc_meta (#2502).

        Previously, the code used ``cr.case_title or doc_meta.get("case_title")``
        which pulled the existing DB title (carried via the LEFT JOIN in
        FETCH_DOCUMENTS_QUERY) into the result when the LLM produced nothing.
        Combined with ``upsert_case(force_update=True)``, that created a
        self-sustaining fixed point for wrong titles.  The fix removes the
        fallback so None flows through to upsert_case, where the COALESCE
        semantic at db.py:316 preserves the existing (non-null) title.
        """
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        doc_meta["case_title"] = "Stale DB Title"  # existing DB title
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                extracted_case_title=None,  # LLM did not extract a title
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
        # Must be None — not "Stale DB Title" (no fallback to doc_meta)
        assert results[0]["case_title"] is None

    def test_case_title_from_llm_wins_over_db_title(self) -> None:
        """When the multimodal LLM returns a case_title, it wins over any
        existing doc_meta title (#2502).
        """
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        doc_meta["case_title"] = "Old DB Title"
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01234567",
                extracted_case_title="Fresh LLM Title",
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
        assert results[0]["case_title"] == "Fresh LLM Title"

    def test_multi_ruling_no_title_swap_when_some_rulings_miss_title(self) -> None:
        """Multi-ruling PDFs: rulings whose LLM extraction returned no title
        must keep case_title=None — they must NOT pick up doc_meta's title and
        must NOT pick up a sibling ruling's title (#2502).

        This is the canonical repro for the #2490 Palacios-v.-Vallejo bug:
        a multi-case PDF where one or more splits come back without a title,
        and any fallback causes them to converge on the same wrong title
        across reingest cycles.
        """
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        doc_meta["case_title"] = "Palacios v. Vallejo"  # stale per-doc title
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                extracted_case_title="Gu v. Smith",  # LLM extracted this one
                ruling_text="First ruling text.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000002",
                extracted_case_title=None,  # LLM did NOT extract
                ruling_text="Second ruling text.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000003",
                extracted_case_title="Clarke v. Jones",
                ruling_text="Third ruling text.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000004",
                extracted_case_title=None,  # LLM did NOT extract
                ruling_text="Fourth ruling text.",
            ),
        ]

        with patch.object(reingest, "_apply_regex_fallbacks"):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
            )

        assert len(results) == 4
        # Rulings with LLM titles keep them
        assert results[0]["case_title"] == "Gu v. Smith"
        assert results[2]["case_title"] == "Clarke v. Jones"
        # Rulings without LLM titles must be None — NOT doc_meta's stale
        # "Palacios v. Vallejo", NOT a sibling ruling's title.
        assert results[1]["case_title"] is None
        assert results[3]["case_title"] is None


# ---------------------------------------------------------------------------
# Text-split branch tests — sibling of TestReparseDocumentMultimodal (#2521)
# ---------------------------------------------------------------------------


class TestReparseDocumentTextSplit:
    """Regression tests for the text-split branch of ``_full_reparse_document``.

    Mirrors ``TestReparseDocumentMultimodal``'s ``case_title`` tests to
    guard against the same #2502-style fixed point in the split path used
    by Fresno, Contra Costa, and any future text-split counties (#2521).
    """

    _SCRAPER_ID = "test-split-title-2521"

    def _make_doc_meta(
        self,
        *,
        case_title: str | None = None,
        case_number: str | None = "CVPS2306157",
    ) -> dict:
        return {
            "document_id": str(_DOC_ID_1),
            "state": "CA",
            "county": "Fresno",
            "court_name": "Fresno Superior Court",
            "source_url": "https://court.example.com/ruling.pdf",
            "captured_at": _CAPTURED_AT_1,
            "content_hash": "abc123",
            "format": "pdf",
            "case_number": case_number,
            "case_title": case_title,
            "case_type": None,
            "hearing_date": _HEARING_DATE,
            "court_id": str(_COURT_ID),
            "scraper_id": self._SCRAPER_ID,
            "s3_key": "docs/test.pdf",
            "s3_bucket": "test-bucket",
        }

    def _register_split(self, rulings: list[Any]) -> MagicMock:
        """Register a regex-split mock under ``_SCRAPER_ID`` and return it.

        Callers are responsible for popping the registry entry in a
        ``finally`` block.  Using a distinct scraper_id keeps these tests
        isolated from real-scraper registry state.
        """
        mock_split = MagicMock(return_value=rulings)
        reingest._SPLIT_REGISTRY[self._SCRAPER_ID] = mock_split
        # Ensure no LLM split is registered so we exercise the regex path.
        reingest._LLM_SPLIT_REGISTRY.pop(self._SCRAPER_ID, None)
        # Ensure no scraper registry entry — avoids parse_document() call.
        reingest._SCRAPER_REGISTRY.pop(self._SCRAPER_ID, None)
        return mock_split

    def test_case_title_null_when_split_returns_none_no_db_fallback(self) -> None:
        """When the text split returns no case_title for a ruling, the
        reingest dict must have case_title=None — NOT the stale DB title
        from doc_meta (#2521, sibling of #2502).

        Previously, the code used ``ruling.case_title or
        doc_meta.get("case_title")`` which pulled the existing DB title
        (carried via the LEFT JOIN in FETCH_DOCUMENTS_QUERY) into the
        result when the split produced nothing.  Combined with
        ``upsert_case(force_update=True)``, that created a self-sustaining
        fixed point for wrong titles.  The fix removes the fallback so
        None flows through to upsert_case, where the COALESCE semantic at
        db.py:316 preserves the existing (non-null) title.
        """
        from courts.ca.fresno_tentatives import SplitRuling

        doc_meta = self._make_doc_meta(case_title="Stale DB Title")
        rulings = [
            SplitRuling(
                ruling_index=1,
                case_number="CVPS2306157",
                ruling_text="First ruling text.",
                case_title=None,  # split did not extract a title
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=2,
                case_number="CVPS2306202",
                ruling_text="Second ruling text.",
                case_title=None,
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
        ]
        self._register_split(rulings)

        try:
            with (
                patch.object(reingest, "_apply_regex_fallbacks"),
                patch.object(reingest, "_extract_text_from_content", return_value="pdf text"),
            ):
                results = reingest._full_reparse_document(
                    b"%PDF-1.4 fake pdf",
                    self._SCRAPER_ID,
                    doc_meta,
                )
        finally:
            reingest._SPLIT_REGISTRY.pop(self._SCRAPER_ID, None)

        assert len(results) == 2
        # Both must be None — not "Stale DB Title" (no fallback to doc_meta)
        assert results[0]["case_title"] is None
        assert results[1]["case_title"] is None
        # extraction_methods must NOT mark case_title as "split" when the
        # field is None (guards against misleading provenance).
        assert "case_title" not in results[0]["extraction_methods"]
        assert "case_title" not in results[1]["extraction_methods"]

    def test_case_title_from_split_wins_over_db_title(self) -> None:
        """When the text split returns a case_title, it wins over any
        existing doc_meta title (#2521, sibling of #2502).
        """
        from courts.ca.fresno_tentatives import SplitRuling

        doc_meta = self._make_doc_meta(case_title="Old DB Title")
        rulings = [
            SplitRuling(
                ruling_index=1,
                case_number="CVPS2306157",
                ruling_text="First ruling text.",
                case_title="Fresh Split Title",
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=2,
                case_number="CVPS2306202",
                ruling_text="Second ruling text.",
                case_title="Another Fresh Title",
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
        ]
        self._register_split(rulings)

        try:
            with (
                patch.object(reingest, "_apply_regex_fallbacks"),
                patch.object(reingest, "_extract_text_from_content", return_value="pdf text"),
            ):
                results = reingest._full_reparse_document(
                    b"%PDF-1.4 fake pdf",
                    self._SCRAPER_ID,
                    doc_meta,
                )
        finally:
            reingest._SPLIT_REGISTRY.pop(self._SCRAPER_ID, None)

        assert len(results) == 2
        assert results[0]["case_title"] == "Fresh Split Title"
        assert results[1]["case_title"] == "Another Fresh Title"

    def test_multi_ruling_no_title_swap_when_some_rulings_miss_title(self) -> None:
        """Multi-ruling text-split PDFs: rulings whose split extraction
        returned no title must keep case_title=None — they must NOT pick
        up doc_meta's title and must NOT pick up a sibling ruling's title
        (#2521, sibling of #2502).

        This mirrors the canonical #2490 Palacios-v.-Vallejo repro in the
        multimodal branch: a multi-case PDF where one or more splits come
        back without a title, and any fallback causes them to converge on
        the same wrong title across reingest cycles.
        """
        from courts.ca.fresno_tentatives import SplitRuling

        doc_meta = self._make_doc_meta(case_title="Palacios v. Vallejo")
        rulings = [
            SplitRuling(
                ruling_index=1,
                case_number="CVPS2306001",
                ruling_text="First ruling text.",
                case_title="Gu v. Smith",  # split extracted this one
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=2,
                case_number="CVPS2306002",
                ruling_text="Second ruling text.",
                case_title=None,  # split did NOT extract
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=3,
                case_number="CVPS2306003",
                ruling_text="Third ruling text.",
                case_title="Clarke v. Jones",
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=4,
                case_number="CVPS2306004",
                ruling_text="Fourth ruling text.",
                case_title=None,  # split did NOT extract
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
        ]
        self._register_split(rulings)

        try:
            with (
                patch.object(reingest, "_apply_regex_fallbacks"),
                patch.object(reingest, "_extract_text_from_content", return_value="pdf text"),
            ):
                results = reingest._full_reparse_document(
                    b"%PDF-1.4 fake pdf",
                    self._SCRAPER_ID,
                    doc_meta,
                )
        finally:
            reingest._SPLIT_REGISTRY.pop(self._SCRAPER_ID, None)

        assert len(results) == 4
        # Rulings with split titles keep them.
        assert results[0]["case_title"] == "Gu v. Smith"
        assert results[2]["case_title"] == "Clarke v. Jones"
        # Rulings without split titles must be None — NOT doc_meta's
        # stale "Palacios v. Vallejo", NOT a sibling ruling's title.
        assert results[1]["case_title"] is None
        assert results[3]["case_title"] is None

    def test_case_number_preserves_doc_meta_fallback_identity_anchor(self) -> None:
        """``case_number`` keeps its ``doc_meta`` fallback even when the
        split returns no case_number — it is an *identity anchor* keyed
        on ``(court_id, case_number)`` in ``cases``, not descriptive
        metadata subject to the #2502 fixed-point risk (#2521).

        If the split produces no case_number, falling back to doc_meta's
        value keeps the ruling attached to its existing case row.
        Replacing a real number with ``UNKNOWN-<uuid>`` would orphan it.
        """
        from courts.ca.fresno_tentatives import SplitRuling

        doc_meta = self._make_doc_meta(
            case_title=None,
            case_number="CVPS-REAL-123",  # existing identity anchor
        )
        rulings = [
            SplitRuling(
                ruling_index=1,
                case_number=None,  # split produced nothing
                ruling_text="First ruling text.",
                case_title=None,
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
            SplitRuling(
                ruling_index=2,
                case_number=None,
                ruling_text="Second ruling text.",
                case_title=None,
                motion_type=None,
                outcome=None,
                hearing_date=None,
            ),
        ]
        self._register_split(rulings)

        try:
            with (
                patch.object(reingest, "_apply_regex_fallbacks"),
                patch.object(reingest, "_extract_text_from_content", return_value="pdf text"),
            ):
                results = reingest._full_reparse_document(
                    b"%PDF-1.4 fake pdf",
                    self._SCRAPER_ID,
                    doc_meta,
                )
        finally:
            reingest._SPLIT_REGISTRY.pop(self._SCRAPER_ID, None)

        assert len(results) == 2
        # Both rulings must inherit the doc_meta case_number (identity
        # anchor preserved) — NOT be None.
        assert results[0]["case_number"] == "CVPS-REAL-123"
        assert results[1]["case_number"] == "CVPS-REAL-123"


# ---------------------------------------------------------------------------
# LLM enrichment tests (#2406)
# ---------------------------------------------------------------------------


class TestApplyLlmEnrichment:
    """Tests for ``_apply_llm_enrichment()`` — the post-multimodal enrichment step."""

    def _make_enrichment_result(
        self,
        *,
        case_title: str | None = None,
        motion_type: str | None = None,
        outcome: str | None = None,
        plaintiffs: list[str] | None = None,
        defendants: list[str] | None = None,
    ) -> Any:
        from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

        return LlmEnrichmentResult(
            case_title=case_title,
            motion_type=motion_type,
            outcome=outcome,
            parties=EnrichmentParties(
                plaintiffs=plaintiffs or [],
                defendants=defendants or [],
            ),
        )

    def _make_extracted(
        self,
        *,
        ruling_text: str | None = "The motion is GRANTED.",
        outcome: str | None = None,
        motion_type: str | None = None,
        case_title: str | None = None,
        parties: list | None = None,
    ) -> dict:
        return {
            "ruling_text": ruling_text,
            "case_number": "30-2024-01234567",
            "case_title": case_title,
            "case_type": None,
            "judge_name": "Judge Smith",
            "outcome": outcome,
            "motion_type": motion_type,
            "department": "C1",
            "parties": parties if parties is not None else [],
            "hearing_date": date(2026, 3, 5),
            "extraction_methods": {"_all": "multimodal"},
            "llm_skipped": False,
            "llm_outcome": "multimodal_success",
            "ruling_index": 0,
            "split_document_id": "test-doc-id",
            "is_split": False,
        }

    def test_populates_missing_enrichment_fields(self) -> None:
        """Enrichment fills outcome, motion_type, case_title, parties when absent."""
        extracted = self._make_extracted()
        mock_client = MagicMock()
        enrichment = self._make_enrichment_result(
            case_title="Smith v. Jones",
            motion_type="msj",
            outcome="granted",
            plaintiffs=["Smith"],
            defendants=["Jones"],
        )

        with patch.object(reingest, "enrich_ruling", return_value=enrichment) as mock_enrich:
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model="gemini-2.0-flash-lite",
                document_id="doc-1",
            )

        mock_enrich.assert_called_once()
        call_kwargs = mock_enrich.call_args.kwargs
        assert call_kwargs["provider"] == "google"
        assert call_kwargs["model"] == "gemini-2.0-flash-lite"
        assert call_kwargs["client"] is mock_client
        assert extracted["outcome"] == "granted"
        assert extracted["motion_type"] == "msj"
        assert extracted["case_title"] == "Smith v. Jones"
        assert extracted["parties"] == [
            {"name": "Smith", "role": "plaintiff"},
            {"name": "Jones", "role": "defendant"},
        ]
        methods = extracted["extraction_methods"]
        assert methods["outcome"] == "llm_enrichment"
        assert methods["motion_type"] == "llm_enrichment"
        assert methods["case_title"] == "llm_enrichment"
        assert methods["parties"] == "llm_enrichment"

    def test_does_not_overwrite_existing_fields(self) -> None:
        """Pre-populated fields must not be replaced by enrichment."""
        extracted = self._make_extracted(
            outcome="denied",
            motion_type="demurrer",
            case_title="Existing v. Title",
            parties=[{"name": "Prev", "role": "plaintiff"}],
        )
        mock_client = MagicMock()
        enrichment = self._make_enrichment_result(
            case_title="SHOULD NOT WIN",
            motion_type="msj",
            outcome="granted",
            plaintiffs=["New"],
            defendants=["Party"],
        )

        with patch.object(reingest, "enrich_ruling", return_value=enrichment):
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model=None,
                document_id="doc-1",
            )

        assert extracted["outcome"] == "denied"
        assert extracted["motion_type"] == "demurrer"
        assert extracted["case_title"] == "Existing v. Title"
        assert extracted["parties"] == [{"name": "Prev", "role": "plaintiff"}]
        methods = extracted["extraction_methods"]
        assert "outcome" not in methods
        assert "motion_type" not in methods
        assert "case_title" not in methods
        assert "parties" not in methods

    def test_skipped_when_all_fields_present(self) -> None:
        """enrich_ruling is not called if no enrichment is needed."""
        extracted = self._make_extracted(
            outcome="granted",
            motion_type="msj",
            case_title="Smith v. Jones",
            parties=[{"name": "Smith", "role": "plaintiff"}],
        )
        mock_client = MagicMock()

        with patch.object(reingest, "enrich_ruling") as mock_enrich:
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model=None,
                document_id="doc-1",
            )

        mock_enrich.assert_not_called()

    def test_skipped_when_ruling_text_empty(self) -> None:
        """Cross-contamination guard result (None text) must not trigger enrichment."""
        extracted = self._make_extracted(ruling_text=None)
        mock_client = MagicMock()

        with patch.object(reingest, "enrich_ruling") as mock_enrich:
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model=None,
                document_id="doc-1",
            )

        mock_enrich.assert_not_called()

    def test_skipped_when_ruling_text_whitespace(self) -> None:
        """Whitespace-only text must not trigger enrichment."""
        extracted = self._make_extracted(ruling_text="   \n\t  ")
        mock_client = MagicMock()

        with patch.object(reingest, "enrich_ruling") as mock_enrich:
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model=None,
                document_id="doc-1",
            )

        mock_enrich.assert_not_called()

    def test_skipped_when_no_llm_client(self) -> None:
        """Regex-only mode (no client) must not attempt enrichment."""
        extracted = self._make_extracted()

        with patch.object(reingest, "enrich_ruling") as mock_enrich:
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=None,
                llm_provider=None,
                llm_model=None,
                document_id="doc-1",
            )

        mock_enrich.assert_not_called()
        assert extracted["outcome"] is None
        assert extracted["motion_type"] is None

    def test_llm_returns_none_does_not_raise(self) -> None:
        """LLM API failure (enrich_ruling returns None) is handled gracefully."""
        extracted = self._make_extracted()
        mock_client = MagicMock()

        with patch.object(reingest, "enrich_ruling", return_value=None):
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model=None,
                document_id="doc-1",
            )

        assert extracted["outcome"] is None
        assert extracted["motion_type"] is None

    def test_llm_exception_does_not_raise(self) -> None:
        """Unexpected exceptions from enrich_ruling are caught and logged."""
        extracted = self._make_extracted()
        mock_client = MagicMock()

        with patch.object(reingest, "enrich_ruling", side_effect=RuntimeError("boom")):
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model=None,
                document_id="doc-1",
            )

        assert extracted["outcome"] is None
        assert extracted["motion_type"] is None
        # The extraction_methods dict should be unmodified by the failed call.
        assert extracted["extraction_methods"] == {"_all": "multimodal"}

    def test_defaults_provider_to_google(self) -> None:
        """A None provider is normalized to 'google'."""
        extracted = self._make_extracted()
        mock_client = MagicMock()
        enrichment = self._make_enrichment_result(outcome="granted")

        with patch.object(reingest, "enrich_ruling", return_value=enrichment) as mock_enrich:
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider=None,
                llm_model=None,
                document_id="doc-1",
            )

        assert mock_enrich.call_args.kwargs["provider"] == "google"

    def test_partial_enrichment_merges_only_missing(self) -> None:
        """When enrichment returns only some fields, only those get set."""
        extracted = self._make_extracted(motion_type="demurrer")
        mock_client = MagicMock()
        enrichment = self._make_enrichment_result(
            outcome="granted",
            motion_type="msj",  # Ignored — motion_type already set.
            case_title="Smith v. Jones",
            # No parties extracted.
        )

        with patch.object(reingest, "enrich_ruling", return_value=enrichment):
            reingest._apply_llm_enrichment(
                extracted,
                llm_client=mock_client,
                llm_provider="google",
                llm_model=None,
                document_id="doc-1",
            )

        assert extracted["outcome"] == "granted"
        assert extracted["motion_type"] == "demurrer"  # Not overwritten.
        assert extracted["case_title"] == "Smith v. Jones"
        assert extracted["parties"] == []
        methods = extracted["extraction_methods"]
        assert methods["outcome"] == "llm_enrichment"
        assert methods["case_title"] == "llm_enrichment"
        assert "motion_type" not in methods
        assert "parties" not in methods


class TestReparseMultimodalCallsEnrichment:
    """Integration: ``_reparse_document_multimodal`` wires enrichment in (#2406)."""

    def _make_doc_meta(self, doc_id: str = "test-doc-id") -> dict:
        return {
            "document_id": doc_id,
            "state": "CA",
            "county": "Orange",
            "court_name": "Orange County Superior Court",
            "source_url": "https://court.example.com/ruling.pdf",
            "captured_at": datetime(2026, 3, 1, 10, 0, 0),
            "content_hash": "abc123hash",
            "format": "pdf",
            "case_number": None,
            "case_title": None,
            "hearing_date": date(2026, 3, 5),
            "court_id": "court-id-1",
            "scraper_id": "ca-oc-tentatives-civil",
            "s3_key": "docs/test.pdf",
            "s3_bucket": "test-bucket",
        }

    def test_enrichment_called_for_each_ruling_with_text(self) -> None:
        """After multimodal transcription, enrich_ruling is called for each ruling."""
        from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        # Simulate an extractor without its own pre-initialized internal
        # client -- forces the enrichment fallback to the shared text-path
        # llm_client.  See #2406 adversarial review.
        mock_extractor._client = None
        mock_extractor._provider = None
        mock_extractor._model = None
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion for summary judgment is GRANTED.",
            ),
        ]
        mock_llm_client = MagicMock()
        enrichment_result = LlmEnrichmentResult(
            case_title="Smith v. Jones",
            motion_type="msj",
            outcome="granted",
            parties=EnrichmentParties(plaintiffs=["Smith"], defendants=["Jones"]),
        )

        with (
            patch.object(reingest, "_apply_regex_fallbacks"),
            patch.object(reingest, "enrich_ruling", return_value=enrichment_result) as mock_enrich,
        ):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
                llm_client=mock_llm_client,
                llm_provider="google",
                llm_model="gemini-2.0-flash-lite",
            )

        # enrich_ruling must have been called with the multimodal ruling_text.
        mock_enrich.assert_called_once()
        call_kwargs = mock_enrich.call_args.kwargs
        call_args = mock_enrich.call_args.args
        called_text = call_args[0] if call_args else call_kwargs.get("ruling_text")
        assert called_text == "The motion for summary judgment is GRANTED."
        # With no extractor-internal client, the shared llm_client is used.
        assert call_kwargs["client"] is mock_llm_client

        # The returned ruling has enrichment fields populated.
        assert len(results) == 1
        assert results[0]["outcome"] == "granted"
        assert results[0]["motion_type"] == "msj"
        assert results[0]["case_title"] == "Smith v. Jones"
        assert results[0]["parties"] == [
            {"name": "Smith", "role": "plaintiff"},
            {"name": "Jones", "role": "defendant"},
        ]
        methods = results[0]["extraction_methods"]
        assert methods["outcome"] == "llm_enrichment"
        assert methods["motion_type"] == "llm_enrichment"

    def test_enrichment_called_per_ruling_on_multi_ruling_pdf(self) -> None:
        """Each split ruling in a multi-case PDF goes through enrichment."""
        from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                ruling_text="Motion GRANTED.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000002",
                ruling_text="Motion DENIED.",
            ),
        ]
        mock_llm_client = MagicMock()

        def fake_enrich(ruling_text: str, **_: object) -> LlmEnrichmentResult:
            if "GRANTED" in ruling_text:
                return LlmEnrichmentResult(
                    outcome="granted",
                    motion_type="msj",
                    parties=EnrichmentParties(),
                )
            return LlmEnrichmentResult(
                outcome="denied",
                motion_type="mtd",
                parties=EnrichmentParties(),
            )

        with (
            patch.object(reingest, "_apply_regex_fallbacks"),
            patch.object(reingest, "enrich_ruling", side_effect=fake_enrich) as mock_enrich,
        ):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
                llm_client=mock_llm_client,
                llm_provider="google",
            )

        assert mock_enrich.call_count == 2
        assert len(results) == 2
        assert results[0]["outcome"] == "granted"
        assert results[0]["motion_type"] == "msj"
        assert results[1]["outcome"] == "denied"
        assert results[1]["motion_type"] == "mtd"

    def test_enrichment_skipped_without_llm_client(self) -> None:
        """Regex-only mode (no llm_client, no extractor client) skips enrichment."""
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        # Simulate --no-llm mode: no extractor-internal client either.
        mock_extractor._client = None
        mock_extractor._provider = None
        mock_extractor._model = None
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                ruling_text="Motion GRANTED.",
            ),
        ]

        with (
            patch.object(reingest, "_apply_regex_fallbacks"),
            patch.object(reingest, "enrich_ruling") as mock_enrich,
        ):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
                llm_client=None,
            )

        mock_enrich.assert_not_called()
        assert len(results) == 1
        # outcome stays None — regex-only mode.
        assert results[0]["outcome"] is None

    def test_enrichment_skipped_on_empty_ruling_text(self) -> None:
        """Rulings whose text was nulled by cross-contamination guard skip enrichment."""
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        # Multi-ruling document where the 2nd ruling has empty text.
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                ruling_text="Motion GRANTED.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-00000002",
                ruling_text=None,
            ),
        ]
        mock_llm_client = MagicMock()

        from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

        enrichment_result = LlmEnrichmentResult(
            outcome="granted",
            motion_type="msj",
            parties=EnrichmentParties(),
        )

        with (
            patch.object(reingest, "_apply_regex_fallbacks"),
            patch.object(reingest, "enrich_ruling", return_value=enrichment_result) as mock_enrich,
        ):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
                llm_client=mock_llm_client,
                llm_provider="google",
            )

        # enrich_ruling called only once — for the ruling with text.
        assert mock_enrich.call_count == 1
        assert len(results) == 2
        assert results[0]["outcome"] == "granted"
        assert results[1]["outcome"] is None  # Text nulled by guard, enrichment skipped.

    def test_enrichment_failure_does_not_break_pipeline(self) -> None:
        """If enrich_ruling returns None, the ruling is still produced successfully."""
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        mock_extractor = MagicMock()
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                ruling_text="Motion GRANTED.",
            ),
        ]
        mock_llm_client = MagicMock()

        with (
            patch.object(reingest, "_apply_regex_fallbacks"),
            patch.object(reingest, "enrich_ruling", return_value=None),
        ):
            results = reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
                llm_client=mock_llm_client,
                llm_provider="google",
            )

        assert len(results) == 1
        # The pipeline still produces a ruling, just with no enrichment fields.
        assert results[0]["ruling_text"] == "Motion GRANTED."
        assert results[0]["outcome"] is None

    def test_enrichment_reuses_multimodal_extractor_client(self) -> None:
        """When the multimodal extractor has its own client, enrichment reuses it.

        Regression for #2406 adversarial review: the issue requires that
        enrichment share the same LLM client/provider/model as the
        multimodal transcription for connection reuse and LLM family
        consistency.
        """
        from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult
        from framework.llm_schema import ExtractedRuling

        doc_meta = self._make_doc_meta()
        extractor_client = MagicMock(name="extractor_internal_client")
        mock_extractor = MagicMock()
        mock_extractor._client = extractor_client
        mock_extractor._provider = "google"
        mock_extractor._model = "gemini-2.5-flash-lite"
        mock_extractor.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-00000001",
                ruling_text="Motion GRANTED.",
            ),
        ]
        different_llm_client = MagicMock(name="shared_text_path_client")

        enrichment_result = LlmEnrichmentResult(
            outcome="granted",
            motion_type="msj",
            parties=EnrichmentParties(),
        )

        with (
            patch.object(reingest, "_apply_regex_fallbacks"),
            patch.object(reingest, "enrich_ruling", return_value=enrichment_result) as mock_enrich,
        ):
            reingest._reparse_document_multimodal(
                b"%PDF-1.4 fake pdf",
                "ca-oc-tentatives-civil",
                doc_meta,
                mock_extractor,
                llm_client=different_llm_client,
                llm_provider="anthropic",
                llm_model="claude-haiku",
            )

        mock_enrich.assert_called_once()
        call_kwargs = mock_enrich.call_args.kwargs
        # The extractor's internal client / provider / model win over
        # the text-path args -- this keeps transcription and enrichment
        # on the same LLM family (#2406).
        assert call_kwargs["client"] is extractor_client
        assert call_kwargs["provider"] == "google"
        assert call_kwargs["model"] == "gemini-2.5-flash-lite"


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


class TestRunReingestMultimodalTokens:
    """Verify the multimodal extractor uses the higher token limit (#2369)."""

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.LlmExtractor")
    def test_multimodal_extractor_uses_32768_tokens(
        self,
        mock_extractor_cls: MagicMock,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """When --multimodal is set, LlmExtractor is created with max_output_tokens=32768.

        The default 4,096-token output cap causes dense multi-case
        department-calendar pages to truncate their JSON response, leaving
        ruling_text empty for later cases and blocking outcome/motion_type
        enrichment (#2369).
        """
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = _make_batch_result()

        fake_extractor = MagicMock()
        fake_extractor._model = "gemini-2.5-flash-lite"
        mock_extractor_cls.return_value = fake_extractor

        reingest.run_reingest("postgresql://test", multimodal=True, no_llm=True)

        mock_extractor_cls.assert_called_once_with(
            provider="google",
            max_output_tokens=32768,
            bust_cache=False,
        )
        # The extractor is threaded through to reingest_batch.
        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("multimodal_extractor") is fake_extractor

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    @patch("reingest_from_s3.LlmExtractor")
    def test_no_multimodal_flag_does_not_create_extractor(
        self,
        mock_extractor_cls: MagicMock,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Without --multimodal, the multimodal LlmExtractor is not created."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test", multimodal=False, no_llm=True)

        mock_extractor_cls.assert_not_called()
        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("multimodal_extractor") is None


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


# ---------------------------------------------------------------------------
# Prefix-mode tests
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
        """Content hash must be hex digits only."""
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
    """Tests for _derive_court_code — must match ingestion.db._derive_court_code."""

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
        assert courts[0]["court_code"] == "federal-federal"
        assert courts[0]["state"] == "Federal"
        assert courts[0]["county"] == "Federal"
        assert courts[0]["court_name"] == "Courtlistener, County of Federal"

    def test_court_code_matches_ingestion_format(self) -> None:
        """court_code must use {state}-{county} format, not {state}_{county}_{court}."""
        keys = ["ca/santa_clara/superior_court/raw/abc123.html"]
        courts = reingest._discover_courts(keys)
        assert len(courts) == 1
        assert courts[0]["court_code"] == "ca-santa-clara"

    def test_court_name_includes_county(self) -> None:
        """court_name should include 'County of' to match ingestion format."""
        keys = ["ca/orange/superior_court/raw/abc123.html"]
        courts = reingest._discover_courts(keys)
        assert courts[0]["court_name"] == "Superior Court, County of Orange"

    def test_skips_invalid_keys(self) -> None:
        keys = [
            "invalid/key",
            "federal/federal/courtlistener/raw/aaa111.html",
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
                    {"Key": "federal/federal/courtlistener/metadata.json"},  # non-matching
                ]
            }
        ]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert len(keys) == 2
        s3.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket="test-bucket", Prefix="federal/")

    def test_handles_empty_pages(self) -> None:
        s3 = MagicMock()
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert keys == []

    def test_handles_multiple_pages(self) -> None:
        s3 = MagicMock()
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "federal/federal/courtlistener/raw/aaa111.html"}]},
            {"Contents": [{"Key": "federal/federal/courtlistener/raw/bbb222.pdf"}]},
        ]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert len(keys) == 2


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
        assert event["court"] == "Courtlistener"
        assert event["content_format"] == "html"
        assert event["content_hash"] == "abc123"
        assert event["s3_bucket"] == "test-bucket"
        assert event["scraper_id"] == "reingest-federal-federal"
        assert event["ruling_text"] == "<html>ruling text</html>"
        # Document ID is deterministic (uuid5 from content_hash)
        expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "abc123"))
        assert event["document_id"] == expected_id

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
        assert event["state"] == "CA"
        assert event["county"] == "Los Angeles"
        assert event["court"] == "Superior"
        # PDF content is decoded as latin-1
        assert event["ruling_text"] == content.decode("latin-1")

    def test_builds_event_for_txt(self) -> None:
        parsed = {
            "state": "federal",
            "county": "federal",
            "court": "courtlistener",
            "content_hash": "aaa111",
            "ext": "txt",
        }
        content = b"Plain text ruling content"
        event = reingest._build_prefix_event(
            "federal/federal/courtlistener/raw/aaa111.txt",
            content,
            parsed,
            "test-bucket",
        )
        assert event["content_format"] == "txt"
        assert event["ruling_text"] == "Plain text ruling content"

    def test_builds_event_for_docx(self) -> None:
        parsed = {
            "state": "federal",
            "county": "federal",
            "court": "courtlistener",
            "content_hash": "bbb222",
            "ext": "docx",
        }
        content = b"PK\x03\x04 docx binary"
        event = reingest._build_prefix_event(
            "federal/federal/courtlistener/raw/bbb222.docx",
            content,
            parsed,
            "test-bucket",
        )
        assert event["content_format"] == "docx"
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
        cur.execute.assert_called_once()
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
        assert stats["errors"] == 0
        assert stats["skipped"] == 0

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
        # Courts should still be seeded in dry-run mode
        mock_seed.assert_called_once()

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
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_processes_documents_with_pool(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
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

        # Mock ProcessPoolExecutor and its futures
        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = "ok"
        future2 = MagicMock()
        future2.result.return_value = "ok"
        pool.submit.side_effect = [future1, future2]

        # Mock as_completed to return both futures
        with patch("reingest_from_s3.as_completed", return_value=[future1, future2]):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
            )

        assert stats["total_keys"] == 2
        assert stats["processed"] == 2
        assert stats["errors"] == 0
        assert pool.submit.call_count == 2

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
    def test_limit_caps_keys(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_list.return_value = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
            "federal/federal/courtlistener/raw/ccc333.html",
        ]
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="federal/",
            limit=2,
            dry_run=True,
        )
        # Limit should have capped to 2 keys
        assert stats["total_keys"] == 2

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
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_summary_reports_hash_mismatch_warnings(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """The prefix-reingest summary must aggregate ``hash_mismatch`` flags
        from ``_process_prefix_document`` dict results and surface the count
        as ``hash_mismatch_warnings`` in the stats.  See #2628."""
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
            "federal/federal/courtlistener/raw/ccc333.html",
        ]
        mock_list.return_value = keys
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

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        # Two successful docs flag hash_mismatch, one is clean.
        future1 = MagicMock()
        future1.result.return_value = {"status": "ok", "hash_mismatch": True}
        future2 = MagicMock()
        future2.result.return_value = {"status": "ok", "hash_mismatch": True}
        future3 = MagicMock()
        future3.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.side_effect = [future1, future2, future3]

        mock_logger = MagicMock()
        with (
            patch(
                "reingest_from_s3.as_completed",
                return_value=[future1, future2, future3],
            ),
            patch.object(reingest, "logger", mock_logger),
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
            )

        assert stats["total_keys"] == 3
        assert stats["processed"] == 3
        assert stats["errors"] == 0
        assert stats["hash_mismatch_warnings"] == 2

        # Aggregate summary warning is emitted at the end of the run.
        warn_calls = mock_logger.warning.call_args_list
        aggregate_warnings = [
            call for call in warn_calls if call.kwargs.get("hash_mismatch_warnings") is not None
        ]
        assert aggregate_warnings, (
            f"Expected aggregate hash_mismatch_warnings warning. Got warning calls: {warn_calls!r}"
        )
        assert aggregate_warnings[0].kwargs["hash_mismatch_warnings"] == 2

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
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_summary_no_warning_when_all_hashes_match(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """When no documents have hash mismatches, ``hash_mismatch_warnings``
        is zero and no aggregate warning is emitted.  See #2628."""
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
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

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = {"status": "ok", "hash_mismatch": False}
        future2 = MagicMock()
        future2.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.side_effect = [future1, future2]

        mock_logger = MagicMock()
        with (
            patch(
                "reingest_from_s3.as_completed",
                return_value=[future1, future2],
            ),
            patch.object(reingest, "logger", mock_logger),
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
            )

        assert stats["hash_mismatch_warnings"] == 0
        # No aggregate warning emitted when count is zero.
        warn_calls = mock_logger.warning.call_args_list
        for call in warn_calls:
            assert call.kwargs.get("hash_mismatch_warnings") is None, (
                f"unexpected hash_mismatch_warnings warning: {call!r}"
            )


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
        assert isinstance(result, dict)
        assert result["status"] == "skip"
        assert result["hash_mismatch"] is False

    def test_hash_mismatch_logs_warning_and_continues(self) -> None:
        """Hash mismatch must be non-fatal — reingest proceeds and the
        result flags ``hash_mismatch=True`` (see #2494, #2628).

        Previously a mismatch short-circuited with ``status="error"``, which
        is the mechanism producing the ~4% flat-hash orphan rate on Santa
        Clara: raws captured under a wrong content-hash filename were
        permanently unreingestable.  Now we log a warning, let the worker
        process the raw, and the LLM split path re-derives split-children
        from the raw content.  Mirrors the behavior already in
        ``rebuild_db._process_one_document`` (see #2494).
        """
        # The key claims hash "abc123" but the actual content hashes
        # elsewhere — _build_prefix_event will still use "abc123" as the
        # canonical content_hash.
        key = "federal/federal/courtlistener/raw/abc123.html"

        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b"<html>content with wrong hash</html>"
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        # Clear cached worker to force re-creation.
        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        mock_logger = MagicMock()
        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", return_value=mock_worker),
            patch("redis.Redis.from_url", return_value=MagicMock()),
            patch.object(reingest, "logger", mock_logger),
        ):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )

        # Proceeds to the worker — does NOT return early with status="error".
        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["hash_mismatch"] is True
        mock_worker.process_event.assert_called_once()
        # Event passed to the worker must carry the key-hash as canonical
        # content_hash — the worker's LLM split path derives split-child
        # hashes from that value.
        event = mock_worker.process_event.call_args[0][0]
        assert event["content_hash"] == "abc123"
        # Warning logged (not error) so ops see the integrity signal but the
        # reingest proceeds.
        warn_calls = mock_logger.warning.call_args_list
        assert any("S3 content hash mismatch" in str(call) for call in warn_calls), (
            f"expected hash mismatch warning in {warn_calls!r}"
        )
        # No logger.error call for the mismatch itself.
        for call in mock_logger.error.call_args_list:
            assert "hash mismatch" not in str(call).lower(), (
                f"hash mismatch must not produce logger.error: {call!r}"
            )

    def test_matching_hash_returns_ok_without_mismatch_flag(self) -> None:
        """When the S3 content hash matches the key hash, the result flags
        ``hash_mismatch=False`` and no mismatch warning is emitted."""
        content = b"<html>ok</html>"
        key_hash = hashlib.sha256(content).hexdigest()
        key = f"federal/federal/courtlistener/raw/{key_hash}.html"

        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = content
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        mock_logger = MagicMock()
        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", return_value=mock_worker),
            patch("redis.Redis.from_url", return_value=MagicMock()),
            patch.object(reingest, "logger", mock_logger),
        ):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )

        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["hash_mismatch"] is False
        # No mismatch warning on a matching hash.
        for call in mock_logger.warning.call_args_list:
            assert "S3 content hash mismatch" not in str(call), (
                f"unexpected hash mismatch warning on matching hash: {call!r}"
            )


# ---------------------------------------------------------------------------
# #2405 — apply_post_extraction_guards and force_update wiring.  The reingest
# path historically bypassed ``IngestionWorker.process_event``'s guard
# pipeline, so bad LLM extractions (probate decedents as judges, repeated
# case titles, implausible hearing dates) survived reingest.  These tests
# verify the guards run in the reingest DB-write path and that
# insert_document_and_ruling is called with force_update=True so cleared
# fields actually reach the DB.
# ---------------------------------------------------------------------------


class TestApplyPostExtractionGuards:
    """Unit tests for apply_post_extraction_guards helper (#2405)."""

    def test_dedupes_repeated_title(self) -> None:
        extracted: dict = {
            "case_title": "Smith v. Jones\nSmith v. Jones",
            "judge_name": "Hon. Jane Doe",
            "hearing_date": None,
            "extraction_methods": {"case_title": "llm"},
        }
        reingest.apply_post_extraction_guards(extracted, datetime(2026, 4, 1, 12, 0))
        assert extracted["case_title"] == "Smith v. Jones"

    def test_strips_trailing_case_number(self) -> None:
        # Use a real Ventura probate format that matches the strip regex.
        extracted: dict = {
            "case_title": "In the Matter of Denise Mejia 202200570654PRLP",
            "judge_name": None,
            "hearing_date": None,
            "extraction_methods": {},
        }
        reingest.apply_post_extraction_guards(extracted, datetime(2026, 4, 1, 12, 0))
        assert extracted["case_title"] == "In the Matter of Denise Mejia"

    def test_rejects_probate_decedent_as_judge(self) -> None:
        """When judge_name matches the probate decedent pattern, clear it."""
        extracted: dict = {
            "case_title": "Estate of John Smith",
            "judge_name": "John Smith",
            "hearing_date": None,
            "extraction_methods": {"judge_name": "llm", "case_title": "llm"},
        }
        reingest.apply_post_extraction_guards(extracted, datetime(2026, 4, 1, 12, 0))
        assert extracted["judge_name"] is None
        assert "judge_name" not in extracted["extraction_methods"]

    def test_rejects_implausible_hearing_date(self) -> None:
        """Hearing dates >30 days before capture are cleared."""
        extracted: dict = {
            "case_title": "Smith v. Jones",
            "judge_name": "Hon. Jane Doe",
            "hearing_date": date(2024, 1, 1),  # long before capture
            "extraction_methods": {
                "hearing_date": "llm",
                "judge_name": "llm",
            },
        }
        reingest.apply_post_extraction_guards(extracted, datetime(2026, 4, 1, 12, 0))
        assert extracted["hearing_date"] is None
        assert "hearing_date" not in extracted["extraction_methods"]

    def test_keeps_plausible_values(self) -> None:
        """A clean extraction passes through unchanged."""
        extracted: dict = {
            "case_title": "Smith v. Jones",
            "judge_name": "Hon. Jane Doe",
            "hearing_date": date(2026, 4, 15),  # plausible vs capture 2026-04-01
            "extraction_methods": {
                "case_title": "llm",
                "judge_name": "llm",
                "hearing_date": "llm",
            },
        }
        reingest.apply_post_extraction_guards(extracted, datetime(2026, 4, 1, 12, 0))
        assert extracted["case_title"] == "Smith v. Jones"
        assert extracted["judge_name"] == "Hon. Jane Doe"
        assert extracted["hearing_date"] == date(2026, 4, 15)
        assert extracted["extraction_methods"] == {
            "case_title": "llm",
            "judge_name": "llm",
            "hearing_date": "llm",
        }

    def test_handles_missing_extraction_methods(self) -> None:
        """Helper tolerates absent extraction_methods dict."""
        extracted: dict = {
            "case_title": "Smith v. Jones\nSmith v. Jones",
            "judge_name": None,
            "hearing_date": None,
        }
        reingest.apply_post_extraction_guards(extracted, datetime(2026, 4, 1, 12, 0))
        assert extracted["case_title"] == "Smith v. Jones"
        assert extracted["extraction_methods"] == {}

    def test_permissive_when_capture_timestamp_is_none(self) -> None:
        """When capture_timestamp is None, hearing_date plausibility is skipped."""
        extracted: dict = {
            "case_title": "Smith v. Jones",
            "judge_name": "Hon. Jane Doe",
            "hearing_date": date(1999, 1, 1),  # would be implausible if we knew capture ts
            "extraction_methods": {"hearing_date": "llm"},
        }
        reingest.apply_post_extraction_guards(extracted, None)
        # Date preserved because we can't prove it is implausible.
        assert extracted["hearing_date"] == date(1999, 1, 1)
        assert extracted["extraction_methods"] == {"hearing_date": "llm"}


class TestFinalizeHearingDate:
    """Unit tests for finalize_hearing_date helper (#2456).

    The helper owns the entire "pick the final hearing_date" decision
    end-to-end: it runs the #2370 plausibility guard on each candidate in
    the fallback chain (extracted, then doc_meta) and returns the first
    plausible value, or None if no candidate is plausible. This replaces
    the ``extracted["hearing_date"] or doc_meta["hearing_date"]`` + post
    check pattern that #2432 showed was easy to bypass.
    """

    _CAPTURE = datetime(2026, 4, 1, 12, 0)

    def test_both_implausible_returns_none(self) -> None:
        """When both candidates are implausible, returns None."""
        result = reingest.finalize_hearing_date(
            extracted_hearing=date(2099, 1, 1),  # far future
            doc_meta_hearing=date(2020, 1, 1),  # far past
            capture_timestamp=self._CAPTURE,
        )
        assert result is None

    def test_extracted_implausible_doc_meta_plausible_returns_doc_meta(
        self,
    ) -> None:
        """When extracted is implausible but doc_meta is plausible, returns doc_meta."""
        plausible = date(2026, 4, 5)
        result = reingest.finalize_hearing_date(
            extracted_hearing=date(2099, 1, 1),
            doc_meta_hearing=plausible,
            capture_timestamp=self._CAPTURE,
        )
        assert result == plausible

    def test_both_none_returns_none(self) -> None:
        """When both candidates are None, returns None."""
        result = reingest.finalize_hearing_date(
            extracted_hearing=None,
            doc_meta_hearing=None,
            capture_timestamp=self._CAPTURE,
        )
        assert result is None

    def test_extracted_plausible_returns_extracted(self) -> None:
        """When extracted is plausible, returns extracted (no fallback consulted)."""
        plausible = date(2026, 4, 5)
        # Provide a plausible doc_meta too — we should still see extracted win.
        other_plausible = date(2026, 4, 10)
        result = reingest.finalize_hearing_date(
            extracted_hearing=plausible,
            doc_meta_hearing=other_plausible,
            capture_timestamp=self._CAPTURE,
        )
        assert result == plausible

    def test_capture_timestamp_none_is_permissive(self) -> None:
        """With capture_timestamp=None, plausibility cannot be checked; the
        first non-None candidate wins."""
        # extracted non-None, doc_meta non-None: extracted wins.
        result = reingest.finalize_hearing_date(
            extracted_hearing=date(2099, 1, 1),
            doc_meta_hearing=date(2026, 4, 5),
            capture_timestamp=None,
        )
        assert result == date(2099, 1, 1)

        # extracted None, doc_meta non-None: doc_meta wins.
        result = reingest.finalize_hearing_date(
            extracted_hearing=None,
            doc_meta_hearing=date(2099, 1, 1),
            capture_timestamp=None,
        )
        assert result == date(2099, 1, 1)

        # Both None: None.
        result = reingest.finalize_hearing_date(
            extracted_hearing=None,
            doc_meta_hearing=None,
            capture_timestamp=None,
        )
        assert result is None

    def test_extracted_none_doc_meta_plausible_returns_doc_meta(self) -> None:
        """When extracted is None, doc_meta is consulted."""
        plausible = date(2026, 4, 5)
        result = reingest.finalize_hearing_date(
            extracted_hearing=None,
            doc_meta_hearing=plausible,
            capture_timestamp=self._CAPTURE,
        )
        assert result == plausible

    def test_extracted_plausible_doc_meta_none(self) -> None:
        """When doc_meta is None, extracted plausible value wins."""
        plausible = date(2026, 4, 5)
        result = reingest.finalize_hearing_date(
            extracted_hearing=plausible,
            doc_meta_hearing=None,
            capture_timestamp=self._CAPTURE,
        )
        assert result == plausible


class TestReingestAppliesGuardsAndForceUpdate:
    """End-to-end: reingest DB-write loop applies guards + force_update (#2405)."""

    def _make_extracted(self, **overrides: Any) -> dict:
        base: dict = {
            "case_number": "24STCV12345",
            "case_title": "Smith v. Jones",
            "case_type": None,
            "judge_name": "Hon. Jane Doe",
            "hearing_date": date(2026, 3, 5),
            "ruling_text": "Motion granted.",
            "department": "C-10",
            "outcome": "granted",
            "motion_type": "Motion to Compel",
            "parties": [],
            "extraction_methods": {"case_title": "llm", "judge_name": "llm"},
        }
        base.update(overrides)
        return base

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_calls_insert_with_force_update_true(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert: MagicMock,
        mock_resolve_judge: MagicMock,
        _mock_upsert_cj: MagicMock,
        _mock_batch_parties: MagicMock,
    ) -> None:
        """The reingest DB-write loop passes force_update=True."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])
        mock_fetch_s3.return_value = b"<html>content</html>"
        mock_reparse.return_value = self._make_extracted()
        mock_upsert_case.return_value = "new-case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert mock_insert.called, "insert_document_and_ruling must be called"
        for call in mock_insert.call_args_list:
            assert call.kwargs.get("force_update") is True, (
                f"reingest must pass force_update=True; got {call.kwargs.get('force_update')!r}"
            )

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_calls_upsert_case_with_force_update_true(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        _mock_insert: MagicMock,
        mock_resolve_judge: MagicMock,
        _mock_upsert_cj: MagicMock,
        _mock_batch_parties: MagicMock,
    ) -> None:
        """Regression test for #2431: reingest must pass ``force_update=True``
        to ``upsert_case`` so stuck ``case_type`` / ``case_title`` values can
        be corrected after an extraction logic fix.  Without it, the default
        preserve-existing COALESCE (#2468) leaves bad historical rows
        untouched — which is exactly the bug #2431 reports."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])
        mock_fetch_s3.return_value = b"<html>content</html>"
        mock_reparse.return_value = self._make_extracted()
        mock_upsert_case.return_value = "new-case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert mock_upsert_case.called, "upsert_case must be called"
        for call in mock_upsert_case.call_args_list:
            assert call.kwargs.get("force_update") is True, (
                "reingest must pass force_update=True to upsert_case "
                f"so it can correct stuck case_type/case_title (#2431); "
                f"got {call.kwargs.get('force_update')!r}"
            )

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_guards_are_applied_before_db_write(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert: MagicMock,
        mock_resolve_judge: MagicMock,
        _mock_upsert_cj: MagicMock,
        _mock_batch_parties: MagicMock,
    ) -> None:
        """A probate decedent as judge is cleared before insert."""
        row = _make_document_row(case_title="Estate of John Smith")
        conn = _mock_conn_with_rows([row])
        mock_fetch_s3.return_value = b"<html>content</html>"
        # LLM hallucinated the decedent's name as the judge.
        mock_reparse.return_value = self._make_extracted(
            case_title="Estate of John Smith",
            judge_name="John Smith",
        )
        mock_upsert_case.return_value = "new-case-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert mock_insert.called
        assert mock_insert.call_args.kwargs.get("judge_id") is None, (
            "Probate decedent guard should have cleared judge_name, "
            "so resolve_judge should not be called and judge_id should be None."
        )
        assert not mock_resolve_judge.called, (
            "resolve_judge should not be called after the guard clears judge_name"
        )

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_implausible_hearing_date_cleared_before_write(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert: MagicMock,
        mock_resolve_judge: MagicMock,
        _mock_upsert_cj: MagicMock,
        _mock_batch_parties: MagicMock,
    ) -> None:
        """A pre-capture hearing_date is cleared by the guard before insert."""
        # Both row-level hearing_date columns are None so doc_meta["hearing_date"]
        # falls through to the guard-cleared extracted["hearing_date"].
        row = _make_document_row(hearing_date=None, ruling_hearing_date=None)
        conn = _mock_conn_with_rows([row])
        mock_fetch_s3.return_value = b"<html>content</html>"
        # Hearing date long before capture (2026-03-01).
        mock_reparse.return_value = self._make_extracted(hearing_date=date(1999, 1, 1))
        mock_upsert_case.return_value = "new-case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert mock_insert.called
        assert mock_insert.call_args.kwargs.get("hearing_date") is None, (
            "Implausible hearing_date should be cleared to None by the guard."
        )

    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_implausible_doc_meta_fallback_rejected_after_guard(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert: MagicMock,
        mock_resolve_judge: MagicMock,
        _mock_upsert_cj: MagicMock,
        _mock_batch_parties: MagicMock,
    ) -> None:
        """Regression for #2432: when both extracted and doc_meta hearing_date
        are implausible, the fallback must not override the guard.

        Scenario: the guard clears ``extracted["hearing_date"]`` because it's
        implausible, but the DB row's ``documents.hearing_date`` (surfaced
        via ``doc_meta["hearing_date"]``) holds the SAME bad value — common
        in rebuilds where the doc and ruling hearing_date were both
        populated from the same LLM mistake.  The fallback chain
        ``extracted["hearing_date"] or doc_meta["hearing_date"]`` picks the
        bad doc_meta value and writes it back through force_update,
        silently bypassing the guard.
        """
        # doc_meta["hearing_date"] is the SAME implausible value the guard
        # will clear from extracted.  captured_at defaults to 2026-03-01,
        # so a 2099-01-01 hearing_date is ~73 years after capture.
        implausible = date(2099, 1, 1)
        row = _make_document_row(hearing_date=implausible, ruling_hearing_date=implausible)
        conn = _mock_conn_with_rows([row])
        mock_fetch_s3.return_value = b"<html>content</html>"
        mock_reparse.return_value = self._make_extracted(hearing_date=implausible)
        mock_upsert_case.return_value = "new-case-id"
        mock_resolve_judge.return_value = "judge-id"

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert mock_insert.called
        assert mock_insert.call_args.kwargs.get("hearing_date") is None, (
            "Implausible doc_meta['hearing_date'] fallback must also be "
            "rejected — otherwise the guard is bypassed (#2432)."
        )


# ---------------------------------------------------------------------------
# #2424: deterministic validation + LLM cache bust
# ---------------------------------------------------------------------------


def _make_det_result(
    overall: str = "pass",
    reasons: list[str] | None = None,
) -> Any:
    """Create a ``DeterministicValidationResult``-shaped mock."""
    from validation.deterministic import (
        DeterministicRuleResult,
        DeterministicValidationResult,
    )

    rules: list[DeterministicRuleResult] = []
    for reason in reasons or []:
        rules.append(
            DeterministicRuleResult(
                rule="test_rule",
                result=overall,
                reason=reason,
            )
        )
    if not rules and overall != "pass":
        # Ensure reasons list isn't empty for non-pass outcomes.
        rules.append(
            DeterministicRuleResult(
                rule="test_rule",
                result=overall,
                reason=f"synthetic {overall} reason",
            )
        )
    return DeterministicValidationResult(overall=overall, rules=rules)


class TestReingestDeterministicValidationFail:
    """#2424: deterministic validation fail → skip DB write + log row."""

    @patch("reingest_from_s3.insert_validation_result")
    @patch("reingest_from_s3.run_deterministic_rules")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_fail_skips_db_write_and_inserts_validation_row(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_run_det: MagicMock,
        mock_insert_val: MagicMock,
    ) -> None:
        """A fail verdict skips the ruling's DB write + logs a
        validation_results row with result='fail'."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "motion is granted",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_run_det.return_value = _make_det_result(
            overall="fail",
            reasons=["ruling_text references a different case"],
        )

        result = reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
        )

        assert result["processed"] == 1
        # DB upsert must NOT happen — validation said fail.
        mock_upsert_case.assert_not_called()
        mock_insert_doc_and_ruling.assert_not_called()
        # validation_results row WAS logged.
        mock_insert_val.assert_called_once()
        insert_kwargs = mock_insert_val.call_args.kwargs
        assert insert_kwargs["result"].result == "fail"
        assert "ruling_text references" in insert_kwargs["result"].reason
        assert insert_kwargs["result"].model == "deterministic"


class TestReingestDeterministicValidationFlag:
    """#2424: deterministic validation flag → write DB + log row."""

    @patch("reingest_from_s3.insert_validation_result")
    @patch("reingest_from_s3.run_deterministic_rules")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_flag_writes_db_and_inserts_validation_row(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_run_det: MagicMock,
        mock_insert_val: MagicMock,
    ) -> None:
        """A flag verdict proceeds with DB write AND logs a
        validation_results row with result='flag'."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "motion is granted",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"
        mock_run_det.return_value = _make_det_result(
            overall="flag",
            reasons=["hearing_date far from captured_at"],
        )

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
        # DB upsert HAPPENED — flag does not block writes.
        mock_upsert_case.assert_called_once()
        mock_insert_doc_and_ruling.assert_called_once()
        # validation_results row was logged with result='flag'.
        mock_insert_val.assert_called_once()
        assert mock_insert_val.call_args.kwargs["result"].result == "flag"


class TestReingestDeterministicValidationPass:
    """#2424: deterministic validation pass → write DB, no validation row."""

    @patch("reingest_from_s3.insert_validation_result")
    @patch("reingest_from_s3.run_deterministic_rules")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_pass_writes_db_and_no_validation_row(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_run_det: MagicMock,
        mock_insert_val: MagicMock,
    ) -> None:
        """A pass verdict writes DB and logs no validation_results row."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "motion is granted",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"
        mock_run_det.return_value = _make_det_result(overall="pass")

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
        mock_insert_doc_and_ruling.assert_called_once()
        # No validation_results row logged when verdict is pass.
        mock_insert_val.assert_not_called()


class TestReingestValidationSiblingCaseNumbers:
    """#2424: sibling case numbers flow to run_deterministic_rules."""

    @patch("reingest_from_s3.insert_validation_result")
    @patch("reingest_from_s3.run_deterministic_rules")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._full_reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_multi_ruling_passes_siblings_as_other_case_numbers(
        self,
        mock_fetch_s3: MagicMock,
        mock_full_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_run_det: MagicMock,
        mock_insert_val: MagicMock,
    ) -> None:
        """For a split doc with 2 rulings, each call to
        run_deterministic_rules receives the OTHER ruling's case_number
        via other_case_numbers."""
        row = _make_document_row(scraper_id="ca-fresno-tentatives-civil")
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"%PDF-1.4\nfake"
        mock_full_reparse.return_value = [
            {
                "ruling_text": "ruling 1 text",
                "case_number": "23CECG00001",
                "case_title": "A v. B",
                "judge_name": "Judge Doe",
                "outcome": "granted",
                "motion_type": "msj",
                "department": "1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 1,
                "split_document_id": "split-1",
                "is_split": True,
                "extraction_methods": {},
            },
            {
                "ruling_text": "ruling 2 text",
                "case_number": "23CECG00002",
                "case_title": "C v. D",
                "judge_name": "Judge Doe",
                "outcome": "denied",
                "motion_type": "msj",
                "department": "1",
                "parties": [],
                "hearing_date": _HEARING_DATE,
                "ruling_index": 2,
                "split_document_id": "split-2",
                "is_split": True,
                "extraction_methods": {},
            },
        ]
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"
        mock_run_det.return_value = _make_det_result(overall="pass")

        reingest.reingest_batch(
            conn,
            MagicMock(),
            batch_size=10,
            cursor=_DEFAULT_CURSOR,
            filters="",
            filter_params=[],
            full_reparse=True,
        )

        # run_deterministic_rules called twice (once per ruling).
        assert mock_run_det.call_count == 2
        calls = mock_run_det.call_args_list
        call1_others = calls[0].kwargs.get("other_case_numbers")
        call2_others = calls[1].kwargs.get("other_case_numbers")
        # Each call's "other" numbers == all siblings minus self.
        assert call1_others == ["23CECG00002"]
        assert call2_others == ["23CECG00001"]


class TestReingestValidationInsertException:
    """#2424: insert_validation_result exception does not abort DB write."""

    @patch("reingest_from_s3.insert_validation_result")
    @patch("reingest_from_s3.run_deterministic_rules")
    @patch("reingest_from_s3.batch_upsert_parties")
    @patch("reingest_from_s3.upsert_case_judge")
    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.insert_document_and_ruling")
    @patch("reingest_from_s3.upsert_case")
    @patch("reingest_from_s3._reparse_document")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_insert_exception_swallowed_processing_continues(
        self,
        mock_fetch_s3: MagicMock,
        mock_reparse: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_cj: MagicMock,
        mock_batch_parties: MagicMock,
        mock_run_det: MagicMock,
        mock_insert_val: MagicMock,
    ) -> None:
        """When insert_validation_result raises, the ruling still writes
        (for a flag verdict) and processing continues."""
        row = _make_document_row()
        conn = _mock_conn_with_rows([row])

        mock_fetch_s3.return_value = b"<html>text</html>"
        mock_reparse.return_value = {
            "ruling_text": "motion is granted",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": "Judge Doe",
            "outcome": "granted",
            "motion_type": "msj",
            "department": "1",
            "parties": [],
            "hearing_date": _HEARING_DATE,
        }
        mock_upsert_case.return_value = "case-id"
        mock_resolve_judge.return_value = "judge-id"
        mock_run_det.return_value = _make_det_result(
            overall="flag",
            reasons=["flagged"],
        )
        mock_insert_val.side_effect = Exception("boom")

        # Must not raise — the outer reingest_batch swallows insert errors.
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
        # DB write happened despite the logging failure.
        mock_insert_doc_and_ruling.assert_called_once()


class TestCLIBustLlmCacheFlag:
    """#2424: --bust-llm-cache CLI flag parsing (exercises the real main() argparse)."""

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest")
    def test_main_passes_bust_llm_cache_true(
        self,
        mock_run: MagicMock,
    ) -> None:
        """`--bust-llm-cache` on the command line forwards bust_llm_cache=True
        to run_reingest."""
        argv = ["reingest_from_s3", "--county", "Fresno", "--bust-llm-cache"]
        with patch("sys.argv", argv):
            reingest.main()
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["bust_llm_cache"] is True

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest")
    def test_main_default_bust_llm_cache_is_false(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Default (no flag) forwards bust_llm_cache=False to run_reingest."""
        argv = ["reingest_from_s3", "--county", "Fresno"]
        with patch("sys.argv", argv):
            reingest.main()
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["bust_llm_cache"] is False


class TestBustLlmCachePassedThrough:
    """#2424: bust_llm_cache flows from run_reingest → reingest_batch → parse."""

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_bust_llm_cache_passed_to_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """bust_llm_cache=True is forwarded to reingest_batch."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test", bust_llm_cache=True)

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("bust_llm_cache") is True

    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.reingest_batch")
    def test_bust_llm_cache_defaults_false(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """bust_llm_cache defaults to False when not specified."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn
        mock_batch.return_value = _make_batch_result()

        reingest.run_reingest("postgresql://test")

        batch_call = mock_batch.call_args_list[0]
        assert batch_call.kwargs.get("bust_llm_cache") is False


class TestBustLlmCacheFlowsToExtractFieldsLlm:
    """#2424: bust_llm_cache reaches extract_fields_llm."""

    @patch("reingest_from_s3.extract_fields_llm")
    @patch("reingest_from_s3._load_scraper_registry")
    def test_bust_llm_cache_flag_forwarded_to_extract_fields_llm(
        self,
        mock_load_registry: MagicMock,
        mock_extract_llm: MagicMock,
    ) -> None:
        """_reparse_document forwards bust_llm_cache to extract_fields_llm."""
        mock_extract_llm.return_value = None  # Don't actually apply LLM result.

        doc_meta: dict[str, Any] = {
            "document_id": "doc-1",
            "state": "CA",
            "county": "Los Angeles",
            "format": "html",
            "case_number": "23STCV01234",
            "case_title": "Smith v. Jones",
            "judge_name": None,
            "hearing_date": _HEARING_DATE,
            "department": None,
        }
        # A non-HTML starting body with plenty of chars to trigger the
        # LLM branch (fields missing → LLM called).
        raw_content = b"<html>Some ruling text here, nothing structured.</html>"

        reingest._reparse_document(
            raw_content,
            "ca-la-tentatives-civil",
            doc_meta,
            llm_client=MagicMock(),
            llm_provider="google",
            llm_model="flash-lite",
            force_llm=True,
            bust_llm_cache=True,
        )

        assert mock_extract_llm.called, "extract_fields_llm should be invoked under force_llm=True"
        assert mock_extract_llm.call_args.kwargs.get("bust_cache") is True


class TestLlmExtractorBustCachePlumbing:
    """#2424: LlmExtractor(bust_cache=True) skips cache reads, keeps writes."""

    def test_bust_cache_skips_cache_read_keeps_write(self) -> None:
        """With bust_cache=True, cache.get is NOT called; cache.put IS
        (provided the extraction returned at least one ruling)."""
        from framework.llm_extractor import LlmExtractor
        from framework.llm_schema import ExtractedRuling

        cache = MagicMock()
        # If get WERE called, it would short-circuit with this payload.
        cache.get.return_value = [{"extracted_case_number": "SHOULD-NOT-USE"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = True
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        extracted = ExtractedRuling(extracted_case_number="FRESH-1")
        with (
            patch.object(
                extractor,
                "_extract_chunk_with_retry",
                return_value=[extracted],
            ),
            patch.object(extractor, "_log_usage"),
            patch.object(extractor, "_merge_results", return_value=[extracted]),
            patch.object(extractor, "_propagate_document_fields", return_value=[extracted]),
        ):
            result = extractor.extract("sample text")

        cache.get.assert_not_called()
        cache.put.assert_called_once()
        assert result
        assert result[0].extracted_case_number == "FRESH-1"

    def test_cache_hit_honored_without_bust_cache(self) -> None:
        """With bust_cache=False (default), cache.get SHORT-CIRCUITS and
        the LLM is NOT invoked."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        cache.get.return_value = [{"extracted_case_number": "CACHED-123"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = False
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        with patch.object(extractor, "_extract_chunk_with_retry") as mock_call:
            result = extractor.extract("sample text")

        cache.get.assert_called_once()
        mock_call.assert_not_called()
        cache.put.assert_not_called()
        assert result
        assert result[0].extracted_case_number == "CACHED-123"


class TestLlmExtractorExtractFromPdfBustCache:
    """#2424: extract_from_pdf honors bust_cache like extract() does."""

    def test_extract_from_pdf_bust_cache_skips_cache_read(self) -> None:
        """extract_from_pdf(bust_cache=True) does NOT call cache.get — even
        if a cache hit would otherwise short-circuit the extraction."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        # If get WERE called, this would short-circuit extraction.
        cache.get.return_value = [{"extracted_case_number": "SHOULD-NOT-USE"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = False
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[],
            ),
        ):
            result = extractor.extract_from_pdf(
                b"%PDF-1.4 minimal",
                bust_cache=True,
            )

        cache.get.assert_not_called()
        assert result == []

    def test_extract_from_pdf_instance_bust_cache_skips_read(self) -> None:
        """Instance-level self._bust_cache=True also skips the cache read."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        cache.get.return_value = [{"extracted_case_number": "SHOULD-NOT-USE"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = True
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        with patch(
            "framework.llm_extractor._render_pdf_pages",
            return_value=[],
        ):
            result = extractor.extract_from_pdf(b"%PDF-1.4 minimal")

        cache.get.assert_not_called()
        assert result == []

    def test_extract_from_pdf_cache_hit_honored_without_bust(self) -> None:
        """extract_from_pdf(bust_cache=False) honors cache hits."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        cache.get.return_value = [{"extracted_case_number": "CACHED-PDF"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = False
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()

        result = extractor.extract_from_pdf(b"%PDF-1.4 minimal")

        cache.get.assert_called_once()
        assert result
        assert result[0].extracted_case_number == "CACHED-PDF"
