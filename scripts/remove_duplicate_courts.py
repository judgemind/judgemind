#!/usr/bin/env python3
"""Remove orphan court records created by rebuild_db.py's old court_code format.

Before #2373 fixed the root cause, rebuild_db.py and reingest_from_s3.py used
``{state}_{county}_{court}`` as the court_code (e.g. ``ca_santa_clara_superior_court``),
while the ingestion worker used ``{state}-{county}`` (e.g. ``ca-santa-clara``).  The
``ON CONFLICT (court_code)`` upsert never matched, creating duplicate court rows — one
per format — for every county.  The underscore-format rows have 0 documents and can be
safely deleted.

Usage:
    scripts/run-py.sh scripts/remove_duplicate_courts.py --dry-run
    scripts/run-py.sh scripts/remove_duplicate_courts.py

ECS (dev):
    scripts/ecs-run-task.sh scripts/remove_duplicate_courts.py -- --dry-run
    scripts/ecs-run-task.sh scripts/remove_duplicate_courts.py
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import sys

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove orphan court records with underscore-format court_codes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which records would be deleted without actually deleting them.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)

    # Find orphan courts: court_code contains underscores (old format) and has
    # no documents referencing it.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.court_code, c.court_name, c.county,
                   (SELECT COUNT(*) FROM documents d WHERE d.court_id = c.id) AS doc_count
            FROM courts c
            WHERE c.court_code LIKE '%%\\_%%'
            ORDER BY c.county
            """
        )
        orphans = cur.fetchall()

    if not orphans:
        print("No orphan court records found.")
        conn.close()
        return

    print(f"Found {len(orphans)} orphan court record(s):\n")
    safe_to_delete: list[str] = []
    for row in orphans:
        court_id, court_code, court_name, county, doc_count = row
        status = "SAFE" if doc_count == 0 else "HAS DOCUMENTS - SKIPPING"
        print(f"  {court_code:40s}  {county:20s}  docs={doc_count}  {status}")
        if doc_count == 0:
            safe_to_delete.append(str(court_id))

    if not safe_to_delete:
        print("\nNo orphan records are safe to delete (all have documents).")
        conn.close()
        return

    if args.dry_run:
        print(f"\n[DRY RUN] Would delete {len(safe_to_delete)} orphan court record(s).")
        # Also check scraper_runs referencing these orphans
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM scraper_runs
                WHERE court_id = ANY(%s::uuid[])
                """,
                (safe_to_delete,),
            )
            scraper_run_count = cur.fetchone()[0]
        if scraper_run_count:
            print(
                f"[DRY RUN] Would also cascade-delete {scraper_run_count} "
                f"scraper_runs referencing orphan courts."
            )
        conn.close()
        return

    # Delete scraper_runs first (no CASCADE on this FK)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM scraper_runs WHERE court_id = ANY(%s::uuid[])
            """,
            (safe_to_delete,),
        )
        scraper_runs_deleted = cur.rowcount
        print(
            f"\nDeleted {scraper_runs_deleted} scraper_runs referencing orphan courts."
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM courts WHERE id = ANY(%s::uuid[])
            """,
            (safe_to_delete,),
        )
        deleted = cur.rowcount

    conn.commit()
    print(f"Deleted {deleted} orphan court record(s).")
    conn.close()


if __name__ == "__main__":
    main()
