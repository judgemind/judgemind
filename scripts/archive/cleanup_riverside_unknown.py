#!/usr/bin/env python3
# venv: scraper-framework
"""Clean up Riverside County UNKNOWN records: boilerplate PDFs and orphaned cases.

Identifies and removes three categories of invalid UNKNOWN records:

1. **Boilerplate PDFs** — Cases linked to "No Tentative Rulings" PDF documents that
   were ingested before the ``_is_boilerplate()`` filter was added. These have
   documents with real S3 keys but the content is just a placeholder page.

2. **Empty orphans** — Cases with no rulings and no documents. These are artifacts
   from reingest or duplicate processing that created case shells without content.

3. **Unsplit duplicate rulings** — Cases with ruling text but no documents or source
   URLs. The ruling_text contains the full, unsplit department page (all rulings for
   a date/department concatenated). Properly-parsed individual rulings already exist
   under real case numbers, so these are duplicates.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- python3 scripts/cleanup_riverside_unknown.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- python3 scripts/cleanup_riverside_unknown.py --dry-run

The script is idempotent — re-running it when no UNKNOWN records remain is a no-op.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

# Find the Riverside court ID.
FIND_COURT_QUERY = """
    SELECT id FROM courts WHERE county = 'Riverside' AND state = 'CA'
"""

# Fetch all Riverside UNKNOWN cases with their document and ruling info.
FETCH_UNKNOWN_CASES_QUERY = """
    SELECT
        c.id,
        c.case_number,
        c.created_at,
        d.id AS doc_id,
        d.s3_key,
        d.source_url,
        r.id AS ruling_id,
        r.ruling_text IS NOT NULL AS has_text
    FROM cases c
    LEFT JOIN documents d ON d.case_id = c.id
    LEFT JOIN rulings r ON r.case_id = c.id
    WHERE c.case_number LIKE 'UNKNOWN-%%'
      AND c.court_id = %s
    ORDER BY c.created_at
"""

# Cascade delete: rulings -> documents -> case_judges -> case
DELETE_RULINGS_QUERY = "DELETE FROM rulings WHERE case_id = %s::uuid"
DELETE_DOCUMENTS_QUERY = "DELETE FROM documents WHERE case_id = %s::uuid"
DELETE_CASE_JUDGES_QUERY = "DELETE FROM case_judges WHERE case_id = %s::uuid"
DELETE_CASE_PARTIES_QUERY = "DELETE FROM case_parties WHERE case_id = %s::uuid"
DELETE_CASE_QUERY = """
    DELETE FROM cases
    WHERE id = %s::uuid
      AND case_number LIKE 'UNKNOWN-%%'
"""


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def classify_unknown_case(
    case_id: str,
    case_number: str,
    doc_id: str | None,
    s3_key: str | None,
    source_url: str | None,
    has_text: bool,
) -> str:
    """Classify an UNKNOWN case into a cleanup category.

    Returns one of: 'boilerplate_pdf', 'empty_orphan', 'unsplit_duplicate', 'unknown'.
    """
    has_doc = doc_id is not None

    if has_doc and s3_key is not None:
        # Has a document with an S3 key — check if it is a boilerplate PDF.
        # Known boilerplate indicators in Riverside source URLs:
        #   - "NoTRs" in the filename
        #   - Known boilerplate S3 keys from the issue
        lower_url = (source_url or "").lower()
        if "notrs" in lower_url or "no-trs" in lower_url:
            return "boilerplate_pdf"
        # Also match the specific S3 keys called out in issue #806
        if s3_key and (
            "4faa18cc-c5aa-47ae-83fc-c1b67063901e" in s3_key
            or "f64ef9e2-5d4b-4acc-ac66-59ee3345bd2b" in s3_key
        ):
            return "boilerplate_pdf"
        return "unknown"

    if not has_doc and not has_text:
        # No document, no ruling text — empty shell.
        return "empty_orphan"

    if not has_doc and has_text:
        # Has ruling text but no document — unsplit full-page dump.
        return "unsplit_duplicate"

    return "unknown"


# ---------------------------------------------------------------------------
# Core cleanup logic
# ---------------------------------------------------------------------------


def delete_case_cascade(
    conn: psycopg.Connection,
    case_id: str,
    case_number: str,
    category: str,
) -> None:
    """Delete a case and all its dependent rows."""
    with conn.cursor() as cur:
        cur.execute(DELETE_RULINGS_QUERY, (case_id,))
        rulings_deleted = cur.rowcount

        cur.execute(DELETE_DOCUMENTS_QUERY, (case_id,))
        docs_deleted = cur.rowcount

        cur.execute(DELETE_CASE_JUDGES_QUERY, (case_id,))
        judges_deleted = cur.rowcount

        cur.execute(DELETE_CASE_PARTIES_QUERY, (case_id,))
        parties_deleted = cur.rowcount

        cur.execute(DELETE_CASE_QUERY, (case_id,))
        case_deleted = cur.rowcount

    logger.info(
        "  Deleted [%s] %s (%s): case=%d, rulings=%d, docs=%d, "
        "case_judges=%d, case_parties=%d",
        category,
        case_number,
        case_id,
        case_deleted,
        rulings_deleted,
        docs_deleted,
        judges_deleted,
        parties_deleted,
    )


def run_cleanup(
    dsn: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full cleanup. Returns summary stats."""
    stats: dict[str, int] = {
        "boilerplate_pdf": 0,
        "empty_orphan": 0,
        "unsplit_duplicate": 0,
        "skipped_unknown": 0,
        "total_deleted": 0,
    }

    with psycopg.connect(dsn) as conn:
        # Find the Riverside court.
        with conn.cursor() as cur:
            cur.execute(FIND_COURT_QUERY)
            row = cur.fetchone()
            if not row:
                logger.error("Riverside court not found in courts table")
                return stats
            court_id = str(row[0])

        logger.info("Riverside court_id: %s", court_id)

        # Fetch all UNKNOWN cases.
        with conn.cursor() as cur:
            cur.execute(FETCH_UNKNOWN_CASES_QUERY, (court_id,))
            rows = cur.fetchall()

        logger.info("Found %d UNKNOWN case rows for Riverside", len(rows))

        for (
            case_id,
            case_number,
            created_at,
            doc_id,
            s3_key,
            source_url,
            ruling_id,
            has_text,
        ) in rows:
            case_id_str = str(case_id)
            doc_id_str = str(doc_id) if doc_id else None

            category = classify_unknown_case(
                case_id_str,
                case_number,
                doc_id_str,
                s3_key,
                source_url,
                has_text,
            )

            if category == "unknown":
                logger.warning(
                    "  Skipping unclassified UNKNOWN case: %s (%s) "
                    "s3_key=%s source_url=%s has_text=%s",
                    case_number,
                    case_id_str,
                    s3_key,
                    source_url,
                    has_text,
                )
                stats["skipped_unknown"] += 1
                continue

            logger.info(
                "  Classifying %s (%s) as: %s",
                case_number,
                case_id_str,
                category,
            )

            delete_case_cascade(conn, case_id_str, case_number, category)
            stats[category] += 1
            stats["total_deleted"] += 1

        if dry_run:
            conn.rollback()
            logger.info("Dry run — all changes rolled back")
        else:
            conn.commit()
            logger.info("All changes committed")

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Clean up Riverside County UNKNOWN records: boilerplate PDFs, "
            "empty orphans, and unsplit duplicate rulings."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without writing to the database.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_cleanup(dsn, dry_run=args.dry_run)

    logger.info(
        "Cleanup complete: %d records deleted "
        "(boilerplate_pdf=%d, empty_orphan=%d, unsplit_duplicate=%d, skipped=%d)",
        stats["total_deleted"],
        stats["boilerplate_pdf"],
        stats["empty_orphan"],
        stats["unsplit_duplicate"],
        stats["skipped_unknown"],
    )

    # Final UNKNOWN count
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(FIND_COURT_QUERY)
            court_row = cur.fetchone()
            if court_row:
                cur.execute(
                    "SELECT COUNT(*) FROM cases "
                    "WHERE case_number LIKE 'UNKNOWN-%%' "
                    "AND court_id = %s",
                    (str(court_row[0]),),
                )
                count_row = cur.fetchone()
                remaining = count_row[0] if count_row else "?"
                logger.info(
                    "Remaining Riverside UNKNOWN records: %s",
                    remaining,
                )


if __name__ == "__main__":
    main()
