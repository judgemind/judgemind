#!/usr/bin/env python3
"""Backfill judges.department from the most recent ruling's department.

Sets each judge's department column to the department from their most recent
ruling (by hearing_date DESC). Only updates judges that have rulings with a
non-null department and whose current department column differs.

Usage:
    scripts/run-py.sh scripts/backfill_judge_department.py [--dry-run]
    scripts/ecs-run-task.sh scripts/backfill_judge_department.py [-- --dry-run]
"""
# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import sys

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill judges.department from rulings")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            # Find the most recent department per judge from rulings
            cur.execute("""
                SELECT DISTINCT ON (r.judge_id)
                    r.judge_id,
                    r.department,
                    j.department AS current_dept
                FROM derived.rulings r
                JOIN derived.judges j ON j.id = r.judge_id
                WHERE r.department IS NOT NULL
                ORDER BY r.judge_id, r.hearing_date DESC, r.id DESC
            """)

            updates: list[tuple[str, str, str | None]] = []
            for row in cur.fetchall():
                judge_id, new_dept, current_dept = row
                if new_dept != current_dept:
                    updates.append((judge_id, new_dept, current_dept))

            print(f"Found {len(updates)} judges needing department update")

            if args.dry_run:
                for judge_id, new_dept, current_dept in updates[:20]:
                    print(f"  {judge_id}: {current_dept!r} -> {new_dept!r}")
                if len(updates) > 20:
                    print(f"  ... and {len(updates) - 20} more")
                print("DRY RUN — no changes made")
                return

            if not updates:
                print("Nothing to update")
                return

            # Batch update
            for judge_id, new_dept, _ in updates:
                cur.execute(
                    "UPDATE derived.judges SET department = %s WHERE id = %s",
                    (new_dept, judge_id),
                )

            conn.commit()
            print(f"Updated {len(updates)} judges")


if __name__ == "__main__":
    main()
