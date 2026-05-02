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
    HEARTBEAT_INTERVAL — Empty poll cycles between heartbeat log messages (default: 60)
    HEARTBEAT_LOG_LEVEL — Log level for heartbeat messages: "DEBUG" or "INFO" (default: "INFO")
    ENABLE_RULING_FORMATTING — Enable LLM-powered ruling text formatting (default: disabled)
    ENABLE_RULING_SUMMARIZATION — Enable LLM-powered ruling summarization (default: disabled)
    ENABLE_INGESTION_VALIDATION — Enable LLM-based validation gate (default: disabled)
    GITHUB_TOKEN   — GitHub token for auto-filing validation issues (optional)
    GITHUB_REPO    — GitHub repo for validation issues (default: judgemind/judgemind)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import time
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import psycopg.errors

from courts.ca.cc_dept_reassignments import apply_cc_dept_reassignment
from courts.ca.la_dept_judges import (
    fetch_department_judge_mapping,
    lookup_judge_for_department,
)
from framework.enrichment import EnrichmentEngine
from framework.llm_extractor import LlmExtractor
from framework.search.indexer import IndexingConsumer
from framework.search.mapping import TENTATIVE_RULINGS_ALIAS
from validation.deterministic import (
    check_no_cross_case_ruling_text,
    check_no_duplicate_ruling_text,
    run_deterministic_rules,
)
from validation.gate import ValidationResult, insert_validation_result, validate_document
from validation.issue_filer import file_validation_issue

from .db import (
    batch_upsert_parties,
    delete_stale_split_children,
    insert_document_and_ruling,
    resolve_judge,
    resolve_judge_from_department,
    upsert_case_judge,
    upsert_case_returning_title,
    upsert_court,
)
from .department_normalize import normalize_department
from .extract import (
    clean_case_title,
    dedupe_repeated_title,
    extract_case_number,
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
    extract_hearing_date,
    extract_judge_name,
    is_plausible_case_title,
    is_plausible_hearing_date,
    is_probate_decedent_name,
    is_valid_case_number,
    normalize_motion_type,
    normalize_outcome,
    strip_case_number_prefix_suffix,
    strip_trailing_case_number,
    strip_trailing_connectors,
)
from .llm_extract import (
    LLMExtractionResult,
    LLMRulingResult,
    extract_fields_llm,
    extract_text_from_pdf,
    is_pdf_binary,
)
from .llm_providers import create_client as create_llm_client
from .ruling_formatter import format_ruling_text
from .ruling_guards import (
    NON_RULING_PDF_DROPPED_EVENT,
    NON_RULING_PDF_DROPPED_METRIC,
    check_no_orphan_rulings,
    convert_extracted_rulings,
    is_all_null_metadata,
)
from .ruling_summarizer import summarize_ruling
from .text_cleanup import clean_ruling_text

if TYPE_CHECKING:
    from opensearchpy import OpenSearch
    from redis import Redis

logger = logging.getLogger(__name__)

# Extraction-methods tag recorded for every field populated by the LLM
# enrichment stage (``_llm_enrich_fields`` → ``_apply_enrichment_result``).
_ENRICHMENT_METHOD_TAG = "llm_enrichment"

# Diagnostic markers written to extraction_methods when the LLM enrichment
# stage runs but does NOT populate a field.  These distinguish four failure
# modes so we can query derived.rulings.extraction_methods after deploy and
# identify the dominant cause of null outcome/motion_type (#3780).
_ENRICHMENT_EMPTY_TAG = "llm_enrichment_empty"
_ENRICHMENT_NO_EXTRACTION_TAG = "llm_enrichment_no_extraction"
_ENRICHMENT_ERRORED_TAG = "llm_enrichment_errored"
_ENRICHMENT_SKIPPED_NO_TEXT_TAG = "llm_enrichment_skipped_no_text"

# ---------------------------------------------------------------------------
# Markdown ↔ HTML/plain text for multimodal PDF extraction
# ---------------------------------------------------------------------------

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"\*(.+?)\*")


def _strip_markdown(text: str) -> str:
    """Strip markdown formatting to produce plain text."""
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    return text


def _markdown_to_html(text: str) -> str:
    """Convert simple markdown to HTML (bold, italic, paragraphs, lists)."""
    # Escape HTML entities first
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Bold and italic
    text = _MD_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC_RE.sub(r"<em>\1</em>", text)
    # Split into paragraphs on double newlines
    paragraphs = re.split(r"\n{2,}", text)
    html_parts = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # Check if this is a numbered list
        lines = p.split("\n")
        if all(re.match(r"^\d+\.\s", line.strip()) for line in lines if line.strip()):
            items = [re.sub(r"^\d+\.\s*", "", line.strip()) for line in lines if line.strip()]
            html_parts.append("<ol>" + "".join(f"<li>{item}</li>" for item in items) + "</ol>")
        else:
            html_parts.append(f"<p>{p}</p>")
    return "\n".join(html_parts)


def _try_sd_calendar_split(
    event_data: dict[str, Any],
    document_id: str,
    ruling_text: str,
    dispatch: Any,
) -> bool:
    """If *ruling_text* is a San Diego calendar HTML page, deterministically
    split it into per-case rulings and dispatch one synthetic split event per
    case via *dispatch*.  Returns True if the split ran, False otherwise.

    This path bypasses the LLM entirely — calendar HTML pages carry 30+
    fully-structured cases that are cheap to parse with BeautifulSoup.
    Sending them through the LLM produced two severe correctness bugs
    (#2447):

    1. The full 60-100KB HTML was stored as ``ruling_text`` for a single
       "ruling" record, hitting the 50000-char truncation cap.
    2. The LLM's first-case extraction was joined onto a DB row whose
       ``case_number`` came from another case, producing cross-case
       contamination.

    The function first checks whether the content looks like a SD calendar
    page.  If not, it returns False and the caller continues with the
    normal extraction flow.
    """
    # Lazy import to avoid a circular dependency between the worker and
    # the courts package at module load time.
    from courts.ca.sd_calendar import _split_rulings, is_sd_calendar_html

    if not is_sd_calendar_html(ruling_text):
        return False

    split_rulings = _split_rulings(ruling_text)
    if not split_rulings:
        logger.warning(
            "SD calendar HTML detected but splitter returned no rulings — falling through",
            extra={"document_id": document_id},
        )
        return False

    logger.info(
        "SD calendar deterministic split dispatching %d ruling(s)",
        len(split_rulings),
        extra={
            "document_id": document_id,
            "ruling_count": len(split_rulings),
            "scraper_id": event_data.get("scraper_id"),
            "extraction_method": "sd_calendar_deterministic",
        },
    )

    # Import here so we don't create a new top-level dependency.
    from .split_ids import make_split_document_id

    is_multi = len(split_rulings) > 1
    for idx, sr in enumerate(split_rulings):
        split_doc_id = make_split_document_id(document_id, idx) if is_multi else document_id
        hearing_date_value: str | None = None
        if sr.hearing_date is not None:
            hearing_date_value = (
                sr.hearing_date.date().isoformat()
                if isinstance(sr.hearing_date, datetime)
                else str(sr.hearing_date)
            )

        split_event: dict[str, Any] = {
            **event_data,
            "document_id": split_doc_id,
            "_original_document_id": document_id,
            "_split_processed": True,
            "_llm_extracted": True,  # Skip further LLM attempts.
            "_split_index": idx,
            "_split_count": len(split_rulings),
            "ruling_text": sr.ruling_text,
            "ruling_text_html": None,
            "case_number": sr.case_number or event_data.get("case_number"),
            "case_title": sr.case_title or event_data.get("case_title"),
            "judge_name": sr.judge_name or event_data.get("judge_name"),
            "department": sr.department or event_data.get("department"),
            "motion_type": sr.motion_type or event_data.get("motion_type"),
            "outcome": sr.outcome or event_data.get("outcome"),
            "hearing_date": hearing_date_value or event_data.get("hearing_date"),
            "parties": sr.parties if sr.parties else event_data.get("parties", []),
        }
        try:
            dispatch(split_event)
        except Exception as _exc:
            from framework.llm_enrichment import LlmEnrichmentExhaustedError

            if not isinstance(_exc, LlmEnrichmentExhaustedError):
                raise
            logger.critical(
                "per-child enrichment exhausted on SD calendar split — ruling permanently lost",
                extra={
                    "document_id": document_id,
                    "_split_index": idx,
                    "_split_count": len(split_rulings),
                    "case_number": sr.case_number or event_data.get("case_number"),
                    "error": str(_exc),
                },
            )

    return True


def _try_la_html_split(
    event_data: dict[str, Any],
    document_id: str,
    ruling_text: str,
    dispatch: Any,
) -> bool:
    """If *ruling_text* is an LA department-response HTML page, deterministically
    split it into per-case rulings and dispatch one synthetic split event per
    case via *dispatch*.  Returns True if the split ran, False otherwise.

    This path bypasses the LLM entirely — LA department HTML pages have a
    predictable structure (``<b>Case Number:</b>`` blocks inside
    ``<div id="speechSynthesis">``) that is cheap to parse with BeautifulSoup.
    Sending the raw HTML through the LLM produced a correctness bug (#2450):

    * On Word-pasted HTML variants lacking the standard
      ``DEPARTMENT N LAW AND MOTION RULINGS`` header, the LLM failed to
      extract ``case_number``.  The worker then synthesized
      ``UNKNOWN-<uuid>`` as the case_number, producing ~12% UNKNOWN rate
      across ``rebuild-ca-los_angeles`` events.

    The function first checks whether the content looks like an LA HTML
    page via ``is_la_html``.  If not, it returns False and the caller
    continues with the normal extraction flow.

    Mirrors the SD calendar pattern (``_try_sd_calendar_split``, #2447).
    """
    # Lazy import to avoid a circular dependency between the worker and
    # the courts package at module load time.
    from courts.ca.la_tentatives import _split_rulings, is_la_html

    if not is_la_html(ruling_text):
        return False

    split_rulings = _split_rulings(ruling_text)
    if not split_rulings:
        logger.warning(
            "LA HTML detected but splitter returned no rulings — falling through",
            extra={"document_id": document_id},
        )
        return False

    logger.info(
        "LA HTML deterministic split dispatching %d ruling(s)",
        len(split_rulings),
        extra={
            "document_id": document_id,
            "ruling_count": len(split_rulings),
            "scraper_id": event_data.get("scraper_id"),
            "extraction_method": "la_html_deterministic",
        },
    )

    # Import here so we don't create a new top-level dependency.
    from .split_ids import make_split_document_id

    is_multi = len(split_rulings) > 1
    for idx, sr in enumerate(split_rulings):
        split_doc_id = make_split_document_id(document_id, idx) if is_multi else document_id
        hearing_date_value: str | None = None
        if sr.hearing_date is not None:
            hearing_date_value = (
                sr.hearing_date.date().isoformat()
                if isinstance(sr.hearing_date, datetime)
                else str(sr.hearing_date)
            )

        # LASplitRuling has no judge_name field — preserve whatever the scraper
        # provided in the original event (LA judge_name is resolved from the
        # dept-to-judge directory snapshot).
        split_event: dict[str, Any] = {
            **event_data,
            "document_id": split_doc_id,
            "_original_document_id": document_id,
            "_split_processed": True,
            "_llm_extracted": True,  # Skip further LLM attempts.
            "_split_index": idx,
            "_split_count": len(split_rulings),
            "ruling_text": sr.ruling_text,
            "ruling_text_html": sr.ruling_text_html,
            "case_number": sr.case_number or event_data.get("case_number"),
            "case_title": sr.case_title or event_data.get("case_title"),
            "department": sr.department or event_data.get("department"),
            "motion_type": sr.motion_type or event_data.get("motion_type"),
            "outcome": sr.outcome or event_data.get("outcome"),
            "hearing_date": hearing_date_value or event_data.get("hearing_date"),
            "parties": sr.parties if sr.parties else event_data.get("parties", []),
        }
        try:
            dispatch(split_event)
        except Exception as _exc:
            from framework.llm_enrichment import LlmEnrichmentExhaustedError

            if not isinstance(_exc, LlmEnrichmentExhaustedError):
                raise
            logger.critical(
                "per-child enrichment exhausted on LA HTML split — ruling permanently lost",
                extra={
                    "document_id": document_id,
                    "_split_index": idx,
                    "_split_count": len(split_rulings),
                    "case_number": sr.case_number or event_data.get("case_number"),
                    "error": str(_exc),
                },
            )

    return True


def _try_fresno_pdf_split(
    event_data: dict[str, Any],
    document_id: str,
    ruling_text: str,
    dispatch: Any,
) -> bool:
    """If *ruling_text* is a Fresno multi-ruling PDF, deterministically split
    it into per-case rulings and dispatch one synthetic split event per case
    via *dispatch*.  Returns True if the split ran, False otherwise.

    This path bypasses the LLM entirely — Fresno PDFs contain numbered ruling
    entries (``(20) Tentative Ruling``, ``(21) Tentative Ruling``, etc.) that
    are cheap to parse with the existing regex-based ``_split_rulings``
    function in ``courts.ca.fresno_tentatives``.  Sending multi-ruling PDFs
    through the LLM previously produced cross-case contamination: every ruling
    was mis-attributed to the case referenced in the page-1 preamble.

    Detection gate: only triggers when event is Fresno county **and** content
    format is ``"pdf"`` — avoids false-positive matches on other courts.

    When ``_split_rulings`` returns ``[]`` (no numbered entries found) or a
    1-element list (single-ruling PDF), this function returns ``False`` so the
    existing LLM path handles it (#3599, AC4).  The deterministic regex
    extraction does not reliably populate ``outcome``/``motion_type`` for
    single-ruling PDFs; falling through to ``_llm_enrich_fields`` restores
    pre-#3553 behaviour.

    Mirrors the SD/LA pattern (``_try_sd_calendar_split`` #2447,
    ``_try_la_html_split`` #2450).
    """
    county = event_data.get("county") or ""
    if county.upper() != "FRESNO":
        return False
    if event_data.get("content_format") != "pdf":
        return False

    # Lazy import to avoid a circular dependency between the worker and
    # the courts package at module load time.
    from courts.ca.fresno_tentatives import _split_rulings

    split_rulings = _split_rulings(ruling_text)
    if not split_rulings:
        # No numbered entries found — fall through to LLM.
        logger.info(
            "fresno_split_fall_through",
            extra={
                "document_id": document_id,
                "reason": "no_numbered_entries",
                "raw_len": len(split_rulings),
                "scraper_id": event_data.get("scraper_id"),
                "extraction_method": "fresno_pdf_deterministic",
            },
        )
        return False
    if len(split_rulings) == 1:
        # Single-ruling PDF — the deterministic regex extraction does not
        # reliably populate outcome/motion_type. Fall through to the LLM
        # path so _llm_enrich_fields fills those fields (matches pre-#3553
        # behavior; AC4 of #3534, fix for #3599).
        logger.info(
            "fresno_split_fall_through",
            extra={
                "document_id": document_id,
                "reason": "single_ruling_pdf",
                "raw_len": len(split_rulings),
                "scraper_id": event_data.get("scraper_id"),
                "extraction_method": "fresno_pdf_deterministic",
            },
        )
        return False

    logger.info(
        "Fresno PDF deterministic split dispatching %d ruling(s)",
        len(split_rulings),
        extra={
            "document_id": document_id,
            "ruling_count": len(split_rulings),
            "scraper_id": event_data.get("scraper_id"),
            "extraction_method": "fresno_pdf_deterministic",
        },
    )

    from .split_ids import make_split_document_id

    # At this point len(split_rulings) > 1 — always generate split document IDs.
    for idx, sr in enumerate(split_rulings):
        split_doc_id = make_split_document_id(document_id, idx)
        hearing_date_value: str | None = None
        if sr.hearing_date is not None:
            hearing_date_value = (
                sr.hearing_date.date().isoformat()
                if isinstance(sr.hearing_date, datetime)
                else str(sr.hearing_date)
            )

        split_event: dict[str, Any] = {
            **event_data,
            "document_id": split_doc_id,
            "_original_document_id": document_id,
            "_split_processed": True,
            "_llm_extracted": True,  # Skip further LLM attempts.
            "_split_index": idx,
            "_split_count": len(split_rulings),
            "ruling_text": sr.ruling_text,
            "ruling_text_html": None,
            "case_number": sr.case_number or event_data.get("case_number"),
            "case_title": sr.case_title or event_data.get("case_title"),
            "department": sr.department or event_data.get("department"),
            "motion_type": sr.motion_type or event_data.get("motion_type"),
            "outcome": sr.outcome or event_data.get("outcome"),
            "hearing_date": hearing_date_value or event_data.get("hearing_date"),
        }
        try:
            dispatch(split_event)
        except Exception as _exc:
            from framework.llm_enrichment import LlmEnrichmentExhaustedError

            if not isinstance(_exc, LlmEnrichmentExhaustedError):
                raise
            logger.critical(
                "per-child enrichment exhausted on Fresno PDF split — ruling permanently lost",
                extra={
                    "document_id": document_id,
                    "_split_index": idx,
                    "_split_count": len(split_rulings),
                    "case_number": sr.case_number or event_data.get("case_number"),
                    "error": str(_exc),
                },
            )

    return True


# Fields that LLM extraction can populate when missing from the scraper event.
EXTRACTABLE_FIELDS = (
    "hearing_date",
    "outcome",
    "motion_type",
    "case_number",
    "case_title",
    "case_type",
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

# psycopg error classes that indicate deterministic schema/data-shape mismatches.
# These errors mean the data violates a DB constraint and will NEVER succeed on
# retry — retrying just wastes LLM API calls (the extraction succeeds but the
# insert fails every time).  Dead-letter immediately on first attempt.
_SCHEMA_CONSTRAINT_ERRORS: tuple[type[Exception], ...] = (
    psycopg.errors.StringDataRightTruncation,  # value too long for type character(N)
    psycopg.errors.NumericValueOutOfRange,  # numeric value out of range
    psycopg.errors.CheckViolation,  # CHECK constraint failed
    psycopg.errors.NotNullViolation,  # NOT NULL constraint violated by data
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


def is_schema_constraint_error(exc: Exception) -> bool:
    """Return True if the exception is a deterministic schema constraint violation.

    Schema constraint errors indicate a data/schema mismatch that will never
    succeed on retry (e.g. value too long, numeric overflow, CHECK violation).
    These should be dead-lettered immediately on first attempt — retrying wastes
    resources (especially LLM API calls that succeed but whose results can't be
    inserted into the DB).
    """
    return isinstance(exc, _SCHEMA_CONSTRAINT_ERRORS)


STREAM_DOCUMENT_CAPTURED = "document.captured"
CONSUMER_GROUP = "ingestion-workers"
# Unique per process so multiple workers can share the group without collision
CONSUMER_NAME = f"ingestion-{socket.gethostname()}-{os.getpid()}"

DEFAULT_BATCH_SIZE = 10
DEFAULT_BLOCK_MS = 5000
DEFAULT_MAX_RETRIES = 3
# Number of consecutive empty poll cycles between heartbeat log messages.
# At the default block timeout of 5 seconds, 60 cycles ≈ 5 minutes.
DEFAULT_HEARTBEAT_INTERVAL = 60
# Consumers idle for longer than this (in milliseconds) with 0 pending messages
# are considered stale and eligible for cleanup.  1 hour = 3,600,000 ms.
STALE_CONSUMER_IDLE_MS = 3_600_000

# Minimum idle time (milliseconds) for a pending message to be eligible for
# reclamation via XAUTOCLAIM.  Messages stuck longer than this are assumed
# abandoned by a crashed consumer.  30 seconds is long enough that a healthy
# consumer processing a slow message won't lose it.
PENDING_RECLAIM_MIN_IDLE_MS = 30_000

# How often (in empty-poll cycles) to attempt PEL reclamation during the
# main loop.  At the default 5 s block, 12 cycles ≈ 1 minute.
PENDING_RECLAIM_INTERVAL = 12


class IngestionWorker:
    """Consumes document.captured events from Redis Streams.

    For each event:
      1. Upserts court, case, and document rows in Postgres.
      2. Inserts a ruling row in Postgres (idempotent).
      3. Indexes the document in OpenSearch via IndexingConsumer.
      4. Acknowledges the message (XACK) only after both writes succeed.

    Error handling distinguishes three categories:

    **Infrastructure errors** (DB down, missing tables, connection failures):
      Raised as InfrastructureError without acknowledging the message. The
      worker process exits with non-zero status so ECS restarts it. Messages
      remain in the stream and will be reprocessed after recovery.

    **Schema constraint errors** (value too long, numeric overflow, CHECK/NOT NULL
    violations): Dead-lettered immediately on first attempt — no retries.
      These are deterministic data/schema mismatches that will never succeed
      on retry. Retrying wastes LLM API calls (extraction succeeds but DB
      insert fails every time).

    **Message-level errors** (bad data, validation failures, transient issues):
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
        llm_timeout: float | None = 60.0,
    ) -> None:
        self._redis = redis_client
        self._pg_dsn = pg_dsn
        self._max_retries = max_retries
        self._conn: psycopg.Connection | None = None
        self._s3_client = s3_client
        self._archive_bucket = archive_bucket
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
        self._llm_timeout: float | None = llm_timeout
        self._llm_client: object | None = llm_client
        if self._llm_client is None:
            self._llm_client = create_llm_client(provider=self._llm_provider)
        if self._llm_client is None:
            logger.warning("LLM API key not set — per-field LLM extraction disabled")

        # Heartbeat tracking — periodic log when idle to prove the worker is alive.
        heartbeat_interval_env = os.environ.get("HEARTBEAT_INTERVAL")
        self._heartbeat_interval: int = (
            int(heartbeat_interval_env) if heartbeat_interval_env else DEFAULT_HEARTBEAT_INTERVAL
        )
        heartbeat_level_env = os.environ.get("HEARTBEAT_LOG_LEVEL", "INFO").upper()
        self._heartbeat_log_level: int = getattr(logging, heartbeat_level_env, logging.INFO)
        self._empty_polls: int = 0
        self._last_event_time: float = time.monotonic()

        # Ruling formatting — uses a dedicated Anthropic client (always Haiku),
        # separate from the field extraction LLM client which may use Google.
        self._formatting_enabled = os.environ.get("ENABLE_RULING_FORMATTING", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self._formatting_client: object | None = None
        if self._formatting_enabled:
            self._formatting_client = create_llm_client(provider="anthropic")
            if self._formatting_client is None:
                logger.warning(
                    "Ruling formatting enabled but ANTHROPIC_API_KEY not set — "
                    "formatting will use <pre> fallback"
                )

        # Ruling summarization — uses a dedicated Anthropic client (always Haiku),
        # separate from the field extraction LLM client which may use Google.
        self._summarization_enabled = os.environ.get("ENABLE_RULING_SUMMARIZATION", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self._summarization_client: object | None = None
        if self._summarization_enabled:
            self._summarization_client = create_llm_client(provider="anthropic")
            if self._summarization_client is None:
                logger.warning(
                    "Ruling summarization enabled but ANTHROPIC_API_KEY not set — "
                    "summarization disabled"
                )

        # Ingestion validation gate — LLM-based quality checks between
        # enrichment and DB write. Uses a dedicated Anthropic client (always
        # Haiku), separate from extraction and formatting.
        self._validation_enabled = os.environ.get("ENABLE_INGESTION_VALIDATION", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self._validation_client: object | None = None
        if self._validation_enabled:
            self._validation_client = create_llm_client(provider="anthropic")
            if self._validation_client is None:
                logger.warning(
                    "Ingestion validation enabled but ANTHROPIC_API_KEY not set — "
                    "validation disabled"
                )
                self._validation_enabled = False

        # LLM enrichment — uses enrich_ruling() for motion_type, outcome,
        # case_title, and parties. Always enabled (regex fallback removed
        # in #2178). Uses the same LLM provider/client as per-field
        # extraction for connection reuse.
        self._llm_enrichment_enabled: bool = True
        # Reuse the existing LLM client for enrichment to avoid creating a
        # separate connection. The enrichment module accepts a client kwarg.
        self._enrichment_client: object | None = self._llm_client
        if self._enrichment_client is None:
            logger.warning(
                "LLM enrichment enabled but no LLM client available — "
                "enrichment fields may be missing"
            )

        # Reusable httpx client for GitHub issue filing (validation gate).
        # Created lazily on first use; None means "not yet created".
        self._github_client: object | None = None

        # Framework-level LLM extractor — lazy-initialized on first use.
        # Uses the Anthropic API (always Haiku) and is separate from the
        # per-field LLM client above.
        self._framework_extractor: LlmExtractor | None = None

        # County-specific LLM extractors — keyed by (provider, model) tuple.
        # Lazy-initialized on first use per county config.
        self._county_extractors: dict[tuple[str, str], LlmExtractor] = {}

        # Multimodal LLM extractor for PDF page-image extraction (#1590).
        # Uses Google Gemini Flash Lite for cost-effective per-page extraction.
        # Lazy-initialized on first use; None means "not yet created".
        #
        # Legacy single-slot cache (preserved for backward compatibility with
        # tests that directly assign ``worker._multimodal_extractor``).  Used
        # only when ``_get_multimodal_extractor()`` is called with the default
        # ``max_output_tokens=None``.
        self._multimodal_extractor: LlmExtractor | None = None

        # Per-token-limit cache for multimodal extractors (#2369).  Counties
        # with different multimodal ``max_output_tokens`` configurations get
        # separate ``LlmExtractor`` instances so that Orange County's
        # 32,768-token limit does not bleed into extractors created with
        # different limits.  Key is the ``max_output_tokens`` int (or ``0``
        # when unspecified).
        self._multimodal_extractors: dict[int, LlmExtractor] = {}

        # LA County department-to-judge mapping — lazy-loaded on first LA event.
        # None means "not yet fetched"; empty dict means "fetch failed or empty".
        self._la_dept_map: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _get_framework_extractor(self) -> LlmExtractor | None:
        """Return the framework-level LlmExtractor, creating lazily on first call.

        Returns None if the Anthropic API key is not available (the county
        will fall back to regex extraction with a warning).
        """
        if self._framework_extractor is None:
            try:
                self._framework_extractor = LlmExtractor()
                logger.info("Initialized framework LlmExtractor for LLM extraction path")
            except Exception as exc:
                logger.warning(
                    "Failed to initialize LlmExtractor — LLM extraction path unavailable: %s",
                    exc,
                )
        return self._framework_extractor

    def _get_multimodal_extractor(
        self,
        max_output_tokens: int | None = None,
    ) -> LlmExtractor | None:
        """Return the multimodal LlmExtractor for PDF extraction, creating lazily.

        Uses Google Gemini Flash Lite for cost-effective per-page image
        extraction.  Returns None if the Google API key is not available.

        Parameters
        ----------
        max_output_tokens : int | None
            Optional per-call response token cap.  When the county config
            specifies a larger limit (e.g. Orange County at 32,768 tokens),
            the caller passes it through so dense multi-case pages do not
            truncate the LLM JSON response (#2369).  When ``None``, the
            ``LlmExtractor`` default of 4,096 is used.

        Notes
        -----
        Extractors are cached per ``max_output_tokens`` value, so two
        counties with different multimodal token limits get separate
        ``LlmExtractor`` instances.  For backward compatibility with tests
        that pre-populate ``self._multimodal_extractor``, that legacy slot
        is returned whenever it is already set — regardless of the requested
        ``max_output_tokens``.  Production code paths always call with
        ``max_output_tokens`` set, so this fallback only matters in tests.
        """
        # Backward-compat: if the legacy single-slot cache is already
        # populated (e.g. by a test or by a prior call without a token
        # limit), return it without reinitializing.
        if self._multimodal_extractor is not None:
            return self._multimodal_extractor

        if max_output_tokens is None:
            # No county-specific token limit — use the legacy single-slot
            # cache, created with the LlmExtractor default of 4,096 tokens.
            try:
                self._multimodal_extractor = LlmExtractor(
                    provider="google",
                    model="gemini-2.5-flash-lite",
                )
                logger.info("Initialized multimodal LlmExtractor (Google Flash Lite)")
            except Exception as exc:
                logger.warning(
                    "Failed to initialize multimodal LlmExtractor — PDF extraction unavailable: %s",
                    exc,
                )
            return self._multimodal_extractor

        # Per-token-limit cache (#2369).
        cache_key = max_output_tokens
        if cache_key not in self._multimodal_extractors:
            try:
                extractor = LlmExtractor(
                    provider="google",
                    model="gemini-2.5-flash-lite",
                    max_output_tokens=max_output_tokens,
                )
                self._multimodal_extractors[cache_key] = extractor
                logger.info(
                    "Initialized multimodal LlmExtractor (Google Flash Lite)",
                    extra={"max_output_tokens": max_output_tokens},
                )
            except Exception as exc:
                logger.warning(
                    "Failed to initialize multimodal LlmExtractor — PDF extraction unavailable: %s",
                    exc,
                    extra={"max_output_tokens": max_output_tokens},
                )
                return None
        return self._multimodal_extractors.get(cache_key)

    def _fetch_raw_pdf_from_s3(
        self,
        s3_key: str | None,
        s3_bucket: str | None,
        document_id: str,
    ) -> bytes | None:
        """Download raw PDF bytes from S3 for multimodal extraction (#2312).

        When a scraper pre-extracts PDF text in ``parse_document()`` (e.g.
        Santa Clara), the event's ``ruling_text`` is pdfplumber-extracted text
        rather than raw binary.  This means ``is_pdf_binary()`` returns False
        and ``raw_pdf_bytes`` is None, so multimodal extraction cannot run.

        This method downloads the original PDF from S3 using the event's
        ``s3_key`` so the multimodal path can send page images to the LLM.

        Returns the raw PDF bytes, or ``None`` if the download fails or the
        S3 key is not available.
        """
        if not s3_key:
            logger.debug(
                "Cannot fetch raw PDF from S3 — no s3_key in event",
                extra={"document_id": document_id},
            )
            return None

        bucket = s3_bucket or self._archive_bucket
        try:
            response = self._s3_client.get_object(Bucket=bucket, Key=s3_key)
            raw_bytes: bytes = response["Body"].read()
            logger.info(
                "Fetched raw PDF from S3 for multimodal extraction",
                extra={
                    "document_id": document_id,
                    "s3_key": s3_key,
                    "pdf_size": len(raw_bytes),
                },
            )
            return raw_bytes
        except Exception as exc:
            logger.warning(
                "Failed to fetch raw PDF from S3",
                extra={
                    "document_id": document_id,
                    "s3_key": s3_key,
                    "error": str(exc),
                },
            )
            return None

    def _get_county_extractor(
        self,
        provider: str,
        model: str | None,
        max_output_tokens: int | None,
        max_chars_per_chunk: int | None = None,
    ) -> LlmExtractor | None:
        """Return a county-specific LlmExtractor, creating lazily.

        Caches by (provider, model, max_chars_per_chunk, max_output_tokens)
        tuple so repeated calls for the same county config reuse the same
        extractor instance.  The max_output_tokens dimension was added in
        #2355 to prevent counties with different token limits from sharing
        an extractor.
        """
        cache_key = (provider, model or "", max_chars_per_chunk or 0, max_output_tokens or 0)
        if cache_key not in self._county_extractors:
            try:
                kwargs: dict[str, object] = {"provider": provider}
                if model:
                    kwargs["model"] = model
                if max_output_tokens:
                    kwargs["max_output_tokens"] = max_output_tokens
                if max_chars_per_chunk:
                    kwargs["max_chars_per_chunk"] = max_chars_per_chunk
                extractor = LlmExtractor(**kwargs)
                self._county_extractors[cache_key] = extractor
                logger.info(
                    "Initialized county-specific LlmExtractor",
                    extra={"provider": provider, "model": model},
                )
            except Exception as exc:
                logger.warning(
                    "Failed to initialize county LlmExtractor: %s",
                    exc,
                    extra={"provider": provider, "model": model},
                )
                return None
        return self._county_extractors.get(cache_key)

    def _llm_enrich_fields(
        self,
        ruling_text: str,
        document_id: str,
    ) -> tuple[object | None, str]:
        """Run LLM enrichment on ruling text to extract structured fields.

        Calls ``enrich_ruling()`` from ``framework.llm_enrichment`` to extract
        ``motion_type``, ``outcome``, ``case_title``, and ``parties`` from the
        ruling text in a single LLM call.

        The import is deferred (lazy) to avoid a circular import between
        ``framework`` and ``ingestion`` packages.

        Parameters
        ----------
        ruling_text : str
            The plain text of the ruling (already transcribed).
        document_id : str
            The document ID for logging.

        Returns
        -------
        tuple[LlmEnrichmentResult | None, str]
            ``(result, status_tag)`` where ``status_tag`` is one of:

            * ``"ok"`` — enrichment ran and returned a non-None result.
            * ``"empty"`` — enrichment ran but the LLM returned all-None
              fields (``enrich_ruling_with_retry`` returned ``None``).
            * ``"errored"`` — ``LlmEnrichmentExhaustedError`` was raised;
              ``result`` is ``None``.  The caller decides whether to re-raise
              (single-event path) or absorb (split-loop path).
            * ``"skipped_no_text"`` — ``ruling_text`` was empty/whitespace;
              enrichment was never attempted.
            * ``"skipped_disabled"`` — enrichment is disabled or the client
              is unavailable; enrichment was never attempted.
        """
        if not self._llm_enrichment_enabled:
            return None, "skipped_disabled"

        if not self._enrichment_client:
            return None, "skipped_disabled"

        if not ruling_text or not ruling_text.strip():
            return None, "skipped_no_text"

        from framework.llm_enrichment import LlmEnrichmentExhaustedError, enrich_ruling_with_retry

        t0 = time.monotonic()
        try:
            result = enrich_ruling_with_retry(
                ruling_text,
                provider=self._llm_provider or "google",
                model=self._llm_model,
                client=self._enrichment_client,
            )
        except LlmEnrichmentExhaustedError:
            latency_ms = round((time.monotonic() - t0) * 1000)
            logger.warning(
                "LLM enrichment exhausted all retries",
                extra={
                    "document_id": document_id,
                    "enrichment_latency_ms": latency_ms,
                },
            )
            return None, "errored"
        latency_ms = round((time.monotonic() - t0) * 1000)

        if result is None:
            # LLM responded but extracted nothing (all-None fields, e.g. text
            # too short).  Not a transient failure — return None without retry.
            logger.info(
                "LLM enrichment completed with empty result",
                extra={
                    "document_id": document_id,
                    "enrichment_latency_ms": latency_ms,
                },
            )
            return None, "empty"

        logger.info(
            "LLM enrichment completed",
            extra={
                "document_id": document_id,
                "enrichment_latency_ms": latency_ms,
                "has_data": True,
                "case_title": result.case_title[:80] if result.case_title else None,
                "motion_type": result.motion_type,
                "outcome": result.outcome,
                "plaintiff_count": len(result.parties.plaintiffs),
                "defendant_count": len(result.parties.defendants),
            },
        )

        return result, "ok"

    @staticmethod
    def _apply_enrichment_result(
        enrichment_result: Any,
        *,
        status_tag: str,
        outcome: str | None,
        motion_type: str | None,
        case_title: str | None,
        parties_data: list[dict[str, str]],
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        list[dict[str, str]],
        dict[str, str],
    ]:
        """Merge an LLM enrichment result into the current field values.

        Applies ``enrichment_result`` fields (``outcome``, ``motion_type``,
        ``case_title``, ``parties``) to the current values, preserving the
        precedence rule that existing values are never overwritten — the
        enrichment only fills fields that are currently missing.  Also
        converts :class:`EnrichmentParties` (plaintiffs/defendants lists)
        to the ``list[dict]`` format used by the rest of the pipeline.

        When enrichment ran but a field is still ``None`` after the merge,
        a diagnostic marker is written to the returned methods dict.  This
        enables post-deploy querying of ``derived.rulings.extraction_methods``
        to identify the dominant failure mode for null-outcome rows (#3780).

        Parameters
        ----------
        enrichment_result :
            An :class:`LlmEnrichmentResult` (or ``None``).  The parameter is
            typed loosely so callers do not need to import the class at the
            top of the module (avoids a circular-import concern mirroring
            the lazy import in :meth:`_llm_enrich_fields`).  When ``None``,
            all current values are returned unchanged and the methods dict
            is empty.
        status_tag :
            The status tag returned by :meth:`_llm_enrich_fields`.  Controls
            which diagnostic marker is written for fields that remain ``None``
            after the merge:

            * ``"empty"`` → ``_ENRICHMENT_EMPTY_TAG``
            * ``"errored"`` → ``_ENRICHMENT_ERRORED_TAG``
            * ``"skipped_no_text"`` → ``_ENRICHMENT_SKIPPED_NO_TEXT_TAG``
            * ``"ok"`` and field still ``None`` → ``_ENRICHMENT_NO_EXTRACTION_TAG``
            * ``"skipped_disabled"`` → no diagnostic marker (enrichment intentionally off)
        outcome, motion_type, case_title, parties_data :
            Current values to merge into.

        Returns
        -------
        tuple
            ``(outcome, motion_type, case_title, parties_data,
            extraction_methods_entries)``.  ``extraction_methods_entries``
            contains one entry per field the helper actually populated
            (``"llm_enrichment"``) or a diagnostic marker for fields that
            the enrichment stage was expected to fill but left as ``None``.
            Fields already populated before enrichment ran are NOT present.

        Notes
        -----
        A parallel implementation exists in ``scripts/reingest_from_s3.py``
        that operates on a dict instead of individual variables.  Any
        schema change (new field, renamed field) must be applied in both
        places.
        """
        methods: dict[str, str] = {}
        method_tag = _ENRICHMENT_METHOD_TAG

        # Map status_tag to the diagnostic marker for fields that remain None.
        # "skipped_disabled" means enrichment was intentionally off — no marker.
        _failure_marker: str | None
        if status_tag == "empty":
            _failure_marker = _ENRICHMENT_EMPTY_TAG
        elif status_tag == "errored":
            _failure_marker = _ENRICHMENT_ERRORED_TAG
        elif status_tag == "skipped_no_text":
            _failure_marker = _ENRICHMENT_SKIPPED_NO_TEXT_TAG
        elif status_tag == "ok":
            _failure_marker = _ENRICHMENT_NO_EXTRACTION_TAG
        else:
            # "skipped_disabled" or any future tag — no diagnostic marker.
            _failure_marker = None

        # Enrichment did not run or was disabled — nothing to merge.
        if enrichment_result is None and status_tag == "skipped_disabled":
            return outcome, motion_type, case_title, parties_data, methods

        if enrichment_result is None:
            # Enrichment ran but produced no result (empty / errored /
            # skipped_no_text).  Record diagnostic markers for each field
            # that is still None and that the enrichment gate was expected
            # to fill.
            if _failure_marker is not None:
                if outcome is None:
                    methods["outcome"] = _failure_marker
                if motion_type is None:
                    methods["motion_type"] = _failure_marker
                if not case_title:
                    methods["case_title"] = _failure_marker
                if not parties_data:
                    methods["parties"] = _failure_marker
            return outcome, motion_type, case_title, parties_data, methods

        if outcome is None and enrichment_result.outcome is not None:
            outcome = enrichment_result.outcome
            methods["outcome"] = method_tag
        elif outcome is None and _failure_marker is not None:
            # Enrichment ran (ok) but did not return this field.
            methods["outcome"] = _failure_marker

        if motion_type is None and enrichment_result.motion_type is not None:
            motion_type = enrichment_result.motion_type
            methods["motion_type"] = method_tag
        elif motion_type is None and _failure_marker is not None:
            methods["motion_type"] = _failure_marker

        if not case_title and enrichment_result.case_title is not None:
            case_title = enrichment_result.case_title
            methods["case_title"] = method_tag
        elif not case_title and _failure_marker is not None:
            methods["case_title"] = _failure_marker

        if not parties_data and (
            enrichment_result.parties.plaintiffs or enrichment_result.parties.defendants
        ):
            # Convert from EnrichmentParties to the parties_data
            # list[dict] format used by the rest of the pipeline.
            parties_data = []
            for name in enrichment_result.parties.plaintiffs:
                parties_data.append({"name": name, "role": "plaintiff"})
            for name in enrichment_result.parties.defendants:
                parties_data.append({"name": name, "role": "defendant"})
            methods["parties"] = method_tag
        elif not parties_data and _failure_marker is not None:
            methods["parties"] = _failure_marker

        return outcome, motion_type, case_title, parties_data, methods

    def _file_validation_issue(
        self,
        *,
        result: str,
        reason: str,
        county: str,
        case_number: str | None,
        document_id: str,
        s3_key: str | None,
        ruling_text_excerpt: str | None,
        extracted_fields: dict[str, Any] | None,
    ) -> None:
        """File a GitHub issue for a validation flag/fail result.

        Creates the httpx client lazily on first use for connection reuse.
        Never raises — issue filing failures are logged but do not block
        ingestion.
        """
        import httpx as _httpx

        if self._github_client is None:
            self._github_client = _httpx.Client(timeout=30.0)

        try:
            file_validation_issue(
                result=result,
                reason=reason,
                county=county,
                case_number=case_number,
                document_id=document_id,
                s3_key=s3_key,
                ruling_text_excerpt=ruling_text_excerpt,
                extracted_fields=extracted_fields,
                http_client=self._github_client,
            )
        except Exception as exc:
            logger.warning(
                "Failed to file validation issue: %s",
                exc,
                extra={"document_id": document_id, "result": result},
            )

    def _get_la_dept_map(self) -> dict[str, str]:
        """Return the LA County department-to-judge mapping, fetching lazily on first call.

        The mapping is cached for the lifetime of the worker instance. If the
        fetch fails (network error, page changed), returns an empty dict and
        logs a warning — the worker continues without dept-based judge lookup.
        """
        if self._la_dept_map is None:
            try:
                self._la_dept_map = fetch_department_judge_mapping()
                logger.info(
                    "Loaded LA department-to-judge mapping",
                    extra={"department_count": len(self._la_dept_map)},
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch LA department-to-judge mapping — "
                    "dept-based judge lookup disabled: %s",
                    exc,
                )
                self._la_dept_map = {}
        return self._la_dept_map

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
        """Close the persistent Postgres connection and httpx client if open.

        Safe to call multiple times. Called automatically when the worker
        shuts down via ``run()``.
        """
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            self._conn = None
        if self._github_client is not None:
            import httpx as _httpx

            if isinstance(self._github_client, _httpx.Client):
                self._github_client.close()
            self._github_client = None

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
        self._cleanup_stale_consumers()

        # Reclaim any messages left pending by crashed consumers before
        # starting the main loop.  This is critical: after an infrastructure
        # error (e.g. missing DB column) is fixed, the messages that caused
        # crash-loops are stuck in the PEL because XREADGROUP '>' only
        # delivers new messages.  See #1044.
        reclaimed = self._reclaim_pending_messages()
        if reclaimed:
            logger.info("Startup PEL reclamation processed %d message(s)", reclaimed)

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

                    # Periodically check for pending messages that may have
                    # been abandoned by other consumers that crashed.
                    if self._empty_polls > 0 and self._empty_polls % PENDING_RECLAIM_INTERVAL == 0:
                        self._reclaim_pending_messages()
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
        scraper_ruling_text_html: str | None = event_data.get("ruling_text_html")
        content_format: str = event_data.get("content_format", "html")
        content_hash: str = event_data.get("content_hash", "")
        s3_key: str | None = event_data.get("s3_key")
        s3_bucket: str | None = event_data.get("s3_bucket")
        source_url: str = event_data.get("source_url", "")
        scraper_id: str = event_data.get("scraper_id", "")

        # Pipeline branch — non-ruling scrapers (#3688).
        # The ca-governor-appointments scraper emits judge-bio press releases
        # that share the CapturedDocument shape but are NOT rulings. Without
        # this branch the LLM extractor produces UNKNOWN-* case_numbers,
        # empty case_titles, and a 'Governor / Statewide' court row that
        # pollutes derived.rulings (178 rows as of 2026-04-28). Raw HTML is
        # preserved in S3 + staging.captures so #2144 can recover appointee
        # data via courts.ca.governor_appointments.parse_appointees().
        if scraper_id == "ca-governor-appointments":
            logger.info(
                "Routing governor-appointment document to bio path (skipping ruling extraction)",
                extra={
                    "document_id": document_id,
                    "scraper_id": scraper_id,
                    "telemetry_event": "governor_appointment_routed_to_bio_path",
                },
            )
            return

        # PDF preprocessing: if ruling_text is raw PDF binary, extract text first.
        # This handles documents where the scraper stored raw PDF bytes instead of
        # extracted text (e.g. Riverside, Orange county PDFs).
        #
        # Preserve the raw PDF bytes before text extraction so the multimodal
        # extraction path (per-page image LLM calls) can use them (#1590).
        raw_pdf_bytes: bytes | None = None
        if ruling_text and content_format == "pdf" and is_pdf_binary(ruling_text):
            raw_pdf_bytes = (
                ruling_text.encode("latin-1") if isinstance(ruling_text, str) else ruling_text
            )
            extracted_pdf_text = extract_text_from_pdf(ruling_text)
            if extracted_pdf_text:
                logger.info(
                    "Extracted text from raw PDF binary",
                    extra={
                        "document_id": document_id,
                        "original_length": len(ruling_text),
                        "extracted_length": len(extracted_pdf_text),
                    },
                )
                ruling_text = extracted_pdf_text
            else:
                logger.warning(
                    "PDF text extraction returned no text — LLM may fail",
                    extra={"document_id": document_id},
                )
                # When extraction returned empty (not None), normalize to
                # empty string so the downstream "no ruling text" warning
                # fires correctly (#1335).  When extraction returned None
                # (complete failure), keep the original raw binary so
                # existing behavior is preserved.
                if extracted_pdf_text is not None:
                    ruling_text = ""

        # ------------------------------------------------------------------
        # S3 PDF download for multimodal extraction (#2312)
        # ------------------------------------------------------------------
        # When a scraper pre-extracts PDF text in parse_document() (e.g.
        # Santa Clara), ruling_text is pdfplumber text, not raw binary.
        # is_pdf_binary() returns False, so raw_pdf_bytes stays None.
        # For MULTIMODAL counties, download the original PDF from S3 so
        # the multimodal path can send page images to the LLM.
        if raw_pdf_bytes is None and content_format == "pdf" and s3_key:
            from framework.extraction_config import (
                ExtractionMethod,
                get_county_extraction_config,
            )

            county_config = get_county_extraction_config(state, county, scraper_id=scraper_id)
            needs_multimodal = (
                county_config is not None and county_config.method == ExtractionMethod.MULTIMODAL
            ) or county_config is None  # unconfigured counties default to multimodal
            if needs_multimodal:
                raw_pdf_bytes = self._fetch_raw_pdf_from_s3(s3_key, s3_bucket, document_id)

        # ------------------------------------------------------------------
        # Document splitting — detect multi-case calendar pages (#1475)
        # ------------------------------------------------------------------
        # Update ruling_text in event_data so the LLM extractor sees
        # PDF-extracted text.
        if ruling_text != event_data.get("ruling_text"):
            event_data = {**event_data, "ruling_text": ruling_text}

        # LLM extraction is the sole path for document splitting and
        # structured field extraction.  The legacy regex splitter framework
        # was removed in #1475.
        if not event_data.get("_split_processed"):
            llm_split = self._llm_split_document(
                event_data,
                document_id,
                ruling_text,
                state,
                county,
                raw_pdf_bytes=raw_pdf_bytes,
            )
            if llm_split:
                # LLM extractor returned results — recurse with split events.
                return
            # LLM extractor failed or returned nothing — continue with
            # single-document processing and per-field extraction below.

        # Parse timestamps
        capture_ts = _parse_datetime(event_data.get("capture_timestamp"))
        hearing_dt = _parse_date(event_data.get("hearing_date"))

        raw_outcome: str | None = event_data.get("outcome")
        # Normalize scraper-provided outcome to lowercase enum value (#1878).
        outcome: str | None = normalize_outcome(raw_outcome) if raw_outcome else None
        if raw_outcome and outcome != raw_outcome:
            logger.info(
                "Normalized outcome",
                extra={
                    "document_id": document_id,
                    "raw_outcome": raw_outcome,
                    "normalized_outcome": outcome,
                },
            )
        raw_motion_type: str | None = event_data.get("motion_type")
        # Normalize scraper-provided motion_type to snake_case (#1712).
        # If the value cannot be mapped, set to None so the regex
        # fallback below can extract from ruling_text instead.
        motion_type: str | None = (
            normalize_motion_type(raw_motion_type) if raw_motion_type else None
        )
        if raw_motion_type and motion_type != raw_motion_type:
            logger.info(
                "Normalized motion_type",
                extra={
                    "document_id": document_id,
                    "raw_motion_type": raw_motion_type,
                    "normalized_motion_type": motion_type,
                },
            )
        case_type: str | None = event_data.get("case_type")
        parties_data: list[dict[str, str]] = event_data.get("parties", [])

        # ------------------------------------------------------------------
        # Determine which fields are missing and need extraction
        # ------------------------------------------------------------------
        missing_fields = [f for f in EXTRACTABLE_FIELDS if not event_data.get(f)]

        # Track which method populated each extracted field for logging
        extraction_methods: dict[str, str] = {}

        # When the event was produced by the LLM extraction path (_llm_extracted),
        # all fields are already populated — skip per-field LLM and regex extraction.
        is_llm_extracted = event_data.get("_llm_extracted", False)
        if is_llm_extracted:
            # Mark all populated fields as "llm" for the extraction methods log.
            for field in EXTRACTABLE_FIELDS:
                if event_data.get(field):
                    extraction_methods[field] = "llm_extractor"

        # When the scraper/county uses ExtractionMethod.NONE, all fields are
        # already populated by the scraper — skip per-field LLM extraction.
        # This handles scrapers like SD calendar where structured HTML is
        # fully parsed and the LLM would produce wrong results (#2331).
        is_extraction_none = False
        if not is_llm_extracted:
            from framework.extraction_config import ExtractionMethod, get_county_extraction_config

            _ext_config = get_county_extraction_config(state, county, scraper_id=scraper_id)
            if _ext_config is not None and _ext_config.method == ExtractionMethod.NONE:
                is_extraction_none = True
                for field in EXTRACTABLE_FIELDS:
                    if event_data.get(field):
                        extraction_methods[field] = "scraper"

        # ------------------------------------------------------------------
        # LLM extraction — primary method for missing fields
        # ------------------------------------------------------------------
        llm_result: LLMExtractionResult | None = None
        case_number_prefix: str | None = None
        skip_llm = is_llm_extracted or is_extraction_none
        if not skip_llm and missing_fields and ruling_text and self._llm_client is not None:
            metadata = {
                "link_text": event_data.get("extra", {}).get("link_text")
                if isinstance(event_data.get("extra"), dict)
                else None,
                "judge_name": event_data.get("judge_name"),
                "department": event_data.get("department"),
            }
            # Look up county-specific max_output_tokens (#2355).
            # Large-document counties (e.g. Santa Clara, 130K+ chars) need
            # more than the default 4096 output tokens to avoid truncated JSON.
            from framework.extraction_config import get_county_extraction_config as _get_cc

            _cc = _get_cc(state, county)
            _llm_max_tokens = _cc.max_output_tokens if _cc and _cc.max_output_tokens else 4096

            t0 = time.monotonic()
            llm_result = extract_fields_llm(
                document_text=ruling_text,
                content_format=content_format,
                metadata=metadata,
                client=self._llm_client,
                provider=self._llm_provider,
                model=self._llm_model,
                timeout=self._llm_timeout,
                max_tokens=_llm_max_tokens,
            )
            llm_latency_ms = round((time.monotonic() - t0) * 1000)

            if llm_result is not None:
                # Find the matching ruling by case_number if available
                ruling = _match_ruling(llm_result, case_number)

                # Apply document-level fields from LLM.  Guard against
                # implausible hearing dates (#2370): the LLM sometimes
                # picks up continuance dates from ruling body text.  A
                # hearing date far outside the capture window is almost
                # certainly wrong — skip it and let regex/capture-date
                # fallbacks run.
                if hearing_dt is None and llm_result.hearing_date is not None:
                    if is_plausible_hearing_date(llm_result.hearing_date, capture_ts):
                        hearing_dt = llm_result.hearing_date
                        extraction_methods["hearing_date"] = "llm"
                    else:
                        logger.info(
                            "Rejected LLM hearing_date as implausible",
                            extra={
                                "document_id": document_id,
                                "llm_hearing_date": str(llm_result.hearing_date),
                                "capture_timestamp": str(capture_ts) if capture_ts else None,
                            },
                        )
                if not judge_name and llm_result.judge_name:
                    judge_name = llm_result.judge_name
                    extraction_methods["judge_name"] = "llm"
                if not department and llm_result.department:
                    department = llm_result.department
                    extraction_methods["department"] = "llm"

                # Apply ruling-level fields from the matched ruling
                if ruling is not None:
                    if ruling.case_number_prefix:
                        case_number_prefix = ruling.case_number_prefix
                    if not case_number and ruling.case_number:
                        if is_valid_case_number(ruling.case_number):
                            case_number = ruling.case_number
                            extraction_methods["case_number"] = "llm"
                        else:
                            # LLM returned a case title as case_number —
                            # use it as case_title instead (#1524).
                            logger.warning(
                                "LLM case_number looks like a case title — "
                                "redirecting to case_title",
                                extra={
                                    "document_id": document_id,
                                    "rejected_case_number": ruling.case_number[:80],
                                },
                            )
                            if not case_title:
                                case_title = ruling.case_number
                                extraction_methods["case_title"] = "llm_redirect"
                    if not case_title and ruling.case_title:
                        case_title = ruling.case_title
                        extraction_methods["case_title"] = "llm"
                    if outcome is None and ruling.outcome:
                        outcome = ruling.outcome
                        extraction_methods["outcome"] = "llm"
                    if motion_type is None and ruling.motion_type:
                        motion_type = ruling.motion_type
                        extraction_methods["motion_type"] = "llm"
                    if case_type is None and ruling.case_type:
                        case_type = ruling.case_type
                        extraction_methods["case_type"] = "llm"
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
                    "LLM extraction returned None — enrichment fields may be missing",
                    extra={
                        "document_id": document_id,
                        "llm_latency_ms": llm_latency_ms,
                    },
                )

        # ------------------------------------------------------------------
        # LLM enrichment — dedicated enrichment call for motion_type,
        # outcome, case_title, and parties (#2176).  Runs after per-field
        # LLM extraction.  Regex fallback removed in #2178 — LLM is now
        # the only enrichment path for these four fields.
        #
        # Skip enrichment when both case_number and case_title are empty
        # (#2571).  When BOTH are missing after per-field LLM extraction,
        # the ruling is almost certainly a fragment of a bad split — e.g.
        # a CC LLM splitter over-split a single multi-Issue MSJ/MSA
        # ruling into N Issue-level fragments, each of which lacks case
        # metadata because that only appears once at the top of the
        # parent entry.  Running enrichment on such fragments is
        # counter-productive: the fragment text mentions "summary
        # adjudication" so the enrichment LLM confidently returns
        # motion_type="motion_for_summary_adjudication", polluting the
        # DB with wrong classifications.  Leaving motion_type=None (and
        # letting the Layer 3 deterministic rule reject the row) is the
        # correct behaviour.
        # ------------------------------------------------------------------
        is_bad_split_fragment = not case_number and not case_title
        enrichment_fields_missing = (
            ruling_text
            and not is_extraction_none
            and not is_bad_split_fragment
            and (outcome is None or motion_type is None or not case_title or not parties_data)
        )
        if is_bad_split_fragment and ruling_text and not is_extraction_none:
            logger.info(
                "Skipping LLM enrichment — ruling looks like a bad-split "
                "fragment (case_number and case_title both empty); "
                "leaving motion_type=None",
                extra={
                    "document_id": document_id,
                    "county": county,
                },
            )
            extraction_methods.setdefault("motion_type", "skipped_bad_split_fragment")
        if enrichment_fields_missing:
            enrichment_result, enrich_status = self._llm_enrich_fields(ruling_text, document_id)
            (
                outcome,
                motion_type,
                case_title,
                parties_data,
                enrichment_methods,
            ) = self._apply_enrichment_result(
                enrichment_result,
                status_tag=enrich_status,
                outcome=outcome,
                motion_type=motion_type,
                case_title=case_title,
                parties_data=parties_data,
            )
            extraction_methods.update(enrichment_methods)
            if enrich_status == "errored":
                # Re-raise for non-split events so the caller's retry /
                # dead-letter logic can act on the exhaustion.  The
                # diagnostic marker has already been recorded above.
                from framework.llm_enrichment import LlmEnrichmentExhaustedError

                raise LlmEnrichmentExhaustedError("LLM enrichment exhausted all retries")

        # ------------------------------------------------------------------
        # Remaining regex fallbacks — hearing_date, judge_name, case_number,
        # and case_type are still extracted via regex when missing.  The
        # enrichment fields (outcome, motion_type, case_title, parties)
        # are handled exclusively by LLM enrichment above (#2178).
        # ------------------------------------------------------------------
        if hearing_dt is None and ruling_text:
            hearing_dt = extract_hearing_date(ruling_text)
            if hearing_dt is not None:
                extraction_methods.setdefault("hearing_date", "regex")
                logger.info(
                    "Extracted hearing_date from ruling text (regex fallback)",
                    extra={"document_id": document_id, "hearing_date": str(hearing_dt)},
                )

        if is_llm_extracted and ruling_text:
            if judge_name is None:
                judge_name = extract_judge_name(ruling_text)
                if judge_name is not None:
                    extraction_methods["judge_name"] = "regex_post_llm"

        # case_type from case number — does not require ruling_text.
        if case_type is None and case_number:
            case_type = extract_case_type_from_number(case_number)
            if case_type:
                extraction_methods.setdefault("case_type", "regex")

        # Clean ruling text for display — the cleaned version is stored
        # in Postgres.
        cleaned_ruling_text = clean_ruling_text(ruling_text)

        court_name = f"{court}, County of {county}"

        # Fallback case number extraction (regex)
        if not case_number and ruling_text:
            extracted = extract_case_number(ruling_text)
            if extracted:
                method = "regex_post_llm" if is_llm_extracted else "regex"
                extraction_methods.setdefault("case_number", method)
                logger.info(
                    "Extracted case_number from ruling text (regex fallback)",
                    extra={"document_id": document_id, "case_number": extracted},
                )
                case_number = extracted

        # ------------------------------------------------------------------
        # Deterministic case_title cleanup (#2212, #2370)
        # ------------------------------------------------------------------
        # First: strip repeated segments and trailing case numbers that the
        # LLM sometimes appends (#2370).  These are cheap checks that
        # always run; they leave well-formed titles unchanged.
        if case_title:
            # Strip → dedupe → strip ordering (#3511): strip trailing case
            # number first so dedupe sees a clean boundary, then dedupe
            # repeated segments, then strip again in case dedupe surfaced a
            # trailing fragment from a middle copy.
            without_cn = strip_trailing_case_number(case_title)
            if without_cn and without_cn != case_title:
                logger.info(
                    "Stripped trailing case number from title",
                    extra={
                        "document_id": document_id,
                        "old_title": case_title[:120],
                        "new_title": without_cn[:120],
                    },
                )
                case_title = without_cn
            deduped = dedupe_repeated_title(case_title)
            if deduped and deduped != case_title:
                logger.info(
                    "Removed repeated title segments",
                    extra={
                        "document_id": document_id,
                        "old_title": case_title[:120],
                        "new_title": deduped[:120],
                    },
                )
                case_title = deduped
            without_cn2 = strip_trailing_case_number(case_title)
            if without_cn2 and without_cn2 != case_title:
                logger.info(
                    "Stripped trailing case number from title (post-dedupe)",
                    extra={
                        "document_id": document_id,
                        "old_title": case_title[:120],
                        "new_title": without_cn2[:120],
                    },
                )
                case_title = without_cn2
            without_prefix = strip_case_number_prefix_suffix(case_title, case_number_prefix)
            if without_prefix and without_prefix != case_title:
                logger.info(
                    "Stripped OC case number prefix suffix from title",
                    extra={
                        "document_id": document_id,
                        "old_title": case_title[:120],
                        "new_title": without_prefix[:120],
                        "prefix": case_number_prefix,
                    },
                )
                case_title = without_prefix

        # Strip trailing connector words from LLM-extracted titles that are
        # already <=120 chars (i.e. they bypass clean_case_title's truncation
        # path) but still end in a dangling connector (#3730).
        if case_title:
            sanitized = strip_trailing_connectors(case_title)
            if sanitized and sanitized != case_title:
                logger.info(
                    "Stripped trailing connector from title",
                    extra={
                        "document_id": document_id,
                        "old_title": case_title[:120],
                        "new_title": sanitized[:120],
                    },
                )
                case_title = sanitized

        # For LLM-extracted events, the LLM-provided case_title can be
        # messy (motion descriptions, case citations, multiple parties).
        # Apply deterministic cleanup via clean_case_title().
        if case_title and not is_plausible_case_title(case_title):
            cleaned_title = clean_case_title(case_title)
            if cleaned_title and is_plausible_case_title(cleaned_title):
                if cleaned_title != case_title:
                    logger.info(
                        "Deterministic case_title cleanup applied",
                        extra={
                            "document_id": document_id,
                            "old_title": case_title[:80],
                            "new_title": cleaned_title[:80],
                        },
                    )
                case_title = cleaned_title
                extraction_methods["case_title"] = "deterministic_cleaned"

        # Probate decedent-as-judge guard (#2370): for probate cases the
        # decedent/conservatee name is prominently printed in the caption
        # and the LLM can mistake it for the judge.  If the extracted
        # judge_name matches the probate party name, discard it so the
        # dept-to-judge mapping fallback can take over.
        if judge_name and case_title and is_probate_decedent_name(judge_name, case_title):
            logger.info(
                "Rejected LLM judge_name as probate decedent",
                extra={
                    "document_id": document_id,
                    "rejected_judge": judge_name,
                    "case_title": case_title[:80],
                },
            )
            judge_name = None
            extraction_methods.pop("judge_name", None)

        # Fallback judge_name extraction from ruling text (#401).
        if not is_llm_extracted and not judge_name and ruling_text:
            judge_name = extract_judge_name(ruling_text)
            if judge_name:
                extraction_methods.setdefault("judge_name", "regex")
                logger.info(
                    "Extracted judge_name from ruling text (regex fallback)",
                    extra={"document_id": document_id, "judge_name": judge_name},
                )

        # LA County department-to-judge lookup (#584).
        # When LLM and regex extraction both fail to find a judge name but a
        # department is available, look up the judge via the LA judicial officer
        # directory mapping.
        la_dept_eligible = (
            not is_llm_extracted
            and not judge_name
            and department
            and state == "CA"
            and county == "Los Angeles"
        )
        if la_dept_eligible:
            dept_map = self._get_la_dept_map()
            dept_judge = lookup_judge_for_department(dept_map, department)
            if dept_judge:
                judge_name = dept_judge
                extraction_methods["judge_name"] = "dept_lookup"
                logger.info(
                    "Resolved judge_name from LA department mapping",
                    extra={
                        "document_id": document_id,
                        "department": department,
                        "judge_name": judge_name,
                    },
                )

        # Fallback case_type from case number prefix (#706).
        if case_type is None and case_number:
            case_type = extract_case_type_from_number(case_number)
            if case_type:
                extraction_methods.setdefault("case_type", "regex")

        # Fallback case_type from scraper_id (#1524).
        # When the case number is absent or doesn't encode a type prefix
        # (e.g. OC North JC PDFs have no case numbers), infer from the
        # scraper_id which encodes the case category in its suffix.
        if case_type is None and scraper_id:
            case_type = extract_case_type_from_scraper_id(scraper_id)
            if case_type:
                extraction_methods.setdefault("case_type", "scraper_id")

        # Fallback case_type from motion_type (#1702).
        # Final fallback for cases where the case number has no embedded
        # type code and the scraper_id is generic (e.g. Ventura's
        # all-digit case numbers like 202300574258).  Many civil motion
        # types unambiguously identify the case type.
        if case_type is None and motion_type:
            case_type = extract_case_type_from_motion_type(motion_type)
            if case_type:
                extraction_methods.setdefault("case_type", "motion_type")

        # Fallback case_type from case title (#2062).
        # Final fallback for cases where no other signal determined the
        # case type.  Probate-style titles ("In the Matter of...",
        # "Conservatorship of...", "Guardianship of...") are unambiguous.
        if case_type is None:
            eff_title = case_title or event_data.get("case_title")
            if eff_title:
                case_type = extract_case_type_from_title(eff_title)
                if case_type:
                    extraction_methods.setdefault("case_type", "title")

        if extraction_methods:
            logger.info(
                "Field extraction summary",
                extra={
                    "document_id": document_id,
                    "extraction_methods": extraction_methods,
                },
            )

        # Normalize department name before it's used for lookups or DB
        # writes (#2141).  Placed here so the dept-to-judge lookup below
        # sees the canonical department name.
        department = normalize_department(
            county,
            department,
            courthouse=event_data.get("courthouse"),
        )

        # Apply Contra Costa department reassignment mapping (#2612).
        # Runs after normalization so both sides of the comparison are in
        # canonical form, and before the dept-to-judge roster lookup so
        # that the lookup uses the post-reassignment department.
        if state == "CA" and county == "Contra Costa":
            department = apply_cc_dept_reassignment(
                department,
                hearing_date=hearing_dt,
                case_number=case_number,
                courthouse=event_data.get("courthouse"),
            )

        # ------------------------------------------------------------------
        # Universal dept-to-judge fallback via court directory snapshots (#2269).
        # When all extraction methods (LLM, regex, scraper) have failed to
        # provide a judge name but a department is available, look up the
        # judge from the court directory snapshot (roster data).
        #
        # This fires for ALL counties and regardless of _llm_extracted status,
        # fixing the regression where rebuilds (which use the LLM split path
        # with _llm_extracted=True) skipped the old LA-specific fallback and
        # had no fallback at all for other counties.
        # ------------------------------------------------------------------
        if not judge_name and department and state == "CA":
            conn = self._get_connection()
            court_id_for_dept = upsert_court(conn, state, county, court_name)
            dept_judge = resolve_judge_from_department(
                conn, court_id_for_dept, department, hearing_date=hearing_dt
            )
            if dept_judge:
                judge_name = dept_judge
                extraction_methods["judge_name"] = "roster_dept_lookup"
                logger.info(
                    "Resolved judge_name from court directory snapshot",
                    extra={
                        "document_id": document_id,
                        "department": department,
                        "judge_name": judge_name,
                        "county": county,
                    },
                )

        # ------------------------------------------------------------------
        # Warn when a PDF document has no ruling text after all extraction
        # attempts — likely an image-only PDF that yielded no text (#1335).
        # ------------------------------------------------------------------
        if not ruling_text and content_format == "pdf":
            logger.warning(
                "PDF document has no ruling text after all extraction attempts",
                extra={
                    "document_id": document_id,
                    "source_url": source_url,
                    "scraper_id": scraper_id,
                },
            )

        # ------------------------------------------------------------------
        # Warn when case_title is still NULL after all extraction attempts
        # — the case will be stored without a title (#1359).
        # ------------------------------------------------------------------
        if not case_title:
            logger.warning(
                "No case_title extracted after all attempts — case will have NULL title",
                extra={
                    "document_id": document_id,
                    "case_number": case_number,
                    "scraper_id": scraper_id,
                },
            )

        # ------------------------------------------------------------------
        # Ruling text HTML: prefer scraper-provided sanitized HTML (#2179),
        # fall back to LLM formatting (opt-in via ENABLE_RULING_FORMATTING).
        # ------------------------------------------------------------------
        ruling_text_html: str | None = scraper_ruling_text_html
        if ruling_text_html is not None:
            logger.info(
                "Using scraper-provided ruling_text_html",
                extra={
                    "document_id": document_id,
                    "html_length": len(ruling_text_html) if ruling_text_html else 0,
                },
            )
        elif self._formatting_enabled and cleaned_ruling_text:
            t0_fmt = time.monotonic()
            ruling_text_html = format_ruling_text(
                cleaned_ruling_text,
                client=self._formatting_client,
                timeout=self._llm_timeout,
            )
            fmt_latency_ms = round((time.monotonic() - t0_fmt) * 1000)
            logger.info(
                "Ruling formatting completed",
                extra={
                    "document_id": document_id,
                    "formatting_latency_ms": fmt_latency_ms,
                    "is_pre_fallback": "<pre>" in ruling_text_html if ruling_text_html else False,
                },
            )

        # ------------------------------------------------------------------
        # LLM-powered ruling summarization (opt-in via ENABLE_RULING_SUMMARIZATION)
        # ------------------------------------------------------------------
        summary: str | None = None
        summary_model: str | None = None
        summary_generated_at: datetime | None = None
        if self._summarization_enabled and cleaned_ruling_text:
            # Build case context for better summaries
            case_context: dict[str, str] = {}
            if case_title:
                case_context["case_title"] = case_title
            if motion_type:
                case_context["motion_type"] = motion_type

            t0_sum = time.monotonic()
            summary, summary_model = summarize_ruling(
                cleaned_ruling_text,
                case_context=case_context if case_context else None,
                client=self._summarization_client,
                timeout=self._llm_timeout,
            )
            sum_latency_ms = round((time.monotonic() - t0_sum) * 1000)
            if summary is not None:
                summary_generated_at = datetime.now(UTC)
            logger.info(
                "Ruling summarization completed",
                extra={
                    "document_id": document_id,
                    "summarization_latency_ms": sum_latency_ms,
                    "has_summary": summary is not None,
                    "summary_length": len(summary) if summary else 0,
                },
            )

        # ------------------------------------------------------------------
        # Deterministic validation rules — cheap, rule-based checks that
        # run on every document (#2329).  These catch obvious structural
        # failures (raw HTML, wrong dates, concatenated titles) without
        # requiring an LLM call.  Runs before the LLM validation gate.
        # ------------------------------------------------------------------
        captured_at_date: date | None = capture_ts.date() if capture_ts else None
        det_result = run_deterministic_rules(
            ruling_text=cleaned_ruling_text,
            case_number=case_number or f"UNKNOWN-{document_id}",
            case_title=case_title,
            hearing_date=hearing_dt,
            captured_at=captured_at_date,
        )

        if det_result.overall != "pass":
            logger.info(
                "Deterministic validation result",
                extra={
                    "document_id": document_id,
                    "det_overall": det_result.overall,
                    "det_failed_rules": [r.rule for r in det_result.failed_rules],
                    "det_flagged_rules": [r.rule for r in det_result.flagged_rules],
                    "det_reasons": det_result.reasons,
                    "county": county,
                    "case_number": case_number,
                },
            )

        if det_result.overall == "fail":
            # Convert to a ValidationResult for DB logging.
            det_validation_result = ValidationResult(
                result="fail",
                reason="; ".join(det_result.reasons),
                model="deterministic",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
            )
            failed_rule_names = [r.rule for r in det_result.failed_rules]
            # Surface a dedicated telemetry marker when the drop was driven
            # by the empty-text rule (#2646).  This lets log-based
            # dashboards/alerts count drops without string-matching the
            # aggregated reason field.
            telemetry_event = (
                "data_quality.ruling_empty_text_dropped"
                if "ruling_text_not_empty" in failed_rule_names
                else None
            )
            logger.warning(
                "Deterministic validation FAIL — skipping DB write",
                extra={
                    "document_id": document_id,
                    "det_reasons": det_result.reasons,
                    "det_failed_rules": failed_rule_names,
                    "telemetry_event": telemetry_event,
                    "county": county,
                    "case_number": case_number,
                },
            )
            try:
                conn = self._get_connection()
                insert_validation_result(
                    conn,
                    document_id=document_id,
                    ruling_id=None,
                    result=det_validation_result,
                )
                conn.commit()
            except Exception as exc:
                logger.warning(
                    "Failed to log deterministic validation result to DB: %s",
                    exc,
                )
                # Rollback to clear any aborted transaction state so
                # subsequent DB operations on the shared connection can
                # proceed (#2385).
                try:
                    conn.rollback()
                except Exception:
                    pass
            return

        if det_result.overall == "flag":
            # Log flag results as ValidationResult for monitoring.
            det_validation_result = ValidationResult(
                result="flag",
                reason="; ".join(det_result.reasons),
                model="deterministic",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
            )
            try:
                conn = self._get_connection()
                insert_validation_result(
                    conn,
                    document_id=document_id,
                    ruling_id=None,
                    result=det_validation_result,
                )
                conn.commit()
            except Exception as exc:
                logger.warning(
                    "Failed to log deterministic validation flag to DB: %s",
                    exc,
                )
                # Rollback to clear any aborted transaction state so
                # subsequent DB operations on the shared connection can
                # proceed (#2385).
                try:
                    conn.rollback()
                except Exception:
                    pass
            # Continue to DB write — flag does not block.

        # ------------------------------------------------------------------
        # Validation gate — LLM-based quality check (opt-in via
        # ENABLE_INGESTION_VALIDATION). Runs between enrichment and DB write.
        # On fail: skip DB write entirely. On error: treat as pass.
        # ------------------------------------------------------------------
        validation_result: ValidationResult | None = None
        if self._validation_enabled:
            hearing_date_str = str(hearing_dt) if hearing_dt else None
            validation_result = validate_document(
                ruling_text=cleaned_ruling_text,
                case_number=case_number,
                case_title=case_title,
                judge_name=judge_name,
                motion_type=motion_type,
                outcome=outcome,
                hearing_date=hearing_date_str,
                department=department,
                county=county,
                client=self._validation_client,
                provider="anthropic",
                timeout=self._llm_timeout,
            )

            logger.info(
                "Validation gate result",
                extra={
                    "document_id": document_id,
                    "validation_result": validation_result.result,
                    "validation_reason": validation_result.reason,
                    "validation_model": validation_result.model,
                    "validation_latency_ms": validation_result.latency_ms,
                    "validation_input_tokens": validation_result.input_tokens,
                    "validation_output_tokens": validation_result.output_tokens,
                },
            )

            # Auto-file GitHub issues for flag/fail results.
            if validation_result.result in ("flag", "fail"):
                extracted_fields = {
                    "case_number": case_number,
                    "case_title": case_title,
                    "judge_name": judge_name,
                    "motion_type": motion_type,
                    "outcome": outcome,
                    "hearing_date": hearing_date_str,
                    "department": department,
                }
                try:
                    self._file_validation_issue(
                        result=validation_result.result,
                        reason=validation_result.reason or "Unknown reason",
                        county=county,
                        case_number=case_number,
                        document_id=document_id,
                        s3_key=s3_key,
                        ruling_text_excerpt=(
                            cleaned_ruling_text[:200] if cleaned_ruling_text else None
                        ),
                        extracted_fields=extracted_fields,
                    )
                except Exception as exc:
                    logger.warning(
                        "Issue filing failed (outer catch): %s",
                        exc,
                        extra={"document_id": document_id},
                    )

            # On fail: skip DB write entirely. Log and return.
            if validation_result.result == "fail":
                logger.warning(
                    "Validation FAIL — skipping DB write",
                    extra={
                        "document_id": document_id,
                        "validation_reason": validation_result.reason,
                        "county": county,
                        "case_number": case_number,
                    },
                )
                # Still log the validation result to the DB (validation_results
                # table only — not the ruling itself).
                try:
                    conn = self._get_connection()
                    insert_validation_result(
                        conn,
                        document_id=document_id,
                        ruling_id=None,
                        result=validation_result,
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "Failed to log validation result to DB: %s",
                        exc,
                    )
                    # Rollback to clear any aborted transaction state so
                    # subsequent DB operations on the shared connection can
                    # proceed (#2385).
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                return

        # ------------------------------------------------------------------
        # Defense-in-depth: all-NULL metadata guard (#2676).
        # ------------------------------------------------------------------
        # Capture-side filters (#2486) skip non-ruling PDFs (admin notices,
        # cover sheets, "no tentative rulings today" pages) before they
        # ever reach this worker.  As a belt-and-suspenders fallback, we
        # also check here: if every one of the six core metadata fields is
        # NULL/empty (with ``UNKNOWN-*`` case_numbers treated as NULL),
        # this is non-ruling noise that would pollute ``derived.rulings``.
        # Drop it, emit telemetry, and return before the DB upsert.
        _guard_ruling = {
            "case_number": case_number,
            "case_title": case_title,
            "judge_name": judge_name,
            "department": department,
            "motion_type": motion_type,
            "outcome": outcome,
        }
        if is_all_null_metadata(_guard_ruling):
            s3_key_val = event_data.get("s3_key")
            logger.warning(
                "Pipeline-side guard: dropping non-ruling PDF with all-NULL metadata",
                extra={
                    "document_id": document_id,
                    "county": county,
                    "state": state,
                    "scraper_id": scraper_id,
                    "s3_key": s3_key_val,
                    "telemetry_event": NON_RULING_PDF_DROPPED_EVENT,
                },
            )
            # Best-effort telemetry write (savepoint-protected so a transient
            # failure does not abort the caller's transaction).
            try:
                conn = self._get_connection()
                with conn.cursor() as cur:
                    cur.execute("SAVEPOINT non_ruling_pdf_metric")
                    try:
                        cur.execute(
                            """
                            INSERT INTO data_quality_metrics
                                (recorded_at, county, metric_name,
                                 metric_value, metadata)
                            VALUES (now(), %s, %s, %s, %s::jsonb)
                            """,
                            (
                                county,
                                NON_RULING_PDF_DROPPED_METRIC,
                                1,
                                json.dumps(
                                    {
                                        "document_id": document_id,
                                        "s3_key": s3_key_val,
                                        "state": state,
                                        "scraper_id": scraper_id,
                                    }
                                ),
                            ),
                        )
                        cur.execute("RELEASE SAVEPOINT non_ruling_pdf_metric")
                    except Exception:  # noqa: BLE001 — best-effort
                        cur.execute("ROLLBACK TO SAVEPOINT non_ruling_pdf_metric")
                        raise
                conn.commit()
            except Exception:  # noqa: BLE001 — telemetry is best-effort
                logger.debug(
                    "non_ruling_pdf_dropped telemetry write failed",
                    exc_info=True,
                )
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            return

        # For split events, each split ruling gets its own document row keyed
        # by the synthetic split document_id.  This ensures the FK from
        # insert_ruling(document_id=...) is satisfied.  See #1775, PR #1755.
        is_split = event_data.get("_split_processed", False)
        if is_split:
            split_index = event_data.get("_split_index", 0)
            content_hash = hashlib.sha256(
                f"{content_hash}:ruling:{split_index}".encode()
            ).hexdigest()

        conn = self._get_connection()
        try:
            # 1. Ensure court exists
            court_id = upsert_court(conn, state, county, court_name)

            # 1b. Enrichment — resolve extracted fields against DB data (#1576).
            # Skip enrichment when case_number is empty/synthetic, as there is
            # insufficient data for meaningful matching.  hearing_date being
            # None does NOT skip enrichment — the case_number is the primary
            # lookup key (#2215).
            _skip_enrichment = not case_number or case_number.startswith("UNKNOWN-")
            if not _skip_enrichment:
                enrichment_engine = EnrichmentEngine(conn)
                party_names = [p.get("name") for p in parties_data if p.get("name")]
                enrich_result = enrichment_engine.enrich(
                    case_number=case_number,
                    court_id=court_id,
                    judge_name=judge_name,
                    department=department,
                    hearing_date=hearing_dt,
                    parties=party_names,
                )

                # Apply case number correction from fuzzy match.
                if (
                    enrich_result.case_match is not None
                    and enrich_result.case_match.match_type == "fuzzy"
                ):
                    original_case_number = case_number
                    case_number = enrich_result.case_match.case_number
                    logger.info(
                        "Enrichment corrected case_number via fuzzy match",
                        extra={
                            "document_id": document_id,
                            "original_case_number": original_case_number,
                            "corrected_case_number": case_number,
                            "confidence": enrich_result.case_match.confidence,
                            "levenshtein_distance": enrich_result.case_match.levenshtein_distance,
                            "party_overlap": enrich_result.case_match.party_overlap,
                        },
                    )

                # Capture original judge name before any enrichment updates so
                # that all log messages consistently reference the scraper's
                # original extracted value, not a post-enrichment canonical name.
                _original_judge_name = judge_name

                # Apply judge name resolution from alias match.
                if (
                    enrich_result.judge_resolution is not None
                    and enrich_result.judge_resolution.match_type == "alias"
                    and enrich_result.judge_resolution.canonical_name
                ):
                    judge_name = enrich_result.judge_resolution.canonical_name
                    if _original_judge_name != judge_name:
                        logger.info(
                            "Enrichment resolved judge via alias",
                            extra={
                                "document_id": document_id,
                                "original_judge_name": _original_judge_name,
                                "canonical_name": judge_name,
                                "confidence": enrich_result.judge_resolution.confidence,
                            },
                        )

                # Log directory mismatches as warnings (do NOT override judge).
                if (
                    enrich_result.judge_resolution is not None
                    and enrich_result.judge_resolution.directory_mismatch
                ):
                    logger.warning(
                        "Enrichment: judge/directory mismatch — flagged for review",
                        extra={
                            "document_id": document_id,
                            "extracted_judge": _original_judge_name,
                            "directory_judge": enrich_result.judge_resolution.directory_judge,
                            "department": department,
                        },
                    )

                # Log enrichment flags for human review.
                if enrich_result.flags:
                    logger.info(
                        "Enrichment flags for human review",
                        extra={
                            "document_id": document_id,
                            "flags": enrich_result.flags,
                        },
                    )

            # 2. Ensure case exists — use document_id as synthetic case_number if absent
            effective_case_number = case_number or f"UNKNOWN-{document_id}"
            if effective_case_number.startswith("UNKNOWN-"):
                logger.warning(
                    "No case_number extractable for document — using synthetic UNKNOWN identifier",
                    extra={"document_id": document_id},
                )
            # 2b. Cross-case title lookup via upsert (#2006).
            # Use upsert_case_returning_title to get the effective title after
            # COALESCE — if the DB already has a title from a prior ruling and
            # this ingestion has no title, the existing title is preserved and
            # returned.  This prevents future null-title warnings for cases
            # that have already been titled.
            case_id, effective_title = upsert_case_returning_title(
                conn, effective_case_number, court_id, case_title=case_title, case_type=case_type
            )
            if not case_title and effective_title:
                case_title = effective_title
                extraction_methods.setdefault("case_title", "db_lookup")
                logger.info(
                    "Populated case_title from existing DB record",
                    extra={
                        "document_id": document_id,
                        "case_number": effective_case_number,
                        "case_title": effective_title,
                    },
                )

            # 3. Resolve judge name to canonical judge record
            judge_id: str | None = None
            if judge_name:
                judge_id = resolve_judge(
                    conn,
                    judge_name,
                    court_id,
                    source=scraper_id or "scraper",
                )

            # 4. Insert document + ruling via shared helper (#1790).
            # The helper guarantees the same document_id is passed to both
            # insert_document and insert_ruling, preventing FK divergence (#1775).
            is_new = insert_document_and_ruling(
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
                captured_at=capture_ts or datetime.now(UTC),
                hearing_date=hearing_dt,
                ruling_text=cleaned_ruling_text,
                ruling_text_html=ruling_text_html,
                department=department,
                judge_id=judge_id,
                outcome=outcome,
                motion_type=motion_type,
                summary=summary,
                summary_model=summary_model,
                summary_generated_at=summary_generated_at,
            )

            # 5. Link case to judge
            if judge_id is not None:
                upsert_case_judge(conn, case_id, judge_id, hearing_dt)

            # 6. Create party records and link to case (batched — O(1) queries)
            batch_upsert_parties(conn, case_id, parties_data)

            # 7. Log validation result (if validation was run).
            if validation_result is not None:
                insert_validation_result(
                    conn,
                    document_id=document_id,
                    ruling_id=None,
                    result=validation_result,
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Index in OpenSearch.  For split rulings, always index (each split
        # gets its own OS entry keyed by the synthetic split document_id).
        # For non-split rulings, only index when the document is new.
        should_index = is_new or is_split
        if should_index:
            # Use the ruling's document_id (synthetic for splits) as the OS _id
            self._indexer.index_document(
                {
                    "document_id": document_id,
                    "case_number": case_number,
                    "court": court_name,
                    "county": county,
                    "state": state,
                    "judge_name": judge_name,
                    "hearing_date": event_data.get("hearing_date"),
                    "motion_type": motion_type,
                    "outcome": outcome,
                    "case_title": case_title,
                    "summary": summary
                    or (cleaned_ruling_text[:500] if cleaned_ruling_text else None),
                    "ruling_text": ruling_text,
                    "s3_key": s3_key,
                    "content_hash": content_hash,
                    "content_format": content_format,
                }
            )
        else:
            logger.debug("Document %s already in Postgres — skipping OpenSearch index", document_id)

    # ------------------------------------------------------------------
    # LLM extraction path (#1473, #1475)
    # ------------------------------------------------------------------

    def _llm_split_document(
        self,
        event_data: dict[str, Any],
        document_id: str,
        ruling_text: str | None,
        state: str,
        county: str,
        *,
        raw_pdf_bytes: bytes | None = None,
    ) -> bool:
        """Use the framework LlmExtractor to split and extract a document.

        The framework ``LlmExtractor`` extracts all structured fields in a
        single API call, producing one ``ExtractedRuling`` per case in the
        document.  This is the sole splitting/extraction path since #1475
        removed the legacy regex splitter framework.

        When ``raw_pdf_bytes`` are available, uses multimodal per-page
        extraction via ``extract_from_pdf()`` for more accurate results
        on image-based PDFs (e.g., OC tentative rulings).  Falls back to
        text-based extraction via ``extract()`` if multimodal is unavailable.

        Each extracted ruling is re-injected as a synthetic split event with all
        fields pre-populated (``_llm_extracted=True``), so the normal
        ``process_event`` flow handles DB writes and indexing without redundant
        LLM calls.

        Parameters
        ----------
        event_data : dict
            The full event dict.
        document_id : str
            The document's unique ID.
        ruling_text : str | None
            The document's text content (may be PDF-extracted).
        state : str
            Two-letter state code.
        county : str
            County name.
        raw_pdf_bytes : bytes | None
            Raw PDF file content for multimodal extraction.  When provided,
            the multimodal path is attempted first; on failure, falls back
            to text-based extraction.

        Returns
        -------
        bool
            True if the LLM extractor succeeded and split events were
            dispatched. False if extraction failed and the caller should
            continue with single-document processing.
        """
        if not ruling_text and not raw_pdf_bytes:
            return False

        # ------------------------------------------------------------------
        # San Diego calendar HTML deterministic split (#2447)
        # ------------------------------------------------------------------
        # SD calendar pages contain 30+ cases in one HTML document.  Sending
        # the full page to an LLM produces raw-HTML dumps and cross-case
        # contamination — even with the NONE scraper override, events
        # emitted by ``scripts/rebuild_db.py`` with the synthetic scraper_id
        # ``rebuild-ca-san_diego`` previously fell through to the county
        # LLM default and corrupted ~40% of the SD sample.  Detect the
        # calendar HTML by content and dispatch one split event per case
        # using the deterministic ``_split_rulings`` in ``sd_calendar``.
        # This path is taken regardless of scraper_id so any future SD
        # calendar capture paths stay correct by default.
        if ruling_text and _try_sd_calendar_split(
            event_data, document_id, ruling_text, self.process_event
        ):
            return True

        # ------------------------------------------------------------------
        # LA tentative ruling HTML deterministic split (#2450)
        # ------------------------------------------------------------------
        # LA department-response HTML pages have a predictable structure
        # (<b>Case Number:</b> blocks inside <div id="speechSynthesis">) that
        # is cheap to parse with BeautifulSoup.  Sending the raw HTML to the
        # LLM previously failed on Word-pasted variants lacking the standard
        # DEPARTMENT header, producing ~12% UNKNOWN-<uuid> case_numbers
        # among events emitted by ``scripts/rebuild_db.py`` with the
        # synthetic scraper_id ``rebuild-ca-los_angeles``.  The deterministic
        # splitter correctly extracts case_number, hearing_date, department,
        # and parties from the HTML.  This path is taken regardless of
        # scraper_id so any future LA HTML capture paths stay correct.
        if ruling_text and _try_la_html_split(
            event_data, document_id, ruling_text, self.process_event
        ):
            return True

        # ------------------------------------------------------------------
        # Fresno multi-ruling PDF deterministic split (#3534)
        # ------------------------------------------------------------------
        # Fresno PDFs contain numbered ruling entries like ``(20) Tentative
        # Ruling``.  The LLM previously mis-attributed every ruling in a
        # multi-ruling PDF to the case referenced in the page-1 preamble.
        # The deterministic ``_split_rulings`` in ``fresno_tentatives`` is
        # county+format gated (Fresno + pdf only) so it never fires for other
        # counties.  Single-ruling PDFs (``_split_rulings`` returns ``[]``)
        # fall through to the normal LLM path below.
        if ruling_text and _try_fresno_pdf_split(
            event_data, document_id, ruling_text, self.process_event
        ):
            return True

        # Build metadata from scraper-provided fields.
        metadata: dict[str, str] = {}
        if event_data.get("judge_name"):
            metadata["judge_name"] = event_data["judge_name"]
        if event_data.get("department"):
            metadata["department"] = event_data["department"]
        if event_data.get("hearing_date"):
            metadata["hearing_date"] = str(event_data["hearing_date"])

        t0 = time.monotonic()
        extracted_rulings = None
        extraction_method = "llm"  # Track which path was used for logging.

        # Check if the county/scraper has a configured extraction method (#2271, #2331).
        # Counties with ExtractionMethod.LLM (e.g. Ventura) have custom
        # text-based prompts that extract structured fields including outcome.
        # The multimodal per-page extraction does NOT extract outcome or
        # motion_type, so using it for LLM-configured counties causes field
        # regressions.  Only use multimodal for counties explicitly configured
        # as ExtractionMethod.MULTIMODAL (e.g. Orange County).
        # Counties/scrapers with ExtractionMethod.NONE skip framework extraction
        # entirely (e.g. SD calendar — structured HTML already fully parsed).
        from framework.extraction_config import ExtractionMethod, get_county_extraction_config

        event_scraper_id: str = event_data.get("scraper_id", "")
        county_config = get_county_extraction_config(state, county, scraper_id=event_scraper_id)

        # NONE means the scraper handles everything — no framework extraction.
        if county_config is not None and county_config.method == ExtractionMethod.NONE:
            logger.debug(
                "Skipping framework extraction — scraper/county uses ExtractionMethod.NONE",
                extra={
                    "document_id": document_id,
                    "county": county,
                    "scraper_id": event_scraper_id,
                },
            )
            return False

        use_multimodal = (
            county_config is not None and county_config.method == ExtractionMethod.MULTIMODAL
        ) or county_config is None  # Default: allow multimodal for unconfigured counties

        # Multimodal path: per-page image extraction from raw PDF (#1590).
        # Only used when the county is configured for multimodal extraction
        # or has no specific config (default behavior).
        if raw_pdf_bytes is not None and use_multimodal:
            # Pass the county's multimodal max_output_tokens (if configured)
            # so dense multi-case pages don't truncate the LLM JSON response
            # at the default 4,096 tokens (#2369).
            mm_max_output_tokens: int | None = (
                county_config.max_output_tokens if county_config is not None else None
            )
            multimodal_extractor = self._get_multimodal_extractor(
                max_output_tokens=mm_max_output_tokens,
            )
            if multimodal_extractor is not None:
                try:
                    extracted_rulings = multimodal_extractor.extract_from_pdf(
                        raw_pdf_bytes, metadata=metadata or None
                    )
                    extraction_method = "multimodal"
                    if extracted_rulings:
                        logger.info(
                            "Multimodal extraction succeeded",
                            extra={
                                "document_id": document_id,
                                "ruling_count": len(extracted_rulings),
                                "extraction_method": "multimodal",
                            },
                        )
                except Exception as exc:
                    logger.warning(
                        "Multimodal extraction failed — falling back to text",
                        extra={
                            "document_id": document_id,
                            "error": str(exc),
                            "extraction_method": "multimodal",
                        },
                    )
                    extracted_rulings = None
        elif raw_pdf_bytes is not None and not use_multimodal:
            logger.debug(
                "Skipping multimodal extraction — county uses text-based LLM",
                extra={
                    "document_id": document_id,
                    "county": county,
                    "state": state,
                },
            )

        # Text-based fallback (or primary path when no raw PDF available).
        # Use `is None` to distinguish between failed extraction (None) and
        # successful extraction that found no results ([]).  An empty list
        # from multimodal extraction is authoritative — do not retry with text.
        if extracted_rulings is None and ruling_text:
            extraction_method = "llm"  # Falling back to text-based.

            # Check for county/scraper-specific extraction config (#1728, #2331).
            from framework.extraction_config import get_county_extraction_config

            county_config = get_county_extraction_config(state, county, scraper_id=event_scraper_id)
            county_prompt: str | None = None

            if county_config and county_config.provider:
                # County has a custom provider — use a dedicated extractor.
                extractor = self._get_county_extractor(
                    county_config.provider,
                    county_config.model,
                    county_config.max_output_tokens,
                    county_config.max_chars_per_chunk,
                )
                county_prompt = county_config.system_prompt
                if county_prompt:
                    extraction_method = "llm_county"
            else:
                extractor = self._get_framework_extractor()
                if county_config and county_config.system_prompt:
                    county_prompt = county_config.system_prompt
                    extraction_method = "llm_county"

            if extractor is None:
                return False
            try:
                extracted_rulings = extractor.extract(
                    ruling_text,
                    metadata=metadata or None,
                    system_prompt=county_prompt,
                )
            except Exception as exc:
                llm_latency_ms = round((time.monotonic() - t0) * 1000)
                logger.error(
                    "LLM extraction path failed",
                    extra={
                        "document_id": document_id,
                        "error": str(exc),
                        "llm_latency_ms": llm_latency_ms,
                        "extraction_method": extraction_method,
                    },
                )
                return False

        llm_latency_ms = round((time.monotonic() - t0) * 1000)

        if not extracted_rulings:
            # Post-extraction integrity check (#1337): surface orphan documents
            # (those that produce zero ruling records) via a structured, easily
            # searchable warning so operators can detect when probate PDFs or
            # other problematic content fail extraction.
            #
            # A per-county telemetry row is also written so the zero-extraction
            # rate is dashboard-queryable rather than grep-only (#2569).
            orphan_result = check_no_orphan_rulings(0)
            if orphan_result.is_orphan:
                logger.warning(
                    "Orphan document: no rulings extracted",
                    extra={
                        "document_id": document_id,
                        "original_document_id": document_id,
                        "s3_key": event_data.get("s3_key"),
                        "county": county,
                        "state": state,
                        "scraper_id": event_data.get("scraper_id"),
                        "llm_latency_ms": llm_latency_ms,
                        "extraction_method": extraction_method,
                        "reason": orphan_result.reason,
                        "event": "zero_ruling_extraction",
                    },
                )
                # Best-effort telemetry write (savepoint-protected so a
                # transient failure does not abort the caller's
                # transaction).
                try:
                    conn = self._get_connection()
                    with conn.cursor() as cur:
                        cur.execute("SAVEPOINT zero_ruling_metric")
                        try:
                            cur.execute(
                                """
                                INSERT INTO data_quality_metrics
                                    (recorded_at, county, metric_name,
                                     metric_value, metadata)
                                VALUES (now(), %s, %s, %s, %s::jsonb)
                                """,
                                (
                                    county,
                                    "zero_ruling_extraction",
                                    1,
                                    json.dumps(
                                        {
                                            "document_id": document_id,
                                            "s3_key": event_data.get("s3_key"),
                                            "state": state,
                                            "scraper_id": event_data.get("scraper_id"),
                                            "extraction_method": extraction_method,
                                            "reason": orphan_result.reason,
                                        }
                                    ),
                                ),
                            )
                            cur.execute("RELEASE SAVEPOINT zero_ruling_metric")
                        except Exception:  # noqa: BLE001 — best-effort
                            cur.execute("ROLLBACK TO SAVEPOINT zero_ruling_metric")
                            raise
                except Exception:  # noqa: BLE001 — telemetry is best-effort
                    logger.debug(
                        "zero_ruling_extraction telemetry write failed",
                        exc_info=True,
                    )
            return False

        logger.info(
            "LLM extraction path: extracted %d ruling(s)",
            len(extracted_rulings),
            extra={
                "document_id": document_id,
                "ruling_count": len(extracted_rulings),
                "llm_latency_ms": llm_latency_ms,
                "extraction_method": extraction_method,
                "county": county,
                "state": state,
            },
        )

        # Convert ExtractedRuling objects to split events using the shared
        # multi-ruling guard logic (#2084).  For single-ruling documents the
        # full document text is the fallback; for multi-ruling documents the
        # guard returns None to prevent cross-contamination (#2057, #2078).
        converted = convert_extracted_rulings(
            extracted_rulings,
            document_id,
            fallback_text=ruling_text,
        )

        # When a multi-ruling document produces results but >= 50% of entries
        # are missing both judge and department, the header metadata was not
        # captured by the LLM — write a best-effort telemetry row so the
        # failure rate is dashboard-queryable (#3559, #3722).
        null_count = sum(1 for cr in converted if cr.judge_name is None and cr.department is None)
        if len(converted) > 1 and null_count / len(converted) >= 0.5:
            try:
                conn = self._get_connection()
                with conn.cursor() as cur:
                    cur.execute("SAVEPOINT multimodal_null_metadata_metric")
                    try:
                        cur.execute(
                            """
                            INSERT INTO data_quality_metrics
                                (recorded_at, county, metric_name,
                                 metric_value, metadata)
                            VALUES (now(), %s, %s, %s, %s::jsonb)
                            """,
                            (
                                county,
                                "multimodal_all_null_metadata",
                                1,
                                json.dumps(
                                    {
                                        "document_id": document_id,
                                        "s3_key": event_data.get("s3_key"),
                                        "state": state,
                                        "county": county,
                                        "scraper_id": event_data.get("scraper_id"),
                                        "ruling_count": len(converted),
                                        "null_count": null_count,
                                        "total_rulings": len(converted),
                                        "null_ratio": null_count / len(converted),
                                    }
                                ),
                            ),
                        )
                        cur.execute("RELEASE SAVEPOINT multimodal_null_metadata_metric")
                    except Exception:  # noqa: BLE001 — best-effort
                        cur.execute("ROLLBACK TO SAVEPOINT multimodal_null_metadata_metric")
                        raise
            except Exception:  # noqa: BLE001 — telemetry is best-effort
                logger.debug(
                    "multimodal_all_null_metadata telemetry write failed",
                    exc_info=True,
                )

        # When a document produces results but >= 30% of entries have a missing
        # or UNKNOWN- synthetic case_number, write a best-effort telemetry row
        # so the OC case_number miss rate is dashboard-queryable (#3729).
        miss_count = sum(
            1 for cr in converted if not cr.case_number or cr.case_number.startswith("UNKNOWN-")
        )
        if len(converted) > 0 and miss_count / len(converted) >= 0.3:
            try:
                conn = self._get_connection()
                with conn.cursor() as cur:
                    cur.execute("SAVEPOINT multimodal_case_number_miss_metric")
                    try:
                        cur.execute(
                            """
                            INSERT INTO data_quality_metrics
                                (recorded_at, county, metric_name,
                                 metric_value, metadata)
                            VALUES (now(), %s, %s, %s, %s::jsonb)
                            """,
                            (
                                county,
                                "multimodal_case_number_miss",
                                1,
                                json.dumps(
                                    {
                                        "document_id": document_id,
                                        "s3_key": event_data.get("s3_key"),
                                        "state": state,
                                        "county": county,
                                        "scraper_id": event_data.get("scraper_id"),
                                        "ruling_count": len(converted),
                                        "miss_count": miss_count,
                                        "miss_ratio": miss_count / len(converted),
                                    }
                                ),
                            ),
                        )
                        cur.execute("RELEASE SAVEPOINT multimodal_case_number_miss_metric")
                    except Exception:  # noqa: BLE001 — best-effort
                        cur.execute("ROLLBACK TO SAVEPOINT multimodal_case_number_miss_metric")
                        raise
            except Exception:  # noqa: BLE001 — telemetry is best-effort
                logger.debug(
                    "multimodal_case_number_miss telemetry write failed",
                    exc_info=True,
                )

        # Clean up stale split-child documents from a previous processing run
        # that produced more rulings than this run (#2295).  The new split IDs
        # will be upserted cleanly; any old IDs beyond the new count are
        # orphans.  This must happen before the split events are dispatched
        # so the DELETE does not race with the INSERT/UPSERT.
        #
        # This also handles the case where a previous multi-ruling run is
        # re-processed as a single ruling — all old UUID v5 split children
        # are cleaned up because the single-ruling's document_id is the
        # original UUID v4 ID, not a UUID v5 split ID.
        s3_key = event_data.get("s3_key")
        if s3_key:
            valid_ids = [cr.document_id for cr in converted]
            try:
                conn = self._get_connection()
                deleted = delete_stale_split_children(conn, s3_key, valid_ids)
                if deleted:
                    conn.commit()
                    logger.info(
                        "Cleaned up %d stale split-child document(s)",
                        deleted,
                        extra={
                            "document_id": document_id,
                            "s3_key": s3_key,
                            "new_split_count": len(converted),
                        },
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to clean up stale split children — continuing",
                    extra={
                        "document_id": document_id,
                        "s3_key": s3_key,
                        "error": str(exc),
                    },
                )

        # ------------------------------------------------------------------
        # Document-level deterministic validation: duplicate ruling text
        # lengths (#2350).  This check runs across all split rulings from
        # the same document before they are dispatched individually.
        # ------------------------------------------------------------------
        ruling_text_lengths: list[int | None] = [
            len(cr.ruling_text) if cr.ruling_text is not None else None for cr in converted
        ]
        dup_result = check_no_duplicate_ruling_text(ruling_text_lengths)
        if dup_result.result == "flag":
            logger.info(
                "Deterministic validation flag: duplicate ruling text lengths",
                extra={
                    "document_id": document_id,
                    "ruling_count": len(converted),
                    "ruling_text_lengths": ruling_text_lengths,
                    "reason": dup_result.reason,
                    "county": county,
                    "state": state,
                },
            )
            try:
                conn = self._get_connection()
                insert_validation_result(
                    conn,
                    document_id=document_id,
                    ruling_id=None,
                    result=ValidationResult(
                        result="flag",
                        reason=dup_result.reason or "",
                        model="deterministic",
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=0,
                    ),
                )
                conn.commit()
            except Exception as exc:
                logger.warning(
                    "Failed to log duplicate ruling text validation flag to DB: %s",
                    exc,
                )
                # Rollback to clear any aborted transaction state so
                # subsequent split-event DB writes can proceed (#2350).
                try:
                    conn.rollback()
                except Exception:
                    pass
            # Continue — flag does not block split event dispatch.

        # ------------------------------------------------------------------
        # Document-level deterministic validation: cross-case ruling text
        # misattribution (#2371).  For each split ruling, scan its text for
        # sibling case numbers.  A hit indicates the transcription step
        # likely assigned one case's ruling text to another case's metadata.
        # ------------------------------------------------------------------
        all_sibling_case_numbers: list[str] = [cr.case_number for cr in converted if cr.case_number]
        # Only run when we have 2+ sibling case numbers — otherwise there
        # is nothing to cross-reference.
        if len(all_sibling_case_numbers) >= 2:
            for cr in converted:
                # Siblings = all-except-self (by reference identity).
                sibling_nums = [
                    other.case_number
                    for other in converted
                    if other is not cr and other.case_number
                ]
                if not sibling_nums:
                    continue
                cross_result = check_no_cross_case_ruling_text(
                    ruling_text=cr.ruling_text,
                    own_case_number=cr.case_number,
                    other_case_numbers=sibling_nums,
                )
                if cross_result.result != "flag":
                    continue
                logger.info(
                    "Deterministic validation flag: cross-case ruling text",
                    extra={
                        "document_id": document_id,
                        "split_document_id": cr.document_id,
                        "own_case_number": cr.case_number,
                        "sibling_case_numbers": sibling_nums,
                        "reason": cross_result.reason,
                        "county": county,
                        "state": state,
                    },
                )
                try:
                    conn = self._get_connection()
                    insert_validation_result(
                        conn,
                        document_id=cr.document_id,
                        ruling_id=None,
                        result=ValidationResult(
                            result="flag",
                            reason=cross_result.reason or "",
                            model="deterministic",
                            input_tokens=0,
                            output_tokens=0,
                            latency_ms=0,
                        ),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "Failed to log cross-case ruling text validation flag to DB: %s",
                        exc,
                    )
                    # Rollback to clear any aborted transaction state so
                    # subsequent split-event DB writes can proceed (#2371).
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                # Continue — flag does not block split event dispatch.

        # NOTE: The per-ruling ``ruling_text_case_number_marker`` check (#2402)
        # lives inside ``run_deterministic_rules`` and runs during each split
        # event's recursive ``process_event`` call (see the deterministic
        # validation block earlier in this method).  It does not need a
        # document-level block here because it operates on a single ruling's
        # own text + case_number — no sibling context is required.  Keeping
        # it in the aggregated per-ruling path avoids double-flagging the
        # same ruling (once here and once in the split recursion).

        for cr in converted:
            # For multimodal-extracted PDFs, ruling_text is markdown.
            # Convert to HTML for ruling_text_html, strip for plain text.
            ruling_text_for_event = cr.ruling_text
            ruling_text_html_for_event = None
            if extraction_method == "multimodal" and cr.ruling_text:
                ruling_text_html_for_event = _markdown_to_html(cr.ruling_text)
                ruling_text_for_event = _strip_markdown(cr.ruling_text)

            split_event: dict[str, Any] = {
                **event_data,
                "document_id": cr.document_id,
                "_original_document_id": cr.original_document_id,
                "_split_processed": True,
                "_llm_extracted": True,
                "_split_index": cr.split_index,
                "_split_count": cr.split_count,
                # Fields from LLM extraction (fall back to event_data):
                "ruling_text": ruling_text_for_event,
                "ruling_text_html": ruling_text_html_for_event,
                "case_number": cr.case_number or event_data.get("case_number"),
                "case_title": cr.case_title or event_data.get("case_title"),
                "judge_name": cr.judge_name or event_data.get("judge_name"),
                "department": cr.department or event_data.get("department"),
                "motion_type": cr.motion_type or event_data.get("motion_type"),
                "outcome": cr.outcome or event_data.get("outcome"),
                "hearing_date": cr.hearing_date or event_data.get("hearing_date"),
                "parties": cr.parties if cr.parties else event_data.get("parties", []),
            }

            try:
                self.process_event(split_event)
            except Exception as _exc:
                from framework.llm_enrichment import LlmEnrichmentExhaustedError

                if not isinstance(_exc, LlmEnrichmentExhaustedError):
                    raise
                logger.critical(
                    "per-child enrichment exhausted on LLM split — ruling permanently lost",
                    extra={
                        "document_id": document_id,
                        "_split_index": cr.split_index,
                        "_split_count": cr.split_count,
                        "case_number": cr.case_number or event_data.get("case_number"),
                        "error": str(_exc),
                    },
                )

        return True

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

    def _cleanup_stale_consumers(self) -> None:
        """Remove stale consumers from the consumer group on startup.

        A consumer is considered stale when it has been idle for longer than
        ``STALE_CONSUMER_IDLE_MS`` *and* has zero pending messages.  The
        current process's consumer name is always preserved.

        If the ``XINFO CONSUMERS`` call fails (e.g. the stream doesn't exist
        yet), we log a warning and continue — cleanup is best-effort and must
        never block worker startup.
        """
        try:
            consumers: list[dict[str, object]] = self._redis.xinfo_consumers(
                STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP
            )
        except Exception as exc:
            logger.warning(
                "Could not list consumers for cleanup: %s",
                exc,
            )
            return

        deleted = 0
        for consumer in consumers:
            raw_name = consumer.get("name", b"")
            name: str = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
            idle: int = int(consumer.get("idle", 0))
            pending: int = int(consumer.get("pending", 0))

            if name == CONSUMER_NAME:
                continue

            if idle >= STALE_CONSUMER_IDLE_MS and pending == 0:
                try:
                    self._redis.xgroup_delconsumer(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, name)
                    deleted += 1
                    logger.info(
                        "Deleted stale consumer %s (idle %d ms)",
                        name,
                        idle,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to delete stale consumer %s: %s",
                        name,
                        exc,
                    )

        logger.info(
            "Stale consumer cleanup finished",
            extra={
                "total_consumers": len(consumers),
                "deleted": deleted,
            },
        )

    def _reclaim_pending_messages(self, batch_size: int = 100) -> int:
        """Reclaim abandoned pending messages from dead consumers.

        Uses XAUTOCLAIM to transfer ownership of messages that have been
        pending (unacknowledged) for longer than ``PENDING_RECLAIM_MIN_IDLE_MS``
        to this consumer.  The reclaimed messages are then processed normally.

        This handles the case where a consumer crash-looped on an infrastructure
        error (e.g. missing DB column), left messages unacknowledged in the PEL,
        and then the infrastructure was fixed.  Without reclamation, those
        messages would be stuck forever because ``XREADGROUP ... >`` only
        delivers new messages, not pending ones.

        Args:
            batch_size: Maximum number of messages to reclaim per call.

        Returns:
            Number of messages successfully reclaimed and processed.
        """
        processed = 0
        start_id = b"0-0"

        try:
            # XAUTOCLAIM returns (next_start_id, [(msg_id, data), ...], [deleted_ids])
            result = self._redis.xautoclaim(
                STREAM_DOCUMENT_CAPTURED,
                CONSUMER_GROUP,
                CONSUMER_NAME,
                min_idle_time=PENDING_RECLAIM_MIN_IDLE_MS,
                start_id=start_id,
                count=batch_size,
            )
        except Exception as exc:
            logger.warning("XAUTOCLAIM failed: %s", exc)
            return 0

        # XAUTOCLAIM returns a 3-tuple: (next_cursor, [(msg_id, data), ...], [deleted_ids]).
        # Guard against unexpected shapes gracefully.
        try:
            claimed_messages = result[1]
        except (IndexError, TypeError):
            return 0

        if not claimed_messages:
            return 0

        logger.info(
            "Reclaimed %d pending message(s) from PEL",
            len(claimed_messages),
        )

        for msg_id, data in claimed_messages:
            if data is None:
                # Message was deleted from the stream but still in PEL.
                # Acknowledge it to clear the PEL entry.
                logger.debug("Acknowledging deleted PEL entry %s", msg_id)
                self._redis.xack(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, msg_id)
                continue
            self._process_message(msg_id, data)
            processed += 1

        return processed

    def _process_batch(self, batch_size: int, block_ms: int) -> None:
        messages = self._redis.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            {STREAM_DOCUMENT_CAPTURED: ">"},
            count=batch_size,
            block=block_ms,
        )
        if not messages:
            self._empty_polls += 1
            if self._heartbeat_interval > 0 and self._empty_polls % self._heartbeat_interval == 0:
                idle_seconds = round(time.monotonic() - self._last_event_time)
                logger.log(
                    self._heartbeat_log_level,
                    "Heartbeat: idle for %d seconds (%d empty polls)",
                    idle_seconds,
                    self._empty_polls,
                    extra={
                        "consumer": CONSUMER_NAME,
                        "idle_seconds": idle_seconds,
                        "empty_polls": self._empty_polls,
                    },
                )
            return

        self._empty_polls = 0
        self._last_event_time = time.monotonic()
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
                if is_schema_constraint_error(exc):
                    # Deterministic schema/data mismatch — retrying will never
                    # succeed. Dead-letter immediately to avoid wasting LLM API
                    # calls on futile retries. See #2233.
                    logger.critical(
                        "Schema constraint error processing message %s: %s — "
                        "dead-lettering immediately (no retry)",
                        msg_id,
                        exc,
                    )
                    self._redis.xack(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, msg_id)
                    return
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
