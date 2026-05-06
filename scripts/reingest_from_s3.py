#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Re-ingest existing documents from S3 through the pipeline.

**This script operates on existing database records only.** It queries the
``documents`` table for matching rows, then re-processes each one through the
current extraction logic.  If the county/court has no records in the database
yet (e.g. a newly added county with S3 data but no prior ingestion), this
script will process 0 documents.  For initial population from S3, use
``rebuild_db.py --county <name>`` instead — it discovers documents directly
from S3 keys and does not require pre-existing database records.

**Idempotency (#2490):** This script is a drop-in idempotent counterpart of
the live worker path — known divergences have been resolved.

**Resolved divergences:**

-   ``FETCH_DOCUMENTS_QUERY`` now selects ``judge_name`` (via
    ``rulings.judge_id`` -> ``judges.canonical_name``) and ``department``
    (from ``rulings.department``) and propagates them into ``doc_meta`` so
    the LLM-cache metadata footprint matches the worker path for the same
    raw PDF (#2501).

-   The multimodal branch no longer falls back to
    ``doc_meta.get("case_title")`` when the LLM returns no title (#2502).
    Previously, this fallback pulled the existing DB title (carried via
    the LEFT JOIN in ``FETCH_DOCUMENTS_QUERY``) into the result, and
    combined with ``force_update=True`` on ``upsert_case`` created a
    self-sustaining fixed point for wrong titles: once a bad title was
    stored for a case, every reingest where the LLM returned no title
    re-wrote that same wrong title back.  Now ``case_title`` is ``None``
    when the LLM returns nothing, and the ``COALESCE(EXCLUDED.case_title,
    cases.case_title)`` semantic at ``db.py:316`` preserves the existing
    non-null title instead of erasing it.

-   The text-split branch of ``_full_reparse_document`` (the regex- or
    LLM-split path used by counties like Fresno and Contra Costa) also
    no longer falls back to ``doc_meta.get("case_title")`` when the
    split produces no title (#2521).  Same fixed-point pattern, same
    fix — let ``None`` flow through and rely on the ``db.py:316``
    COALESCE safeguard.  Note: the scaffold path at
    ``_reparse_document`` line ~797 *does* still initialise
    ``case_title`` from ``doc_meta`` as the starting point, because a
    per-scraper ``parse_document()`` overwrite immediately replaces it
    with the parser's output; that pattern is intentional and distinct
    from the split-branch bug.

-   ``case_number`` is treated differently from ``case_title`` in the
    split branch: it keeps its ``doc_meta`` fallback because it is an
    identity anchor (``cases`` is keyed on ``(court_id,
    case_number)``), not descriptive metadata.  Replacing a real number
    with ``UNKNOWN-<uuid>`` would orphan the ruling from its existing
    case row.  The LLM always wins when it produces a case_number, so
    the fixed-point risk that affected ``case_title`` does not apply.

-   ``_reparse_document`` and ``_full_reparse_document`` now honor
    ``ExtractionMethod.NONE`` (#4056).  Previously, the script consulted
    ``get_county_extraction_config`` only to read ``max_output_tokens``
    (#2355) and ran the LLM regardless of the configured ``method``.
    That mismatch caused NONE-configured counties (Federal CourtListener
    clusters, #3967; San Diego calendar override, #2331) to be re-run
    through the CA-tuned LLM prompt during reingest, producing
    truncation, JSON-parse errors, and field contamination.  Both reparse
    entry points now skip ``extract_fields_llm`` (and any registered
    ``llm_split_fn``) when the lookup returns ``ExtractionMethod.NONE``,
    matching the live worker guard at ``worker.py:1719-1732``.

For cleanup of corrupted ``derived.*`` state, prefer ``rebuild_db.py
--county <name>`` over reingest — rebuild walks S3 directly and does not
touch existing ``cases.*`` rows when the split set changes across runs.

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
    --case-number-like PATTERN
                        Only re-ingest documents whose associated case_number
                        matches this PostgreSQL LIKE pattern.  Useful for
                        targeting placeholder case numbers, e.g.
                        --case-number-like 'UNKNOWN-%%'
    --case-title-regex PATTERN
                        Only re-ingest documents whose current case_title
                        matches this PostgreSQL regex (~ operator).  Useful
                        for targeting garbled titles, e.g.
                        --case-title-regex 'vs\\.?\\s*$|(?i)(Before the Court|moves the)'
    --null-motion-type  Only re-ingest documents whose associated rulings
                        have NULL motion_type.  Useful after a normalization
                        backfill that set unmappable motion types to NULL —
                        re-ingestion lets the enrichment pipeline extract
                        motion_type from ruling text.
    --orphaned-only     Only re-ingest documents that have no associated
                        ruling records. Useful after a backfill that created
                        document records but did not process them through
                        transcription/enrichment.
    --filter-null-outcome
                        Only re-ingest documents that have at least one
                        ruling with a NULL outcome.  Useful for targeted
                        reprocessing after extraction improvements — avoids
                        re-ingesting the entire county when only a small
                        subset of documents need outcome extraction.
    --no-llm            Disable LLM extraction, use regex-only mode.
    --llm-timeout N     Per-call LLM API timeout in seconds (default: 60).
    --force-llm         Force LLM even when all fields are already populated.
    --full-reparse      Split multi-ruling documents using scraper logic.
                        Creates individual ruling records for each ruling in
                        a multi-ruling PDF and supersedes the original unsplit
                        document. Uses deterministic IDs for idempotency.
    --multimodal        Use multimodal per-page LLM extraction for PDF
                        documents. Sends page images directly to Google
                        Flash Lite, bypassing pdfplumber. Produces more
                        accurate results for OC tentative rulings.
                        Requires GOOGLE_API_KEY environment variable.
    --checkpoint-file PATH
                        Write cursor position and cumulative stats to this
                        JSON file after each batch. Enables resumption on
                        interruption via --resume.
    --resume            Resume from the checkpoint saved by --checkpoint-file.
                        Reads the saved cursor and stats, then continues from
                        where the previous run left off. Requires
                        --checkpoint-file to point to an existing file.
    --prefix PREFIX     Scan S3 directly by key prefix instead of querying the
                        database.  Useful for ingesting documents that were
                        never written to the DB (e.g. dead-lettered events).
                        Lists S3 objects under the prefix, seeds court records,
                        and processes each document through the ingestion
                        pipeline via IngestionWorker.process_event().
                        Example: --prefix federal/
    --bust-llm-cache    Skip LLM cache reads for the duration of this reingest
                        run.  Cache writes still happen so subsequent runs
                        without the flag benefit from the fresh results.
                        Useful when the LLM prompt has been updated and the
                        existing cache entries store stale outputs keyed by
                        the old prompt hash.  See #2424.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
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

from framework.extraction_config import (  # noqa: E402
    ExtractionMethod,
    decide_extraction_strategy,
    get_county_extraction_config,
)
from framework.llm_enrichment import (  # noqa: E402
    LlmEnrichmentExhaustedError,
    LlmEnrichmentResult,
    enrich_ruling_strict_retry,
    enrich_ruling_with_retry,
)
from framework.llm_extractor import LlmExtractor  # noqa: E402
from framework.llm_schema import ExtractedRuling  # noqa: E402
from framework.logging import configure_structlog  # noqa: E402
from framework.models import CapturedDocument, ContentFormat, ScraperConfig  # noqa: E402
from ingestion.db import (  # noqa: E402
    batch_upsert_parties,
    insert_document_and_ruling,
    resolve_judge,
    resolve_judge_from_department,
    upsert_case,
    upsert_case_judge,
)
from ingestion.doc_timing import DocTiming, emit_via_structlog_logger  # noqa: E402
from ingestion.extract import (  # noqa: E402
    dedupe_repeated_title,
    extract_case_number,
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_hearing_date,
    extract_judge_name,
    is_plausible_hearing_date,
    is_probate_decedent_name,
    normalize_motion_type,
    normalize_outcome,
    strip_trailing_case_number,
)
from ingestion.llm_extract import (  # noqa: E402
    LLMExtractionResult,
    LLMRulingResult,
    TokenTracker,
    extract_fields_llm,
    extract_text_from_pdf,
)
from ingestion.llm_providers import create_client as create_llm_client  # noqa: E402
from ingestion.ruling_guards import convert_extracted_rulings  # noqa: E402
from ingestion.split_ids import is_split_child_id, make_split_document_id  # noqa: E402
from validation.deterministic import run_deterministic_rules  # noqa: E402
from validation.gate import ValidationResult, insert_validation_result  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()


# Registry mapping scraper_id to scraper class for parse_document()
_SCRAPER_REGISTRY: dict[str, type] = {}

# Registry mapping scraper_id to a callable that splits raw PDF/document
# content into multiple extracted-field dicts.  Only populated for scrapers
# that produce multi-ruling documents (e.g. Riverside).  The callable
# signature is:  (raw_content: bytes, doc_meta: dict) -> list[dict]
# Each dict in the returned list has the same shape as _reparse_document()'s
# return value plus a "ruling_index" key.
_SPLIT_REGISTRY: dict[str, Any] = {}

# Registry mapping scraper_id to an LLM-based split function.  These are
# preferred over the regex-based _SPLIT_REGISTRY entries in full-reparse
# mode because they use LLM prompts to extract ruling_text (which may have
# been improved, e.g. #1948/#1959).  The callable signature is:
#   (text: str) -> list[SplitRuling] | None
# Returns None on LLM failure, in which case the regex fallback is used.
_LLM_SPLIT_REGISTRY: dict[str, Any] = {}


def _load_scraper_registry() -> None:
    """Lazily populate the scraper registry by auto-discovering court modules.

    Scans the ``courts/`` package tree for modules that expose a
    ``default_config()`` function and a ``BaseScraper`` subclass.  For each
    such module the ``scraper_id`` from the config is mapped to the scraper
    class.  This eliminates the need to maintain a hardcoded import list —
    adding a new scraper module automatically registers it.

    Multi-scraper modules (e.g. LA civil + appellate in ``la_tentatives``)
    can opt-in to explicit registration by exporting a module-level
    ``_SCRAPER_CLASS_BY_ID: dict[str, type]`` mapping each ``scraper_id``
    to its concrete ``BaseScraper`` subclass and pairing ``default_config``
    factories (``default_config``, ``default_config_<suffix>``, ...).
    See ``courts.ca.la_tentatives`` for the canonical example.
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

        # Collect all default_config* factory functions in this module.
        # Single-scraper modules expose just ``default_config``; multi-
        # scraper modules expose additional ``default_config_<suffix>``.
        config_factories: list = []
        for _attr_name, _attr in inspect.getmembers(mod, inspect.isfunction):
            if _attr_name == "default_config" or _attr_name.startswith(
                "default_config_"
            ):
                if _attr.__module__ == mod.__name__:
                    config_factories.append(_attr)

        if not config_factories:
            continue

        # Module-level hint: explicit scraper_id → class mapping.  Required
        # for multi-scraper modules where class-by-alphabetical-order would
        # be ambiguous.  Optional for single-scraper modules (we fall back
        # to auto-detection).
        scraper_class_by_id: dict[str, type] | None = getattr(
            mod, "_SCRAPER_CLASS_BY_ID", None
        )

        # Auto-detect single scraper class in the module (backward compat
        # for modules that don't export _SCRAPER_CLASS_BY_ID).
        auto_scraper_cls: type | None = None
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, BaseScraper)
                and obj is not BaseScraper
                and obj.__module__ == mod.__name__
            ):
                auto_scraper_cls = obj
                break

        for config_fn in config_factories:
            try:
                config = config_fn()
            except Exception:
                logger.warning(
                    "default_config factory failed, skipping",
                    module=modname,
                    factory=config_fn.__name__,
                    exc_info=True,
                )
                continue

            scraper_id = config.scraper_id
            scraper_cls: type | None = None
            if scraper_class_by_id is not None:
                scraper_cls = scraper_class_by_id.get(scraper_id)
            if scraper_cls is None:
                # Fall back to the auto-detected single class.  Only safe
                # when there is exactly one scraper in the module.
                scraper_cls = auto_scraper_cls

            if scraper_cls is None:
                continue

            _SCRAPER_REGISTRY[scraper_id] = scraper_cls

            # Register split function if the module exports one.
            # Convention: a module-level ``_split_rulings`` callable
            # indicates that the scraper produces multi-ruling documents
            # that need splitting during full reparse.
            split_fn = getattr(mod, "_split_rulings", None)
            if split_fn is not None and callable(split_fn):
                _SPLIT_REGISTRY[scraper_id] = split_fn

            # Register LLM-based split function if available.
            # Convention: a module-level ``_llm_extract_rulings`` callable
            # indicates that the scraper has an LLM-based splitter that
            # produces higher-quality ruling_text than regex splitting.
            llm_split_fn = getattr(mod, "_llm_extract_rulings", None)
            if llm_split_fn is not None and callable(llm_split_fn):
                _LLM_SPLIT_REGISTRY[scraper_id] = llm_split_fn


FETCH_DOCUMENTS_QUERY = """
    SELECT
        d.id, d.case_id, d.court_id, d.s3_key, d.s3_bucket,
        d.content_hash, d.source_url, d.scraper_id, d.captured_at,
        d.hearing_date, d.format,
        ct.state, ct.county, ct.court_name,
        c.case_number, c.case_title,
        (SELECT r.hearing_date FROM rulings r
         WHERE r.document_id = d.id LIMIT 1) AS ruling_hearing_date,
        (SELECT r.ruling_text FROM rulings r
         WHERE r.document_id = d.id LIMIT 1) AS stored_ruling_text,
        (SELECT r.department FROM rulings r
         WHERE r.document_id = d.id LIMIT 1) AS ruling_department,
        (SELECT j.canonical_name FROM rulings r
         LEFT JOIN judges j ON j.id = r.judge_id
         WHERE r.document_id = d.id LIMIT 1) AS ruling_judge_name
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


# ---------------------------------------------------------------------------
# Checkpoint / resume helpers
# ---------------------------------------------------------------------------

_CHECKPOINT_VERSION = 1


def _write_checkpoint(
    checkpoint_path: Path,
    cursor: tuple[datetime, str],
    stats: dict[str, Any],
) -> None:
    """Write a checkpoint file with the current cursor and cumulative stats.

    The checkpoint is written atomically: first to a temporary sibling file,
    then renamed into place.  This prevents a crash during write from leaving
    a truncated (unreadable) checkpoint file.

    Parameters
    ----------
    checkpoint_path:
        Destination file path.
    cursor:
        Current ``(captured_at, document_id)`` keyset pagination cursor.
    stats:
        Cumulative processing stats to persist (processed, updated, etc.).
    """
    data = {
        "version": _CHECKPOINT_VERSION,
        "cursor": {
            "captured_at": cursor[0].isoformat(),
            "document_id": cursor[1],
        },
        "stats": stats,
        "updated_at": datetime.now(tz=None).isoformat(),
    }
    tmp_path = checkpoint_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.rename(checkpoint_path)


def _read_checkpoint(
    checkpoint_path: Path,
) -> tuple[tuple[datetime, str], dict[str, Any]]:
    """Read a checkpoint file and return ``(cursor, stats)``.

    Parameters
    ----------
    checkpoint_path:
        Path to the checkpoint JSON file written by ``_write_checkpoint``.

    Returns
    -------
    tuple
        ``(cursor, stats)`` where *cursor* is ``(captured_at, document_id)``
        and *stats* is the cumulative stats dict from the checkpoint.

    Raises
    ------
    FileNotFoundError
        If the checkpoint file does not exist.
    ValueError
        If the file is not valid checkpoint JSON or has an unsupported version.
    """
    raw = checkpoint_path.read_text(encoding="utf-8")
    data = json.loads(raw)

    if data.get("version") != _CHECKPOINT_VERSION:
        msg = (
            f"Unsupported checkpoint version {data.get('version')}; "
            f"expected {_CHECKPOINT_VERSION}"
        )
        raise ValueError(msg)

    cursor_data = data["cursor"]
    captured_at = datetime.fromisoformat(cursor_data["captured_at"])
    document_id = cursor_data["document_id"]
    stats = data.get("stats", {})
    return (captured_at, document_id), stats


def _build_filters(
    county: str | None,
    date_from: date | None,
    date_to: date | None,
    case_title_regex: str | None = None,
    null_motion_type: bool = False,
    orphaned_only: bool = False,
    case_number_like: str | None = None,
    filter_null_outcome: bool = False,
    department_in: list[str] | None = None,
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
    if case_number_like:
        clauses.append("AND c.case_number LIKE %s")
        params.append(case_number_like)
    if case_title_regex:
        clauses.append("AND c.case_title ~ %s")
        params.append(case_title_regex)
    if null_motion_type:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM rulings r"
            " WHERE r.document_id = d.id AND r.motion_type IS NULL)"
        )
    if filter_null_outcome:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM rulings r"
            " WHERE r.document_id = d.id AND r.outcome IS NULL)"
        )
    if orphaned_only:
        clauses.append(
            "AND NOT EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = d.id)"
        )
    if department_in:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM rulings r"
            " WHERE r.document_id = d.id AND r.department = ANY(%s))"
        )
        params.append(department_in)
    return " ".join(clauses), params


def _is_real_case_number(case_number: str | None) -> bool:
    """Return True if the case number is a real value, not an UNKNOWN placeholder."""
    if not case_number:
        return False
    return not case_number.startswith("UNKNOWN-")


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

    When pdfplumber returns no text (image-only PDFs), falls back to OCR
    via the framework's ``extract_text_from_pdf()`` which uses LLM vision.

    For other formats (HTML, plain text), decodes as UTF-8.
    """
    if doc_format == "pdf":
        text = _extract_pdf_text_subprocess(raw_content, timeout=pdf_timeout)
        if text and text.strip():
            return text
        # Subprocess returned no text — likely an image-only PDF.
        # Try OCR fallback via the framework's extract_text_from_pdf(),
        # which renders pages and uses LLM vision for OCR (#1334).
        # Pass the original raw bytes to avoid lossy UTF-8 round-trip.
        logger.debug("PDF subprocess extraction returned no text, trying OCR fallback")
        ocr_text = extract_text_from_pdf(raw_content)
        if ocr_text and ocr_text.strip():
            return ocr_text
        # OCR also failed — fall back to UTF-8 decode as last resort.
        logger.debug("OCR fallback returned no text, falling back to UTF-8")
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


def apply_post_extraction_guards(
    extracted: dict,
    capture_timestamp: datetime | None,
) -> None:
    """Apply the #2370 post-extraction guards to a reingested ruling.

    Mirrors the guard pipeline that ``IngestionWorker.process_event`` runs
    after LLM/regex extraction, so reingest cannot produce rulings that
    violate the guards.  The guards are (in order):

    1. ``dedupe_repeated_title``: collapse "Foo v Bar\\nFoo v Bar" headers.
    2. ``strip_trailing_case_number``: strip "Foo v Bar 30-2024-..." suffixes.
    3. ``is_probate_decedent_name``: reject probate decedent names that the
       LLM hallucinated as the judge.  Clears ``judge_name`` to ``None``
       when the heuristic matches.
    4. ``is_plausible_hearing_date``: reject hearing_dates more than 30
       days before capture or more than 2 years after capture.  Clears
       ``hearing_date`` to ``None`` when implausible.

    Mutates ``extracted`` in place.  Expects
    ``extracted["extraction_methods"]`` to exist as a ``dict`` — if a
    guard clears a field, the corresponding ``extraction_methods`` entry
    is popped so the caller cannot later re-use a stale provenance.

    ``capture_timestamp`` may be ``None`` (e.g. when the fixture lacks a
    timestamp, or the document was captured before the ``captured_at``
    column was populated).  When ``None``, the ``is_plausible_hearing_date``
    check is skipped — we cannot compare an unknown capture time against
    a candidate hearing_date (#2405).
    """
    methods = extracted.setdefault("extraction_methods", {})

    # Title guards (dedupe + trailing case number)
    original_title = extracted.get("case_title")
    cleaned_title = dedupe_repeated_title(original_title)
    cleaned_title = strip_trailing_case_number(cleaned_title)
    if cleaned_title != original_title:
        logger.info(
            "post-extraction guard: cleaned case_title",
            original=original_title,
            cleaned=cleaned_title,
        )
        extracted["case_title"] = cleaned_title

    # Probate decedent guard — judge_name must not be the decedent
    judge_name = extracted.get("judge_name")
    case_title = extracted.get("case_title")
    if judge_name and case_title and is_probate_decedent_name(judge_name, case_title):
        logger.info(
            "post-extraction guard: rejected probate decedent as judge",
            judge_name=judge_name,
            case_title=case_title,
        )
        extracted["judge_name"] = None
        methods.pop("judge_name", None)

    # Hearing date plausibility guard.
    #
    # Probate departments publish multi-week master calendars (#4251) — pass
    # ``case_type`` so the helper widens the window for ``"probate"``.  Civil
    # tentatives use the default ±14 day window.
    hearing_date_value = extracted.get("hearing_date")
    if hearing_date_value is not None and capture_timestamp is not None:
        if not is_plausible_hearing_date(
            hearing_date_value,
            capture_timestamp,
            case_type=extracted.get("case_type"),
        ):
            logger.info(
                "post-extraction guard: rejected implausible hearing_date",
                hearing_date=hearing_date_value,
                capture_timestamp=capture_timestamp,
                case_type=extracted.get("case_type"),
            )
            extracted["hearing_date"] = None
            methods.pop("hearing_date", None)


def finalize_hearing_date(
    *,
    extracted_hearing: date | None,
    doc_meta_hearing: date | None,
    capture_timestamp: datetime | None,
    case_type: str | None = None,
) -> date | None:
    """Return the final hearing_date to write, or None if no plausible value.

    Owns the entire "pick the final hearing_date" decision end-to-end for
    the reingest DB-write path.  Runs the #2370 plausibility guard on each
    candidate in the fallback chain — the first plausible value wins,
    implausible values are skipped.

    This centralizes the logic that three successive point patches chased
    around the codebase:

    -   #2370 — original plausibility guard on ``extracted["hearing_date"]``.
    -   #2405 / PR #2422 — reingest bypassed the guard; added
        ``apply_post_extraction_guards`` to fix.
    -   #2432 — ``effective = extracted or doc_meta`` bypassed the guard
        again when ``doc_meta`` held the same bad value.

    Making the fallback chain go through a single plausibility-checking
    helper makes it structurally impossible for a fallback branch to
    introduce an implausible hearing_date.  New fallback sources (e.g.
    ruling row column, regex re-extraction) are added inside this helper
    and automatically inherit the guard.

    Parameters
    ----------
    extracted_hearing : date | None
        Preferred candidate — the hearing_date produced by the extraction
        pipeline (LLM / regex / scraper metadata).
    doc_meta_hearing : date | None
        Fallback candidate — the hearing_date surfaced from
        ``documents.hearing_date`` via ``doc_meta``.
    capture_timestamp : datetime | None
        The document capture timestamp passed through to
        ``is_plausible_hearing_date``.  When ``None``, plausibility cannot
        be checked and the first non-None candidate wins (permissive —
        don't drop data we can't verify).
    case_type : str | None
        Optional case type passed through to ``is_plausible_hearing_date``.
        ``"probate"`` widens the window to ±60 days (#4251); other values
        (and ``None``) keep the civil ±14 day default.

    Returns
    -------
    date | None
        The chosen hearing_date, or ``None`` if no candidate is plausible.
    """
    for candidate in (extracted_hearing, doc_meta_hearing):
        if candidate is None:
            continue
        if is_plausible_hearing_date(candidate, capture_timestamp, case_type=case_type):
            return candidate
    return None


def _apply_regex_fallbacks(extracted: dict, text: str, scraper_id: str = "") -> None:
    """Apply the regex fallback chain to fill any fields still missing.

    Mutates *extracted* in place.  Expects ``extracted["extraction_methods"]``
    to already exist as a ``dict``.

    The fallback chain covers fields NOT handled by LLM enrichment:
    judge_name, case_number, hearing_date, and case_type.  The enrichment
    fields (outcome, motion_type, case_title, parties) are handled
    exclusively by LLM enrichment (#2178).

    This function is called from both ``_reparse_document()`` (single-doc path)
    and ``_full_reparse_document()`` (split-doc path) to keep the fallback
    chains in sync — see #1763 / #1749 / #1836.
    """
    methods = extracted["extraction_methods"]

    if not extracted["judge_name"]:
        val = extract_judge_name(text)
        if val:
            extracted["judge_name"] = val
            methods.setdefault("judge_name", "regex")
    if not extracted["case_number"]:
        val = extract_case_number(text)
        if val:
            extracted["case_number"] = val
            methods.setdefault("case_number", "regex")
    if not extracted["hearing_date"]:
        val = extract_hearing_date(text)
        if val:
            extracted["hearing_date"] = val
            methods.setdefault("hearing_date", "regex")

    # Fallback case_type from case number prefix (#706).
    if not extracted["case_type"] and extracted["case_number"]:
        val = extract_case_type_from_number(extracted["case_number"])
        if val:
            extracted["case_type"] = val
            methods.setdefault("case_type", "regex")

    # Fallback case_type from scraper_id (#1524 / #1836).
    # When the case number is absent or doesn't encode a type prefix
    # (e.g. OC North JC PDFs have no case numbers), infer from the
    # scraper_id which encodes the case category in its suffix.
    if not extracted["case_type"] and scraper_id:
        val = extract_case_type_from_scraper_id(scraper_id)
        if val:
            extracted["case_type"] = val
            methods.setdefault("case_type", "scraper_id")

    # Fallback case_type from motion_type (#1731).
    # Final fallback for cases where the case number has no embedded
    # type code and the scraper_id is generic (e.g. Ventura's
    # all-digit case numbers like 202300574258).  Many civil motion
    # types unambiguously identify the case type.
    if not extracted["case_type"] and extracted["motion_type"]:
        val = extract_case_type_from_motion_type(extracted["motion_type"])
        if val:
            extracted["case_type"] = val
            methods.setdefault("case_type", "motion_type")


def _extract_doc_level_judge_department(
    doc_meta: dict,
    scraper_id: str,
    raw_content: bytes,
    *,
    parsed: Any | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(doc_judge_name, doc_department)`` with the symmetric DB-seed merge.

    Centralises the #4142 / #4145 / #4150 fixes so future scrapers added to
    ``_SCRAPER_REGISTRY`` automatically inherit the symmetric-merge behaviour.
    Pre-fix, three near-identical inline copies of this logic lived at the
    three reingest call sites — ``_reparse_document``,
    ``_full_reparse_document``, and ``_reparse_document_multimodal`` — and
    each was patched independently when the same silent-data-loss bug surfaced
    (#3986 root cause). #4154 hoists the merge into one place.

    Always: prefer ``parsed.X``, fall back to ``doc_meta.get("X")``, default
    both to ``doc_meta.get("X")`` on every fall-through (no scraper class
    registered, ``parse_document`` raises, ``parse_document`` returns falsy
    values).  This mirrors the ``case_number`` / ``case_title`` symmetric-
    merge pattern already used elsewhere in this script.

    Why a DB-seed fallback exists at all: ``doc_meta`` carries
    ``judge_name`` and ``department`` from the rulings row (via
    ``FETCH_DOCUMENTS_QUERY``).  A no-op ``parse_document`` on a Live-only
    scraper (e.g. ``cc_tentatives_portal``, ``oc_tentatives``,
    ``sf_civil_tentatives``) returns the cap_doc unchanged with
    ``judge_name`` / ``department`` equal to ``None``.  Without this
    fallback those values silently overwrite the DB-seeded values pulled
    from the rulings row at the DB-write path, which is the same silent-
    data-loss path #3986 surfaced for CourtListener.  See
    ``docs/investigations/parse_document-reingest-safety-2026-05.md`` for
    the per-scraper audit.

    Parameters
    ----------
    doc_meta : dict
        Document metadata from ``FETCH_DOCUMENTS_QUERY``.  Carries the
        DB-seeded ``judge_name`` / ``department`` (from the rulings row's
        ``canonical_name`` and ``department``).  Both keys are optional —
        ``.get()`` returns ``None`` when absent (no KeyError).
    scraper_id : str
        Scraper ID (e.g. ``"ca-sf-civil-tentatives"``).  Used to look up
        the registered scraper class from ``_SCRAPER_REGISTRY``.
    raw_content : bytes
        Raw document bytes from S3.  Used to build a ``CapturedDocument``
        when this helper drives the ``parse_document`` call itself
        (``parsed`` is ``None``).
    parsed : Any | None, keyword-only
        Pre-computed ``parse_document`` result.  Some callers (e.g.
        ``_reparse_document``) already invoke ``parse_document`` for other
        fields (ruling_text, case_number, case_title, etc.) and pass the
        result in to avoid a second call.  When ``None``, the helper
        builds a ``CapturedDocument`` and calls ``parse_document``
        itself — matching the dedicated doc-level extraction blocks in
        ``_full_reparse_document`` and ``_reparse_document_multimodal``.

    Returns
    -------
    tuple[str | None, str | None]
        ``(doc_judge_name, doc_department)`` with the symmetric DB-seed
        fallback applied.  Either may be ``None`` when neither
        ``parsed.X`` nor ``doc_meta`` carries a value for that field.

    Notes
    -----
    Post-helper text-based regex fallbacks (``extract_judge_name(text)``
    in ``_full_reparse_document`` and the ``DEPT NNN`` regex in
    ``_reparse_document_multimodal``) intentionally stay at the call
    sites — they are path-specific (text-availability, format-specific
    regexes) and not part of the shared bug class.
    """
    judge_seed = doc_meta.get("judge_name")
    dept_seed = doc_meta.get("department")

    # Caller-provided parsed object: skip the parse_document call and
    # apply the symmetric merge directly.  ``getattr`` defends against
    # parsed objects that omit the attribute entirely (defensive — the
    # production ``CapturedDocument`` always carries both, but a future
    # alternate parsed shape could not).
    if parsed is not None:
        return (
            getattr(parsed, "judge_name", None) or judge_seed,
            getattr(parsed, "department", None) or dept_seed,
        )

    scraper_cls = _SCRAPER_REGISTRY.get(scraper_id)
    if scraper_cls is None:
        # No scraper class registered (e.g. ``rebuild-ca-san_diego``
        # synthetic scraper_id, or a future LLM-only split scraper):
        # the DB seed is the only source of doc-level judge/department.
        return judge_seed, dept_seed

    try:
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
            content_format=ContentFormat.from_db_value(doc_meta["format"]),
            raw_content=raw_content,
            content_hash=doc_meta.get("content_hash", ""),
        )
        parsed_result = scraper.parse_document(cap_doc)
        return (
            parsed_result.judge_name or judge_seed,
            parsed_result.department or dept_seed,
        )
    except Exception:
        logger.warning(
            "Scraper parse_document failed during doc-level judge/department extraction",
            document_id=doc_meta.get("document_id"),
            scraper_id=scraper_id,
            exc_info=True,
        )
        return judge_seed, dept_seed


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
    token_tracker: TokenTracker | None = None,
    force_retranscribe: bool = False,
    bust_llm_cache: bool = False,
) -> dict:
    """Re-parse a document using a three-tier extraction strategy.

    Extraction priority per field:
      1. Scraper ``parse_document()`` (highest priority).
      2. LLM extraction via ``extract_fields_llm()`` (if *llm_client*
         is provided and fields are missing, or *force_llm* is True),
         **unless** the (state, county, scraper_id) lookup in
         ``framework.extraction_config`` returns ``ExtractionMethod.NONE``.
         NONE-configured counties (e.g. Federal CourtListener clusters,
         San Diego calendar) ship complete scraper-populated fields and
         the CA-tuned LLM prompt corrupts them — see #4056.
      3. Regex fallback (lowest priority).

    For PDF documents, extracts text via pdfplumber in a subprocess with
    a hard timeout to prevent hangs from C extensions.

    Returns a dict of extracted fields plus an ``extraction_methods`` dict
    recording which method populated each field, and an ``llm_skipped``
    boolean indicating whether LLM extraction was skipped (because all
    fields were already populated, or because the county is registered
    with ``ExtractionMethod.NONE``).  ``llm_outcome`` distinguishes the
    two: ``"skipped"`` for the all-fields-present path and
    ``"skipped_none"`` for the NONE-config short-circuit.
    """
    _load_scraper_registry()

    # Per-doc phase timing (#4116) — accumulated into the result dict so
    # the main DB-write loop can pick them up via ``DocTiming.add_ms``
    # without re-implementing parse/regex timing across the 3 reparse
    # entry points.  ``regex_fallback_ms`` is filled in when
    # ``_apply_regex_fallbacks`` runs (single chokepoint at line ~1196).
    _parse_t0 = time.perf_counter()

    doc_format = doc_meta.get("format", "html")
    text = _extract_text_from_content(
        raw_content, doc_format, pdf_timeout=pdf_timeout
    ).replace("\x00", "")
    extracted: dict = {
        "ruling_text": text,
        "case_number": doc_meta.get("case_number"),
        "case_title": doc_meta.get("case_title"),
        "case_type": doc_meta.get("case_type"),
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
                content_format=ContentFormat.from_db_value(doc_meta["format"]),
                raw_content=raw_content,
                content_hash=doc_meta["content_hash"],
            )
            # Set case_number from DB metadata so parse_document can
            # narrow multi-case calendar pages to the specific case
            # (#2311 — SD calendar LLM extraction failures).
            if doc_meta.get("case_number"):
                cap_doc.case_number = doc_meta["case_number"]
            parsed = scraper.parse_document(cap_doc)
            ruling = parsed.ruling_text or text
            extracted["ruling_text"] = ruling.replace("\x00", "") if ruling else text
            extracted["case_number"] = parsed.case_number or extracted["case_number"]
            extracted["case_title"] = parsed.case_title or extracted["case_title"]
            # Symmetric DB-seed merge for doc-level judge_name and department
            # (#4142, refactored in #4154 — see
            # ``_extract_doc_level_judge_department``).  This is the shared
            # helper that owns the merge formula across all three reingest
            # entry points; passing the existing ``parsed`` in avoids a
            # second ``parse_document`` call.
            extracted["judge_name"], extracted["department"] = (
                _extract_doc_level_judge_department(
                    doc_meta, scraper_id, raw_content, parsed=parsed
                )
            )
            # ``outcome`` and ``motion_type`` legitimately fall back to
            # ``None`` here rather than a DB seed: ``doc_meta`` does not
            # carry these fields (FETCH_DOCUMENTS_QUERY at lines ~380-403
            # does not select them from the rulings row), so there is no
            # seed to fall back to.  The LLM and regex tiers below
            # repopulate them from text when missing.  Normalization
            # (#2113 outcome, #1849 motion_type) mirrors the worker.py
            # path so reingest produces the same canonical values as
            # live ingestion.
            extracted["outcome"] = (
                normalize_outcome(parsed.outcome) if parsed.outcome else None
            )
            extracted["motion_type"] = (
                normalize_motion_type(parsed.motion_type)
                if parsed.motion_type
                else None
            )
            # ``parties`` falls back to the prior ``extracted["parties"]``
            # (initialized to ``[]`` at line 911) because ``doc_meta``
            # does not carry a parties seed.  The LLM and enrichment
            # tiers below repopulate this when text is available.
            extracted["parties"] = parsed.parties or extracted["parties"]
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
        "case_type",
        "hearing_date",
        "department",
        "parties",
    ):
        val = extracted.get(field)
        if val and (not isinstance(val, list) or len(val) > 0):
            # UNKNOWN-prefixed case numbers are placeholders, not real values.
            if field == "case_number" and not _is_real_case_number(val):
                continue
            extraction_methods[field] = "scraper"

    # ------------------------------------------------------------------
    # San Diego direct narrowing (#2311, #2381)
    # ------------------------------------------------------------------
    # San Diego calendar pages contain 30+ cases per HTML file.  When a
    # document has no registered scraper class (e.g. scraper_id
    # "rebuild-ca-san_diego" from rebuild_db.py), ``parse_document`` is
    # never called and ``extracted["ruling_text"]`` remains the full raw
    # HTML page (~50KB).  We narrow directly here so the stored
    # ruling_text becomes the plain-text excerpt for the specific case,
    # independent of whether LLM extraction runs below.
    sd_case_num = extracted.get("case_number")
    if (
        sd_case_num
        and extracted.get("ruling_text") == text  # Not already narrowed
        and doc_meta.get("county", "").lower() == "san diego"
        and doc_format == "html"
    ):
        try:
            from courts.ca.sd_calendar import extract_case_section

            section = extract_case_section(
                raw_content.decode("utf-8", errors="replace"),
                sd_case_num,
            )
            if section:
                extracted["ruling_text"] = section
        except Exception:
            pass  # Fall through to full-text extraction

    # End of parse_document_ms phase (#4116) — captured before the LLM
    # extraction call which has its own ``llm_latency_ms`` timer.
    extracted["parse_document_ms"] = (time.perf_counter() - _parse_t0) * 1000.0

    # ------------------------------------------------------------------
    # LLM extraction — secondary method for missing fields
    # ------------------------------------------------------------------
    llm_skipped = False
    llm_outcome = "not_attempted"

    # Resolve the extraction strategy once and reuse below (#4081 — Option B).
    # Single source of truth shared with ``packages/scraper-framework/src/
    # ingestion/worker.py`` — both paths read ``strategy.skip_llm`` and
    # ``strategy.max_output_tokens`` instead of re-deriving the gate logic
    # from ``CountyExtractionConfig`` inline.  This closes the recurring
    # divergence pattern (#2490, #2501, #2502, #2521, #4056).
    strategy = decide_extraction_strategy(
        doc_meta.get("state", ""),
        doc_meta.get("county", ""),
        scraper_id=scraper_id,
    )

    # Short-circuit LLM extraction when the county/scraper is registered
    # with ``ExtractionMethod.NONE`` (#4056).  The live worker path honors
    # this in ``ingestion.worker.process_event``, and now both paths read
    # ``strategy.skip_llm`` from the same helper.
    if strategy.skip_llm:
        logger.info(
            "Skipping LLM extraction — county uses ExtractionMethod.NONE",
            document_id=doc_meta["document_id"],
            state=doc_meta.get("state"),
            county=doc_meta.get("county"),
            scraper_id=scraper_id,
        )
        llm_skipped = True
        llm_outcome = "skipped_none"
    elif llm_client is not None:
        missing_fields = [
            f
            for f in (
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
            if not extracted.get(f)
            or (isinstance(extracted.get(f), list) and len(extracted[f]) == 0)
            # UNKNOWN-prefixed case numbers are placeholders, not real values.
            or (f == "case_number" and not _is_real_case_number(extracted.get(f)))
        ]
        if not missing_fields and not force_llm:
            logger.info(
                "All fields present, skipping LLM extraction",
                document_id=doc_meta["document_id"],
            )
            llm_skipped = True
            llm_outcome = "skipped"
        elif (missing_fields or force_llm) and text.strip():
            # Use narrowed ruling_text when available (#2311 — SD calendar
            # pages contain 30+ cases; sending the full page to the LLM
            # causes output truncation).  Narrowing already happened
            # above for SD HTML pages regardless of LLM availability.
            llm_text = extracted.get("ruling_text") or text
            # County-specific max_output_tokens (#2355) — large-document
            # counties (e.g. Santa Clara, 130K+ chars) need more than the
            # default 4096 output tokens to avoid truncated JSON responses.
            # ``strategy.max_output_tokens`` already resolves to 4096 when
            # no county override is configured (#4081).
            _llm_max_tokens = strategy.max_output_tokens

            t0 = time.monotonic()
            llm_result = extract_fields_llm(
                document_text=llm_text,
                content_format=doc_format,
                metadata=None,
                client=llm_client,
                provider=llm_provider,
                model=llm_model,
                timeout=llm_timeout,
                token_tracker=token_tracker,
                max_tokens=_llm_max_tokens,
                bust_cache=bust_llm_cache,
                document_id=doc_meta["document_id"],
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
                    if (
                        not _is_real_case_number(extracted["case_number"])
                        and ruling.case_number
                    ):
                        extracted["case_number"] = ruling.case_number
                        extraction_methods["case_number"] = "llm"
                    if not extracted["case_title"] and ruling.case_title:
                        extracted["case_title"] = ruling.case_title
                        extraction_methods["case_title"] = "llm"
                    if not extracted["outcome"] and ruling.outcome:
                        # Normalize LLM-provided outcome (#2113).
                        normalized_out = normalize_outcome(ruling.outcome)
                        if normalized_out:
                            extracted["outcome"] = normalized_out
                            extraction_methods["outcome"] = "llm"
                    if not extracted["motion_type"] and ruling.motion_type:
                        # Normalize LLM-provided motion_type (#1849).
                        normalized = normalize_motion_type(ruling.motion_type)
                        if normalized:
                            extracted["motion_type"] = normalized
                            extraction_methods["motion_type"] = "llm"
                    if not extracted["case_type"] and ruling.case_type:
                        extracted["case_type"] = ruling.case_type
                        extraction_methods["case_type"] = "llm"
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
    # Prefer stored ruling_text for regex extraction and DB persistence
    # ------------------------------------------------------------------
    # When stored_ruling_text exists (from a prior LLM transcription), it
    # is scoped to the *individual* case's ruling (e.g. ~1K chars).  The
    # pdfplumber ``text`` is the *full* multi-ruling PDF (e.g. 77K chars).
    # Using the full PDF for regex extraction produces wrong matches
    # (motion_type from a different case) and overwrites the scoped
    # ruling_text in the DB via the ON CONFLICT upsert.  See #1848.
    #
    # However, for HTML documents the scraper's parse_document() produces
    # correct clean text via BeautifulSoup.  Preserving stored_ruling_text
    # for HTML docs prevents re-transcription when the stored value IS the
    # problem (e.g. raw HTML instead of clean text).  See #2360.
    stored = doc_meta.get("stored_ruling_text")
    use_stored = stored and not force_retranscribe and doc_format != "html"
    if use_stored:
        extracted["ruling_text"] = stored.replace("\x00", "") if stored else stored
        regex_text = extracted["ruling_text"]
    else:
        regex_text = text

    # ------------------------------------------------------------------
    # Regex fallback — fill any fields still missing after scraper + LLM
    # ------------------------------------------------------------------
    extracted["extraction_methods"] = extraction_methods
    _regex_t0 = time.perf_counter()
    _apply_regex_fallbacks(extracted, regex_text, scraper_id=scraper_id)
    extracted["regex_fallback_ms"] = (time.perf_counter() - _regex_t0) * 1000.0

    if extraction_methods:
        logger.info(
            "Field extraction summary",
            document_id=doc_meta["document_id"],
            methods=extraction_methods,
        )

    extracted["llm_skipped"] = llm_skipped
    extracted["llm_outcome"] = llm_outcome
    return extracted


def _full_reparse_document(
    raw_content: bytes,
    scraper_id: str,
    doc_meta: dict,
    pdf_timeout: float = 30.0,
    llm_client: object | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_timeout: float | None = 60.0,
    force_llm: bool = False,
    token_tracker: TokenTracker | None = None,
    force_retranscribe: bool = False,
    bust_llm_cache: bool = False,
) -> list[dict]:
    """Re-parse a document with full splitting logic.

    Unlike ``_reparse_document()`` which only calls ``parse_document()``,
    this function also invokes the scraper's splitting logic (e.g.
    ``_split_rulings()`` for Riverside) to break multi-ruling PDFs into
    individual ruling records.

    Returns a **list** of extracted-field dicts (one per ruling).  For
    scrapers without splitting logic, or documents that don't split,
    returns a single-element list equivalent to ``_reparse_document()``.

    Each dict in the returned list includes:
      - All fields from ``_reparse_document()``
      - ``ruling_index``: int — the 1-based ruling number within the PDF
      - ``split_document_id``: str — deterministic UUID for the split ruling
      - ``is_split``: bool — True if this came from splitting
    """
    _load_scraper_registry()

    # Short-circuit the LLM-based split path when the county/scraper is
    # registered with ``ExtractionMethod.NONE`` (#4056).  Even though the
    # downstream ``_reparse_document`` path also guards LLM extraction, an
    # ``llm_split_fn`` registered for a NONE-configured scraper would still
    # invoke an LLM here.  No NONE-registered scraper has an llm_split_fn
    # today, but the explicit guard makes the contract self-documenting and
    # protects future scrapers from regressing the federal cleanup case.
    _ext_config = get_county_extraction_config(
        doc_meta.get("state", ""),
        doc_meta.get("county", ""),
        scraper_id=scraper_id,
    )
    if _ext_config is not None and _ext_config.method == ExtractionMethod.NONE:
        result = _reparse_document(
            raw_content,
            scraper_id,
            doc_meta,
            pdf_timeout=pdf_timeout,
            llm_client=llm_client,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            force_llm=force_llm,
            token_tracker=token_tracker,
            force_retranscribe=force_retranscribe,
            bust_llm_cache=bust_llm_cache,
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        return [result]

    split_fn = _SPLIT_REGISTRY.get(scraper_id)
    llm_split_fn = _LLM_SPLIT_REGISTRY.get(scraper_id)

    if split_fn is None and llm_split_fn is None:
        # No splitting logic (neither regex nor LLM) — fall back to
        # single-document reparse.
        result = _reparse_document(
            raw_content,
            scraper_id,
            doc_meta,
            pdf_timeout=pdf_timeout,
            llm_client=llm_client,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            force_llm=force_llm,
            token_tracker=token_tracker,
            force_retranscribe=force_retranscribe,
            bust_llm_cache=bust_llm_cache,
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        return [result]

    # Extract text from the raw content (PDF or HTML).  ``parse_document_ms``
    # in the split-doc path covers only the text extraction here — the
    # downstream split function may invoke its own LLM call which is timed
    # separately by the LLM extractor.
    _split_parse_t0 = time.perf_counter()
    doc_format = doc_meta.get("format", "html")
    text = _extract_text_from_content(
        raw_content,
        doc_format,
        pdf_timeout=pdf_timeout,
    ).replace("\x00", "")
    _split_parse_ms = (time.perf_counter() - _split_parse_t0) * 1000.0

    # Prefer LLM-based splitting when available — it produces higher-quality
    # ruling_text (e.g. full legal analyses instead of disposition summaries,
    # see #1948/#1959).  Falls back to regex-based split on LLM failure.
    # Note: some scrapers (e.g. LA) only have an LLM splitter without a
    # regex fallback — previously these were skipped entirely (#2007).
    if llm_split_fn is not None:
        try:
            split_results = llm_split_fn(text)
        except Exception:
            logger.warning(
                "LLM split raised exception, falling back to regex",
                document_id=doc_meta["document_id"],
                scraper_id=scraper_id,
                exc_info=True,
            )
            split_results = None
        if split_results is None:
            if split_fn is not None:
                logger.warning(
                    "LLM split failed, falling back to regex split",
                    document_id=doc_meta["document_id"],
                    scraper_id=scraper_id,
                )
                split_results = split_fn(text)
            else:
                logger.warning(
                    "LLM split failed and no regex fallback available",
                    document_id=doc_meta["document_id"],
                    scraper_id=scraper_id,
                )
                split_results = []
    else:
        split_results = split_fn(text)

    if len(split_results) <= 1:
        # Single ruling or no rulings — fall back to standard reparse
        result = _reparse_document(
            raw_content,
            scraper_id,
            doc_meta,
            pdf_timeout=pdf_timeout,
            llm_client=llm_client,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            force_llm=force_llm,
            token_tracker=token_tracker,
            force_retranscribe=force_retranscribe,
            bust_llm_cache=bust_llm_cache,
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        return [result]

    # Multiple rulings — split into individual records
    content_hash = doc_meta.get("content_hash", "")
    if not content_hash:
        content_hash = hashlib.sha256(raw_content).hexdigest()

    # Extract hearing date from the full PDF text (document-level, shared
    # across all rulings).  ``doc_hearing_date`` is path-specific to
    # ``_full_reparse_document`` — it does not apply to the single-doc
    # ``_reparse_document`` path or the multimodal path's per-ruling
    # ``hearing_date_val`` flow — so it stays inline here.
    scraper_cls = _SCRAPER_REGISTRY.get(scraper_id)
    doc_hearing_date: Any = doc_meta.get("hearing_date")
    parsed: Any | None = None

    if scraper_cls:
        try:
            config = ScraperConfig(
                scraper_id=scraper_id,
                state=doc_meta["state"],
                county=doc_meta["county"],
                court=doc_meta["court_name"],
                target_urls=[],
            )
            scraper = scraper_cls(config=config)
            # Use scraper's parse_document for doc-level field extraction
            # by creating a synthetic doc with the full text.
            cap_doc = CapturedDocument(
                document_id=doc_meta["document_id"],
                scraper_id=scraper_id,
                state=doc_meta["state"],
                county=doc_meta["county"],
                court=doc_meta["court_name"],
                source_url=doc_meta["source_url"],
                capture_timestamp=doc_meta["captured_at"],
                content_format=ContentFormat.from_db_value(doc_meta["format"]),
                raw_content=raw_content,
                content_hash=content_hash,
            )
            parsed = scraper.parse_document(cap_doc)
            if parsed.hearing_date:
                doc_hearing_date = (
                    parsed.hearing_date.date()
                    if isinstance(parsed.hearing_date, datetime)
                    else parsed.hearing_date
                )
        except Exception:
            logger.warning(
                "Scraper parse_document failed during full-reparse",
                document_id=doc_meta["document_id"],
                exc_info=True,
            )
            parsed = None

    # Symmetric DB-seed merge for doc-level judge_name and department
    # (#4145, refactored in #4154 — see
    # ``_extract_doc_level_judge_department``).  Single source of truth
    # for the merge formula across all three reingest entry points.
    # When ``parsed`` is ``None`` (no scraper class registered, or
    # ``parse_document`` raised), the helper falls back to the DB seed
    # carried on ``doc_meta`` — protecting against the silent
    # data-loss class #3986 surfaced for CourtListener.
    doc_judge_name, doc_department = _extract_doc_level_judge_department(
        doc_meta, scraper_id, raw_content, parsed=parsed
    )

    # Fall back to regex for judge name if scraper didn't provide one
    if not doc_judge_name:
        doc_judge_name = extract_judge_name(text)

    logger.info(
        "Splitting document into multiple rulings",
        document_id=doc_meta["document_id"],
        ruling_count=len(split_results),
        scraper_id=scraper_id,
    )

    results: list[dict] = []
    for ruling in split_results:
        ruling_index = ruling.ruling_index
        split_doc_id = make_split_document_id(doc_meta["document_id"], ruling_index)

        # Guard against cross-contamination (#2078): when splitting
        # produces a ruling with no text, use None instead of empty
        # string so the DB stores NULL rather than a misleading value.
        # This matches the guard in _reparse_document_multimodal().
        ruling_text_cleaned: str | None = (
            ruling.ruling_text.replace("\x00", "") if ruling.ruling_text else None
        )

        # Prefer per-ruling department when the split provides one
        # (e.g. Fresno extracts it from each ruling's "(Dept. NNN)"
        # header).  Fall back to the doc-level department otherwise.
        ruling_department = getattr(ruling, "department", None) or doc_department

        extracted: dict = {
            "ruling_text": ruling_text_cleaned,
            # ``case_number`` keeps its ``doc_meta`` fallback because it is
            # an *identity anchor*, not descriptive metadata: the ``cases``
            # row is keyed on ``(court_id, case_number)`` (``db.py:325``).
            # If the split returns no case_number we want to continue
            # writing this ruling back to the *same* case row — replacing
            # a real number with ``UNKNOWN-<uuid>`` would orphan the
            # ruling from the existing case.  The same doc_meta ->
            # UNKNOWN layering repeats in the DB-write path at lines
            # 2373-2377 as a final safety net.  (Unlike case_title, the
            # #2502 fixed-point risk does not apply here: the LLM always
            # wins when it produces a case_number, and a stable identity
            # anchor is the desired behaviour.)
            "case_number": ruling.case_number or doc_meta.get("case_number"),
            # #2521 (sibling of #2502): Do NOT fall back to
            # ``doc_meta.get("case_title")`` here — ``doc_meta`` carries
            # the *existing* DB ``case_title`` (via the LEFT JOIN in
            # ``FETCH_DOCUMENTS_QUERY``).  Falling back would create a
            # self-sustaining fixed point for wrong titles: once a bad
            # title is stored for a case, every reingest where the split
            # returns no title re-writes that same wrong title back
            # (because reingest uses ``upsert_case(force_update=True)``).
            # Letting ``None`` flow through is SAFE: ``upsert_case``'s
            # ``force_update=True`` branch at ``db.py:316`` uses
            # ``COALESCE(EXCLUDED.case_title, cases.case_title)``, so an
            # incoming ``NULL`` preserves the existing non-null title
            # instead of erasing it.
            "case_title": ruling.case_title,
            "case_type": doc_meta.get("case_type"),
            "judge_name": doc_judge_name,
            "outcome": normalize_outcome(ruling.outcome),
            # Normalize split-provided motion_type to snake_case (#1849).
            "motion_type": (
                normalize_motion_type(ruling.motion_type)
                if ruling.motion_type
                else None
            ),
            "department": ruling_department,
            "parties": [],
            "hearing_date": doc_hearing_date,
            "ruling_index": ruling_index,
            "split_document_id": split_doc_id,
            "is_split": True,
            "extraction_methods": {"ruling_text": "split"},
            "llm_skipped": True,
            "llm_outcome": "not_attempted",
        }

        # Track which fields came from the split
        for field in ("case_number", "case_title", "outcome", "motion_type"):
            if extracted.get(field):
                extracted["extraction_methods"][field] = "split"
        if doc_judge_name:
            extracted["extraction_methods"]["judge_name"] = "scraper"
        if doc_hearing_date:
            extracted["extraction_methods"]["hearing_date"] = "scraper"
        if ruling_department:
            extracted["extraction_methods"]["department"] = (
                "split" if getattr(ruling, "department", None) else "scraper"
            )

        # ------------------------------------------------------------------
        # Regex fallback — fill any fields still missing after split (#1749)
        # Uses the shared helper to stay in sync with _reparse_document().
        # ------------------------------------------------------------------
        _regex_t0 = time.perf_counter()
        if extracted["ruling_text"]:
            _apply_regex_fallbacks(
                extracted, extracted["ruling_text"], scraper_id=scraper_id
            )
        extracted["regex_fallback_ms"] = (time.perf_counter() - _regex_t0) * 1000.0

        # Attribute the doc-level parse_document_ms to the first ruling so
        # the per-doc timing log captures the work-once cost; subsequent
        # rulings within the same split see 0 for parse_document_ms but
        # carry their own regex_fallback_ms.  The main DB-write loop
        # accumulates across all extracted entries from the same doc.
        extracted["parse_document_ms"] = _split_parse_ms if not results else 0.0

        results.append(extracted)

    return results


# Minimum stripped length of ruling_text before the strict-retry second
# pass will run.  Mirrors ``ingestion.worker._STRICT_RETRY_MIN_TEXT_LEN``
# (#3991) — keep both values in sync.
_STRICT_RETRY_MIN_TEXT_LEN = 100


def _apply_llm_enrichment(
    extracted: dict,
    *,
    llm_client: object | None,
    llm_provider: str | None,
    llm_model: str | None,
    document_id: str,
) -> None:
    """Fill missing enrichment fields (outcome, motion_type, case_title, parties).

    The multimodal transcription pipeline only extracts ``ruling_text`` and
    identifying metadata.  Enrichment fields (``outcome``, ``motion_type``,
    ``case_title``, ``parties``) are the responsibility of a separate
    enrichment stage -- see ``docs/specs/architecture-spec-v1.md`` §5.2.1
    and ``framework.llm_enrichment``.

    This helper mirrors the live worker's ``_llm_enrich_fields`` call at
    ``worker.py`` line 1188: for any ruling with ``ruling_text`` but at
    least one missing enrichment field, it calls ``enrich_ruling()`` and
    merges the returned fields in place.

    Behaviour
    ---------
    * No-op when ``ruling_text`` is empty/``None`` (cross-contamination
      guard result) -- enrichment cannot extract from nothing.
    * No-op when all enrichment fields are already populated.
    * No-op when ``llm_client`` is ``None`` (regex-only mode).
    * Retries up to 5 attempts with exponential backoff on transient LLM
      failures.  Raises ``LlmEnrichmentExhaustedError`` on terminal
      exhaustion so the caller surfaces the failure rather than committing
      a NULL-enrichment row.
    * Only fills fields that are currently missing -- never overwrites
      existing values, matching the worker's precedence behaviour.
    * Sets ``extraction_methods[field] = "llm_enrichment"`` for each
      field it populates, matching worker.py line 1192.

    Parameters
    ----------
    extracted : dict
        A single ruling's extracted-field dict produced by
        ``convert_extracted_rulings``.  Modified in place.
    llm_client : object | None
        Pre-created LLM provider client for connection reuse.  If
        ``None``, enrichment is skipped (regex-only mode).
    llm_provider : str | None
        LLM provider name ("google" or "anthropic").  Defaults to
        ``"google"`` when ``None``.
    llm_model : str | None
        Model override.  ``None`` uses the provider default.
    document_id : str
        Original document ID, used for log correlation.
    """
    # Regex-only mode: no enrichment possible.
    if llm_client is None:
        return

    ruling_text = extracted.get("ruling_text")
    if not ruling_text or not ruling_text.strip():
        # No ruling text — enrichment cannot run.  Record diagnostic markers
        # for each field that is still missing so we can distinguish this case
        # from a successful enrichment that returned no-extraction (#3780).
        extraction_methods = extracted.setdefault("extraction_methods", {})
        if not extracted.get("outcome"):
            extraction_methods.setdefault("outcome", "llm_enrichment_skipped_no_text")
        if not extracted.get("motion_type"):
            extraction_methods.setdefault(
                "motion_type", "llm_enrichment_skipped_no_text"
            )
        if not extracted.get("case_title"):
            extraction_methods.setdefault(
                "case_title", "llm_enrichment_skipped_no_text"
            )
        if not extracted.get("parties"):
            extraction_methods.setdefault("parties", "llm_enrichment_skipped_no_text")
        return

    # Skip if all enrichment fields are already populated.
    has_case_title = bool(extracted.get("case_title"))
    has_motion_type = bool(extracted.get("motion_type"))
    has_outcome = bool(extracted.get("outcome"))
    has_parties = bool(extracted.get("parties"))
    if has_case_title and has_motion_type and has_outcome and has_parties:
        return

    extraction_methods = extracted.setdefault("extraction_methods", {})

    try:
        result = enrich_ruling_with_retry(
            ruling_text,
            provider=llm_provider or "google",
            model=llm_model,
            client=llm_client,
        )
    except LlmEnrichmentExhaustedError:
        # Terminal LLM failure — record diagnostic markers and re-raise so
        # the caller surfaces the failure rather than committing NULL rows.
        logger.warning(
            "LLM enrichment exhausted all retries",
            document_id=document_id,
        )
        if not has_outcome:
            extraction_methods.setdefault("outcome", "llm_enrichment_errored")
        if not has_motion_type:
            extraction_methods.setdefault("motion_type", "llm_enrichment_errored")
        if not has_case_title:
            extraction_methods.setdefault("case_title", "llm_enrichment_errored")
        if not has_parties:
            extraction_methods.setdefault("parties", "llm_enrichment_errored")
        raise

    # Strict-retry second pass (#3991): when the first pass leaves outcome
    # and / or motion_type null on substantive ruling text, fire one more
    # decisive LLM call asking specifically for the missing fields.  Mirrors
    # ``ingestion.worker._llm_enrich_fields``.  ``strict_retry_fields``
    # tracks which fields were filled by the strict retry so the diagnostic
    # marker can be set correctly downstream.
    strict_retry_fields: set[str] = set()
    if result is None:
        first_missing_outcome = True
        first_missing_motion_type = True
    else:
        first_missing_outcome = result.outcome is None and not has_outcome
        first_missing_motion_type = result.motion_type is None and not has_motion_type

    text_long_enough = len(ruling_text.strip()) >= _STRICT_RETRY_MIN_TEXT_LEN
    should_strict_retry = (
        first_missing_outcome or first_missing_motion_type
    ) and text_long_enough

    if should_strict_retry:
        strict_result = enrich_ruling_strict_retry(
            ruling_text,
            missing_outcome=first_missing_outcome,
            missing_motion_type=first_missing_motion_type,
            provider=llm_provider or "google",
            model=llm_model,
            client=llm_client,
        )
        if strict_result is not None:
            if result is None:
                result = LlmEnrichmentResult()
            if first_missing_outcome and strict_result.outcome is not None:
                result = result.model_copy(update={"outcome": strict_result.outcome})
                strict_retry_fields.add("outcome")
            if first_missing_motion_type and strict_result.motion_type is not None:
                result = result.model_copy(
                    update={"motion_type": strict_result.motion_type}
                )
                strict_retry_fields.add("motion_type")
            logger.info(
                "LLM enrichment strict retry completed",
                document_id=document_id,
                fields_populated=sorted(strict_retry_fields),
                missing_outcome=first_missing_outcome,
                missing_motion_type=first_missing_motion_type,
            )
        else:
            logger.warning(
                "LLM enrichment strict retry returned None — continuing with first-pass result",
                document_id=document_id,
            )

    if result is None:
        # LLM responded but extracted nothing (all-None fields), and the
        # strict-retry pass either was skipped (text too short) or also
        # returned nothing.  Record diagnostic markers and return.
        logger.info(
            "LLM enrichment returned empty result — no fields populated",
            document_id=document_id,
        )
        if not has_outcome:
            extraction_methods.setdefault("outcome", "llm_enrichment_empty")
        if not has_motion_type:
            extraction_methods.setdefault("motion_type", "llm_enrichment_empty")
        if not has_case_title:
            extraction_methods.setdefault("case_title", "llm_enrichment_empty")
        if not has_parties:
            extraction_methods.setdefault("parties", "llm_enrichment_empty")
        return

    if not has_outcome and result.outcome is not None:
        extracted["outcome"] = result.outcome
        extraction_methods["outcome"] = (
            "llm_enrichment_strict_retry"
            if "outcome" in strict_retry_fields
            else "llm_enrichment"
        )
    elif not has_outcome:
        # Enrichment ran successfully but did not return this field.
        extraction_methods.setdefault("outcome", "llm_enrichment_no_extraction")

    if not has_motion_type and result.motion_type is not None:
        extracted["motion_type"] = result.motion_type
        extraction_methods["motion_type"] = (
            "llm_enrichment_strict_retry"
            if "motion_type" in strict_retry_fields
            else "llm_enrichment"
        )
    elif not has_motion_type:
        extraction_methods.setdefault("motion_type", "llm_enrichment_no_extraction")

    if not has_case_title and result.case_title is not None:
        extracted["case_title"] = result.case_title
        extraction_methods["case_title"] = "llm_enrichment"
    elif not has_case_title:
        extraction_methods.setdefault("case_title", "llm_enrichment_no_extraction")

    if not has_parties and (result.parties.plaintiffs or result.parties.defendants):
        # Convert from EnrichmentParties to the list[dict] format used by
        # the rest of the reingest pipeline.  Matches worker.py lines
        # 1204-1208.
        parties_data: list[dict[str, str]] = []
        for name in result.parties.plaintiffs:
            parties_data.append({"name": name, "role": "plaintiff"})
        for name in result.parties.defendants:
            parties_data.append({"name": name, "role": "defendant"})
        extracted["parties"] = parties_data
        extraction_methods["parties"] = "llm_enrichment"
    elif not has_parties:
        extraction_methods.setdefault("parties", "llm_enrichment_no_extraction")

    logger.info(
        "LLM enrichment applied",
        document_id=document_id,
        outcome=extracted.get("outcome"),
        motion_type=extracted.get("motion_type"),
        case_title=(extracted.get("case_title") or "")[:80] or None,
        parties_count=len(extracted.get("parties") or []),
    )


def _reparse_document_multimodal(
    raw_content: bytes,
    scraper_id: str,
    doc_meta: dict,
    multimodal_extractor: LlmExtractor,
    pdf_timeout: float = 30.0,
    llm_client: object | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_timeout: float | None = 60.0,
    force_llm: bool = False,
    token_tracker: TokenTracker | None = None,
    force_retranscribe: bool = False,
    bust_llm_cache: bool = False,
) -> list[dict]:
    """Re-parse a PDF document using multimodal per-page extraction.

    Uses ``LlmExtractor.extract_from_pdf()`` to send page images directly
    to a multimodal LLM, bypassing pdfplumber text extraction entirely.
    This produces more accurate results for image-based PDFs (e.g., OC
    tentative rulings) where pdfplumber frequently garbles text.

    Falls back to ``_reparse_document()`` if the document format is not
    PDF or if multimodal extraction returns no results.

    After successful multimodal transcription, each extracted ruling is
    passed through ``_apply_llm_enrichment`` to populate the enrichment
    fields (``outcome``, ``motion_type``, ``case_title``, ``parties``)
    that multimodal transcription deliberately omits -- see
    ``architecture-spec-v1.md`` §5.2.1 for the stage split.  Without this
    step the reingest multimodal path would leave those fields NULL for
    Orange County (#2406).

    Returns a list of extracted-field dicts (one per ruling), in the same
    format as ``_full_reparse_document()``.

    Parameters
    ----------
    raw_content : bytes
        Raw document content from S3.
    scraper_id : str
        The scraper ID (e.g., ``"ca-oc-tentatives-civil"``).
    doc_meta : dict
        Document metadata from the DB query row.
    multimodal_extractor : LlmExtractor
        Pre-configured ``LlmExtractor`` instance (Google Flash Lite)
        for multimodal extraction.
    pdf_timeout : float
        Timeout for pdfplumber subprocess (used in text fallback).
    llm_client : object | None
        LLM client for text-based fallback extraction AND post-transcription
        enrichment.  When ``None`` (regex-only mode), the enrichment step is
        skipped.
    llm_provider : str | None
        LLM provider name for text-based fallback and enrichment.
    llm_model : str | None
        LLM model name for text-based fallback and enrichment.
    llm_timeout : float | None
        Per-call LLM timeout for text-based fallback.
    force_llm : bool
        Force LLM even when all fields are present (text fallback).
    token_tracker : TokenTracker | None
        Token tracker for cost estimation.
    """
    doc_format = doc_meta.get("format", "html")

    # Multimodal extraction only works for PDF documents.
    if doc_format != "pdf":
        result = _reparse_document(
            raw_content,
            scraper_id,
            doc_meta,
            pdf_timeout=pdf_timeout,
            llm_client=llm_client,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            force_llm=force_llm,
            token_tracker=token_tracker,
            force_retranscribe=force_retranscribe,
            bust_llm_cache=bust_llm_cache,
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        return [result]

    # Build metadata for multimodal extraction (judge_name, department,
    # hearing_date from the scraper/DB).
    metadata: dict[str, str] = {}
    if doc_meta.get("judge_name"):
        metadata["judge_name"] = str(doc_meta["judge_name"])
    if doc_meta.get("department"):
        metadata["department"] = str(doc_meta["department"])
    if doc_meta.get("hearing_date"):
        metadata["hearing_date"] = str(doc_meta["hearing_date"])

    # Try multimodal extraction.
    extracted_rulings: list[ExtractedRuling] = []
    try:
        extracted_rulings = multimodal_extractor.extract_from_pdf(
            raw_content,
            metadata=metadata or None,
            bust_cache=bust_llm_cache,
        )
    except Exception:
        logger.warning(
            "Multimodal extraction failed, falling back to text-based",
            document_id=doc_meta["document_id"],
            exc_info=True,
        )

    if not extracted_rulings:
        # Multimodal extraction returned nothing — fall back to text-based.
        logger.info(
            "Multimodal extraction returned no rulings, falling back",
            document_id=doc_meta["document_id"],
        )
        result = _reparse_document(
            raw_content,
            scraper_id,
            doc_meta,
            pdf_timeout=pdf_timeout,
            llm_client=llm_client,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_timeout=llm_timeout,
            force_llm=force_llm,
            token_tracker=token_tracker,
            force_retranscribe=force_retranscribe,
            bust_llm_cache=bust_llm_cache,
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        result["llm_outcome"] = "multimodal_fallback"
        return [result]

    # Extract doc-level judge_name and department as a fallback for
    # individual rulings where the LLM didn't extract these fields.
    # Matches the worker's fallback at worker.py line 1570 (#2063).
    #
    # Symmetric DB-seed merge (#4150, refactored in #4154 — see
    # ``_extract_doc_level_judge_department``).  Single source of truth
    # for the merge formula across all three reingest entry points;
    # protects against the silent data-loss class #3986 surfaced for
    # CourtListener (Live-only ``parse_document`` returning a no-op
    # cap_doc clobbering the DB-seeded ``judge_name`` / ``department``).
    doc_judge_name, doc_department = _extract_doc_level_judge_department(
        doc_meta, scraper_id, raw_content
    )

    # Fall back to regex extraction from the full PDF text if the scraper
    # didn't provide a judge name (e.g. OC scraper's parse_document is a no-op).
    if not doc_judge_name:
        try:
            text = _extract_text_from_content(raw_content, "pdf", pdf_timeout)
            if text:
                doc_judge_name = extract_judge_name(text)
                # Also try to extract department from the PDF header
                # (e.g. "DEPT C25" in OC headers).
                if not doc_department:
                    import re as _re

                    dept_m = _re.search(r"\bDEPT\.?\s+(\S+)", text, _re.IGNORECASE)
                    if dept_m:
                        doc_department = dept_m.group(1)
        except Exception:
            logger.warning(
                "Text extraction failed during multimodal doc-level fallback",
                document_id=doc_meta["document_id"],
                exc_info=True,
            )

    # Convert ExtractedRuling objects to the dict format used by reingest.
    content_hash = doc_meta.get("content_hash", "")
    if not content_hash:
        content_hash = hashlib.sha256(raw_content).hexdigest()

    # Convert ExtractedRuling objects using the shared multi-ruling guard
    # logic (#2084).  The cross-contamination guard and field conversion are
    # now in ingestion.ruling_guards — both worker.py and this script call
    # the same function.  Reingest-specific fields (hearing_date parsing,
    # case_number UNKNOWN fallback, extraction_methods, etc.) are layered
    # on top of the shared result.
    converted = convert_extracted_rulings(
        extracted_rulings,
        doc_meta["document_id"],
        fallback_text="",
        normalize_motion=True,
    )

    results: list[dict] = []

    for cr in converted:
        # Parse hearing_date from string to date if present.
        hearing_date_val = doc_meta.get("hearing_date")
        if cr.hearing_date:
            try:
                hearing_date_val = date.fromisoformat(cr.hearing_date)
            except (ValueError, TypeError):
                pass

        extracted: dict = {
            "ruling_text": cr.ruling_text,
            "case_number": (
                cr.case_number
                or doc_meta.get("case_number")
                or f"UNKNOWN-{cr.document_id}"
            ),
            # #2502: Do NOT fall back to ``doc_meta.get("case_title")`` here —
            # ``doc_meta`` carries the *existing* DB ``case_title`` (via the
            # LEFT JOIN in ``FETCH_DOCUMENTS_QUERY``).  Falling back would
            # create a self-sustaining fixed point for wrong titles: once a
            # bad title is stored for a case, every reingest where the LLM
            # returns no title re-writes that same wrong title back (because
            # the reingest path uses ``upsert_case(force_update=True)``).
            # Letting ``None`` flow through is SAFE: ``upsert_case``'s
            # ``force_update=True`` branch at ``db.py:316`` uses
            # ``COALESCE(EXCLUDED.case_title, cases.case_title)``, so an
            # incoming ``NULL`` preserves the existing non-null title
            # instead of erasing it.
            "case_title": cr.case_title,
            "case_type": cr.case_type,
            "judge_name": cr.judge_name or doc_judge_name,
            "outcome": cr.outcome,
            "motion_type": cr.motion_type,
            "department": cr.department or doc_department,
            "parties": cr.parties,
            "hearing_date": hearing_date_val,
            "extraction_methods": {"_all": "multimodal"},
            "llm_skipped": False,
            "llm_outcome": "multimodal_success",
            "ruling_index": cr.split_index,
            "split_document_id": cr.document_id,
            "is_split": cr.is_multi,
        }

        # Apply regex fallbacks for any fields still missing.
        # The multimodal pipeline extracts ruling_text, case_number,
        # and case_title from page images.  Other fields (judge_name,
        # outcome, motion_type) may still be missing and can be filled
        # by regex extraction from the ruling text.
        _regex_t0 = time.perf_counter()
        if extracted["ruling_text"]:
            _apply_regex_fallbacks(
                extracted, extracted["ruling_text"], scraper_id=scraper_id
            )
        else:
            # Even without ruling_text (e.g. multi-ruling PDFs where the
            # cross-contamination guard nulled out the text), we can still
            # extract case_type from case_number.  See #2270.
            # Party extraction from captions was removed in #2178 — LLM
            # enrichment handles party extraction.
            if not extracted["case_type"] and extracted["case_number"]:
                val = extract_case_type_from_number(extracted["case_number"])
                if val:
                    extracted["case_type"] = val
                    extracted["extraction_methods"].setdefault("case_type", "regex")
        extracted["regex_fallback_ms"] = (time.perf_counter() - _regex_t0) * 1000.0
        # Multimodal does not invoke pdfplumber; ``parse_document_ms`` is
        # 0 for this path.  The multimodal LLM call itself is timed
        # separately by ``LlmExtractor`` (#4116).
        extracted["parse_document_ms"] = 0.0

        # LLM enrichment — fills outcome, motion_type, case_title, parties.
        # The multimodal transcription pipeline deliberately does NOT extract
        # these enrichment-stage fields (see architecture-spec-v1.md §5.2.1);
        # without this call they would remain null on OC rulings re-ingested
        # via --multimodal.  The live worker has an equivalent call at
        # worker.py line 1188.  See #2406.
        #
        # Prefer the multimodal extractor's own client/provider/model for
        # connection reuse and to keep transcription and enrichment on the
        # same LLM family (issue #2406 requirement).  Fall back to the
        # shared text-path client if the multimodal extractor did not
        # initialize one.
        enrichment_client = getattr(multimodal_extractor, "_client", None) or llm_client
        enrichment_provider = (
            getattr(multimodal_extractor, "_provider", None) or llm_provider
        )
        enrichment_model = getattr(multimodal_extractor, "_model", None) or llm_model
        try:
            _apply_llm_enrichment(
                extracted,
                llm_client=enrichment_client,
                llm_provider=enrichment_provider,
                llm_model=enrichment_model,
                document_id=doc_meta["document_id"],
            )
        except LlmEnrichmentExhaustedError as _exc:
            logger.critical(
                "per-child enrichment exhausted on multimodal reingest — "
                "ruling will land without LLM enrichment",
                document_id=doc_meta["document_id"],
                split_document_id=extracted.get("split_document_id"),
                ruling_index=extracted.get("ruling_index"),
                case_number=extracted.get("case_number"),
                error=str(_exc),
            )

        results.append(extracted)

    logger.info(
        "Multimodal extraction completed",
        document_id=doc_meta["document_id"],
        ruling_count=len(results),
    )

    return results


def _supersede_document(
    conn: psycopg.Connection,
    document_id: str,
    change_type: str = "split_supersede",
) -> None:
    """Mark a document as superseded (replaced by split children).

    Sets ``documents.status = 'superseded'`` and ``change_type`` so the
    original unsplit document is excluded from future queries and reingest
    runs.  The row is preserved for audit trail purposes.

    Also deletes any ruling rows that reference the original document,
    since the split children create their own ruling rows with correct
    per-ruling text.  Without this, the old single-ruling row (with
    corrupted or merged text) would persist alongside the new split rows.

    Parameters
    ----------
    change_type:
        Reason code written to ``documents.change_type``.  Defaults to
        ``'split_supersede'`` (the live split path).  Pass a different
        value when calling from a backfill or test context.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM rulings WHERE document_id = %s::uuid",
            (document_id,),
        )
        deleted = cur.rowcount
        cur.execute(
            "UPDATE documents SET status = 'superseded', change_type = %s"
            " WHERE id = %s::uuid",
            (change_type, document_id),
        )
    logger.debug(
        "Superseded document %s (deleted %d old ruling(s))",
        document_id,
        deleted,
    )


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
    full_reparse: bool = False,
    processed_s3_keys: set[tuple[str, str]] | None = None,
    running_processed: int = 0,
    running_updated: int = 0,
    batch_number: int = 0,
    token_tracker: TokenTracker | None = None,
    multimodal_extractor: LlmExtractor | None = None,
    force_retranscribe: bool = False,
    bust_llm_cache: bool = False,
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

    If *full_reparse* is True, uses ``_full_reparse_document()`` which
    invokes the scraper's splitting logic (e.g. ``_split_rulings()`` for
    Riverside) to break multi-ruling PDFs into individual ruling records.
    Original unsplit documents are marked as superseded.
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
    # In full_reparse mode, deduplicate by (s3_key, s3_bucket) to avoid
    # fetching the same PDF multiple times when multiple document rows
    # share an S3 object (e.g. Riverside calendar PDFs).  See #1984.
    s3_results: dict[int, bytes] = {}
    # Map (s3_key, s3_bucket) -> row index of the *first* row that uses
    # this S3 object.  Only populated in full_reparse mode.
    s3_pair_to_first_idx: dict[tuple[str, str], int] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        for idx, row in enumerate(rows):
            s3_key = row[3]
            s3_bucket = row[4]
            if s3_key and s3_bucket:
                if full_reparse:
                    s3_pair = (s3_key, s3_bucket)
                    if s3_pair in s3_pair_to_first_idx:
                        # Another row already triggered a fetch for this
                        # object; mark this row to reuse that result later.
                        continue
                    s3_pair_to_first_idx[s3_pair] = idx
                future = pool.submit(_fetch_s3_content, s3_client, s3_bucket, s3_key)
                futures[future] = idx

        for future in as_completed(futures):
            idx = futures[future]
            doc_id_str = str(rows[idx][0])
            try:
                content = future.result()
                s3_results[idx] = content
            except Exception:
                logger.warning(
                    "Failed to fetch S3 content, skipping",
                    document_id=doc_id_str,
                    exc_info=True,
                )

    # In full_reparse mode, propagate fetched S3 content to rows that
    # share the same S3 object.
    if full_reparse:
        for idx, row in enumerate(rows):
            if idx in s3_results:
                continue
            s3_key = row[3]
            s3_bucket = row[4]
            if s3_key and s3_bucket:
                s3_pair = (s3_key, s3_bucket)
                first_idx = s3_pair_to_first_idx.get(s3_pair)
                if first_idx is not None and first_idx in s3_results:
                    s3_results[idx] = s3_results[first_idx]

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
            ruling_hearing_date,
            stored_ruling_text,
            ruling_department,
            ruling_judge_name,
        ) = row
        processed += 1
        doc_id_str = str(doc_id)
        next_cursor = (captured_at, doc_id_str)

        # Guard: in any mode that re-splits the parent PDF, skip documents
        # that are already split children.  Split children have UUID v5 IDs
        # (from make_split_document_id) and share their parent's S3 object.
        # Re-processing them would re-split the parent PDF, creating N new
        # grandchildren per child — an exponential/infinite loop.  See #1919.
        #
        # Both ``full_reparse`` and ``multimodal`` modes re-split the parent
        # PDF: ``full_reparse`` via the scraper's ``_split_rulings()`` and
        # ``multimodal`` via ``convert_extracted_rulings`` over the per-page
        # extraction results.  Without this guard in multimodal mode, the
        # Santa Clara reingest on 2026-04-16 inflated the ruling count from
        # 808 to 1,615 by creating ~10k grandchildren rows.  See #2416.
        #
        # We pass content_hash to is_split_child_id so it can distinguish
        # scraper-generated v5 IDs (from uuid5(NAMESPACE_URL, content_hash))
        # from true split-child v5 IDs.  Without the content_hash, the
        # version-check alone mis-classifies regular scraper docs as split
        # children (#2367 follow-up).
        re_split_mode = full_reparse or multimodal_extractor is not None
        if re_split_mode and is_split_child_id(doc_id_str, content_hash):
            logger.info(
                "Skipping split-child document in re-split mode",
                document_id=doc_id_str,
                mode="full_reparse" if full_reparse else "multimodal",
            )
            skipped += 1
            continue

        # Guard: in full-reparse mode, skip documents whose S3 object has
        # already been processed.  Multi-case calendar PDFs (e.g. Riverside)
        # have one S3 object shared across N document rows — one per case.
        # Without this guard, the same PDF would be split N times, producing
        # N * R ruling records instead of R (a Cartesian product).  See #1984.
        if full_reparse and processed_s3_keys is not None and s3_key and s3_bucket:
            s3_pair = (s3_key, s3_bucket)
            if s3_pair in processed_s3_keys:
                logger.info(
                    "Skipping duplicate S3 key in full-reparse mode",
                    document_id=doc_id_str,
                    s3_key=s3_key,
                )
                skipped += 1
                continue
            processed_s3_keys.add(s3_pair)

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

        # Use document hearing_date, falling back to the ruling's
        # hearing_date.  documents.hearing_date is nullable; some
        # scrapers (e.g. OC PDF) don't provide hearing_date in the
        # capture event.  When the ingestion worker later extracts it
        # via LLM/regex, it stores the date on the ruling row
        # (rulings.hearing_date NOT NULL) but the document row may
        # remain NULL.  Without this fallback, insert_ruling is
        # silently skipped during reingest because
        # insert_document_and_ruling guards on hearing_date != None.
        effective_doc_hearing = hearing_date or ruling_hearing_date

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
            "hearing_date": effective_doc_hearing,
            "court_id": str(court_id),
            "scraper_id": scraper_id,
            "s3_key": s3_key,
            "s3_bucket": s3_bucket,
            "stored_ruling_text": stored_ruling_text,
            # judge_name and department are pulled from the rulings row so
            # the multimodal LLM cache key matches the worker path.  Without
            # these, reingest builds a different content_hash_for_cache than
            # the live ingestion worker for the same PDF, causing duplicate
            # LLM spend and divergent extraction output.  See #2501.
            "judge_name": ruling_judge_name,
            "department": ruling_department,
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
    #
    # In full_reparse mode, each document may produce multiple extracted dicts
    # (one per split ruling).  parsed_docs stores (doc_meta, [extracted, ...]).
    parsed_docs: list[tuple[dict, list[dict]]] = []

    if multimodal_extractor is not None:
        parse_fn = _reparse_document_multimodal
    elif full_reparse:
        parse_fn = _full_reparse_document
    else:
        parse_fn = _reparse_document

    with ThreadPoolExecutor(max_workers=parse_workers) as pool:
        parse_futures = {}
        for idx, doc_meta, raw_content in parseable:
            if multimodal_extractor is not None:
                future = pool.submit(
                    parse_fn,
                    raw_content,
                    doc_meta["scraper_id"],
                    doc_meta,
                    multimodal_extractor,
                    parse_timeout,
                    llm_client,
                    llm_provider,
                    llm_model,
                    llm_timeout,
                    force_llm,
                    token_tracker,
                    force_retranscribe,
                    bust_llm_cache,
                )
            else:
                future = pool.submit(
                    parse_fn,
                    raw_content,
                    doc_meta["scraper_id"],
                    doc_meta,
                    parse_timeout,
                    llm_client,
                    llm_provider,
                    llm_model,
                    llm_timeout,
                    force_llm,
                    token_tracker,
                    force_retranscribe,
                    bust_llm_cache,
                )
            parse_futures[future] = (idx, doc_meta)

        for doc_index, future in enumerate(as_completed(parse_futures)):
            idx, doc_meta = parse_futures[future]
            doc_id_str = doc_meta["document_id"]
            try:
                raw_result = future.result()
            except Exception:
                logger.warning(
                    "Parse failed, skipping",
                    document_id=doc_id_str,
                    exc_info=True,
                )
                skipped += 1
                continue

            # Normalize: _reparse_document returns a single dict,
            # _full_reparse_document returns a list of dicts.
            if isinstance(raw_result, dict):
                extracted_list = [raw_result]
            else:
                extracted_list = raw_result

            for extracted in extracted_list:
                if extracted.get("llm_skipped"):
                    llm_skipped += 1

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
                ruling_count=len(extracted_list),
                full_reparse=full_reparse,
            )

            if dry_run:
                for extracted in extracted_list:
                    logger.info(
                        "DRY-RUN",
                        document_id=extracted.get("split_document_id", doc_id_str),
                        county=doc_meta["county"],
                        judge=extracted["judge_name"],
                        outcome=extracted["outcome"],
                        motion_type=extracted["motion_type"],
                        case_title=extracted["case_title"],
                        case_number=extracted["case_number"],
                        ruling_index=extracted.get("ruling_index", 0),
                        is_split=extracted.get("is_split", False),
                        parties_count=len(extracted["parties"]),
                    )
                continue

            parsed_docs.append((doc_meta, extracted_list))

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

    # --- DB writes — commit after each source document -----------------------
    # Each source document (and all its split children) is written inside a
    # single savepoint and then committed.  This ensures atomicity: either
    # all split rulings from a document are written, or none are.
    for doc_meta, extracted_list in parsed_docs:
        doc_id_str = doc_meta["document_id"]
        court_id_str = doc_meta["court_id"]

        # Per-doc phase timing (#4116).  Parse + regex_fallback ms are
        # already accumulated on each entry of ``extracted_list`` by the
        # parse functions; this loop adds them up and times its own
        # validation_ms + db_write_ms boundaries.  ``timing.emit()`` runs
        # exactly once per source document on every exit path (success,
        # exception, deterministic-FAIL skip-this-ruling).
        timing = DocTiming(
            emit_via_structlog_logger(logger),
            document_id=doc_id_str,
            county=doc_meta.get("county"),
            state=doc_meta.get("state"),
            scraper_id=doc_meta.get("scraper_id"),
        )
        for _e in extracted_list:
            if "parse_document_ms" in _e:
                timing.add_ms("parse_document_ms", _e.pop("parse_document_ms"))
            if "regex_fallback_ms" in _e:
                timing.add_ms("regex_fallback_ms", _e.pop("regex_fallback_ms"))

        try:
            with conn.transaction():
                # Check if this document was split into multiple rulings
                any_split = any(e.get("is_split") for e in extracted_list)

                # If the document was split, supersede the original BEFORE
                # inserting split children (#2365).  This must happen first
                # because insert_ruling's content-hash dedup would otherwise
                # match the old ruling (still linked to the parent document),
                # updating it in place instead of creating a new row for the
                # split child.  When _supersede_document then runs AFTER the
                # loop, it deletes those same rulings — leaving split children
                # with no ruling rows at all.
                if any_split:
                    _supersede_document(conn, doc_id_str)

                # Sibling case numbers — used by the deterministic
                # cross-case contamination check (#2371) to flag rulings
                # whose text references another case number from the
                # same multi-ruling document.  Only relevant when the
                # document split into 2+ rulings.
                sibling_case_numbers: list[str] = [
                    e.get("case_number") for e in extracted_list if e.get("case_number")
                ]

                for extracted in extracted_list:
                    # Apply #2370 post-extraction guards before any DB
                    # write: dedupe_repeated_title, strip_trailing_case_number,
                    # probate decedent rejection, hearing_date plausibility.
                    # Reingest's own extraction path bypassed
                    # ``IngestionWorker.process_event`` entirely, so this
                    # call is the only place the guards run on reingested
                    # rulings (#2405).
                    apply_post_extraction_guards(
                        extracted,
                        doc_meta.get("captured_at"),
                    )

                    # For rulings, use split_document_id if available;
                    # for documents, always use original document_id
                    # (matching ingestion worker behavior — one PDF = one
                    # document row).
                    effective_doc_id = extracted.get("split_document_id", doc_id_str)

                    # ------------------------------------------------------
                    # Deterministic validation (#2424).  Reingest's extract
                    # path bypasses ``IngestionWorker.process_event``, so
                    # the #2402 validator only fires on live ingestion.
                    # Run it here so reingest produces ``validation_results``
                    # rows for any bad rulings and skips DB writes on fail.
                    #
                    # For multi-ruling documents we pass sibling case
                    # numbers (all-except-self) so the cross-case
                    # contamination check fires.  Mirrors worker.py
                    # lines 2287-2303.
                    # ------------------------------------------------------
                    own_case_number = (
                        extracted.get("case_number")
                        or doc_meta.get("case_number")
                        or f"UNKNOWN-{effective_doc_id}"
                    )
                    det_hearing = extracted.get("hearing_date")
                    captured_ts = doc_meta.get("captured_at")
                    captured_at_date = (
                        captured_ts.date()
                        if isinstance(captured_ts, datetime)
                        else captured_ts
                    )
                    other_nums = [
                        num
                        for num in sibling_case_numbers
                        if num and num != own_case_number
                    ]
                    _val_t0 = time.perf_counter()
                    det_result = run_deterministic_rules(
                        ruling_text=extracted.get("ruling_text"),
                        case_number=own_case_number,
                        case_title=extracted.get("case_title"),
                        hearing_date=det_hearing,
                        captured_at=captured_at_date,
                        other_case_numbers=other_nums or None,
                    )
                    timing.add_ms(
                        "validation_ms",
                        (time.perf_counter() - _val_t0) * 1000.0,
                    )
                    if det_result.overall != "pass":
                        logger.info(
                            "Deterministic validation result (reingest)",
                            document_id=effective_doc_id,
                            det_overall=det_result.overall,
                            det_failed_rules=[r.rule for r in det_result.failed_rules],
                            det_flagged_rules=[
                                r.rule for r in det_result.flagged_rules
                            ],
                            det_reasons=det_result.reasons,
                            county=doc_meta.get("county"),
                            case_number=own_case_number,
                        )
                    if det_result.overall in ("fail", "flag"):
                        det_validation_result = ValidationResult(
                            result=det_result.overall,
                            reason="; ".join(det_result.reasons),
                            model="deterministic",
                            input_tokens=0,
                            output_tokens=0,
                            latency_ms=0,
                        )
                        # Logging the validation result should never abort
                        # the document's transaction — wrap the insert in
                        # a nested savepoint so a logging failure rolls
                        # back only the validation insert and subsequent
                        # siblings can still be written (mirrors worker.py
                        # #2385).
                        try:
                            with conn.transaction():
                                insert_validation_result(
                                    conn,
                                    document_id=effective_doc_id,
                                    ruling_id=None,
                                    result=det_validation_result,
                                )
                        except Exception as exc:
                            logger.warning(
                                "Failed to log deterministic validation result "
                                "to DB: %s",
                                exc,
                            )
                    if det_result.overall == "fail":
                        logger.warning(
                            "Deterministic validation FAIL — skipping DB write",
                            document_id=effective_doc_id,
                            det_reasons=det_result.reasons,
                            county=doc_meta.get("county"),
                            case_number=own_case_number,
                        )
                        # Skip this ruling's DB write but continue with
                        # sibling rulings from the same document.
                        continue
                    _db_t0 = time.perf_counter()
                    effective_case_number = (
                        extracted["case_number"]
                        or doc_meta["case_number"]
                        or f"UNKNOWN-{effective_doc_id}"
                    )
                    # ``force_update=True`` overrides the default preserve-
                    # existing COALESCE order (#2468) so reingest can correct
                    # previously-stuck ``case_type`` / ``case_title`` values
                    # after an extraction fix (e.g. the #2368 SF family-court
                    # regex correction that left ``cases.case_type``
                    # stuck on ``criminal``).  A NULL incoming value still
                    # falls through to the preserved ``cases.*`` column —
                    # we never erase a good existing value with an incoming
                    # NULL.  Mirrors the ``force_update`` flag already
                    # passed to ``insert_document_and_ruling`` below (#2431).
                    new_case_id = upsert_case(
                        conn,
                        effective_case_number,
                        court_id_str,
                        case_title=extracted["case_title"],
                        case_type=extracted.get("case_type"),
                        force_update=True,
                    )

                    # #2456: single chokepoint owns the fallback chain +
                    # plausibility check so no fallback branch can bypass
                    # the #2370 guard.  See ``finalize_hearing_date`` for
                    # the full rationale (#2370 / #2405 / #2432 / #4251).
                    effective_hearing = finalize_hearing_date(
                        extracted_hearing=extracted["hearing_date"],
                        doc_meta_hearing=doc_meta["hearing_date"],
                        capture_timestamp=doc_meta.get("captured_at"),
                        case_type=extracted.get("case_type"),
                    )

                    # For split documents, generate a synthetic content hash
                    # by incorporating the ruling index.  All split children
                    # share the same S3 object (s3_key, s3_bucket), so a
                    # unique hash per ruling is needed to satisfy the DB's
                    # UNIQUE constraint on (s3_bucket, s3_key, content_hash).
                    if extracted.get("is_split"):
                        ruling_idx = extracted.get("ruling_index", 0)
                        split_hash = hashlib.sha256(
                            f"{doc_meta['content_hash']}:ruling:{ruling_idx}".encode()
                        ).hexdigest()
                    else:
                        split_hash = doc_meta["content_hash"]

                    # Resolve judge.  When LLM/regex extraction didn't
                    # find a judge_name but a department is available,
                    # fall back to the court directory snapshot (#2269).
                    judge_name = extracted["judge_name"]
                    if (
                        not judge_name
                        and extracted.get("department")
                        and doc_meta.get("state", "").upper() == "CA"
                    ):
                        hearing_dt = extracted.get("hearing_date")
                        judge_name = resolve_judge_from_department(
                            conn,
                            court_id_str,
                            extracted["department"],
                            hearing_date=hearing_dt,
                        )
                        if judge_name:
                            extracted["extraction_methods"]["judge_name"] = (
                                "roster_dept_lookup"
                            )
                            logger.info(
                                "Resolved judge from court directory snapshot",
                                department=extracted["department"],
                                judge_name=judge_name,
                                county=doc_meta.get("county"),
                            )
                    judge_id = None
                    if judge_name:
                        judge_id = resolve_judge(conn, judge_name, court_id_str)

                    # Truncate excessively long ruling text
                    ruling_text = extracted["ruling_text"]
                    if ruling_text and len(ruling_text) > 50000:
                        ruling_text = ruling_text[:50000]

                    # Insert document + ruling via shared helper (#1790).
                    # The helper guarantees the same document_id is passed
                    # to both insert_document and insert_ruling, preventing
                    # FK divergence (#1775).
                    #
                    # ``force_update=True`` overrides the default COALESCE
                    # upsert semantics so reingest can overwrite bad
                    # historical case_id / judge_id / hearing_date /
                    # outcome / motion_type / department with the new
                    # values (including NULL when a guard cleared the
                    # field).  Text / summary fields always keep COALESCE —
                    # see ``db.insert_ruling`` docstring (#2405).
                    insert_document_and_ruling(
                        conn,
                        document_id=effective_doc_id,
                        case_id=new_case_id,
                        court_id=court_id_str,
                        content_format=doc_meta["format"],
                        content_hash=split_hash,
                        s3_key=doc_meta["s3_key"],
                        s3_bucket=doc_meta["s3_bucket"],
                        source_url=doc_meta["source_url"],
                        scraper_id=doc_meta["scraper_id"],
                        captured_at=doc_meta["captured_at"],
                        hearing_date=effective_hearing,
                        ruling_text=ruling_text,
                        department=extracted["department"],
                        judge_id=judge_id,
                        # Safety net: normalize outcome to valid enum value
                        # before writing to DB (#2113).  The upstream
                        # extraction paths should already normalize, but
                        # this guards against any missed code path.
                        outcome=normalize_outcome(extracted["outcome"]),
                        motion_type=extracted["motion_type"],
                        force_update=True,
                    )

                    if judge_id:
                        upsert_case_judge(
                            conn, new_case_id, judge_id, effective_hearing
                        )

                    # Parties
                    batch_upsert_parties(
                        conn, new_case_id, extracted.get("parties", [])
                    )
                    timing.add_ms(
                        "db_write_ms", (time.perf_counter() - _db_t0) * 1000.0
                    )

            conn.commit()
            updated += 1

            logger.info(
                "Committed document",
                document_id=doc_id_str,
                ruling_count=len(extracted_list),
                split=any_split,
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
        finally:
            # Emit the per-doc ``reingest_doc_timing`` log line on every
            # exit path (commit success, exception, deterministic FAIL).
            # Idempotent — DocTiming.emit guards against double-emission.
            timing.emit()

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


# ---------------------------------------------------------------------------
# Quality metrics queries — spotcheck data quality before/after reingest
# ---------------------------------------------------------------------------

# SQL queries for data quality metrics.  Each returns a single integer count.
# All queries filter on active documents only.
_QUALITY_QUERIES: dict[str, str] = {
    "truncated_vs_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title ~ 'vs\\.?\\s*$'
        {county_filter}
    """,
    "header_merge_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title ~ '(?i)(Before the Court|moves the|Hearing on|Motion for)'
          AND c.case_title ~ ' vs?\\.? '
          AND length(c.case_title) > 100
        {county_filter}
    """,
    "null_case_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title IS NULL
        {county_filter}
    """,
    "missing_parties": """
        SELECT COUNT(DISTINCT c.id) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        LEFT JOIN case_parties cp ON cp.case_id = c.id
        WHERE cp.id IS NULL
        {county_filter}
    """,
    "all_caps_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title = upper(c.case_title)
          AND c.case_title IS NOT NULL
          AND length(c.case_title) > 5
        {county_filter}
    """,
    "short_ruling_text": """
        SELECT COUNT(*) FROM rulings r
        JOIN documents d ON d.id::text = r.document_id::text AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE length(r.ruling_text) < 20
        {county_filter}
    """,
    "long_ruling_text": """
        SELECT COUNT(*) FROM rulings r
        JOIN documents d ON d.id::text = r.document_id::text AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE length(r.ruling_text) > 30000
        {county_filter}
    """,
    "total_rulings": """
        SELECT COUNT(*) FROM rulings r
        JOIN documents d ON d.id::text = r.document_id::text AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE 1=1
        {county_filter}
    """,
}


def _run_quality_queries(
    conn: psycopg.Connection,
    county: str | None = None,
) -> dict[str, int]:
    """Execute all spotcheck quality queries and return results.

    If *county* is given, each query is scoped to that county.
    Returns a dict mapping metric name to its integer count.
    """
    county_filter = ""
    params: list[str] = []
    if county:
        county_filter = "AND ct.county = %s"
        params = [county]

    results: dict[str, int] = {}
    with conn.cursor() as cur:
        for name, query_template in _QUALITY_QUERIES.items():
            query = query_template.format(county_filter=county_filter)
            cur.execute(query, params)
            row = cur.fetchone()
            results[name] = row[0] if row else 0
    return results


# ---------------------------------------------------------------------------
# Prefix mode — scan S3 directly for documents not in the DB
# ---------------------------------------------------------------------------

# S3 content-addressed key pattern: {state}/{county}/{court}/raw/{content_hash}.{ext}
_S3_KEY_PATTERN = re.compile(
    r"^(?P<state>[^/]+)/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/"
    r"(?P<content_hash>[0-9a-f]+)\.(?P<ext>\w+)$"
)

_EXT_TO_FORMAT = {"html": "html", "pdf": "pdf", "docx": "docx", "txt": "txt"}

# Timezone lookup by state (expand as states are added)
_STATE_TIMEZONES = {
    "ca": "America/Los_Angeles",
    "tx": "America/Chicago",
    "ny": "America/New_York",
}


def _unsluggify(s: str) -> str:
    """Convert slug to display name: 'los_angeles' -> 'Los Angeles', 'ca' -> 'CA'."""
    if len(s) <= 2:
        return s.upper()
    return s.replace("_", " ").title()


def _parse_s3_key(key: str) -> dict[str, str] | None:
    """Extract metadata from a content-addressed S3 key.

    Returns a dict with keys ``state``, ``county``, ``court``,
    ``content_hash``, and ``ext``, or ``None`` if the key doesn't
    match the expected pattern.
    """
    m = _S3_KEY_PATTERN.match(key)
    if not m:
        return None
    return m.groupdict()


def _list_s3_keys(s3_client: object, bucket: str, prefix: str) -> list[str]:
    """List all content-addressed S3 keys under *prefix*."""
    paginator = s3_client.get_paginator("list_objects_v2")  # type: ignore[union-attr]
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if _S3_KEY_PATTERN.match(key):
                keys.append(key)
    return keys


def _derive_court_code(state: str, county: str) -> str:
    """Derive a URL-safe court code from state + county.

    Must match the canonical format in ``ingestion.db._derive_court_code``
    so that ``ON CONFLICT (court_code)`` upserts hit the same row the
    ingestion worker creates.  See #2373.
    """
    return f"{state.lower()}-{county.lower().replace(' ', '-')}"


def _discover_courts(keys: list[str]) -> list[dict[str, str]]:
    """Derive unique courts from S3 key prefixes.

    Uses the same ``{state}-{county}`` court_code format as the ingestion
    worker so that ``ON CONFLICT (court_code)`` in ``_seed_courts`` merges
    with existing rows instead of creating duplicates.  See #2373.
    """
    seen: set[str] = set()
    courts: list[dict[str, str]] = []
    for key in keys:
        parsed = _parse_s3_key(key)
        if not parsed:
            continue
        state = _unsluggify(parsed["state"])
        county = _unsluggify(parsed["county"])
        court_name = _unsluggify(parsed["court"])
        court_code = _derive_court_code(state, county)
        if court_code in seen:
            continue
        seen.add(court_code)
        courts.append(
            {
                "state": state,
                "county": county,
                "court_name": f"{court_name}, County of {county}",
                "court_code": court_code,
                "timezone": _STATE_TIMEZONES.get(
                    parsed["state"], "America/Los_Angeles"
                ),
            }
        )
    return courts


def _seed_courts(
    conn: psycopg.Connection, courts: list[dict[str, str]]
) -> dict[str, str]:
    """Insert or update court records. Returns ``{court_code: court_id}``."""
    court_ids: dict[str, str] = {}
    with conn.cursor() as cur:
        for court in courts:
            cur.execute(
                """
                INSERT INTO courts (state, county, court_name, court_code, timezone)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (court_code) DO UPDATE
                    SET state = EXCLUDED.state,
                        county = EXCLUDED.county,
                        court_name = EXCLUDED.court_name
                RETURNING id
                """,
                (
                    court["state"],
                    court["county"],
                    court["court_name"],
                    court["court_code"],
                    court["timezone"],
                ),
            )
            row = cur.fetchone()
            court_ids[court["court_code"]] = str(row[0])
    conn.commit()
    return court_ids


def _build_prefix_event(
    key: str,
    content: bytes,
    parsed: dict[str, str],
    bucket: str,
) -> dict[str, Any]:
    """Construct an ingestion event dict from an S3 object for prefix mode."""
    content_hash = parsed["content_hash"]
    document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, content_hash))
    content_format = _EXT_TO_FORMAT.get(parsed["ext"], "bin")

    event: dict[str, Any] = {
        "document_id": document_id,
        "state": _unsluggify(parsed["state"]),
        "county": _unsluggify(parsed["county"]),
        "court": _unsluggify(parsed["court"]),
        "content_format": content_format,
        "content_hash": content_hash,
        "s3_key": key,
        "s3_bucket": bucket,
        "scraper_id": f"reingest-{parsed['state']}-{parsed['county']}",
        "source_url": "",
        "capture_timestamp": datetime.now(UTC).isoformat(),
    }

    # For text-based formats (HTML, TXT), pass content as ruling_text.
    # For binary formats (PDF, DOCX), pass raw bytes as latin-1 string
    # (the worker handles extraction from binary formats).
    if content_format in ("html", "txt"):
        event["ruling_text"] = content.decode("utf-8", errors="replace")
    elif content_format in ("pdf", "docx"):
        event["ruling_text"] = content.decode("latin-1")

    return event


def _process_prefix_document(
    key: str,
    bucket: str,
    database_url: str,
    redis_url: str,
    os_url: str,
    bust_llm_cache: bool = False,
) -> dict[str, Any]:
    """Process a single S3 object through the ingestion pipeline.

    Creates its own DB connection, Redis client, and IngestionWorker.
    Designed for ProcessPoolExecutor — each call is fully independent.

    Parameters
    ----------
    bust_llm_cache:
        When True, the per-process ``IngestionWorker`` is constructed with
        ``bust_llm_cache=True`` so every ``LlmExtractor`` it creates skips
        cache reads.  This is the only path through which already-split
        documents (parents whose split-child rows live in
        ``derived.documents``) can be re-extracted with a fresh LLM call —
        DB-row mode (``--multimodal --bust-llm-cache``) correctly skips
        split-child rows to avoid the #2416 exponential explosion, leaving
        prefix-mode the sole avenue once a parent has been split.  See
        #4049.

    Returns a dict with:
      - ``status``: ``"ok"``, ``"skip"``, or ``"error"``
      - ``hash_mismatch``: whether the S3 key hash did not match the object
        bytes' SHA-256.  Non-fatal — reingest proceeds using the key hash as
        the canonical ``content_hash`` so the worker's LLM split path can
        re-derive any split-children from the raw content.  Mirrors the
        behavior already in ``rebuild_db._process_one_document`` (see #2494,
        #2628).
    """
    parsed = _parse_s3_key(key)
    if not parsed:
        return {"status": "skip", "hash_mismatch": False}

    from framework.s3_cache import make_s3_client as _make_s3

    s3 = _make_s3()
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()
    except Exception:
        logger.warning("Failed to fetch S3 object, skipping", s3_key=key, exc_info=True)
        return {"status": "error", "hash_mismatch": False}

    if not content:
        return {"status": "skip", "hash_mismatch": False}

    # Byte-integrity check.  A mismatch used to short-circuit with
    # ``status="error"``, but that caused the ~4% flat-hash orphan rate on
    # Santa Clara (#2628): raws stored under a wrong content-hash filename
    # were permanently unreingestable.  Now we log a warning and let the
    # worker process the raw; the LLM split path derives split-children from
    # the content and stores them with hashes derived from the canonical key
    # hash.  Matches ``rebuild_db._process_one_document`` (#2494).
    #
    # Root cause of the mislabeled filenames: the 2026-03-28 one-time
    # migration script ``scripts/archive/migrate_s3_keys.py`` used DB
    # ``content_hash`` values to build new S3 keys, but for multi-case-PDF
    # split-child rows that value is synthetic (not the hash of any bytes).
    # Live scraper writes are correct.  See #2638 and
    # ``docs/investigations/mislabeled-s3-writes-2026-04.md``.
    actual_hash = hashlib.sha256(content).hexdigest()
    hash_mismatch = actual_hash != parsed["content_hash"]
    if hash_mismatch:
        logger.warning(
            "S3 content hash mismatch — proceeding with reingest using key "
            "hash as canonical content_hash (see #2494, #2628)",
            s3_key=key,
            key_hash=parsed["content_hash"],
            actual_content_hash=actual_hash,
        )

    event = _build_prefix_event(key, content, parsed, bucket)

    # Lazy per-process worker — cached on the function object.
    worker = getattr(_process_prefix_document, "_worker", None)
    if worker is None:
        import redis as redis_lib
        from unittest.mock import MagicMock

        from ingestion.worker import IngestionWorker

        rc = redis_lib.Redis.from_url(redis_url, decode_responses=False)
        if os_url:
            from opensearchpy import OpenSearch

            # 30s timeout + 3 retries: opensearchpy defaults to 10s and no
            # retries, which produces sporadic ``ConnectionTimeout`` entries
            # under load (#2481).  ``IndexingConsumer`` also swallows
            # terminal timeouts as a warning so a missed index never fails
            # document processing.
            os_kwargs: dict = {
                "hosts": [os_url],
                "timeout": 30,
                "max_retries": 3,
                "retry_on_timeout": True,
            }
            os_user = os.environ.get("OPENSEARCH_USERNAME", "")
            os_pass = os.environ.get("OPENSEARCH_PASSWORD", "")
            if os_user and os_pass:
                os_kwargs["http_auth"] = (os_user, os_pass)
            os_client = OpenSearch(**os_kwargs)
        else:
            os_client = MagicMock()
        s3_for_worker = _make_s3()
        # ``bust_llm_cache=...`` propagates the operator's --bust-llm-cache
        # flag through this worker into every ``LlmExtractor`` it creates
        # (multimodal + framework).  This is the only way to re-extract
        # already-split parent PDFs with a fresh LLM call — see #4049.
        worker = IngestionWorker(
            redis_client=rc,
            pg_dsn=database_url,
            opensearch_client=os_client,
            s3_client=s3_for_worker,
            archive_bucket=bucket,
            bust_llm_cache=bust_llm_cache,
        )
        _process_prefix_document._worker = worker  # type: ignore[attr-defined]

    try:
        worker.process_event(event)
        return {"status": "ok", "hash_mismatch": hash_mismatch}
    except Exception:
        logger.warning("Failed to process document", s3_key=key, exc_info=True)
        return {"status": "error", "hash_mismatch": hash_mismatch}


def run_reingest_from_prefix(
    dsn: str,
    *,
    prefix: str,
    concurrency: int = 10,
    limit: int | None = None,
    dry_run: bool = False,
    bust_llm_cache: bool = False,
) -> dict[str, Any]:
    """Scan S3 by key prefix and ingest documents not in the DB.

    Unlike :func:`run_reingest`, which queries existing DB records for
    re-processing, this function lists S3 objects directly and feeds each
    one through ``IngestionWorker.process_event()``.  This is the correct
    approach when documents were captured and archived to S3 but never
    written to the database (e.g. dead-lettered events).

    Parameters
    ----------
    dsn:
        PostgreSQL connection string.
    prefix:
        S3 key prefix to scan (e.g. ``"federal/"``).
    concurrency:
        Number of parallel worker processes.
    limit:
        Maximum number of documents to process.
    dry_run:
        If True, list keys and seed courts but skip document processing.
    bust_llm_cache:
        If True, the per-process ``IngestionWorker`` is constructed with
        ``bust_llm_cache=True`` so every ``LlmExtractor`` it builds skips
        cache reads.  Required to re-extract documents whose parent PDF has
        already been split into per-ruling child rows: DB-row mode (the
        ``--multimodal`` path) correctly skips split-child rows via the
        ``is_split_child_id`` guard to avoid the #2416 exponential
        explosion, so the prefix path is the only avenue once a parent has
        been split.  See #4049.

    Returns
    -------
    dict with keys: ``total_keys``, ``processed``, ``errors``, ``skipped``.
    """
    bucket = os.environ.get(
        "JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev"
    )
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    os_url = os.environ.get("OPENSEARCH_URL", "")

    s3_client = boto3.client("s3")

    # Step 1: List S3 keys matching the prefix.
    logger.info("Listing S3 objects...", prefix=prefix, bucket=bucket)
    keys = _list_s3_keys(s3_client, bucket, prefix)
    logger.info("Found S3 objects", count=len(keys), prefix=prefix)

    if not keys:
        logger.warning("No content-addressed keys found under prefix", prefix=prefix)
        return {
            "total_keys": 0,
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }

    if limit is not None:
        total_available = len(keys)
        keys = keys[:limit]
        logger.info(
            "Limited to first N keys", limit=limit, total_available=total_available
        )

    # Step 2: Discover and seed courts from key prefixes.
    courts = _discover_courts(keys)
    logger.info(
        "Discovered courts from S3 keys",
        count=len(courts),
        courts=[c["court_code"] for c in courts],
    )

    with psycopg.connect(dsn) as conn:
        court_ids = _seed_courts(conn, courts)
    logger.info("Courts seeded", court_ids=court_ids)

    if dry_run:
        logger.info(
            "Dry run — skipping document processing",
            total_keys=len(keys),
            courts_seeded=len(court_ids),
        )
        return {
            "total_keys": len(keys),
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }

    # Step 3: Process documents using child processes.
    # Uses ProcessPoolExecutor (like rebuild_db.py) because pdfplumber/pdfminer
    # C extensions are not thread-safe.
    database_url = dsn
    if not os_url:
        logger.info("OPENSEARCH_URL not set — skipping search indexing")

    t_start = time.monotonic()
    processed = 0
    errors = 0
    skipped = 0
    hash_mismatch_warnings = 0

    logger.info(
        "Processing documents from S3",
        concurrency=concurrency,
        total=len(keys),
    )

    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _process_prefix_document,
                key,
                bucket,
                database_url,
                redis_url,
                os_url,
                bust_llm_cache,
            ): key
            for key in keys
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
                # ``_process_prefix_document`` returns a dict with ``status``
                # and ``hash_mismatch`` keys.  Older versions returned a bare
                # string — tolerate that shape for one release to avoid
                # breaking any callers that pickled intermediate results.
                if isinstance(result, dict):
                    status = result.get("status", "error")
                    if result.get("hash_mismatch"):
                        hash_mismatch_warnings += 1
                else:
                    status = result
                if status == "ok":
                    processed += 1
                elif status == "skip":
                    skipped += 1
                else:
                    errors += 1
                total_done = processed + errors + skipped
                if processed > 0 and processed % 50 == 0:
                    elapsed = time.monotonic() - t_start
                    elapsed_min = elapsed / 60
                    keys_per_min = total_done / elapsed * 60 if elapsed > 0 else 0
                    eta_min = (
                        (len(keys) - total_done) / (total_done / elapsed) / 60
                        if total_done > 0 and elapsed > 0
                        else 0
                    )
                    logger.info(
                        "Progress",
                        keys_done=total_done,
                        keys_total=len(keys),
                        pct=round(100 * total_done / len(keys), 1),
                        keys_per_min=round(keys_per_min, 1),
                        elapsed_min=round(elapsed_min, 1),
                        eta_min=round(eta_min, 1),
                        errors=errors,
                    )
            except Exception as exc:
                errors += 1
                logger.error("Failed to process", key=key, error=str(exc))

    wall_time = round(time.monotonic() - t_start, 2)

    stats: dict[str, Any] = {
        "total_keys": len(keys),
        "processed": processed,
        "errors": errors,
        "skipped": skipped,
        "hash_mismatch_warnings": hash_mismatch_warnings,
        "wall_time_seconds": wall_time,
    }

    logger.info(
        "Prefix reingest complete",
        **stats,
    )

    if hash_mismatch_warnings > 0:
        logger.warning(
            "%d raw S3 objects had content-hash mismatches — reingest "
            "proceeded using the S3 key hash as canonical content_hash "
            "(see #2494, #2628).  Investigate the scraper S3 upload path "
            "if this count is non-trivial.",
            hash_mismatch_warnings,
            hash_mismatch_warnings=hash_mismatch_warnings,
            processed=processed,
        )

    return stats


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
    full_reparse: bool = False,
    case_title_regex: str | None = None,
    null_motion_type: bool = False,
    orphaned_only: bool = False,
    report_metrics: bool = False,
    multimodal: bool = False,
    checkpoint_file: str | None = None,
    resume: bool = False,
    case_number_like: str | None = None,
    force_retranscribe: bool = False,
    filter_null_outcome: bool = False,
    bust_llm_cache: bool = False,
    department_in: list[str] | None = None,
) -> dict[str, Any]:
    """Run the full reingest. Returns summary stats including cost.

    Parameters
    ----------
    checkpoint_file:
        If provided, the cursor position and cumulative stats are written to
        this file after each batch.  On interruption, the run can be resumed
        from the checkpoint by passing ``resume=True``.
    resume:
        When *True* **and** *checkpoint_file* points to an existing file,
        the cursor and cumulative stats are restored from the checkpoint
        instead of starting from the beginning.  Requires *checkpoint_file*.
    filter_null_outcome:
        When *True*, only re-ingest documents that have at least one ruling
        with a NULL outcome.  Useful for targeted reprocessing when only a
        subset of documents need outcome extraction.
    """
    filters, filter_params = _build_filters(
        county,
        date_from,
        date_to,
        case_title_regex=case_title_regex,
        null_motion_type=null_motion_type,
        orphaned_only=orphaned_only,
        case_number_like=case_number_like,
        filter_null_outcome=filter_null_outcome,
        department_in=department_in,
    )

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

    # Create multimodal extractor for per-page PDF extraction (#1719).
    #
    # The default ``LlmExtractor.max_output_tokens`` of 4,096 truncates the
    # per-page JSON response on dense multi-case department-calendar PDFs
    # (e.g. Orange County), which leaves ruling_text empty for later cases
    # and blocks downstream outcome/motion_type enrichment.  Matching the
    # text-based county configs, the multimodal extractor is created with
    # ``max_output_tokens=32768`` so per-page responses fit comfortably
    # (#2369).  All multimodal counties today benefit from the higher limit.
    multimodal_extractor: LlmExtractor | None = None
    if multimodal:
        try:
            multimodal_extractor = LlmExtractor(
                provider="google",
                max_output_tokens=32768,
                bust_cache=bust_llm_cache,
            )
            logger.info(
                "Multimodal extraction enabled (Google Flash Lite)",
                model=multimodal_extractor._model,
                max_output_tokens=32768,
                bust_cache=bust_llm_cache,
            )
        except Exception:
            logger.error(
                "Failed to initialize multimodal LlmExtractor — GOOGLE_API_KEY may be missing",
                exc_info=True,
            )
            sys.exit(1)

    # Token tracking for cost estimation.
    tracker = TokenTracker()

    total_processed = 0
    total_updated = 0
    total_llm_skipped = 0
    total_failed = 0
    total_skipped = 0
    total_batches = 0
    total_llm_success = 0
    total_llm_failure = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    # Resolve checkpoint path (if provided).
    cp_path: Path | None = None
    if checkpoint_file:
        cp_path = Path(checkpoint_file)

    # Resume from checkpoint if requested.
    if resume and cp_path is not None and cp_path.exists():
        restored_cursor, restored_stats = _read_checkpoint(cp_path)
        cursor = restored_cursor
        total_processed = restored_stats.get("total_processed", 0)
        total_updated = restored_stats.get("total_updated", 0)
        total_llm_skipped = restored_stats.get("total_llm_skipped", 0)
        total_failed = restored_stats.get("total_failed", 0)
        total_skipped = restored_stats.get("total_skipped", 0)
        total_batches = restored_stats.get("total_batches", 0)
        total_llm_success = restored_stats.get("total_llm_success", 0)
        total_llm_failure = restored_stats.get("total_llm_failure", 0)
        logger.info(
            "Resumed from checkpoint",
            checkpoint_file=str(cp_path),
            cursor_captured_at=cursor[0].isoformat(),
            cursor_document_id=cursor[1],
            total_processed=total_processed,
        )

    t0 = time.monotonic()

    # Collect quality metrics before reingest if requested.
    before_metrics: dict[str, int] | None = None
    if report_metrics:
        with psycopg.connect(dsn) as metrics_conn:
            before_metrics = _run_quality_queries(metrics_conn, county)
            logger.info("quality_metrics_before", **before_metrics)

    # Track processed S3 keys across batches to avoid Cartesian products
    # in full-reparse mode.  Multi-case calendar PDFs (e.g. Riverside)
    # share one S3 object across many document rows.  Without cross-batch
    # dedup, the same PDF could be split multiple times if the document
    # rows span batch boundaries.  See #1984.
    s3_dedup: set[tuple[str, str]] | None = set() if full_reparse else None

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
                full_reparse=full_reparse,
                processed_s3_keys=s3_dedup,
                running_processed=total_processed,
                running_updated=total_updated,
                batch_number=total_batches,
                token_tracker=tracker,
                multimodal_extractor=multimodal_extractor,
                force_retranscribe=force_retranscribe,
                bust_llm_cache=bust_llm_cache,
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

            # Persist checkpoint after each batch so we can resume on
            # interruption without re-processing already-finished documents.
            # Skip checkpoint writes in dry-run mode — a dry run should not
            # produce side effects that could cause a subsequent real run
            # with --resume to skip documents (#1925).
            if cp_path is not None and not dry_run:
                _write_checkpoint(
                    cp_path,
                    cursor,
                    {
                        "total_processed": total_processed,
                        "total_updated": total_updated,
                        "total_llm_skipped": total_llm_skipped,
                        "total_failed": total_failed,
                        "total_skipped": total_skipped,
                        "total_batches": total_batches,
                        "total_llm_success": total_llm_success,
                        "total_llm_failure": total_llm_failure,
                    },
                )

            if processed < effective_batch:
                break

    wall_time = round(time.monotonic() - t0, 2)

    # Compute cost estimate.
    estimated_cost_usd = tracker.estimated_cost(provider=llm_provider)

    # Collect quality metrics after reingest if requested.
    after_metrics: dict[str, int] | None = None
    metrics_delta: dict[str, int] | None = None
    if report_metrics:
        with psycopg.connect(dsn) as metrics_conn:
            after_metrics = _run_quality_queries(metrics_conn, county)
            logger.info("quality_metrics_after", **after_metrics)
        if before_metrics is not None and after_metrics is not None:
            metrics_delta = {
                k: after_metrics.get(k, 0) - before_metrics.get(k, 0)
                for k in before_metrics
            }
            logger.info("quality_metrics_delta", **metrics_delta)

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
        input_tokens=tracker.input_tokens,
        output_tokens=tracker.output_tokens,
        llm_api_calls=tracker.api_calls,
        estimated_cost_usd=round(estimated_cost_usd, 4),
    )

    result: dict[str, Any] = {
        "total_processed": total_processed,
        "total_updated": total_updated,
        "total_llm_skipped": total_llm_skipped,
        "total_failed": total_failed,
        "total_skipped": total_skipped,
        "total_batches": total_batches,
        "llm_success": total_llm_success,
        "llm_failure": total_llm_failure,
        "wall_time_seconds": wall_time,
        "input_tokens": tracker.input_tokens,
        "output_tokens": tracker.output_tokens,
        "llm_api_calls": tracker.api_calls,
        "estimated_cost_usd": round(estimated_cost_usd, 4),
    }
    if before_metrics is not None:
        result["quality_before"] = before_metrics
    if after_metrics is not None:
        result["quality_after"] = after_metrics
    if metrics_delta is not None:
        result["quality_delta"] = metrics_delta
    return result


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
    parser.add_argument(
        "--full-reparse",
        action="store_true",
        help=(
            "Enable full reparse with splitting logic. For scrapers that "
            "produce multi-ruling documents (e.g. Riverside), this splits "
            "each PDF into individual ruling records and supersedes the "
            "original unsplit document. Deterministic IDs ensure idempotency."
        ),
    )
    parser.add_argument(
        "--case-number-like",
        type=str,
        default=None,
        help=(
            "Only re-ingest documents whose associated case_number matches "
            "this PostgreSQL LIKE pattern. Useful for targeting placeholder "
            "case numbers, e.g. 'UNKNOWN-%%' to find UNKNOWN-UUID cases."
        ),
    )
    parser.add_argument(
        "--case-title-regex",
        type=str,
        default=None,
        help=(
            "Only re-ingest documents whose current case_title matches this "
            "PostgreSQL regex (~ operator). Useful for targeting garbled "
            "titles, e.g. 'vs\\.?\\s*$' to find truncated titles."
        ),
    )
    parser.add_argument(
        "--null-motion-type",
        action="store_true",
        help=(
            "Only re-ingest documents whose associated rulings have NULL "
            "motion_type. Useful after a normalization backfill that set "
            "unmappable motion types to NULL — re-ingestion lets the "
            "enrichment pipeline extract motion_type from ruling text."
        ),
    )
    parser.add_argument(
        "--filter-null-outcome",
        action="store_true",
        help=(
            "Only re-ingest documents that have at least one ruling "
            "with a NULL outcome. Useful for targeted reprocessing "
            "after extraction improvements — avoids re-ingesting an "
            "entire county when only a small subset of documents need "
            "outcome extraction."
        ),
    )
    parser.add_argument(
        "--orphaned-only",
        action="store_true",
        help=(
            "Only re-ingest documents that have no associated ruling "
            "records. Useful after a backfill that created document "
            "records but did not process them through transcription "
            "and enrichment."
        ),
    )
    parser.add_argument(
        "--report-metrics",
        action="store_true",
        help=(
            "Run data quality spotcheck queries before and after reingest "
            "and report the comparison. Checks truncated titles, header-merge "
            "titles, null titles, missing parties, ALL CAPS titles, "
            "short/long ruling text, and total ruling counts."
        ),
    )
    parser.add_argument(
        "--multimodal",
        action="store_true",
        help=(
            "Use multimodal per-page LLM extraction for PDF documents. "
            "Sends page images directly to a multimodal LLM (Google Flash "
            "Lite), bypassing pdfplumber text extraction. Produces more "
            "accurate results for image-based PDFs like OC tentative "
            "rulings. Requires GOOGLE_API_KEY environment variable."
        ),
    )
    parser.add_argument(
        "--checkpoint-file",
        type=str,
        default=None,
        help=(
            "Path to a JSON checkpoint file. After each batch, the current "
            "cursor position and cumulative stats are written here. Use with "
            "--resume to restart from the last checkpoint on interruption."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume from the checkpoint file specified by --checkpoint-file. "
            "Reads the saved cursor position and cumulative stats, then "
            "continues processing from where the previous run left off. "
            "Requires --checkpoint-file to point to an existing checkpoint."
        ),
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help=(
            "Scan S3 directly by key prefix instead of querying the database. "
            "Useful for ingesting documents that were never written to the DB "
            "(e.g. dead-lettered events). Lists S3 objects under the prefix, "
            "seeds court records, and processes each document through the "
            "ingestion pipeline via IngestionWorker.process_event(). "
            "Example: --prefix federal/. "
            "Combine with --bust-llm-cache to re-extract already-split "
            "documents (the only path once parent PDFs have produced "
            "split-child rows; DB-row mode skips them to avoid #2416). "
            "See #4049."
        ),
    )
    parser.add_argument(
        "--bust-llm-cache",
        action="store_true",
        help=(
            "Skip LLM cache reads for this run (#2424). Every document "
            "re-runs through the LLM regardless of cached results, which "
            "is required after prompt updates or extraction-logic changes. "
            "Cache *writes* still happen, so subsequent runs without this "
            "flag benefit from the fresh extraction. Use this when you "
            "need to pick up the latest prompt without invalidating the "
            "entire cache namespace."
        ),
    )
    parser.add_argument(
        "--department-in",
        nargs="+",
        type=str,
        default=None,
        dest="department_in",
        help=(
            "Only re-ingest documents that have at least one ruling whose "
            "department matches one of these values. Values must match "
            "rulings.department exactly (case-sensitive). Useful for targeting "
            "specific courtrooms without processing the entire county, e.g. "
            "--department-in C11 C20 C32. Applies an EXISTS subquery against "
            "the rulings table."
        ),
    )
    parser.add_argument(
        "--force-retranscribe",
        action="store_true",
        help=(
            "Skip stored_ruling_text preservation for ALL document formats. "
            "Normally, stored ruling_text from prior LLM transcriptions is "
            "preserved for PDF documents to avoid overwriting scoped text "
            "with the full multi-ruling PDF text. This flag forces "
            "re-transcription from raw content for every document, "
            "regardless of format. Useful when the stored ruling_text "
            "itself is the problem (e.g. stale LLM output). "
            "Note: HTML documents always use freshly-parsed text even "
            "without this flag — it only affects PDF documents."
        ),
    )
    args = parser.parse_args()

    if args.resume and not args.checkpoint_file:
        parser.error("--resume requires --checkpoint-file to be specified.")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    # Prefix mode: scan S3 directly for documents not in the DB.
    if args.prefix:
        stats = run_reingest_from_prefix(
            dsn,
            prefix=args.prefix,
            concurrency=args.concurrency,
            limit=args.limit,
            dry_run=args.dry_run,
            bust_llm_cache=args.bust_llm_cache,
        )
        logger.info(
            "Prefix reingest complete",
            total_keys=stats["total_keys"],
            processed=stats["processed"],
            errors=stats["errors"],
            skipped=stats["skipped"],
        )
        return

    # Standard mode: query the database for existing document records.
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
        full_reparse=args.full_reparse,
        case_title_regex=args.case_title_regex,
        null_motion_type=args.null_motion_type,
        orphaned_only=args.orphaned_only,
        report_metrics=args.report_metrics,
        multimodal=args.multimodal,
        checkpoint_file=args.checkpoint_file,
        resume=args.resume,
        case_number_like=args.case_number_like,
        force_retranscribe=args.force_retranscribe,
        filter_null_outcome=args.filter_null_outcome,
        bust_llm_cache=args.bust_llm_cache,
        department_in=args.department_in,
    )

    logger.info(
        "Reingest complete",
        total_processed=stats["total_processed"],
        total_updated=stats["total_updated"],
        total_llm_skipped=stats["total_llm_skipped"],
        input_tokens=stats.get("input_tokens", 0),
        output_tokens=stats.get("output_tokens", 0),
        llm_api_calls=stats.get("llm_api_calls", 0),
        estimated_cost_usd=stats.get("estimated_cost_usd", 0),
    )


if __name__ == "__main__":
    main()
