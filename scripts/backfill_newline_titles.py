#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill case titles containing literal newline characters.

Finds existing case titles that contain newline (LF), carriage return (CR),
or tab characters and replaces them with single spaces, then collapses any
resulting multi-space sequences.

Usage (on ECS):
    scripts/ecs-run-task.sh scripts/backfill_newline_titles.py -- --dry-run

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of cases per batch (default: 100).
    --limit N       Maximum total cases to process (default: unlimited).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
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

# Find case titles containing newline, carriage return, or tab characters.
# Uses keyset pagination on (id) for efficient batching.
FETCH_QUERY = """
    SELECT id, case_title
    FROM cases
    WHERE (
        case_title LIKE '%%' || chr(10) || '%%'
        OR case_title LIKE '%%' || chr(13) || '%%'
        OR case_title LIKE '%%' || chr(9) || '%%'
    )
      AND id > %s
    ORDER BY id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE cases
    SET case_title = %s,
        updated_at = NOW()
    WHERE id = %s::uuid
"""

# Zero UUID for the first batch cursor
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"


def _clean_title(title: str) -> str:
    """Replace whitespace control characters with spaces and collapse."""
    return re.sub(r"\s+", " ", title).strip()


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[int, int, str]:
    """Process one batch of titles with newline characters.

    Returns (processed, updated, next_cursor).
    """
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for case_id, old_title in rows:
        processed += 1
        next_cursor = str(case_id)

        new_title = _clean_title(old_title)

        if new_title == old_title:
            logger.debug("Skipping case %s — no change after cleaning", case_id)
            continue

        logger.info("Case %s: %r -> %r", case_id, old_title, new_title)

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (new_title, str(case_id)))
        updated += 1

    return processed, updated, next_cursor


def run_backfill(
    dsn: str,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full backfill.  Returns summary stats."""
    total_processed = 0
    total_updated = 0
    cursor: str = _CURSOR_MIN_UUID

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated, cursor = backfill_batch(conn, effective_batch, cursor)
            total_processed += processed
            total_updated += updated

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d updated=%d (total: %d/%d)%s",
                processed,
                updated,
                total_processed,
                total_updated,
                " [dry-run, rolled back]" if dry_run else " [committed]",
            )

            if processed < effective_batch:
                break

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill case titles containing newline characters.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing to the database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of cases per batch (default: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total cases to process.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_backfill(
        dsn,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Backfill complete: %d cases processed, %d updated",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
