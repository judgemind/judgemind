#!/usr/bin/env python3
"""Delete orphaned document records that have no ruling and a sibling with a ruling.

When the ingestion pipeline splits a multi-case PDF into N rulings, each split
gets a unique document_id derived from (original_doc_id, split_index).  If the
same PDF is re-processed and the LLM extracts a different number of rulings,
new document records are created with different split indices while the old ones
remain — leaving orphaned documents with no ruling reference.

This script identifies and deletes those orphans.  A document is only eligible
for deletion when:
  1. It has NO entry in the rulings table (no ruling reference).
  2. At least one other document with the SAME s3_key DOES have a ruling.
     This ensures we never delete the sole record for an S3 object.

Usage (local):
  DATABASE_URL=postgres://judgemind:localdev@localhost:5432/judgemind \
  scripts/run-py.sh scripts/cleanup_orphaned_documents.py --dry-run

Usage (ECS):
  scripts/ecs-run-task.sh scripts/cleanup_orphaned_documents.py -- --dry-run

Options:
  --county NAME    Only clean up orphans for a specific county (default: all)
  --dry-run        Show what would be deleted without deleting
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import sys

import psycopg


def find_orphans(
    conn: psycopg.Connection,
    county: str | None = None,
) -> list[dict[str, str]]:
    """Find orphaned document records eligible for deletion.

    Returns a list of dicts with keys: id, s3_key, county, created_at.
    """
    county_filter = ""
    params: list[str] = []
    if county:
        county_filter = "AND ct.county = %s"
        params.append(county)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH orphans AS (
                SELECT d.id, d.s3_key, ct.county, d.created_at
                FROM documents d
                JOIN courts ct ON d.court_id = ct.id
                LEFT JOIN rulings r ON r.document_id = d.id
                WHERE r.id IS NULL
                {county_filter}
            ),
            s3_with_ruling AS (
                SELECT DISTINCT d.s3_key
                FROM documents d
                JOIN rulings r ON r.document_id = d.id
            )
            SELECT o.id, o.s3_key, o.county, o.created_at::text
            FROM orphans o
            JOIN s3_with_ruling s ON o.s3_key = s.s3_key
            ORDER BY o.county, o.created_at
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "s3_key": row[1],
            "county": row[2],
            "created_at": row[3],
        }
        for row in rows
    ]


def delete_orphans(
    conn: psycopg.Connection,
    orphan_ids: list[str],
) -> int:
    """Delete orphaned document records by ID. Returns count deleted."""
    if not orphan_ids:
        return 0

    with conn.cursor() as cur:
        # Delete any references in dependent tables first.
        # alert_events references document_id.
        cur.execute(
            "DELETE FROM alert_events WHERE document_id = ANY(%s::uuid[])",
            (orphan_ids,),
        )
        alerts_deleted = cur.rowcount

        # validation_results references document_id.
        cur.execute(
            "DELETE FROM validation_results WHERE document_id = ANY(%s::uuid[])",
            (orphan_ids,),
        )
        validation_deleted = cur.rowcount

        # Now delete the document records themselves.
        cur.execute(
            "DELETE FROM documents WHERE id = ANY(%s::uuid[])",
            (orphan_ids,),
        )
        doc_deleted = cur.rowcount

    if alerts_deleted:
        print(f"  Deleted {alerts_deleted} alert_events rows")
    if validation_deleted:
        print(f"  Deleted {validation_deleted} validation_results rows")

    return doc_deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete orphaned document records with no ruling and a sibling with a ruling.",
    )
    parser.add_argument(
        "--county",
        help="Only clean up orphans for a specific county",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without deleting",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required", file=sys.stderr)
        sys.exit(1)

    with psycopg.connect(database_url) as conn:
        orphans = find_orphans(conn, county=args.county)

        if not orphans:
            print("No orphaned documents found.")
            return

        # Group by county for reporting
        by_county: dict[str, list[dict[str, str]]] = {}
        for orphan in orphans:
            county = orphan["county"]
            by_county.setdefault(county, []).append(orphan)

        print(
            f"Found {len(orphans)} orphaned document(s) across {len(by_county)} county(ies):"
        )
        for county, county_orphans in sorted(by_county.items()):
            s3_keys = {o["s3_key"] for o in county_orphans}
            print(
                f"  {county}: {len(county_orphans)} orphan(s) from {len(s3_keys)} S3 key(s)"
            )
            for orphan in county_orphans:
                print(
                    f"    - {orphan['id']} (s3_key: ...{orphan['s3_key'][-20:]}, created: {orphan['created_at']})"
                )

        if args.dry_run:
            print("\n--dry-run: No changes made.")
            return

        orphan_ids = [o["id"] for o in orphans]
        deleted = delete_orphans(conn, orphan_ids)
        conn.commit()
        print(f"\nDeleted {deleted} orphaned document record(s).")


if __name__ == "__main__":
    main()
