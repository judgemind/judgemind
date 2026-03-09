"""Ingestion worker — Redis Streams consumer that writes document.captured events
to Postgres and OpenSearch.

Designed to run as a long-lived process (ECS Fargate service). One process per
replica; multiple replicas share the same consumer group and partition work via
Redis Streams competitive consumption.

Environment variables:
    DATABASE_URL      — PostgreSQL DSN (required)
    REDIS_URL         — Redis URL, e.g. redis://localhost:6379 (required)
    OPENSEARCH_URL    — OpenSearch endpoint, e.g. https://localhost:9200 (required)
    JUDGEMIND_ARCHIVE_BUCKET — S3 bucket for document content (required for full-text indexing)
    LLM_PROVIDER      — LLM provider: "google" (default) or "anthropic"
    LLM_MODEL         — Model ID (provider-specific; sensible defaults per provider)
    GOOGLE_API_KEY     — Required when LLM_PROVIDER is "google" (default)
    ANTHROPIC_API_KEY  — Required when LLM_PROVIDER is "anthropic"
    MAX_RETRIES       — Per-message retry limit before dead-lettering (default: 3)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import psycopg.errors

from framework.search.indexer import IndexingConsumer
from framework.search.mapping import TENTATIVE_RULINGS_ALIAS

from .db import (
    batch_upsert_parties,
    insert_document,
    insert_ruling,
    resolve_judge,
    upsert_case,
    upsert_case_judge,
    upsert_court,
)
from .extract import (
    extract_case_number,
    extract_case_title,
    extract_hearing_date,
    extract_judge_name,
    extract_motion_type,
    extract_outcome,
    extract_parties_from_caption,
)
from .llm_extract import LLMExtractionResult, LLMRulingResult, extract_fields_llm
from .llm_providers import create_client as create_llm_client
from .text_cleanup import clean_ruling_text

if TYPE_CHECKING:
    from opensearchpy import OpenSearch
    from redis import Redis

logger = logging.getLogger(__name__)

# Fields that LLM extraction can populate when missing from the scraper event.
EXTRACTABLE_FIELDS = (
    "hearing_date",
    "outcome",
    "motion_type",
    "case_number",
    "case_title",
    "judge_name",
    "department",
    "parties",
)

# ---------------------------------------------------------------------------
# Infrastructure vs message error classification
# ---------------------------------------------------------------------------

# psycopg error classes that indicate infrastructure problems (DB unreachable,
# schema missing, connection dropped). These should never cause dead-lettering
# because the message itself is fine — the infrastructure is broken.
_INFRA_PG_ERRORS: tuple[type[Exception], ...] = (
    psycopg.OperationalError,  # connection refused, server closed, etc.
    psycopg.InterfaceError,  # connection already closed
    psycopg.errors.UndefinedTable,  # relation does not exist (migration not run)
    psycopg.errors.UndefinedColumn,  # column does not exist (schema mismatch)
    psycopg.errors.InsufficientPrivilege,  # permission denied
    psycopg.errors.UndefinedFunction,  # function/type does not exist
    psycopg.errors.InvalidCatalogName,  # database does not exist
)

# Non-psycopg errors that indicate infrastructure problems.
_INFRA_GENERIC_ERRORS: tuple[type[Exception], ...] = (
    ConnectionError,
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
    OSError,
)


class InfrastructureError(Exception):
    """Raised when a message processing failure is caused by infrastructure,
    not by bad message data.

    The worker should exit (non-zero) on this error so ECS can restart it.
    Messages must NOT be acknowledged — they stay in the stream for processing
    after the infrastructure issue is resolved.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.__cause__ = cause


def is_infrastructure_error(exc: Exception) -> bool:
    """Return True if the exception indicates an infrastructure problem.

    Infrastructure errors mean the message itself is fine but the system
    cannot process it right now (DB down, schema missing, etc.). These
    should NOT cause dead-lettering.
    """
    return isinstance(exc, (*_INFRA_PG_ERRORS, *_INFRA_GENERIC_ERRORS))


STREAM_DOCUMENT_CAPTURED = "document.captured"
CONSUMER_GROUP = "ingestion-workers"
# Unique per process so multiple workers can share the group without collision
CONSUMER_NAME = f"ingestion-{socket.gethostname()}-{os.getpid()}"

DEFAULT_BATCH_SIZE = 10
DEFAULT_BLOCK_MS = 5000
DEFAULT_MAX_RETRIES = 3


class IngestionWorker:
    """Consumes document.captured events from Redis Streams.

    For each event:
      1. Upserts court, case, and document rows in Postgres.
      2. Inserts a ruling row in Postgres (idempotent).
      3. Indexes the document in OpenSearch via IndexingConsumer.
      4. Acknowledges the message (XACK) only after both writes succeed.

    Error handling distinguishes two categories:

    **Infrastructure errors** (DB down, missing tables, connection failures):
      Raised as InfrastructureError without acknowledging the message. The
      worker process exits with non-zero status so ECS restarts it. Messages
      remain in the stream and will be reprocessed after recovery.

    **Message-level errors** (bad data, validation failures, constraint violations):
      Retried up to max_retries times. After exhaustion, the message is
      acknowledged (dead-letter pattern) and logged as CRITICAL for alerting.
    """

    def __init__(
        self,
        redis_client: Redis,
        pg_dsn: str,
        opensearch_client: OpenSearch,
        s3_client: Any,
        archive_bucket: str,
        max_retries: int = DEFAULT_MAX_RETRIES,
        llm_client: object | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        self._redis = redis_client
        self._pg_dsn = pg_dsn
        self._max_retries = max_retries
        self._conn: psycopg.Connection | None = None
        self._indexer = IndexingConsumer(
            opensearch_client=opensearch_client,
            s3_client=s3_client,
            bucket=archive_bucket,
            index_name=TENTATIVE_RULINGS_ALIAS,
            ensure_index=True,
        )

        # LLM extraction — provider and model resolved from args, then env vars.
        self._llm_provider: str | None = llm_provider or os.environ.get("LLM_PROVIDER")
        self._llm_model: str | None = llm_model or os.environ.get("LLM_MODEL")
        self._llm_client: object | None = llm_client
        if self._llm_client is None:
            self._llm_client = create_llm_client(provider=self._llm_provider)
        if self._llm_client is None:
            logger.warning("LLM API key not set — LLM extraction disabled, using regex-only mode")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _get_connection(self) -> psycopg.Connection:
        """Return the persistent Postgres connection, creating or reconnecting as needed.

        The connection is created lazily on first call and reused across all
        subsequent ``process_event`` invocations. If the connection is closed
        (e.g. server restart, network blip), a new one is transparently created.

        Autocommit is OFF — callers manage transactions explicitly via
        ``conn.commit()`` / ``conn.rollback()``.
        """
        if self._conn is None or self._conn.closed:
            if self._conn is not None:
                logger.info("Reconnecting to Postgres (previous connection was closed)")
            self._conn = psycopg.connect(self._pg_dsn, autocommit=False)
        return self._conn

    def close(self) -> None:
        """Close the persistent Postgres connection if open.

        Safe to call multiple times. Called automatically when the worker
        shuts down via ``run()``.
        """
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            self._conn = None

    def health_check(self) -> None:
        """Verify DB connectivity and that required tables exist.

        Raises InfrastructureError if the database is unreachable or the
        schema is not ready. Called on startup before consuming messages.

        Uses the persistent connection so the same connection is reused for
        subsequent ``process_event`` calls.
        """
        required_tables = ("courts", "cases", "documents", "rulings")
        try:
            conn = self._get_connection()
            with conn.cursor() as cur:
                for table in required_tables:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables WHERE table_name = %s LIMIT 1",
                        (table,),
                    )
            conn.rollback()  # Release any read-lock from the health check
        except (*_INFRA_PG_ERRORS, *_INFRA_GENERIC_ERRORS) as exc:
            raise InfrastructureError(exc) from exc

    def run(
        self,
        batch_size: int = DEFAULT_BATCH_SIZE,
        block_ms: int = DEFAULT_BLOCK_MS,
    ) -> None:
        """Block indefinitely, processing events from the Redis stream.

        Call this from the process entrypoint. Returns only on KeyboardInterrupt.
        Raises InfrastructureError if the database is unavailable or the schema
        is missing — this causes a non-zero exit so ECS can restart the task.
        """
        self.health_check()
        self._ensure_consumer_group()
        logger.info(
            "Ingestion worker started",
            extra={
                "stream": STREAM_DOCUMENT_CAPTURED,
                "group": CONSUMER_GROUP,
                "consumer": CONSUMER_NAME,
            },
        )

        try:
            while True:
                try:
                    self._process_batch(batch_size=batch_size, block_ms=block_ms)
                except KeyboardInterrupt:
                    logger.info("Ingestion worker stopped")
                    break
                except InfrastructureError:
                    # Propagate infra errors to exit the process for ECS restart.
                    # Messages stay unacknowledged in the stream.
                    raise
                except Exception as exc:
                    logger.error("Unexpected error in consumer loop: %s", exc, exc_info=True)
        finally:
            self.close()

    def process_event(self, event_data: dict[str, Any]) -> None:
        """Process a single deserialized event dict.

        Extraction strategy (per field):
          1. Use the scraper-provided value if present.
          2. Try LLM extraction (if the LLM provider API key is configured).
          3. Fall back to regex extraction.

        Exposed for testing. Raises on unrecoverable errors.
        """
        document_id: str = event_data["document_id"]
        state: str = event_data["state"]
        county: str = event_data["county"]
        court: str = event_data.get("court", "Superior Court")
        case_number: str | None = event_data.get("case_number")
        case_title: str | None = event_data.get("case_title")
        department: str | None = event_data.get("department")
        judge_name: str | None = event_data.get("judge_name")
        ruling_text: str | None = event_data.get("ruling_text")
        content_format: str = event_data.get("content_format", "html")
        content_hash: str = event_data.get("content_hash", "")
        s3_key: str | None = event_data.get("s3_key")
        s3_bucket: str | None = event_data.get("s3_bucket")
        source_url: str = event_data.get("source_url", "")
        scraper_id: str = event_data.get("scraper_id", "")

        # Parse timestamps
        capture_ts = _parse_datetime(event_data.get("capture_timestamp"))
        hearing_dt = _parse_date(event_data.get("hearing_date"))

        outcome: str | None = event_data.get("outcome")
        motion_type: str | None = event_data.get("motion_type")
        parties_data: list[dict[str, str]] = event_data.get("parties", [])

        # ------------------------------------------------------------------
        # Determine which fields are missing and need extraction
        # ------------------------------------------------------------------
        missing_fields = [f for f in EXTRACTABLE_FIELDS if not event_data.get(f)]

        # Track which method populated each extracted field for logging
        extraction_methods: dict[str, str] = {}

        # ------------------------------------------------------------------
        # LLM extraction — primary method for missing fields
        # ------------------------------------------------------------------
        llm_result: LLMExtractionResult | None = None
        if missing_fields and ruling_text and self._llm_client is not None:
            metadata = {
                "link_text": event_data.get("extra", {}).get("link_text")
                if isinstance(event_data.get("extra"), dict)
                else None,
                "judge_name": event_data.get("judge_name"),
                "department": event_data.get("department"),
            }
            t0 = time.monotonic()
            llm_result = extract_fields_llm(
                document_text=ruling_text,
                content_format=content_format,
                metadata=metadata,
                client=self._llm_client,
                provider=self._llm_provider,
                model=self._llm_model,
            )
            llm_latency_ms = round((time.monotonic() - t0) * 1000)

            if llm_result is not None:
                # Find the matching ruling by case_number if available
                ruling = _match_ruling(llm_result, case_number)

                # Apply document-level fields from LLM
                if hearing_dt is None and llm_result.hearing_date is not None:
                    hearing_dt = llm_result.hearing_date
                    extraction_methods["hearing_date"] = "llm"
                if not judge_name and llm_result.judge_name:
                    judge_name = llm_result.judge_name
                    extraction_methods["judge_name"] = "llm"
                if not department and llm_result.department:
                    department = llm_result.department
                    extraction_methods["department"] = "llm"

                # Apply ruling-level fields from the matched ruling
                if ruling is not None:
                    if not case_number and ruling.case_number:
                        case_number = ruling.case_number
                        extraction_methods["case_number"] = "llm"
                    if not case_title and ruling.case_title:
                        case_title = ruling.case_title
                        extraction_methods["case_title"] = "llm"
                    if outcome is None and ruling.outcome:
                        outcome = ruling.outcome
                        extraction_methods["outcome"] = "llm"
                    if motion_type is None and ruling.motion_type:
                        motion_type = ruling.motion_type
                        extraction_methods["motion_type"] = "llm"
                    if not parties_data and ruling.parties:
                        parties_data = ruling.parties
                        extraction_methods["parties"] = "llm"

                logger.info(
                    "LLM extraction completed",
                    extra={
                        "document_id": document_id,
                        "llm_latency_ms": llm_latency_ms,
                        "extraction_methods": extraction_methods,
                        "llm_case_count": llm_result.case_count,
                    },
                )
            else:
                logger.info(
                    "LLM extraction returned None — falling back to regex",
                    extra={
                        "document_id": document_id,
                        "llm_latency_ms": llm_latency_ms,
                    },
                )

        # ------------------------------------------------------------------
        # Regex fallback — fill any fields still missing after LLM
        # ------------------------------------------------------------------
        if hearing_dt is None and ruling_text:
            hearing_dt = extract_hearing_date(ruling_text)
            if hearing_dt is not None:
                extraction_methods.setdefault("hearing_date", "regex")
                logger.info(
                    "Extracted hearing_date from ruling text (regex fallback)",
                    extra={"document_id": document_id, "hearing_date": str(hearing_dt)},
                )

        if ruling_text and (outcome is None or motion_type is None):
            if outcome is None:
                outcome = extract_outcome(ruling_text)
                if outcome is not None:
                    extraction_methods.setdefault("outcome", "regex")
            if motion_type is None:
                motion_type = extract_motion_type(ruling_text)
                if motion_type is not None:
                    extraction_methods.setdefault("motion_type", "regex")

        # Clean ruling text for display — extraction uses raw text above for
        # better regex matching; the cleaned version is stored in Postgres.
        cleaned_ruling_text = clean_ruling_text(ruling_text)

        court_name = f"{court}, County of {county}"

        # Fallback case number extraction (regex)
        if not case_number and ruling_text:
            extracted = extract_case_number(ruling_text)
            if extracted:
                extraction_methods.setdefault("case_number", "regex")
                logger.info(
                    "Extracted case_number from ruling text (regex fallback)",
                    extra={"document_id": document_id, "case_number": extracted},
                )
                case_number = extracted

        # Fallback case_title extraction (regex)
        if not case_title and ruling_text:
            case_title = extract_case_title(ruling_text)
            if case_title:
                extraction_methods.setdefault("case_title", "regex")

        # Fallback judge_name extraction from ruling text (#401).
        if not judge_name and ruling_text:
            judge_name = extract_judge_name(ruling_text)
            if judge_name:
                extraction_methods.setdefault("judge_name", "regex")
                logger.info(
                    "Extracted judge_name from ruling text (regex fallback)",
                    extra={"document_id": document_id, "judge_name": judge_name},
                )

        # Fallback: if no parties yet but we have a case title with "v.",
        # extract plaintiff/defendant from the caption.
        effective_title = case_title or event_data.get("case_title")
        if not parties_data and effective_title:
            parties_data = extract_parties_from_caption(effective_title)
            if parties_data:
                extraction_methods.setdefault("parties", "regex")
                logger.info(
                    "Extracted parties from case caption (regex fallback)",
                    extra={
                        "document_id": document_id,
                        "party_count": len(parties_data),
                    },
                )

        if extraction_methods:
            logger.info(
                "Field extraction summary",
                extra={
                    "document_id": document_id,
                    "extraction_methods": extraction_methods,
                },
            )

        conn = self._get_connection()
        try:
            # 1. Ensure court exists
            court_id = upsert_court(conn, state, county, court_name)

            # 2. Ensure case exists — use document_id as synthetic case_number if absent
            effective_case_number = case_number or f"UNKNOWN-{document_id}"
            if effective_case_number.startswith("UNKNOWN-"):
                logger.warning(
                    "No case_number extractable for document — using synthetic UNKNOWN identifier",
                    extra={"document_id": document_id},
                )
            case_id = upsert_case(conn, effective_case_number, court_id, case_title=case_title)

            # 3. Insert document (idempotent on document_id)
            is_new = insert_document(
                conn,
                document_id=document_id,
                case_id=case_id,
                court_id=court_id,
                content_format=content_format,
                content_hash=content_hash,
                s3_key=s3_key,
                s3_bucket=s3_bucket,
                source_url=source_url,
                scraper_id=scraper_id,
                captured_at=capture_ts or datetime.utcnow(),
                hearing_date=hearing_dt,
            )

            # 4. Resolve judge name to canonical judge record
            judge_id: str | None = None
            if judge_name:
                judge_id = resolve_judge(conn, judge_name, court_id)

            # 5. Insert ruling (only if hearing_date is known)
            if hearing_dt is not None:
                insert_ruling(
                    conn,
                    document_id=document_id,
                    case_id=case_id,
                    court_id=court_id,
                    hearing_date=hearing_dt,
                    ruling_text=cleaned_ruling_text,
                    department=department,
                    judge_id=judge_id,
                    outcome=outcome,
                    motion_type=motion_type,
                )
            else:
                logger.warning("No hearing_date for document %s — ruling row skipped", document_id)

            # 6. Link case to judge
            if judge_id is not None:
                upsert_case_judge(conn, case_id, judge_id, hearing_dt)

            # 7. Create party records and link to case (batched — O(1) queries)
            batch_upsert_parties(conn, case_id, parties_data)

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        if is_new:
            # Index in OpenSearch — document_id is used as the OS doc id
            # so rulings.document_id FK aligns with OpenSearch _id
            self._indexer.index_document(
                {
                    "document_id": document_id,
                    "case_number": case_number,
                    "court": court_name,
                    "county": county,
                    "state": state,
                    "judge_name": judge_name,
                    "hearing_date": event_data.get("hearing_date"),
                    "ruling_text": ruling_text,
                    "s3_key": s3_key,
                    "content_hash": content_hash,
                    "content_format": content_format,
                }
            )
        else:
            logger.debug("Document %s already in Postgres — skipping OpenSearch index", document_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_consumer_group(self) -> None:
        try:
            self._redis.xgroup_create(
                STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, id="0", mkstream=True
            )
            logger.info(
                "Created consumer group %s on stream %s", CONSUMER_GROUP, STREAM_DOCUMENT_CAPTURED
            )
        except Exception:
            # Group already exists — this is expected on restart
            pass

    def _process_batch(self, batch_size: int, block_ms: int) -> None:
        messages = self._redis.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            {STREAM_DOCUMENT_CAPTURED: ">"},
            count=batch_size,
            block=block_ms,
        )
        if not messages:
            return

        for _stream_name, entries in messages:
            for msg_id, data in entries:
                self._process_message(msg_id, data)

    def _process_message(self, msg_id: bytes, data: dict[bytes, bytes]) -> None:
        raw = data.get(b"data", data.get("data", "{}"))
        if isinstance(raw, bytes):
            raw = raw.decode()

        try:
            event_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("Malformed event message %s: %s — dead-lettering", msg_id, exc)
            self._redis.xack(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, msg_id)
            return

        attempt = 0
        last_exc: Exception | None = None
        while attempt < self._max_retries:
            try:
                self.process_event(event_data)
                self._redis.xack(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, msg_id)
                return
            except Exception as exc:
                if is_infrastructure_error(exc):
                    # Infrastructure is broken — do NOT acknowledge the message.
                    # Raise immediately so the worker exits and ECS restarts it.
                    logger.critical(
                        "Infrastructure error processing message %s: %s — "
                        "exiting for restart (message NOT acknowledged)",
                        msg_id,
                        exc,
                    )
                    raise InfrastructureError(exc) from exc
                attempt += 1
                last_exc = exc
                logger.warning(
                    "Message-level error processing event (attempt %d/%d): %s",
                    attempt,
                    self._max_retries,
                    exc,
                )

        # Exhausted retries on a message-level error — dead-letter and alert.
        # This message has genuinely bad data that won't succeed on retry.
        logger.critical(
            "Dead-lettering message %s after %d retries. Last error: %s",
            msg_id,
            self._max_retries,
            last_exc,
        )
        self._redis.xack(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, msg_id)


# ---------------------------------------------------------------------------
# LLM ruling matching helper
# ---------------------------------------------------------------------------


def _match_ruling(
    llm_result: LLMExtractionResult,
    case_number: str | None,
) -> LLMRulingResult | None:
    """Find the ruling matching the event's case_number, or return the first ruling.

    If the event already has a case_number from the scraper, look for a matching
    ruling in the LLM results. If no match is found, fall back to the first ruling
    (the LLM may have normalized the case number differently).
    """
    if not llm_result.rulings:
        return None
    if case_number:
        matching = [r for r in llm_result.rulings if r.case_number == case_number]
        if matching:
            return matching[0]
    return llm_result.rulings[0]


# ---------------------------------------------------------------------------
# Timestamp parsing helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string, returning None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_date(value: str | datetime | None) -> date | None:
    """Parse a date value from an ISO string or datetime, returning None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        # ISO date string: "2026-03-05" or full ISO datetime
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None
