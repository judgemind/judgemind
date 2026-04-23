"""BaseScraper — the abstract base class all court scrapers must implement."""

from __future__ import annotations

import abc
import time
import uuid
from datetime import UTC, datetime

import structlog

from .css_inliner import inline_css
from .events import EventBus
from .hashing import sha256_hex
from .models import (
    CapturedDocument,
    ContentFormat,
    ScraperConfig,
    ScraperHealthEvent,
    ValidationStatus,
)
from .retry import retry_sync
from .storage import S3Archiver

logger = structlog.get_logger(__name__)


class ScraperPreconditionFailure(RuntimeError):  # noqa: N818
    """Raised when a prerequisite step (e.g. session acquisition, auth handshake)
    fails before any documents can be fetched.

    Subclasses RuntimeError (not plain Exception) so that existing
    ``pytest.raises(RuntimeError, match=...)`` assertions in court-specific
    tests continue to pass unchanged — ``isinstance(ScraperPreconditionFailure(...),
    RuntimeError) is True``.

    Canonical example: SF civil tentative scraper session acquisition (#2620).
    Introduced in #2667.
    """


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

    Precondition failures:
        If ``fetch_documents`` requires a prerequisite step (session acquisition,
        auth token, proxy handshake) that, when it fails, prevents fetching *any*
        documents, call ``self._require_precondition(cond, msg)`` instead of
        returning ``[]``.  Returning ``[]`` would be recorded by ``run()`` as a
        successful zero-records run and mask silent outages — see #2620.
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
        run_timestamp = datetime.now(UTC)
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

    def _require_precondition(self, cond: bool, msg: str) -> None:
        """Raise ScraperPreconditionFailure if *cond* is falsy.

        Use this in ``fetch_documents`` for prerequisite steps — session
        acquisition, auth token exchange, proxy handshake — that, when they
        fail, prevent *any* documents from being fetched.  Calling
        ``return []`` instead would be indistinguishable from a legitimate
        "no rulings today" result: ``run()`` would record
        ``success=True, records=0``, masking silent outages in CloudWatch.

        Example::

            self._require_precondition(
                self._session_id is not None,
                "session acquisition failed",
            )

        Origin: #2620 (SF civil silent-zero bug), introduced in #2667.
        """
        if not cond:
            raise ScraperPreconditionFailure(msg)

    def _process_document(self, doc: CapturedDocument) -> None:
        """Inline CSS → hash → derive deterministic ID → parse → archive → emit.

        For HTML-format documents, external CSS is inlined into the HTML before
        hashing so that archived documents are self-contained and render
        correctly when viewed directly.

        The document_id is derived deterministically from the content hash so
        that re-scraping the same content produces the same UUID. This makes
        insert_document (ON CONFLICT DO NOTHING on documents.id) and
        insert_ruling (WHERE NOT EXISTS on document_id) idempotent across
        scraper runs — the same raw content always maps to the same document.

        For pre-split child documents (``doc.extra["pre_split"] == True``)
        that share the same ``raw_content`` as their siblings (e.g. a
        multi-ruling PDF split into per-ruling children), the document_id
        is further salted with ``ruling_index`` so each child gets a unique
        ``document_id``.  Without this, all split children would collide on
        ``rulings.document_id`` UNIQUE, causing only the first child to
        land in the DB (#2367).
        """
        # Inline CSS for HTML documents (makes archived HTML self-contained).
        # TODO(perf): If a scraper produces many HTML docs per run, consider
        # sharing an httpx.Client across calls to avoid per-document overhead.
        if doc.content_format == ContentFormat.HTML:
            try:
                doc.raw_content = inline_css(doc.raw_content, base_url=doc.source_url)
            except Exception as exc:
                self._log.warning(
                    "CSS inlining failed, continuing with original content",
                    source_url=doc.source_url,
                    error=str(exc),
                )

        doc.content_hash = sha256_hex(doc.raw_content)
        # Deterministic document_id: same content → same UUID → dedup works.
        # uuid5 with NAMESPACE_URL is a standard way to derive reproducible
        # UUIDs from a string key (here, the SHA-256 hex digest).
        parent_document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, doc.content_hash))

        # Pre-split children share the same raw_content but represent
        # different rulings extracted from it.  Salt the document_id with
        # the ruling_index so each child gets a unique UUID that still
        # round-trips deterministically.  See ingestion.split_ids for the
        # same helper used by the reingest path (#2367).
        if doc.extra.get("pre_split") and "ruling_index" in doc.extra:
            from ingestion.split_ids import make_split_document_id

            doc.document_id = make_split_document_id(parent_document_id, doc.extra["ruling_index"])
        else:
            doc.document_id = parent_document_id
        doc = self.parse_document(doc)

        # Warn when a non-empty PDF yields no extracted text — likely an
        # image-only PDF that pdfplumber cannot OCR (#1335).
        if doc.content_format == ContentFormat.PDF and doc.raw_content and not doc.ruling_text:
            self._log.warning(
                "PDF text extraction returned empty — possible image-only PDF",
                source_url=doc.source_url,
                pdf_size=len(doc.raw_content),
            )

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
            capture_timestamp=datetime.now(UTC),
            content_format=content_format,
            raw_content=raw_content,
            content_hash="",  # filled in by _process_document
        )
