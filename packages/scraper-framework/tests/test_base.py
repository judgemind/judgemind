"""Tests for BaseScraper run loop."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig


class DummyScraper(BaseScraper):
    """Minimal concrete scraper for testing."""

    def __init__(self, docs: list[CapturedDocument], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._docs = docs

    def fetch_documents(self) -> list[CapturedDocument]:
        return self._docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        doc.ruling_text = "Tentative: motion granted."
        return doc


def _make_config() -> ScraperConfig:
    return ScraperConfig(
        scraper_id="test-scraper",
        state="CA",
        county="Test County",
        court="Superior Court",
        target_urls=["https://example.com"],
    )


def _make_doc(config: ScraperConfig) -> CapturedDocument:
    return CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://example.com/ruling/1",
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=b"<html>tentative ruling text</html>",
        content_hash="",
    )


def test_run_returns_health_on_success() -> None:
    config = _make_config()
    doc = _make_doc(config)
    scraper = DummyScraper(docs=[doc], config=config)

    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 1
    assert health.scraper_id == "test-scraper"
    assert health.error_message is None


def test_run_hashes_content() -> None:
    from framework.hashing import sha256_hex

    config = _make_config()
    doc = _make_doc(config)
    captured = []

    class CapturingScraper(DummyScraper):
        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            captured.append(d)
            return d

    scraper = CapturingScraper(docs=[doc], config=config)
    scraper.run()

    assert captured[0].content_hash == sha256_hex(b"<html>tentative ruling text</html>")


def test_run_calls_archiver() -> None:
    config = _make_config()
    doc = _make_doc(config)
    mock_archiver = MagicMock()
    mock_archiver.archive.return_value = "ca/test/key.html"

    scraper = DummyScraper(docs=[doc], config=config, archiver=mock_archiver)
    scraper.run()

    mock_archiver.archive.assert_called_once()


def test_run_emits_events() -> None:
    config = _make_config()
    doc = _make_doc(config)
    mock_bus = MagicMock()

    scraper = DummyScraper(docs=[doc], config=config, event_bus=mock_bus)
    scraper.run()

    mock_bus.emit_document_captured.assert_called_once()
    mock_bus.emit_health.assert_called_once()


def test_run_returns_failure_on_fetch_error() -> None:
    config = _make_config()

    class FailingScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            raise ConnectionError("court website unreachable")

        def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
            return doc

    scraper = FailingScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0
    assert "court website unreachable" in (health.error_message or "")


def test_run_continues_after_single_doc_failure() -> None:
    config = _make_config()
    good_doc = _make_doc(config)

    class PartialFailScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            bad_doc = _make_doc(config)
            bad_doc.raw_content = b""
            return [bad_doc, good_doc]

        def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
            if not doc.raw_content:
                raise ValueError("empty content")
            return doc

    scraper = PartialFailScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 1


def test_run_continues_after_parse_error() -> None:
    """Parse errors are recoverable — the run should skip the bad doc and continue."""
    config = _make_config()
    good_doc = _make_doc(config)
    bad_doc = _make_doc(config)

    class ParseFailScraper(DummyScraper):
        def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
            if doc is bad_doc:
                raise ValueError("malformed HTML — could not extract ruling")
            return super().parse_document(doc)

    scraper = ParseFailScraper(docs=[bad_doc, good_doc], config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 1


def test_run_aborts_on_s3_failure() -> None:
    """S3 archival failures must abort the run — document loss is unacceptable."""
    config = _make_config()
    doc = _make_doc(config)

    failing_archiver = MagicMock()
    failing_archiver.archive.side_effect = OSError("connection reset by peer")

    scraper = DummyScraper(docs=[doc], config=config, archiver=failing_archiver)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0
    assert health.error_message is not None
    assert "S3 archival failed" in health.error_message


def test_run_wraps_s3_error_as_s3_archival_error() -> None:
    """_process_document wraps S3 failures in S3ArchivalError."""
    from framework import S3ArchivalError

    config = _make_config()
    doc = _make_doc(config)

    failing_archiver = MagicMock()
    failing_archiver.archive.side_effect = RuntimeError("boto3 timeout")

    scraper = DummyScraper(docs=[doc], config=config, archiver=failing_archiver)

    import pytest

    with pytest.raises(S3ArchivalError, match="S3 archival failed"):
        scraper._process_document(doc)


def test_run_s3_failure_does_not_count_record() -> None:
    """When S3 fails for the first doc, subsequent docs are not processed."""
    config = _make_config()
    doc1 = _make_doc(config)
    doc2 = _make_doc(config)

    failing_archiver = MagicMock()
    failing_archiver.archive.side_effect = OSError("s3 unavailable")

    scraper = DummyScraper(docs=[doc1, doc2], config=config, archiver=failing_archiver)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0
    # archiver was only called once — run aborted after first S3 failure
    assert failing_archiver.archive.call_count == 1
