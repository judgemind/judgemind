#!/usr/bin/env python3
"""One-time backfill: set case_type='civil' for all OC cases with NULL case_type.

The existing backfill_oc_field_gaps.py only targets documents created in the
last 14 days. This broader script covers ALL OC cases regardless of age,
closing the gap identified in #1580.

All OC tentative rulings are civil cases, so setting case_type='civil' is
correct for the entire population.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_oc_case_type_all.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_oc_case_type_all.py
"""

# venv: scraper-framework
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


def run_backfill(conn: psycopg.Connection, *, dry_run: bool = True) -> int:
    """Set case_type='civil' for all OC cases where case_type IS NULL.

    Returns the number of rows updated.
    """
    with conn.cursor() as cur:
        # First, report the current state.
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN c.case_type IS NOT NULL THEN 1 END) AS with_type
            FROM cases c
            JOIN courts ct ON ct.id = c.court_id
            WHERE ct.county = 'Orange'
            """
        )
        row = cur.fetchone()
        total, with_type = row[0], row[1]
        logger.info(
            "OC cases: %d total, %d with case_type, %d missing (%.1f%% complete)",
            total,
            with_type,
            total - with_type,
            100.0 * with_type / total if total > 0 else 0,
        )

        if total == with_type:
            logger.info("All OC cases already have case_type set. Nothing to do.")
            return 0

        # Update all OC cases with NULL case_type to 'civil'.
        cur.execute(
            """
            UPDATE cases
            SET case_type = 'civil'
            WHERE case_type IS NULL
              AND court_id IN (
                  SELECT id FROM courts WHERE county = 'Orange'
              )
            """
        )
        updated = cur.rowcount
        logger.info(
            "%s %d OC cases with case_type='civil'",
            "Would update" if dry_run else "Updated",
            updated,
        )

        # Report post-update state.
        if not dry_run:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(CASE WHEN c.case_type IS NOT NULL THEN 1 END) AS with_type
                FROM cases c
                JOIN courts ct ON ct.id = c.court_id
                WHERE ct.county = 'Orange'
                """
            )
            row = cur.fetchone()
            total_after, with_type_after = row[0], row[1]
            completeness = (
                100.0 * with_type_after / total_after if total_after > 0 else 0
            )
            logger.info(
                "After update: %d/%d OC cases have case_type (%.1f%%)",
                with_type_after,
                total_after,
                completeness,
            )

    if dry_run:
        conn.rollback()
        logger.info("DRY RUN — no changes committed.")
    else:
        conn.commit()
        logger.info("Changes committed.")

    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill case_type for all OC cases (#1580)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        updated = run_backfill(conn, dry_run=args.dry_run)

    mode = "DRY RUN" if args.dry_run else "APPLIED"
    logger.info("Backfill complete (%s): %d cases updated", mode, updated)


if __name__ == "__main__":
    main()
