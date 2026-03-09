#!/usr/bin/env python3
"""Deduplicate ruling rows that were created by non-idempotent scraper runs.

Prior to the deterministic document_id fix (#302), each scraper run generated
a random UUID for document_id, causing insert_document and insert_ruling to
create new rows on every run instead of deduplicating. This script:

  1. Finds groups of duplicate rulings (same case_id, hearing_date, department).
  2. Within each group, keeps the oldest ruling (smallest created_at).
  3. Deletes the newer duplicates.
  4. Deletes orphaned document rows (documents whose only referencing ruling
     was deleted and which have no other references).

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/dedup_rulings.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/dedup_rulings.py --dry-run

Options:
    --dry-run       Print what would be deleted without writing to the database.
    --batch-size N  Number of duplicate groups to process per batch (default: 100).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core dedup logic (importable for testing)
# ---------------------------------------------------------------------------

# Minimum cursor value for the first batch
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

# Find duplicate groups: rulings that share (case_id, hearing_date, department)
# and have more than one row. Returns the IDs to KEEP (oldest per group).
FIND_DUPLICATES_QUERY = """
    WITH ranked AS (
        SELECT id,
               document_id,
               case_id,
               hearing_date,
               department,
               ROW_NUMBER() OVER (
                   PARTITION BY case_id, hearing_date, COALESCE(department, '')
                   ORDER BY created_at ASC, id ASC
               ) AS rn
        FROM rulings
    )
    SELECT id, document_id
    FROM ranked
    WHERE rn > 1
    AND id > %s::uuid
    ORDER BY id
    LIMIT %s
"""

# Count total duplicates (for progress reporting)
COUNT_DUPLICATES_QUERY = """
    WITH ranked AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY case_id, hearing_date, COALESCE(department, '')
                   ORDER BY created_at ASC, id ASC
               ) AS rn
        FROM rulings
    )
    SELECT COUNT(*) FROM ranked WHERE rn > 1
"""


def count_duplicates(conn: psycopg.Connection) -> int:
    """Count total duplicate ruling rows."""
    with conn.cursor() as cur:
        cur.execute(COUNT_DUPLICATES_QUERY)
        row = cur.fetchone()
    return row[0] if row else 0


def dedup_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[dict[str, int], str]:
    """Delete one batch of duplicate rulings and their orphaned documents.

    Returns (stats_dict, next_cursor) where stats has keys:
    rulings_deleted, documents_deleted.
    """
    stats: dict[str, int] = {"rulings_deleted": 0, "documents_deleted": 0}

    with conn.cursor() as cur:
        cur.execute(FIND_DUPLICATES_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return stats, cursor

    ruling_ids = [str(row[0]) for row in rows]
    document_ids = [str(row[1]) for row in rows]
    next_cursor = ruling_ids[-1]

    # Delete duplicate rulings
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM rulings WHERE id = ANY(%s::uuid[])",
            (ruling_ids,),
        )
        stats["rulings_deleted"] = cur.rowcount

    # Delete orphaned documents — documents that are no longer referenced
    # by any ruling. We only check the document_ids from the deleted rulings.
    with conn.cursor() as cur:
        cur.execute(
            """DELETE FROM documents
               WHERE id = ANY(%s::uuid[])
                 AND NOT EXISTS (
                     SELECT 1 FROM rulings WHERE document_id = documents.id
                 )""",
            (document_ids,),
        )
        stats["documents_deleted"] = cur.rowcount

    return stats, next_cursor


def run_dedup(
    dsn: str,
    *,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full deduplication. Returns summary stats."""
    total: dict[str, int] = {
        "total_rulings_deleted": 0,
        "total_documents_deleted": 0,
    }

    with psycopg.connect(dsn) as conn:
        dup_count = count_duplicates(conn)
        logger.info("Found %d duplicate ruling rows to remove", dup_count)

        if dup_count == 0:
            return total

        # Process in batches using keyset pagination on ruling id.
        # In non-dry-run mode, deleted rows are removed so the cursor
        # still advances correctly. In dry-run mode, the cursor also
        # advances (rows are rolled back but the cursor tracks position).
        cursor: str = _CURSOR_MIN_UUID
        while True:
            batch_stats, cursor = dedup_batch(conn, batch_size, cursor)

            if batch_stats["rulings_deleted"] == 0:
                break

            total["total_rulings_deleted"] += batch_stats["rulings_deleted"]
            total["total_documents_deleted"] += batch_stats["documents_deleted"]

            if dry_run:
                conn.rollback()
                logger.info(
                    "Batch: rulings=%d docs=%d (total: %d/%d) [dry-run, rolled back]",
                    batch_stats["rulings_deleted"],
                    batch_stats["documents_deleted"],
                    total["total_rulings_deleted"],
                    total["total_documents_deleted"],
                )
            else:
                conn.commit()
                logger.info(
                    "Batch: rulings=%d docs=%d (total: %d/%d) [committed]",
                    batch_stats["rulings_deleted"],
                    batch_stats["documents_deleted"],
                    total["total_rulings_deleted"],
                    total["total_documents_deleted"],
                )

    return total


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate ruling rows created by non-idempotent scraper runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without writing to the database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of duplicate groups per batch (default: 100).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_dedup(
        dsn,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    logger.info(
        "Dedup complete: %d rulings deleted, %d orphaned documents deleted",
        stats["total_rulings_deleted"],
        stats["total_documents_deleted"],
    )


if __name__ == "__main__":
    main()
