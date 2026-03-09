#!/usr/bin/env python3
"""Backfill LA County judge names using the department-to-judge mapping.

Many LA County rulings have a department set but no judge_id, because the
judge name was not present in the ruling text.  The LA judicial officer
directory provides a department-to-judge mapping that can retroactively
fill these gaps.

For each LA ruling with a department but no judge_id, this script:
  1. Looks up the judge name via the dept-to-judge mapping
  2. Resolves the judge name to a canonical judge record (resolve_judge)
  3. Updates the ruling's judge_id
  4. Links the judge to the case via case_judges

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_la_judge_from_dept.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_la_judge_from_dept.py --dry-run

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

from courts.ca.la_dept_judges import (  # noqa: E402
    fetch_department_judge_mapping,
    lookup_judge_for_department,
)
from ingestion.db import resolve_judge, upsert_case_judge  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------

# Minimum cursor value for the first batch
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

# Fetch LA County rulings that have a department but no judge_id.
# Uses keyset pagination on ruling id (not LIMIT/OFFSET).
FETCH_QUERY = """
    SELECT r.id AS ruling_id,
           r.department,
           r.court_id,
           r.case_id,
           r.hearing_date
    FROM rulings r
    JOIN courts ct ON ct.id = r.court_id
    WHERE ct.state = 'CA'
      AND ct.county = 'Los Angeles'
      AND r.department IS NOT NULL
      AND r.judge_id IS NULL
      AND r.id > %s::uuid
    ORDER BY r.id
    LIMIT %s
"""

UPDATE_RULING_JUDGE_QUERY = """
    UPDATE rulings
    SET judge_id   = %s::uuid,
        updated_at = NOW()
    WHERE id = %s::uuid
      AND judge_id IS NULL
"""


def backfill_batch(
    conn: psycopg.Connection,
    dept_map: dict[str, str],
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[int, int, str]:
    """Process one batch of rulings.  Returns (processed, updated, next_cursor)."""
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for ruling_id, department, court_id, case_id, hearing_date in rows:
        processed += 1
        next_cursor = str(ruling_id)

        judge_name = lookup_judge_for_department(dept_map, department)
        if judge_name is None:
            logger.debug(
                "No mapping for department %r (ruling %s)", department, ruling_id
            )
            continue

        # Resolve to canonical judge record
        judge_id = resolve_judge(conn, judge_name, str(court_id))

        # Update the ruling
        with conn.cursor() as cur:
            cur.execute(UPDATE_RULING_JUDGE_QUERY, (judge_id, str(ruling_id)))

        # Link judge to case
        upsert_case_judge(conn, str(case_id), judge_id, hearing_date)

        logger.info(
            "Backfilled ruling %s: dept=%s -> judge=%s (id=%s)",
            ruling_id,
            department,
            judge_name,
            judge_id,
        )
        updated += 1

    return processed, updated, next_cursor


def run_backfill(
    dsn: str,
    *,
    dept_map: dict[str, str],
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

            processed, updated, cursor = backfill_batch(
                conn, dept_map, effective_batch, cursor
            )
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


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill LA County judge names from department-to-judge mapping.",
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

    # Fetch the live dept-to-judge mapping
    logger.info("Fetching LA department-to-judge mapping...")
    dept_map = fetch_department_judge_mapping()
    logger.info("Loaded %d department mappings", len(dept_map))

    stats = run_backfill(
        dsn,
        dept_map=dept_map,
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
