#!/usr/bin/env python3
"""Remove orphaned document rows left over after the ruling dedup.

After dedup_rulings.py removes duplicate ruling rows, some document rows may be
left with no ruling reference.  Additionally, in the 1:1 ruling-to-document
model, multiple documents can legitimately share the same content_hash when
different cases reference the same court page — those are NOT duplicates.

This script only deletes truly orphaned documents: rows that have no ruling
referencing them.  It also reports (but does not delete) the count of
content_hash groups that have more than one document, for visibility.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/dedup_documents.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/dedup_documents.py --dry-run

Options:
    --dry-run   Print what would be deleted without writing to the database.
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

COUNT_ORPHANED_QUERY = """
    SELECT COUNT(*)
    FROM documents d
    WHERE NOT EXISTS (
        SELECT 1 FROM rulings r WHERE r.document_id = d.id
    )
"""

DELETE_ORPHANED_QUERY = """
    DELETE FROM documents d
    WHERE NOT EXISTS (
        SELECT 1 FROM rulings r WHERE r.document_id = d.id
    )
"""

COUNT_CONTENT_HASH_GROUPS_QUERY = """
    SELECT COUNT(*) FROM (
        SELECT court_id, content_hash
        FROM documents
        GROUP BY court_id, content_hash
        HAVING COUNT(*) > 1
    ) sub
"""


def run_cleanup(
    dsn: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete orphaned documents. Returns summary stats."""
    stats: dict[str, int] = {
        "orphaned_before": 0,
        "orphaned_deleted": 0,
        "content_hash_groups_before": 0,
        "content_hash_groups_after": 0,
    }

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(COUNT_ORPHANED_QUERY)
            row = cur.fetchone()
            stats["orphaned_before"] = row[0] if row else 0

            cur.execute(COUNT_CONTENT_HASH_GROUPS_QUERY)
            row = cur.fetchone()
            stats["content_hash_groups_before"] = row[0] if row else 0

        logger.info(
            "Found %d orphaned documents (no ruling reference) and "
            "%d content_hash groups with >1 document",
            stats["orphaned_before"],
            stats["content_hash_groups_before"],
        )

        if stats["orphaned_before"] == 0:
            logger.info("Nothing to clean up.")
            return stats

        with conn.cursor() as cur:
            cur.execute(DELETE_ORPHANED_QUERY)
            stats["orphaned_deleted"] = cur.rowcount

        if dry_run:
            conn.rollback()
            logger.info(
                "Deleted %d orphaned documents [dry-run, rolled back]",
                stats["orphaned_deleted"],
            )
        else:
            conn.commit()
            logger.info(
                "Deleted %d orphaned documents [committed]",
                stats["orphaned_deleted"],
            )

        # Report remaining content_hash groups after cleanup
        with conn.cursor() as cur:
            cur.execute(COUNT_CONTENT_HASH_GROUPS_QUERY)
            row = cur.fetchone()
            stats["content_hash_groups_after"] = row[0] if row else 0

        logger.info(
            "Content-hash groups with >1 document: %d before, %d after "
            "(remaining groups are legitimate — different cases sharing "
            "the same document content)",
            stats["content_hash_groups_before"],
            stats["content_hash_groups_after"],
        )

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove orphaned document rows left over after ruling dedup.",
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
        "Document cleanup complete: %d orphaned documents deleted",
        stats["orphaned_deleted"],
    )


if __name__ == "__main__":
    main()
