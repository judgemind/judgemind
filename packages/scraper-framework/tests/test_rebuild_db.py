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
from unittest.mock import MagicMock, patch

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
        # Create a local cache file
        key = "ca/orange/superior_court/raw/abc123.html"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / "abc123.html").write_bytes(b"<html>Date: 03/15/2026 ruling text</html>")

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
        key = "ca/orange/superior_court/raw/abc456.pdf"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / "abc456.pdf").write_bytes(b"%PDF-1.4 binary content here")

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
        key = "ca/orange/superior_court/raw/abc789.html"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        (key_path / "abc789.html").write_bytes(b"<html>some content</html>")

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
        assert "ca_orange_superior_court" in codes
        assert "ca_los_angeles_superior_court" in codes

    def test_skips_invalid_keys(self) -> None:
        keys = ["invalid/key", "also/not/valid"]
        courts = rebuild_db.discover_courts(keys)
        assert len(courts) == 0


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
