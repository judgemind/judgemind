#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Fix San Bernardino department naming inconsistency and bad hearing date (#2123).

Two fixes:
1. Normalize hyphenated department names: S-17 -> S17, R-14 -> R14
2. Fix future hearing date: 2030-03-26 -> 2026-03-26 on San Bernardino R14 rulings

Usage (via ECS):
    scripts/ecs-run-task.sh scripts/fix_sb_departments.py -- --dry-run
    scripts/ecs-run-task.sh scripts/fix_sb_departments.py

Requires DATABASE_URL environment variable.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Matches letter prefix + hyphen + digits (e.g. S-17, R-14)
_DEPT_HYPHEN_RE = re.compile(r"^([A-Za-z]+)-(\d+)$")


def normalize_department(dept: str) -> str:
    """Strip hyphens from department codes: S-17 -> S17, R-14 -> R14."""
    m = _DEPT_HYPHEN_RE.match(dept.strip())
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return dept.strip()


def fix_departments(conn: psycopg.Connection, *, dry_run: bool) -> int:
    """Normalize hyphenated SB department names in the rulings table.

    Returns the number of rows that would be (or were) updated.
    """
    # Find all hyphenated SB departments
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT r.department
            FROM rulings r
            JOIN cases c ON r.case_id = c.id
            JOIN courts ct ON c.court_id = ct.id
            WHERE ct.county = 'San Bernardino'
              AND r.department ~ '^[A-Za-z]+-[0-9]+$'
            ORDER BY r.department
            """
        )
        hyphenated_depts = [row[0] for row in cur.fetchall()]

    if not hyphenated_depts:
        logger.info("No hyphenated SB departments found — nothing to fix.")
        return 0

    total_estimated = 0
    total_actually_updated = 0
    for dept in hyphenated_depts:
        normalized = normalize_department(dept)
        logger.info("Department: %s -> %s", dept, normalized)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM rulings r
                JOIN cases c ON r.case_id = c.id
                JOIN courts ct ON c.court_id = ct.id
                WHERE ct.county = 'San Bernardino'
                  AND r.department = %s
                """,
                (dept,),
            )
            count = cur.fetchone()[0]
            logger.info("  Rulings affected: %d", count)
            total_estimated += count

            if not dry_run:
                cur.execute(
                    """
                    UPDATE rulings
                    SET department = %s
                    WHERE department = %s
                      AND case_id IN (
                          SELECT c.id FROM cases c
                          JOIN courts ct ON c.court_id = ct.id
                          WHERE ct.county = 'San Bernardino'
                      )
                    """,
                    (normalized, dept),
                )
                logger.info("  Updated %d rows.", cur.rowcount)
                total_actually_updated += cur.rowcount

    if not dry_run:
        conn.commit()
        logger.info(
            "Department fix committed. Total rows updated: %d", total_actually_updated
        )
        return total_actually_updated
    else:
        logger.info("[DRY RUN] Would update %d rows total.", total_estimated)
        return total_estimated


def fix_hearing_date(conn: psycopg.Connection, *, dry_run: bool) -> int:
    """Fix the bad hearing date 2030-03-26 -> 2026-03-26 on SB R14 rulings.

    Returns the number of rows that would be (or were) updated.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.department, r.hearing_date
            FROM rulings r
            JOIN cases c ON r.case_id = c.id
            JOIN courts ct ON c.court_id = ct.id
            WHERE ct.county = 'San Bernardino'
              AND r.hearing_date = '2030-03-26'
            """
        )
        rows = cur.fetchall()

    if not rows:
        logger.info("No SB rulings with hearing_date = 2030-03-26 found.")
        return 0

    logger.info("Found %d ruling(s) with hearing_date = 2030-03-26:", len(rows))
    for ruling_id, dept, hd in rows:
        logger.info("  id=%s, department=%s, hearing_date=%s", ruling_id, dept, hd)

    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE rulings
                SET hearing_date = '2026-03-26'
                WHERE hearing_date = '2030-03-26'
                  AND case_id IN (
                      SELECT c.id FROM cases c
                      JOIN courts ct ON c.court_id = ct.id
                      WHERE ct.county = 'San Bernardino'
                  )
                """
            )
            updated = cur.rowcount
            conn.commit()
            logger.info("Hearing date fix committed. Updated %d rows.", updated)
            return updated
    else:
        logger.info("[DRY RUN] Would update %d rows.", len(rows))
        return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix SB department naming and bad hearing date (#2123)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing to the database.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required.")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)
    try:
        logger.info("=== Fix 1: Normalize hyphenated department names ===")
        dept_count = fix_departments(conn, dry_run=args.dry_run)

        logger.info("")
        logger.info("=== Fix 2: Fix bad hearing date (2030-03-26 -> 2026-03-26) ===")
        date_count = fix_hearing_date(conn, dry_run=args.dry_run)

        logger.info("")
        logger.info(
            "Summary: %d department rows, %d hearing date rows %s.",
            dept_count,
            date_count,
            "would be updated" if args.dry_run else "updated",
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
