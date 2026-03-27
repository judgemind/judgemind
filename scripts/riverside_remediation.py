#!/usr/bin/env python3
# venv: scraper-framework
"""Riverside data quality remediation — Phase 1 cleanup (#1985).

Cleans up Riverside county data quality issues caused by failed reingest
attempts (#1969, #1919):

Phase 1a: Takes a pre-cleanup snapshot of row counts.
Phase 1b: Deletes orphaned document records (documents with no rulings).
Phase 1c: Deletes cross-contaminated duplicate rulings (keeps the one with
           the longest ruling_text per ruling_text_hash).
Phase 1 gate: Verifies no duplicate ruling_text_hash and no orphaned docs.

Usage:
    scripts/ecs-run-task.sh scripts/riverside_remediation.py -- --dry-run
    scripts/ecs-run-task.sh scripts/riverside_remediation.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL queries — Phase 1a snapshot
# ---------------------------------------------------------------------------

SNAPSHOT_QUERY = """\
SELECT 'rulings' AS table_name, COUNT(*) AS row_count
FROM rulings r
JOIN courts co ON r.court_id = co.id
WHERE co.county = 'Riverside'
UNION ALL
SELECT 'documents', COUNT(*)
FROM documents d
JOIN courts co ON d.court_id = co.id
WHERE co.county = 'Riverside'
UNION ALL
SELECT 'cases', COUNT(*)
FROM cases ca
JOIN courts co ON ca.court_id = co.id
WHERE co.county = 'Riverside'
UNION ALL
SELECT 'unique_s3_keys', COUNT(DISTINCT d.s3_key)
FROM documents d
JOIN courts co ON d.court_id = co.id
WHERE co.county = 'Riverside'
"""

# ---------------------------------------------------------------------------
# SQL queries — Phase 1b: Delete orphaned documents
# ---------------------------------------------------------------------------

COUNT_ORPHANED_DOCS_QUERY = """\
SELECT COUNT(*)
FROM documents d
JOIN courts co ON d.court_id = co.id
LEFT JOIN rulings r ON r.document_id = d.id
WHERE co.county = 'Riverside' AND r.id IS NULL
"""

DELETE_ORPHANED_DOCS_QUERY = """\
DELETE FROM documents d
WHERE d.id IN (
    SELECT d2.id
    FROM documents d2
    JOIN courts co ON d2.court_id = co.id
    LEFT JOIN rulings r ON r.document_id = d2.id
    WHERE co.county = 'Riverside' AND r.id IS NULL
)
"""

# ---------------------------------------------------------------------------
# SQL queries — Phase 1c: Delete cross-contaminated duplicate rulings
# ---------------------------------------------------------------------------

COUNT_DUPLICATE_RULINGS_QUERY = """\
SELECT COUNT(*)
FROM rulings r
JOIN courts co ON r.court_id = co.id
WHERE co.county = 'Riverside'
  AND r.ruling_text_hash IN (
      SELECT ruling_text_hash
      FROM rulings r2
      JOIN courts co2 ON r2.court_id = co2.id
      WHERE co2.county = 'Riverside' AND r2.ruling_text_hash IS NOT NULL
      GROUP BY ruling_text_hash
      HAVING COUNT(*) > 1
  )
  AND r.id NOT IN (
      SELECT DISTINCT ON (ruling_text_hash) r3.id
      FROM rulings r3
      JOIN courts co3 ON r3.court_id = co3.id
      WHERE co3.county = 'Riverside' AND r3.ruling_text_hash IS NOT NULL
      ORDER BY ruling_text_hash, LENGTH(r3.ruling_text) DESC NULLS LAST
  )
"""

DELETE_DUPLICATE_RULINGS_QUERY = """\
DELETE FROM rulings
WHERE id IN (
    SELECT r.id
    FROM rulings r
    JOIN courts co ON r.court_id = co.id
    WHERE co.county = 'Riverside'
      AND r.ruling_text_hash IN (
          SELECT ruling_text_hash
          FROM rulings r2
          JOIN courts co2 ON r2.court_id = co2.id
          WHERE co2.county = 'Riverside' AND r2.ruling_text_hash IS NOT NULL
          GROUP BY ruling_text_hash
          HAVING COUNT(*) > 1
      )
      AND r.id NOT IN (
          SELECT DISTINCT ON (ruling_text_hash) r3.id
          FROM rulings r3
          JOIN courts co3 ON r3.court_id = co3.id
          WHERE co3.county = 'Riverside' AND r3.ruling_text_hash IS NOT NULL
          ORDER BY ruling_text_hash, LENGTH(r3.ruling_text) DESC NULLS LAST
      )
)
"""

# ---------------------------------------------------------------------------
# SQL queries — Phase 1 gate verification
# ---------------------------------------------------------------------------

GATE_DUPLICATE_HASHES_QUERY = """\
SELECT COUNT(*)
FROM (
    SELECT ruling_text_hash
    FROM rulings r
    JOIN courts co ON r.court_id = co.id
    WHERE co.county = 'Riverside' AND r.ruling_text_hash IS NOT NULL
    GROUP BY ruling_text_hash
    HAVING COUNT(*) > 1
) sub
"""

GATE_ORPHANED_DOCS_QUERY = """\
SELECT COUNT(*)
FROM documents d
JOIN courts co ON d.court_id = co.id
LEFT JOIN rulings r ON r.document_id = d.id
WHERE co.county = 'Riverside' AND r.id IS NULL
"""


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def take_snapshot(conn: psycopg.Connection) -> dict[str, int]:
    """Take a snapshot of Riverside row counts.

    Returns a dict mapping table name to row count.
    """
    result: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(SNAPSHOT_QUERY)
        for row in cur.fetchall():
            result[row[0]] = row[1]
    return result


def count_orphaned_docs(conn: psycopg.Connection) -> int:
    """Count orphaned document records (documents with no rulings)."""
    with conn.cursor() as cur:
        cur.execute(COUNT_ORPHANED_DOCS_QUERY)
        row = cur.fetchone()
        return row[0] if row else 0


def delete_orphaned_docs(conn: psycopg.Connection) -> int:
    """Delete orphaned document records. Returns the number deleted."""
    with conn.cursor() as cur:
        cur.execute(DELETE_ORPHANED_DOCS_QUERY)
        return cur.rowcount


def count_duplicate_rulings(conn: psycopg.Connection) -> int:
    """Count cross-contaminated duplicate rulings to be deleted."""
    with conn.cursor() as cur:
        cur.execute(COUNT_DUPLICATE_RULINGS_QUERY)
        row = cur.fetchone()
        return row[0] if row else 0


def delete_duplicate_rulings(conn: psycopg.Connection) -> int:
    """Delete duplicate rulings, keeping the longest per hash. Returns count."""
    with conn.cursor() as cur:
        cur.execute(DELETE_DUPLICATE_RULINGS_QUERY)
        return cur.rowcount


def verify_gates(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Verify Phase 1 gate conditions.

    Returns a list of gate results, each with 'name', 'passed', and 'value'.
    """
    gates: list[dict[str, Any]] = []

    with conn.cursor() as cur:
        # Gate 1: No duplicate ruling_text_hash across different cases
        cur.execute(GATE_DUPLICATE_HASHES_QUERY)
        row = cur.fetchone()
        dup_count = row[0] if row else -1
        gates.append(
            {
                "name": "no_duplicate_ruling_text_hash",
                "passed": dup_count == 0,
                "value": dup_count,
                "expected": 0,
            }
        )

        # Gate 2: No orphaned documents
        cur.execute(GATE_ORPHANED_DOCS_QUERY)
        row = cur.fetchone()
        orphan_count = row[0] if row else -1
        gates.append(
            {
                "name": "no_orphaned_documents",
                "passed": orphan_count == 0,
                "value": orphan_count,
                "expected": 0,
            }
        )

    return gates


def run_remediation(
    dsn: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the full Phase 1 remediation.

    Returns a summary dict with snapshot, actions taken, and gate results.
    """
    summary: dict[str, Any] = {}

    with psycopg.connect(dsn) as conn:
        # Phase 1a: Pre-cleanup snapshot
        logger.info("=== Phase 1a: Taking pre-cleanup snapshot ===")
        before = take_snapshot(conn)
        summary["snapshot_before"] = before
        for table, count in sorted(before.items()):
            logger.info("  %s: %d", table, count)

        # Phase 1b: Delete orphaned documents
        logger.info("=== Phase 1b: Deleting orphaned documents ===")
        orphan_count = count_orphaned_docs(conn)
        logger.info("  Found %d orphaned documents", orphan_count)
        summary["orphaned_docs_found"] = orphan_count

        if orphan_count > 0 and not dry_run:
            deleted = delete_orphaned_docs(conn)
            logger.info("  Deleted %d orphaned documents", deleted)
            summary["orphaned_docs_deleted"] = deleted
            conn.commit()
        elif dry_run:
            logger.info("  [DRY RUN] Would delete %d orphaned documents", orphan_count)
            summary["orphaned_docs_deleted"] = 0
        else:
            logger.info("  No orphaned documents to delete")
            summary["orphaned_docs_deleted"] = 0

        # Phase 1c: Delete cross-contaminated duplicate rulings
        logger.info("=== Phase 1c: Deleting duplicate rulings ===")
        dup_count = count_duplicate_rulings(conn)
        logger.info("  Found %d duplicate rulings to remove", dup_count)
        summary["duplicate_rulings_found"] = dup_count

        if dup_count > 0 and not dry_run:
            deleted = delete_duplicate_rulings(conn)
            logger.info("  Deleted %d duplicate rulings", deleted)
            summary["duplicate_rulings_deleted"] = deleted
            conn.commit()
        elif dry_run:
            logger.info("  [DRY RUN] Would delete %d duplicate rulings", dup_count)
            summary["duplicate_rulings_deleted"] = 0
        else:
            logger.info("  No duplicate rulings to delete")
            summary["duplicate_rulings_deleted"] = 0

        # Post-cleanup snapshot
        logger.info("=== Post-cleanup snapshot ===")
        after = take_snapshot(conn)
        summary["snapshot_after"] = after
        for table, count in sorted(after.items()):
            logger.info("  %s: %d", table, count)

        # Phase 1 gate verification
        logger.info("=== Phase 1 gate verification ===")
        gates = verify_gates(conn)
        summary["gates"] = gates
        all_passed = True
        for gate in gates:
            status = "PASS" if gate["passed"] else "FAIL"
            logger.info(
                "  [%s] %s: got %s (expected %s)",
                status,
                gate["name"],
                gate["value"],
                gate["expected"],
            )
            if not gate["passed"]:
                all_passed = False

        summary["all_gates_passed"] = all_passed
        if all_passed:
            logger.info("All Phase 1 gates PASSED")
        else:
            logger.warning("Some Phase 1 gates FAILED — do not proceed to Phase 2")

    return summary


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Riverside data quality remediation — Phase 1 cleanup (#1985)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without modifying the database.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output summary as JSON to stdout.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    summary = run_remediation(dsn, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))

    if not summary["all_gates_passed"] and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
