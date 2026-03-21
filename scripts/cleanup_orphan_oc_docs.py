#!/usr/bin/env python3
# venv: scraper-framework
"""Clean up orphan Orange County documents that have no associated ruling records.

Two categories of orphan documents are handled:

1. **Duplicate orphans** (~170): Documents created by non-idempotent backfill runs
   where a sibling document with the same case_id + source_url already has ruling
   records. These are redundant and safe to mark as removed.

2. **Genuinely orphaned** (~42): Documents created by the OC splitter where ruling
   creation failed (e.g., probate cases). These have no useful data and are also
   marked as removed.

Both categories are set to ``status = 'removed'`` (the document_status enum does not
include 'inactive' — 'removed' is the correct enum value for documents that should
no longer appear in queries).

Usage:
    scripts/ecs-run-task.sh scripts/cleanup_orphan_oc_docs.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_orphan_oc_docs.py -- --apply

Options:
    --dry-run   Report counts without modifying the database (default).
    --apply     Actually update document statuses in the database.

See: https://github.com/judgemind/judgemind/issues/1336
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

# Find all active OC documents with no ruling records.
FIND_ORPHANS_QUERY = """
    SELECT
        d.id            AS document_id,
        d.case_id,
        d.source_url,
        d.content_hash,
        d.created_at
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN rulings r ON r.document_id = d.id
    WHERE d.status = 'active'
      AND ct.county = 'Orange'
      AND r.id IS NULL
    ORDER BY d.created_at
"""

# For each orphan, check if a sibling document (same case_id + source_url)
# exists that DOES have a ruling. If so, this orphan is a duplicate.
CHECK_SIBLING_QUERY = """
    SELECT EXISTS (
        SELECT 1
        FROM documents d2
        JOIN rulings r ON r.document_id = d2.id
        WHERE d2.case_id = %s
          AND d2.source_url = %s
          AND d2.id != %s
          AND d2.status = 'active'
    )
"""

# Mark a document as removed.
MARK_REMOVED_QUERY = """
    UPDATE documents
    SET status = 'removed'
    WHERE id = %s
"""

# Count remaining active OC orphans (for verification).
COUNT_REMAINING_QUERY = """
    SELECT COUNT(*)
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN rulings r ON r.document_id = d.id
    WHERE d.status = 'active'
      AND ct.county = 'Orange'
      AND r.id IS NULL
"""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def run_cleanup(
    dsn: str,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Find and clean up orphan OC documents.

    Returns a dict with counts: total_orphans, duplicate_orphans,
    genuine_orphans, marked_removed, remaining_after.
    """
    stats: dict[str, int] = {
        "total_orphans": 0,
        "duplicate_orphans": 0,
        "genuine_orphans": 0,
        "marked_removed": 0,
        "remaining_after": 0,
    }

    with psycopg.connect(dsn) as conn:
        # Step 1: Find all orphans
        with conn.cursor() as cur:
            cur.execute(FIND_ORPHANS_QUERY)
            orphans = cur.fetchall()

        stats["total_orphans"] = len(orphans)
        logger.info("Found %d active OC documents with no ruling records", len(orphans))

        if not orphans:
            logger.info("Nothing to clean up.")
            return stats

        # Step 2: Classify each orphan
        duplicate_ids: list[str] = []
        genuine_ids: list[str] = []

        for doc_id, case_id, source_url, _content_hash, _created_at in orphans:
            with conn.cursor() as cur:
                cur.execute(
                    CHECK_SIBLING_QUERY, (str(case_id), source_url, str(doc_id))
                )
                row = cur.fetchone()
                has_sibling = row[0] if row else False

            if has_sibling:
                duplicate_ids.append(str(doc_id))
            else:
                genuine_ids.append(str(doc_id))

        stats["duplicate_orphans"] = len(duplicate_ids)
        stats["genuine_orphans"] = len(genuine_ids)

        logger.info(
            "Classification: %d duplicate orphans, %d genuinely orphaned",
            len(duplicate_ids),
            len(genuine_ids),
        )

        # Step 3: Mark all orphans as removed
        all_orphan_ids = duplicate_ids + genuine_ids
        with conn.cursor() as cur:
            for doc_id in all_orphan_ids:
                cur.execute(MARK_REMOVED_QUERY, (doc_id,))
            stats["marked_removed"] = len(all_orphan_ids)

        if apply:
            conn.commit()
            logger.info(
                "Marked %d documents as removed [committed]",
                stats["marked_removed"],
            )
        else:
            conn.rollback()
            logger.info(
                "Would mark %d documents as removed [dry-run, rolled back]",
                stats["marked_removed"],
            )

        # Step 4: Count remaining orphans for verification
        with conn.cursor() as cur:
            cur.execute(COUNT_REMAINING_QUERY)
            row = cur.fetchone()
            stats["remaining_after"] = row[0] if row else 0

        logger.info(
            "Remaining active OC orphan documents: %d",
            stats["remaining_after"],
        )

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Clean up orphan OC documents with no ruling records. "
            "Marks them as status='removed'."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report counts without modifying the database (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update document statuses in the database.",
    )
    args = parser.parse_args()

    # --apply overrides --dry-run
    apply = args.apply

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info("Mode: %s", "APPLY" if apply else "DRY-RUN")

    stats = run_cleanup(dsn, apply=apply)

    logger.info(
        "Summary: %d total orphans (%d duplicate, %d genuine), "
        "%d marked removed, %d remaining",
        stats["total_orphans"],
        stats["duplicate_orphans"],
        stats["genuine_orphans"],
        stats["marked_removed"],
        stats["remaining_after"],
    )


if __name__ == "__main__":
    main()
