#!/usr/bin/env python3
"""Backfill Riverside rulings with LLM-based field extraction.

Re-processes existing Riverside tentative ruling documents through the LLM
extraction pipeline, replacing regex-extracted fields (case_number, case_title,
ruling_text, outcome, motion_type) with more accurate LLM-extracted values.

Background:
  Riverside PDFs contain multiple numbered rulings per document.  The original
  regex-based ``_split_rulings()`` has known issues with mis-assigning ruling
  text to wrong cases (#1716) and leaking motion type descriptions into case
  titles (#1401).  The LLM extractor (``LlmExtractor.extract()``) handles
  multi-case documents natively and produces cleaner splits.

Usage:
    # Dry run — show what would change without writing to DB
    scripts/ecs-run-task.sh scripts/backfill_riverside_llm.py -- --dry-run

    # Process with a limit (good for testing)
    scripts/ecs-run-task.sh scripts/backfill_riverside_llm.py -- --dry-run --limit 5

    # Full backfill (requires ANTHROPIC_API_KEY or GOOGLE_API_KEY)
    scripts/ecs-run-task.sh scripts/backfill_riverside_llm.py

    # Use Google Gemini (cheaper) instead of default Anthropic
    scripts/ecs-run-task.sh scripts/backfill_riverside_llm.py -- --provider google
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import time
from typing import Any

import boto3
import psycopg
import structlog

# Add scraper-framework source to path for imports
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

from framework.llm_extractor import LlmExtractor  # noqa: E402
from framework.llm_schema import ExtractedRuling  # noqa: E402
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRAPER_ID = "ca-riverside-tentatives-civil"

# Valid ruling_outcome PostgreSQL enum values
VALID_OUTCOMES: frozenset[str] = frozenset(
    {
        "granted",
        "denied",
        "granted_in_part",
        "denied_in_part",
        "moot",
        "continued",
        "off_calendar",
        "submitted",
        "other",
    }
)

OUTCOME_ALIAS: dict[str, str] = {
    "no tentative ruling": "other",
    "no tentative": "other",
    "withdrawn": "off_calendar",
    "vacated": "off_calendar",
    "overruled": "denied",
    "sustained": "granted",
}


def normalize_outcome(raw: str | None) -> str | None:
    """Normalize a raw outcome string to a valid ruling_outcome enum value."""
    if not raw:
        return None
    lower = raw.strip().lower()
    if lower in VALID_OUTCOMES:
        return lower
    if lower in OUTCOME_ALIAS:
        return OUTCOME_ALIAS[lower]
    for outcome in (
        "granted_in_part",
        "denied_in_part",
        "granted",
        "denied",
        "moot",
        "continued",
        "off_calendar",
        "submitted",
    ):
        if outcome.replace("_", " ") in lower:
            return outcome
    return None


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

# Fetch all Riverside documents that have associated rulings.
# Groups by document so we process each PDF once even if it produced
# multiple ruling rows.
FETCH_DOCUMENTS_QUERY = """
    SELECT DISTINCT
        d.id AS document_id,
        d.s3_key,
        d.s3_bucket,
        d.content_hash,
        d.source_url,
        d.captured_at,
        d.hearing_date AS doc_hearing_date,
        d.format,
        d.case_id AS doc_case_id,
        ct.state,
        ct.county,
        ct.court_name,
        d.court_id
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.scraper_id = %s
      AND d.status = 'active'
    ORDER BY d.captured_at DESC
"""

# Fetch all rulings for a given document
FETCH_RULINGS_FOR_DOC_QUERY = """
    SELECT
        r.id AS ruling_id,
        r.document_id,
        r.case_id,
        r.ruling_text,
        r.outcome::text,
        r.motion_type,
        r.hearing_date,
        c.case_number,
        c.case_title,
        c.case_type
    FROM rulings r
    JOIN cases c ON c.id = r.case_id
    WHERE r.document_id = %s
    ORDER BY r.id
"""

# Also fetch rulings for split document IDs (which use deterministic UUIDs)
FETCH_SPLIT_RULINGS_QUERY = """
    SELECT
        r.id AS ruling_id,
        r.document_id,
        r.case_id,
        r.ruling_text,
        r.outcome::text,
        r.motion_type,
        r.hearing_date,
        c.case_number,
        c.case_title,
        c.case_type
    FROM rulings r
    JOIN cases c ON c.id = r.case_id
    JOIN documents sd ON sd.id = r.document_id
    WHERE sd.source_url = %s
      AND sd.scraper_id = %s
    ORDER BY r.id
"""


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def extract_pdf_text(raw_content: bytes) -> str | None:
    """Extract readable text from PDF bytes using pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed")
        return None

    try:
        pages_text: list[str] = []
        with pdfplumber.open(io.BytesIO(raw_content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
        if pages_text:
            return "\n\f\n".join(pages_text)
        return None
    except Exception:
        logger.warning("PDF text extraction failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Matching logic — map LLM rulings to existing DB rulings
# ---------------------------------------------------------------------------


def match_llm_to_db_rulings(
    llm_rulings: list[ExtractedRuling],
    db_rulings: list[dict],
) -> list[tuple[dict, ExtractedRuling]]:
    """Match LLM-extracted rulings to existing DB ruling records.

    Uses case number matching as the primary key.  Falls back to positional
    matching when case numbers don't align (the LLM may normalize them
    differently).

    Returns a list of (db_ruling, llm_ruling) pairs.
    """
    matched: list[tuple[dict, ExtractedRuling]] = []
    used_llm_indices: set[int] = set()

    # Pass 1: match by case number
    for db_ruling in db_rulings:
        db_case_num = db_ruling["case_number"] or ""
        best_idx: int | None = None
        for i, llm_ruling in enumerate(llm_rulings):
            if i in used_llm_indices:
                continue
            llm_case_num = llm_ruling.extracted_case_number or ""
            # Exact match or one contains the other (handle prefix differences)
            if llm_case_num and db_case_num:
                if (
                    llm_case_num == db_case_num
                    or llm_case_num in db_case_num
                    or db_case_num in llm_case_num
                ):
                    best_idx = i
                    break
        if best_idx is not None:
            matched.append((db_ruling, llm_rulings[best_idx]))
            used_llm_indices.add(best_idx)

    # Pass 2: positional matching for unmatched DB rulings
    unmatched_db = [r for r in db_rulings if not any(m[0] is r for m in matched)]
    unmatched_llm_indices = [
        i for i in range(len(llm_rulings)) if i not in used_llm_indices
    ]

    for db_ruling, llm_idx in zip(unmatched_db, unmatched_llm_indices):
        matched.append((db_ruling, llm_rulings[llm_idx]))

    return matched


# ---------------------------------------------------------------------------
# S3 fetch
# ---------------------------------------------------------------------------


def fetch_s3_content(s3_client: Any, bucket: str, key: str) -> bytes:
    """Fetch raw content from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------


def compute_ruling_text_hash(ruling_text: str) -> str:
    """Compute SHA-256 hash of normalized ruling text for dedup."""
    normalized = re.sub(r"\s+", " ", ruling_text.lower().strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def run_backfill(
    dsn: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    provider: str = "anthropic",
    model: str | None = None,
) -> dict[str, int]:
    """Run the Riverside LLM backfill.

    For each Riverside document:
      1. Fetch the raw PDF from S3
      2. Extract text via pdfplumber
      3. Run LlmExtractor.extract() to get per-case extractions
      4. Match LLM results to existing DB ruling records
      5. Update ruling and case fields with LLM-extracted values

    Returns summary stats.
    """
    stats = {
        "documents_processed": 0,
        "documents_skipped": 0,
        "rulings_updated": 0,
        "rulings_skipped": 0,
        "cases_updated": 0,
        "llm_calls": 0,
        "llm_errors": 0,
        "match_failures": 0,
    }

    extractor = LlmExtractor(provider=provider, model=model)
    s3_client = boto3.client("s3", region_name="us-west-2")

    with psycopg.connect(dsn) as conn:
        # Fetch all Riverside documents
        with conn.cursor() as cur:
            cur.execute(FETCH_DOCUMENTS_QUERY, (SCRAPER_ID,))
            columns = [desc.name for desc in cur.description]
            documents = [dict(zip(columns, row)) for row in cur.fetchall()]

        logger.info("Found %d Riverside documents to process", len(documents))

        if limit is not None:
            documents = documents[:limit]
            logger.info("Processing limited to %d documents", limit)

        for doc_idx, doc in enumerate(documents):
            doc_id = str(doc["document_id"])

            # Fetch existing rulings for this document
            with conn.cursor() as cur:
                # Try direct document_id match first
                cur.execute(FETCH_RULINGS_FOR_DOC_QUERY, (doc_id,))
                columns = [desc.name for desc in cur.description]
                db_rulings = [dict(zip(columns, row)) for row in cur.fetchall()]

                # Also try split documents (matching by source_url)
                if not db_rulings:
                    cur.execute(
                        FETCH_SPLIT_RULINGS_QUERY,
                        (doc["source_url"], SCRAPER_ID),
                    )
                    columns = [desc.name for desc in cur.description]
                    db_rulings = [dict(zip(columns, row)) for row in cur.fetchall()]

            if not db_rulings:
                logger.debug(
                    "No rulings found for document, skipping",
                    document_id=doc_id,
                )
                stats["documents_skipped"] += 1
                continue

            # Fetch raw PDF from S3
            try:
                raw_content = fetch_s3_content(
                    s3_client, doc["s3_bucket"], doc["s3_key"]
                )
            except Exception:
                logger.warning(
                    "S3 fetch failed, skipping",
                    document_id=doc_id,
                    s3_key=doc["s3_key"],
                    exc_info=True,
                )
                stats["documents_skipped"] += 1
                continue

            # Extract text from PDF
            text = extract_pdf_text(raw_content)
            if not text or not text.strip():
                # Fallback to UTF-8 decode
                text = raw_content.decode("utf-8", errors="replace")
            text = text.replace("\x00", "")

            if not text.strip():
                logger.warning(
                    "No text extracted from document, skipping",
                    document_id=doc_id,
                )
                stats["documents_skipped"] += 1
                continue

            # Run LLM extraction
            stats["llm_calls"] += 1
            try:
                llm_rulings = extractor.extract(text)
            except Exception:
                logger.warning(
                    "LLM extraction failed, skipping",
                    document_id=doc_id,
                    exc_info=True,
                )
                stats["llm_errors"] += 1
                stats["documents_skipped"] += 1
                continue

            if not llm_rulings:
                logger.info(
                    "LLM returned no rulings",
                    document_id=doc_id,
                    db_ruling_count=len(db_rulings),
                )
                stats["documents_skipped"] += 1
                continue

            # Match LLM results to existing DB rulings
            matches = match_llm_to_db_rulings(llm_rulings, db_rulings)

            if not matches:
                logger.warning(
                    "No matches between LLM and DB rulings",
                    document_id=doc_id,
                    llm_count=len(llm_rulings),
                    db_count=len(db_rulings),
                )
                stats["match_failures"] += 1
                stats["documents_skipped"] += 1
                continue

            stats["documents_processed"] += 1

            logger.info(
                "Processing document",
                document_index=doc_idx,
                document_id=doc_id,
                db_ruling_count=len(db_rulings),
                llm_ruling_count=len(llm_rulings),
                matched_count=len(matches),
            )

            # Apply updates for each matched pair
            for db_ruling, llm_ruling in matches:
                ruling_id = str(db_ruling["ruling_id"])
                case_id = str(db_ruling["case_id"])

                # Build update fields — only update if LLM provided a value
                ruling_updates: dict[str, Any] = {}
                case_updates: dict[str, Any] = {}

                # Ruling text
                if llm_ruling.ruling_text:
                    new_text = llm_ruling.ruling_text.strip()
                    if new_text and len(new_text) <= 50000:
                        ruling_updates["ruling_text"] = new_text
                        ruling_updates["ruling_text_hash"] = compute_ruling_text_hash(
                            new_text
                        )

                # Outcome
                if llm_ruling.outcome:
                    normalized = normalize_outcome(str(llm_ruling.outcome.value))
                    if normalized:
                        ruling_updates["outcome"] = normalized

                # Motion type
                if llm_ruling.motion_type:
                    ruling_updates["motion_type"] = llm_ruling.motion_type

                # Case number
                if llm_ruling.extracted_case_number:
                    case_updates["case_number"] = llm_ruling.extracted_case_number

                # Case title
                if llm_ruling.extracted_case_title:
                    case_updates["case_title"] = llm_ruling.extracted_case_title

                # Case type
                if llm_ruling.case_type:
                    case_updates["case_type"] = str(llm_ruling.case_type.value)

                if not ruling_updates and not case_updates:
                    stats["rulings_skipped"] += 1
                    continue

                if dry_run:
                    logger.info(
                        "DRY-RUN: would update",
                        ruling_id=ruling_id,
                        case_id=case_id,
                        case_number=db_ruling["case_number"],
                        ruling_updates=list(ruling_updates.keys()),
                        case_updates=list(case_updates.keys()),
                        llm_case_number=llm_ruling.extracted_case_number,
                        llm_case_title=llm_ruling.extracted_case_title,
                        llm_outcome=str(llm_ruling.outcome)
                        if llm_ruling.outcome
                        else None,
                        llm_motion_type=llm_ruling.motion_type,
                    )
                    stats["rulings_updated"] += 1
                    if case_updates:
                        stats["cases_updated"] += 1
                    continue

                # Apply updates within a transaction
                try:
                    with conn.transaction():
                        if ruling_updates:
                            set_clauses = []
                            params: list[Any] = []
                            for col, val in ruling_updates.items():
                                if col == "outcome":
                                    set_clauses.append(f"{col} = %s::ruling_outcome")
                                else:
                                    set_clauses.append(f"{col} = %s")
                                params.append(val)
                            params.append(ruling_id)

                            sql = (
                                f"UPDATE rulings SET {', '.join(set_clauses)} "
                                "WHERE id = %s::uuid"
                            )
                            with conn.cursor() as cur:
                                cur.execute(sql, params)

                        if case_updates:
                            set_clauses = []
                            params = []
                            for col, val in case_updates.items():
                                set_clauses.append(f"{col} = %s")
                                params.append(val)
                            params.append(case_id)

                            sql = (
                                f"UPDATE cases SET {', '.join(set_clauses)} "
                                "WHERE id = %s::uuid"
                            )
                            with conn.cursor() as cur:
                                cur.execute(sql, params)

                    stats["rulings_updated"] += 1
                    if case_updates:
                        stats["cases_updated"] += 1

                except Exception:
                    logger.warning(
                        "Update failed, skipping ruling",
                        ruling_id=ruling_id,
                        exc_info=True,
                    )
                    stats["rulings_skipped"] += 1

            # Commit after each document
            if not dry_run:
                conn.commit()

            # Progress logging
            if (doc_idx + 1) % 10 == 0:
                logger.info(
                    "Progress",
                    documents_processed=stats["documents_processed"],
                    rulings_updated=stats["rulings_updated"],
                    total_documents=len(documents),
                )

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill Riverside rulings with LLM-based field extraction. "
            "Re-processes PDF documents through LlmExtractor.extract() to "
            "replace regex-extracted fields with more accurate LLM values."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing to the database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of documents to process.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="anthropic",
        choices=["anthropic", "google"],
        help="LLM provider (default: anthropic).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="LLM model ID (default: provider default).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    start_time = time.monotonic()

    stats = run_backfill(
        dsn,
        dry_run=args.dry_run,
        limit=args.limit,
        provider=args.provider,
        model=args.model,
    )

    elapsed = time.monotonic() - start_time

    logger.info(
        "Backfill complete%s",
        " [dry-run]" if args.dry_run else "",
        documents_processed=stats["documents_processed"],
        documents_skipped=stats["documents_skipped"],
        rulings_updated=stats["rulings_updated"],
        rulings_skipped=stats["rulings_skipped"],
        cases_updated=stats["cases_updated"],
        llm_calls=stats["llm_calls"],
        llm_errors=stats["llm_errors"],
        match_failures=stats["match_failures"],
        elapsed_seconds=round(elapsed, 1),
    )


if __name__ == "__main__":
    main()
