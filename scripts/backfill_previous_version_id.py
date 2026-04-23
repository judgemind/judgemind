#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill previous_version_id for pre-#2569 superseded documents (#2653).

The #2569 commit wired up the live ingestion path to populate
previous_version_id = <winner_id> and change_type = 'duplicate_content' on
content-hash dedup losers.  This script performs the matching backfill for
losers that were superseded by the earlier #2458 code path — those rows have
status='superseded' but previous_version_id IS NULL AND change_type IS NULL.

Matching strategy: for each loser, look up active rulings in the loser's
case_id (using derived.rulings, filtered to ruling_text_hash IS NOT NULL to
mirror the partial unique index semantics).  If the case has exactly one
distinct ruling_text_hash the winner's document_id is unambiguous and we link
it.  If multiple distinct hashes exist the intended match cannot be recovered
without re-extracting from S3, so we log WARN and skip (ambiguous).  If no
rulings exist we log WARN and skip (no_winner).

This is an ECS oneshot script: no local imports from scripts/, only stdlib +
installed packages.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_previous_version_id.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_previous_version_id.py -- --limit 100
    scripts/ecs-run-task.sh scripts/backfill_previous_version_id.py

Options:
    --dry-run       Show what would be updated without writing to DB.
    --limit N       Maximum number of loser rows to process (default: all).
"""

from __future__ import annotations

import argparse
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

# Sentinel string returned by the SQL query when a case has 2+ distinct
# ruling_text_hashes — the winner cannot be determined without S3 re-extraction.
_AMBIGUOUS_SENTINEL = "AMBIGUOUS"


def fetch_loser_winner_pairs(
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
) -> list[dict]:
    """Fetch loser documents paired with their unambiguous winner (or sentinel).

    Returns a list of dicts with keys:
        loser_id   (str)  — document id of the superseded loser
        case_id    (str)  — case the loser belongs to
        s3_key     (str)  — loser's S3 key (for logging)
        county     (str)  — court county (for per-county reporting)
        winner_id  (str | None)
            - UUID string when exactly one distinct ruling_text_hash exists for
              the case (unambiguous match)
            - 'AMBIGUOUS' when 2+ distinct hashes exist
            - None when no rulings exist (no_winner)

    The SELECT already excludes rows where previous_version_id IS NOT NULL or
    change_type IS NOT NULL, so re-running the script is idempotent.
    """
    query = """
        WITH winners AS (
            SELECT
                r.case_id,
                COUNT(DISTINCT r.ruling_text_hash) AS distinct_hashes,
                MIN(r.document_id::text) AS winner_id
            FROM derived.rulings r
            WHERE r.ruling_text_hash IS NOT NULL
            GROUP BY r.case_id
        )
        SELECT
            d.id::text                              AS loser_id,
            d.case_id::text                         AS case_id,
            d.s3_key                                AS s3_key,
            co.county                               AS county,
            CASE
                WHEN w.case_id IS NULL          THEN NULL
                WHEN w.distinct_hashes = 1      THEN w.winner_id
                ELSE 'AMBIGUOUS'
            END                                     AS winner_id
        FROM derived.documents d
        JOIN derived.courts co ON co.id = d.court_id
        LEFT JOIN winners w ON w.case_id = d.case_id
        WHERE d.status = 'superseded'
          AND d.previous_version_id IS NULL
          AND d.change_type IS NULL
        ORDER BY co.county, d.id
    """
    if limit is not None:
        query += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    return [
        {
            "loser_id": row[0],
            "case_id": row[1],
            "s3_key": row[2],
            "county": row[3],
            "winner_id": row[4],
        }
        for row in rows
    ]


def apply_link(
    conn: psycopg.Connection,
    loser_id: str,
    winner_id: str,
) -> None:
    """UPDATE a single loser document to set previous_version_id + change_type.

    Mirrors the live writer shape in packages/scraper-framework/src/ingestion/db.py
    lines 1783-1791.  The WHERE clause guards against accidentally re-updating a
    row that was already linked since the loser set was fetched.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE derived.documents "
            "SET previous_version_id = %s::uuid, "
            "    change_type = 'duplicate_content' "
            "WHERE id = %s::uuid "
            "  AND previous_version_id IS NULL "
            "  AND change_type IS NULL",
            (winner_id, loser_id),
        )


def summarize(rows: list[dict]) -> dict:
    """Aggregate outcome rows into a summary dict.

    Args:
        rows: list of dicts, each with keys 'county' and 'outcome'.
              Outcome values: 'updated', 'ambiguous', 'no_winner'.

    Returns:
        {
            'updated_per_county': {county: count, ...},
            'ambiguous': int,
            'no_winner': int,
        }
    """
    updated_per_county: dict[str, int] = {}
    ambiguous = 0
    no_winner = 0

    for row in rows:
        outcome = row["outcome"]
        county = row["county"]
        if outcome == "updated":
            updated_per_county[county] = updated_per_county.get(county, 0) + 1
        elif outcome == "ambiguous":
            ambiguous += 1
        elif outcome == "no_winner":
            no_winner += 1

    return {
        "updated_per_county": updated_per_county,
        "ambiguous": ambiguous,
        "no_winner": no_winner,
    }


def main() -> None:
    """Run the previous_version_id backfill."""
    parser = argparse.ArgumentParser(
        description="Backfill previous_version_id for pre-#2569 superseded documents.",
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
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    conn = psycopg.connect(db_url, autocommit=False)
    try:
        pairs = fetch_loser_winner_pairs(conn, limit=args.limit)
        logger.info("Fetched loser rows", count=len(pairs))

        if not pairs:
            logger.info(
                "No unlinked superseded documents found — backfill already complete"
            )
            return

        outcome_rows: list[dict] = []

        for pair in pairs:
            loser_id = pair["loser_id"]
            case_id = pair["case_id"]
            s3_key = pair["s3_key"]
            county = pair["county"]
            winner_id = pair["winner_id"]

            if winner_id is None:
                logger.warning(
                    "no_winner: case has zero rulings with a text hash",
                    loser_id=loser_id,
                    case_id=case_id,
                    s3_key=s3_key,
                    county=county,
                )
                outcome_rows.append({"county": county, "outcome": "no_winner"})
                continue

            if winner_id == _AMBIGUOUS_SENTINEL:
                logger.warning(
                    "ambiguous: case has multiple distinct ruling hashes — "
                    "cannot determine winner without S3 re-extraction",
                    loser_id=loser_id,
                    case_id=case_id,
                    s3_key=s3_key,
                    county=county,
                )
                outcome_rows.append({"county": county, "outcome": "ambiguous"})
                continue

            # Unambiguous match
            if args.dry_run:
                logger.info(
                    "DRY RUN: would link loser to winner",
                    loser_id=loser_id,
                    winner_id=winner_id,
                    county=county,
                )
            else:
                apply_link(conn, loser_id, winner_id)
                conn.commit()
                logger.info(
                    "Linked loser to winner",
                    loser_id=loser_id,
                    winner_id=winner_id,
                    county=county,
                )

            outcome_rows.append({"county": county, "outcome": "updated"})

        summary = summarize(outcome_rows)
        logger.info(
            "Backfill complete",
            dry_run=args.dry_run,
            total=len(pairs),
            updated_per_county=summary["updated_per_county"],
            ambiguous=summary["ambiguous"],
            no_winner=summary["no_winner"],
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
