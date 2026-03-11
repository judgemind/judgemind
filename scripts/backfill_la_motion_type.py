#!/usr/bin/env python3
"""Backfill motion_type for LA County rulings that have NULL motion_type.

After the pattern fixes in #421 (new patterns and bug fixes for curly
apostrophes and plural "motions to compel"), existing LA County rulings
with NULL motion_type can be re-extracted from their ruling_text.

This script is idempotent: it only touches rows where motion_type IS NULL
and ruling_text IS NOT NULL. Re-running it produces the same result.

Usage:
    scripts/ecs-run.sh --script scripts/backfill_la_motion_type.py

    # Dry run (no DB writes):
    scripts/ecs-run.sh --script scripts/backfill_la_motion_type.py -- --dry-run

    # Limit to N rows:
    scripts/ecs-run.sh --script scripts/backfill_la_motion_type.py -- --limit 10

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of rulings to process per batch (default: 100).
    --limit N       Maximum total rulings to process (default: unlimited).
"""

from __future__ import annotations

# Ensure we are running inside the scraper-framework venv (re-execs if not).
from _venv_helper import ensure_venv

ensure_venv("scraper-framework")

import argparse
import logging
import os
import sys
from datetime import datetime

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402

from ingestion.extract import extract_motion_type  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

FETCH_QUERY = """
    SELECT r.id, r.ruling_text, r.created_at
    FROM rulings r
    JOIN courts ct ON ct.id = r.court_id
    WHERE ct.state = 'CA'
      AND ct.county = 'Los Angeles'
      AND r.motion_type IS NULL
      AND r.ruling_text IS NOT NULL
      AND (r.created_at, r.id) > (%s, %s)
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE rulings
    SET motion_type = %s,
        updated_at  = NOW()
    WHERE id = %s::uuid
      AND motion_type IS NULL
"""

COUNT_QUERY = """
    SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE r.motion_type IS NULL) AS null_count,
        COUNT(*) FILTER (WHERE r.motion_type IS NOT NULL) AS filled_count
    FROM rulings r
    JOIN courts ct ON ct.id = r.court_id
    WHERE ct.state = 'CA'
      AND ct.county = 'Los Angeles'
      AND r.ruling_text IS NOT NULL
"""


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID),
) -> tuple[int, int, tuple[datetime, str]]:
    """Process one batch. Returns (processed, updated, next_cursor)."""
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for ruling_id, ruling_text, created_at in rows:
        processed += 1
        next_cursor = (created_at, str(ruling_id))

        motion_type = extract_motion_type(ruling_text)
        if motion_type is None:
            continue

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (motion_type, str(ruling_id)))
        updated += 1

    return processed, updated, next_cursor


def run_backfill(
    dsn: str,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full backfill. Returns summary stats."""
    total_processed = 0
    total_updated = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    with psycopg.connect(dsn) as conn:
        # Report pre-backfill counts
        with conn.cursor() as cur:
            cur.execute(COUNT_QUERY)
            row = cur.fetchone()
            if row:
                total, null_count, filled_count = row
                pct = (filled_count / total * 100) if total > 0 else 0
                logger.info(
                    "Pre-backfill: %d total LA rulings, %d with motion_type (%.1f%%), "
                    "%d NULL",
                    total,
                    filled_count,
                    pct,
                    null_count,
                )

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

        # Report post-backfill counts
        with conn.cursor() as cur:
            cur.execute(COUNT_QUERY)
            row = cur.fetchone()
            if row:
                total, null_count, filled_count = row
                pct = (filled_count / total * 100) if total > 0 else 0
                logger.info(
                    "Post-backfill: %d total LA rulings, %d with motion_type (%.1f%%), "
                    "%d still NULL",
                    total,
                    filled_count,
                    pct,
                    null_count,
                )

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill motion_type for LA County rulings with NULL motion_type.",
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
        help="Number of rulings per batch (default: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total rulings to process.",
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
        "Backfill complete: %d rulings processed, %d updated",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
