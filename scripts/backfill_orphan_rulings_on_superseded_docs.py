#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill — delete orphan rulings that reference non-active documents (#3728).

"Orphan rulings" are rows in ``derived.rulings`` whose ``document_id`` points
to a ``derived.documents`` row with ``status != 'active'`` (most commonly
``status = 'superseded'``).  These arise when dedup-supersede marks the losing
document as superseded but does not delete the associated ruling row first.

The migration in ``56_rulings-block-non-active-document.sql`` prevents NEW
orphans from being created via a DB trigger.  This script cleans up existing
orphans that pre-date that trigger.

For each orphan ruling the script:

1. **Winner check (defense-in-depth):** Queries for a ruling on the *same*
   ``case_id`` with the *same* ``ruling_text_hash`` whose document is active.
   - If no winner exists: logs ``WARN orphan_ruling_winner_missing`` and SKIPS
     the delete. A missing winner might indicate data corruption that needs
     manual review.
   - If a winner exists: proceeds to step 2.

2. **DELETE** the orphan ruling row from ``derived.rulings``.

3. **INSERT** a ``telemetry.data_quality_metrics`` row with
   ``metric_name='orphan_ruling_deleted'`` containing ``loser_document_id``,
   ``winner_document_id``, ``case_id``, ``ruling_text_hash``.  Mirrors the
   ``content_hash_dedup_supersede`` metric shape in ``db.py:1948-1971``.

This is an ECS oneshot script: no local imports from other scripts/, only
stdlib + installed packages.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_orphan_rulings_on_superseded_docs.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_orphan_rulings_on_superseded_docs.py -- --dry-run --limit 5
    scripts/ecs-run-task.sh scripts/backfill_orphan_rulings_on_superseded_docs.py -- --county "Los Angeles"
    scripts/ecs-run-task.sh scripts/backfill_orphan_rulings_on_superseded_docs.py

Options:
    --dry-run       Show what would be deleted without writing to DB.
    --limit N       Maximum number of orphan rows to process (default: all).
    --county NAME   Scope to one county (case-insensitive, default: all).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402
import structlog  # noqa: E402

from framework.logging import configure_structlog  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

# SELECT all orphan rulings — rulings whose document is not active.
_SELECT_ORPHANS = """
    SELECT
        r.id::text              AS ruling_id,
        r.case_id::text         AS case_id,
        r.ruling_text_hash      AS ruling_text_hash,
        r.document_id::text     AS loser_document_id,
        d.previous_version_id::text AS winner_document_id,
        c.county                AS county
    FROM derived.rulings r
    JOIN derived.documents d ON d.id = r.document_id
    JOIN derived.courts c ON c.id = r.court_id
    WHERE d.status != 'active'
"""

# Check that a winner (active document) carries the same ruling text hash.
_SELECT_WINNER_CHECK = """
    SELECT 1
    FROM derived.rulings r2
    JOIN derived.documents d2 ON d2.id = r2.document_id
    WHERE r2.case_id = %s::uuid
      AND r2.ruling_text_hash = %s
      AND d2.status = 'active'
"""

# Delete the orphan ruling row.
_DELETE_ORPHAN_RULING = "DELETE FROM derived.rulings WHERE id = %s::uuid"

# Insert a telemetry metric row.
_INSERT_METRIC = """
    INSERT INTO telemetry.data_quality_metrics
        (recorded_at, county, metric_name, metric_value, metadata)
    VALUES (now(), %s, %s, %s, %s::jsonb)
"""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def fetch_orphans(
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
    county: str | None = None,
) -> list[dict]:
    """Fetch orphan rulings whose document is not active."""
    query = _SELECT_ORPHANS
    params: list[object] = []

    if county:
        query += " AND lower(c.county) = lower(%s)"
        params.append(county)

    query += " ORDER BY c.county, r.id"

    if limit is not None:
        query += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(query, params or None)
        rows = cur.fetchall()

    return [
        {
            "ruling_id": row[0],
            "case_id": row[1],
            "ruling_text_hash": row[2],
            "loser_document_id": row[3],
            "winner_document_id": row[4],
            "county": row[5],
        }
        for row in rows
    ]


def winner_exists(
    conn: psycopg.Connection,
    case_id: str,
    ruling_text_hash: str | None,
) -> bool:
    """Return True if an active-document ruling with the same hash exists."""
    if ruling_text_hash is None:
        # No hash — cannot verify winner; treat as missing for safety.
        return False
    with conn.cursor() as cur:
        cur.execute(_SELECT_WINNER_CHECK, (case_id, ruling_text_hash))
        row = cur.fetchone()
    return row is not None


def delete_orphan(
    conn: psycopg.Connection,
    ruling_id: str,
    *,
    dry_run: bool,
) -> bool:
    """Delete a single orphan ruling.

    Returns True if the row was (or would be) deleted.
    """
    if dry_run:
        return True
    with conn.cursor() as cur:
        cur.execute(_DELETE_ORPHAN_RULING, (ruling_id,))
    return True


def insert_metric(
    conn: psycopg.Connection,
    county: str,
    orphan: dict,
    *,
    dry_run: bool,
) -> None:
    """Insert a telemetry metric row for the deleted orphan ruling."""
    if dry_run:
        return
    metadata = json.dumps(
        {
            "loser_document_id": orphan["loser_document_id"],
            "winner_document_id": orphan["winner_document_id"],
            "case_id": orphan["case_id"],
            "ruling_text_hash": orphan["ruling_text_hash"],
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_METRIC,
            (county, "orphan_ruling_deleted", 1, metadata),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the orphan-rulings cleanup backfill."""
    parser = argparse.ArgumentParser(
        description=("Delete orphan rulings on non-active documents (#3728)."),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without writing to DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of orphan rows to process (default: all).",
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Scope to one county by name (case-insensitive, default: all).",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    conn = psycopg.connect(db_url, autocommit=False)
    try:
        orphans = fetch_orphans(conn, limit=args.limit, county=args.county)
        total = len(orphans)
        logger.info(
            "Fetched orphan rulings to process",
            total=total,
            dry_run=args.dry_run,
            county=args.county,
        )

        if not orphans:
            logger.info("No orphan rulings found -- database is clean")
            return

        deleted = 0
        skipped_missing_winner = 0

        for orphan in orphans:
            ruling_id = orphan["ruling_id"]
            case_id = orphan["case_id"]
            ruling_text_hash = orphan["ruling_text_hash"]
            county = orphan["county"]

            # Defense-in-depth: refuse to delete when we cannot verify a winner.
            if not winner_exists(conn, case_id, ruling_text_hash):
                logger.warning(
                    "orphan_ruling_winner_missing -- skipping delete",
                    ruling_id=ruling_id,
                    case_id=case_id,
                    loser_document_id=orphan["loser_document_id"],
                    ruling_text_hash=ruling_text_hash,
                )
                skipped_missing_winner += 1
                continue

            if args.dry_run:
                logger.info(
                    "DRY RUN: would delete orphan ruling",
                    ruling_id=ruling_id,
                    loser_document_id=orphan["loser_document_id"],
                    winner_document_id=orphan["winner_document_id"],
                    case_id=case_id,
                    county=county,
                )
                deleted += 1
                continue

            delete_orphan(conn, ruling_id, dry_run=False)
            insert_metric(conn, county, orphan, dry_run=False)
            conn.commit()
            deleted += 1
            logger.info(
                "orphan_ruling_deleted",
                ruling_id=ruling_id,
                loser_document_id=orphan["loser_document_id"],
                winner_document_id=orphan["winner_document_id"],
                case_id=case_id,
                county=county,
            )

        logger.info(
            "backfill_orphan_rulings_summary",
            total_scanned=total,
            deleted=deleted,
            skipped_missing_winner=skipped_missing_winner,
            dry_run=args.dry_run,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
