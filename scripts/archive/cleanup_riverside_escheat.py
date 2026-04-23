#!/usr/bin/env python3
"""Clean up Riverside escheat documents and ruling from the database.

Investigation #1820 found two escheat-related documents incorrectly captured
by the Riverside scraper, plus one ruling record created from the escheat list
PDF.  This script:

1. Marks both escheat documents as ``status = 'removed'``.
2. Deletes the ruling created from the escheat list PDF.
3. Deletes the orphaned case record (if no other rulings reference it).

Usage (via ECS):
    scripts/ecs-run-task.sh scripts/cleanup_riverside_escheat.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_riverside_escheat.py
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

# ---------------------------------------------------------------------------
# IDs from issue #1846
# ---------------------------------------------------------------------------

ESCHEAT_DOCUMENT_IDS = [
    "283b7c12-fde3-5060-bf77-2dd9a4bd2aa7",  # Court's Website Escheat List.pdf
    "b5c2bd49-1357-5d41-ad59-2751a4ef5f4c",  # 2026 OT Escheat Claim Form.pdf
]

RULING_ID = "98230c56-05c5-460c-85d6-a56bf52992da"
CASE_ID = "5db8e043-8c59-49f8-a365-c74d94a51f1a"


def run_cleanup(dsn: str, *, dry_run: bool = False) -> None:
    """Execute the cleanup operations."""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # ----------------------------------------------------------
            # Step 1: Mark escheat documents as removed
            # ----------------------------------------------------------
            for doc_id in ESCHEAT_DOCUMENT_IDS:
                cur.execute(
                    "SELECT id, status, source_url FROM documents WHERE id = %s::uuid",
                    (doc_id,),
                )
                row = cur.fetchone()
                if row is None:
                    logger.warning("Document %s not found — skipping", doc_id)
                    continue

                current_status = row[1]
                source_url = row[2]
                logger.info(
                    "Document %s (source_url=%s) current status=%s",
                    doc_id,
                    source_url,
                    current_status,
                )

                if current_status == "removed":
                    logger.info("  Already removed — skipping")
                    continue

                cur.execute(
                    "UPDATE documents SET status = 'removed' WHERE id = %s::uuid",
                    (doc_id,),
                )
                logger.info("  Marked as removed (rows updated: %d)", cur.rowcount)

            # ----------------------------------------------------------
            # Step 2: Delete the ruling
            # ----------------------------------------------------------
            cur.execute(
                "SELECT id, case_id, document_id FROM rulings WHERE id = %s::uuid",
                (RULING_ID,),
            )
            ruling_row = cur.fetchone()
            if ruling_row is None:
                logger.warning("Ruling %s not found — skipping", RULING_ID)
            else:
                logger.info(
                    "Ruling %s found (case_id=%s, document_id=%s)",
                    RULING_ID,
                    ruling_row[1],
                    ruling_row[2],
                )
                cur.execute(
                    "DELETE FROM rulings WHERE id = %s::uuid",
                    (RULING_ID,),
                )
                logger.info("  Deleted ruling (rows deleted: %d)", cur.rowcount)

            # ----------------------------------------------------------
            # Step 3: Delete orphaned case if no other rulings reference it
            # ----------------------------------------------------------
            cur.execute(
                "SELECT COUNT(*) FROM rulings WHERE case_id = %s::uuid",
                (CASE_ID,),
            )
            remaining_rulings = cur.fetchone()[0]
            logger.info(
                "Case %s has %d remaining rulings after cleanup",
                CASE_ID,
                remaining_rulings,
            )

            if remaining_rulings == 0:
                # Also clean up case_judges and case_parties (they cascade on
                # delete in the schema, but delete explicitly for clarity in logs)
                cur.execute(
                    "DELETE FROM case_judges WHERE case_id = %s::uuid",
                    (CASE_ID,),
                )
                judges_deleted = cur.rowcount
                logger.info("  Deleted case_judges rows: %d", judges_deleted)

                cur.execute(
                    "DELETE FROM case_parties WHERE case_id = %s::uuid",
                    (CASE_ID,),
                )
                parties_deleted = cur.rowcount
                logger.info("  Deleted case_parties rows: %d", parties_deleted)

                # Unlink documents from the case (set case_id to NULL rather
                # than deleting, since the documents table row is the index to
                # an S3 object — we already marked the escheat docs 'removed')
                cur.execute(
                    "UPDATE documents SET case_id = NULL WHERE case_id = %s::uuid",
                    (CASE_ID,),
                )
                docs_unlinked = cur.rowcount
                logger.info("  Unlinked removed documents from case: %d", docs_unlinked)

                cur.execute(
                    "DELETE FROM cases WHERE id = %s::uuid",
                    (CASE_ID,),
                )
                logger.info("  Deleted orphaned case (rows deleted: %d)", cur.rowcount)
            else:
                logger.info("  Case has other rulings — not deleting")

        # ----------------------------------------------------------
        # Commit or rollback
        # ----------------------------------------------------------
        if dry_run:
            conn.rollback()
            logger.info("DRY RUN — all changes rolled back")
        else:
            conn.commit()
            logger.info("All changes committed")

        # ----------------------------------------------------------
        # Verification queries
        # ----------------------------------------------------------
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM documents WHERE id IN (%s::uuid, %s::uuid)",
                tuple(ESCHEAT_DOCUMENT_IDS),
            )
            for row in cur.fetchall():
                logger.info("Verify document %s: status=%s", row[0], row[1])

            cur.execute(
                "SELECT COUNT(*) FROM rulings WHERE id = %s::uuid",
                (RULING_ID,),
            )
            ruling_count = cur.fetchone()[0]
            logger.info("Verify ruling %s exists: %s", RULING_ID, ruling_count > 0)

            cur.execute(
                "SELECT COUNT(*) FROM cases WHERE id = %s::uuid",
                (CASE_ID,),
            )
            case_count = cur.fetchone()[0]
            logger.info("Verify case %s exists: %s", CASE_ID, case_count > 0)


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Clean up Riverside escheat documents and ruling (#1846).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing to the database.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    run_cleanup(dsn, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
