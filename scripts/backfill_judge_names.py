#!/usr/bin/env python3
"""Backfill truncated judge canonical_name values.

Some judge names were truncated during ingestion (e.g. "James I. Montgomer"
instead of "James I. Montgomery"). This script re-extracts judge names from
ruling text and updates truncated canonical_name records in the judges table.

For each judge, it finds linked rulings via rulings.judge_id, re-extracts the
judge name from ruling_text using extract_judge_name(), normalizes it, and
updates canonical_name if the re-extracted name is longer (indicating the
stored name was truncated).

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_judge_names.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_judge_names.py --dry-run

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of judges to process per batch (default: 100).
    --limit N       Maximum total judges to process (default: unlimited).
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

from ingestion.db import normalize_judge_name  # noqa: E402
from ingestion.extract import extract_judge_name  # noqa: E402

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

# Fetch all judges, joining with rulings to get ruling_text for re-extraction.
# We look for judges whose rulings contain extractable judge names.
FETCH_QUERY = """
    SELECT DISTINCT j.id AS judge_id, j.canonical_name,
           r.ruling_text
    FROM judges j
    JOIN rulings r ON r.judge_id = j.id
    WHERE r.ruling_text IS NOT NULL
    AND j.id > %s::uuid
    ORDER BY j.id
    LIMIT %s
"""

UPDATE_JUDGE_QUERY = """
    UPDATE judges
    SET canonical_name = %s,
        updated_at     = NOW()
    WHERE id = %s::uuid
"""

UPDATE_ALIAS_QUERY = """
    UPDATE judge_aliases
    SET raw_name   = %s
    WHERE judge_id = %s::uuid
      AND raw_name = %s
"""


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[int, int, str]:
    """Process one batch of judges.  Returns (processed, updated, next_cursor)."""
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    # Group rulings by judge_id — a judge may have multiple rulings
    judge_rulings: dict[str, list[tuple[str, str]]] = {}
    last_judge_id: str = cursor
    for judge_id, canonical_name, ruling_text in rows:
        key = str(judge_id)
        last_judge_id = key
        if key not in judge_rulings:
            judge_rulings[key] = []
        judge_rulings[key].append((str(canonical_name), ruling_text))

    next_cursor = last_judge_id

    for judge_id, entries in judge_rulings.items():
        processed += 1
        current_canonical = entries[0][0]

        # Try to extract a full judge name from any of the linked rulings
        best_name: str | None = None
        for _, ruling_text in entries:
            extracted = extract_judge_name(ruling_text)
            if extracted:
                normalized = normalize_judge_name(extracted)
                # Keep the longest extracted name as the best candidate
                if best_name is None or len(normalized) > len(best_name):
                    best_name = normalized

        if best_name is None:
            continue

        # Only update if the re-extracted name is longer than the stored one.
        # A longer name indicates the stored name was likely truncated.
        if len(best_name) <= len(current_canonical):
            continue

        logger.info(
            "Fixing truncated judge name: %r -> %r (judge_id=%s)",
            current_canonical,
            best_name,
            judge_id,
        )

        with conn.cursor() as cur:
            cur.execute(UPDATE_JUDGE_QUERY, (best_name, judge_id))

        # Also update the alias table: if there's an alias matching the
        # truncated canonical_name, update it to the full name.
        with conn.cursor() as cur:
            cur.execute(
                UPDATE_ALIAS_QUERY,
                (best_name, judge_id, current_canonical),
            )

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


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill truncated judge canonical_name values.",
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
        help="Number of judges per batch (default: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total judges to process.",
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
        "Backfill complete: %d judges processed, %d updated",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
