"""Tests for BaseScraper run loop."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import structlog.testing

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.hashing import sha256_hex


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
        capture_timestamp=datetime.now(UTC),
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


# ---------------------------------------------------------------------------
# Deterministic document_id (#302 — duplicate ruling prevention)
# ---------------------------------------------------------------------------


def test_document_id_is_deterministic_for_same_content() -> None:
    """Re-processing the same raw content should produce the same document_id.

    This is the key property that prevents duplicate rulings: if the scraper
    fetches the same page on two separate runs, the deterministic document_id
    means insert_document hits ON CONFLICT DO NOTHING and insert_ruling's
    WHERE NOT EXISTS check also skips the insert.
    """
    config = _make_config()
    captured: list[CapturedDocument] = []

    class CapturingScraper(DummyScraper):
        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            captured.append(d)
            return d

    content = b"<html>same content both times</html>"

    # First run
    doc1 = _make_doc(config)
    doc1.raw_content = content
    scraper1 = CapturingScraper(docs=[doc1], config=config)
    scraper1.run()

    # Second run — same content, different initial document_id
    doc2 = _make_doc(config)
    doc2.raw_content = content
    scraper2 = CapturingScraper(docs=[doc2], config=config)
    scraper2.run()

    assert len(captured) == 2
    # The two documents started with different random UUIDs but after
    # _process_document both should have the same deterministic document_id.
    assert captured[0].document_id == captured[1].document_id
    # And it should be the expected uuid5 value
    expected_hash = sha256_hex(content)
    expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, expected_hash))
    assert captured[0].document_id == expected_id


def test_document_id_differs_for_different_content() -> None:
    """Different raw content should produce different document_ids."""
    config = _make_config()
    captured: list[CapturedDocument] = []

    class CapturingScraper(DummyScraper):
        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            captured.append(d)
            return d

    doc1 = _make_doc(config)
    doc1.raw_content = b"<html>ruling A</html>"

    doc2 = _make_doc(config)
    doc2.raw_content = b"<html>ruling B</html>"

    scraper = CapturingScraper(docs=[doc1, doc2], config=config)
    scraper.run()

    assert len(captured) == 2
    assert captured[0].document_id != captured[1].document_id


def test_document_id_is_valid_uuid() -> None:
    """The deterministic document_id should be a valid UUID string."""
    config = _make_config()
    captured: list[CapturedDocument] = []

    class CapturingScraper(DummyScraper):
        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            captured.append(d)
            return d

    doc = _make_doc(config)
    scraper = CapturingScraper(docs=[doc], config=config)
    scraper.run()

    assert len(captured) == 1
    # Should parse as a valid UUID without raising
    parsed = uuid.UUID(captured[0].document_id)
    assert parsed.version == 5


# ---------------------------------------------------------------------------
# PDF empty text warning (#1335)
# ---------------------------------------------------------------------------


def test_process_document_warns_on_empty_pdf_text() -> None:
    """When a PDF document has non-empty raw_content but parse_document yields
    no ruling_text, _process_document should log a warning about a possible
    image-only PDF.

    Uses ``structlog.testing.capture_logs()`` instead of ``capsys`` so
    the assertion is independent of structlog's global renderer config
    (which other tests may change by importing ``ingestion.__main__``).
    """
    config = _make_config()
    pdf_bytes = b"%PDF-1.4 fake image-only content"

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://example.com/ruling.pdf",
        capture_timestamp=datetime.now(UTC),
        content_format=ContentFormat.PDF,
        raw_content=pdf_bytes,
        content_hash="",
    )

    class EmptyPdfScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            return [doc]

        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            # Simulate image-only PDF: text extraction returns empty
            d.ruling_text = ""
            return d

    scraper = EmptyPdfScraper(config=config)

    with structlog.testing.capture_logs() as cap_logs:
        health = scraper.run()

    assert health.success is True
    assert health.records_captured == 1

    warnings = [
        e
        for e in cap_logs
        if e.get("log_level") == "warning" and "image-only PDF" in e.get("event", "")
    ]
    assert warnings, f"Expected a warning about image-only PDF, captured events: {cap_logs!r}"


def test_process_document_no_warning_when_pdf_has_text() -> None:
    """When a PDF document has extracted ruling_text, no warning should be logged."""
    config = _make_config()
    pdf_bytes = b"%PDF-1.4 real content with text"

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://example.com/ruling.pdf",
        capture_timestamp=datetime.now(UTC),
        content_format=ContentFormat.PDF,
        raw_content=pdf_bytes,
        content_hash="",
    )

    class TextPdfScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            return [doc]

        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            d.ruling_text = "The motion is GRANTED."
            return d

    scraper = TextPdfScraper(config=config)

    with structlog.testing.capture_logs() as cap_logs:
        health = scraper.run()

    assert health.success is True

    pdf_warnings = [e for e in cap_logs if "image-only PDF" in e.get("event", "")]
    assert not pdf_warnings, "No warning should be logged when PDF has text"


def test_process_document_no_warning_for_html_without_text() -> None:
    """When an HTML document has no ruling_text, no PDF-specific warning should fire."""
    config = _make_config()

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://example.com/ruling.html",
        capture_timestamp=datetime.now(UTC),
        content_format=ContentFormat.HTML,
        raw_content=b"<html>empty page</html>",
        content_hash="",
    )

    class NoTextScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            return [doc]

        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            d.ruling_text = ""
            return d

    scraper = NoTextScraper(config=config)

    with structlog.testing.capture_logs() as cap_logs:
        scraper.run()

    pdf_warnings = [e for e in cap_logs if "image-only PDF" in e.get("event", "")]
    assert not pdf_warnings, "No PDF warning for HTML documents"


# ---------------------------------------------------------------------------
# ScraperPreconditionFailure + _require_precondition (#2667)
# ---------------------------------------------------------------------------


def test_require_precondition_raises_on_false() -> None:
    """_require_precondition(False, msg) must raise ScraperPreconditionFailure with the
    given message, and ScraperPreconditionFailure must be a RuntimeError subclass.
    """
    from framework import ScraperPreconditionFailure

    config = _make_config()
    scraper = DummyScraper(docs=[], config=config)

    with pytest.raises(ScraperPreconditionFailure, match="session acquisition failed") as exc_info:
        scraper._require_precondition(False, "session acquisition failed")

    # Confirm subclass contract: existing pytest.raises(RuntimeError, ...) assertions
    # in court-specific tests continue to pass unchanged.
    assert isinstance(exc_info.value, RuntimeError)


def test_require_precondition_passes_on_true() -> None:
    """_require_precondition(True, msg) must return None without raising."""
    config = _make_config()
    scraper = DummyScraper(docs=[], config=config)

    result = scraper._require_precondition(True, "session acquisition failed")
    assert result is None


def test_require_precondition_surfaces_as_run_failure() -> None:
    """Integration: a scraper that calls _require_precondition(False, ...) inside
    fetch_documents should cause run() to return a ScraperHealthEvent with
    success=False and the error message captured in error_message.

    This guards the end-to-end silent-zero-records regression (#2620) at the
    framework level: the failure must be visible, not silently swallowed as a
    zero-records success.
    """
    config = _make_config()

    class PreconditionFailScraper(BaseScraper):
        def fetch_documents(self) -> list[CapturedDocument]:
            self._require_precondition(False, "missing session — cannot fetch rulings")
            return []  # unreachable

        def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
            return doc

    scraper = PreconditionFailScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0
    assert health.error_message is not None
    assert "missing" in health.error_message
