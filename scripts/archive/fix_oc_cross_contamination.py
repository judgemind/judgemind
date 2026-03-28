#!/usr/bin/env python3
# venv: scraper-framework
"""Fix cross-contaminated OC ruling text by re-extracting from source PDFs.

Identifies OC rulings where the same ruling_text was incorrectly assigned to
multiple unrelated cases (cross-contamination from the worker.py fallback bug
fixed in #2057).  For each contaminated ruling, fetches the source PDF from S3,
runs multimodal extraction (Google Flash Lite) to get correct per-case text,
and updates the ruling_text in the database.

This is a targeted fix that updates only the ruling_text and ruling_text_hash
fields of contaminated rulings, without reorganising the document hierarchy.

Usage:
    scripts/ecs-run-task.sh scripts/fix_oc_cross_contamination.py -- --dry-run
    scripts/ecs-run-task.sh scripts/fix_oc_cross_contamination.py

Options:
    --dry-run       Show what would be updated without writing to DB.
    --limit N       Process at most N unique PDFs (default: all).
    --verbose       Log detailed extraction results.

Closes #2065.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import defaultdict


import boto3
import psycopg

# ---------------------------------------------------------------------------
# Lazy import of framework modules — these are installed in the
# scraper-framework venv.
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    """Read DATABASE_URL from environment."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)
    return url


# ---------------------------------------------------------------------------
# Step 1: Find contaminated rulings
# ---------------------------------------------------------------------------

CONTAMINATED_RULINGS_QUERY = """
SELECT
    r.id AS ruling_id,
    r.document_id,
    r.case_id,
    r.ruling_text,
    r.ruling_text_hash,
    d.s3_key,
    d.s3_bucket,
    d.hearing_date AS doc_hearing_date,
    c.case_number,
    c.case_title,
    ct.county,
    j.canonical_name AS judge_name
FROM rulings r
JOIN courts ct ON r.court_id = ct.id
JOIN documents d ON r.document_id = d.id
LEFT JOIN cases c ON r.case_id = c.id
LEFT JOIN case_judges cj ON cj.case_id = c.id
LEFT JOIN judges j ON j.id = cj.judge_id
WHERE ct.county = 'Orange'
  AND r.ruling_text_hash IN (
      SELECT r2.ruling_text_hash
      FROM rulings r2
      JOIN courts ct2 ON r2.court_id = ct2.id
      WHERE ct2.county = 'Orange'
        AND r2.ruling_text_hash IS NOT NULL
      GROUP BY r2.ruling_text_hash
      HAVING COUNT(DISTINCT r2.case_id) > 1
  )
  AND LENGTH(r.ruling_text) > 500
ORDER BY d.s3_key, c.case_number
"""

# Also fix rulings with NULL/empty ruling_text in OC
EMPTY_RULINGS_QUERY = """
SELECT
    r.id AS ruling_id,
    r.document_id,
    r.case_id,
    r.ruling_text,
    r.ruling_text_hash,
    d.s3_key,
    d.s3_bucket,
    d.hearing_date AS doc_hearing_date,
    c.case_number,
    c.case_title,
    ct.county,
    j.canonical_name AS judge_name
FROM rulings r
JOIN courts ct ON r.court_id = ct.id
JOIN documents d ON r.document_id = d.id
LEFT JOIN cases c ON r.case_id = c.id
LEFT JOIN case_judges cj ON cj.case_id = c.id
LEFT JOIN judges j ON j.id = cj.judge_id
WHERE ct.county = 'Orange'
  AND (r.ruling_text IS NULL OR LENGTH(r.ruling_text) = 0)
ORDER BY d.s3_key, c.case_number
"""


def _fetch_contaminated_rulings(
    conn: psycopg.Connection,
) -> list[dict]:
    """Fetch all cross-contaminated OC rulings."""
    with conn.cursor() as cur:
        cur.execute(CONTAMINATED_RULINGS_QUERY)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _fetch_empty_rulings(
    conn: psycopg.Connection,
) -> list[dict]:
    """Fetch all OC rulings with NULL/empty ruling_text."""
    with conn.cursor() as cur:
        cur.execute(EMPTY_RULINGS_QUERY)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


# ---------------------------------------------------------------------------
# Step 2: Group by S3 key and fetch PDFs
# ---------------------------------------------------------------------------


def _group_by_s3_key(
    rulings: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    """Group rulings by (s3_bucket, s3_key)."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rulings:
        if r["s3_key"] and r["s3_bucket"]:
            groups[(r["s3_bucket"], r["s3_key"])].append(r)
    return groups


def _fetch_pdf(s3_client: object, bucket: str, key: str) -> bytes:
    """Fetch PDF content from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
    return response["Body"].read()  # type: ignore[index]


# ---------------------------------------------------------------------------
# Step 3: Run multimodal extraction
# ---------------------------------------------------------------------------


def _extract_rulings_from_pdf(
    pdf_bytes: bytes,
    extractor: object,
    metadata: dict[str, str] | None = None,
) -> list[object]:
    """Run multimodal extraction on a PDF and return ExtractedRuling objects."""
    return extractor.extract_from_pdf(pdf_bytes, metadata=metadata)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Step 4: Match extracted rulings to DB rulings and update
# ---------------------------------------------------------------------------


def _normalize_case_number(cn: str | None) -> str:
    """Normalize case number for matching (strip spaces, uppercase)."""
    if not cn:
        return ""
    return cn.strip().upper().replace(" ", "")


def _match_and_update(
    conn: psycopg.Connection,
    db_rulings: list[dict],
    extracted_rulings: list[object],
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Match extracted rulings to DB rulings by case_number and update text.

    Returns stats dict with keys: matched, updated, unmatched_db, unmatched_extracted.
    """
    stats = {
        "matched": 0,
        "updated": 0,
        "unmatched_db": 0,
        "unmatched_extracted": 0,
        "skipped_empty": 0,
    }

    # Build lookup from normalized case_number -> extracted ruling
    extracted_by_cn: dict[str, list[object]] = defaultdict(list)
    for er in extracted_rulings:
        cn = _normalize_case_number(er.extracted_case_number)  # type: ignore[union-attr]
        if cn:
            extracted_by_cn[cn].append(er)

    # Track which extracted rulings were matched
    matched_extracted: set[int] = set()

    for db_ruling in db_rulings:
        cn = _normalize_case_number(db_ruling["case_number"])
        candidates = extracted_by_cn.get(cn, [])

        matched = False
        for i, er in enumerate(candidates):
            er_id = id(er)
            if er_id in matched_extracted:
                continue

            new_text = er.ruling_text  # type: ignore[union-attr]
            if not new_text or len(new_text.strip()) == 0:
                # Extracted ruling has empty text -- skip
                stats["skipped_empty"] += 1
                if verbose:
                    logger.info(
                        "Skipping empty extracted text for %s",
                        db_ruling["case_number"],
                    )
                continue

            # Clean the text
            new_text = new_text.replace("\x00", "")
            new_hash = hashlib.sha256(new_text.encode("utf-8")).hexdigest()

            old_hash = db_ruling["ruling_text_hash"]
            if new_hash == old_hash:
                # Text is the same -- no update needed
                matched_extracted.add(er_id)
                stats["matched"] += 1
                matched = True
                break

            if verbose:
                old_len = len(db_ruling["ruling_text"] or "")
                new_len = len(new_text)
                logger.info(
                    "Match: ruling=%s case=%s old_len=%d new_len=%d",
                    db_ruling["ruling_id"],
                    db_ruling["case_number"],
                    old_len,
                    new_len,
                )

            if not dry_run:
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE rulings
                                SET ruling_text = %s,
                                    ruling_text_hash = %s,
                                    updated_at = NOW()
                                WHERE id = %s
                                """,
                                (new_text, new_hash, str(db_ruling["ruling_id"])),
                            )
                    conn.commit()
                except psycopg.errors.UniqueViolation:
                    # Another ruling for the same case already has
                    # this text hash — skip this update.
                    conn.rollback()
                    if verbose:
                        logger.warning(
                            "Skipping duplicate hash: ruling=%s case=%s",
                            db_ruling["ruling_id"],
                            db_ruling["case_number"],
                        )
                    stats["skipped_duplicate"] = stats.get("skipped_duplicate", 0) + 1
                    matched_extracted.add(er_id)
                    stats["matched"] += 1
                    matched = True
                    break

            matched_extracted.add(er_id)
            stats["matched"] += 1
            stats["updated"] += 1
            matched = True
            break

        if not matched:
            stats["unmatched_db"] += 1
            if verbose:
                logger.warning(
                    "No match for DB ruling: ruling_id=%s case=%s",
                    db_ruling["ruling_id"],
                    db_ruling["case_number"],
                )

    # Count unmatched extracted rulings
    for er in extracted_rulings:
        if id(er) not in matched_extracted:
            stats["unmatched_extracted"] += 1

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix cross-contaminated OC ruling text.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing to DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N unique PDFs.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log detailed extraction results.",
    )
    args = parser.parse_args()

    db_url = _get_db_url()
    start_time = time.time()

    # Import the multimodal extractor
    try:
        from framework.llm_extractor import LlmExtractor
    except ImportError:
        logger.error(
            "Cannot import LlmExtractor. Ensure scraper-framework is installed."
        )
        sys.exit(1)

    # Verify GOOGLE_API_KEY is set
    if not os.environ.get("GOOGLE_API_KEY"):
        logger.error(
            "GOOGLE_API_KEY environment variable is required for multimodal extraction"
        )
        sys.exit(1)

    extractor = LlmExtractor(provider="google")
    logger.info("Multimodal extractor initialized (Google Flash Lite)")

    s3_client = boto3.client("s3")

    with psycopg.connect(db_url) as conn:
        # Step 1: Fetch contaminated and empty rulings
        logger.info("Fetching cross-contaminated rulings...")
        contaminated = _fetch_contaminated_rulings(conn)
        logger.info("Found %d cross-contaminated rulings", len(contaminated))

        logger.info("Fetching empty-text rulings...")
        empty = _fetch_empty_rulings(conn)
        logger.info("Found %d empty-text rulings", len(empty))

        # Combine, deduplicating by ruling_id
        seen_ids: set[str] = set()
        all_rulings: list[dict] = []
        for r in contaminated + empty:
            rid = str(r["ruling_id"])
            if rid not in seen_ids:
                seen_ids.add(rid)
                all_rulings.append(r)

        logger.info("Total unique rulings to fix: %d", len(all_rulings))

        # Step 2: Group by S3 key
        groups = _group_by_s3_key(all_rulings)
        logger.info("Grouped into %d unique PDFs", len(groups))

        if args.limit:
            # Take only the first N groups
            limited_keys = list(groups.keys())[: args.limit]
            groups = {k: groups[k] for k in limited_keys}
            logger.info("Limited to %d PDFs", len(groups))

        # Step 3-4: Process each PDF
        total_stats: dict[str, int] = {
            "pdfs_processed": 0,
            "pdfs_failed": 0,
            "matched": 0,
            "updated": 0,
            "unmatched_db": 0,
            "unmatched_extracted": 0,
            "skipped_empty": 0,
            "unknown_nulled": 0,
        }

        for (bucket, key), group_rulings in groups.items():
            logger.info(
                "Processing PDF: s3://%s/%s (%d rulings)",
                bucket,
                key,
                len(group_rulings),
            )

            # Fetch PDF from S3
            try:
                pdf_bytes = _fetch_pdf(s3_client, bucket, key)
            except Exception:
                logger.error(
                    "Failed to fetch PDF: s3://%s/%s", bucket, key, exc_info=True
                )
                total_stats["pdfs_failed"] += 1
                continue

            # Build metadata for extraction
            metadata: dict[str, str] = {}
            sample = group_rulings[0]
            if sample.get("judge_name"):
                metadata["judge_name"] = str(sample["judge_name"])
            if sample.get("doc_hearing_date"):
                metadata["hearing_date"] = str(sample["doc_hearing_date"])

            # Run multimodal extraction
            try:
                extracted = _extract_rulings_from_pdf(
                    pdf_bytes, extractor, metadata=metadata or None
                )
            except Exception:
                logger.error(
                    "Multimodal extraction failed for s3://%s/%s",
                    bucket,
                    key,
                    exc_info=True,
                )
                total_stats["pdfs_failed"] += 1
                continue

            if not extracted:
                logger.warning("No rulings extracted from s3://%s/%s", bucket, key)
                total_stats["pdfs_failed"] += 1
                continue

            logger.info(
                "Extracted %d rulings from PDF (%d DB rulings to match)",
                len(extracted),
                len(group_rulings),
            )

            # Match and update
            stats = _match_and_update(
                conn,
                group_rulings,
                extracted,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )

            total_stats["pdfs_processed"] += 1
            for k in [
                "matched",
                "updated",
                "unmatched_db",
                "unmatched_extracted",
                "skipped_empty",
            ]:
                total_stats[k] += stats[k]

            logger.info(
                "PDF result: matched=%d updated=%d unmatched_db=%d unmatched_extracted=%d",
                stats["matched"],
                stats["updated"],
                stats["unmatched_db"],
                stats["unmatched_extracted"],
            )

        # Step 5: NULL out ruling_text for contaminated UNKNOWN-* rulings
        # These have placeholder case numbers AND contaminated text.
        # They can't be matched to correct extracted text because their
        # case_numbers are synthetic.  NULLing the text prevents showing
        # cross-contaminated calendar dumps to users.
        unknown_nulled = 0
        for r in contaminated:
            cn = r.get("case_number") or ""
            if not cn.startswith("UNKNOWN-"):
                continue
            # Only NULL if the text is still contaminated (> 500 chars)
            if not r.get("ruling_text") or len(r["ruling_text"]) <= 500:
                continue
            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE rulings
                        SET ruling_text = NULL,
                            ruling_text_hash = NULL,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (str(r["ruling_id"]),),
                    )
                conn.commit()
            unknown_nulled += 1

        logger.info(
            "UNKNOWN-* rulings with text NULLed: %d",
            unknown_nulled,
        )
        total_stats["unknown_nulled"] = unknown_nulled

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("COMPLETE in %.1f seconds", elapsed)
    logger.info("PDFs processed: %d", total_stats["pdfs_processed"])
    logger.info("PDFs failed: %d", total_stats["pdfs_failed"])
    logger.info("Rulings matched: %d", total_stats["matched"])
    logger.info("Rulings updated: %d", total_stats["updated"])
    logger.info("Unmatched DB rulings: %d", total_stats["unmatched_db"])
    logger.info("Unmatched extracted: %d", total_stats["unmatched_extracted"])
    logger.info("Skipped (empty text): %d", total_stats["skipped_empty"])
    logger.info("UNKNOWN-* NULLed: %d", total_stats.get("unknown_nulled", 0))

    if args.dry_run:
        logger.info("DRY RUN — no changes written to database")

    # Print JSON summary for easy parsing
    print(json.dumps(total_stats, indent=2))


if __name__ == "__main__":
    main()
