#!/usr/bin/env python3
"""Backfill judge_id, motion_type, and outcome on existing rulings.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and reprocesses ruling_text through
the extraction functions added in PRs #232 and #233.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_ruling_fields.py

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of rulings to process per batch (default: 100).
    --limit N       Maximum total rulings to process (default: unlimited).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402

from ingestion.db import resolve_judge  # noqa: E402
from ingestion.extract import extract_judge_name, extract_motion_type, extract_outcome  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT r.id, r.ruling_text, r.court_id, r.judge_id, r.outcome, r.motion_type
    FROM rulings r
    WHERE r.ruling_text IS NOT NULL
      AND (r.judge_id IS NULL OR r.outcome IS NULL OR r.motion_type IS NULL)
    ORDER BY r.created_at
    LIMIT %s OFFSET %s
"""

UPDATE_QUERY = """
    UPDATE rulings
    SET judge_id     = COALESCE(%s::uuid, judge_id),
        outcome      = COALESCE(%s::ruling_outcome, outcome),
        motion_type  = COALESCE(%s, motion_type)
    WHERE id = %s::uuid
"""


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    offset: int = 0,
) -> tuple[int, int]:
    """Process one batch of rulings.  Returns (processed, updated) counts."""
    processed = 0
    updated = 0

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (batch_size, offset))
        rows = cur.fetchall()

    if not rows:
        return 0, 0

    for (
        ruling_id,
        ruling_text,
        court_id,
        existing_judge_id,
        existing_outcome,
        existing_motion_type,
    ) in rows:
        processed += 1
        new_outcome = None
        new_motion_type = None
        new_judge_id = None

        # Only extract fields that are currently NULL
        if existing_outcome is None:
            new_outcome = extract_outcome(ruling_text)

        if existing_motion_type is None:
            new_motion_type = extract_motion_type(ruling_text)

        if existing_judge_id is None:
            raw_name = extract_judge_name(ruling_text)
            if raw_name:
                new_judge_id = resolve_judge(conn, raw_name, str(court_id))

        # Skip if nothing new to write
        if new_outcome is None and new_motion_type is None and new_judge_id is None:
            continue

        with conn.cursor() as cur:
            cur.execute(
                UPDATE_QUERY,
                (new_judge_id, new_outcome, new_motion_type, str(ruling_id)),
            )
        updated += 1

    return processed, updated


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
    offset = 0

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated = backfill_batch(conn, effective_batch, offset)
            total_processed += processed
            total_updated += updated

            logger.info(
                "Batch: processed=%d updated=%d (total: %d/%d)",
                processed,
                updated,
                total_processed,
                total_updated,
            )

            if processed < effective_batch:
                # Last batch — no more rows
                break

            offset += effective_batch

        if dry_run:
            conn.rollback()
            logger.info("DRY RUN — rolled back all changes")
        else:
            conn.commit()
            logger.info("Committed all changes")

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
        description="Backfill judge_id, outcome, and motion_type on existing rulings.",
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
