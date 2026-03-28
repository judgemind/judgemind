#!/usr/bin/env python3
# venv: scraper-framework
"""Deduplicate party records that share the same canonical_name (case-insensitive).

When the same party name was ingested with different casing (e.g.,
"Citizens Business Bank" vs "CITIZENS BUSINESS BANK"), separate party records
were created.  This script merges them: for each group of duplicates, one
"winner" party is kept and all references (case_parties, party_aliases) are
re-pointed to the winner.  Duplicate case_parties rows (same case_id + role
pointing to different party_ids with the same canonical_name) are collapsed.

Usage via ECS (preferred — dev DB is in a private VPC):
    scripts/ecs-run-task.sh scripts/dedup_parties.py -- --dry-run
    scripts/ecs-run-task.sh scripts/dedup_parties.py

Options:
    --dry-run   Report duplicates without modifying the database.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def find_duplicate_groups(
    conn: psycopg.Connection,
) -> list[dict]:
    """Find groups of party records that share the same canonical_name (case-insensitive).

    Returns a list of dicts with keys: canonical_lower, party_ids, counts.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT LOWER(canonical_name) AS cn_lower,
                   ARRAY_AGG(id ORDER BY created_at) AS party_ids,
                   COUNT(*) AS cnt
            FROM parties
            GROUP BY LOWER(canonical_name)
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC
        """)
        rows = cur.fetchall()

    groups = []
    for row in rows:
        groups.append(
            {
                "canonical_lower": row[0],
                "party_ids": [str(pid) for pid in row[1]],
                "count": row[2],
            }
        )
    return groups


def merge_group(
    conn: psycopg.Connection,
    group: dict,
    dry_run: bool = False,
) -> dict:
    """Merge a group of duplicate party records into the oldest (winner).

    Returns stats: aliases_moved, case_parties_moved, case_parties_deduped,
    parties_deleted.
    """
    party_ids = group["party_ids"]
    winner_id = party_ids[0]  # oldest by created_at
    loser_ids = party_ids[1:]
    stats = {
        "aliases_moved": 0,
        "case_parties_moved": 0,
        "case_parties_deduped": 0,
        "parties_deleted": 0,
    }

    if dry_run:
        logger.info(
            "DRY RUN: Would merge %d parties for %r -> winner %s",
            len(party_ids),
            group["canonical_lower"],
            winner_id,
        )
        return stats

    with conn.cursor() as cur:
        for loser_id in loser_ids:
            # Move party_aliases from loser to winner (skip conflicts)
            cur.execute(
                """
                UPDATE party_aliases
                SET party_id = %s::uuid
                WHERE party_id = %s::uuid
                  AND raw_name NOT IN (
                      SELECT raw_name FROM party_aliases WHERE party_id = %s::uuid
                  )
                """,
                (winner_id, loser_id, winner_id),
            )
            stats["aliases_moved"] += cur.rowcount

            # Delete remaining aliases for loser (duplicates of winner's)
            cur.execute(
                "DELETE FROM party_aliases WHERE party_id = %s::uuid",
                (loser_id,),
            )

            # Move case_parties from loser to winner (skip conflicts via
            # the unique constraint on case_id, party_id, role)
            cur.execute(
                """
                UPDATE case_parties
                SET party_id = %s::uuid
                WHERE party_id = %s::uuid
                  AND NOT EXISTS (
                      SELECT 1 FROM case_parties cp2
                      WHERE cp2.case_id = case_parties.case_id
                        AND cp2.party_id = %s::uuid
                        AND cp2.role = case_parties.role
                  )
                """,
                (winner_id, loser_id, winner_id),
            )
            stats["case_parties_moved"] += cur.rowcount

            # Delete remaining case_parties for loser (duplicates of winner's links)
            cur.execute(
                "DELETE FROM case_parties WHERE party_id = %s::uuid",
                (loser_id,),
            )
            stats["case_parties_deduped"] += cur.rowcount

            # Delete the loser party record
            cur.execute(
                "DELETE FROM parties WHERE id = %s::uuid",
                (loser_id,),
            )
            stats["parties_deleted"] += cur.rowcount

    conn.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate party records.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report duplicates without modifying the database.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is not set")
        sys.exit(1)

    with psycopg.connect(db_url, autocommit=False) as conn:
        groups = find_duplicate_groups(conn)
        logger.info("Found %d groups of duplicate parties", len(groups))

        if not groups:
            logger.info("No duplicates found — nothing to do.")
            return

        total_stats = {
            "aliases_moved": 0,
            "case_parties_moved": 0,
            "case_parties_deduped": 0,
            "parties_deleted": 0,
        }

        for group in groups:
            stats = merge_group(conn, group, dry_run=args.dry_run)
            for key in total_stats:
                total_stats[key] += stats[key]

        prefix = "DRY RUN: Would have" if args.dry_run else "Done."
        logger.info(
            "%s merged %d duplicate groups: "
            "%d aliases moved, %d case_parties moved, "
            "%d case_parties deduped, %d parties deleted",
            prefix,
            len(groups),
            total_stats["aliases_moved"],
            total_stats["case_parties_moved"],
            total_stats["case_parties_deduped"],
            total_stats["parties_deleted"],
        )

        # Verify: check for remaining duplicates in case_parties
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT case_id, party_id, role
                        FROM case_parties
                        GROUP BY case_id, party_id, role
                        HAVING COUNT(*) > 1
                    ) dupes
                """)
                row = cur.fetchone()
                remaining = row[0] if row else 0
                if remaining > 0:
                    logger.warning(
                        "WARNING: %d duplicate case_parties groups remain",
                        remaining,
                    )
                else:
                    logger.info("Verification: 0 duplicate case_parties groups remain")


if __name__ == "__main__":
    main()
