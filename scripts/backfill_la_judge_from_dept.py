#!/usr/bin/env python3
"""Backfill LA County judge names using the department-to-judge mapping.

Many LA County rulings have a department set but no judge_id, because the
judge name was not present in the ruling text.  The LA judicial officer
directory provides a department-to-judge mapping that can retroactively
fill these gaps.

For each LA ruling with a department but no judge_id, this script:
  1. Looks up the judge name via historical directory snapshots when available
  2. Falls back to the live directory when no snapshot predates the ruling
  3. Resolves the judge name to a canonical judge record (resolve_judge)
  4. Updates the ruling's judge_id
  5. Links the judge to the case via case_judges

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
import json
import logging
import os
import sys
from datetime import date, datetime, time

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

# LA County court identifier used for snapshot lookups
LA_COURT_ID_SNAPSHOT = "ca_los_angeles"

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

# Query the court_directory_snapshots table for the most recent snapshot
# captured on or before a given datetime.  This is the same logic as
# CourtDirectory.get_snapshot() but as a standalone function so the
# backfill script doesn't need to instantiate the ABC + S3 client.
SNAPSHOT_QUERY = """
    SELECT mapping FROM court_directory_snapshots
    WHERE court_id = %s AND captured_at <= %s
    ORDER BY captured_at DESC
    LIMIT 1
"""


def get_snapshot_mapping(
    conn: psycopg.Connection,
    court_id: str,
    as_of: datetime,
) -> dict[str, str] | None:
    """Get the directory snapshot closest to (but not after) a given datetime.

    This mirrors ``CourtDirectory.get_snapshot()`` but is a standalone function
    that only requires a DB connection — no S3 client or ABC instantiation.

    Parameters
    ----------
    conn : psycopg.Connection
        An open psycopg3 connection.
    court_id : str
        The court identifier (e.g. ``"ca_los_angeles"``).
    as_of : datetime
        The point in time to look up.

    Returns
    -------
    dict[str, str] | None
        The parsed {department: judge_name} mapping, or None if no
        snapshot exists before the given datetime.
    """
    with conn.cursor() as cur:
        cur.execute(SNAPSHOT_QUERY, (court_id, as_of))
        row = cur.fetchone()

    if row is None:
        return None

    mapping: dict[str, str] = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return mapping


def _hearing_date_to_datetime(hearing_date: date | datetime) -> datetime:
    """Convert a hearing date to a datetime for snapshot lookup.

    If already a datetime, return as-is.  If a date, convert to
    end-of-day (23:59:59) so the snapshot query includes any
    snapshot captured on the same day.
    """
    if isinstance(hearing_date, datetime):
        return hearing_date
    return datetime.combine(hearing_date, time(23, 59, 59))


def _get_effective_mapping(
    conn: psycopg.Connection,
    hearing_date: date | datetime,
    live_dept_map: dict[str, str],
    snapshot_cache: dict[date, dict[str, str] | None],
) -> dict[str, str]:
    """Return the best available department-to-judge mapping for a ruling.

    Uses the historical directory snapshot closest to (but not after) the
    ruling's hearing date.  Falls back to the live directory mapping when
    no snapshot predates the ruling.

    Results are cached per hearing_date to avoid repeated DB queries for
    rulings on the same date within a batch.

    Parameters
    ----------
    conn : psycopg.Connection
        An open psycopg3 connection.
    hearing_date : date | datetime
        The ruling's hearing date.
    live_dept_map : dict[str, str]
        The current live department-to-judge mapping (fallback).
    snapshot_cache : dict[date, dict[str, str] | None]
        Mutable cache keyed by date.  Updated in place.

    Returns
    -------
    dict[str, str]
        The department-to-judge mapping to use for this ruling.
    """
    cache_key = (
        hearing_date
        if isinstance(hearing_date, date) and not isinstance(hearing_date, datetime)
        else hearing_date
    )
    if isinstance(cache_key, datetime):
        cache_key = cache_key.date()

    if cache_key not in snapshot_cache:
        as_of = _hearing_date_to_datetime(hearing_date)
        snapshot_cache[cache_key] = get_snapshot_mapping(
            conn, LA_COURT_ID_SNAPSHOT, as_of
        )

    snapshot = snapshot_cache[cache_key]
    if snapshot is not None:
        return snapshot

    # No historical snapshot — fall back to the live directory
    return live_dept_map


def backfill_batch(
    conn: psycopg.Connection,
    dept_map: dict[str, str],
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
    *,
    snapshot_cache: dict[date, dict[str, str] | None] | None = None,
) -> tuple[int, int, str]:
    """Process one batch of rulings.  Returns (processed, updated, next_cursor).

    For each ruling, looks up the historical directory snapshot closest to
    the ruling's hearing date.  Falls back to the live ``dept_map`` when no
    snapshot predates the ruling.
    """
    processed = 0
    updated = 0
    next_cursor = cursor
    if snapshot_cache is None:
        snapshot_cache = {}

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for ruling_id, department, court_id, case_id, hearing_date in rows:
        processed += 1
        next_cursor = str(ruling_id)

        # Use historical snapshot if available, else fall back to live map
        effective_map = _get_effective_mapping(
            conn, hearing_date, dept_map, snapshot_cache
        )

        judge_name = lookup_judge_for_department(effective_map, department)
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
    """Run the full backfill.  Returns summary stats.

    Uses historical directory snapshots when available.  The snapshot cache
    is shared across batches so repeated hearing dates don't re-query the DB.
    Falls back to the live ``dept_map`` for rulings predating the first snapshot.
    """
    total_processed = 0
    total_updated = 0
    cursor: str = _CURSOR_MIN_UUID
    # Shared across batches so repeated dates don't re-query
    snapshot_cache: dict[date, dict[str, str] | None] = {}

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated, cursor = backfill_batch(
                conn,
                dept_map,
                effective_batch,
                cursor,
                snapshot_cache=snapshot_cache,
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
