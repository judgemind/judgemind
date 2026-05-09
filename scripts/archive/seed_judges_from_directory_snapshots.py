#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill ``derived.judges`` from ``derived.court_directory_snapshots`` (#4370).

Reads the latest snapshot for every court in
``derived.court_directory_snapshots`` and INSERTs any canonical judge
name that isn't already in ``derived.judges``.  Idempotent — re-running
is a no-op once all judges have been seeded.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- scripts/run-py.sh scripts/seed_judges_from_directory_snapshots.py

Options:
    --dry-run    Print the count of judges that would be inserted, then
                 roll back the transaction without committing.
    --court-id   Restrict the seed to a single snapshot court_id (e.g.
                 ``ca_los_angeles``).  By default all courts are seeded.

Why this exists
---------------
After #4297 landed the single-word JUDGE/DEPT surname-expansion helper,
LA dept-25 rulings continued to store ``judge_id = NULL`` because
``Karine Mkrtchyan`` did not exist in ``derived.judges`` — so the
helper's surname-suffix lookup fell through to no-match.  This is a
chicken-and-egg: judges who only ever appear in tentatives as a bare
surname never get created in ``derived.judges``, because
``_looks_like_valid_judge_name`` (db.py:760) rejects single-word names
from creating new judge rows.

``derived.court_directory_snapshots`` already carries the canonical
mapping from the LA judicial-officer directory scrape — those names
are authoritative.  This script seeds them into ``derived.judges`` so
#4297's helper has expansion candidates.

After this script runs against dev, re-run the targeted reingest from
#4297's verification:

    scripts/ecs-run-task.sh scripts/reingest_from_s3.py \\
        -- --county "Los Angeles" --department-in 25 --no-llm

and confirm dept-25 NULL count drops to ≤ ~5.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from ingestion.judge_seed import seed_judges_from_directory_snapshots

# Use ``configure_structlog`` so any ``extra=`` fields passed to logger calls
# surface in CloudWatch Logs Insights output. ``stdlib_bridge=True`` routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
# extras as one event per line. The previous ``logging.basicConfig`` format
# string (``"%(asctime)s %(levelname)-8s %(message)s"``) silently dropped
# every ``extra=`` field — see #4368 for the post-deploy verification
# incident that motivated migrating every ``scripts/*.py`` to this pattern
# (#4373).
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)


def run_backfill(
    dsn: str,
    *,
    dry_run: bool = False,
    only_court_id: str | None = None,
) -> dict[str, int]:
    """Apply the seed and return stats."""
    with psycopg.connect(dsn) as conn:
        stats = seed_judges_from_directory_snapshots(conn, only_court_id=only_court_id)

        if dry_run:
            conn.rollback()
            logger.info(
                "DRY RUN: would insert %d judge row(s) "
                "(courts=%d candidates=%d skipped_existing=%d "
                "skipped_invalid=%d skipped_no_court=%d) [rolled back]",
                stats["inserted"],
                stats["courts"],
                stats["candidates"],
                stats["skipped_existing"],
                stats["skipped_invalid"],
                stats["skipped_no_court"],
            )
        else:
            conn.commit()
            logger.info(
                "Committed: inserted %d judge row(s) "
                "(courts=%d candidates=%d skipped_existing=%d "
                "skipped_invalid=%d skipped_no_court=%d)",
                stats["inserted"],
                stats["courts"],
                stats["candidates"],
                stats["skipped_existing"],
                stats["skipped_invalid"],
                stats["skipped_no_court"],
            )

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed derived.judges from court_directory_snapshots (#4370).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the count of judges that would be inserted, then "
        "roll back without committing.",
    )
    parser.add_argument(
        "--court-id",
        default=None,
        help="Restrict to a single snapshot court_id (e.g. ca_los_angeles). "
        "Default: all courts.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    run_backfill(dsn, dry_run=args.dry_run, only_court_id=args.court_id)


if __name__ == "__main__":
    main()
