#!/usr/bin/env python3
# venv: scraper-framework
"""Deduplicate judge records that share the same canonical_name + court_id (case-insensitive).

When the same judge name was ingested with different casing (e.g.,
"John Smith" vs "JOHN SMITH"), separate judge records may have been
created.  This script merges them: for each group of duplicates, one
"winner" judge is kept and all references (case_judges, judge_aliases) are
re-pointed to the winner.  Duplicate case_judges rows (same case_id
pointing to different judge_ids with the same canonical_name) are collapsed.

Usage via ECS (preferred — dev DB is in a private VPC):
    scripts/ecs-run-task.sh scripts/dedup_judges.py -- --dry-run
    scripts/ecs-run-task.sh scripts/dedup_judges.py

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
    """Find groups of judge records that share the same canonical_name + court_id (case-insensitive).

    Returns a list of dicts with keys: canonical_lower, court_id, judge_ids, count.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT LOWER(canonical_name) AS cn_lower,
                   court_id,
                   ARRAY_AGG(id ORDER BY created_at) AS judge_ids,
                   COUNT(*) AS cnt
            FROM judges
            GROUP BY LOWER(canonical_name), court_id
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC
        """)
        rows = cur.fetchall()

    groups = []
    for row in rows:
        groups.append(
            {
                "canonical_lower": row[0],
                "court_id": str(row[1]),
                "judge_ids": [str(jid) for jid in row[2]],
                "count": row[3],
            }
        )
    return groups


def merge_group(
    conn: psycopg.Connection,
    group: dict,
    dry_run: bool = False,
) -> dict:
    """Merge a group of duplicate judge records into the oldest (winner).

    Returns stats: aliases_moved, case_judges_moved, case_judges_deduped,
    judges_deleted.
    """
    judge_ids = group["judge_ids"]
    winner_id = judge_ids[0]  # oldest by created_at
    loser_ids = judge_ids[1:]
    stats: dict[str, int] = {
        "aliases_moved": 0,
        "case_judges_moved": 0,
        "case_judges_deduped": 0,
        "judges_deleted": 0,
    }

    if dry_run:
        logger.info(
            "DRY RUN: Would merge %d judges for %r (court %s) -> winner %s",
            len(judge_ids),
            group["canonical_lower"],
            group["court_id"],
            winner_id,
        )
        return stats

    with conn.cursor() as cur:
        for loser_id in loser_ids:
            # Move judge_aliases from loser to winner (skip conflicts,
            # case-insensitive to avoid leaving near-duplicate aliases)
            cur.execute(
                """
                UPDATE judge_aliases
                SET judge_id = %s::uuid
                WHERE judge_id = %s::uuid
                  AND LOWER(raw_name) NOT IN (
                      SELECT LOWER(raw_name) FROM judge_aliases WHERE judge_id = %s::uuid
                  )
                """,
                (winner_id, loser_id, winner_id),
            )
            stats["aliases_moved"] += cur.rowcount

            # Delete remaining aliases for loser (duplicates of winner's)
            cur.execute(
                "DELETE FROM judge_aliases WHERE judge_id = %s::uuid",
                (loser_id,),
            )

            # Move case_judges from loser to winner (skip conflicts via
            # the unique constraint on case_id, judge_id)
            cur.execute(
                """
                UPDATE case_judges
                SET judge_id = %s::uuid
                WHERE judge_id = %s::uuid
                  AND NOT EXISTS (
                      SELECT 1 FROM case_judges cj2
                      WHERE cj2.case_id = case_judges.case_id
                        AND cj2.judge_id = %s::uuid
                  )
                """,
                (winner_id, loser_id, winner_id),
            )
            stats["case_judges_moved"] += cur.rowcount

            # Delete remaining case_judges for loser (duplicates of winner's links)
            cur.execute(
                "DELETE FROM case_judges WHERE judge_id = %s::uuid",
                (loser_id,),
            )
            stats["case_judges_deduped"] += cur.rowcount

            # Delete the loser judge record
            cur.execute(
                "DELETE FROM judges WHERE id = %s::uuid",
                (loser_id,),
            )
            stats["judges_deleted"] += cur.rowcount

    conn.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate judge records.")
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
        logger.info("Found %d groups of duplicate judges", len(groups))

        if not groups:
            logger.info("No duplicates found — nothing to do.")
            return

        total_stats: dict[str, int] = {
            "aliases_moved": 0,
            "case_judges_moved": 0,
            "case_judges_deduped": 0,
            "judges_deleted": 0,
        }

        for group in groups:
            stats = merge_group(conn, group, dry_run=args.dry_run)
            for key in total_stats:
                total_stats[key] += stats[key]

        prefix = "DRY RUN: Would have" if args.dry_run else "Done."
        logger.info(
            "%s merged %d duplicate groups: "
            "%d aliases moved, %d case_judges moved, "
            "%d case_judges deduped, %d judges deleted",
            prefix,
            len(groups),
            total_stats["aliases_moved"],
            total_stats["case_judges_moved"],
            total_stats["case_judges_deduped"],
            total_stats["judges_deleted"],
        )

        # Verify: check for remaining duplicate judge records
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT LOWER(canonical_name), court_id
                        FROM judges
                        GROUP BY LOWER(canonical_name), court_id
                        HAVING COUNT(*) > 1
                    ) dupes
                """)
                row = cur.fetchone()
                remaining = row[0] if row else 0
                if remaining > 0:
                    logger.warning(
                        "WARNING: %d duplicate judge groups remain",
                        remaining,
                    )
                else:
                    logger.info("Verification: 0 duplicate judge groups remain")


if __name__ == "__main__":
    main()
