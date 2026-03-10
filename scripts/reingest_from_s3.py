#!/usr/bin/env python3
"""Re-ingest existing documents from S3 through the (now-idempotent) pipeline.

For each document in the database, fetches the raw content from S3, re-runs
the scraper's parse_document() to extract fields with the current (improved)
extraction logic, and pushes a synthetic DocumentCapturedEvent through the
ingestion worker.

This is the "fix and re-run" mechanism: after improving a scraper's extraction
logic, run this script for the affected court/date range to update all records.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/reingest_from_s3.py \
            --county "Los Angeles" --date-from 2026-01-01

Options:
    --county NAME       Scope to documents from this county.
    --date-from DATE    Only re-ingest documents captured on or after this date.
    --date-to DATE      Only re-ingest documents captured on or before this date.
    --dry-run           Parse and show what would be updated, but don't write to DB.
    --batch-size N      Number of documents per batch (default: 25).
    --limit N           Maximum total documents to re-ingest.
    --concurrency N     Number of parallel S3 fetch threads (default: 10).
    --parse-workers N   Number of parallel scraper parse threads (default: 4).
    --parse-timeout N   Per-document parse timeout in seconds (default: 60).
    --no-llm            Disable LLM extraction, use regex-only mode.
    --llm-timeout N     Per-call LLM API timeout in seconds (default: 60).
    --force-llm         Force LLM even when all fields are already populated.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import psycopg  # noqa: E402
import structlog  # noqa: E402

from framework.models import CapturedDocument, ContentFormat  # noqa: E402
from ingestion.db import (  # noqa: E402
    batch_upsert_parties,
    insert_document,
    insert_ruling,
    resolve_judge,
    upsert_case,
    upsert_case_judge,
)
from ingestion.extract import (  # noqa: E402
    extract_case_number,
    extract_case_title,
    extract_hearing_date,
    extract_judge_name,
    extract_motion_type,
    extract_outcome,
)
from ingestion.llm_extract import (  # noqa: E402
    LLMExtractionResult,
    LLMRulingResult,
    extract_fields_llm,
)
from ingestion.llm_providers import create_client as create_llm_client  # noqa: E402

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
        if sys.stderr.isatty()
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


# Registry mapping scraper_id to scraper class for parse_document()
_SCRAPER_REGISTRY: dict[str, type] = {}


def _load_scraper_registry() -> None:
    """Lazily populate the scraper registry by auto-discovering court modules.

    Scans the ``courts/`` package tree for modules that expose a
    ``default_config()`` function and a ``BaseScraper`` subclass.  For each
    such module the ``scraper_id`` from the config is mapped to the scraper
    class.  This eliminates the need to maintain a hardcoded import list —
    adding a new scraper module automatically registers it.
    """
    if _SCRAPER_REGISTRY:
        return

    import importlib
    import inspect
    import pkgutil

    try:
        import courts  # noqa: E402
    except ImportError:
        logger.warning("courts package not importable — scraper registry empty")
        return

    from framework.base import BaseScraper  # noqa: E402

    for importer, modname, ispkg in pkgutil.walk_packages(
        courts.__path__, prefix="courts."
    ):
        if ispkg:
            continue
        try:
            mod = importlib.import_module(modname)
        except Exception:
            logger.debug("Could not import module, skipping", module=modname)
            continue

        config_fn = getattr(mod, "default_config", None)
        if config_fn is None or not callable(config_fn):
            continue

        # Find the concrete BaseScraper subclass in this module.
        scraper_cls: type | None = None
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, BaseScraper)
                and obj is not BaseScraper
                and obj.__module__ == mod.__name__
            ):
                scraper_cls = obj
                break

        if scraper_cls is None:
            continue

        try:
            config = config_fn()
            _SCRAPER_REGISTRY[config.scraper_id] = scraper_cls
        except Exception:
            logger.warning(
                "default_config() failed, skipping", module=modname, exc_info=True
            )


FETCH_DOCUMENTS_QUERY = """
    SELECT
        d.id, d.case_id, d.court_id, d.s3_key, d.s3_bucket,
        d.content_hash, d.source_url, d.scraper_id, d.captured_at,
        d.hearing_date, d.format,
        ct.state, ct.county, ct.court_name,
        c.case_number, c.case_title
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN cases c ON c.id = d.case_id
    WHERE d.status = 'active'
    {filters}
    AND (d.captured_at, d.id) > (%s, %s)
    ORDER BY d.captured_at, d.id
    LIMIT %s
"""

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"


def _build_filters(
    county: str | None,
    date_from: date | None,
    date_to: date | None,
) -> tuple[str, list]:
    """Build WHERE clause fragments and params for the document query."""
    clauses = []
    params: list = []
    if county:
        clauses.append("AND ct.county = %s")
        params.append(county)
    if date_from:
        clauses.append("AND d.captured_at >= %s")
        params.append(datetime.combine(date_from, datetime.min.time()))
    if date_to:
        clauses.append("AND d.captured_at <= %s")
        params.append(datetime.combine(date_to, datetime.max.time()))
    return " ".join(clauses), params


def _fetch_s3_content(s3_client: object, bucket: str, key: str) -> bytes:
    """Fetch raw content from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
    return response["Body"].read()  # type: ignore[index]


def _extract_pdf_text_subprocess(
    raw_content: bytes,
    timeout: float = 30.0,
) -> str | None:
    """Extract text from PDF using pdfplumber in a subprocess with hard timeout.

    Runs pdfplumber in a separate process so that if the C PDF parser hangs,
    the OS can kill it.  ``PyThreadState_SetAsyncExc`` does not work for C
    extensions — this subprocess approach is the only reliable timeout.

    Returns the extracted text, or ``None`` if extraction failed or timed out.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        os.write(tmp_fd, raw_content)
        os.close(tmp_fd)

        # Find the Python interpreter from the current environment.
        python = sys.executable

        result = subprocess.run(
            [
                python,
                "-c",
                (
                    "import pdfplumber,sys\n"
                    "pdf=pdfplumber.open(sys.argv[1])\n"
                    "for p in pdf.pages:\n"
                    "    t=p.extract_text()\n"
                    "    if t:\n"
                    "        print(t)\n"
                ),
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return None
    except subprocess.TimeoutExpired:
        logger.debug("PDF subprocess timed out", timeout_seconds=timeout)
        return None
    except Exception:
        logger.debug("PDF subprocess extraction failed", exc_info=True)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _extract_text_from_content(
    raw_content: bytes,
    doc_format: str,
    pdf_timeout: float = 30.0,
) -> str:
    """Extract readable text from raw document content.

    For PDF documents, uses pdfplumber in a **subprocess** with a hard
    timeout to prevent hangs from C extensions.  The previous in-process
    approach using ``PyThreadState_SetAsyncExc`` could not interrupt
    pdfplumber's C PDF parser, leading to hung threads and blocked batches.

    For other formats (HTML, plain text), decodes as UTF-8.
    """
    if doc_format == "pdf":
        text = _extract_pdf_text_subprocess(raw_content, timeout=pdf_timeout)
        if text and text.strip():
            return text
        # Subprocess failed (timeout, crash, or empty output) — fall back
        # to UTF-8 decode rather than risking an in-process hang.
        logger.debug(
            "PDF subprocess extraction returned no text, falling back to UTF-8"
        )
    return raw_content.decode("utf-8", errors="replace")


def _match_ruling(
    llm_result: LLMExtractionResult,
    case_number: str | None,
) -> LLMRulingResult | None:
    """Find the ruling matching the given case_number, or return the first ruling.

    Mirrors the helper in ``worker.py``: if the document already has a
    case_number from the scraper, look for a matching ruling in the LLM
    results. If no match, fall back to the first ruling (the LLM may have
    normalized the case number differently).
    """
    if not llm_result.rulings:
        return None
    if case_number:
        matching = [r for r in llm_result.rulings if r.case_number == case_number]
        if matching:
            return matching[0]
    return llm_result.rulings[0]


def _reparse_document(
    raw_content: bytes,
    scraper_id: str,
    doc_meta: dict,
    pdf_timeout: float = 30.0,
    llm_client: object | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_timeout: float | None = 60.0,
    force_llm: bool = False,
) -> dict:
    """Re-parse a document using a three-tier extraction strategy.

    Extraction priority per field:
      1. Scraper ``parse_document()`` (highest priority).
      2. LLM extraction via ``extract_fields_llm()`` (if *llm_client*
         is provided and fields are missing, or *force_llm* is True).
      3. Regex fallback (lowest priority).

    For PDF documents, extracts text via pdfplumber in a subprocess with
    a hard timeout to prevent hangs from C extensions.

    Returns a dict of extracted fields plus an ``extraction_methods`` dict
    recording which method populated each field, and an ``llm_skipped``
    boolean indicating whether LLM extraction was skipped because all
    fields were already populated.
    """
    _load_scraper_registry()

    doc_format = doc_meta.get("format", "html")
    text = _extract_text_from_content(
        raw_content, doc_format, pdf_timeout=pdf_timeout
    ).replace("\x00", "")
    extracted: dict = {
        "ruling_text": text,
        "case_number": doc_meta.get("case_number"),
        "case_title": doc_meta.get("case_title"),
        "judge_name": None,
        "outcome": None,
        "motion_type": None,
        "department": None,
        "parties": [],
        "hearing_date": doc_meta.get("hearing_date"),
    }

    scraper_cls = _SCRAPER_REGISTRY.get(scraper_id)
    if scraper_cls:
        # Create a CapturedDocument and run parse_document
        try:
            from framework.models import ScraperConfig

            config = ScraperConfig(
                scraper_id=scraper_id,
                state=doc_meta["state"],
                county=doc_meta["county"],
                court=doc_meta["court_name"],
                target_urls=[],
            )
            scraper = scraper_cls(config=config)
            cap_doc = CapturedDocument(
                document_id=doc_meta["document_id"],
                scraper_id=scraper_id,
                state=doc_meta["state"],
                county=doc_meta["county"],
                court=doc_meta["court_name"],
                source_url=doc_meta["source_url"],
                capture_timestamp=doc_meta["captured_at"],
                content_format=ContentFormat(doc_meta["format"]),
                raw_content=raw_content,
                content_hash=doc_meta["content_hash"],
            )
            parsed = scraper.parse_document(cap_doc)
            ruling = parsed.ruling_text or text
            extracted["ruling_text"] = ruling.replace("\x00", "") if ruling else text
            extracted["case_number"] = parsed.case_number or extracted["case_number"]
            extracted["case_title"] = parsed.case_title or extracted["case_title"]
            extracted["judge_name"] = parsed.judge_name
            extracted["outcome"] = parsed.outcome
            extracted["motion_type"] = parsed.motion_type
            extracted["department"] = parsed.department
            extracted["parties"] = parsed.parties
            if parsed.hearing_date:
                extracted["hearing_date"] = (
                    parsed.hearing_date.date()
                    if isinstance(parsed.hearing_date, datetime)
                    else parsed.hearing_date
                )
        except Exception:
            logger.warning(
                "Scraper parse_document failed, falling back to regex",
                document_id=doc_meta["document_id"],
                exc_info=True,
            )

    # Track extraction method per field for observability.
    extraction_methods: dict[str, str] = {}

    # Record which fields were filled by the scraper.
    for field in (
        "judge_name",
        "outcome",
        "motion_type",
        "case_number",
        "case_title",
        "hearing_date",
        "department",
        "parties",
    ):
        val = extracted.get(field)
        if val and (not isinstance(val, list) or len(val) > 0):
            extraction_methods[field] = "scraper"

    # ------------------------------------------------------------------
    # LLM extraction — secondary method for missing fields
    # ------------------------------------------------------------------
    llm_skipped = False
    llm_outcome = "not_attempted"
    if llm_client is not None:
        missing_fields = [
            f
            for f in (
                "hearing_date",
                "outcome",
                "motion_type",
                "case_number",
                "case_title",
                "judge_name",
                "department",
                "parties",
            )
            if not extracted.get(f)
            or (isinstance(extracted.get(f), list) and len(extracted[f]) == 0)
        ]
        if not missing_fields and not force_llm:
            logger.info(
                "All fields present, skipping LLM extraction",
                document_id=doc_meta["document_id"],
            )
            llm_skipped = True
            llm_outcome = "skipped"
        elif (missing_fields or force_llm) and text.strip():
            t0 = time.monotonic()
            llm_result = extract_fields_llm(
                document_text=text,
                content_format=doc_format,
                metadata=None,
                client=llm_client,
                provider=llm_provider,
                model=llm_model,
                timeout=llm_timeout,
            )
            llm_latency_ms = round((time.monotonic() - t0) * 1000)

            if llm_result is not None:
                ruling = _match_ruling(llm_result, extracted.get("case_number"))

                # Apply document-level fields from LLM
                if not extracted["hearing_date"] and llm_result.hearing_date:
                    extracted["hearing_date"] = llm_result.hearing_date
                    extraction_methods["hearing_date"] = "llm"
                if not extracted["judge_name"] and llm_result.judge_name:
                    extracted["judge_name"] = llm_result.judge_name
                    extraction_methods["judge_name"] = "llm"
                if not extracted.get("department") and llm_result.department:
                    extracted["department"] = llm_result.department
                    extraction_methods["department"] = "llm"

                # Apply ruling-level fields from the matched ruling
                if ruling is not None:
                    if not extracted["case_number"] and ruling.case_number:
                        extracted["case_number"] = ruling.case_number
                        extraction_methods["case_number"] = "llm"
                    if not extracted["case_title"] and ruling.case_title:
                        extracted["case_title"] = ruling.case_title
                        extraction_methods["case_title"] = "llm"
                    if not extracted["outcome"] and ruling.outcome:
                        extracted["outcome"] = ruling.outcome
                        extraction_methods["outcome"] = "llm"
                    if not extracted["motion_type"] and ruling.motion_type:
                        extracted["motion_type"] = ruling.motion_type
                        extraction_methods["motion_type"] = "llm"
                    if not extracted["parties"] and ruling.parties:
                        extracted["parties"] = ruling.parties
                        extraction_methods["parties"] = "llm"

                llm_outcome = "success"
                logger.info(
                    "LLM extraction completed",
                    document_id=doc_meta["document_id"],
                    latency_ms=llm_latency_ms,
                    methods=extraction_methods,
                )
            else:
                llm_outcome = "failure"
                logger.info(
                    "LLM extraction returned None, falling back to regex",
                    document_id=doc_meta["document_id"],
                    latency_ms=llm_latency_ms,
                )

    # ------------------------------------------------------------------
    # Regex fallback — fill any fields still missing after scraper + LLM
    # ------------------------------------------------------------------
    # For PDF documents, ``text`` is pdfplumber-extracted text (not garbage
    # UTF-8 decode), so regex patterns can match real content.
    if not extracted["judge_name"]:
        val = extract_judge_name(text)
        if val:
            extracted["judge_name"] = val
            extraction_methods.setdefault("judge_name", "regex")
    if not extracted["outcome"]:
        val = extract_outcome(text)
        if val:
            extracted["outcome"] = val
            extraction_methods.setdefault("outcome", "regex")
    if not extracted["motion_type"]:
        val = extract_motion_type(text)
        if val:
            extracted["motion_type"] = val
            extraction_methods.setdefault("motion_type", "regex")
    if not extracted["case_number"]:
        val = extract_case_number(text)
        if val:
            extracted["case_number"] = val
            extraction_methods.setdefault("case_number", "regex")
    if not extracted["case_title"]:
        val = extract_case_title(text)
        if val:
            extracted["case_title"] = val
            extraction_methods.setdefault("case_title", "regex")
    if not extracted["hearing_date"]:
        val = extract_hearing_date(text)
        if val:
            extracted["hearing_date"] = val
            extraction_methods.setdefault("hearing_date", "regex")

    if extraction_methods:
        logger.info(
            "Field extraction summary",
            document_id=doc_meta["document_id"],
            methods=extraction_methods,
        )

    extracted["extraction_methods"] = extraction_methods
    extracted["llm_skipped"] = llm_skipped
    extracted["llm_outcome"] = llm_outcome
    return extracted


def reingest_batch(
    conn: psycopg.Connection,
    s3_client: object,
    batch_size: int,
    cursor: tuple[datetime, str],
    filters: str,
    filter_params: list,
    dry_run: bool = False,
    concurrency: int = 10,
    parse_workers: int = 4,
    parse_timeout: float = 60.0,
    llm_client: object | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_timeout: float | None = 60.0,
    force_llm: bool = False,
    running_processed: int = 0,
    running_updated: int = 0,
    batch_number: int = 0,
) -> dict[str, Any]:
    """Process one batch. Returns a dict of batch stats.

    Returned dict keys:
      - ``processed``: number of documents iterated over
      - ``updated``: number of documents successfully written to DB
      - ``llm_skipped``: number of documents where LLM was skipped
      - ``next_cursor``: cursor for the next batch
      - ``failed``: number of documents that failed DB writes
      - ``skipped``: number of documents skipped (no S3 key, fetch failure, parse failure)
      - ``llm_success``: LLM extraction successes
      - ``llm_failure``: LLM extraction failures
      - ``batch_number``: batch sequence number

    S3 objects are fetched in parallel using a thread pool (controlled by
    ``concurrency``).  Scraper parsing is parallelised with ``parse_workers``
    threads.  Each parse call is guarded by a ``parse_timeout`` (seconds).
    DB writes remain sequential.

    Each document is committed individually so that a crash mid-batch does
    not lose already-processed documents.  The DB writes are idempotent
    (upserts), so partial batch commits are safe.

    *running_processed* and *running_updated* are cumulative totals from
    prior batches, used for progress logging.

    If *llm_client* is provided, LLM extraction is used for fields
    that the scraper did not populate, before falling back to regex.
    If *force_llm* is True, LLM extraction runs even when all fields
    are already populated.
    """
    processed = 0
    updated = 0
    llm_skipped = 0
    failed = 0
    skipped = 0
    llm_success = 0
    llm_failure = 0
    next_cursor = cursor

    params = filter_params + [cursor[0], cursor[1], batch_size]

    with conn.cursor() as cur:
        cur.execute(
            FETCH_DOCUMENTS_QUERY.format(filters=filters),
            params,
        )
        rows = cur.fetchall()

    if not rows:
        return {
            "processed": 0,
            "updated": 0,
            "llm_skipped": 0,
            "next_cursor": cursor,
            "failed": 0,
            "skipped": 0,
            "llm_success": 0,
            "llm_failure": 0,
            "batch_number": batch_number,
        }

    # --- Prefetch S3 content in parallel -----------------------------------
    # Parallel S3 fetch — submit all rows with valid s3_key + s3_bucket,
    # then collect results keyed by row index.
    s3_results: dict[int, bytes] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        for idx, row in enumerate(rows):
            s3_key = row[3]
            s3_bucket = row[4]
            if s3_key and s3_bucket:
                future = pool.submit(_fetch_s3_content, s3_client, s3_bucket, s3_key)
                futures[future] = idx

        for future in as_completed(futures):
            idx = futures[future]
            doc_id_str = str(rows[idx][0])
            try:
                s3_results[idx] = future.result()
            except Exception:
                logger.warning(
                    "Failed to fetch S3 content, skipping",
                    document_id=doc_id_str,
                    exc_info=True,
                )

    # --- Build doc_meta for rows with fetched content -----------------------
    parseable: list[tuple[int, dict, bytes]] = []  # (idx, doc_meta, raw_content)

    for idx, row in enumerate(rows):
        (
            doc_id,
            case_id,
            court_id,
            s3_key,
            s3_bucket,
            content_hash,
            source_url,
            scraper_id,
            captured_at,
            hearing_date,
            doc_format,
            state,
            county,
            court_name,
            case_number,
            case_title,
        ) = row
        processed += 1
        doc_id_str = str(doc_id)
        next_cursor = (captured_at, doc_id_str)

        if not s3_key or not s3_bucket:
            logger.warning(
                "Document has no S3 key/bucket, skipping", document_id=doc_id_str
            )
            skipped += 1
            continue

        raw_content = s3_results.get(idx)
        if raw_content is None:
            # S3 fetch failed or was not attempted
            skipped += 1
            continue

        doc_meta = {
            "document_id": doc_id_str,
            "state": state,
            "county": county,
            "court_name": court_name,
            "source_url": source_url,
            "captured_at": captured_at,
            "content_hash": content_hash,
            "format": doc_format,
            "case_number": case_number,
            "case_title": case_title,
            "hearing_date": hearing_date,
            "court_id": str(court_id),
            "scraper_id": scraper_id,
            "s3_key": s3_key,
            "s3_bucket": s3_bucket,
        }

        parseable.append((idx, doc_meta, raw_content))

    if not parseable:
        return {
            "processed": processed,
            "updated": updated,
            "llm_skipped": llm_skipped,
            "next_cursor": next_cursor,
            "failed": failed,
            "skipped": skipped,
            "llm_success": llm_success,
            "llm_failure": llm_failure,
            "batch_number": batch_number,
        }

    # --- Parse documents in parallel ------------------------------------------
    # Parsing runs in threads.  The subprocess-based timeout inside
    # ``_extract_text_from_content`` provides hard isolation for pdfplumber's
    # C extension — if the PDF parser hangs, the subprocess is killed by the
    # OS after ``parse_timeout`` seconds.  No thread-level timeout hacks are
    # needed here.
    parsed_docs: list[tuple[dict, dict]] = []  # (doc_meta, extracted)

    with ThreadPoolExecutor(max_workers=parse_workers) as pool:
        parse_futures = {}
        for idx, doc_meta, raw_content in parseable:
            future = pool.submit(
                _reparse_document,
                raw_content,
                doc_meta["scraper_id"],
                doc_meta,
                parse_timeout,
                llm_client,
                llm_provider,
                llm_model,
                llm_timeout,
                force_llm,
            )
            parse_futures[future] = (idx, doc_meta)

        for doc_index, future in enumerate(as_completed(parse_futures)):
            idx, doc_meta = parse_futures[future]
            doc_id_str = doc_meta["document_id"]
            try:
                extracted = future.result()
            except Exception:
                logger.warning(
                    "Parse failed, skipping",
                    document_id=doc_id_str,
                    exc_info=True,
                )
                skipped += 1
                continue

            if extracted.get("llm_skipped"):
                llm_skipped += 1

            # Track LLM outcomes
            doc_llm_outcome = extracted.get("llm_outcome", "not_attempted")
            if doc_llm_outcome == "success":
                llm_success += 1
            elif doc_llm_outcome == "failure":
                llm_failure += 1

            logger.info(
                "document_progress",
                document_index=doc_index,
                document_id=doc_id_str,
                county=doc_meta["county"],
                case_number=doc_meta.get("case_number"),
                llm_outcome=doc_llm_outcome,
            )

            if dry_run:
                logger.info(
                    "DRY-RUN",
                    document_id=doc_id_str,
                    county=doc_meta["county"],
                    judge=extracted["judge_name"],
                    outcome=extracted["outcome"],
                    motion_type=extracted["motion_type"],
                    case_title=extracted["case_title"],
                    case_number=extracted["case_number"],
                    parties_count=len(extracted["parties"]),
                )
                continue

            parsed_docs.append((doc_meta, extracted))

    if dry_run or not parsed_docs:
        return {
            "processed": processed,
            "updated": updated,
            "llm_skipped": llm_skipped,
            "next_cursor": next_cursor,
            "failed": failed,
            "skipped": skipped,
            "llm_success": llm_success,
            "llm_failure": llm_failure,
            "batch_number": batch_number,
        }

    # --- DB writes — commit after each document ------------------------------
    # Each document is written inside a savepoint and then committed
    # individually.  This ensures that a crash mid-batch does not lose
    # already-processed documents.  The DB writes are idempotent (upserts),
    # so partial batch commits are safe.
    for doc_meta, extracted in parsed_docs:
        doc_id_str = doc_meta["document_id"]
        court_id_str = doc_meta["court_id"]

        try:
            with conn.transaction():
                effective_case_number = (
                    extracted["case_number"]
                    or doc_meta["case_number"]
                    or f"UNKNOWN-{doc_id_str}"
                )
                new_case_id = upsert_case(
                    conn,
                    effective_case_number,
                    court_id_str,
                    case_title=extracted["case_title"],
                )

                effective_hearing = (
                    extracted["hearing_date"] or doc_meta["hearing_date"]
                )
                insert_document(
                    conn,
                    document_id=doc_id_str,
                    case_id=new_case_id,
                    court_id=court_id_str,
                    content_format=doc_meta["format"],
                    content_hash=doc_meta["content_hash"],
                    s3_key=doc_meta["s3_key"],
                    s3_bucket=doc_meta["s3_bucket"],
                    source_url=doc_meta["source_url"],
                    scraper_id=doc_meta["scraper_id"],
                    captured_at=doc_meta["captured_at"],
                    hearing_date=effective_hearing,
                )

                # Resolve judge
                judge_id = None
                if extracted["judge_name"]:
                    judge_id = resolve_judge(
                        conn, extracted["judge_name"], court_id_str
                    )

                # Upsert ruling
                if effective_hearing is not None:
                    ruling_text = extracted["ruling_text"]
                    # Clean ruling text if it's the full raw HTML — take first 50k chars
                    if ruling_text and len(ruling_text) > 50000:
                        ruling_text = ruling_text[:50000]

                    insert_ruling(
                        conn,
                        document_id=doc_id_str,
                        case_id=new_case_id,
                        court_id=court_id_str,
                        hearing_date=effective_hearing,
                        ruling_text=ruling_text,
                        department=extracted["department"],
                        judge_id=judge_id,
                        outcome=extracted["outcome"],
                        motion_type=extracted["motion_type"],
                    )

                if judge_id:
                    upsert_case_judge(conn, new_case_id, judge_id, effective_hearing)

                # Parties (batched — O(1) queries regardless of party count)
                batch_upsert_parties(conn, new_case_id, extracted.get("parties", []))

            # Commit immediately so this document is durable even if a
            # later document (or the process) crashes.
            conn.commit()
            updated += 1

            logger.info(
                "Committed document",
                document_id=doc_id_str,
                total_processed=running_processed + processed,
                total_updated=running_updated + updated,
            )

        except Exception:
            failed += 1
            logger.error(
                "DB write failed, skipping (batch continues)",
                document_id=doc_id_str,
                exc_info=True,
            )

    return {
        "processed": processed,
        "updated": updated,
        "llm_skipped": llm_skipped,
        "next_cursor": next_cursor,
        "failed": failed,
        "skipped": skipped,
        "llm_success": llm_success,
        "llm_failure": llm_failure,
        "batch_number": batch_number,
    }


def run_reingest(
    dsn: str,
    *,
    county: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    batch_size: int = 25,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
    parse_workers: int = 4,
    parse_timeout: float = 60.0,
    no_llm: bool = False,
    llm_timeout: float | None = 60.0,
    force_llm: bool = False,
) -> dict[str, int]:
    """Run the full reingest. Returns summary stats."""
    filters, filter_params = _build_filters(county, date_from, date_to)

    s3_client = boto3.client("s3")

    # Create LLM client for extraction (shared across batches for connection
    # reuse).  Respects LLM_PROVIDER and LLM_MODEL env vars via
    # create_llm_client().  If --no-llm is specified or no API key is
    # available for the configured provider, LLM extraction is skipped.
    llm_client: object | None = None
    llm_provider: str | None = os.environ.get("LLM_PROVIDER")
    llm_model: str | None = os.environ.get("LLM_MODEL")
    if not no_llm:
        llm_client = create_llm_client(provider=llm_provider)
    if llm_client is not None:
        logger.info(
            "LLM extraction enabled",
            provider=llm_provider or "default",
            model=llm_model or "default",
            force=force_llm,
        )
    else:
        reason = "--no-llm flag" if no_llm else "no API key for configured provider"
        logger.info("LLM extraction disabled, using regex-only mode", reason=reason)
    total_processed = 0
    total_updated = 0
    total_llm_skipped = 0
    total_failed = 0
    total_skipped = 0
    total_batches = 0
    total_llm_success = 0
    total_llm_failure = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)
    t0 = time.monotonic()

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            total_batches += 1

            logger.info(
                "batch_start",
                batch_number=total_batches,
                batch_size=effective_batch,
            )

            batch_result = reingest_batch(
                conn,
                s3_client,
                effective_batch,
                cursor,
                filters,
                filter_params,
                dry_run=dry_run,
                concurrency=concurrency,
                parse_workers=parse_workers,
                parse_timeout=parse_timeout,
                llm_client=llm_client,
                llm_provider=llm_provider,
                llm_model=llm_model,
                llm_timeout=llm_timeout,
                force_llm=force_llm,
                running_processed=total_processed,
                running_updated=total_updated,
                batch_number=total_batches,
            )
            processed = batch_result["processed"]
            updated = batch_result["updated"]
            cursor = batch_result["next_cursor"]
            total_processed += processed
            total_updated += updated
            total_llm_skipped += batch_result["llm_skipped"]
            total_failed += batch_result["failed"]
            total_skipped += batch_result["skipped"]
            total_llm_success += batch_result["llm_success"]
            total_llm_failure += batch_result["llm_failure"]

            if dry_run:
                conn.rollback()

            logger.info(
                "batch_complete",
                batch_number=total_batches,
                processed=processed,
                updated=updated,
                llm_skipped=batch_result["llm_skipped"],
                total_processed=total_processed,
                total_updated=total_updated,
                total_llm_skipped=total_llm_skipped,
                mode="dry-run" if dry_run else "committed",
            )

            if processed < effective_batch:
                break

    wall_time = round(time.monotonic() - t0, 2)

    logger.info(
        "reingest_complete",
        total_processed=total_processed,
        total_updated=total_updated,
        total_llm_skipped=total_llm_skipped,
        total_failed=total_failed,
        total_skipped=total_skipped,
        total_batches=total_batches,
        llm_success=total_llm_success,
        llm_failure=total_llm_failure,
        wall_time_seconds=wall_time,
    )

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
        "total_llm_skipped": total_llm_skipped,
        "total_failed": total_failed,
        "total_skipped": total_skipped,
        "total_batches": total_batches,
        "llm_success": total_llm_success,
        "llm_failure": total_llm_failure,
        "wall_time_seconds": wall_time,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-ingest documents from S3 with improved extraction.",
    )
    parser.add_argument(
        "--county", type=str, default=None, help="Scope to this county."
    )
    parser.add_argument(
        "--date-from", type=str, default=None, help="YYYY-MM-DD start date."
    )
    parser.add_argument(
        "--date-to", type=str, default=None, help="YYYY-MM-DD end date."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse but don't update DB."
    )
    parser.add_argument(
        "--batch-size", type=int, default=25, help="Batch size (default: 25)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max documents to process."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Number of parallel S3 fetch threads (default: 10).",
    )
    parser.add_argument(
        "--parse-workers",
        type=int,
        default=4,
        help="Number of parallel scraper parse threads (default: 4).",
    )
    parser.add_argument(
        "--parse-timeout",
        type=float,
        default=60.0,
        help="Per-document parse timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM extraction, use regex-only mode.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=60.0,
        help="Per-call LLM API timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--force-llm",
        action="store_true",
        help="Force LLM extraction even when all fields are already populated.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    date_from = date.fromisoformat(args.date_from) if args.date_from else None
    date_to = date.fromisoformat(args.date_to) if args.date_to else None

    stats = run_reingest(
        dsn,
        county=args.county,
        date_from=date_from,
        date_to=date_to,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        parse_workers=args.parse_workers,
        parse_timeout=args.parse_timeout,
        no_llm=args.no_llm,
        llm_timeout=args.llm_timeout,
        force_llm=args.force_llm,
    )

    logger.info(
        "Reingest complete",
        total_processed=stats["total_processed"],
        total_updated=stats["total_updated"],
        total_llm_skipped=stats["total_llm_skipped"],
    )


if __name__ == "__main__":
    main()
