#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill duplicated case titles and trailing case-number fragments (#3511).

Finds rows in derived.cases where the case_title either:
  - contains a repeated segment (e.g. "A v. B A v. B"), or
  - ends with a stray case-number fragment (e.g. "... 2024Cubco20894")

and applies the corrected strip → dedupe → strip cleanup sequence.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- scripts/run-py.sh scripts/backfill_repeated_title_cleanup.py

Options:
    --dry-run         Print what would be updated without writing to the database.
    --batch-size N    Number of cases per batch (default: 100).
    --limit N         Maximum total cases to process (default: unlimited).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from framework.logging import configure_structlog
from ingestion.extract import dedupe_repeated_title, strip_trailing_case_number

# Use ``configure_structlog`` so any ``extra=`` fields passed to logger calls
# surface in CloudWatch Logs Insights output. ``stdlib_bridge=True`` routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
# extras as one event per line. The previous ``logging.basicConfig`` format
# string silently dropped every ``extra=`` field — see #4368 / #4373.
configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

# Select cases with long repeated titles or trailing case-number fragments.
# Uses keyset pagination on (id) for efficient batching.
FETCH_QUERY = """
    SELECT id, case_title
    FROM derived.cases
    WHERE (
        (
            LENGTH(case_title) > 100
            AND case_title ~ '(.{20,}) \\1'
        ) OR (
            case_title ~* '\\s+\\d{4}[A-Za-z]{2,5}\\d{4,8}\\s*$'
        )
    )
      AND id > %s
    ORDER BY id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE derived.cases
    SET case_title = %s,
        updated_at = NOW()
    WHERE id = %s::uuid
"""

# Zero UUID for the first batch cursor
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"


def _cleanup_title(title: str) -> str:
    """Apply strip → dedupe → strip cleanup sequence."""
    result = strip_trailing_case_number(title) or title
    result = dedupe_repeated_title(result) or result
    result = strip_trailing_case_number(result) or result
    return result


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[int, int, str]:
    """Process one batch of affected case titles.

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

        if not old_title:
            continue

        new_title = _cleanup_title(old_title)

        if new_title == old_title:
            logger.debug("Skipping case %s — no change", case_id)
            continue

        logger.info("Case %s: %r -> %r", case_id, old_title[:120], new_title[:120])

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
        description="Backfill duplicated case titles and trailing case-number fragments.",
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
