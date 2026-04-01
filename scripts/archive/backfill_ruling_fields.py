#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill judge_id, motion_type, and outcome on existing rulings.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and reprocesses ruling_text through
the extraction functions added in PRs #232 and #233.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_ruling_fields.py

Each batch is committed independently so that progress is saved
incrementally.  If the connection drops mid-run, already-committed
batches are preserved and the script can be safely re-run (it is
idempotent — it only updates rows that still have NULL fields).

Options:
    --dry-run            Print what would be updated without writing to the database.
    --batch-size N       Number of rulings to process per batch (default: 100).
    --limit N            Maximum total rulings to process (default: unlimited).
    --validate-schema    Validate SQL column references against live DB before running.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402

from ingestion.db import resolve_judge, upsert_case_judge  # noqa: E402
# NOTE: extract_motion_type and extract_outcome were removed in #2178.  # noqa: E402
from ingestion.extract import extract_judge_name  # noqa: E402
from validation.schema import add_validate_schema_flag, validate_script_queries  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

FETCH_QUERY = """
    SELECT r.id, r.ruling_text, r.court_id, r.judge_id, r.outcome, r.motion_type,
           r.case_id, r.hearing_date, r.created_at
    FROM rulings r
    WHERE r.ruling_text IS NOT NULL
      AND (r.judge_id IS NULL OR r.outcome IS NULL OR r.motion_type IS NULL)
    AND (r.created_at, r.id) > (%s, %s)
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE rulings
    SET judge_id     = COALESCE(%s::uuid, judge_id),
        outcome      = COALESCE(%s::ruling_outcome, outcome),
        motion_type  = COALESCE(%s, motion_type)
    WHERE id = %s::uuid
"""


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID),
) -> tuple[int, int, tuple[datetime, str]]:
    """Process one batch of rulings.  Returns (processed, updated, next_cursor)."""
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for (
        ruling_id,
        ruling_text,
        court_id,
        existing_judge_id,
        existing_outcome,
        existing_motion_type,
        case_id,
        hearing_date,
        created_at,
    ) in rows:
        processed += 1
        next_cursor = (created_at, str(ruling_id))
        new_outcome = None
        new_motion_type = None
        new_judge_id = None

        # Only extract fields that are currently NULL
        if existing_outcome is None:
            new_outcome = extract_outcome(ruling_text)

        if existing_motion_type is None:
            new_motion_type = extract_motion_type(ruling_text)

        if existing_judge_id is None:
            raw_name = extract_judge_name(ruling_text)
            if raw_name:
                new_judge_id = resolve_judge(conn, raw_name, str(court_id))

        # Skip if nothing new to write
        if new_outcome is None and new_motion_type is None and new_judge_id is None:
            continue

        with conn.cursor() as cur:
            cur.execute(
                UPDATE_QUERY,
                (new_judge_id, new_outcome, new_motion_type, str(ruling_id)),
            )

        # Create case-judge association so Case.judges resolver works.
        # Use newly resolved judge_id, or fall back to the existing one on
        # the ruling row (the ruling may already have a judge_id but be
        # missing outcome/motion_type — we still need the case_judges link).
        effective_judge_id = new_judge_id or (
            str(existing_judge_id) if existing_judge_id else None
        )
        if effective_judge_id and case_id:
            upsert_case_judge(conn, str(case_id), effective_judge_id, hearing_date)

        updated += 1

    return processed, updated, next_cursor


CASE_JUDGES_BACKFILL_QUERY = """
    SELECT DISTINCT r.case_id, r.judge_id, r.hearing_date
    FROM rulings r
    WHERE r.judge_id IS NOT NULL
      AND r.case_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM case_judges cj
          WHERE cj.case_id = r.case_id AND cj.judge_id = r.judge_id
      )
    AND r.case_id > %s::uuid
    ORDER BY r.case_id
    LIMIT %s
"""


def backfill_case_judges_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[int, str]:
    """Create missing case_judges rows for rulings that have judge_id set.

    Returns (created_count, next_cursor).
    """
    with conn.cursor() as cur:
        cur.execute(CASE_JUDGES_BACKFILL_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, cursor

    created = 0
    next_cursor = cursor
    for case_id, judge_id, hearing_date in rows:
        upsert_case_judge(conn, str(case_id), str(judge_id), hearing_date)
        next_cursor = str(case_id)
        created += 1

    return created, next_cursor


def run_backfill(
    dsn: str,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full backfill.  Returns summary stats."""
    total_processed = 0
    total_updated = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated, cursor = backfill_batch(conn, effective_batch, cursor)
            total_processed += processed
            total_updated += updated

            # Commit (or rollback) after each batch so that progress is
            # saved incrementally and not lost if the connection drops.
            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d updated=%d (total: %d/%d)%s",
                processed,
                updated,
                total_processed,
                total_updated,
                " [dry-run, rolled back]" if dry_run else " [committed]",
            )

            if processed < effective_batch:
                # Last batch — no more rows
                break

        # Second pass: create case_judges rows for any rulings that already
        # have judge_id set (from a previous backfill or the ingestion
        # pipeline) but are missing the case_judges link.
        cj_cursor: str = _CURSOR_MIN_UUID
        total_case_judges = 0
        while True:
            created, cj_cursor = backfill_case_judges_batch(conn, batch_size, cj_cursor)
            total_case_judges += created

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            if created > 0:
                logger.info(
                    "case_judges: created=%d (total: %d)%s",
                    created,
                    total_case_judges,
                    " [dry-run, rolled back]" if dry_run else " [committed]",
                )

            if created < batch_size:
                break

        if total_case_judges > 0:
            logger.info(
                "case_judges backfill complete: %d links created", total_case_judges
            )

    stats = {
        "total_processed": total_processed,
        "total_updated": total_updated,
        "total_case_judges_created": total_case_judges,
    }
    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill judge_id, outcome, and motion_type on existing rulings.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing to the database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of rulings per batch (default: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total rulings to process.",
    )
    add_validate_schema_flag(parser)
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    if args.validate_schema:
        with psycopg.connect(dsn) as conn:
            errors = validate_script_queries(conn, sys.modules[__name__])
        if errors:
            for e in errors:
                logger.error("Schema validation failed: %s", e)
            sys.exit(1)
        logger.info("Schema validation passed — all column references are valid")

    stats = run_backfill(
        dsn,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Backfill complete: %d rulings processed, %d updated",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
