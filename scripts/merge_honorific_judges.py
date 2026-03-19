#!/usr/bin/env python3
# venv: scraper-framework
"""Merge duplicate judge records that have honorific prefixes in canonical_name.

After PR #331 fixed normalize_judge_name() to strip honorific prefixes, newly
ingested rulings produce clean canonical names (e.g. "Joseph B. Widman").
However, existing judge records created before the fix may still have prefixes
like "Hon. Joseph B. Widman" as separate records from "Joseph B. Widman".

This script finds those duplicates and merges them:
1. Query judges whose canonical_name starts with "Hon.", "Judge", "Honorable",
   or "The Honorable".
2. Re-normalize those names through normalize_judge_name() to get the correct
   canonical name.
3. Find a matching judge record at the same court with the normalized name.
4. Merge: update rulings, case_judges, and judge_aliases to point to the kept
   record, then delete the duplicate.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/merge_honorific_judges.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/merge_honorific_judges.py --dry-run

Options:
    --dry-run       Print what would be changed without writing to the database.
    --batch-size N  Number of judges to process per batch (default: 100).
    --limit N       Maximum total judges to process (default: unlimited).

The script is idempotent — re-running it will find no matching judges if all
honorific duplicates have already been merged.

Parent issue: #331
Closes: #424
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

# Fetch judges whose canonical_name still has an honorific prefix.
# Uses keyset pagination on judge id.
FETCH_HONORIFIC_JUDGES_QUERY = """
    SELECT j.id, j.canonical_name, j.court_id
    FROM judges j
    WHERE j.id > %s::uuid
      AND (
          j.canonical_name ILIKE 'Hon.%%'
          OR j.canonical_name ILIKE 'Hon %%'
          OR j.canonical_name ILIKE 'Judge %%'
          OR j.canonical_name ILIKE 'Judge:%%'
          OR j.canonical_name ILIKE 'Honorable %%'
          OR j.canonical_name ILIKE 'Honorable.%%'
          OR j.canonical_name ILIKE 'The Honorable %%'
          OR j.canonical_name ILIKE 'The Honorable.%%'
      )
    ORDER BY j.id
    LIMIT %s
"""

# Find a matching judge at the same court with the normalized canonical name.
# Excludes the current judge (the one with the prefix) so we find the "clean" one.
FIND_MERGE_TARGET_QUERY = """
    SELECT j.id
    FROM judges j
    WHERE j.court_id = %s::uuid
      AND j.canonical_name = %s
      AND j.id != %s::uuid
    LIMIT 1
"""

# Update rulings to point to the kept judge.
UPDATE_RULINGS_QUERY = """
    UPDATE rulings
    SET judge_id = %s::uuid
    WHERE judge_id = %s::uuid
"""

# Count rulings affected (for logging).
COUNT_RULINGS_QUERY = """
    SELECT COUNT(*) FROM rulings WHERE judge_id = %s::uuid
"""

# Delete case_judges rows for the duplicate judge that would conflict with
# existing rows for the kept judge, then update the remaining ones.
DELETE_CONFLICTING_CASE_JUDGES_QUERY = """
    DELETE FROM case_judges
    WHERE judge_id = %s::uuid
      AND case_id IN (
          SELECT case_id FROM case_judges WHERE judge_id = %s::uuid
      )
"""

UPDATE_CASE_JUDGES_QUERY = """
    UPDATE case_judges
    SET judge_id = %s::uuid
    WHERE judge_id = %s::uuid
"""

# Count case_judges links (for logging).
COUNT_CASE_JUDGES_QUERY = """
    SELECT COUNT(*) FROM case_judges WHERE judge_id = %s::uuid
"""

# Move judge_aliases from the duplicate to the kept judge.
# First delete aliases that would be exact duplicates (same raw_name on kept).
DELETE_DUPLICATE_ALIASES_QUERY = """
    DELETE FROM judge_aliases
    WHERE judge_id = %s::uuid
      AND raw_name IN (
          SELECT raw_name FROM judge_aliases WHERE judge_id = %s::uuid
      )
"""

UPDATE_ALIASES_QUERY = """
    UPDATE judge_aliases
    SET judge_id = %s::uuid
    WHERE judge_id = %s::uuid
"""

# Delete the now-orphaned duplicate judge record.
DELETE_JUDGE_QUERY = """
    DELETE FROM judges
    WHERE id = %s::uuid
"""

# If no existing target judge exists, just rename the canonical_name.
RENAME_JUDGE_QUERY = """
    UPDATE judges
    SET canonical_name = %s
    WHERE id = %s::uuid
"""


# ---------------------------------------------------------------------------
# Core merge logic (importable for testing)
# ---------------------------------------------------------------------------


def merge_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[int, int, int, str]:
    """Process one batch of honorific-prefix judges.

    Returns (processed, merged, renamed, next_cursor).
    - merged = duplicate was found and records were merged into it
    - renamed = no duplicate found, canonical_name was updated in place
    """
    processed = 0
    merged = 0
    renamed = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_HONORIFIC_JUDGES_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, 0, cursor

    for judge_id, canonical_name, court_id in rows:
        judge_id_str = str(judge_id)
        court_id_str = str(court_id)
        next_cursor = judge_id_str
        processed += 1

        # Re-normalize through the updated function
        normalized = normalize_judge_name(canonical_name)

        # If normalization didn't change anything, skip (shouldn't happen
        # given the ILIKE filter, but be defensive)
        if normalized == canonical_name:
            logger.debug(
                "Skipping judge %s: name unchanged after normalization (%r)",
                judge_id_str,
                canonical_name,
            )
            continue

        # Look for a merge target: another judge at the same court with the
        # normalized name
        with conn.cursor() as cur:
            cur.execute(
                FIND_MERGE_TARGET_QUERY,
                (court_id_str, normalized, judge_id_str),
            )
            target_row = cur.fetchone()

        if target_row is not None:
            # Merge into the existing clean judge
            target_id = str(target_row[0])
            _merge_judges(
                conn,
                source_id=judge_id_str,
                target_id=target_id,
                source_name=canonical_name,
                target_name=normalized,
            )
            merged += 1
        else:
            # No duplicate exists — just rename the canonical_name
            logger.info(
                "Renaming judge: %r -> %r (judge_id=%s, no merge target found)",
                canonical_name,
                normalized,
                judge_id_str,
            )
            with conn.cursor() as cur:
                cur.execute(RENAME_JUDGE_QUERY, (normalized, judge_id_str))
            renamed += 1

    return processed, merged, renamed, next_cursor


def _merge_judges(
    conn: psycopg.Connection,
    source_id: str,
    target_id: str,
    source_name: str,
    target_name: str,
) -> None:
    """Merge source judge into target judge.

    Moves all rulings, case_judges, and judge_aliases from source to target,
    then deletes the source judge record.
    """
    # Count affected rows for logging
    with conn.cursor() as cur:
        cur.execute(COUNT_RULINGS_QUERY, (source_id,))
        row = cur.fetchone()
        ruling_count = row[0] if row else 0

    with conn.cursor() as cur:
        cur.execute(COUNT_CASE_JUDGES_QUERY, (source_id,))
        row = cur.fetchone()
        case_judge_count = row[0] if row else 0

    logger.info(
        "Merging judge: %r -> %r (source=%s, target=%s, rulings=%d, case_judges=%d)",
        source_name,
        target_name,
        source_id,
        target_id,
        ruling_count,
        case_judge_count,
    )

    # 1. Move rulings to the target judge
    with conn.cursor() as cur:
        cur.execute(UPDATE_RULINGS_QUERY, (target_id, source_id))

    # 2. Handle case_judges: delete conflicts first, then move remaining
    with conn.cursor() as cur:
        cur.execute(DELETE_CONFLICTING_CASE_JUDGES_QUERY, (source_id, target_id))

    with conn.cursor() as cur:
        cur.execute(UPDATE_CASE_JUDGES_QUERY, (target_id, source_id))

    # 3. Move aliases: delete duplicates first, then move remaining
    with conn.cursor() as cur:
        cur.execute(DELETE_DUPLICATE_ALIASES_QUERY, (source_id, target_id))

    with conn.cursor() as cur:
        cur.execute(UPDATE_ALIASES_QUERY, (target_id, source_id))

    # 4. Delete the now-orphaned source judge
    with conn.cursor() as cur:
        cur.execute(DELETE_JUDGE_QUERY, (source_id,))


def run_merge(
    dsn: str,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full merge. Returns summary stats."""
    total_processed = 0
    total_merged = 0
    total_renamed = 0
    cursor: str = _CURSOR_MIN_UUID

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, merged, renamed, cursor = merge_batch(
                conn, effective_batch, cursor
            )
            total_processed += processed
            total_merged += merged
            total_renamed += renamed

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d merged=%d renamed=%d (total: %d/%d/%d)%s",
                processed,
                merged,
                renamed,
                total_processed,
                total_merged,
                total_renamed,
                " [dry-run, rolled back]" if dry_run else " [committed]",
            )

            if processed < effective_batch:
                break

    return {
        "total_processed": total_processed,
        "total_merged": total_merged,
        "total_renamed": total_renamed,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge duplicate judge records with honorific prefixes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing to the database.",
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

    stats = run_merge(
        dsn,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Merge complete: %d judges processed, %d merged, %d renamed",
        stats["total_processed"],
        stats["total_merged"],
        stats["total_renamed"],
    )


if __name__ == "__main__":
    main()
