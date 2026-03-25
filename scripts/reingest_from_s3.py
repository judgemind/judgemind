#!/usr/bin/env python3
# venv: scraper-framework
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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
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

from framework.llm_extractor import LlmExtractor  # noqa: E402
from framework.llm_schema import ExtractedRuling  # noqa: E402
from framework.logging import configure_structlog  # noqa: E402
from framework.models import CapturedDocument, ContentFormat, ScraperConfig  # noqa: E402
from ingestion.db import (  # noqa: E402
    batch_upsert_parties,
    insert_document_and_ruling,
    resolve_judge,
    upsert_case,
    upsert_case_judge,
)
from ingestion.extract import (  # noqa: E402
    extract_case_number,
    extract_case_title,
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_hearing_date,
    extract_judge_name,
    extract_motion_type,
    extract_outcome,
    extract_parties_from_caption,
    normalize_motion_type,
    normalize_outcome,
)
from ingestion.llm_extract import (  # noqa: E402
    LLMExtractionResult,
    LLMRulingResult,
    TokenTracker,
    extract_fields_llm,
    extract_text_from_pdf,
)
from ingestion.llm_providers import create_client as create_llm_client  # noqa: E402
from ingestion.split_ids import is_split_child_id, make_split_document_id  # noqa: E402

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

            # Register split function if the module exports one.
            # Convention: a module-level ``_split_rulings`` callable
            # indicates that the scraper produces multi-ruling documents
            # that need splitting during full reparse.
            split_fn = getattr(mod, "_split_rulings", None)
            if split_fn is not None and callable(split_fn):
                _SPLIT_REGISTRY[config.scraper_id] = split_fn
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
        c.case_number, c.case_title,
        (SELECT r.hearing_date FROM rulings r
         WHERE r.document_id = d.id LIMIT 1) AS ruling_hearing_date,
        (SELECT r.ruling_text FROM rulings r
         WHERE r.document_id = d.id LIMIT 1) AS stored_ruling_text
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
    if orphaned_only:
        clauses.append(
            "AND NOT EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = d.id)"
        )
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


def _apply_regex_fallbacks(extracted: dict, text: str, scraper_id: str = "") -> None:
    """Apply the regex fallback chain to fill any fields still missing.

    Mutates *extracted* in place.  Expects ``extracted["extraction_methods"]``
    to already exist as a ``dict``.

    The fallback order mirrors ``worker.py`` to ensure reingest produces the
    same field completeness as live ingestion:

      1. judge_name, outcome, motion_type, case_number, case_title, hearing_date
         — each extracted from *text* via the corresponding ``extract_*`` helper.
      2. parties from case_title caption (``extract_parties_from_caption``).
      3. case_type from case-number prefix (``extract_case_type_from_number``).
      4. case_type from scraper_id suffix (``extract_case_type_from_scraper_id``).
      5. case_type from motion_type (``extract_case_type_from_motion_type``).

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
    if not extracted["outcome"]:
        val = extract_outcome(text)
        if val:
            extracted["outcome"] = val
            methods.setdefault("outcome", "regex")
    if not extracted["motion_type"]:
        val = extract_motion_type(text)
        if val:
            extracted["motion_type"] = val
            methods.setdefault("motion_type", "regex")
    if not extracted["case_number"]:
        val = extract_case_number(text)
        if val:
            extracted["case_number"] = val
            methods.setdefault("case_number", "regex")
    if not extracted["case_title"]:
        val = extract_case_title(text)
        if val:
            extracted["case_title"] = val
            methods.setdefault("case_title", "regex")
    if not extracted["hearing_date"]:
        val = extract_hearing_date(text)
        if val:
            extracted["hearing_date"] = val
            methods.setdefault("hearing_date", "regex")

    # Fallback parties from case_title caption (#1836).
    # When no parties were provided by the scraper or LLM, try to extract
    # plaintiff/defendant from a "X v. Y" style case title.
    if not extracted.get("parties") and extracted.get("case_title"):
        parties = extract_parties_from_caption(extracted["case_title"])
        if parties:
            extracted["parties"] = parties
            methods.setdefault("parties", "regex")

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
            # Normalize scraper-provided motion_type to snake_case (#1849).
            # Mirrors the normalization in worker.py so reingest produces
            # the same canonical values as live ingestion.
            extracted["motion_type"] = (
                normalize_motion_type(parsed.motion_type)
                if parsed.motion_type
                else None
            )
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
            t0 = time.monotonic()
            llm_result = extract_fields_llm(
                document_text=text,
                content_format=doc_format,
                metadata=None,
                client=llm_client,
                provider=llm_provider,
                model=llm_model,
                timeout=llm_timeout,
                token_tracker=token_tracker,
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
                        extracted["outcome"] = ruling.outcome
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
    stored = doc_meta.get("stored_ruling_text")
    if stored:
        extracted["ruling_text"] = stored.replace("\x00", "") if stored else stored
        regex_text = extracted["ruling_text"]
    else:
        regex_text = text

    # ------------------------------------------------------------------
    # Regex fallback — fill any fields still missing after scraper + LLM
    # ------------------------------------------------------------------
    extracted["extraction_methods"] = extraction_methods
    _apply_regex_fallbacks(extracted, regex_text, scraper_id=scraper_id)

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

    split_fn = _SPLIT_REGISTRY.get(scraper_id)
    if split_fn is None:
        # No splitting logic — fall back to single-document reparse
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
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        return [result]

    # Extract text from the raw content (PDF or HTML)
    doc_format = doc_meta.get("format", "html")
    text = _extract_text_from_content(
        raw_content,
        doc_format,
        pdf_timeout=pdf_timeout,
    ).replace("\x00", "")

    # Call the scraper-specific splitting function
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
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        return [result]

    # Multiple rulings — split into individual records
    content_hash = doc_meta.get("content_hash", "")
    if not content_hash:
        content_hash = hashlib.sha256(raw_content).hexdigest()

    # Extract hearing date and judge name from the full PDF text
    # (these are document-level, shared across all rulings).
    scraper_cls = _SCRAPER_REGISTRY.get(scraper_id)
    doc_judge_name: str | None = None
    doc_hearing_date: Any = doc_meta.get("hearing_date")

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
                content_format=ContentFormat(doc_meta["format"]),
                raw_content=raw_content,
                content_hash=content_hash,
            )
            parsed = scraper.parse_document(cap_doc)
            doc_judge_name = parsed.judge_name
            if parsed.hearing_date:
                doc_hearing_date = (
                    parsed.hearing_date.date()
                    if isinstance(parsed.hearing_date, datetime)
                    else parsed.hearing_date
                )
            # Also try department from parsed doc
            doc_department = parsed.department
        except Exception:
            logger.warning(
                "Scraper parse_document failed during full-reparse",
                document_id=doc_meta["document_id"],
                exc_info=True,
            )
            doc_department = None
    else:
        doc_department = None

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

        extracted: dict = {
            "ruling_text": ruling.ruling_text.replace("\x00", "")
            if ruling.ruling_text
            else "",
            "case_number": ruling.case_number or doc_meta.get("case_number"),
            "case_title": ruling.case_title or doc_meta.get("case_title"),
            "case_type": doc_meta.get("case_type"),
            "judge_name": doc_judge_name,
            "outcome": normalize_outcome(ruling.outcome),
            # Normalize split-provided motion_type to snake_case (#1849).
            "motion_type": (
                normalize_motion_type(ruling.motion_type)
                if ruling.motion_type
                else None
            ),
            "department": doc_department,
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
        if doc_department:
            extracted["extraction_methods"]["department"] = "scraper"

        # ------------------------------------------------------------------
        # Regex fallback — fill any fields still missing after split (#1749)
        # Uses the shared helper to stay in sync with _reparse_document().
        # ------------------------------------------------------------------
        _apply_regex_fallbacks(
            extracted, extracted["ruling_text"], scraper_id=scraper_id
        )

        results.append(extracted)

    return results


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
) -> list[dict]:
    """Re-parse a PDF document using multimodal per-page extraction.

    Uses ``LlmExtractor.extract_from_pdf()`` to send page images directly
    to a multimodal LLM, bypassing pdfplumber text extraction entirely.
    This produces more accurate results for image-based PDFs (e.g., OC
    tentative rulings) where pdfplumber frequently garbles text.

    Falls back to ``_reparse_document()`` if the document format is not
    PDF or if multimodal extraction returns no results.

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
        LLM client for text-based fallback extraction.
    llm_provider : str | None
        LLM provider name for text-based fallback.
    llm_model : str | None
        LLM model name for text-based fallback.
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
            raw_content, metadata=metadata or None
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
        )
        result["ruling_index"] = 0
        result["split_document_id"] = doc_meta["document_id"]
        result["is_split"] = False
        result["llm_outcome"] = "multimodal_fallback"
        return [result]

    # Convert ExtractedRuling objects to the dict format used by reingest.
    content_hash = doc_meta.get("content_hash", "")
    if not content_hash:
        content_hash = hashlib.sha256(raw_content).hexdigest()

    is_multi = len(extracted_rulings) > 1
    results: list[dict] = []

    for idx, ruling in enumerate(extracted_rulings):
        # Map outcome enum to string value.
        outcome_str: str | None = None
        if ruling.outcome is not None:
            outcome_str = ruling.outcome.value

        # Build parties list.
        parties_data: list[dict[str, str]] = []
        for party in ruling.extracted_parties:
            parties_data.append({"name": party.name, "role": party.role})

        # Parse hearing_date from string to date if present.
        hearing_date_val = doc_meta.get("hearing_date")
        if ruling.hearing_date:
            try:
                hearing_date_val = date.fromisoformat(ruling.hearing_date)
            except (ValueError, TypeError):
                pass

        # Determine document ID for this ruling.
        if is_multi:
            split_doc_id = make_split_document_id(doc_meta["document_id"], idx)
        else:
            split_doc_id = doc_meta["document_id"]

        extracted: dict = {
            "ruling_text": ruling.ruling_text or "",
            "case_number": (
                ruling.extracted_case_number
                or doc_meta.get("case_number")
                or f"UNKNOWN-{split_doc_id}"
            ),
            "case_title": ruling.extracted_case_title or doc_meta.get("case_title"),
            "case_type": ruling.case_type.value if ruling.case_type else None,
            "judge_name": ruling.extracted_judge_name,
            "outcome": outcome_str,
            # Normalize multimodal-provided motion_type to snake_case (#1849).
            "motion_type": (
                normalize_motion_type(ruling.motion_type)
                if ruling.motion_type
                else None
            ),
            "department": ruling.department,
            "parties": parties_data,
            "hearing_date": hearing_date_val,
            "extraction_methods": {"_all": "multimodal"},
            "llm_skipped": False,
            "llm_outcome": "multimodal_success",
            "ruling_index": idx,
            "split_document_id": split_doc_id,
            "is_split": is_multi,
        }

        # Apply regex fallbacks for any fields still missing.
        # The multimodal pipeline extracts ruling_text, case_number,
        # and case_title from page images.  Other fields (judge_name,
        # outcome, motion_type) may still be missing and can be filled
        # by regex extraction from the ruling text.
        if extracted["ruling_text"]:
            _apply_regex_fallbacks(
                extracted, extracted["ruling_text"], scraper_id=scraper_id
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
) -> None:
    """Mark a document as superseded (replaced by split children).

    Sets ``documents.status = 'superseded'`` so the original unsplit
    document is excluded from future queries and reingest runs.
    The row is preserved for audit trail purposes.

    Also deletes any ruling rows that reference the original document,
    since the split children create their own ruling rows with correct
    per-ruling text.  Without this, the old single-ruling row (with
    corrupted or merged text) would persist alongside the new split rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM rulings WHERE document_id = %s::uuid",
            (document_id,),
        )
        deleted = cur.rowcount
        cur.execute(
            "UPDATE documents SET status = 'superseded' WHERE id = %s::uuid",
            (document_id,),
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
    running_processed: int = 0,
    running_updated: int = 0,
    batch_number: int = 0,
    token_tracker: TokenTracker | None = None,
    multimodal_extractor: LlmExtractor | None = None,
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
            ruling_hearing_date,
            stored_ruling_text,
        ) = row
        processed += 1
        doc_id_str = str(doc_id)
        next_cursor = (captured_at, doc_id_str)

        # Guard: in full-reparse mode, skip documents that are already split
        # children.  Split children have UUID v5 IDs (from
        # make_split_document_id) and share their parent's S3 object.
        # Re-processing them would re-split the parent PDF, creating N new
        # children per child — an exponential/infinite loop.  See #1919.
        if full_reparse and is_split_child_id(doc_id_str):
            logger.info(
                "Skipping split-child document in full-reparse mode",
                document_id=doc_id_str,
            )
            skipped += 1
            continue

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

        try:
            with conn.transaction():
                # Check if this document was split into multiple rulings
                any_split = any(e.get("is_split") for e in extracted_list)

                for extracted in extracted_list:
                    # For rulings, use split_document_id if available;
                    # for documents, always use original document_id
                    # (matching ingestion worker behavior — one PDF = one
                    # document row).
                    effective_doc_id = extracted.get("split_document_id", doc_id_str)
                    effective_case_number = (
                        extracted["case_number"]
                        or doc_meta["case_number"]
                        or f"UNKNOWN-{effective_doc_id}"
                    )
                    new_case_id = upsert_case(
                        conn,
                        effective_case_number,
                        court_id_str,
                        case_title=extracted["case_title"],
                        case_type=extracted.get("case_type"),
                    )

                    effective_hearing = (
                        extracted["hearing_date"] or doc_meta["hearing_date"]
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

                    # Resolve judge
                    judge_id = None
                    if extracted["judge_name"]:
                        judge_id = resolve_judge(
                            conn, extracted["judge_name"], court_id_str
                        )

                    # Truncate excessively long ruling text
                    ruling_text = extracted["ruling_text"]
                    if ruling_text and len(ruling_text) > 50000:
                        ruling_text = ruling_text[:50000]

                    # Insert document + ruling via shared helper (#1790).
                    # The helper guarantees the same document_id is passed
                    # to both insert_document and insert_ruling, preventing
                    # FK divergence (#1775).
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
                        outcome=extracted["outcome"],
                        motion_type=extracted["motion_type"],
                    )

                    if judge_id:
                        upsert_case_judge(
                            conn, new_case_id, judge_id, effective_hearing
                        )

                    # Parties
                    batch_upsert_parties(
                        conn, new_case_id, extracted.get("parties", [])
                    )

                # If the document was split, supersede the original
                if any_split:
                    _supersede_document(conn, doc_id_str)

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
    """
    filters, filter_params = _build_filters(
        county,
        date_from,
        date_to,
        case_title_regex=case_title_regex,
        null_motion_type=null_motion_type,
        orphaned_only=orphaned_only,
        case_number_like=case_number_like,
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
    multimodal_extractor: LlmExtractor | None = None
    if multimodal:
        try:
            multimodal_extractor = LlmExtractor(provider="google")
            logger.info(
                "Multimodal extraction enabled (Google Flash Lite)",
                model=multimodal_extractor._model,
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
                running_processed=total_processed,
                running_updated=total_updated,
                batch_number=total_batches,
                token_tracker=tracker,
                multimodal_extractor=multimodal_extractor,
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
    args = parser.parse_args()

    if args.resume and not args.checkpoint_file:
        parser.error("--resume requires --checkpoint-file to be specified.")

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
        full_reparse=args.full_reparse,
        case_title_regex=args.case_title_regex,
        null_motion_type=args.null_motion_type,
        orphaned_only=args.orphaned_only,
        report_metrics=args.report_metrics,
        multimodal=args.multimodal,
        checkpoint_file=args.checkpoint_file,
        resume=args.resume,
        case_number_like=args.case_number_like,
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
