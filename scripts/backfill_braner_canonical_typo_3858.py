#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill Braner judge canonical-name typo (#3858).

Fixes the canonical_name for judge bafa63e4-6a93-4d97-b00e-92e446857c7c from
the roster typo 'Mattew C. Braner' to the correct spelling 'Matthew C. Braner',
and inserts the typo as a roster_match alias so historical lookups still resolve.

This is safe: the judge UUID is preserved, so all 30 attached rulings remain
linked.  The alias insertion uses ON CONFLICT DO NOTHING to be idempotent.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- scripts/run-py.sh scripts/backfill_braner_canonical_typo_3858.py

Options:
    --dry-run    Print what would be changed without writing to the database.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

_JUDGE_UUID = "bafa63e4-6a93-4d97-b00e-92e446857c7c"
_OLD_CANONICAL = "Mattew C. Braner"
_NEW_CANONICAL = "Matthew C. Braner"

UPDATE_QUERY = """
    UPDATE derived.judges
    SET canonical_name = %s,
        updated_at = NOW()
    WHERE id = %s::uuid
      AND canonical_name = %s
"""

ALIAS_QUERY = """
    INSERT INTO derived.judge_aliases (judge_id, raw_name, source, confidence, is_verified)
    VALUES (%s::uuid, %s, 'roster_match', 0.85, FALSE)
    ON CONFLICT DO NOTHING
"""


def run_backfill(dsn: str, *, dry_run: bool = False) -> dict[str, int]:
    """Apply the canonical-name fix and alias insertion.

    Returns a dict with 'rows_updated' and 'aliases_inserted'.
    """
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (_NEW_CANONICAL, _JUDGE_UUID, _OLD_CANONICAL))
            rows_updated = cur.rowcount

            cur.execute(ALIAS_QUERY, (_JUDGE_UUID, _OLD_CANONICAL))
            aliases_inserted = cur.rowcount

        if dry_run:
            conn.rollback()
            logger.info(
                "DRY RUN: would update %d judge row(s), insert %d alias row(s) [rolled back]",
                rows_updated,
                aliases_inserted,
            )
        else:
            conn.commit()
            logger.info(
                "Committed: updated %d judge row(s), inserted %d alias row(s)",
                rows_updated,
                aliases_inserted,
            )

    return {"rows_updated": rows_updated, "aliases_inserted": aliases_inserted}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix Braner judge canonical-name typo (#3858).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing to the database.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_backfill(dsn, dry_run=args.dry_run)

    logger.info(
        "Backfill complete: %d row(s) updated, %d alias row(s) inserted",
        stats["rows_updated"],
        stats["aliases_inserted"],
    )


if __name__ == "__main__":
    main()
