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
