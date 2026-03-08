#!/usr/bin/env python3
"""Clean up UNKNOWN case records that contain 'No Tentative Rulings' boilerplate.

The Riverside scraper previously ingested boilerplate 'No Tentative Rulings' pages
as actual ruling records, creating cases with UNKNOWN-{uuid} case numbers. This
script identifies and deletes those records.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/cleanup_no_tentative_rulings.py

Options:
    --dry-run       Print what would be deleted without writing to the database.
    --batch-size N  Number of cases to process per batch (default: 100).
    --limit N       Maximum total cases to process (default: unlimited).

The script is idempotent — it only deletes rows where case_number starts with
'UNKNOWN-' and the ruling text matches the boilerplate pattern.
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

# Same pattern used in the scraper to detect boilerplate
_NO_TENTATIVE_RULINGS_RE = re.compile(
    r"^\s*No\s+Tentative\s+Rulings?\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT c.id, c.case_number, r.ruling_text
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
    ORDER BY c.created_at
    LIMIT %s OFFSET %s
"""

DELETE_RULINGS_QUERY = """
    DELETE FROM rulings
    WHERE case_id = %s::uuid
"""

DELETE_CASE_QUERY = """
    DELETE FROM cases
    WHERE id = %s::uuid
      AND case_number LIKE 'UNKNOWN-%%'
"""


# ---------------------------------------------------------------------------
# Core cleanup logic
# ---------------------------------------------------------------------------


def cleanup_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    offset: int = 0,
) -> tuple[int, int]:
    """Process one batch of UNKNOWN cases. Returns (processed, deleted) counts."""
    processed = 0
    deleted = 0

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (batch_size, offset))
        rows = cur.fetchall()

    if not rows:
        return 0, 0

    for case_id, case_number, ruling_text in rows:
        processed += 1

        if not ruling_text or not _NO_TENTATIVE_RULINGS_RE.match(ruling_text):
            continue

        logger.info(
            "Deleting boilerplate case %s (%s): %.80s...",
            case_id,
            case_number,
            ruling_text.replace("\n", " "),
        )

        with conn.cursor() as cur:
            cur.execute(DELETE_RULINGS_QUERY, (str(case_id),))
            cur.execute(DELETE_CASE_QUERY, (str(case_id),))
        deleted += 1

    return processed, deleted


def run_cleanup(
    dsn: str,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full cleanup. Returns summary stats."""
    total_processed = 0
    total_deleted = 0
    offset = 0

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, deleted = cleanup_batch(conn, effective_batch, offset)
            total_processed += processed
            total_deleted += deleted

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d deleted=%d (total: %d/%d)%s",
                processed,
                deleted,
                total_processed,
                total_deleted,
                " [dry-run, rolled back]" if dry_run else " [committed]",
            )

            if processed < effective_batch:
                break

            # When deleting, offset does not advance since rows are removed.
            # But when NOT deleting (dry-run or no matches), advance offset.
            if not dry_run and deleted > 0:
                # Rows were deleted, so next batch starts at same offset
                pass
            else:
                offset += effective_batch

    return {
        "total_processed": total_processed,
        "total_deleted": total_deleted,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete UNKNOWN cases containing 'No Tentative Rulings' boilerplate.",
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

    stats = run_cleanup(
        dsn,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Cleanup complete: %d UNKNOWN cases processed, %d deleted (boilerplate)",
        stats["total_processed"],
        stats["total_deleted"],
    )


if __name__ == "__main__":
    main()
