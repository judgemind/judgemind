#!/usr/bin/env python3
"""Clean up Santa Clara data from a failed multimodal reingest on 2026-04-16.

The multimodal reingest created split child document rows without
corresponding ruling records (see #2365).  This script reverses the
partial reingest by:

1. Deleting split-child documents created on/after 2026-04-16
   (UUID v5 documents — these are the orphaned split children).
2. Deleting any rulings attached to those split children (cascade).
3. Re-activating superseded parent documents (status='superseded' ->
   'active') so they are visible again.
4. Reporting counts for verification.

Usage (via ECS):
    scripts/ecs-run-task.sh scripts/cleanup_sc_failed_reingest.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_sc_failed_reingest.py
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import sys

import psycopg
import structlog

from framework.logging import configure_structlog

configure_structlog()
logger = structlog.get_logger()

# The failed reingest ran on 2026-04-16.
_CUTOFF_DATE = "2026-04-16"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without actually changing.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        # Step 0: baseline counts
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM documents d
                JOIN courts ct ON d.court_id = ct.id
                WHERE ct.county = 'Santa Clara' AND d.status = 'active'
                """,
            )
            active_docs_before = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*) FROM rulings r
                JOIN documents d ON r.document_id = d.id
                JOIN courts ct ON d.court_id = ct.id
                WHERE ct.county = 'Santa Clara'
                """,
            )
            rulings_before = cur.fetchone()[0]

            # Count orphaned split children (UUID v5, created after cutoff)
            cur.execute(
                """
                SELECT COUNT(*) FROM documents d
                JOIN courts ct ON d.court_id = ct.id
                WHERE ct.county = 'Santa Clara'
                  AND d.created_at >= %s
                  AND substring(d.id::text, 15, 1) = '5'
                """,
                (_CUTOFF_DATE,),
            )
            orphan_count = cur.fetchone()[0]

            # Count superseded documents
            cur.execute(
                """
                SELECT COUNT(*) FROM documents d
                JOIN courts ct ON d.court_id = ct.id
                WHERE ct.county = 'Santa Clara' AND d.status = 'superseded'
                """,
            )
            superseded_count = cur.fetchone()[0]

        logger.info(
            "Baseline counts",
            active_docs=active_docs_before,
            rulings=rulings_before,
            orphaned_split_children=orphan_count,
            superseded_docs=superseded_count,
        )

        if orphan_count == 0 and superseded_count == 0:
            logger.info("Nothing to clean up — SC data looks healthy")
            return

        if args.dry_run:
            logger.info(
                "DRY RUN — would delete %d orphaned split children and "
                "re-activate %d superseded documents",
                orphan_count,
                superseded_count,
            )
            return

        # Step 1: Delete rulings attached to orphaned split children
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM rulings
                WHERE document_id IN (
                    SELECT d.id FROM documents d
                    JOIN courts ct ON d.court_id = ct.id
                    WHERE ct.county = 'Santa Clara'
                      AND d.created_at >= %s
                      AND substring(d.id::text, 15, 1) = '5'
                )
                """,
                (_CUTOFF_DATE,),
            )
            deleted_rulings = cur.rowcount
            logger.info(
                "Deleted %d rulings from orphaned split children", deleted_rulings
            )

        # Step 2: Delete orphaned split-child documents
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM documents
                WHERE id IN (
                    SELECT d.id FROM documents d
                    JOIN courts ct ON d.court_id = ct.id
                    WHERE ct.county = 'Santa Clara'
                      AND d.created_at >= %s
                      AND substring(d.id::text, 15, 1) = '5'
                )
                """,
                (_CUTOFF_DATE,),
            )
            deleted_docs = cur.rowcount
            logger.info("Deleted %d orphaned split-child documents", deleted_docs)

        # Step 3: Re-activate superseded parent documents
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents SET status = 'active'
                WHERE id IN (
                    SELECT d.id FROM documents d
                    JOIN courts ct ON d.court_id = ct.id
                    WHERE ct.county = 'Santa Clara' AND d.status = 'superseded'
                )
                """,
            )
            reactivated = cur.rowcount
            logger.info("Re-activated %d superseded documents", reactivated)

        conn.commit()

        # Step 4: Verify final state
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM documents d
                JOIN courts ct ON d.court_id = ct.id
                WHERE ct.county = 'Santa Clara' AND d.status = 'active'
                """,
            )
            active_docs_after = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*) FROM rulings r
                JOIN documents d ON r.document_id = d.id
                JOIN courts ct ON d.court_id = ct.id
                WHERE ct.county = 'Santa Clara'
                """,
            )
            rulings_after = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*) FROM documents d
                JOIN courts ct ON d.court_id = ct.id
                WHERE ct.county = 'Santa Clara' AND d.status = 'superseded'
                """,
            )
            superseded_after = cur.fetchone()[0]

        logger.info(
            "Cleanup complete",
            deleted_orphan_docs=deleted_docs,
            deleted_orphan_rulings=deleted_rulings,
            reactivated_docs=reactivated,
            active_docs_before=active_docs_before,
            active_docs_after=active_docs_after,
            rulings_before=rulings_before,
            rulings_after=rulings_after,
            superseded_remaining=superseded_after,
        )


if __name__ == "__main__":
    main()
