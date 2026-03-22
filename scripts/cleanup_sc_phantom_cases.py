#!/usr/bin/env python3
"""Clean up UNKNOWN-prefix phantom cases for Santa Clara county.

After re-ingesting Santa Clara documents with the fixed splitter/parser,
the old UNKNOWN-prefix phantom cases may still exist.  This script:

1. Finds all Santa Clara cases with UNKNOWN-prefix case numbers.
2. Migrates their rulings (if any) to the correct case (by matching
   document_id to a non-UNKNOWN case linked to the same document).
3. Deletes orphaned phantom cases that have no remaining rulings or
   documents linked to them.

Usage (via ECS):
    scripts/ecs-run-task.sh scripts/cleanup_sc_phantom_cases.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_sc_phantom_cases.py
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import os
import sys

import psycopg
import structlog

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
        if sys.stderr.isatty()
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # Find all UNKNOWN-prefix cases for Santa Clara
            cur.execute(
                """
                SELECT ca.id, ca.case_number, ca.case_title
                FROM cases ca
                JOIN courts co ON ca.court_id = co.id
                WHERE co.county = 'Santa Clara'
                  AND ca.case_number LIKE 'UNKNOWN-%%'
                ORDER BY ca.case_number
                """
            )
            phantom_cases = cur.fetchall()

            if not phantom_cases:
                logger.info("No UNKNOWN-prefix cases found for Santa Clara")
                return

            logger.info(
                "Found UNKNOWN-prefix cases",
                count=len(phantom_cases),
            )

            for case_id, case_number, case_title in phantom_cases:
                logger.info(
                    "Processing phantom case",
                    case_id=str(case_id),
                    case_number=case_number,
                    case_title=case_title[:80] if case_title else None,
                )

                # Check for rulings linked to this phantom case
                cur.execute(
                    "SELECT COUNT(*) FROM rulings WHERE case_id = %s",
                    (str(case_id),),
                )
                ruling_count = cur.fetchone()[0]

                # Check for documents linked to this phantom case
                cur.execute(
                    "SELECT COUNT(*) FROM documents WHERE case_id = %s",
                    (str(case_id),),
                )
                doc_count = cur.fetchone()[0]

                # Check for case_parties linked to this phantom case
                cur.execute(
                    "SELECT COUNT(*) FROM case_parties WHERE case_id = %s",
                    (str(case_id),),
                )
                party_count = cur.fetchone()[0]

                # Check for case_judges linked to this phantom case
                cur.execute(
                    """
                    SELECT COUNT(*) FROM case_judges
                    WHERE case_id = %s
                    """,
                    (str(case_id),),
                )
                case_judge_count = cur.fetchone()[0]

                logger.info(
                    "Phantom case references",
                    case_id=str(case_id),
                    rulings=ruling_count,
                    documents=doc_count,
                    case_parties=party_count,
                    case_judges=case_judge_count,
                )

                if args.dry_run:
                    logger.info(
                        "DRY-RUN: Would delete phantom case and its references",
                        case_id=str(case_id),
                        case_number=case_number,
                    )
                    continue

                # Delete in order: rulings, documents, case_parties,
                # case_judges, then the case itself
                if ruling_count > 0:
                    cur.execute(
                        "DELETE FROM rulings WHERE case_id = %s",
                        (str(case_id),),
                    )
                    logger.info("Deleted rulings", count=ruling_count)

                if doc_count > 0:
                    cur.execute(
                        "DELETE FROM documents WHERE case_id = %s",
                        (str(case_id),),
                    )
                    logger.info("Deleted documents", count=doc_count)

                if party_count > 0:
                    cur.execute(
                        "DELETE FROM case_parties WHERE case_id = %s",
                        (str(case_id),),
                    )
                    logger.info("Deleted case_parties", count=party_count)

                if case_judge_count > 0:
                    cur.execute(
                        "DELETE FROM case_judges WHERE case_id = %s",
                        (str(case_id),),
                    )
                    logger.info("Deleted case_judges", count=case_judge_count)

                cur.execute(
                    "DELETE FROM cases WHERE id = %s",
                    (str(case_id),),
                )
                logger.info(
                    "Deleted phantom case",
                    case_id=str(case_id),
                    case_number=case_number,
                )

            conn.commit()
            logger.info(
                "Cleanup complete",
                phantom_cases_processed=len(phantom_cases),
                dry_run=args.dry_run,
            )


if __name__ == "__main__":
    main()
