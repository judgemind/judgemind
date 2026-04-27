#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill previous_version_id and change_type for pre-existing content-hash
dedup losers (#2653, parent #2569, live-code fix in #2650).

Finds ``derived.documents`` rows where ``status='superseded'``,
``previous_version_id IS NULL``, and ``change_type IS NULL`` — these are
pre-existing losers that were marked superseded before the live-code fix in
#2650 started populating those fields.

For each loser it attempts to find the winner document via:
  1. (preferred) ``case_id`` match — find all active documents sharing the
     same ``case_id`` as the loser and use ``pick_winner_for_loser`` to
     resolve when exactly one candidate exists.
  2. (fallback, when loser has ``case_id IS NULL``) staging.captures sibling
     via ``content_hash`` -> ``promoted_document_id``.

If exactly one candidate is found, it UPDATEs
``previous_version_id = winner_id, change_type = 'duplicate_content'``.
If zero or more-than-one candidates are found, it logs WARN and skips.

The UPDATE WHERE clause (``previous_version_id IS NULL AND change_type IS
NULL``) makes the script fully idempotent and safe against concurrent runs.

This is an ECS oneshot script: no local imports from scripts/, only stdlib +
installed packages.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_previous_version_id.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_previous_version_id.py -- --dry-run --limit 5
    scripts/ecs-run-task.sh scripts/backfill_previous_version_id.py -- --county orange
    scripts/ecs-run-task.sh scripts/backfill_previous_version_id.py

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

# SELECT all pre-existing superseded losers (no previous_version_id yet).
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

# Find active siblings sharing the same case_id (primary strategy).
_SELECT_CASE_CANDIDATES = """
    SELECT id::text
    FROM derived.documents
    WHERE case_id = %s::uuid
      AND status = 'active'
      AND id != %s::uuid
"""

# Fallback: find active siblings via staging.captures content_hash.
_SELECT_CAPTURE_CANDIDATES = """
    SELECT DISTINCT cap2.promoted_document_id::text AS id
    FROM staging.captures cap1
    JOIN staging.captures cap2
        ON  cap2.content_hash = cap1.content_hash
        AND cap2.promoted_document_id IS NOT NULL
        AND cap2.promoted_document_id != %s::uuid
    JOIN derived.documents d
        ON d.id = cap2.promoted_document_id
        AND d.status = 'active'
    WHERE cap1.promoted_document_id = %s::uuid
"""

# Idempotent UPDATE -- trailing WHERE guard prevents double-writes.
_UPDATE_LOSER = (
    "UPDATE derived.documents "
    "SET previous_version_id = %s::uuid, "
    "    change_type = 'duplicate_content' "
    "WHERE id = %s::uuid "
    "  AND previous_version_id IS NULL "
    "  AND change_type IS NULL"
)

_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Pure helper -- testable without a live DB
# ---------------------------------------------------------------------------


def pick_winner_for_loser(candidates: list[dict]) -> str | None:
    """Return the winner document id, or None if the decision is ambiguous.

    Parameters
    ----------
    candidates:
        Each dict must have at least an ``"id"`` key (str UUID).  The list
        should contain only *active* sibling documents for the loser.

    Returns
    -------
    str | None
        The winner id when exactly one candidate exists, else ``None``
        (covers both the zero-candidate and the >=2-candidate cases).
    """
    if len(candidates) == 1:
        return candidates[0]["id"]
    # 0 candidates -> no winner; >=2 -> ambiguous
    return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def fetch_losers(
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
    county: str | None = None,
) -> list[dict]:
    """Fetch pre-existing superseded losers that need backfilling."""
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


def find_candidates_by_case_id(
    conn: psycopg.Connection,
    loser_id: str,
    case_id: str,
) -> list[dict]:
    """Find active documents sharing case_id with the loser."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_CASE_CANDIDATES, (case_id, loser_id))
        rows = cur.fetchall()
    return [{"id": row[0]} for row in rows]


def find_candidates_by_capture_hash(
    conn: psycopg.Connection,
    loser_id: str,
) -> list[dict]:
    """Fallback: find active documents via staging.captures content_hash.

    Used when the loser's case_id is NULL and the primary strategy is
    unavailable.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_CAPTURE_CANDIDATES, (loser_id, loser_id))
        rows = cur.fetchall()
    return [{"id": row[0]} for row in rows]


def apply_update_batch(
    conn: psycopg.Connection,
    updates: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> int:
    """Execute a batch of UPDATE statements inside a single transaction.

    Parameters
    ----------
    updates:
        List of ``(winner_id, loser_id)`` tuples.
    dry_run:
        If ``True``, log the intended updates but do not execute.

    Returns
    -------
    int
        Number of intended links (dry-run) or rows actually updated (write).
    """
    if dry_run:
        for winner_id, loser_id in updates:
            logger.info(
                "DRY RUN: would link loser to winner",
                loser_id=loser_id,
                winner_id=winner_id,
            )
        return len(updates)

    updated = 0
    with conn.cursor() as cur:
        for winner_id, loser_id in updates:
            cur.execute(_UPDATE_LOSER, (winner_id, loser_id))
            updated += cur.rowcount
    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:  # noqa: C901 -- acceptable complexity for a one-off script
    """Run the previous_version_id backfill."""
    parser = argparse.ArgumentParser(
        description=(
            "Backfill previous_version_id + change_type='duplicate_content' "
            "on pre-existing content-hash dedup losers (#2653)."
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
            logger.info("No losers to process -- all superseded docs already linked")
            return

        linked = 0
        unlinkable = 0
        unlinkable_reasons: dict[str, int] = defaultdict(int)
        county_counts: dict[str, int] = defaultdict(int)
        pending_updates: list[tuple[str, str]] = []

        def flush_batch() -> None:
            nonlocal linked
            if pending_updates:
                linked += apply_update_batch(
                    conn, pending_updates, dry_run=args.dry_run
                )
                pending_updates.clear()

        for loser in losers:
            loser_id = loser["loser_id"]
            loser_case_id = loser["loser_case_id"]
            county = loser["county"]

            if loser_case_id is not None:
                candidates = find_candidates_by_case_id(conn, loser_id, loser_case_id)
                strategy = "case_id"
            else:
                candidates = find_candidates_by_capture_hash(conn, loser_id)
                strategy = "capture_hash"

            winner_id = pick_winner_for_loser(candidates)

            if winner_id is None:
                if len(candidates) == 0:
                    reason = "no_candidates"
                else:
                    reason = "ambiguous"
                logger.warning(
                    "unlinkable",
                    loser_id=loser_id,
                    county=county,
                    strategy=strategy,
                    reason=reason,
                    candidate_count=len(candidates),
                )
                unlinkable += 1
                unlinkable_reasons[reason] += 1
                continue

            pending_updates.append((winner_id, loser_id))
            county_counts[county] += 1

            if len(pending_updates) >= _BATCH_SIZE:
                flush_batch()

        flush_batch()  # final partial batch

        logger.info(
            "backfill_previous_version_id_summary",
            total_scanned=total,
            linked=linked,
            unlinkable=unlinkable,
            unlinkable_reasons=dict(unlinkable_reasons),
            per_county=dict(county_counts),
            dry_run=args.dry_run,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
