#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Delete test-fixture pollution from telemetry.scraper_runs (#3806).

Removes rows where scraper_id belongs to the closed set of synthetic IDs
introduced by unit-test fixtures that leaked into the dev database before
the _block_telemetry_db_writes guard (PR #3526) was in place.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- scripts/run-py.sh scripts/oneoff/cleanup_test_telemetry_pollution.py

Options:
    --dry-run    Log what would be deleted without writing to the database.
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

SYNTHETIC_SCRAPER_IDS = (
    "test-stub",
    "good",
    "failing",
    "run-raiser",
    "scraper-b",
    "good-after",
    "good-after-ctor",
    "failing-ctor",
    "ctor-fail-db",
)

_PRE_COUNT_QUERY = """
    SELECT scraper_id, COUNT(*) AS cnt
    FROM telemetry.scraper_runs
    WHERE scraper_id IN %s
    GROUP BY scraper_id
    ORDER BY scraper_id
"""

_DELETE_QUERY = """
    DELETE FROM telemetry.scraper_runs
    WHERE scraper_id IN %s
"""

_POST_COUNT_QUERY = """
    SELECT scraper_id, COUNT(*) AS cnt
    FROM telemetry.scraper_runs
    WHERE scraper_id IN %s
    GROUP BY scraper_id
"""


def run_cleanup(dsn: str, *, dry_run: bool = False) -> dict[str, int]:
    """Delete synthetic-scraper rows from telemetry.scraper_runs.

    Returns a dict with 'pre_total' and 'post_total'.
    """
    id_tuple = tuple(SYNTHETIC_SCRAPER_IDS)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Pre-count
            cur.execute(_PRE_COUNT_QUERY, (id_tuple,))
            pre_rows = cur.fetchall()
            pre_total = 0
            for scraper_id, cnt in pre_rows:
                logger.info("pre-count  scraper_id=%s  count=%d", scraper_id, cnt)
                pre_total += cnt
            logger.info("pre-count total: %d row(s) to delete", pre_total)

            # Delete
            cur.execute(_DELETE_QUERY, (id_tuple,))

            # Post-count — assert zero
            cur.execute(_POST_COUNT_QUERY, (id_tuple,))
            post_rows = cur.fetchall()
            post_total = 0
            for scraper_id, cnt in post_rows:
                post_total += cnt
            if post_total != 0:
                conn.rollback()
                raise RuntimeError(
                    f"Post-delete count is {post_total}, expected 0 — rolled back"
                )

        if dry_run:
            conn.rollback()
            logger.info(
                "DRY RUN: would delete %d row(s) [rolled back]",
                pre_total,
            )
        else:
            conn.commit()
            logger.info(
                "Committed: deleted %d row(s), post-count confirmed zero",
                pre_total,
            )

    return {"pre_total": pre_total, "post_total": post_total}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete test-fixture pollution from telemetry.scraper_runs (#3806).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be deleted without writing to the database.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_cleanup(dsn, dry_run=args.dry_run)

    logger.info(
        "Cleanup complete: pre_total=%d, post_total=%d",
        stats["pre_total"],
        stats["post_total"],
    )


if __name__ == "__main__":
    main()
