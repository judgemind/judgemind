#!/usr/bin/env python3
"""One-off fix: set case_type='probate' for Ventura case 202200572505.

Case "In The Matter Michele Nicole Gray" has a typo in its title (missing
"of" after "Matter"), which prevents the title-based case_type fallback from
matching.  The case number is all-digit with no embedded type code, so regex
extraction also fails.  This is the only remaining Ventura case without
case_type after the #2062 backfill.

This script sets case_type directly rather than broadening the regex (which
risks false positives on other "In the Matter..." titles).

Usage (ECS):
    scripts/ecs-run-task.sh scripts/fix_ventura_202200572505_case_type.py -- --dry-run
    scripts/ecs-run-task.sh scripts/fix_ventura_202200572505_case_type.py

Closes #2070.
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

CASE_NUMBER = "202200572505"
EXPECTED_TITLE = "In The Matter Michele Nicole Gray"
TARGET_CASE_TYPE = "probate"


def run_fix(conn: psycopg.Connection, *, dry_run: bool = True) -> bool:
    """Set case_type for the specific Ventura case.

    Returns True if the case was updated (or would be in dry-run mode).
    """
    with conn.cursor() as cur:
        # Verify the case exists and check current state.
        cur.execute(
            """
            SELECT c.id, c.case_title, c.case_type
            FROM cases c
            JOIN courts ct ON ct.id = c.court_id
            WHERE c.case_number = %s
              AND ct.county = 'Ventura'
            """,
            (CASE_NUMBER,),
        )
        row = cur.fetchone()

        if row is None:
            logger.error("Case %s not found in Ventura. Aborting.", CASE_NUMBER)
            return False

        case_id, case_title, current_case_type = row

        logger.info(
            "Found case %s: title=%r, current case_type=%s",
            CASE_NUMBER,
            case_title,
            current_case_type,
        )

        if current_case_type == TARGET_CASE_TYPE:
            logger.info(
                "Case %s already has case_type=%s. Nothing to do.",
                CASE_NUMBER,
                TARGET_CASE_TYPE,
            )
            return False

        if current_case_type is not None:
            logger.warning(
                "Case %s has unexpected case_type=%s (expected NULL). Skipping.",
                CASE_NUMBER,
                current_case_type,
            )
            return False

        # Verify the title matches what we expect (sanity check).
        if case_title and case_title.strip() != EXPECTED_TITLE:
            logger.warning(
                "Case %s title %r does not match expected %r. "
                "Proceeding anyway since the fix is safe.",
                CASE_NUMBER,
                case_title,
                EXPECTED_TITLE,
            )

        logger.info(
            "%s case_type=%s for case %s",
            "Would set" if dry_run else "Setting",
            TARGET_CASE_TYPE,
            CASE_NUMBER,
        )

        if not dry_run:
            cur.execute(
                "UPDATE cases SET case_type = %s WHERE id = %s",
                (TARGET_CASE_TYPE, case_id),
            )

    if dry_run:
        conn.rollback()
        logger.info("DRY RUN -- no changes committed.")
    else:
        conn.commit()
        logger.info("Changes committed.")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Fix case_type for Ventura case 202200572505 (#2070)")
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        updated = run_fix(conn, dry_run=args.dry_run)

    mode = "DRY RUN" if args.dry_run else "APPLIED"
    logger.info(
        "Fix complete (%s): %s",
        mode,
        "case updated" if updated else "no change needed",
    )


if __name__ == "__main__":
    main()
