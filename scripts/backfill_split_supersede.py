#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill change_type (and previous_version_id where possible) for
pre-existing split-superseded documents (#3510).

Finds ``derived.documents`` rows where ``status='superseded'``,
``previous_version_id IS NULL``, and ``change_type IS NULL`` — these are
documents that were marked superseded before the live-code fix in #3510
started populating ``change_type``.

For each loser it attempts to find the canonical successor via:
  1. Find all ``active`` siblings sharing the same ``case_id`` that have at
     least one row in ``derived.rulings``.
  2. Pick the most-recently-``created_at`` sibling as the successor.

If a viable sibling is found:
  UPDATE SET previous_version_id = <sibling.id>, change_type = 'split_supersede'

If case_id IS NULL or no sibling has rulings:
  UPDATE SET change_type = 'split_supersede_unverified'

Both UPDATE statements include an idempotent WHERE guard
(``AND previous_version_id IS NULL AND change_type IS NULL``) so the script
is safe to run multiple times.

This is an ECS oneshot script: no local imports from scripts/, only stdlib +
installed packages.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_split_supersede.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_split_supersede.py -- --dry-run --limit 5
    scripts/ecs-run-task.sh scripts/backfill_split_supersede.py -- --county "Los Angeles"
    scripts/ecs-run-task.sh scripts/backfill_split_supersede.py

Options:
    --dry-run       Show what would be updated without writing to DB.
    --limit N       Maximum number of loser rows to process (default: all).
    --county NAME   Scope to one county (case-insensitive, default: all).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

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

# SELECT all pre-existing superseded documents with no change_type yet.
_SELECT_LOSERS = """
    SELECT
        d.id::text        AS loser_id,
        d.case_id::text   AS loser_case_id,
        co.county         AS county
    FROM derived.documents d
    JOIN derived.courts co ON co.id = d.court_id
    WHERE d.status = 'superseded'
      AND d.previous_version_id IS NULL
      AND d.change_type IS NULL
"""

# Find active siblings sharing the same case_id that have at least one ruling.
# Returns newest-first (by created_at) so pick_sibling_for_loser takes first.
_SELECT_SIBLING_CANDIDATES = """
    SELECT d.id::text, d.created_at
    FROM derived.documents d
    WHERE d.case_id = %s::uuid
      AND d.status = 'active'
      AND d.id != %s::uuid
      AND EXISTS (
          SELECT 1 FROM derived.rulings r WHERE r.document_id = d.id
      )
    ORDER BY d.created_at DESC
"""

# Idempotent UPDATE when a sibling successor is found.
_UPDATE_LOSER_WITH_SIBLING = (
    "UPDATE derived.documents "
    "SET previous_version_id = %s::uuid, "
    "    change_type = 'split_supersede' "
    "WHERE id = %s::uuid "
    "  AND previous_version_id IS NULL "
    "  AND change_type IS NULL"
)

# Idempotent UPDATE when no viable sibling is found.
_UPDATE_LOSER_UNVERIFIED = (
    "UPDATE derived.documents "
    "SET change_type = 'split_supersede_unverified' "
    "WHERE id = %s::uuid "
    "  AND previous_version_id IS NULL "
    "  AND change_type IS NULL"
)

_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Pure helpers -- testable without a live DB
# ---------------------------------------------------------------------------


def pick_sibling_for_loser(candidates: list[dict]) -> str | None:
    """Return the newest sibling document id, or None if no candidates.

    Parameters
    ----------
    candidates:
        Each dict must have at least an ``"id"`` key (str UUID) and a
        ``"created_at"`` key.  The list should already be ordered
        newest-first (as returned by ``_SELECT_SIBLING_CANDIDATES``).

    Returns
    -------
    str | None
        The id of the newest candidate when at least one exists, else
        ``None``.
    """
    if not candidates:
        return None
    # Candidates are pre-sorted newest-first by the SQL ORDER BY.
    return candidates[0]["id"]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def fetch_losers(
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
    county: str | None = None,
) -> list[dict]:
    """Fetch pre-existing superseded documents that need backfilling."""
    query = _SELECT_LOSERS
    params: list[object] = []

    if county:
        query += " AND lower(co.county) = lower(%s)"
        params.append(county)

    query += " ORDER BY co.county, d.id"

    if limit is not None:
        query += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(query, params or None)
        rows = cur.fetchall()

    return [
        {
            "loser_id": row[0],
            "loser_case_id": row[1],
            "county": row[2],
        }
        for row in rows
    ]


def find_sibling_candidates(
    conn: psycopg.Connection,
    loser_id: str,
    case_id: str,
) -> list[dict]:
    """Find active siblings with rulings sharing case_id with the loser."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SIBLING_CANDIDATES, (case_id, loser_id))
        rows = cur.fetchall()
    return [{"id": row[0], "created_at": row[1]} for row in rows]


def apply_update_with_sibling(
    conn: psycopg.Connection,
    updates: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> int:
    """Execute a batch of sibling-link UPDATE statements.

    Parameters
    ----------
    updates:
        List of ``(sibling_id, loser_id)`` tuples.
    dry_run:
        If ``True``, log the intended updates but do not execute.

    Returns
    -------
    int
        Number of intended links (dry-run) or rows actually updated (write).
    """
    if dry_run:
        for sibling_id, loser_id in updates:
            logger.info(
                "DRY RUN: would link loser to sibling",
                loser_id=loser_id,
                sibling_id=sibling_id,
                change_type="split_supersede",
            )
        return len(updates)

    updated = 0
    with conn.cursor() as cur:
        for sibling_id, loser_id in updates:
            cur.execute(_UPDATE_LOSER_WITH_SIBLING, (sibling_id, loser_id))
            updated += cur.rowcount
    conn.commit()
    return updated


def apply_update_unverified(
    conn: psycopg.Connection,
    loser_ids: list[str],
    *,
    dry_run: bool,
) -> int:
    """Execute a batch of unverified UPDATE statements.

    Parameters
    ----------
    loser_ids:
        List of loser document ids.
    dry_run:
        If ``True``, log the intended updates but do not execute.

    Returns
    -------
    int
        Number of intended updates (dry-run) or rows actually updated (write).
    """
    if dry_run:
        for loser_id in loser_ids:
            logger.info(
                "DRY RUN: would mark loser unverified",
                loser_id=loser_id,
                change_type="split_supersede_unverified",
            )
        return len(loser_ids)

    updated = 0
    with conn.cursor() as cur:
        for loser_id in loser_ids:
            cur.execute(_UPDATE_LOSER_UNVERIFIED, (loser_id,))
            updated += cur.rowcount
    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:  # noqa: C901 -- acceptable complexity for a one-off script
    """Run the split_supersede change_type backfill."""
    parser = argparse.ArgumentParser(
        description=(
            "Backfill change_type for pre-existing split-superseded documents (#3510)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing to DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of loser rows to process (default: all).",
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
        losers = fetch_losers(conn, limit=args.limit, county=args.county)
        total = len(losers)
        logger.info(
            "Fetched superseded losers to process",
            total=total,
            dry_run=args.dry_run,
            county=args.county,
        )

        if not losers:
            logger.info(
                "No losers to process -- all superseded docs already backfilled"
            )
            return

        linked = 0
        unverified = 0
        county_counts: dict[str, int] = defaultdict(int)
        pending_sibling_updates: list[tuple[str, str]] = []
        pending_unverified_ids: list[str] = []

        def flush_batches() -> None:
            nonlocal linked, unverified
            if pending_sibling_updates:
                linked += apply_update_with_sibling(
                    conn, pending_sibling_updates, dry_run=args.dry_run
                )
                pending_sibling_updates.clear()
            if pending_unverified_ids:
                unverified += apply_update_unverified(
                    conn, pending_unverified_ids, dry_run=args.dry_run
                )
                pending_unverified_ids.clear()

        for loser in losers:
            loser_id = loser["loser_id"]
            loser_case_id = loser["loser_case_id"]
            county = loser["county"]

            sibling_id: str | None = None
            if loser_case_id is not None:
                candidates = find_sibling_candidates(conn, loser_id, loser_case_id)
                sibling_id = pick_sibling_for_loser(candidates)

            if sibling_id is not None:
                pending_sibling_updates.append((sibling_id, loser_id))
                county_counts[county] += 1
                logger.debug(
                    "queued sibling link",
                    loser_id=loser_id,
                    sibling_id=sibling_id,
                    county=county,
                )
            else:
                reason = (
                    "no_case_id" if loser_case_id is None else "no_sibling_with_rulings"
                )
                pending_unverified_ids.append(loser_id)
                logger.debug(
                    "queued unverified",
                    loser_id=loser_id,
                    county=county,
                    reason=reason,
                )

            if (
                len(pending_sibling_updates) + len(pending_unverified_ids)
                >= _BATCH_SIZE
            ):
                flush_batches()

        flush_batches()  # final partial batch

        logger.info(
            "backfill_split_supersede_summary",
            event="backfill_split_supersede_summary",
            total_scanned=total,
            linked=linked,
            unverified=unverified,
            per_county=dict(county_counts),
            dry_run=args.dry_run,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
