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

    def test_returns_error_on_hash_mismatch(self, tmp_path: Any) -> None:
        """Hash mismatch should return status='error' and not proceed."""
        # The key says the hash is "abc123" but the actual content hashes
        # to something different.
        key = "ca/orange/superior_court/raw/abc123.html"
        cache_dir = str(tmp_path)
        key_path = tmp_path / "ca" / "orange" / "superior_court" / "raw"
        key_path.mkdir(parents=True)
        # Write content whose SHA256 will NOT match "abc123"
        (key_path / "abc123.html").write_bytes(b"<html>content with wrong hash</html>")

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
        assert result["had_hearing_date"] is False

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
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch(
                "rebuild_db.list_local_keys",
                return_value=["ca/santa_clara/superior_court/raw/abc123.html"],
            ),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as mock_global_reset,
            patch("rebuild_db.reset_derived_tables_for_county") as mock_per_county_reset,
            patch("rebuild_db.reset_opensearch_index") as mock_os_reset,
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
            rebuild_db.main()

        mock_global_reset.assert_not_called()
        mock_per_county_reset.assert_called_once()
        # Args: (conn, state, county).  State defaults to "ca".
        call_args = mock_per_county_reset.call_args
        assert call_args[0][1] == "ca"
        assert call_args[0][2] == "Santa Clara"
        # Must skip OpenSearch index reset under per-county mode.
        mock_os_reset.assert_not_called()

    def test_no_reset_does_not_touch_derived_tables(self, tmp_path: Any) -> None:
        """Without ``--reset``, neither reset function is called — existing
        incremental behavior preserved.
        """
        ctx = self._common_patches(tmp_path, ["rebuild_db.py", "--county", "Santa Clara"])
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
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
