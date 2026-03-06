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
    MAX_RETRIES       — Per-message retry limit before dead-lettering (default: 3)
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import psycopg.errors

from framework.search.indexer import IndexingConsumer
from framework.search.mapping import TENTATIVE_RULINGS_ALIAS

from .db import (
    insert_document,
    insert_ruling,
    resolve_judge,
    upsert_case,
    upsert_case_judge,
    upsert_court,
)
from .extract import extract_motion_type, extract_outcome

if TYPE_CHECKING:
    from opensearchpy import OpenSearch
    from redis import Redis

logger = logging.getLogger(__name__)

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
    ) -> None:
        self._redis = redis_client
        self._pg_dsn = pg_dsn
        self._max_retries = max_retries
        self._indexer = IndexingConsumer(
            opensearch_client=opensearch_client,
            s3_client=s3_client,
            bucket=archive_bucket,
            index_name=TENTATIVE_RULINGS_ALIAS,
            ensure_index=True,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def health_check(self) -> None:
        """Verify DB connectivity and that required tables exist.

        Raises InfrastructureError if the database is unreachable or the
        schema is not ready. Called on startup before consuming messages.
        """
        required_tables = ("courts", "cases", "documents", "rulings")
        try:
            with psycopg.connect(self._pg_dsn) as conn:
                with conn.cursor() as cur:
                    for table in required_tables:
                        cur.execute(
                            "SELECT 1 FROM information_schema.tables WHERE table_name = %s LIMIT 1",
                            (table,),
                        )
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

    def process_event(self, event_data: dict[str, Any]) -> None:
        """Process a single deserialized event dict.

        Exposed for testing. Raises on unrecoverable errors.
        """
        document_id: str = event_data["document_id"]
        state: str = event_data["state"]
        county: str = event_data["county"]
        court: str = event_data.get("court", "Superior Court")
        case_number: str | None = event_data.get("case_number")
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

        # Outcome and motion_type: prefer event fields, fall back to regex
        outcome: str | None = event_data.get("outcome")
        motion_type: str | None = event_data.get("motion_type")
        if ruling_text and (outcome is None or motion_type is None):
            if outcome is None:
                outcome = extract_outcome(ruling_text)
            if motion_type is None:
                motion_type = extract_motion_type(ruling_text)

        court_name = f"{court}, County of {county}"

        with psycopg.connect(self._pg_dsn) as conn:
            # 1. Ensure court exists
            court_id = upsert_court(conn, state, county, court_name)

            # 2. Ensure case exists — use document_id as synthetic case_number if absent
            effective_case_number = case_number or f"UNKNOWN-{document_id}"
            case_id = upsert_case(conn, effective_case_number, court_id)

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
                    ruling_text=ruling_text,
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

            conn.commit()

        if is_new:
            # 5. Index in OpenSearch — document_id is used as the OS doc id
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
