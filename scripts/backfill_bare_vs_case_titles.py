#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill bare-vs case titles in derived.cases (#3990).

Finds rows in derived.cases where the case_title ends with a bare vs-separator
(e.g. "Steinman v", "Doe vs", "Aoyagi vs.") — indicating the defendant name
was truncated or missing when the LLM extracted the title — and NULLs the
case_title column so the display layer shows no title rather than a partial form.

The 22 historical rows that landed before the worker.py guard was deployed are
addressed by this script.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- scripts/run-py.sh scripts/backfill_bare_vs_case_titles.py

Options:
    --dry-run         Print what would be updated without writing to the database.
    --limit N         Maximum total cases to process (default: unlimited).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

# Use ``configure_structlog`` so any ``extra=`` fields passed to logger calls
# surface in CloudWatch Logs Insights output. ``stdlib_bridge=True`` routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
# extras as one event per line. The previous ``logging.basicConfig`` format
# string silently dropped every ``extra=`` field — see #4368 / #4373.
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

# Select cases whose case_title is a bare-vs form:
# starts with at least one uppercase letter, then any non-period characters,
# then ends with whitespace + v/vs/vs. (case-insensitive).
# Uses keyset pagination on (id) for efficient batching.
#
# `county` lives on `derived.courts` (not `derived.cases`), so we JOIN courts
# via `cases.court_id` to attach the per-county breakdown for logging.
FETCH_QUERY = """
    SELECT c.id, c.case_title, ct.county
    FROM derived.cases c
    JOIN derived.courts ct ON ct.id = c.court_id
    WHERE c.case_title ~* '^[A-Z][^.]*\\s+v[s]?\\.?\\s*$'
      AND c.id > %s
    ORDER BY c.id
    LIMIT %s
"""

NULL_QUERY = """
    UPDATE derived.cases
    SET case_title = NULL,
        updated_at = NOW()
    WHERE id = %s::uuid
"""

# Zero UUID for the first batch cursor
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

# Default batch size
_DEFAULT_BATCH_SIZE = 100


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    cursor: str = _CURSOR_MIN_UUID,
    dry_run: bool = False,
) -> tuple[int, int, str, dict[str, int]]:
    """Process one batch of affected case titles.

    Returns (processed, updated, next_cursor, county_counts).
    """
    processed = 0
    updated = 0
    next_cursor = cursor
    county_counts: dict[str, int] = {}

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor, county_counts

    for case_id, old_title, county in rows:
        processed += 1
        next_cursor = str(case_id)

        if not old_title:
            continue

        county_key = county or "unknown"
        county_counts[county_key] = county_counts.get(county_key, 0) + 1

        logger.info(
            "Case %s [%s]: nulling bare-vs title %r",
            case_id,
            county_key,
            old_title[:80],
        )

        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(NULL_QUERY, (str(case_id),))
        updated += 1

    return processed, updated, next_cursor, county_counts


def run_backfill(
    dsn: str,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Run the full backfill.  Returns summary stats."""
    total_processed = 0
    total_updated = 0
    all_county_counts: dict[str, int] = {}
    cursor: str = _CURSOR_MIN_UUID

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated, cursor, county_counts = backfill_batch(
                conn,
                effective_batch,
                cursor,
                dry_run=dry_run,
            )
            total_processed += processed
            total_updated += updated
            for county, count in county_counts.items():
                all_county_counts[county] = all_county_counts.get(county, 0) + count

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

    if all_county_counts:
        logger.info("Per-county counts: %s", all_county_counts)

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
        "county_counts": all_county_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill bare-vs case titles — null them in derived.cases.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing to the database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Number of cases per batch (default: {_DEFAULT_BATCH_SIZE}).",
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
        "Backfill complete: %d cases processed, %d nulled%s",
        stats["total_processed"],
        stats["total_updated"],
        " [dry-run]" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
