#!/usr/bin/env python3
"""Clean up malformed judge name entries in the judges table.

Finds and fixes the following data quality issues (discovered in #572):

1. **Garbage entries:** Unicode junk, paragraph text captured as names,
   names exceeding 80 characters.
2. **Last-name-only entries:** Single-word names without a first name.
3. **Honorific not stripped:** Names still prefixed with "Hon.", "Judge", etc.
4. **Suffix parsing bugs:** "Jr. Edward B. Moreton" -> "Edward B. Moreton Jr."
5. **Arbitrator prefix:** "Arbitrator Howard B. Miller" -> "Howard B. Miller"
6. **Duplicate entries:** Multiple records for the same normalized name + court.

Actions taken:
- Re-normalizes all canonical_name values through the updated normalize_judge_name().
- Deletes entries where normalize returns None (garbage/invalid).
- Merges duplicates: re-links rulings, case_judges, and judge_aliases to the
  kept record, then deletes the duplicate.
- Deletes orphan entries with 0 rulings and invalid names.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/cleanup_judge_names.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/cleanup_judge_names.py --dry-run

Options:
    --dry-run       Print what would be changed without writing to the database.
    --court CODE    Only process judges at a specific court_code (e.g. "ca-los-angeles").
    --limit N       Maximum total judges to process (default: unlimited).

The script is idempotent — re-running it will find no issues if all names
have already been cleaned up.

Parent issue: #572
Closes: #589
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "packages",
        "scraper-framework",
        "src",
    ),
)

import psycopg  # noqa: E402

from ingestion.db import (  # noqa: E402
    _looks_like_valid_judge_name,
    normalize_judge_name,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean up malformed judge name entries."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing to the database.",
    )
    parser.add_argument(
        "--court",
        type=str,
        default=None,
        help="Only process judges at a specific court_code.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total judges to process.",
    )
    return parser.parse_args()


def _fetch_judges(
    conn: psycopg.Connection,
    court_code: str | None,
    limit: int | None,
) -> list[dict[str, str | int]]:
    """Fetch all judges with their ruling counts."""
    sql = """
        SELECT j.id, j.canonical_name, j.court_id, c.court_code,
               COUNT(r.id) AS ruling_count
        FROM judges j
        JOIN courts c ON c.id = j.court_id
        LEFT JOIN rulings r ON r.judge_id = j.id
    """
    params: list[str | int] = []
    if court_code:
        sql += " WHERE c.court_code = %s"
        params.append(court_code)
    sql += " GROUP BY j.id, j.canonical_name, j.court_id, c.court_code"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {
            "id": str(row[0]),
            "canonical_name": row[1],
            "court_id": str(row[2]),
            "court_code": row[3],
            "ruling_count": row[4],
        }
        for row in rows
    ]


def _merge_judge(
    conn: psycopg.Connection,
    from_id: str,
    to_id: str,
    dry_run: bool,
) -> None:
    """Merge judge from_id into to_id: re-link rulings, aliases, case_judges."""
    if dry_run:
        logger.info("  [DRY RUN] Would merge judge %s -> %s", from_id, to_id)
        return

    with conn.cursor() as cur:
        # Re-link rulings
        cur.execute(
            "UPDATE rulings SET judge_id = %s WHERE judge_id = %s",
            (to_id, from_id),
        )
        rulings_updated = cur.rowcount

        # Re-link case_judges (skip conflicts)
        cur.execute(
            """
            UPDATE case_judges SET judge_id = %s
            WHERE judge_id = %s
            AND NOT EXISTS (
                SELECT 1 FROM case_judges cj2
                WHERE cj2.case_id = case_judges.case_id AND cj2.judge_id = %s
            )
            """,
            (to_id, from_id, to_id),
        )

        # Delete remaining case_judges for old judge (conflicts)
        cur.execute("DELETE FROM case_judges WHERE judge_id = %s", (from_id,))

        # Re-link judge_aliases (skip conflicts)
        cur.execute(
            """
            UPDATE judge_aliases SET judge_id = %s
            WHERE judge_id = %s
            AND NOT EXISTS (
                SELECT 1 FROM judge_aliases ja2
                WHERE ja2.raw_name = judge_aliases.raw_name AND ja2.judge_id = %s
            )
            """,
            (to_id, from_id, to_id),
        )

        # Delete remaining aliases for old judge (conflicts)
        cur.execute("DELETE FROM judge_aliases WHERE judge_id = %s", (from_id,))

        # Delete the old judge record
        cur.execute("DELETE FROM judges WHERE id = %s", (from_id,))

    logger.info(
        "  Merged judge %s -> %s (%d rulings re-linked)",
        from_id,
        to_id,
        rulings_updated,
    )


def _delete_judge(
    conn: psycopg.Connection,
    judge_id: str,
    dry_run: bool,
) -> None:
    """Delete a judge record and its aliases (must have 0 rulings)."""
    if dry_run:
        logger.info("  [DRY RUN] Would delete judge %s", judge_id)
        return

    with conn.cursor() as cur:
        cur.execute("DELETE FROM case_judges WHERE judge_id = %s", (judge_id,))
        cur.execute("DELETE FROM judge_aliases WHERE judge_id = %s", (judge_id,))
        cur.execute("DELETE FROM judges WHERE id = %s", (judge_id,))

    logger.info("  Deleted judge %s", judge_id)


def _update_canonical_name(
    conn: psycopg.Connection,
    judge_id: str,
    new_name: str,
    dry_run: bool,
) -> None:
    """Update a judge's canonical_name."""
    if dry_run:
        logger.info("  [DRY RUN] Would update judge %s canonical_name to %r", judge_id, new_name)
        return

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE judges SET canonical_name = %s WHERE id = %s",
            (new_name, judge_id),
        )

    logger.info("  Updated judge %s canonical_name to %r", judge_id, new_name)


def run(args: argparse.Namespace) -> None:
    """Main cleanup logic."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)

    try:
        judges = _fetch_judges(conn, args.court, args.limit)
        logger.info("Found %d judge records to process", len(judges))

        stats = {
            "deleted_garbage": 0,
            "deleted_single_word": 0,
            "renamed": 0,
            "merged": 0,
            "unchanged": 0,
        }

        # Build a map of (court_id, normalized_name) -> list of judge records
        # for duplicate detection
        name_groups: dict[tuple[str, str], list[dict[str, str | int]]] = {}

        for judge in judges:
            old_name = judge["canonical_name"]
            assert isinstance(old_name, str)
            new_name = normalize_judge_name(old_name)

            # Case 1: Garbage name -> delete if 0 rulings, else flag
            if new_name is None:
                ruling_count = judge["ruling_count"]
                assert isinstance(ruling_count, int)
                if ruling_count == 0:
                    logger.info(
                        "Garbage name with 0 rulings: %r (court=%s)",
                        old_name,
                        judge["court_code"],
                    )
                    assert isinstance(judge["id"], str)
                    _delete_judge(conn, judge["id"], args.dry_run)
                    stats["deleted_garbage"] += 1
                else:
                    logger.warning(
                        "Garbage name with %d rulings (needs manual review): %r (judge_id=%s)",
                        ruling_count,
                        old_name,
                        judge["id"],
                    )
                continue

            # Case 2: Single-word name -> delete if 0 rulings
            assert isinstance(new_name, str)
            if not _looks_like_valid_judge_name(new_name):
                ruling_count = judge["ruling_count"]
                assert isinstance(ruling_count, int)
                if ruling_count == 0:
                    logger.info(
                        "Single-word name with 0 rulings: %r (court=%s)",
                        old_name,
                        judge["court_code"],
                    )
                    assert isinstance(judge["id"], str)
                    _delete_judge(conn, judge["id"], args.dry_run)
                    stats["deleted_single_word"] += 1
                else:
                    logger.warning(
                        "Single-word name with %d rulings (needs manual review): %r (judge_id=%s)",
                        ruling_count,
                        old_name,
                        judge["id"],
                    )
                continue

            # Case 3: Name changed after re-normalization -> update
            if new_name != old_name:
                logger.info(
                    "Name needs update: %r -> %r (court=%s)",
                    old_name,
                    new_name,
                    judge["court_code"],
                )
                assert isinstance(judge["id"], str)
                _update_canonical_name(conn, judge["id"], new_name, args.dry_run)
                stats["renamed"] += 1

            # Track for duplicate detection (using new normalized name)
            court_id = judge["court_id"]
            assert isinstance(court_id, str)
            key = (court_id, new_name)
            if key not in name_groups:
                name_groups[key] = []
            name_groups[key].append(judge)

        # Case 4: Merge duplicates (same normalized name + court_id)
        for (court_id, name), group in name_groups.items():
            if len(group) < 2:
                stats["unchanged"] += 1
                continue

            # Keep the judge with the most rulings
            group.sort(key=lambda j: j["ruling_count"], reverse=True)
            keep = group[0]
            for dup in group[1:]:
                logger.info(
                    "Duplicate: %r at court %s — merging %s (rulings=%d) into %s (rulings=%d)",
                    name,
                    court_id,
                    dup["id"],
                    dup["ruling_count"],
                    keep["id"],
                    keep["ruling_count"],
                )
                assert isinstance(dup["id"], str)
                assert isinstance(keep["id"], str)
                _merge_judge(conn, dup["id"], keep["id"], args.dry_run)
                stats["merged"] += 1

        if not args.dry_run:
            conn.commit()
            logger.info("Changes committed.")
        else:
            conn.rollback()
            logger.info("[DRY RUN] No changes written.")

        logger.info("Summary:")
        logger.info("  Deleted (garbage):     %d", stats["deleted_garbage"])
        logger.info("  Deleted (single-word): %d", stats["deleted_single_word"])
        logger.info("  Renamed:               %d", stats["renamed"])
        logger.info("  Merged duplicates:     %d", stats["merged"])
        logger.info("  Unchanged:             %d", stats["unchanged"])

    finally:
        conn.close()


if __name__ == "__main__":
    run(_parse_args())
