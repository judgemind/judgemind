"""BaseScraper — the abstract base class all court scrapers must implement."""

from __future__ import annotations

import abc
import time
from datetime import datetime

import structlog

from .events import EventBus
from .hashing import sha256_hex
from .models import CapturedDocument, ScraperConfig, ScraperHealthEvent, ValidationStatus
from .retry import retry_sync
from .storage import S3Archiver

logger = structlog.get_logger(__name__)


class BaseScraper(abc.ABC):
    """Abstract base class for all Judgemind court scrapers.

    Subclasses must implement:
    - fetch_documents(): perform HTTP requests and return raw CapturedDocuments
    - parse_document(): populate structured fields from raw_content

    The base class handles:
    - Content hashing
    - S3 archival
    - Event emission
    - Retry with exponential backoff
    - Health reporting
    """

    def __init__(
        self,
        config: ScraperConfig,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.config = config
        self._archiver = archiver
        self._event_bus = event_bus
        self._log = structlog.get_logger(__name__).bind(scraper_id=config.scraper_id)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch raw documents from the court website.

        Implementations should:
        - Make HTTP requests (using httpx or playwright as needed)
        - Set raw_content, source_url, content_format, capture_timestamp
        - NOT yet set content_hash or s3_key (handled by run())
        - Respect self.config.request_delay_seconds between requests
        - NOT implement retry (handled by run())
        """

    @abc.abstractmethod
    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse structured fields from doc.raw_content.

        Implementations should populate:
        - case_number, department, judge_name, hearing_date, ruling_text
        - Any court-specific fields in doc.extra

        Should never raise — return the doc with whatever fields were parseable.
        """

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def run(self) -> ScraperHealthEvent:
        """Execute a full scraper run: fetch → hash → archive → emit → health report."""
        start = time.monotonic()
        run_timestamp = datetime.utcnow()
        records_captured = 0
        error_message: str | None = None

        try:
            docs = retry_sync(
                self.fetch_documents,
                max_attempts=self.config.max_retries,
                exceptions=(Exception,),
            )

            for doc in docs:
                try:
                    self._process_document(doc)
                    records_captured += 1
                except Exception as exc:
                    self._log.error(
                        "Failed to process document",
                        source_url=doc.source_url,
                        error=str(exc),
                    )

            success = True
            self._log.info("Run complete", records=records_captured)

        except Exception as exc:
            success = False
            error_message = str(exc)
            self._log.error("Run failed", error=error_message)

        elapsed = time.monotonic() - start
        health = ScraperHealthEvent(
            producer_id=self.config.scraper_id,
            scraper_id=self.config.scraper_id,
            success=success,
            records_captured=records_captured,
            response_time_seconds=elapsed,
            error_message=error_message,
            run_timestamp=run_timestamp,
        )

        if self._event_bus:
            try:
                self._event_bus.emit_health(health)
            except Exception as exc:
                self._log.warning("Failed to emit health event", error=str(exc))

        return health

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_document(self, doc: CapturedDocument) -> None:
        """Hash → parse → archive → emit for a single document."""
        doc.content_hash = sha256_hex(doc.raw_content)
        doc = self.parse_document(doc)

        if self._archiver:
            doc.s3_key = self._archiver.archive(doc)
            doc.s3_bucket = self._archiver.bucket
            doc.validation_status = ValidationStatus.PENDING

        if self._event_bus:
            self._event_bus.emit_document_captured(doc, producer_id=self.config.scraper_id)

    def _make_base_doc(
        self, source_url: str, raw_content: bytes, content_format: object
    ) -> CapturedDocument:
        """Convenience method for subclasses to create a partially-populated CapturedDocument."""

        return CapturedDocument(
            scraper_id=self.config.scraper_id,
            state=self.config.state,
            county=self.config.county,
            court=self.config.court,
            source_url=source_url,
            capture_timestamp=datetime.utcnow(),
            content_format=content_format,
            raw_content=raw_content,
            content_hash="",  # filled in by _process_document
        )
