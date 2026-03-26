#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill case_title on existing cases from ruling_text.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and extracts party names from ruling
text to populate the cases.case_title column.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/backfill_case_titles.py

Each batch is committed independently so that progress is saved
incrementally.  If the connection drops mid-run, already-committed
batches are preserved and the script can be safely re-run (it is
idempotent — it only updates rows where case_title IS NULL).

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of cases to process per batch (default: 100).
    --limit N       Maximum total cases to process (default: unlimited).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime

import psycopg

# Title extraction is delegated to the shared function in the scraper
# framework's ingestion package.  This avoids duplicating extraction
# logic across files (#1405).
from ingestion.extract import extract_case_title

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# Re-export extract_case_title so that existing test imports
# (``backfill.extract_case_title``) continue to work without changes.
__all__ = [
    "extract_case_title",
    "is_plausible_case_title",
    "backfill_batch",
    "run_backfill",
]


# ---------------------------------------------------------------------------
# Title validation — safety net between extraction and DB write (#1958)
# ---------------------------------------------------------------------------

# Common verb/preposition prefixes that indicate motion/procedural text,
# not actual case titles.  Checked case-insensitively against the first word.
_IMPLAUSIBLE_PREFIXES = (
    "to",
    "for",
    "by",
    "re:",
    "on",
)

# Phrases that should never appear in a valid case title.
_IMPLAUSIBLE_FRAGMENTS_RE = re.compile(
    r"\b(?:GRANTED|DENIED|CONTINUED|TENTATIVE RULING|OVERRULED|SUSTAINED"
    r"|MOOT|OFF CALENDAR|HEARING|MOTION|DEMURRER|ORDER)\b",
    re.IGNORECASE,
)

# "In the" at the start — unless followed by "Matter of" (which is a
# legitimate non-adversarial case title pattern).
_IN_THE_NOT_MATTER_RE = re.compile(
    r"^In\s+the\b(?!\s+Matter\s+of)",
    re.IGNORECASE,
)

# Length bounds
_MIN_TITLE_LENGTH = 3
_MAX_TITLE_LENGTH = 120


def is_plausible_case_title(title: str) -> bool:
    """Return True if *title* looks like a real case title, not motion text.

    This guards against false positives from ``extract_case_title()`` where
    regex patterns match motion/procedural language (e.g.
    ``"To Respond, Without Objections"``) instead of party names (#1958).

    The function is intentionally conservative — it rejects obvious junk
    while allowing legitimate edge cases through.
    """
    if not title or not title.strip():
        return False

    stripped = title.strip()

    # Length check
    if len(stripped) < _MIN_TITLE_LENGTH or len(stripped) > _MAX_TITLE_LENGTH:
        return False

    # Check first word against implausible prefixes
    first_word = stripped.split()[0].rstrip(":,.")
    if first_word.lower() in _IMPLAUSIBLE_PREFIXES:
        # Allow "In the Matter of" — it's a legitimate case title pattern
        if first_word.lower() == "in" and re.match(
            r"^In\s+(?:re|the\s+Matter\s+of)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        return False

    # Reject "In the <something>" unless it's "In the Matter of"
    if _IN_THE_NOT_MATTER_RE.match(stripped):
        return False

    # Reject titles containing ruling/procedural fragments
    if _IMPLAUSIBLE_FRAGMENTS_RE.search(stripped):
        return False

    return True


# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

FETCH_QUERY = """
    SELECT c.id, r.ruling_text, c.created_at
    FROM cases c
    JOIN LATERAL (
        SELECT r2.ruling_text
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE c.case_title IS NULL
    AND (c.created_at, c.id) > (%s, %s)
    ORDER BY c.created_at, c.id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE cases
    SET case_title = %s,
        updated_at = NOW()
    WHERE id = %s::uuid
      AND case_title IS NULL
"""


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID),
) -> tuple[int, int, tuple[datetime, str]]:
    """Process one batch of cases.  Returns (processed, updated, next_cursor)."""
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for case_id, ruling_text, created_at in rows:
        processed += 1
        next_cursor = (created_at, str(case_id))

        title = extract_case_title(ruling_text)
        if title is None:
            logger.debug("No title extracted for case %s", case_id)
            continue

        if not is_plausible_case_title(title):
            logger.warning("Rejected implausible title for case %s: %r", case_id, title)
            continue

        logger.info("Case %s -> %s", case_id, title)

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (title, str(case_id)))
        updated += 1

    return processed, updated, next_cursor


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

    stats = {
        "total_processed": total_processed,
        "total_updated": total_updated,
    }
    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill case_title on existing cases from ruling_text.",
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
        help="Number of cases per batch (default: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total cases to process.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_backfill(
        dsn,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Backfill complete: %d cases processed, %d updated",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
