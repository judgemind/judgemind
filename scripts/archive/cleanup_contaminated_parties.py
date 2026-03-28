#!/usr/bin/env python3
# venv: scraper-framework
"""Clean up contaminated party records that contain ruling text or court headers.

Identifies party records whose canonical_name matches known contamination
patterns (court headers, ruling text, motion descriptions) and removes them
along with their aliases and case-party links.

Usage (via ECS):
    scripts/ecs-run-task.sh scripts/cleanup_contaminated_parties.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_contaminated_parties.py

Options:
    --dry-run   Print what would be deleted without modifying the database.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Contamination patterns — must stay in sync with
# framework.party_utils.is_contaminated_party_name().  Inlined here because
# ECS oneshot scripts cannot import local modules (see CLAUDE.md).
_CONTAMINATION_SQL_PATTERNS = [
    # Court header patterns
    "%Law And Motion Ruling%",
    "%Superior Court Of%",
    "%Hearing Date%:%",
    "%Hearing Time%:%",
    "%Case Number%:%",
    "%Case No%:%",
    # Ruling/legal text patterns
    "%Tentative%Ruling%",
    "%Before The Court%",
    "%Decl. Ex.%",
    "%The Same Section Further States%",
    "%Ruling Re%:%",
    "%Italics Added%",
    "%Emphasis Added%",
    "%The Court Finds%",
    "%The Court Orders%",
    "%The Court Rules%",
    "%The Court Notes%",
    "%The Court Grants%",
    "%The Court Denies%",
    # Motion-only patterns (motion description as entire party name).
    # Need start-of-string, mid-string, and end-of-string variants because
    # "Motion For" can appear anywhere (e.g. "Granting Motion For").
    "Motion For %",
    "Motion To %",
    "Motion Of %",
    "Motion Re %",
    "% Motion For %",
    "% Motion To %",
    "% Motion Of %",
    "% Motion Re %",
    "% Motion For",
    "% Motion To",
    "% Motion Of",
    "% Motion Re",
    # Docket metadata patterns (filing dates, complaint types leaked into
    # party names, e.g. "Et Al. Comp. Filed : 03-27-25")
    "%Comp. Filed%",
    "%Filed : __-__-__%",
    "%Filed: __-__-__%",
]


def build_contamination_where() -> str:
    """Build a SQL WHERE clause matching contaminated party names."""
    clauses = []
    for pattern in _CONTAMINATION_SQL_PATTERNS:
        clauses.append(f"canonical_name ILIKE '{pattern}'")
    # Also catch very long names (> 150 chars) that are likely contaminated
    clauses.append("LENGTH(canonical_name) > 150")
    return " OR ".join(clauses)


def main() -> None:
    """Run the cleanup."""
    parser = argparse.ArgumentParser(description="Clean up contaminated party records")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print without modifying"
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    import psycopg

    conn = psycopg.connect(database_url, autocommit=False)

    where_clause = build_contamination_where()

    try:
        with conn.cursor() as cur:
            # Find contaminated parties
            cur.execute(
                f"SELECT id, canonical_name FROM parties WHERE {where_clause}"  # noqa: S608
            )
            contaminated = cur.fetchall()

            if not contaminated:
                logger.info("No contaminated party records found.")
                return

            logger.info("Found %d contaminated party records.", len(contaminated))
            for party_id, name in contaminated:
                logger.info("  [%s] %s", party_id, name[:80])

            if args.dry_run:
                logger.info("Dry run — no changes made.")
                return

            party_ids = [row[0] for row in contaminated]

            # Delete case_parties links
            cur.execute(
                "DELETE FROM case_parties WHERE party_id = ANY(%s::uuid[])",
                (party_ids,),
            )
            links_deleted = cur.rowcount
            logger.info("Deleted %d case_parties links.", links_deleted)

            # Delete party_aliases
            cur.execute(
                "DELETE FROM party_aliases WHERE party_id = ANY(%s::uuid[])",
                (party_ids,),
            )
            aliases_deleted = cur.rowcount
            logger.info("Deleted %d party_aliases.", aliases_deleted)

            # Delete parties
            cur.execute(
                "DELETE FROM parties WHERE id = ANY(%s::uuid[])",
                (party_ids,),
            )
            parties_deleted = cur.rowcount
            logger.info("Deleted %d party records.", parties_deleted)

            conn.commit()
            logger.info("Cleanup complete.")

    except Exception:
        conn.rollback()
        logger.exception("Error during cleanup — rolled back.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
