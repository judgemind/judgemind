#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill Orange County department codes: normalize W08 → W8 in derived.rulings.

The forward-fix normalizer (_normalize_orange in
packages/scraper-framework/src/ingestion/department_normalize.py) was shipped in
#3969 (commit 2fc8911) and already canonicalizes W08→W8 for new captures. This
script patches the 75 existing rows that were ingested before the fix.

The UPDATE is idempotent: once all W08 rows have been renamed to W8, a re-run
returns 0 rows and prints "Updated 0 rulings".

Usage via ECS (dev DB is in a private VPC):
    scripts/ecs-run-task.sh scripts/one_off/backfill_oc_dept_w08_to_w8.py

See: https://github.com/judgemind/judgemind/issues/3981
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

UPDATE_SQL = """
    UPDATE derived.rulings
    SET department = 'W8'
    WHERE department = 'W08'
      AND court_id IN (SELECT id FROM derived.courts WHERE county = 'Orange')
    RETURNING id
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the backfill and log row counts."""
    logger.info("Connecting to database")
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            logger.info("Updating derived.rulings: W08 → W8 for Orange County courts")
            cur.execute(UPDATE_SQL)
            updated_ids = [row[0] for row in cur.fetchall()]
            updated_count = len(updated_ids)
            logger.info("Updated ruling IDs: %s", updated_ids)

        conn.commit()
        logger.info("Backfill complete — rulings_updated=%d", updated_count)

    print(f"Updated {updated_count} rulings")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Backfill failed")
        sys.exit(1)
