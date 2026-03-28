#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill LA case titles that contain department header boilerplate (#1244).

Finds cases whose case_title contains "Law And Motion Rulings" (the LA
department calendar header text) and re-extracts the correct title from
the ruling_text stored in the rulings table.

Usage:
    scripts/ecs-run.sh --script scripts/backfill_la_header_titles.py
    scripts/ecs-run.sh --script scripts/backfill_la_header_titles.py -- --dry-run

Options:
    --dry-run    Print what would be updated without writing to the database.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg
from courts.ca.la_title_utils import extract_clean_title as _extract_clean_title

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    """Find and fix LA case titles containing department header boilerplate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)
    try:
        with conn.cursor() as cur:
            # Find cases with header boilerplate in the title
            cur.execute(
                """
                SELECT c.id, c.case_number, c.case_title
                FROM cases c
                JOIN courts ct ON c.court_id = ct.id
                WHERE ct.county = 'Los Angeles'
                  AND (c.case_title LIKE '%%Law And Motion Rulings%%'
                       OR c.case_title LIKE '%%LAW AND MOTION RULINGS%%'
                       OR c.case_title LIKE '%%Department%%Law%%Motion%%')
                """
            )
            bad_cases = cur.fetchall()

        logger.info("Found %d cases with header boilerplate in title", len(bad_cases))

        fixed = 0
        skipped = 0
        for case_id, case_number, old_title in bad_cases:
            # Fetch the ruling text for this case
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.ruling_text
                    FROM rulings r
                    JOIN documents d ON r.document_id = d.id
                    WHERE d.case_id = %s
                    ORDER BY r.hearing_date DESC NULLS LAST
                    LIMIT 1
                    """,
                    (case_id,),
                )
                row = cur.fetchone()

            if not row or not row[0]:
                logger.warning(
                    "No ruling text for case %s (%s), skipping",
                    case_id,
                    case_number,
                )
                skipped += 1
                continue

            ruling_text = row[0]
            new_title = _extract_clean_title(ruling_text)

            if new_title is None:
                logger.warning(
                    "Could not extract clean title for case %s (%s), skipping",
                    case_id,
                    case_number,
                )
                skipped += 1
                continue

            logger.info(
                "Case %s (%s):\n  OLD: %s\n  NEW: %s",
                case_id,
                case_number,
                old_title[:100],
                new_title,
            )

            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cases SET case_title = %s WHERE id = %s",
                        (new_title, case_id),
                    )
                conn.commit()
                fixed += 1
            else:
                fixed += 1

        logger.info(
            "Done: %d fixed, %d skipped%s",
            fixed,
            skipped,
            " (dry-run)" if args.dry_run else "",
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
