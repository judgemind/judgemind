#!/usr/bin/env python3
"""One-time backfill: set case_type for Ventura cases using case number prefix.

Ventura case numbers encode the case type in positions 5-6 (after a 4-digit year):
  - CU = Civil Unlimited  -> case_type = 'civil'
  - CL = Civil Limited     -> case_type = 'civil'
  - PR = Probate           -> case_type = 'probate'

This backfill targets Ventura cases with NULL case_type and a parseable case
number prefix, closing the gap identified in #1694.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_ventura_case_type.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_ventura_case_type.py
"""

# venv: scraper-framework
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

# Ventura case number type code patterns (positions 5-6 after 4-digit year).
_VENTURA_TYPE_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\d{4,}CU", re.IGNORECASE), "civil"),
    (re.compile(r"^\d{4,}CL[A-Z]", re.IGNORECASE), "civil"),
    (re.compile(r"^\d{4,}PR", re.IGNORECASE), "probate"),
]


def _infer_case_type(case_number: str) -> str | None:
    """Infer case type from a Ventura case number prefix."""
    if not case_number:
        return None
    for pattern, case_type in _VENTURA_TYPE_MAP:
        if pattern.match(case_number):
            return case_type
    return None


def run_backfill(conn: psycopg.Connection, *, dry_run: bool = True) -> int:
    """Set case_type for Ventura cases where it is NULL and parseable.

    Returns the number of rows updated.
    """
    with conn.cursor() as cur:
        # Report current state.
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN c.case_type IS NOT NULL THEN 1 END) AS with_type
            FROM cases c
            JOIN courts ct ON ct.id = c.court_id
            WHERE ct.county = 'Ventura'
            """
        )
        row = cur.fetchone()
        total, with_type = row[0], row[1]
        logger.info(
            "Ventura cases: %d total, %d with case_type, %d missing (%.1f%% complete)",
            total,
            with_type,
            total - with_type,
            100.0 * with_type / total if total > 0 else 0,
        )

        if total == with_type:
            logger.info("All Ventura cases already have case_type. Nothing to do.")
            return 0

        # Fetch cases with NULL case_type.
        cur.execute(
            """
            SELECT c.id, c.case_number
            FROM cases c
            JOIN courts ct ON ct.id = c.court_id
            WHERE ct.county = 'Ventura'
              AND c.case_type IS NULL
            """
        )
        rows = cur.fetchall()
        logger.info("Found %d Ventura cases with NULL case_type", len(rows))

        updated = 0
        skipped = 0
        for case_id, case_number in rows:
            inferred = _infer_case_type(case_number)
            if inferred is None:
                logger.warning(
                    "Cannot infer case_type from case_number=%s (case_id=%s)",
                    case_number,
                    case_id,
                )
                skipped += 1
                continue
            cur.execute(
                "UPDATE cases SET case_type = %s WHERE id = %s",
                (inferred, case_id),
            )
            updated += 1

        logger.info(
            "%s %d Ventura cases, skipped %d (unrecognised prefix)",
            "Would update" if dry_run else "Updated",
            updated,
            skipped,
        )

        # Post-update report.
        if not dry_run:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(CASE WHEN c.case_type IS NOT NULL THEN 1 END) AS with_type
                FROM cases c
                JOIN courts ct ON ct.id = c.court_id
                WHERE ct.county = 'Ventura'
                """
            )
            row = cur.fetchone()
            total_after, with_type_after = row[0], row[1]
            completeness = (
                100.0 * with_type_after / total_after if total_after > 0 else 0
            )
            logger.info(
                "After update: %d/%d Ventura cases have case_type (%.1f%%)",
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
        description="Backfill case_type for Ventura cases (#1694)"
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
