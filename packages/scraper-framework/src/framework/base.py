"""BaseScraper — the abstract base class all court scrapers must implement."""

from __future__ import annotations

import abc
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import structlog

from .capture_dedup import content_hash_seen_at, record_capture_dedup_skipped
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

# Default timeout for the per-run shared httpx.Client used to fetch external
# CSS for inlining (#3641).  Matches the per-request timeout in css_inliner.
_INLINE_CSS_CLIENT_TIMEOUT = 10.0


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

    Per-item fetch loops:
        A loop that catches and logs each item's exception must count the
        outcomes with :class:`framework.fetch_tally.FetchTally` and call
        ``tally.raise_if_all_failed(docs)`` before returning, so that a run
        where every item failed is recorded as a failure (#4693).

    Deferred PDF transcription:
        A scraper whose ``parse_document`` intentionally leaves ``ruling_text``
        empty for PDFs, because the ingestion worker transcribes them
        downstream (e.g. the OC multimodal LLM path), sets
        ``defers_pdf_transcription = True``.  This turns off the capture-time
        "possible image-only PDF" warning.  For such a scraper an empty
        ``ruling_text`` says nothing about the PDF's text layer, so the warning
        fired for every document (#4714).  A scraper whose deferred text the
        reingest path cannot recover from ``raw_content`` alone (e.g. a PDF
        wrapped in the CC portal's JSON envelope) also overrides
        ``deferred_ruling_text`` (#4753).
    """

    defers_pdf_transcription: bool = False

    def deferred_ruling_text(
        self,
        raw_content: bytes,
        extract_pdf_text: Callable[[bytes], str | None],
    ) -> str | None:
        """Return ruling text that transcription after capture would produce.

        Called by reingest when ``parse_document`` leaves ``ruling_text``
        empty.  ``extract_pdf_text`` is the caller's PDF text extractor.
        Returns the text; ``""`` when the scraper recognises the content but
        it yields no ruling text (reingest then stores empty text instead of
        the raw content); None when the scraper has nothing to add.
        Default: None (nothing deferred beyond what reingest already does).
        """
        return None

    @classmethod
    def hearing_date_for_raw(
        cls,
        text: str,
        *,
        source_url: str = "",
        content_format: str = "",
        capture_timestamp: datetime | None = None,
    ) -> datetime | None:
        """Return the hearing date a live capture of this raw would carry (#4774).

        ``rebuild_db`` and prefix-mode ``reingest_from_s3`` build ingestion
        events straight from S3, so ``parse_document`` never runs and the
        event has no ``hearing_date``.  The ingestion worker calls this hook
        on such events, before any split, so every split child gets the
        same authoritative date the live scraper would have handed it.

        ``text`` is the document text the worker sees: extracted PDF text
        for PDFs, the raw markup for HTML.  ``source_url`` is the captured
        URL recorded in the S3 object's metadata, or ``""`` when unknown.

        Overrides must derive the date the same way the live scraper does,
        from the filename or a labelled header only.  Never return a date
        from the ruling body, and return None rather than guess (#4682).
        Default: None (the scraper sets no hearing date from the raw).
        """
        return None

    def __init__(
        self,
        config: ScraperConfig,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
        db_conn: object = None,
    ) -> None:
        self.config = config
        self._archiver = archiver
        self._event_bus = event_bus
        self._db_conn = db_conn
        self._log = structlog.get_logger(__name__).bind(scraper_id=config.scraper_id)
        # Lazy per-run shared httpx.Client for inline_css (#3641).  Constructed
        # on first HTML document, closed in run()'s finally block.  Sharing one
        # client across an entire scraper run avoids ~200 redundant TLS
        # handshakes per LA tentatives run (194 docs × 2 parses each).
        self._inline_css_client: httpx.Client | None = None
        # Set by fetch_documents via _mark_partial_failure when the fetch
        # returned docs but skipped items (#4734). Reset at the start of run().
        self._partial_failure: str | None = None

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
        self._partial_failure = None

        try:
            try:
                docs = retry_sync(
                    self.fetch_documents,
                    max_attempts=self.config.max_retries,
                    exceptions=(Exception,),
                )

                for doc in docs:
                    try:
                        captured = self._process_document(doc)
                        if captured:
                            records_captured += 1
                    except Exception as exc:
                        self._log.error(
                            "Failed to process document",
                            source_url=doc.source_url,
                            error=str(exc),
                        )

                if self._partial_failure:
                    # The captured docs are archived above; the run is still
                    # a failure because the fetch skipped items (#4734).
                    success = False
                    error_message = self._partial_failure
                    self._log.error(
                        "Run partially failed",
                        records=records_captured,
                        error=error_message,
                    )
                else:
                    success = True
                    self._log.info("Run complete", records=records_captured)

            except Exception as exc:
                success = False
                error_message = str(exc)
                self._log.error("Run failed", error=error_message)
        finally:
            # Close the shared inline-CSS client if it was created during
            # this run.  Idempotent — safe to call even if no HTML docs
            # triggered construction.  See #3641.
            if self._inline_css_client is not None:
                try:
                    self._inline_css_client.close()
                except Exception as exc:
                    self._log.warning(
                        "Failed to close shared inline_css httpx.Client",
                        error=str(exc),
                    )
                finally:
                    self._inline_css_client = None

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

    def _mark_partial_failure(self, message: str | None) -> None:
        """Record that ``fetch_documents`` is returning docs but skipped items.

        Raising in ``fetch_documents`` would discard the docs it already
        captured, because ``run()`` archives only after the fetch returns.
        Call this instead, just before returning: ``run()`` archives the docs,
        then records ``success=False`` with *message* as ``error_message``.
        ``None`` is a no-op, so callers can pass
        ``FetchTally.partial_failure_message()`` directly.

        Origin: #4734 (SD ROA mid-run abort recorded as success).
        """
        if message:
            self._partial_failure = message

    def _process_document(self, doc: CapturedDocument) -> bool:
        """Inline CSS → hash → derive deterministic ID → parse → archive → emit.

        Returns ``True`` if the document was fully processed (archive + emit),
        or ``False`` if it was skipped due to capture-path content-hash dedup.

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
        # We share one httpx.Client across the entire scraper run to avoid the
        # per-document TLS handshake + DNS lookup overhead — #3641.
        if doc.content_format == ContentFormat.HTML:
            if self._inline_css_client is None:
                self._inline_css_client = httpx.Client(
                    follow_redirects=True,
                    timeout=_INLINE_CSS_CLIENT_TIMEOUT,
                    headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
                )
            try:
                doc.raw_content = inline_css(
                    doc.raw_content,
                    base_url=doc.source_url,
                    http_client=self._inline_css_client,
                )
            except Exception as exc:
                self._log.warning(
                    "CSS inlining failed, continuing with original content",
                    source_url=doc.source_url,
                    error=str(exc),
                )

        doc.content_hash = sha256_hex(doc.raw_content)

        # Capture-path content-hash dedup (#2655): if an identical document
        # was already captured within the look-back window, skip re-archival
        # and re-emission.  The check is best-effort — any error returns None
        # and the scraper continues normally.
        winner_id = content_hash_seen_at(self._db_conn, self.config.county, doc.content_hash)
        if winner_id is not None:
            self._log.warning(
                "capture-path content-hash dedup — skipping re-capture",
                extra={
                    "event": "capture_content_hash_dedup_skipped",
                    "winner_document_id": winner_id,
                    "new_source_url": doc.source_url,
                    "county": self.config.county,
                    "scraper_id": self.config.scraper_id,
                },
            )
            record_capture_dedup_skipped(
                self._db_conn,
                county=self.config.county,
                content_hash=doc.content_hash,
                winner_document_id=winner_id,
                new_source_url=doc.source_url,
                scraper_id=self.config.scraper_id,
            )
            return False

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
        # image-only PDF that pdfplumber cannot OCR (#1335).  Skipped for
        # scrapers that defer transcription to the worker (#4714).
        if (
            doc.content_format == ContentFormat.PDF
            and doc.raw_content
            and not doc.ruling_text
            and not self.defers_pdf_transcription
        ):
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

        return True

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
