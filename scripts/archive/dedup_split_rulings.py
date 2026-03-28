#!/usr/bin/env python3
"""Deduplicate split rulings created by non-idempotent backfill runs.

When the backfill_split_rulings script ran multiple times on documents that
were re-captured by the live scraper in between, each run created new split
rulings with different document_ids but identical content.  This script finds
and removes those duplicates.

The deduplication key is (case_id, ruling_text_hash).  For each group of
duplicates, the ruling with the earliest created_at is kept and the rest
are deleted.  Orphaned document records (documents with no remaining rulings)
are also cleaned up.

Usage:
    # Dry-run (default): report what would be cleaned up
    scripts/run-py.sh scripts/dedup_split_rulings.py

    # Apply changes
    scripts/run-py.sh scripts/dedup_split_rulings.py --apply

    # Restrict to specific departments
    scripts/run-py.sh scripts/dedup_split_rulings.py --departments N15 N16 N17
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass

import psycopg

from ingestion.db import normalize_ruling_text_hash

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class DuplicateGroup:
    """A group of duplicate rulings sharing the same (case_id, ruling_text_hash)."""

    case_id: str
    ruling_text_hash: str
    count: int
    kept_ruling_id: str
    deleted_ruling_ids: list[str]
    deleted_document_ids: list[str]


# ---------------------------------------------------------------------------
# Core dedup logic (importable for testing)
# ---------------------------------------------------------------------------

FIND_DUPLICATES_QUERY = """
    SELECT
        r.case_id,
        r.ruling_text_hash,
        COUNT(*) AS cnt
    FROM rulings r
    WHERE r.ruling_text_hash IS NOT NULL
    {department_filter}
    GROUP BY r.case_id, r.ruling_text_hash
    HAVING COUNT(*) > 1
    ORDER BY COUNT(*) DESC
"""

GET_DUPLICATE_DETAILS_QUERY = """
    SELECT r.id, r.document_id, r.created_at
    FROM rulings r
    WHERE r.case_id = %s::uuid
    AND r.ruling_text_hash = %s
    ORDER BY r.created_at ASC, r.id ASC
"""


def find_duplicate_groups(
    conn: psycopg.Connection,
    departments: list[str] | None = None,
) -> list[DuplicateGroup]:
    """Find groups of duplicate rulings and determine which to keep/delete.

    First backfills any NULL ruling_text_hash values so all rulings are
    covered, then groups by (case_id, ruling_text_hash) to find duplicates.
    For each group, keeps the ruling with the earliest created_at (tie-broken
    by id) and marks the rest for deletion.

    Returns a list of DuplicateGroup objects describing what to do.
    """
    groups: list[DuplicateGroup] = []

    # Phase 1: First backfill any NULL ruling_text_hash values for departments
    # we care about, so the hash-based dedup catches everything.
    _backfill_null_hashes(conn, departments)

    # Phase 2: Find duplicates by (case_id, ruling_text_hash).
    if departments:
        placeholders = ", ".join(["%s"] * len(departments))
        dept_filter = f"AND r.department IN ({placeholders})"
    else:
        dept_filter = ""
    query = FIND_DUPLICATES_QUERY.format(department_filter=dept_filter)
    params: list[str] = list(departments) if departments else []

    with conn.cursor() as cur:
        cur.execute(query, params)
        dup_rows = cur.fetchall()

    for case_id, text_hash, count in dup_rows:
        with conn.cursor() as cur:
            cur.execute(
                GET_DUPLICATE_DETAILS_QUERY,
                (str(case_id), text_hash),
            )
            details = cur.fetchall()

        if len(details) < 2:
            continue

        # Keep the earliest created ruling, delete the rest.
        kept = details[0]
        to_delete = details[1:]
        groups.append(
            DuplicateGroup(
                case_id=str(case_id),
                ruling_text_hash=text_hash,
                count=count,
                kept_ruling_id=str(kept[0]),
                deleted_ruling_ids=[str(d[0]) for d in to_delete],
                deleted_document_ids=[str(d[1]) for d in to_delete],
            )
        )

    return groups


def _backfill_null_hashes(
    conn: psycopg.Connection,
    departments: list[str] | None = None,
) -> int:
    """Compute and set ruling_text_hash for rulings where it is NULL.

    This ensures hash-based dedup covers older rulings that predate the
    ruling_text_hash column.

    Returns the number of rulings updated.
    """
    if departments:
        placeholders = ", ".join(["%s"] * len(departments))
        dept_filter = f"AND department IN ({placeholders})"
    else:
        dept_filter = ""

    query = f"""
        SELECT id, ruling_text
        FROM rulings
        WHERE ruling_text_hash IS NULL
        AND ruling_text IS NOT NULL
        {dept_filter}
    """
    params: list[str] = list(departments) if departments else []

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    updated = 0
    for ruling_id, ruling_text in rows:
        text_hash = normalize_ruling_text_hash(ruling_text)
        if text_hash is None:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rulings SET ruling_text_hash = %s WHERE id = %s::uuid",
                (text_hash, str(ruling_id)),
            )
        updated += 1

    if updated:
        logger.info(
            "Backfilled ruling_text_hash for %d rulings with NULL hashes", updated
        )
    return updated


def apply_dedup(
    conn: psycopg.Connection,
    groups: list[DuplicateGroup],
) -> dict[str, int]:
    """Delete duplicate rulings and clean up orphaned documents.

    Returns stats dict with counts.
    """
    total_deleted_rulings = 0
    total_cleaned_documents = 0

    for group in groups:
        # Delete duplicate rulings.
        for ruling_id in group.deleted_ruling_ids:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM rulings WHERE id = %s::uuid",
                    (ruling_id,),
                )
            total_deleted_rulings += 1

        # Clean up orphaned documents (documents with no remaining rulings).
        for doc_id in group.deleted_document_ids:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM documents
                    WHERE id = %s::uuid
                    AND NOT EXISTS (
                        SELECT 1 FROM rulings WHERE document_id = %s::uuid
                    )
                    """,
                    (doc_id, doc_id),
                )
                if cur.rowcount and cur.rowcount > 0:
                    total_cleaned_documents += 1

    return {
        "deleted_rulings": total_deleted_rulings,
        "cleaned_documents": total_cleaned_documents,
    }


def run_dedup(
    dsn: str,
    *,
    departments: list[str] | None = None,
    dry_run: bool = True,
) -> dict[str, int]:
    """Run the full deduplication process.

    Returns summary stats.
    """
    with psycopg.connect(dsn) as conn:
        groups = find_duplicate_groups(conn, departments)

        if not groups:
            logger.info("No duplicate rulings found")
            conn.rollback()
            return {
                "duplicate_groups": 0,
                "total_duplicates": 0,
                "deleted_rulings": 0,
                "cleaned_documents": 0,
            }

        total_duplicates = sum(len(g.deleted_ruling_ids) for g in groups)
        logger.info(
            "Found %d duplicate groups with %d total duplicate rulings to remove",
            len(groups),
            total_duplicates,
        )

        for group in groups:
            logger.info(
                "  case_id=%s hash=%s: %d duplicates (keeping ruling %s, deleting %s)",
                group.case_id,
                group.ruling_text_hash[:12],
                len(group.deleted_ruling_ids),
                group.kept_ruling_id,
                group.deleted_ruling_ids,
            )

        if dry_run:
            logger.info("[dry-run] No changes written")
            conn.rollback()
            return {
                "duplicate_groups": len(groups),
                "total_duplicates": total_duplicates,
                "deleted_rulings": 0,
                "cleaned_documents": 0,
            }

        stats = apply_dedup(conn, groups)
        conn.commit()
        logger.info(
            "[committed] Deleted %d duplicate rulings, cleaned %d orphaned documents",
            stats["deleted_rulings"],
            stats["cleaned_documents"],
        )

        return {
            "duplicate_groups": len(groups),
            "total_duplicates": total_duplicates,
            **stats,
        }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate split rulings from non-idempotent backfill runs.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute changes (default is dry-run).",
    )
    parser.add_argument(
        "--departments",
        nargs="+",
        default=None,
        help="Restrict to specific departments (e.g., N15 N16 N17).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    dry_run = not args.apply
    if dry_run:
        logger.info("DRY-RUN mode — no changes will be written")
    else:
        logger.info("APPLY mode — changes will be committed")

    stats = run_dedup(dsn, departments=args.departments, dry_run=dry_run)

    logger.info(
        "Dedup complete: %d groups, %d duplicates found, %d deleted, %d documents cleaned",
        stats["duplicate_groups"],
        stats["total_duplicates"],
        stats["deleted_rulings"],
        stats["cleaned_documents"],
    )


if __name__ == "__main__":
    main()
