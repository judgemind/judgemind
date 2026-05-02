"""Tests for the rebuild_db script.

Verifies build_event hearing_date extraction, _process_one_document return
format, and rebuild summary counters.  All I/O (DB, S3, Redis, worker) is
mocked.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)

# Ensure the scraper-framework src is importable
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC_DIR))

rebuild_db = importlib.import_module("rebuild_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parsed(
    *,
    state: str = "ca",
    county: str = "orange",
    court: str = "superior_court",
    content_hash: str = "abc123def456",
    ext: str = "html",
) -> dict[str, str]:
    """Build a parsed S3 key metadata dict."""
    return {
        "state": state,
        "county": county,
        "court": court,
        "content_hash": content_hash,
        "ext": ext,
    }


def _make_key(parsed: dict[str, str]) -> str:
    """Build an S3 key string from parsed metadata."""
    return (
        f"{parsed['state']}/{parsed['county']}/{parsed['court']}"
        f"/raw/{parsed['content_hash']}.{parsed['ext']}"
    )


# ---------------------------------------------------------------------------
# build_event tests
# ---------------------------------------------------------------------------


class TestBuildEvent:
    """Tests for build_event()."""

    def test_html_with_hearing_date_in_text(self) -> None:
        """HTML content containing a date string should populate hearing_date."""
        html_content = b"<html><body>Hearing Date: March 15, 2026</body></html>"
        parsed = _make_parsed(ext="html")
        key = _make_key(parsed)

        event = rebuild_db.build_event(key, html_content, parsed, "test-bucket")

        assert event["content_format"] == "html"
        assert event["hearing_date"] == "2026-03-15"
        assert event["ruling_text"] == html_content.decode("utf-8")

    def test_html_without_hearing_date(self) -> None:
        """HTML content without a recognizable date should not have hearing_date."""
        html_content = b"<html><body>No date here</body></html>"
        parsed = _make_parsed(ext="html")
        key = _make_key(parsed)

        event = rebuild_db.build_event(key, html_content, parsed, "test-bucket")

        assert event["content_format"] == "html"
        assert "hearing_date" not in event

    def test_html_with_date_prefix_format(self) -> None:
        """HTML with 'Date: MM/DD/YYYY' format should extract hearing_date."""
        html_content = b"<html>Date: 03/15/2026 some ruling text</html>"
        parsed = _make_parsed(ext="html")
        key = _make_key(parsed)

        event = rebuild_db.build_event(key, html_content, parsed, "test-bucket")

        assert event["hearing_date"] == "2026-03-15"

    def test_pdf_no_hearing_date(self) -> None:
        """PDF content should NOT attempt hearing_date extraction in build_event."""
        # PDF raw bytes encoded as latin-1 — regex patterns won't match
        pdf_content = b"%PDF-1.4 binary content"
        parsed = _make_parsed(ext="pdf")
        key = _make_key(parsed)

        event = rebuild_db.build_event(key, pdf_content, parsed, "test-bucket")

        assert event["content_format"] == "pdf"
        assert "hearing_date" not in event
        assert event["ruling_text"] == pdf_content.decode("latin-1")

    def test_event_has_required_fields(self) -> None:
        """Every event should have the standard required fields."""
        content = b"<html>some content</html>"
        parsed = _make_parsed()
        key = _make_key(parsed)

        event = rebuild_db.build_event(key, content, parsed, "test-bucket")

        assert "document_id" in event
        assert event["state"] == "CA"
        assert event["county"] == "Orange"
        assert event["court"] == "Superior Court"
        assert event["s3_key"] == key
        assert event["s3_bucket"] == "test-bucket"
        assert event["scraper_id"] == "rebuild-ca-orange"
        assert event["capture_timestamp"]

    def test_txt_content_decoded_as_utf8(self) -> None:
        """TXT content should be decoded as utf-8 and set as ruling_text."""
        txt_content = b"This is a plain text ruling document."
        parsed = _make_parsed(ext="txt")
        key = _make_key(parsed)

        event = rebuild_db.build_event(key, txt_content, parsed, "test-bucket")

        assert event["content_format"] == "txt"
        assert event["ruling_text"] == txt_content.decode("utf-8")

    def test_docx_content_decoded_as_latin1(self) -> None:
        """DOCX content should be decoded as latin-1 and set as ruling_text."""
        docx_content = b"PK\x03\x04 fake docx binary content"
        parsed = _make_parsed(ext="docx")
        key = _make_key(parsed)

        event = rebuild_db.build_event(key, docx_content, parsed, "test-bucket")

        assert event["content_format"] == "docx"
        assert event["ruling_text"] == docx_content.decode("latin-1")
        assert "hearing_date" not in event

    def test_html_hearing_date_extraction_import_error(self) -> None:
        """If ingestion.extract is not importable, build_event still works."""
        html_content = b"<html>Hearing Date: March 15, 2026</html>"
        parsed = _make_parsed(ext="html")
        key = _make_key(parsed)

        # Simulate ImportError for extract_hearing_date
        with patch.dict("sys.modules", {"ingestion.extract": None}):
            event = rebuild_db.build_event(key, html_content, parsed, "test-bucket")

        # Should still produce a valid event, just without hearing_date
        assert event["content_format"] == "html"
        assert event["ruling_text"] == html_content.decode("utf-8")


# ---------------------------------------------------------------------------
# _process_one_document tests
# ---------------------------------------------------------------------------


class TestProcessOneDocument:
    """Tests for _process_one_document() return format."""

    def test_returns_dict_with_status_ok(self, tmp_path: Any) -> None:
        """Successful processing should return a dict with status='ok'."""
        # Create a local cache file — use the real SHA256 of the content
        # so the hash check passes.
        content = b"<html>Date: 03/15/2026 ruling text</html>"
        import hashlib

        content_hash = hashlib.sha256(content).hexdigest()
        key = f"ca/orange/superior_court/raw/{content_hash}.html"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / f"{content_hash}.html").write_bytes(content)

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        with patch.object(
            rebuild_db._process_one_document,
            "__wrapped__",
            None,
            create=True,
        ):
            # Clear cached worker to force re-creation
            if hasattr(rebuild_db._process_one_document, "_worker"):
                delattr(rebuild_db._process_one_document, "_worker")

            # Patch the lazy worker creation
            with (
                patch(
                    "ingestion.worker.IngestionWorker",
                    return_value=mock_worker,
                ),
                patch(
                    "redis.Redis.from_url",
                    return_value=MagicMock(),
                ),
                patch(
                    "framework.s3_cache.make_s3_client",
                    return_value=MagicMock(),
                ),
            ):
                result = rebuild_db._process_one_document(
                    key,
                    cache_dir,
                    "test-bucket",
                    "postgres://test",
                    "redis://test",
                    "",
                )

        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["content_format"] == "html"
        assert result["had_hearing_date"] is True

    def test_hash_mismatch_logs_warning_and_continues(self, tmp_path: Any) -> None:
        """Hash mismatch must be non-fatal — the rebuild proceeds and the
        result flags ``hash_mismatch=True`` (see #2494).

        Previously a mismatch short-circuited with ``status="error"``, which
        caused multi-case-PDF counties to lose every split-child on rebuild.
        Now we log a warning, let the worker process the raw PDF, and the
        LLM split path re-derives the split-children from the raw content.
        """
        # The key claims hash "abc123" but the actual content hashes
        # elsewhere — build_event will still use "abc123" as the canonical
        # content_hash.
        key = "ca/orange/superior_court/raw/abc123.html"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / "abc123.html").write_bytes(b"<html>content with wrong hash</html>")

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        # Clear cached worker to force re-creation.
        if hasattr(rebuild_db._process_one_document, "_worker"):
            delattr(rebuild_db._process_one_document, "_worker")

        mock_logger = MagicMock()
        with (
            patch(
                "ingestion.worker.IngestionWorker",
                return_value=mock_worker,
            ),
            patch(
                "redis.Redis.from_url",
                return_value=MagicMock(),
            ),
            patch(
                "framework.s3_cache.make_s3_client",
                return_value=MagicMock(),
            ),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            result = rebuild_db._process_one_document(
                key,
                cache_dir,
                "test-bucket",
                "postgres://test",
                "redis://test",
                "",
            )

        # Proceeds to the worker — does NOT return early with status=error.
        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["content_format"] == "html"
        assert result["hash_mismatch"] is True
        mock_worker.process_event.assert_called_once()
        # Event passed to the worker must carry the key-hash as canonical
        # content_hash — the worker's LLM split path derives split-child
        # hashes from that value.
        event = mock_worker.process_event.call_args[0][0]
        assert event["content_hash"] == "abc123"
        # Warning logged (not error) so ops see the integrity signal but
        # the rebuild proceeds.
        warn_calls = mock_logger.warning.call_args_list
        assert any("S3 content hash mismatch" in str(call) for call in warn_calls), (
            f"expected hash mismatch warning in {warn_calls!r}"
        )

    def test_returns_dict_with_status_skip_for_bad_key(self) -> None:
        """Unparseable keys should return status='skip'."""
        result = rebuild_db._process_one_document(
            "not/a/valid/key",
            "",
            "test-bucket",
            "postgres://test",
            "redis://test",
            "",
        )

        assert isinstance(result, dict)
        assert result["status"] == "skip"

    def test_returns_dict_with_no_hearing_date_for_pdf(self, tmp_path: Any) -> None:
        """PDF documents should report had_hearing_date=False."""
        content = b"%PDF-1.4 binary content here"
        import hashlib

        content_hash = hashlib.sha256(content).hexdigest()
        key = f"ca/orange/superior_court/raw/{content_hash}.pdf"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / f"{content_hash}.pdf").write_bytes(content)

        mock_worker = MagicMock()

        # Clear cached worker
        if hasattr(rebuild_db._process_one_document, "_worker"):
            delattr(rebuild_db._process_one_document, "_worker")

        with (
            patch(
                "ingestion.worker.IngestionWorker",
                return_value=mock_worker,
            ),
            patch(
                "redis.Redis.from_url",
                return_value=MagicMock(),
            ),
            patch(
                "framework.s3_cache.make_s3_client",
                return_value=MagicMock(),
            ),
        ):
            result = rebuild_db._process_one_document(
                key,
                cache_dir,
                "test-bucket",
                "postgres://test",
                "redis://test",
                "",
            )

        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["content_format"] == "pdf"
        assert result["had_hearing_date"] is False

    def test_returns_error_on_worker_exception(self, tmp_path: Any) -> None:
        """Worker exceptions should return status='error'."""
        content = b"<html>some content</html>"
        import hashlib

        content_hash = hashlib.sha256(content).hexdigest()
        key = f"ca/orange/superior_court/raw/{content_hash}.html"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / f"{content_hash}.html").write_bytes(content)

        mock_worker = MagicMock()
        mock_worker.process_event.side_effect = RuntimeError("boom")

        # Clear cached worker
        if hasattr(rebuild_db._process_one_document, "_worker"):
            delattr(rebuild_db._process_one_document, "_worker")

        with (
            patch(
                "ingestion.worker.IngestionWorker",
                return_value=mock_worker,
            ),
            patch(
                "redis.Redis.from_url",
                return_value=MagicMock(),
            ),
            patch(
                "framework.s3_cache.make_s3_client",
                return_value=MagicMock(),
            ),
        ):
            result = rebuild_db._process_one_document(
                key,
                cache_dir,
                "test-bucket",
                "postgres://test",
                "redis://test",
                "",
            )

        assert isinstance(result, dict)
        assert result["status"] == "error"
        assert result["content_format"] == "html"


# ---------------------------------------------------------------------------
# parse_s3_key tests
# ---------------------------------------------------------------------------


class TestParseS3Key:
    """Tests for parse_s3_key()."""

    def test_valid_key(self) -> None:
        key = "ca/orange/superior_court/raw/abc123def456.pdf"
        result = rebuild_db.parse_s3_key(key)
        assert result is not None
        assert result["state"] == "ca"
        assert result["county"] == "orange"
        assert result["court"] == "superior_court"
        assert result["content_hash"] == "abc123def456"
        assert result["ext"] == "pdf"

    def test_invalid_key(self) -> None:
        assert rebuild_db.parse_s3_key("invalid/key") is None


# ---------------------------------------------------------------------------
# unsluggify / sluggify tests
# ---------------------------------------------------------------------------


class TestSlugConversions:
    """Tests for slug conversion utilities."""

    def test_unsluggify_county(self) -> None:
        assert rebuild_db.unsluggify("los_angeles") == "Los Angeles"

    def test_unsluggify_state(self) -> None:
        assert rebuild_db.unsluggify("ca") == "CA"

    def test_sluggify_county(self) -> None:
        assert rebuild_db.sluggify("Los Angeles") == "los_angeles"

    def test_sluggify_state(self) -> None:
        assert rebuild_db.sluggify("CA") == "ca"


# ---------------------------------------------------------------------------
# discover_courts tests
# ---------------------------------------------------------------------------


class TestDeriveCourtCode:
    """Tests for _derive_court_code() — must match ingestion.db._derive_court_code."""

    def test_basic_county(self) -> None:
        assert rebuild_db._derive_court_code("CA", "Orange") == "ca-orange"

    def test_multi_word_county(self) -> None:
        assert rebuild_db._derive_court_code("CA", "Los Angeles") == "ca-los-angeles"

    def test_multi_word_county_three_words(self) -> None:
        assert rebuild_db._derive_court_code("CA", "San Bernardino") == "ca-san-bernardino"

    def test_lowercase_state(self) -> None:
        assert rebuild_db._derive_court_code("ca", "Orange") == "ca-orange"

    def test_federal(self) -> None:
        assert rebuild_db._derive_court_code("Federal", "Federal") == "federal-federal"


class TestDiscoverCourts:
    """Tests for discover_courts()."""

    def test_discovers_unique_courts(self) -> None:
        keys = [
            "ca/orange/superior_court/raw/abc123.pdf",
            "ca/orange/superior_court/raw/def456.pdf",
            "ca/los_angeles/superior_court/raw/aaa789bbb.html",
        ]
        courts = rebuild_db.discover_courts(keys)
        assert len(courts) == 2
        codes = {c["court_code"] for c in courts}
        assert "ca-orange" in codes
        assert "ca-los-angeles" in codes

    def test_court_code_matches_ingestion_format(self) -> None:
        """court_code must use {state}-{county} format to match ingestion.db."""
        keys = ["ca/santa_clara/superior_court/raw/abc123.html"]
        courts = rebuild_db.discover_courts(keys)
        assert len(courts) == 1
        assert courts[0]["court_code"] == "ca-santa-clara"

    def test_court_name_includes_county(self) -> None:
        """court_name should include 'County of' to match ingestion format."""
        keys = ["ca/orange/superior_court/raw/abc123.html"]
        courts = rebuild_db.discover_courts(keys)
        assert courts[0]["court_name"] == "Superior Court, County of Orange"

    def test_skips_invalid_keys(self) -> None:
        keys = ["invalid/key", "also/not/valid"]
        courts = rebuild_db.discover_courts(keys)
        assert len(courts) == 0

    def test_deduplicates_by_court_code(self) -> None:
        """Multiple keys for the same state/county should produce one court."""
        keys = [
            "ca/orange/superior_court/raw/abc123.pdf",
            "ca/orange/superior_court/raw/def456.html",
        ]
        courts = rebuild_db.discover_courts(keys)
        assert len(courts) == 1
        assert courts[0]["court_code"] == "ca-orange"


# ---------------------------------------------------------------------------
# Integration: main() summary output
# ---------------------------------------------------------------------------


class TestMainSummary:
    """Tests that main() reports hearing_date skip counts in the summary."""

    def test_summary_includes_no_hearing_date_warning(self, tmp_path: Any) -> None:
        """When documents lack hearing_date, the summary should warn about it."""
        # Mock the ProcessPoolExecutor to avoid real multiprocessing and
        # control the results.  We patch rebuild_db.logger directly instead
        # of using structlog.configure() which is global state and breaks
        # under pytest-xdist parallel execution.
        mock_logger = MagicMock()

        # Create a local cache with one HTML file (with date) and one PDF (no date)
        cache_dir = str(tmp_path / "cache")
        html_dir = tmp_path / "cache" / "ca" / "orange" / "superior_court" / "raw"
        html_dir.mkdir(parents=True)
        (html_dir / "abc123.html").write_bytes(b"<html>Date: 03/15/2026 ruling text</html>")
        pdf_dir = tmp_path / "cache" / "ca" / "riverside" / "superior_court" / "raw"
        pdf_dir.mkdir(parents=True)
        (pdf_dir / "def456.pdf").write_bytes(b"%PDF-1.4 binary content")

        # Mock the ProcessPoolExecutor to return controlled results
        mock_future_html = MagicMock()
        mock_future_html.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": True,
        }
        mock_future_pdf = MagicMock()
        mock_future_pdf.result.return_value = {
            "status": "ok",
            "content_format": "pdf",
            "had_hearing_date": False,
        }

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.side_effect = [mock_future_html, mock_future_pdf]

        # as_completed returns futures in order
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=[
                    "ca/orange/superior_court/raw/abc123.html",
                    "ca/riverside/superior_court/raw/def456.pdf",
                ],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=mock_pool,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([mock_future_html, mock_future_pdf]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
            ),
            patch("sys.argv", ["rebuild_db.py"]),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            rebuild_db.main()

        # Check that logger.warning was called with no_hearing_date=1.
        # The call signature is: logger.warning(msg, count, no_hearing_date=N, ...)
        warning_calls = mock_logger.warning.call_args_list
        skip_warnings = [
            call for call in warning_calls if call.kwargs.get("no_hearing_date") is not None
        ]
        assert len(skip_warnings) > 0, (
            f"Expected a warning about hearing_date skips. Got warning calls: {warning_calls}"
        )
        # The warning should mention 1 skip (the PDF document)
        assert skip_warnings[0].kwargs["no_hearing_date"] == 1


# ---------------------------------------------------------------------------
# Rebuild marker tests (#2222)
# ---------------------------------------------------------------------------


class TestWriteRebuildMarker:
    """Tests for _write_rebuild_marker in rebuild_db.py."""

    def test_writes_start_marker(self) -> None:
        """Writes metric_value=1.0 when in_progress=True."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        rebuild_db._write_rebuild_marker(mock_conn, in_progress=True)

        mock_cur.execute.assert_called_once()
        params = mock_cur.execute.call_args[0][1]
        # params: (now, county, metric_name, metric_value, metadata)
        assert params[1] == "_system"
        assert params[2] == "rebuild_in_progress"
        assert params[3] == 1.0
        mock_conn.commit.assert_called_once()

    def test_writes_completion_marker(self) -> None:
        """Writes metric_value=0.0 when in_progress=False."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        rebuild_db._write_rebuild_marker(mock_conn, in_progress=False)

        mock_cur.execute.assert_called_once()
        params = mock_cur.execute.call_args[0][1]
        assert params[3] == 0.0
        mock_conn.commit.assert_called_once()

    def test_db_error_does_not_raise(self) -> None:
        """DB errors are caught and logged, not propagated."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.execute.side_effect = Exception("connection lost")

        # Should not raise
        rebuild_db._write_rebuild_marker(mock_conn, in_progress=True)
        mock_conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Per-county reset tests (#2465)
# ---------------------------------------------------------------------------


def _build_per_county_mock_conn(
    court_ids: list[str],
    counts: dict[str, int] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a mock psycopg connection + cursor for per-county reset tests.

    The cursor is driven by a scripted ``execute``/``fetchone``/``fetchall``
    sequence so each DELETE/COUNT call returns deterministic data.  Returns
    ``(mock_conn, mock_cur)`` so tests can inspect call history.
    """
    counts = counts or {}
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # First call: _resolve_county_court_ids → SELECT id FROM derived.courts ...
    # fetchall() for that should return a list of (uuid,) tuples.
    court_id_rows = [(cid,) for cid in court_ids]
    mock_cur.fetchall.return_value = court_id_rows

    # Subsequent fetchone() calls are COUNT(*) results.
    # Order: join tables (case_judges, case_parties, case_attorneys), then
    # court-id tables (documents, rulings, cases, judges).
    count_sequence = [
        (counts.get("case_judges", 0),),
        (counts.get("case_parties", 0),),
        (counts.get("case_attorneys", 0),),
        (counts.get("documents", 0),),
        (counts.get("rulings", 0),),
        (counts.get("cases", 0),),
        (counts.get("judges", 0),),
    ]
    mock_cur.fetchone.side_effect = count_sequence
    return mock_conn, mock_cur


class TestResolveCountyCourtIds:
    """Tests for _resolve_county_court_ids()."""

    def test_returns_list_of_court_ids(self) -> None:
        """Returns string UUIDs from derived.courts matching state/county."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = [
            ("00000000-0000-0000-0000-000000000001",),
            ("00000000-0000-0000-0000-000000000002",),
        ]

        result = rebuild_db._resolve_county_court_ids(mock_conn, "CA", "Santa Clara")

        assert result == [
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        ]
        # Verify the query filters by state AND county, case-insensitive.
        sql, params = mock_cur.execute.call_args[0]
        assert "LOWER(state)" in sql
        assert "LOWER(county)" in sql
        assert params == ("CA", "Santa Clara")

    def test_returns_empty_list_when_no_match(self) -> None:
        """Returns [] when no courts match — caller must raise."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = []

        result = rebuild_db._resolve_county_court_ids(mock_conn, "CA", "Nonexistent")
        assert result == []


class TestResetDerivedTablesForCounty:
    """Tests for reset_derived_tables_for_county()."""

    def test_raises_when_county_not_found(self) -> None:
        """Unknown county should raise ValueError, not silently no-op."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = []

        try:
            rebuild_db.reset_derived_tables_for_county(mock_conn, "CA", "Atlantis")
        except ValueError as exc:
            assert "Atlantis" in str(exc)
        else:
            raise AssertionError("expected ValueError")

        # Must not commit when the lookup fails.
        mock_conn.commit.assert_not_called()

    def test_deletes_per_county_and_returns_counts(self) -> None:
        """Resolves courts, deletes in order, returns row-count summary."""
        counts = {
            "case_judges": 100,
            "case_parties": 200,
            "case_attorneys": 50,
            "documents": 5000,
            "rulings": 400,
            "cases": 600,
            "judges": 25,
        }
        court_ids = ["00000000-0000-0000-0000-000000000001"]
        mock_conn, mock_cur = _build_per_county_mock_conn(court_ids, counts)

        result = rebuild_db.reset_derived_tables_for_county(mock_conn, "CA", "Santa Clara")

        assert result == counts
        mock_conn.commit.assert_called_once()

        # Collect all SQL emitted.
        all_sql = [call[0][0] for call in mock_cur.execute.call_args_list]

        # Must have issued DELETEs for the join tables and court-id tables.
        joined = "\n".join(all_sql)
        for table in (
            "case_judges",
            "case_parties",
            "case_attorneys",
            "documents",
            "rulings",
            "cases",
            "judges",
        ):
            assert f"DELETE FROM derived.{table}" in joined, (
                f"expected DELETE FROM derived.{table} in emitted SQL"
            )

        # Must NOT truncate anything or delete courts/attorneys/parties/aliases.
        assert "TRUNCATE" not in joined
        assert "DELETE FROM derived.courts" not in joined
        assert "DELETE FROM derived.attorneys" not in joined
        assert "DELETE FROM derived.parties" not in joined
        assert "DELETE FROM derived.judge_aliases" not in joined
        assert "DELETE FROM derived.attorney_aliases" not in joined
        assert "DELETE FROM derived.party_aliases" not in joined

    def test_delete_order_rulings_before_cases(self) -> None:
        """Must delete rulings before cases to avoid FK constraint violations.

        rulings.case_id → cases.id is NOT NULL, so cases must be deleted last
        among the court-id-keyed tables.
        """
        court_ids = ["00000000-0000-0000-0000-000000000001"]
        counts = {
            "case_judges": 1,
            "case_parties": 1,
            "case_attorneys": 1,
            "documents": 1,
            "rulings": 1,
            "cases": 1,
            "judges": 1,
        }
        mock_conn, mock_cur = _build_per_county_mock_conn(court_ids, counts)

        rebuild_db.reset_derived_tables_for_county(mock_conn, "CA", "Santa Clara")

        # Index only DELETE statements in order of emission.
        delete_sql = [
            call[0][0]
            for call in mock_cur.execute.call_args_list
            if call[0][0].lstrip().startswith(("DELETE", "\n                DELETE"))
            or "DELETE FROM derived." in call[0][0]
        ]

        # Find positions of the court-id-table DELETEs.
        def _find(sub: str) -> int:
            for i, s in enumerate(delete_sql):
                if f"DELETE FROM derived.{sub}" in s and ("case_id IN" not in s):
                    return i
            return -1

        rulings_idx = _find("rulings")
        cases_idx = _find("cases")
        documents_idx = _find("documents")
        judges_idx = _find("judges")

        assert rulings_idx != -1, "rulings DELETE not emitted"
        assert cases_idx != -1, "cases DELETE not emitted"
        assert documents_idx != -1, "documents DELETE not emitted"
        assert judges_idx != -1, "judges DELETE not emitted"
        # rulings must be deleted before cases (rulings.case_id FK).
        assert rulings_idx < cases_idx
        # documents must be deleted before cases (documents.case_id FK).
        assert documents_idx < cases_idx
        # judges must be deleted after rulings (rulings.judge_id FK).
        assert rulings_idx < judges_idx

    def test_logs_row_counts_before_delete(self) -> None:
        """Must log a COUNT(*) per table before issuing DELETE — operators
        can eyeball the scope.
        """
        counts = {
            "case_judges": 10,
            "case_parties": 20,
            "case_attorneys": 5,
            "documents": 500,
            "rulings": 400,
            "cases": 60,
            "judges": 3,
        }
        court_ids = ["00000000-0000-0000-0000-000000000001"]
        mock_conn, mock_cur = _build_per_county_mock_conn(court_ids, counts)

        mock_logger = MagicMock()
        with patch.object(rebuild_db, "logger", mock_logger):
            rebuild_db.reset_derived_tables_for_county(mock_conn, "CA", "Santa Clara")

        info_calls = mock_logger.info.call_args_list
        pre_delete_logs = [
            call for call in info_calls if call.args and call.args[0] == "Pre-delete row count"
        ]
        # One per derived table we touch (7 total).
        assert len(pre_delete_logs) == 7
        # At least one log records the actual row count.
        seen_tables = {call.kwargs.get("table") for call in pre_delete_logs}
        assert "derived.documents" in seen_tables
        assert "derived.rulings" in seen_tables
        assert "derived.cases" in seen_tables

    def test_uses_court_ids_from_resolved_lookup(self) -> None:
        """Resolved court UUIDs must be passed as parameters to every DELETE /
        COUNT query, not interpolated as SQL.
        """
        court_ids = [
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        ]
        counts = dict.fromkeys(
            (
                "case_judges",
                "case_parties",
                "case_attorneys",
                "documents",
                "rulings",
                "cases",
                "judges",
            ),
            0,
        )
        mock_conn, mock_cur = _build_per_county_mock_conn(court_ids, counts)

        rebuild_db.reset_derived_tables_for_county(mock_conn, "CA", "Santa Clara")

        # Every DELETE / COUNT after the initial resolve should receive
        # court_ids as its parameter list, never string-interpolated.
        # The first call is the resolve SELECT.
        calls_after_resolve = mock_cur.execute.call_args_list[1:]
        for call in calls_after_resolve:
            params = call[0][1] if len(call[0]) > 1 else None
            # Params must carry the court_ids list/tuple.
            assert params is not None, f"missing params for {call[0][0]!r}"
            # The first positional is always the court_ids tuple.
            first = params[0]
            assert list(first) == court_ids, f"expected court_ids={court_ids}, got {first}"


class TestDeleteOpensearchDocsForCounty:
    """Tests for delete_opensearch_docs_for_county() (#2568).

    The global ``tentative_rulings_v1`` index cannot be reset under
    ``--reset --county`` because that would wipe docs for every other
    county.  And upsert-by-ruling_id only refreshes content for ruling_ids
    that survive the rebuild — rulings dropped during rebuild remain as
    orphans.  This helper issues a delete-by-query filtered on the target
    county before the DB reset so the indexer rebuilds the county from a
    clean slate.
    """

    def test_issues_delete_by_query_filtered_on_county(self) -> None:
        """Calls _delete_by_query on tentative_rulings_v1 filtered on county."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete_by_query.return_value = {"deleted": 42}

        with patch("opensearchpy.OpenSearch", return_value=mock_client):
            result = rebuild_db.delete_opensearch_docs_for_county("http://os", "Contra Costa")

        assert result == 42
        mock_client.delete_by_query.assert_called_once()
        kwargs = mock_client.delete_by_query.call_args.kwargs
        assert kwargs["index"] == "tentative_rulings_v1"
        assert kwargs["body"] == {"query": {"term": {"county": "Contra Costa"}}}
        assert kwargs["refresh"] is True
        assert kwargs["conflicts"] == "proceed"

    def test_no_op_when_index_missing(self) -> None:
        """Returns 0 without calling delete_by_query if the index is missing."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = False

        with patch("opensearchpy.OpenSearch", return_value=mock_client):
            result = rebuild_db.delete_opensearch_docs_for_county("http://os", "Contra Costa")

        assert result == 0
        mock_client.delete_by_query.assert_not_called()

    def test_uses_basic_auth_when_credentials_set(self) -> None:
        """OPENSEARCH_USERNAME/PASSWORD env vars feed http_auth tuple."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete_by_query.return_value = {"deleted": 0}

        with (
            patch("opensearchpy.OpenSearch", return_value=mock_client) as mock_ctor,
            patch.dict(
                os.environ,
                {
                    "OPENSEARCH_USERNAME": "admin",
                    "OPENSEARCH_PASSWORD": "s3cret",
                },
                clear=False,
            ),
        ):
            rebuild_db.delete_opensearch_docs_for_county("http://os", "Ventura")

        kwargs = mock_ctor.call_args.kwargs
        assert kwargs["hosts"] == ["http://os"]
        assert kwargs["timeout"] == 30
        assert kwargs["max_retries"] == 3
        assert kwargs["retry_on_timeout"] is True
        assert kwargs["http_auth"] == ("admin", "s3cret")

    def test_no_auth_when_credentials_unset(self) -> None:
        """Missing env vars → http_auth not passed to the client ctor."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete_by_query.return_value = {"deleted": 0}

        env = {k: v for k, v in os.environ.items()}
        env.pop("OPENSEARCH_USERNAME", None)
        env.pop("OPENSEARCH_PASSWORD", None)

        with (
            patch("opensearchpy.OpenSearch", return_value=mock_client) as mock_ctor,
            patch.dict(os.environ, env, clear=True),
        ):
            rebuild_db.delete_opensearch_docs_for_county("http://os", "Ventura")

        kwargs = mock_ctor.call_args.kwargs
        assert "http_auth" not in kwargs


class TestMainPerCountyResetDispatch:
    """Tests for main()'s dispatch between global and per-county reset."""

    def _common_patches(
        self,
        tmp_path: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        """Set up common patches used by both dispatch tests."""
        cache_dir = str(tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        # Create one dummy key so list_local_keys returns a non-empty list.
        key_dir = tmp_path / "cache" / "ca" / "santa_clara" / "superior_court" / "raw"
        key_dir.mkdir(parents=True)
        (key_dir / "abc123.html").write_bytes(b"<html>x</html>")

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
        }
        mock_pool.submit.return_value = mock_future

        return {
            "cache_dir": cache_dir,
            "argv": argv,
            "mock_pool": mock_pool,
            "mock_future": mock_future,
        }

    def test_reset_without_county_truncates_globally(self, tmp_path: Any) -> None:
        """``--reset`` without ``--county`` → global TRUNCATE."""
        ctx = self._common_patches(tmp_path, ["rebuild_db.py", "--reset"])
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/santa_clara/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as mock_global_reset,
            patch("rebuild_db.reset_derived_tables_for_county") as mock_per_county_reset,
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=ctx["mock_pool"],
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([ctx["mock_future"]]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": ctx["cache_dir"],
                },
                clear=False,
            ),
            patch("sys.argv", ctx["argv"]),
        ):
            rebuild_db.main()

        mock_global_reset.assert_called_once()
        mock_per_county_reset.assert_not_called()

    def test_reset_with_county_uses_per_county_reset(self, tmp_path: Any) -> None:
        """``--reset --county X`` → per-county DELETE, not global TRUNCATE.

        Also verifies OpenSearch index reset is skipped (global index
        self-heals, we don't rebuild non-target counties).
        """
        ctx = self._common_patches(
            tmp_path, ["rebuild_db.py", "--reset", "--county", "Santa Clara"]
        )
        parent = MagicMock()
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/santa_clara/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as mock_global_reset,
            patch("rebuild_db.reset_derived_tables_for_county") as mock_per_county_reset,
            patch("rebuild_db.reset_opensearch_index") as mock_os_reset,
            patch("rebuild_db.delete_opensearch_docs_for_county") as mock_os_delete_county,
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=ctx["mock_pool"],
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([ctx["mock_future"]]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": ctx["cache_dir"],
                    "OPENSEARCH_URL": "http://opensearch:9200",
                },
                clear=False,
            ),
            patch("sys.argv", ctx["argv"]),
        ):
            parent.attach_mock(mock_os_delete_county, "os_delete_county")
            parent.attach_mock(mock_per_county_reset, "per_county_reset")
            rebuild_db.main()

        mock_global_reset.assert_not_called()
        mock_per_county_reset.assert_called_once()
        # Args: (conn, state, county).  State defaults to "ca".
        call_args = mock_per_county_reset.call_args
        assert call_args[0][1] == "ca"
        assert call_args[0][2] == "Santa Clara"
        # Global OpenSearch index reset must not be called under --county mode.
        mock_os_reset.assert_not_called()
        # Per-county OS delete-by-query runs once with (os_url, county).
        mock_os_delete_county.assert_called_once_with("http://opensearch:9200", "Santa Clara")
        # OS delete must run BEFORE the DB reset — a delete-by-query failure
        # should abort the rebuild rather than leave the DB reset and OS
        # half-stale.
        method_names = [name for name, _, _ in parent.mock_calls]
        os_idx = method_names.index("os_delete_county")
        db_idx = method_names.index("per_county_reset")
        assert os_idx < db_idx

    def test_per_county_reset_skips_os_delete_when_url_unset(self, tmp_path: Any) -> None:
        """Without ``OPENSEARCH_URL``, per-county DB reset still runs and the
        OS delete is skipped entirely.
        """
        ctx = self._common_patches(
            tmp_path, ["rebuild_db.py", "--reset", "--county", "Santa Clara"]
        )
        env = {k: v for k, v in os.environ.items()}
        env.pop("OPENSEARCH_URL", None)
        env["DATABASE_URL"] = "postgres://test"
        env["S3_CACHE_DIR"] = ctx["cache_dir"]

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/santa_clara/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as mock_global_reset,
            patch("rebuild_db.reset_derived_tables_for_county") as mock_per_county_reset,
            patch("rebuild_db.delete_opensearch_docs_for_county") as mock_os_delete_county,
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=ctx["mock_pool"],
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([ctx["mock_future"]]),
            ),
            patch.dict(os.environ, env, clear=True),
            patch("sys.argv", ctx["argv"]),
        ):
            rebuild_db.main()

        mock_global_reset.assert_not_called()
        mock_per_county_reset.assert_called_once()
        mock_os_delete_county.assert_not_called()

    def test_no_reset_does_not_touch_derived_tables(self, tmp_path: Any) -> None:
        """Without ``--reset``, neither reset function is called — existing
        incremental behavior preserved.
        """
        ctx = self._common_patches(tmp_path, ["rebuild_db.py", "--county", "Santa Clara"])
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/santa_clara/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as mock_global_reset,
            patch("rebuild_db.reset_derived_tables_for_county") as mock_per_county_reset,
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker") as mock_marker,
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=ctx["mock_pool"],
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([ctx["mock_future"]]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": ctx["cache_dir"],
                },
                clear=False,
            ),
            patch("sys.argv", ctx["argv"]),
        ):
            rebuild_db.main()

        mock_global_reset.assert_not_called()
        mock_per_county_reset.assert_not_called()
        # No marker writes either — that's reserved for --reset runs.
        mock_marker.assert_not_called()


# ---------------------------------------------------------------------------
# Split-child re-derivation on rebuild (#2494)
# ---------------------------------------------------------------------------


class TestRebuildMultiCasePdfProducesSplitChildren:
    """Regression tests for #2494 — rebuild must re-derive split-children.

    Before #2494, ``_process_one_document`` returned ``status="error"`` on any
    byte-level hash mismatch, which skipped 158/260 Santa Clara raws and lost
    every split-child on rebuild.  The fix:

    * Hash mismatch logs a warning instead of returning an error.
    * The event passed to ``worker.process_event`` carries the S3 key hash as
      ``content_hash``.  The worker's ``_llm_split_document`` path uses that
      value as the seed for per-child hashes (``sha256(f"{content_hash}:
      ruling:{idx}".encode())``), so all N children are reproducible.
    * The ``--force-split-child-loss`` flag is a deprecated no-op.

    These tests don't run the real LLM split — that's an integration test and
    is exercised in the ingestion worker test suite.  Here we verify the
    rebuild-side contract: (a) mismatched raws aren't rejected, and (b) the
    worker receives an event whose ``content_hash`` matches the S3 key hash,
    so the existing LLM split path can fan out correctly.
    """

    def _fixture_pdf(self) -> bytes:
        """Load a real Santa Clara multi-case PDF fixture."""
        fixture_path = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "sc_dept16_wed.pdf",
        )
        with open(fixture_path, "rb") as f:
            return f.read()

    def test_multi_case_pdf_byte_mismatch_proceeds_to_worker(self, tmp_path: Any) -> None:
        """A raw PDF whose bytes do not hash to its S3 key hash must still
        reach the worker, carrying the key hash as canonical content_hash.

        This is the core #2494 regression: the previous code rejected such
        raws and prevented the worker from re-deriving split-children.
        """
        pdf_bytes = self._fixture_pdf()
        # Intentionally use a synthetic "wrong" hash in the key to mimic the
        # observed SC production condition (key hash != sha256(bytes)).
        key_hash = "deadbeef" * 8  # 64 hex chars — valid content-hash shape
        key = f"ca/santa_clara/superior_court/raw/{key_hash}.pdf"
        cache_dir = str(tmp_path)
        raw_dir = tmp_path / "ca" / "santa_clara" / "superior_court" / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / f"{key_hash}.pdf").write_bytes(pdf_bytes)

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        if hasattr(rebuild_db._process_one_document, "_worker"):
            delattr(rebuild_db._process_one_document, "_worker")

        with (
            patch(
                "ingestion.worker.IngestionWorker",
                return_value=mock_worker,
            ),
            patch(
                "redis.Redis.from_url",
                return_value=MagicMock(),
            ),
            patch(
                "framework.s3_cache.make_s3_client",
                return_value=MagicMock(),
            ),
        ):
            result = rebuild_db._process_one_document(
                key,
                cache_dir,
                "test-bucket",
                "postgres://test",
                "redis://test",
                "",
            )

        # The raw PDF did not fail the rebuild.  It reached the worker.
        assert result["status"] == "ok"
        assert result["content_format"] == "pdf"
        assert result["hash_mismatch"] is True
        mock_worker.process_event.assert_called_once()
        event = mock_worker.process_event.call_args[0][0]
        # Canonical content_hash is the key hash — the worker's LLM split
        # path uses this as the seed for per-child hashes, so all N children
        # are reproducible from the same raw PDF.
        assert event["content_hash"] == key_hash
        assert event["s3_key"] == key
        assert event["content_format"] == "pdf"

    def test_multi_case_pdf_matching_hash_also_proceeds(self, tmp_path: Any) -> None:
        """Happy path: when the byte hash matches the key hash, rebuild
        still reaches the worker with ``content_hash`` set to that hash.
        ``hash_mismatch`` is False.
        """
        import hashlib as _h

        pdf_bytes = self._fixture_pdf()
        key_hash = _h.sha256(pdf_bytes).hexdigest()
        key = f"ca/santa_clara/superior_court/raw/{key_hash}.pdf"
        cache_dir = str(tmp_path)
        raw_dir = tmp_path / "ca" / "santa_clara" / "superior_court" / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / f"{key_hash}.pdf").write_bytes(pdf_bytes)

        mock_worker = MagicMock()
        if hasattr(rebuild_db._process_one_document, "_worker"):
            delattr(rebuild_db._process_one_document, "_worker")

        with (
            patch(
                "ingestion.worker.IngestionWorker",
                return_value=mock_worker,
            ),
            patch(
                "redis.Redis.from_url",
                return_value=MagicMock(),
            ),
            patch(
                "framework.s3_cache.make_s3_client",
                return_value=MagicMock(),
            ),
        ):
            result = rebuild_db._process_one_document(
                key,
                cache_dir,
                "test-bucket",
                "postgres://test",
                "redis://test",
                "",
            )

        assert result["status"] == "ok"
        assert result["hash_mismatch"] is False
        mock_worker.process_event.assert_called_once()
        event = mock_worker.process_event.call_args[0][0]
        assert event["content_hash"] == key_hash


class TestForceSplitChildLossDeprecated:
    """As of #2494, ``--force-split-child-loss`` is a no-op.  It is kept for
    CLI/tooling compatibility but logs a deprecation warning.
    """

    def test_flag_still_accepted_and_logs_deprecation(self, tmp_path: Any) -> None:
        """Passing ``--force-split-child-loss`` must not error out, but must
        log a deprecation warning.  Rebuild still proceeds normally.
        """
        cache_dir = str(tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        key_dir = tmp_path / "cache" / "ca" / "santa_clara" / "superior_court" / "raw"
        key_dir.mkdir(parents=True)
        (key_dir / "abc123.html").write_bytes(b"<html>x</html>")

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }
        mock_pool.submit.return_value = mock_future

        mock_logger = MagicMock()
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/santa_clara/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables_for_county"),
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=mock_pool,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([mock_future]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
                clear=False,
            ),
            patch(
                "sys.argv",
                [
                    "rebuild_db.py",
                    "--reset",
                    "--county",
                    "Santa Clara",
                    "--force-split-child-loss",
                ],
            ),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            # Must not raise.
            rebuild_db.main()

        warn_calls = mock_logger.warning.call_args_list
        assert any("--force-split-child-loss is deprecated" in str(call) for call in warn_calls), (
            f"expected deprecation warning in {warn_calls!r}"
        )


class TestRebuildSummaryHashMismatchCounter:
    """Main() must report the hash_mismatch_warnings count in its summary.

    Ops look at the summary to decide whether an S3 integrity issue is
    actually affecting data.  Burying it in per-doc warnings only makes that
    diagnosis harder.
    """

    def test_summary_reports_hash_mismatch_count(self, tmp_path: Any) -> None:
        """A rebuild run where some docs had hash_mismatch=True should log
        an aggregate warning naming that count.
        """
        cache_dir = str(tmp_path / "cache")
        html_dir = tmp_path / "cache" / "ca" / "orange" / "superior_court" / "raw"
        html_dir.mkdir(parents=True)
        (html_dir / "abc123.html").write_bytes(b"<html>x</html>")
        pdf_dir = tmp_path / "cache" / "ca" / "santa_clara" / "superior_court" / "raw"
        pdf_dir.mkdir(parents=True)
        (pdf_dir / "def456.pdf").write_bytes(b"%PDF")

        # Two processed documents, one with a hash mismatch.
        mock_future_html = MagicMock()
        mock_future_html.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": True,
            "hash_mismatch": False,
        }
        mock_future_pdf = MagicMock()
        mock_future_pdf.result.return_value = {
            "status": "ok",
            "content_format": "pdf",
            "had_hearing_date": False,
            "hash_mismatch": True,
        }

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.side_effect = [mock_future_html, mock_future_pdf]

        mock_logger = MagicMock()
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=[
                    "ca/orange/superior_court/raw/abc123.html",
                    "ca/santa_clara/superior_court/raw/def456.pdf",
                ],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=mock_pool,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([mock_future_html, mock_future_pdf]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
            ),
            patch("sys.argv", ["rebuild_db.py"]),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            rebuild_db.main()

        warn_calls = mock_logger.warning.call_args_list
        mismatch_warnings = [
            call for call in warn_calls if call.kwargs.get("hash_mismatch_warnings") is not None
        ]
        assert len(mismatch_warnings) > 0, (
            f"Expected aggregate hash_mismatch_warnings warning. Got warning calls: {warn_calls}"
        )
        assert mismatch_warnings[0].kwargs["hash_mismatch_warnings"] == 1
        # Rebuild-complete info log should also carry the count.
        info_calls = mock_logger.info.call_args_list
        rebuild_complete = [
            call for call in info_calls if call.args and call.args[0] == "Rebuild complete"
        ]
        assert len(rebuild_complete) == 1
        assert rebuild_complete[0].kwargs.get("hash_mismatch_warnings") == 1


# ---------------------------------------------------------------------------
# Worker-crash resilience — #2495
# ---------------------------------------------------------------------------


class TestAutoscaleConcurrency:
    """Tests for _autoscale_concurrency()."""

    def test_disabled_when_max_memory_zero(self) -> None:
        """max_worker_memory_mb=0 returns requested concurrency unchanged."""
        decision = rebuild_db._autoscale_concurrency(
            requested=64, max_worker_memory_mb=0, available_memory_mb=8192, source="cgroup_v2"
        )
        assert decision.effective == 64
        assert decision.reason == "autoscaling_disabled"

    def test_disabled_when_max_memory_negative(self) -> None:
        """Negative values also disable autoscaling."""
        decision = rebuild_db._autoscale_concurrency(
            requested=64, max_worker_memory_mb=-1, available_memory_mb=8192, source="cgroup_v2"
        )
        assert decision.effective == 64
        assert decision.reason == "autoscaling_disabled"

    def test_caps_when_memory_tight(self) -> None:
        """With 8 GB RAM and 1 GB per worker, 64 requested → 8 effective."""
        decision = rebuild_db._autoscale_concurrency(
            requested=64, max_worker_memory_mb=1024, available_memory_mb=8192, source="cgroup_v2"
        )
        assert decision.effective == 8
        assert decision.reason == "memory_budget"

    def test_returns_requested_when_memory_plentiful(self) -> None:
        """With more than enough RAM, requested concurrency wins."""
        decision = rebuild_db._autoscale_concurrency(
            requested=4, max_worker_memory_mb=1024, available_memory_mb=32768, source="cgroup_v2"
        )
        assert decision.effective == 4
        assert decision.reason == "memory_budget"

    def test_floor_of_one(self) -> None:
        """Even with tiny RAM, at least one worker must run (no-zero floor)."""
        decision = rebuild_db._autoscale_concurrency(
            requested=64, max_worker_memory_mb=2048, available_memory_mb=512, source="cgroup_v2"
        )
        assert decision.effective == 1
        assert decision.reason == "memory_budget"

    def test_falls_back_to_requested_when_memory_unknown(self) -> None:
        """available_memory_mb=0 with cgroup_v2 source — memory_unknown path."""
        decision = rebuild_db._autoscale_concurrency(
            requested=32, max_worker_memory_mb=1024, available_memory_mb=0, source="cgroup_v2"
        )
        assert decision.effective == 32
        assert decision.reason == "memory_unknown"

    def test_fargate_host_memory_fallback_clamps_to_4(self) -> None:
        """source="psutil_host" with 64 GB host memory → fargate_fallback caps at 4."""
        decision = rebuild_db._autoscale_concurrency(
            requested=64, max_worker_memory_mb=1024, available_memory_mb=65536, source="psutil_host"
        )
        assert decision.effective == 4
        assert decision.reason == "fargate_fallback"

    def test_unresolved_memory_falls_back_to_4(self) -> None:
        """source="unresolved" always triggers fargate_fallback cap at min(requested, 4)."""
        decision = rebuild_db._autoscale_concurrency(
            requested=64, max_worker_memory_mb=1024, available_memory_mb=0, source="unresolved"
        )
        assert decision.effective == 4
        assert decision.reason == "fargate_fallback"

    def test_cgroup_v2_small_task_clamps_normally(self) -> None:
        """source="cgroup_v2", 4 GB available, 1 GB per worker, 64 requested → 4 effective."""
        decision = rebuild_db._autoscale_concurrency(
            requested=64, max_worker_memory_mb=1024, available_memory_mb=4096, source="cgroup_v2"
        )
        assert decision.effective == 4
        assert decision.reason == "memory_budget"

    def test_cgroup_v2_large_task_respects_requested(self) -> None:
        """source="cgroup_v2", 32 GB available, 1 GB per worker, 4 requested → 4 effective."""
        decision = rebuild_db._autoscale_concurrency(
            requested=4, max_worker_memory_mb=1024, available_memory_mb=32768, source="cgroup_v2"
        )
        assert decision.effective == 4
        assert decision.reason == "memory_budget"

    def test_ecs_metadata_source_uses_memory_budget(self) -> None:
        """source="ecs_metadata", 16 GB container, 1 GB per worker, 64 requested → 16 effective.

        Confirms that "ecs_metadata" is treated as a trusted source (not fargate_fallback).
        Satisfies AC#2.
        """
        decision = rebuild_db._autoscale_concurrency(
            requested=64,
            max_worker_memory_mb=1024,
            available_memory_mb=16384,
            source="ecs_metadata",
        )
        assert decision.effective == 16
        assert decision.reason == "memory_budget"


class TestAvailableMemoryMb:
    """Tests for _available_memory_mb().

    The helper reads cgroup memory limits (Fargate) before falling back to
    ``psutil.virtual_memory()``.  We test the contract — a non-negative
    integer — rather than mocking the filesystem, because the cgroup path
    layout varies between cgroup v1 and v2 and across kernels.
    """

    def test_returns_non_negative_int(self) -> None:
        value, source = rebuild_db._available_memory_mb()
        assert isinstance(value, int)
        assert value >= 0

    def test_returns_source_label(self) -> None:
        """_available_memory_mb() returns a (int, str) tuple with a known source label."""
        result = rebuild_db._available_memory_mb()
        assert isinstance(result, tuple)
        assert len(result) == 2
        value, source = result
        assert isinstance(value, int)
        assert value >= 0
        assert source in {"cgroup_v2", "cgroup_v1", "ecs_metadata", "psutil_host", "unresolved"}

    def test_cgroup_v2_reads_known_value(self) -> None:
        """Stub /sys/fs/cgroup/memory.max to return a known value; assert correct MiB conversion.

        16 GiB == 17179869184 bytes == 16384 MiB.  Satisfies AC#4.
        """
        with patch("builtins.open", mock_open(read_data="17179869184\n")):
            value, source = rebuild_db._available_memory_mb()
        assert (value, source) == (16384, "cgroup_v2")

    def test_ecs_metadata_returns_container_limit(self) -> None:
        """ECS container metadata endpoint returns the container's memory limit in MiB.

        Stubs builtins.open to raise OSError (simulating cgroup paths unavailable),
        then patches ECS_CONTAINER_METADATA_URI_V4 and urllib.request.urlopen to return
        a fake response with Limits.Memory == 16384.  Asserts (16384, "ecs_metadata").
        Satisfies AC#1.
        """
        import json

        fake_payload = json.dumps({"Limits": {"Memory": 16384}}).encode()
        fake_response = MagicMock()
        fake_response.read.return_value = fake_payload
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("builtins.open", side_effect=OSError("no cgroup")),
            patch.dict(
                os.environ,
                {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/abc"},
                clear=False,
            ),
            patch("urllib.request.urlopen", return_value=fake_response),
        ):
            value, source = rebuild_db._available_memory_mb()
        assert (value, source) == (16384, "ecs_metadata")


class TestAutoscaleLogging:
    """Integration tests: autoscale decision logging in main().

    Verify that main() always emits an INFO "Autoscaled concurrency to fit memory budget" log
    with the four required fields, and that a WARNING fires when the Fargate fallback
    path is triggered.
    """

    def _run_main_with_memory(
        self,
        tmp_path: Any,
        memory_return_value: tuple,
        concurrency: str = "64",
        max_worker_memory_mb: str = "1024",
    ) -> MagicMock:
        """Run main() with a patched _available_memory_mb and return the mock logger."""
        keys = ["ca/orange/superior_court/raw/abc123.html"]
        cache_dir = str(tmp_path / "cache")
        full = tmp_path / "cache" / keys[0]
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"<html>x</html>")

        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        def _pool_factory(*args: Any, **kwargs: Any) -> MagicMock:
            return mock_pool

        mock_logger = MagicMock()

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch("rebuild_db.list_local_keys", return_value=keys),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._available_memory_mb", return_value=memory_return_value),
            patch("concurrent.futures.ProcessPoolExecutor", side_effect=_pool_factory),
            patch("concurrent.futures.as_completed", return_value=iter([mock_future])),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
                clear=False,
            ),
            patch(
                "sys.argv",
                [
                    "rebuild_db.py",
                    "--concurrency",
                    concurrency,
                    "--max-worker-memory-mb",
                    max_worker_memory_mb,
                ],
            ),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            rebuild_db.main()

        return mock_logger

    def test_info_log_carries_required_fields(self, tmp_path: Any) -> None:
        """INFO "Autoscaled concurrency to fit memory budget" must include the four required fields.

        These fields are consumed by log-based alerting and the ops dashboard.
        """
        mock_logger = self._run_main_with_memory(
            tmp_path, memory_return_value=(4096, "cgroup_v2"), concurrency="64"
        )

        info_calls = mock_logger.info.call_args_list
        autoscale_calls = [
            call
            for call in info_calls
            if call.args and call.args[0] == "Autoscaled concurrency to fit memory budget"
        ]
        assert len(autoscale_calls) == 1, (
            "Expected exactly 1 'Autoscaled concurrency to fit memory budget' INFO log; "
            f"got {autoscale_calls!r}"
        )
        kwargs = autoscale_calls[0].kwargs
        assert "available_memory_mb" in kwargs, f"Missing available_memory_mb in {kwargs!r}"
        assert "max_worker_memory_mb" in kwargs, f"Missing max_worker_memory_mb in {kwargs!r}"
        assert "requested_concurrency" in kwargs, f"Missing requested_concurrency in {kwargs!r}"
        assert "effective" in kwargs, f"Missing effective in {kwargs!r}"

    def test_fargate_fallback_emits_warning(self, tmp_path: Any) -> None:
        """When cgroup detection fails (psutil_host + large value), a WARNING fires.

        Simulates a Fargate task where cgroup memory.max is unreadable so
        _available_memory_mb() falls back to psutil and returns the 64 GB host total.
        """
        # 65536 MiB = 64 GB host memory — well above the Fargate container limit.
        mock_logger = self._run_main_with_memory(
            tmp_path, memory_return_value=(65536, "psutil_host"), concurrency="64"
        )

        # WARNING must fire
        warning_calls = mock_logger.warning.call_args_list
        fallback_warnings = [
            call
            for call in warning_calls
            if call.args and "cgroup limit unreadable" in call.args[0]
        ]
        assert len(fallback_warnings) >= 1, (
            f"Expected Fargate-fallback WARNING; got warning calls: {warning_calls!r}"
        )

        # The INFO log must also carry the four required fields
        info_calls = mock_logger.info.call_args_list
        autoscale_calls = [
            call
            for call in info_calls
            if call.args and call.args[0] == "Autoscaled concurrency to fit memory budget"
        ]
        assert len(autoscale_calls) == 1
        kwargs = autoscale_calls[0].kwargs
        assert "available_memory_mb" in kwargs
        assert "max_worker_memory_mb" in kwargs
        assert "requested_concurrency" in kwargs
        assert "effective" in kwargs
        # Effective concurrency must be capped at 4
        assert kwargs["effective"] == 4


class TestRetryCrashedKeysSerially:
    """Tests for _retry_crashed_keys_serially()."""

    def test_empty_keys_returns_empty_summary(self) -> None:
        """No crashed keys → no work, empty summary."""
        summary = rebuild_db._retry_crashed_keys_serially(
            [], "", "bucket", "postgres://x", "redis://x", ""
        )
        assert summary["processed"] == 0
        assert summary["errors"] == 0
        assert summary["skipped"] == 0
        assert summary["still_crashed"] == []

    def test_successful_retry_recovers_key(self, tmp_path: Any) -> None:
        """When the retry worker processes the key without crashing, the
        summary records it as processed and still_crashed stays empty.

        Uses a real single-worker ``ProcessPoolExecutor`` via the function
        under test; the child process uses the same ``_process_one_document``
        code path and returns via a mocked ``IngestionWorker``.
        """
        content = b"<html>Date: 03/15/2026 ruling text</html>"
        import hashlib

        content_hash = hashlib.sha256(content).hexdigest()
        key = f"ca/orange/superior_court/raw/{content_hash}.html"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / f"{content_hash}.html").write_bytes(content)

        # Stub the pool so we don't fork — the retry helper uses
        # ProcessPoolExecutor(max_workers=1) as a context manager.
        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": True,
            "hash_mismatch": False,
        }
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        with patch("concurrent.futures.ProcessPoolExecutor", return_value=mock_pool):
            summary = rebuild_db._retry_crashed_keys_serially(
                [key],
                cache_dir,
                "test-bucket",
                "postgres://test",
                "redis://test",
                "",
            )

        assert summary["processed"] == 1
        assert summary["errors"] == 0
        assert summary["still_crashed"] == []
        assert summary["format_counts"] == {"html": 1}

    def test_retry_that_also_crashes_appends_to_still_crashed(self, tmp_path: Any) -> None:
        """When the single-worker retry also raises BrokenProcessPool, the
        key lands in ``still_crashed`` and ``errors`` increments."""
        from concurrent.futures.process import BrokenProcessPool

        key = "ca/santa_clara/superior_court/raw/deadbeef.pdf"

        mock_future = MagicMock()
        mock_future.result.side_effect = BrokenProcessPool("retry segfault")
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        with patch("concurrent.futures.ProcessPoolExecutor", return_value=mock_pool):
            summary = rebuild_db._retry_crashed_keys_serially(
                [key],
                "",
                "test-bucket",
                "postgres://test",
                "redis://test",
                "",
            )

        assert summary["processed"] == 0
        assert summary["errors"] == 1
        assert summary["still_crashed"] == [key]


class TestMainPoolBreakHandling:
    """Integration tests for main()'s BrokenProcessPool handling.

    Before #2495, a worker crash triggered ``BrokenProcessPool`` on every
    in-flight future, and the orchestrator's generic ``except Exception``
    logged a single opaque "Failed to process" line per victim without
    distinguishing "this PDF ran in the dead worker" from "some other PDF
    ran in the dead worker."  The fix:

    * The loop catches ``BrokenProcessPool`` specifically and logs
      ``in-flight at pool break`` with the specific S3 key.
    * After the concurrent pass, affected keys are retried serially in
      fresh single-worker pools.
    * The rebuild summary carries ``pool_break_events``,
      ``pool_break_keys_recovered``, and ``pool_break_keys_unrecovered``.
    """

    def _make_cache(self, tmp_path: Any, keys: list[str]) -> str:
        """Create a local cache with stub content for each key."""
        cache_dir = str(tmp_path / "cache")
        for key in keys:
            parsed = rebuild_db.parse_s3_key(key)
            assert parsed is not None
            full = tmp_path / "cache" / key
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"<html>x</html>")
        return cache_dir

    def test_pool_break_logs_in_flight_key(self, tmp_path: Any) -> None:
        """A ``BrokenProcessPool`` raised by ``future.result()`` must produce
        an ``in-flight at pool break`` log carrying the specific S3 key of
        the future, so operators can identify candidate culprit PDFs."""
        from concurrent.futures.process import BrokenProcessPool

        keys = [
            "ca/santa_clara/superior_court/raw/aaa111.html",
            "ca/santa_clara/superior_court/raw/bbb222.html",
        ]
        cache_dir = self._make_cache(tmp_path, keys)

        # Future A: normal success.  Future B: raises BrokenProcessPool.
        future_a = MagicMock()
        future_a.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }
        future_b = MagicMock()
        future_b.result.side_effect = BrokenProcessPool("pool died")

        # Main-pass pool.  Its submit() is called for all input keys.
        # We have to make the mapping deterministic so we know which key
        # goes to which future.
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.side_effect = [future_a, future_b]

        # Retry pool — the retry path submits its own ProcessPoolExecutor.
        # Give it a future that re-raises so still_crashed is populated.
        retry_future = MagicMock()
        retry_future.result.side_effect = BrokenProcessPool("retry also died")
        retry_pool = MagicMock()
        retry_pool.__enter__ = MagicMock(return_value=retry_pool)
        retry_pool.__exit__ = MagicMock(return_value=False)
        retry_pool.submit.return_value = retry_future

        # The main loop and the retry helper both instantiate
        # ProcessPoolExecutor — patch so the main call gets the main pool
        # and subsequent calls get the retry pool.
        pool_sequence = [mock_pool, retry_pool]

        def _pool_factory(*args: Any, **kwargs: Any) -> MagicMock:
            return pool_sequence.pop(0) if pool_sequence else retry_pool

        mock_logger = MagicMock()

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=keys,
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                side_effect=_pool_factory,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([future_a, future_b]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                    # Disable retry-cap gates (#2572) so this test exercises
                    # the retry path even when crashed/total > 10%.  Without
                    # this, main() aborts with sys.exit(2) before the
                    # pool-break summary is logged.  See #2567.
                    "REBUILD_MAX_RETRY_COUNT": "0",
                    "REBUILD_MAX_RETRY_RATIO": "0",
                },
            ),
            # Disable the retry-abort gate so the pool-break test can
            # exercise its serial-retry path.  The 100% crash ratio in this
            # scenario would otherwise trip the #2572 abort and sys.exit(2)
            # before the rebuild summary is logged.
            patch(
                "sys.argv",
                [
                    "rebuild_db.py",
                    "--max-retry-count",
                    "0",
                    "--max-retry-ratio",
                    "0",
                ],
            ),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            rebuild_db.main()

        # Verify the in-flight log fired with the specific key.
        err_calls = mock_logger.error.call_args_list
        in_flight_logs = [
            call for call in err_calls if call.args and call.args[0] == "in-flight at pool break"
        ]
        assert len(in_flight_logs) == 1, f"expected 1 in-flight log, got: {err_calls}"
        # The key in the log must be one of the inputs, not an opaque
        # "Failed to process" message.
        assert in_flight_logs[0].kwargs.get("s3_key") in keys

        # Verify the rebuild summary reports pool-break stats.
        info_calls = mock_logger.info.call_args_list
        rebuild_complete = [
            call for call in info_calls if call.args and call.args[0] == "Rebuild complete"
        ]
        assert len(rebuild_complete) == 1
        kwargs = rebuild_complete[0].kwargs
        assert kwargs.get("pool_break_events") == 1
        # The retry also crashed, so unrecovered = 1, recovered = 0.
        assert kwargs.get("pool_break_keys_recovered") == 0
        assert kwargs.get("pool_break_keys_unrecovered") == 1

    def test_pool_break_retry_recovers_key(self, tmp_path: Any) -> None:
        """When a worker crashes but a serial retry succeeds, the key moves
        from errors → processed and the summary reports recovery."""
        from concurrent.futures.process import BrokenProcessPool

        keys = ["ca/santa_clara/superior_court/raw/aaa111.html"]
        cache_dir = self._make_cache(tmp_path, keys)

        crash_future = MagicMock()
        crash_future.result.side_effect = BrokenProcessPool("initial crash")

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = crash_future

        recover_future = MagicMock()
        recover_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }
        retry_pool = MagicMock()
        retry_pool.__enter__ = MagicMock(return_value=retry_pool)
        retry_pool.__exit__ = MagicMock(return_value=False)
        retry_pool.submit.return_value = recover_future

        pool_sequence = [mock_pool, retry_pool]

        def _pool_factory(*args: Any, **kwargs: Any) -> MagicMock:
            return pool_sequence.pop(0) if pool_sequence else retry_pool

        mock_logger = MagicMock()

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=keys,
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                side_effect=_pool_factory,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([crash_future]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                    # Disable retry-cap gates (#2572) so this test exercises
                    # the retry path even when crashed/total > 10%.  Without
                    # this, main() aborts with sys.exit(2) before the
                    # pool-break summary is logged.  See #2567.
                    "REBUILD_MAX_RETRY_COUNT": "0",
                    "REBUILD_MAX_RETRY_RATIO": "0",
                },
            ),
            # Disable the retry-abort gate so the serial-retry recovery
            # path runs.  The 100% crash ratio in this scenario would
            # otherwise trip the #2572 abort and sys.exit(2) before the
            # rebuild summary is logged.
            patch(
                "sys.argv",
                [
                    "rebuild_db.py",
                    "--max-retry-count",
                    "0",
                    "--max-retry-ratio",
                    "0",
                ],
            ),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            rebuild_db.main()

        info_calls = mock_logger.info.call_args_list
        rebuild_complete = [
            call for call in info_calls if call.args and call.args[0] == "Rebuild complete"
        ]
        assert len(rebuild_complete) == 1
        kwargs = rebuild_complete[0].kwargs
        assert kwargs.get("pool_break_events") == 1
        assert kwargs.get("pool_break_keys_recovered") == 1
        assert kwargs.get("pool_break_keys_unrecovered") == 0
        # Overall: 1 processed via retry, 0 errors.
        assert kwargs.get("processed") == 1
        assert kwargs.get("errors") == 0


class TestMaxWorkerMemoryCLI:
    """CLI integration: ``--max-worker-memory-mb`` caps concurrency."""

    def test_flag_caps_concurrency_before_pool_start(self, tmp_path: Any) -> None:
        """``--max-worker-memory-mb 1024`` with 2 GB available must result
        in ``max_workers=2`` on the pool (2048/1024), even if --concurrency
        asks for 64."""
        keys = ["ca/orange/superior_court/raw/abc123.html"]
        cache_dir = str(tmp_path / "cache")
        full = tmp_path / "cache" / keys[0]
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"<html>x</html>")

        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        captured_max_workers: list[int] = []

        def _pool_factory(*args: Any, **kwargs: Any) -> MagicMock:
            captured_max_workers.append(kwargs.get("max_workers", -1))
            return mock_pool

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=keys,
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch(
                "rebuild_db._available_memory_mb",
                return_value=(2048, "cgroup_v2"),
            ),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                side_effect=_pool_factory,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([mock_future]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
                clear=False,
            ),
            patch(
                "sys.argv",
                [
                    "rebuild_db.py",
                    "--concurrency",
                    "64",
                    "--max-worker-memory-mb",
                    "1024",
                ],
            ),
        ):
            rebuild_db.main()

        # The main-pass pool gets max_workers=2 (2048/1024), not 64.
        assert captured_max_workers[0] == 2

    def test_flag_default_autoscales_with_memory_budget(self, tmp_path: Any) -> None:
        """Without an explicit ``--max-worker-memory-mb``, the default (1024)
        applies.  With 16 GB available that yields 16 capacity, so
        ``--concurrency 4`` still wins via ``min(4, 16)`` and the pool gets
        ``max_workers=4`` — verifying the default does not over-throttle
        when memory is plentiful.  See #2576.
        """
        keys = ["ca/orange/superior_court/raw/abc123.html"]
        cache_dir = str(tmp_path / "cache")
        full = tmp_path / "cache" / keys[0]
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"<html>x</html>")

        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        captured_max_workers: list[int] = []

        def _pool_factory(*args: Any, **kwargs: Any) -> MagicMock:
            captured_max_workers.append(kwargs.get("max_workers", -1))
            return mock_pool

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=keys,
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch(
                "rebuild_db._available_memory_mb",
                return_value=(16384, "cgroup_v2"),
            ),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                side_effect=_pool_factory,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([mock_future]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
                clear=False,
            ),
            patch(
                "sys.argv",
                [
                    "rebuild_db.py",
                    "--concurrency",
                    "4",
                ],
            ),
        ):
            rebuild_db.main()

        assert captured_max_workers[0] == 4

    def test_flag_default_throttles_on_low_memory(self, tmp_path: Any) -> None:
        """With the default ``--max-worker-memory-mb=1024`` and only 4 GB
        available (ECS ecs-run-task.sh default), ``--concurrency 64`` must
        autoscale down to 4 (4096/1024) — preventing the OOM footgun from
        #2576.  This is the regression test for the issue's acceptance
        criterion.
        """
        keys = ["ca/orange/superior_court/raw/abc123.html"]
        cache_dir = str(tmp_path / "cache")
        full = tmp_path / "cache" / keys[0]
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"<html>x</html>")

        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        captured_max_workers: list[int] = []

        def _pool_factory(*args: Any, **kwargs: Any) -> MagicMock:
            captured_max_workers.append(kwargs.get("max_workers", -1))
            return mock_pool

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=keys,
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch(
                "rebuild_db._available_memory_mb",
                return_value=(4096, "cgroup_v2"),
            ),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                side_effect=_pool_factory,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([mock_future]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
                clear=False,
            ),
            patch(
                "sys.argv",
                [
                    "rebuild_db.py",
                    "--concurrency",
                    "64",
                ],
            ),
        ):
            rebuild_db.main()

        assert captured_max_workers[0] == 4


# ---------------------------------------------------------------------------
# Connection-budget pre-flight — #2575
# ---------------------------------------------------------------------------


class TestClampConcurrencyToBudget:
    """Tests for _clamp_concurrency_to_budget() pure function."""

    def test_budget_ok_when_plentiful(self) -> None:
        """172 max_connections, 5 used, 64 requested → no clamp (budget_ok).

        available = int((172-5) * 0.8) = int(133.6) = 133
        133 >= 64 + 1 = 65 → no clamp
        """
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=64, max_connections=172, used=5
        )
        assert effective == 64
        assert reason == "budget_ok"

    def test_clamps_when_budget_tight(self) -> None:
        """20 max_connections, 2 used, 64 requested → clamp to 13.

        available = int((20-2) * 0.8) = int(14.4) = 14
        14 < 64 + 1 = 65 → clamp to max(4, 14-1) = 13
        """
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=64, max_connections=20, used=2
        )
        assert effective == 13
        assert reason == "clamped_to_budget"

    def test_floor_of_four(self) -> None:
        """20 max_connections, 18 used, 64 requested → floor clamps to 4.

        available = int((20-18) * 0.8) = int(1.6) = 1
        1 < 65 → clamp to max(4, 1-1) = max(4, 0) = 4
        """
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=64, max_connections=20, used=18
        )
        assert effective == 4
        assert reason == "clamped_to_budget"

    def test_max_connections_zero_returns_budget_unknown(self) -> None:
        """max_connections=0 → (requested, budget_unknown) — skip clamping."""
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=64, max_connections=0, used=0
        )
        assert effective == 64
        assert reason == "budget_unknown"

    def test_max_connections_negative_returns_budget_unknown(self) -> None:
        """Negative max_connections also returns budget_unknown."""
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=64, max_connections=-1, used=0
        )
        assert effective == 64
        assert reason == "budget_unknown"

    def test_no_clamp_at_boundary(self) -> None:
        """Clamp triggers only when available < requested + 1.

        When available == requested + 1, the condition is false → budget_ok.
        Example: max=100, used=0, headroom_pct=0.0 →
          available = int((100-0)*(1-0.0)) = 100
          requested = 99 → 100 >= 99+1=100 → not clamped
        """
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=99, max_connections=100, used=0, headroom_pct=0.0
        )
        assert effective == 99
        assert reason == "budget_ok"

    def test_clamp_at_exact_boundary(self) -> None:
        """When available == requested, the condition fires → clamped.

        available = int((100-0)*(1-0.0)) = 100
        requested = 100 → 100 < 100+1=101 → clamped to max(4, 100-1)=99
        """
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=100, max_connections=100, used=0, headroom_pct=0.0
        )
        assert effective == 99
        assert reason == "clamped_to_budget"

    def test_custom_floor(self) -> None:
        """floor parameter overrides the default of 4."""
        effective, reason = rebuild_db._clamp_concurrency_to_budget(
            requested=64, max_connections=20, used=18, floor=2
        )
        assert effective == 2
        assert reason == "clamped_to_budget"


class TestValidationSummary:
    """Tests for _group_validation_reasons, _summarize_validation_results, and
    main() integration of the post-rebuild validation summary (#2607)."""

    # ------------------------------------------------------------------
    # Pure-function tests
    # ------------------------------------------------------------------

    def test_group_validation_reasons_splits_concatenated_reasons(self) -> None:
        """Reasons joined by '; ' are split and counted individually."""
        rows = [
            ("fail", "reason A; reason B"),
            ("fail", "reason A"),
        ]
        result = rebuild_db._group_validation_reasons(rows)
        assert result[0] == ("reason A", 2), f"expected reason A:2 first, got {result}"
        assert result[1] == ("reason B", 1), f"expected reason B:1 second, got {result}"

    def test_group_validation_reasons_only_buckets_fail_rows(self) -> None:
        """pass, flag, and error rows must not contribute to the reason bucket."""
        rows = [
            ("pass", "should be ignored"),
            ("flag", "also ignored"),
            ("error", "error ignored"),
            ("fail", "only this counts"),
        ]
        result = rebuild_db._group_validation_reasons(rows)
        assert len(result) == 1
        assert result[0][0] == "only this counts"

    def test_group_validation_reasons_caps_top_3(self) -> None:
        """Only the top 3 distinct reason fragments are returned."""
        rows = [("fail", f"reason_{i}") for i in range(10)]
        result = rebuild_db._group_validation_reasons(rows)
        assert len(result) == 3, f"expected at most 3 reasons, got {len(result)}: {result}"

    def test_group_validation_reasons_empty(self) -> None:
        """Empty input returns an empty list without error."""
        assert rebuild_db._group_validation_reasons([]) == []

    def test_group_validation_reasons_no_fail_rows(self) -> None:
        """When no fail rows exist, returns empty list."""
        rows = [("pass", "all good"), ("flag", "minor issue")]
        assert rebuild_db._group_validation_reasons(rows) == []

    # ------------------------------------------------------------------
    # DB-read helper tests
    # ------------------------------------------------------------------

    def test_summarize_validation_results_aggregates_counts_by_result(self) -> None:
        """Returns correct accepted/flagged/rejected/errors counts from DB rows."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = [
            ("pass", ""),
            ("pass", ""),
            ("flag", "minor"),
            ("fail", "check_no_html_in_ruling_text"),
            ("fail", "check_no_html_in_ruling_text; check_ruling_text_length"),
            ("error", "exception"),
        ]

        from datetime import UTC, datetime

        result = rebuild_db._summarize_validation_results(mock_conn, datetime.now(UTC))

        assert result["accepted"] == 2
        assert result["flagged"] == 1
        assert result["rejected"] == 2
        assert result["errors"] == 1
        assert len(result["top_reasons"]) <= 3
        # check_no_html_in_ruling_text appears in both fail rows → count 2
        top_reason_names = [r for r, _ in result["top_reasons"]]
        assert "check_no_html_in_ruling_text" in top_reason_names

    def test_summarize_validation_results_returns_sentinel_on_failure(self) -> None:
        """When the DB query raises, returns all-zero sentinel without raising."""
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = Exception("DB exploded")

        from datetime import UTC, datetime

        result = rebuild_db._summarize_validation_results(mock_conn, datetime.now(UTC))

        assert result == {
            "accepted": 0,
            "flagged": 0,
            "rejected": 0,
            "errors": 0,
            "top_reasons": [],
        }

    # ------------------------------------------------------------------
    # main() integration tests
    # ------------------------------------------------------------------

    def _run_main_with_mocks(
        self,
        tmp_path: Any,
        *,
        county: str | None = None,
        validation_summary: dict[str, Any],
    ) -> tuple[MagicMock, SystemExit | None]:
        """Run main() with controlled mocks and return (mock_logger, exit_exc).

        Returns (logger_mock, None) if main() completed without sys.exit.
        Returns (logger_mock, SystemExit_exc) if sys.exit was called.
        """
        import sys as _sys

        # Create a minimal local cache with one HTML key.
        key = "ca/orange/superior_court/raw/abc123.html"
        html_dir = tmp_path / "cache" / "ca" / "orange" / "superior_court" / "raw"
        html_dir.mkdir(parents=True)
        (html_dir / "abc123.html").write_bytes(b"<html>Date: 03/15/2026 ruling text</html>")
        cache_dir = str(tmp_path / "cache")

        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": True,
            "hash_mismatch": False,
        }

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        mock_logger = MagicMock()

        argv = ["rebuild_db.py"]
        if county:
            argv += ["--county", county]

        exit_exc: SystemExit | None = None
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch("rebuild_db.list_local_keys", return_value=[key]),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db._fetch_rosters"),
            patch(
                "rebuild_db._summarize_validation_results",
                return_value=validation_summary,
            ),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=mock_pool,
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([mock_future]),
            ),
            patch.dict(
                _sys.modules,
                {},
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": cache_dir,
                },
            ),
            patch("sys.argv", argv),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            try:
                rebuild_db.main()
            except SystemExit as exc:
                exit_exc = exc

        return mock_logger, exit_exc

    def test_main_county_zero_accepted_exits_nonzero(self, tmp_path: Any) -> None:
        """County-scoped run with 0 accepted + 0 flagged exits with code 3."""
        vs = {
            "accepted": 0,
            "flagged": 0,
            "rejected": 15,
            "errors": 0,
            "top_reasons": [("check_no_html", 15)],
        }
        mock_logger, exit_exc = self._run_main_with_mocks(
            tmp_path, county="Orange", validation_summary=vs
        )

        assert exit_exc is not None, "expected sys.exit to be called"
        assert exit_exc.code == 3, f"expected exit code 3, got {exit_exc.code!r}"

        # The error-level log must include the reject hint
        err_calls = mock_logger.error.call_args_list
        summary_errors = [c for c in err_calls if c.args and c.args[0] == "Validation summary"]
        assert len(summary_errors) == 1, (
            f"expected 1 'Validation summary' error log, got: {err_calls}"
        )
        assert summary_errors[0].kwargs.get("rejected") == 15

    def test_main_no_county_zero_accepted_does_not_exit_nonzero(self, tmp_path: Any) -> None:
        """Full (non-county) rebuild with 0 accepted does NOT exit non-zero."""
        vs = {"accepted": 0, "flagged": 0, "rejected": 100, "errors": 0, "top_reasons": []}
        mock_logger, exit_exc = self._run_main_with_mocks(
            tmp_path, county=None, validation_summary=vs
        )

        # Should complete without raising SystemExit (or exit 0 is fine)
        assert exit_exc is None, (
            f"non-county run with zero accepted must not exit non-zero, got {exit_exc!r}"
        )

    def test_main_county_with_accepted_exits_zero(self, tmp_path: Any) -> None:
        """County run with accepted=10 must not raise and must log the summary."""
        vs = {
            "accepted": 10,
            "flagged": 2,
            "rejected": 2,
            "errors": 0,
            "top_reasons": [("check_x", 2)],
        }
        mock_logger, exit_exc = self._run_main_with_mocks(
            tmp_path, county="Ventura", validation_summary=vs
        )

        assert exit_exc is None, f"expected clean exit, got {exit_exc!r}"

        # Validation summary must have been logged (info or warning level)
        all_info = mock_logger.info.call_args_list
        all_warning = mock_logger.warning.call_args_list
        summary_logs = [
            c for c in all_info + all_warning if c.args and c.args[0] == "Validation summary"
        ]
        assert len(summary_logs) == 1, f"expected 1 Validation summary log, got: {summary_logs}"
        kwargs = summary_logs[0].kwargs
        assert "built" in kwargs
        assert "accepted" in kwargs
        assert "rejected" in kwargs
        assert "top_reasons" in kwargs


class TestQueryConnectionBudget:
    """Tests for _query_connection_budget() I/O wrapper."""

    def test_issues_expected_sql(self) -> None:
        """Issues the two expected SQL queries and returns (max, used) tuple."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # fetchone() is called twice: once for max_connections, once for used
        mock_cur.fetchone.side_effect = [(172,), (5,)]

        with patch("psycopg.connect", return_value=mock_conn):
            result = rebuild_db._query_connection_budget("postgres://test")

        assert result == (172, 5)
        # Verify both SQL strings were issued
        execute_calls = [call[0][0] for call in mock_cur.execute.call_args_list]
        assert any("max_connections" in sql for sql in execute_calls), (
            f"expected max_connections query in {execute_calls!r}"
        )
        assert any("pg_stat_activity" in sql for sql in execute_calls), (
            f"expected pg_stat_activity query in {execute_calls!r}"
        )

    def test_failure_returns_zero_zero_and_logs_warning(self) -> None:
        """Connection failure returns (0, 0) and logs a warning — does not raise."""
        mock_logger = MagicMock()

        with (
            patch("psycopg.connect", side_effect=Exception("connection refused")),
            patch.object(rebuild_db, "logger", mock_logger),
        ):
            result = rebuild_db._query_connection_budget("postgres://test")

        assert result == (0, 0)
        # Must have logged a warning — not raised
        calls = mock_logger.warning.call_args_list
        assert mock_logger.warning.called, (
            f"expected warning log on connection failure; got {calls!r}"
        )


# ---------------------------------------------------------------------------
# Per-court reset tests (#3014)
# ---------------------------------------------------------------------------


def _build_per_court_mock_conn(
    court_id: str,
    court_row: tuple[str, str, str, str] | None = None,
    counts: dict[str, int] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a mock psycopg connection + cursor for per-court reset tests.

    The cursor is driven by a scripted ``execute``/``fetchone``/``fetchall``
    sequence so each DELETE/COUNT call returns deterministic data.  Returns
    ``(mock_conn, mock_cur)`` so tests can inspect call history.

    court_row is (id, state, county, court_name).
    """
    counts = counts or {}
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # First fetchone(): _resolve_court_code_to_id → SELECT id FROM derived.courts
    # Returns (uuid,) tuple.
    # Subsequent fetchone() calls are COUNT(*) results.
    count_sequence = [
        (court_id,),  # resolve lookup
        (counts.get("case_judges", 0),),
        (counts.get("case_parties", 0),),
        (counts.get("case_attorneys", 0),),
        (counts.get("documents", 0),),
        (counts.get("rulings", 0),),
        (counts.get("cases", 0),),
        (counts.get("judges", 0),),
    ]
    mock_cur.fetchone.side_effect = count_sequence
    return mock_conn, mock_cur


class TestResolveCourtCodeToId:
    """Tests for _resolve_court_code_to_id()."""

    def test_returns_court_id_string(self) -> None:
        """Returns a single string UUID from derived.courts matching court_code."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = ("00000000-0000-0000-0000-000000000001",)

        result = rebuild_db._resolve_court_code_to_id(mock_conn, "ca-orange")

        assert result == "00000000-0000-0000-0000-000000000001"
        sql, params = mock_cur.execute.call_args[0]
        assert "LOWER(court_code)" in sql
        assert params == ("ca-orange",)

    def test_raises_when_court_code_not_found(self) -> None:
        """Unknown court_code raises ValueError — fail loud, not silent no-op."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None

        try:
            rebuild_db._resolve_court_code_to_id(mock_conn, "ca-nonexistent")
        except ValueError as exc:
            assert "ca-nonexistent" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestDeleteOpensearchDocsForCourt:
    """Tests for delete_opensearch_docs_for_court() (#3014).

    The global ``tentative_rulings_v1`` index cannot be reset under
    ``--reset --court`` because that would wipe docs for every other court.
    Since ``court`` is not unique across counties, the filter must use the
    (state, county, court) triple so the delete-by-query scopes exactly to
    the target court.
    """

    def test_issues_delete_by_query_filtered_on_state_county_court_triple(self) -> None:
        """Calls _delete_by_query on tentative_rulings_v1 filtered on (state, county, court)."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete_by_query.return_value = {"deleted": 17}

        with patch("opensearchpy.OpenSearch", return_value=mock_client):
            result = rebuild_db.delete_opensearch_docs_for_court(
                "http://os", "ca", "Orange", "Superior Court"
            )

        assert result == 17
        mock_client.delete_by_query.assert_called_once()
        kwargs = mock_client.delete_by_query.call_args.kwargs
        assert kwargs["index"] == "tentative_rulings_v1"
        assert kwargs["body"] == {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"state": "ca"}},
                        {"term": {"county": "Orange"}},
                        {"term": {"court": "Superior Court"}},
                    ]
                }
            }
        }
        assert kwargs["refresh"] is True
        assert kwargs["conflicts"] == "proceed"

    def test_no_op_when_index_missing(self) -> None:
        """Returns 0 without calling delete_by_query if the index is missing."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = False

        with patch("opensearchpy.OpenSearch", return_value=mock_client):
            result = rebuild_db.delete_opensearch_docs_for_court(
                "http://os", "ca", "Orange", "Superior Court"
            )

        assert result == 0
        mock_client.delete_by_query.assert_not_called()

    def test_uses_basic_auth_when_credentials_set(self) -> None:
        """OPENSEARCH_USERNAME/PASSWORD env vars feed http_auth tuple."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete_by_query.return_value = {"deleted": 0}

        with (
            patch("opensearchpy.OpenSearch", return_value=mock_client) as mock_ctor,
            patch.dict(
                os.environ,
                {
                    "OPENSEARCH_USERNAME": "admin",
                    "OPENSEARCH_PASSWORD": "s3cret",
                },
                clear=False,
            ),
        ):
            rebuild_db.delete_opensearch_docs_for_court(
                "http://os", "ca", "Orange", "Superior Court"
            )

        kwargs = mock_ctor.call_args.kwargs
        assert kwargs["hosts"] == ["http://os"]
        assert kwargs["timeout"] == 30
        assert kwargs["max_retries"] == 3
        assert kwargs["retry_on_timeout"] is True
        assert kwargs["http_auth"] == ("admin", "s3cret")

    def test_no_auth_when_credentials_unset(self) -> None:
        """Missing env vars → http_auth not passed to the client ctor."""
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete_by_query.return_value = {"deleted": 0}

        env = {k: v for k, v in os.environ.items()}
        env.pop("OPENSEARCH_USERNAME", None)
        env.pop("OPENSEARCH_PASSWORD", None)

        with (
            patch("opensearchpy.OpenSearch", return_value=mock_client) as mock_ctor,
            patch.dict(os.environ, env, clear=True),
        ):
            rebuild_db.delete_opensearch_docs_for_court(
                "http://os", "ca", "Orange", "Superior Court"
            )

        kwargs = mock_ctor.call_args.kwargs
        assert "http_auth" not in kwargs


class TestResetDerivedTablesForCourt:
    """Tests for reset_derived_tables_for_court() (#3014)."""

    def test_raises_when_court_code_not_found(self) -> None:
        """Unknown court_code raises ValueError, not a silent no-op."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None

        try:
            rebuild_db.reset_derived_tables_for_court(mock_conn, "ca-nonexistent")
        except ValueError as exc:
            assert "ca-nonexistent" in str(exc)
        else:
            raise AssertionError("expected ValueError")

        mock_conn.commit.assert_not_called()

    def test_deletes_per_court_and_returns_counts(self) -> None:
        """Resolves court, deletes in dependency order, returns row-count summary."""
        court_id = "00000000-0000-0000-0000-000000000001"
        counts = {
            "case_judges": 50,
            "case_parties": 80,
            "case_attorneys": 20,
            "documents": 2000,
            "rulings": 150,
            "cases": 300,
            "judges": 10,
        }
        mock_conn, mock_cur = _build_per_court_mock_conn(court_id, counts=counts)

        result = rebuild_db.reset_derived_tables_for_court(mock_conn, "ca-orange")

        assert result == counts
        mock_conn.commit.assert_called_once()

        all_sql = [call[0][0] for call in mock_cur.execute.call_args_list]
        joined = "\n".join(all_sql)
        for table in (
            "case_judges",
            "case_parties",
            "case_attorneys",
            "documents",
            "rulings",
            "cases",
            "judges",
        ):
            assert f"DELETE FROM derived.{table}" in joined, (
                f"expected DELETE FROM derived.{table} in emitted SQL"
            )

        assert "TRUNCATE" not in joined
        assert "DELETE FROM derived.courts" not in joined

    def test_delete_order_rulings_before_cases(self) -> None:
        """Must delete rulings before cases (FK: rulings.case_id → cases.id)."""
        court_id = "00000000-0000-0000-0000-000000000001"
        counts = dict.fromkeys(
            (
                "case_judges",
                "case_parties",
                "case_attorneys",
                "documents",
                "rulings",
                "cases",
                "judges",
            ),
            1,
        )
        mock_conn, mock_cur = _build_per_court_mock_conn(court_id, counts=counts)

        rebuild_db.reset_derived_tables_for_court(mock_conn, "ca-orange")

        delete_sql = [
            call[0][0]
            for call in mock_cur.execute.call_args_list
            if "DELETE FROM derived." in call[0][0]
        ]

        def _find(sub: str) -> int:
            for i, s in enumerate(delete_sql):
                if f"DELETE FROM derived.{sub}" in s and ("case_id IN" not in s):
                    return i
            return -1

        rulings_idx = _find("rulings")
        cases_idx = _find("cases")
        documents_idx = _find("documents")
        judges_idx = _find("judges")

        assert rulings_idx != -1, "rulings DELETE not emitted"
        assert cases_idx != -1, "cases DELETE not emitted"
        assert documents_idx != -1, "documents DELETE not emitted"
        assert judges_idx != -1, "judges DELETE not emitted"
        assert rulings_idx < cases_idx
        assert documents_idx < cases_idx
        assert rulings_idx < judges_idx


class TestMainPerCourtResetDispatch:
    """Tests for main()'s dispatch for --reset --court (#3014)."""

    def _common_patches(
        self,
        tmp_path: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        """Set up common patches used by per-court dispatch tests."""
        cache_dir = str(tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        key_dir = tmp_path / "cache" / "ca" / "orange" / "superior_court" / "raw"
        key_dir.mkdir(parents=True)
        (key_dir / "abc123.html").write_bytes(b"<html>x</html>")

        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_future = MagicMock()
        mock_future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
        }
        mock_pool.submit.return_value = mock_future

        return {
            "cache_dir": cache_dir,
            "argv": argv,
            "mock_pool": mock_pool,
            "mock_future": mock_future,
        }

    def test_reset_with_court_uses_per_court_reset(self, tmp_path: Any) -> None:
        """``--reset --court ca-orange`` → per-court delete + OS delete, not global.

        Also verifies OS delete runs BEFORE the DB reset.
        """
        ctx = self._common_patches(tmp_path, ["rebuild_db.py", "--reset", "--court", "ca-orange"])
        parent = MagicMock()
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/orange/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as mock_global_reset,
            patch("rebuild_db.reset_derived_tables_for_county") as mock_per_county_reset,
            patch("rebuild_db.reset_derived_tables_for_court") as mock_per_court_reset,
            patch("rebuild_db.reset_opensearch_index") as mock_os_reset,
            patch("rebuild_db.delete_opensearch_docs_for_county") as mock_os_delete_county,
            patch("rebuild_db.delete_opensearch_docs_for_court") as mock_os_delete_court,
            patch(
                "rebuild_db._resolve_court_for_reset",
                return_value=(
                    "00000000-0000-0000-0000-000000000001",
                    "ca",
                    "Orange",
                    "Superior Court",
                ),
            ),
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=ctx["mock_pool"],
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([ctx["mock_future"]]),
            ),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgres://test",
                    "S3_CACHE_DIR": ctx["cache_dir"],
                    "OPENSEARCH_URL": "http://opensearch:9200",
                },
                clear=False,
            ),
            patch("sys.argv", ctx["argv"]),
        ):
            parent.attach_mock(mock_os_delete_court, "os_delete_court")
            parent.attach_mock(mock_per_court_reset, "per_court_reset")
            rebuild_db.main()

        mock_global_reset.assert_not_called()
        mock_per_county_reset.assert_not_called()
        mock_os_reset.assert_not_called()
        mock_os_delete_county.assert_not_called()
        mock_per_court_reset.assert_called_once()
        mock_os_delete_court.assert_called_once_with(
            "http://opensearch:9200", "ca", "Orange", "Superior Court"
        )
        # OS delete must run BEFORE the DB reset.
        method_names = [name for name, _, _ in parent.mock_calls]
        os_idx = method_names.index("os_delete_court")
        db_idx = method_names.index("per_court_reset")
        assert os_idx < db_idx

    def test_per_court_reset_skips_os_delete_when_url_unset(self, tmp_path: Any) -> None:
        """Without OPENSEARCH_URL, per-court DB reset still runs; OS delete skipped."""
        ctx = self._common_patches(tmp_path, ["rebuild_db.py", "--reset", "--court", "ca-orange"])
        env = {k: v for k, v in os.environ.items()}
        env.pop("OPENSEARCH_URL", None)
        env["DATABASE_URL"] = "postgres://test"
        env["S3_CACHE_DIR"] = ctx["cache_dir"]

        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/orange/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as mock_global_reset,
            patch("rebuild_db.reset_derived_tables_for_court") as mock_per_court_reset,
            patch("rebuild_db.delete_opensearch_docs_for_court") as mock_os_delete_court,
            patch(
                "rebuild_db._resolve_court_for_reset",
                return_value=(
                    "00000000-0000-0000-0000-000000000001",
                    "ca",
                    "Orange",
                    "Superior Court",
                ),
            ),
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker"),
            patch(
                "concurrent.futures.ProcessPoolExecutor",
                return_value=ctx["mock_pool"],
            ),
            patch(
                "concurrent.futures.as_completed",
                return_value=iter([ctx["mock_future"]]),
            ),
            patch.dict(os.environ, env, clear=True),
            patch("sys.argv", ctx["argv"]),
        ):
            rebuild_db.main()

        mock_global_reset.assert_not_called()
        mock_per_court_reset.assert_called_once()
        mock_os_delete_court.assert_not_called()

    def test_court_and_county_are_mutually_exclusive(self) -> None:
        """``--reset --court X --county Y`` must cause argparse SystemExit."""
        argv = ["rebuild_db.py", "--reset", "--court", "ca-orange", "--county", "Orange"]
        with patch("sys.argv", argv):
            try:
                rebuild_db.main()
            except SystemExit:
                pass  # expected — argparse rejects mutually exclusive args
            else:
                raise AssertionError("expected SystemExit from argparse mutual exclusion")


# ---------------------------------------------------------------------------
# build_event courthouse extraction tests (#4014)
# ---------------------------------------------------------------------------


class TestBuildEventCourthouse:
    """build_event sets event["courthouse"] from HTML body text (#4014).

    The rebuild path does not have a courthouse field in the S3 key metadata.
    A best-effort regex scan of the HTML body extracts the courthouse name so
    that normalize_department can skip the letter+digits collapse for LB/CH/AV
    courthouses.
    """

    def _la_parsed(self, content_hash: str = "abc123def456") -> dict[str, str]:
        return {
            "state": "ca",
            "county": "los_angeles",
            "court": "superior_court",
            "content_hash": content_hash,
            "ext": "html",
        }

    def test_chatsworth_courthouse_extracted(self) -> None:
        """HTML body with 'Chatsworth Courthouse North' → event["courthouse"]."""
        html = (
            b"<html><body>Chatsworth Courthouse North Department F49 Tentative Ruling</body></html>"
        )
        parsed = self._la_parsed()
        key = "ca/los_angeles/superior_court/raw/abc123def456.html"
        event = rebuild_db.build_event(key, html, parsed, "test-bucket")
        assert event.get("courthouse") == "Chatsworth Courthouse North"

    def test_long_beach_courthouse_extracted(self) -> None:
        """HTML body with 'Long Beach Courthouse' → event["courthouse"]."""
        html = b"<html><body>Long Beach Courthouse Department S27 Tentative Ruling</body></html>"
        parsed = self._la_parsed()
        key = "ca/los_angeles/superior_court/raw/abc123def456.html"
        event = rebuild_db.build_event(key, html, parsed, "test-bucket")
        assert event.get("courthouse") == "Long Beach Courthouse"

    def test_antelope_valley_courthouse_extracted(self) -> None:
        """HTML body with 'Antelope Valley Courthouse' → event["courthouse"]."""
        html = (
            b"<html><body>Antelope Valley Courthouse Department A14 Tentative Ruling</body></html>"
        )
        parsed = self._la_parsed()
        key = "ca/los_angeles/superior_court/raw/abc123def456.html"
        event = rebuild_db.build_event(key, html, parsed, "test-bucket")
        assert event.get("courthouse") == "Antelope Valley Courthouse"

    def test_no_courthouse_omits_key(self) -> None:
        """HTML body without any courthouse string must not set courthouse."""
        html = b"<html><body>Department X14 Tentative Ruling text here</body></html>"
        parsed = self._la_parsed()
        key = "ca/los_angeles/superior_court/raw/abc123def456.html"
        event = rebuild_db.build_event(key, html, parsed, "test-bucket")
        assert "courthouse" not in event

    def test_courthouse_extraction_idempotent_non_la(self) -> None:
        """Non-LA county HTML with a courthouse name: key may or may not appear,
        but no crash — idempotent."""
        html = b"<html><body>Long Beach Courthouse some content</body></html>"
        parsed = {
            "state": "ca",
            "county": "orange",
            "court": "superior_court",
            "content_hash": "abc123def456",
            "ext": "html",
        }
        key = "ca/orange/superior_court/raw/abc123def456.html"
        # Should not raise
        event = rebuild_db.build_event(key, html, parsed, "test-bucket")
        assert isinstance(event, dict)
