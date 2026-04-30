#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Delete derived.rulings and derived.cases rows created by the ca-governor-appointments
scraper before the #3688 early-return fix was deployed.

The scraper emits judge-bio press releases that share the CapturedDocument shape but
are NOT rulings. Before #3688, the ingestion worker passed them through the LLM
extractor, producing 178 rows with UNKNOWN-* case_numbers, empty case_titles, and a
'Governor / Statewide' court entry in derived.rulings.

Surgical DELETE is used here rather than rebuild_db.py --county Statewide because
rebuild would re-walk the same S3 press-release objects through the now-fixed scraper
(which early-returns correctly), producing the same end-state but at higher cost and
noise. staging.captures rows are intentionally preserved — #2144 will consume them.

Usage via ECS (dev DB is in a private VPC):
    scripts/ecs-run-task.sh scripts/one_off/cleanup_governor_rulings_pollution.py

The script is idempotent: re-running on a clean DB is a no-op (DELETE returns 0 rows).

See: https://github.com/judgemind/judgemind/issues/3688
"""

from __future__ import annotations

import logging
import os
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Delete all derived.rulings rows for the 'Governor / Statewide' court.
DELETE_RULINGS_SQL = """
    DELETE FROM derived.rulings r
    USING derived.courts co
    WHERE r.court_id = co.id
      AND co.county = 'Statewide'
      AND co.court_name ILIKE '%governor%'
    RETURNING r.id
"""

# Delete orphaned derived.cases rows: cases with no remaining rulings that
# were created for the Governor court and have an UNKNOWN-* case_number.
DELETE_CASES_SQL = """
    DELETE FROM derived.cases c
    WHERE NOT EXISTS (
        SELECT 1 FROM derived.rulings r WHERE r.case_id = c.id
    )
      AND c.case_number LIKE 'UNKNOWN-%'
      AND c.court_id IN (
          SELECT id FROM derived.courts
          WHERE county = 'Statewide'
            AND court_name ILIKE '%governor%'
      )
    RETURNING c.id
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the cleanup and log row counts."""
    logger.info("Connecting to database")
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            logger.info("Deleting derived.rulings rows for Governor/Statewide court")
            cur.execute(DELETE_RULINGS_SQL)
            deleted_rulings = cur.rowcount
            logger.info("Deleted %d derived.rulings rows", deleted_rulings)

            logger.info("Deleting orphaned derived.cases rows")
            cur.execute(DELETE_CASES_SQL)
            deleted_cases = cur.rowcount
            logger.info("Deleted %d derived.cases rows", deleted_cases)

        conn.commit()
        logger.info(
            "Cleanup complete — rulings_deleted=%d cases_deleted=%d",
            deleted_rulings,
            deleted_cases,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Cleanup failed")
        sys.exit(1)
