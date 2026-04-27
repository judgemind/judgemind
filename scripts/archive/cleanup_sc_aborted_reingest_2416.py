#!/usr/bin/env python3
"""Clean up Santa Clara data from the aborted 2026-04-16 reingest (see #2335/#2416).

The `reingest_from_s3.py --multimodal` run for Santa Clara on 2026-04-16 hit
the split-child reprocessing bug described in #2416: it created ~10,563 new
split-child documents and superseded ~992 parents without reducing the null
outcome rate (went from 45.4% to 72.7%).

This script restores the pre-reingest state:
1. Deletes rulings linked to split-child documents created on 2026-04-16.
2. Deletes those split-child documents.
3. Re-activates parent documents superseded on 2026-04-16.
"""
# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import psycopg

CUTOFF = datetime(2026, 4, 16, 23, 0, 0, tzinfo=timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would be changed")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Baseline counts
            # NOTE: All current superseded SC docs come from this aborted reingest
            # (baseline after #2365 cleanup had 0 superseded), so we can target
            # all superseded SC docs for reactivation without a timestamp filter.
            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents d JOIN courts ct ON d.court_id = ct.id
                     WHERE ct.county = 'Santa Clara' AND d.status = 'active') AS active_sc_docs,
                    (SELECT COUNT(*) FROM documents d JOIN courts ct ON d.court_id = ct.id
                     WHERE ct.county = 'Santa Clara' AND d.status = 'superseded') AS superseded_all,
                    (SELECT COUNT(*) FROM documents d JOIN courts ct ON d.court_id = ct.id
                     WHERE ct.county = 'Santa Clara' AND d.status = 'active' AND d.created_at >= %s) AS new_active_recent,
                    (SELECT COUNT(*) FROM rulings r
                     JOIN cases c ON r.case_id = c.id
                     JOIN courts ct ON c.court_id = ct.id
                     WHERE ct.county = 'Santa Clara') AS total_rulings,
                    (SELECT COUNT(*) FROM rulings r
                     JOIN cases c ON r.case_id = c.id
                     JOIN courts ct ON c.court_id = ct.id
                     WHERE ct.county = 'Santa Clara' AND r.outcome IS NULL) AS null_outcomes
                """,
                (CUTOFF,),
            )
            row = cur.fetchone()
            (active_sc_docs, superseded_all, new_active_recent, total_rulings, null_outcomes) = row
            print(f"Baseline: active_sc_docs={active_sc_docs} superseded_all={superseded_all}"
                  f" new_active_recent={new_active_recent} total_rulings={total_rulings}"
                  f" null_outcomes={null_outcomes}")

            if args.dry_run:
                print("DRY RUN — no changes will be made")
                # Count rulings that would be deleted
                cur.execute(
                    """
                    SELECT COUNT(*) FROM rulings r
                    WHERE r.document_id IN (
                        SELECT d.id FROM documents d
                        JOIN courts ct ON d.court_id = ct.id
                        WHERE ct.county = 'Santa Clara'
                          AND d.status = 'active'
                          AND d.created_at >= %s
                    )
                    """,
                    (CUTOFF,),
                )
                to_delete_rulings = cur.fetchone()[0]
                print(f"DRY RUN — would delete {to_delete_rulings} rulings from {new_active_recent} "
                      f"new docs and re-activate {superseded_all} superseded docs")
                return 0

            # Step 1: Delete rulings for the newly-created split children
            cur.execute(
                """
                DELETE FROM rulings
                WHERE document_id IN (
                    SELECT d.id FROM documents d
                    JOIN courts ct ON d.court_id = ct.id
                    WHERE ct.county = 'Santa Clara'
                      AND d.status = 'active'
                      AND d.created_at >= %s
                )
                """,
                (CUTOFF,),
            )
            deleted_rulings = cur.rowcount
            print(f"Deleted {deleted_rulings} rulings from newly-created split children")

            # Step 2: Delete the newly-created split child documents
            cur.execute(
                """
                DELETE FROM documents
                WHERE id IN (
                    SELECT d.id FROM documents d
                    JOIN courts ct ON d.court_id = ct.id
                    WHERE ct.county = 'Santa Clara'
                      AND d.status = 'active'
                      AND d.created_at >= %s
                )
                """,
                (CUTOFF,),
            )
            deleted_docs = cur.rowcount
            print(f"Deleted {deleted_docs} split-child documents")

            # Step 3: Re-activate parent documents that were superseded
            # NOTE: All current superseded SC docs come from this aborted reingest
            # (baseline after #2365 cleanup had 0 superseded). No timestamp filter needed.
            cur.execute(
                """
                UPDATE documents
                SET status = 'active'
                WHERE id IN (
                    SELECT d.id FROM documents d
                    JOIN courts ct ON d.court_id = ct.id
                    WHERE ct.county = 'Santa Clara'
                      AND d.status = 'superseded'
                )
                """
            )
            reactivated = cur.rowcount
            print(f"Re-activated {reactivated} superseded parent documents")

            conn.commit()
            print("Committed")

            # Verification
            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents d JOIN courts ct ON d.court_id = ct.id
                     WHERE ct.county = 'Santa Clara' AND d.status = 'active') AS active_sc_docs,
                    (SELECT COUNT(*) FROM documents d JOIN courts ct ON d.court_id = ct.id
                     WHERE ct.county = 'Santa Clara' AND d.status = 'superseded') AS superseded,
                    (SELECT COUNT(*) FROM rulings r
                     JOIN cases c ON r.case_id = c.id
                     JOIN courts ct ON c.court_id = ct.id
                     WHERE ct.county = 'Santa Clara') AS total_rulings,
                    (SELECT COUNT(*) FROM rulings r
                     JOIN cases c ON r.case_id = c.id
                     JOIN courts ct ON c.court_id = ct.id
                     WHERE ct.county = 'Santa Clara' AND r.outcome IS NULL) AS null_outcomes
                """
            )
            (active_sc_docs2, superseded2, total_rulings2, null_outcomes2) = cur.fetchone()
            null_pct = 100.0 * null_outcomes2 / max(total_rulings2, 1)
            print(f"After: active_sc_docs={active_sc_docs2} superseded={superseded2}"
                  f" total_rulings={total_rulings2} null_outcomes={null_outcomes2}"
                  f" null_pct={null_pct:.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
