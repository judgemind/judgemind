#!/usr/bin/env python3
"""Backfill UNKNOWN-{uuid} case numbers by re-extracting from ruling text.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and attempts to extract real case numbers
from ruling text stored in the rulings table.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/backfill_unknown_case_numbers.py

Each batch is committed independently so that progress is saved
incrementally.  If the connection drops mid-run, already-committed
batches are preserved and the script can be safely re-run (it is
idempotent — it only updates rows where case_number starts with 'UNKNOWN-').

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of cases to process per batch (default: 100).
    --limit N       Maximum total cases to process (default: unlimited).
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

# Add the scraper-framework src to the path so we can import extract_case_number.
# This script is intended to be run from the repo root.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402

from ingestion.extract import extract_case_number  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

FETCH_QUERY = """
    SELECT c.id, c.case_number, r.ruling_text, c.created_at
    FROM cases c
    JOIN LATERAL (
        SELECT r2.ruling_text
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE c.case_number LIKE 'UNKNOWN-%%'
    AND (c.created_at, c.id) > (%s, %s)
    ORDER BY c.created_at, c.id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE cases
    SET case_number = %s,
        updated_at = NOW()
    WHERE id = %s::uuid
      AND case_number LIKE 'UNKNOWN-%%'
"""


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID),
) -> tuple[int, int, tuple[datetime, str]]:
    """Process one batch of UNKNOWN cases.  Returns (processed, updated, next_cursor)."""
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for case_id, old_case_number, ruling_text, created_at in rows:
        processed += 1
        next_cursor = (created_at, str(case_id))

        case_number = extract_case_number(ruling_text)
        if case_number is None:
            logger.debug(
                "No case number extracted for case %s (%s)",
                case_id,
                old_case_number,
            )
            continue

        logger.info("Case %s: %s -> %s", case_id, old_case_number, case_number)

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (case_number, str(case_id)))
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
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

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

    stats = {
        "total_processed": total_processed,
        "total_updated": total_updated,
    }
    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill UNKNOWN case numbers by re-extracting from ruling text.",
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
        "Backfill complete: %d UNKNOWN cases processed, %d updated with real case numbers",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
