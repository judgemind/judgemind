#!/usr/bin/env python3
"""Backfill case_title on existing cases from ruling_text.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and extracts party names from ruling
text to populate the cases.case_title column.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/backfill_case_titles.py

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

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Title extraction regex
# ---------------------------------------------------------------------------

# Matches party caption blocks found in LA ruling text, e.g.:
#   "EMELITA BUENAVENTURA, Plaintiff(s), vs. CITY OF PASADENA, Defendant(s)."
# Also handles Petitioner/Respondent and Cross-Complainant/Cross-Defendant.
_CASE_TITLE_RE = re.compile(
    r"^(?P<plaintiff>.+?),?\s*\n\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?,?"
    r"\s+vs\.\s+"
    r"(?P<defendant>.+?),?\s*\n\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\.?",
    re.DOTALL | re.MULTILINE,
)

# Fallback: single-line pattern for text that has been flattened or
# doesn't have newlines before the party designations. Matches e.g.:
#   "EMELITA BUENAVENTURA, Plaintiff(s), vs. CITY OF PASADENA, Defendant(s)."
_CASE_TITLE_FLAT_RE = re.compile(
    r"(?P<plaintiff>[A-Z][A-Z\s,.'()-]+?),?\s*"
    r"(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?,?\s+"
    r"vs\.\s+"
    r"(?P<defendant>[A-Z][A-Z\s,.'()-]+?),?\s*"
    r"(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\.?",
    re.DOTALL,
)


def extract_case_title(ruling_text: str) -> str | None:
    """Extract a case title from ruling text using regex.

    Returns a title like "Buenaventura v. City Of Pasadena" or None if
    no party caption block is found.
    """
    m = _CASE_TITLE_RE.search(ruling_text)
    if m is None:
        m = _CASE_TITLE_FLAT_RE.search(ruling_text)
    if m is None:
        return None

    plaintiff = " ".join(m.group("plaintiff").split()).strip().rstrip(",")
    defendant = " ".join(m.group("defendant").split()).strip().rstrip(",")

    if not plaintiff or not defendant:
        return None

    return f"{plaintiff.title()} v. {defendant.title()}"


# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT c.id, r.ruling_text
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
    ORDER BY c.created_at
    LIMIT %s OFFSET %s
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
    offset: int = 0,
) -> tuple[int, int]:
    """Process one batch of cases.  Returns (processed, updated) counts."""
    processed = 0
    updated = 0

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (batch_size, offset))
        rows = cur.fetchall()

    if not rows:
        return 0, 0

    for case_id, ruling_text in rows:
        processed += 1

        title = extract_case_title(ruling_text)
        if title is None:
            logger.debug("No title extracted for case %s", case_id)
            continue

        logger.info("Case %s -> %s", case_id, title)

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (title, str(case_id)))
        updated += 1

    return processed, updated


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
    offset = 0

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated = backfill_batch(conn, effective_batch, offset)
            total_processed += processed
            total_updated += updated

            logger.info(
                "Batch: processed=%d updated=%d (total: %d/%d)",
                processed,
                updated,
                total_processed,
                total_updated,
            )

            if processed < effective_batch:
                # Last batch — no more rows
                break

            offset += effective_batch

        if dry_run:
            conn.rollback()
            logger.info("DRY RUN — rolled back all changes")
        else:
            conn.commit()
            logger.info("Committed all changes")

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
