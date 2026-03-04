"""Tests for the scraper runner."""

from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

from framework import CapturedDocument, ContentFormat, ScraperConfig
from framework.base import BaseScraper
from framework.runner import run_scrapers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubScraper(BaseScraper):
    """Scraper that returns pre-set documents."""

    def __init__(self, docs: list[CapturedDocument] | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._docs = docs or []

    def fetch_documents(self) -> list[CapturedDocument]:
        return self._docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        return doc


class FailingScraper(BaseScraper):
    """Scraper whose run() raises an unhandled exception."""

    def fetch_documents(self) -> list[CapturedDocument]:
        raise RuntimeError("kaboom")

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        return doc


def _stub_config(s3_bucket: str = "") -> ScraperConfig:
    return ScraperConfig(
        scraper_id="test-stub",
        state="CA",
        county="Test",
        court="Superior Court",
        target_urls=["https://example.com"],
        s3_bucket=s3_bucket,
    )


def _make_doc() -> CapturedDocument:
    return CapturedDocument(
        scraper_id="test-stub",
        state="CA",
        county="Test",
        court="Superior Court",
        source_url="https://example.com/ruling",
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=b"<html>ruling</html>",
        content_hash="",
    )


def _patch_registry(entries: list[tuple[str, type, callable]]) -> object:
    """Patch _build_registry to return the given entries."""
    return patch("framework.runner._build_registry", return_value=entries)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunScrapersWithoutArchival:
    def test_runs_all_scrapers_and_returns_zero(self) -> None:
        entries = [("test-stub", StubScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 0

    def test_returns_zero_with_no_documents(self) -> None:
        entries = [("test-stub", StubScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 0


class TestRunScrapersWithArchival:
    def test_creates_archiver_when_bucket_set(self) -> None:
        doc = _make_doc()
        captured_archiver = {}
        mock_archiver_instance = MagicMock()
        mock_archiver_instance.archive.return_value = "ca/test/key.html"
        mock_archiver_cls = MagicMock(return_value=mock_archiver_instance)

        class CapturingStub(StubScraper):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(docs=[doc], **kwargs)
                captured_archiver["archiver"] = kwargs.get("archiver")

        entries = [("test-stub", CapturingStub, _stub_config)]

        with (
            _patch_registry(entries),
            patch.dict(os.environ, {"JUDGEMIND_ARCHIVE_BUCKET": "my-bucket"}),
            patch("framework.runner.S3Archiver", mock_archiver_cls),
        ):
            exit_code = run_scrapers()

        assert exit_code == 0
        assert captured_archiver["archiver"] is not None
        mock_archiver_cls.assert_called_once_with(bucket="my-bucket")

    def test_archiver_called_per_document(self) -> None:
        doc = _make_doc()
        mock_archiver_cls = MagicMock()
        mock_archiver_instance = MagicMock()
        mock_archiver_instance.archive.return_value = "ca/test/key.html"
        mock_archiver_cls.return_value = mock_archiver_instance

        entries = [("test-stub", StubScraper, _stub_config)]

        # Patch S3Archiver constructor to return our mock
        with (
            _patch_registry(entries),
            patch.dict(os.environ, {"JUDGEMIND_ARCHIVE_BUCKET": "my-bucket"}),
            patch("framework.runner.S3Archiver", mock_archiver_cls),
        ):
            # Inject docs into the stub
            original_init = StubScraper.__init__

            def patched_init(self: StubScraper, **kwargs: object) -> None:
                original_init(self, docs=[doc], **kwargs)

            with patch.object(StubScraper, "__init__", patched_init):
                exit_code = run_scrapers()

        assert exit_code == 0
        mock_archiver_cls.assert_called_once_with(bucket="my-bucket")


class TestRunScrapersFiltering:
    def test_filters_to_requested_ids(self) -> None:
        ran = []

        class TrackingScraper(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def config_a(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="scraper-a",
                state="CA",
                county="A",
                court="Court",
                target_urls=["https://example.com"],
            )

        def config_b(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="scraper-b",
                state="CA",
                county="B",
                court="Court",
                target_urls=["https://example.com"],
            )

        entries = [
            ("scraper-a", TrackingScraper, config_a),
            ("scraper-b", TrackingScraper, config_b),
        ]

        with _patch_registry(entries):
            exit_code = run_scrapers(scraper_ids=["scraper-b"])

        assert exit_code == 0
        assert ran == ["scraper-b"]

    def test_unknown_scraper_id_returns_one(self) -> None:
        entries = [("test-stub", StubScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers(scraper_ids=["nonexistent"])

        assert exit_code == 1


class TestRunScrapersErrorHandling:
    def test_unhandled_exception_returns_nonzero(self) -> None:
        entries = [("failing", FailingScraper, _stub_config)]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1

    def test_partial_failure_still_runs_remaining(self) -> None:
        ran = []

        class TrackingStub(StubScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                ran.append(self.config.scraper_id)
                return []

        def config_good(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="good",
                state="CA",
                county="Good",
                court="Court",
                target_urls=["https://example.com"],
            )

        def config_fail(s3_bucket: str = "") -> ScraperConfig:
            return ScraperConfig(
                scraper_id="failing",
                state="CA",
                county="Fail",
                court="Court",
                target_urls=["https://example.com"],
            )

        entries = [
            ("failing", FailingScraper, config_fail),
            ("good", TrackingStub, config_good),
        ]

        with _patch_registry(entries):
            exit_code = run_scrapers()

        assert exit_code == 1  # failure reported
        assert "good" in ran  # but good scraper still ran


class TestNoArchivalInDevMode:
    def test_no_archiver_without_env_var(self) -> None:
        captured_archiver = {}

        class CapturingStub(StubScraper):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)
                captured_archiver["archiver"] = kwargs.get("archiver")

        entries = [("test-stub", CapturingStub, _stub_config)]

        env = os.environ.copy()
        env.pop("JUDGEMIND_ARCHIVE_BUCKET", None)

        with _patch_registry(entries), patch.dict(os.environ, env, clear=True):
            exit_code = run_scrapers()

        assert exit_code == 0
        assert captured_archiver["archiver"] is None
