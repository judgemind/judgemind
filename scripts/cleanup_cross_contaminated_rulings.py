#!/usr/bin/env python3
# venv: scraper-framework
"""Clean up cross-contaminated ruling_text in the database.

Finds rulings where multiple distinct case_ids share the same ruling_text_hash
and the ruling_text is suspiciously long (> 500 chars), indicating that the
full-PDF calendar text was stored instead of per-case ruling text.

Sets ruling_text to NULL for affected rows and clears the ruling_text_hash.

This addresses the cross-contamination bug described in #2078: when
reingest_from_s3.py processed multi-ruling PDFs with multimodal fallback,
the full-PDF text was stored as ruling_text for every case in the document.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        scripts/run-py.sh scripts/cleanup_cross_contaminated_rulings.py \
        --county Orange --dry-run

    # Via ECS (preferred for dev):
    scripts/ecs-run-task.sh scripts/cleanup_cross_contaminated_rulings.py \
        -- --county Orange --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up cross-contaminated ruling_text",
    )
    parser.add_argument(
        "--county",
        required=True,
        help="County to clean up (e.g., 'Orange')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be cleaned without modifying data",
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=500,
        help="Minimum ruling_text length to consider contaminated (default: 500)",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required", file=sys.stderr)
        sys.exit(1)

    try:
        import psycopg
    except ImportError:
        print(
            "ERROR: psycopg is required. Install via: pip install psycopg[binary]",
            file=sys.stderr,
        )
        sys.exit(1)

    # Find contaminated ruling_text_hashes: hashes shared by multiple distinct
    # case_ids where the ruling_text is suspiciously long.
    find_query = """
        SELECT r.id, r.case_id, r.document_id, LENGTH(r.ruling_text) AS text_len,
               r.ruling_text_hash
        FROM rulings r
        JOIN courts ct ON r.court_id = ct.id
        WHERE ct.county = %s
          AND r.ruling_text_hash IN (
              SELECT r2.ruling_text_hash
              FROM rulings r2
              JOIN courts ct2 ON r2.court_id = ct2.id
              WHERE ct2.county = %s
                AND r2.ruling_text_hash IS NOT NULL
              GROUP BY r2.ruling_text_hash
              HAVING COUNT(DISTINCT r2.case_id) > 1
          )
          AND LENGTH(r.ruling_text) > %s
        ORDER BY r.ruling_text_hash, r.case_id
    """

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(find_query, (args.county, args.county, args.min_length))
            rows = cur.fetchall()

        if not rows:
            print(f"No cross-contaminated rulings found in {args.county}.")
            return

        # Group by ruling_text_hash for display
        by_hash: dict[str, list[tuple]] = {}
        for row in rows:
            ruling_id, case_id, doc_id, text_len, text_hash = row
            by_hash.setdefault(text_hash, []).append(row)

        print(
            f"Found {len(rows)} contaminated rulings across {len(by_hash)} shared hashes:"
        )
        for text_hash, group in by_hash.items():
            print(
                f"\n  Hash: {text_hash[:16]}... ({len(group)} rulings, {group[0][3]} chars each)"
            )
            for ruling_id, case_id, doc_id, text_len, _ in group:
                print(f"    ruling={ruling_id}, case={case_id}, doc={doc_id}")

        if args.dry_run:
            print(f"\nDRY RUN: Would set ruling_text=NULL for {len(rows)} rulings.")
            return

        # Clean up: set ruling_text to NULL and clear ruling_text_hash.
        ruling_ids = [row[0] for row in rows]
        update_query = """
            UPDATE rulings
            SET ruling_text = NULL,
                ruling_text_hash = NULL,
                ruling_text_html = NULL
            WHERE id = ANY(%s::uuid[])
        """
        with conn.cursor() as cur:
            cur.execute(update_query, (ruling_ids,))
            updated = cur.rowcount

        conn.commit()
        print(f"\nCleaned {updated} contaminated rulings (ruling_text set to NULL).")


if __name__ == "__main__":
    main()
