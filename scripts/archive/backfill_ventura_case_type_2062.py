#!/usr/bin/env python3
"""One-time backfill: set case_type for Ventura probate cases with unrecognised formats.

Ventura has three old case number formats that the extraction pipeline did not
previously recognise:

1. Long-digit + PR suffix: 200800332092PRCP, 201200427520PRCE, 202200572082PRGE
2. P-prefix: P057837, P076284
3. All-digit with no embedded type: 201600486599, 202200565994

Patterns 1 and 2 are now handled by new regex patterns in extract.py.
Pattern 3 is handled by the new title-based fallback (extract_case_type_from_title).

This backfill applies the new extraction logic to existing Ventura cases with
NULL case_type, closing the gap identified in #2062.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_ventura_case_type_2062.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_ventura_case_type_2062.py
"""

# venv: scraper-framework
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


# Case number patterns that identify probate cases.
# These match the patterns added to _CASE_TYPE_PREFIX_PATTERNS in extract.py.
_PROBATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Standard format: YYYY + PR + subtype
    (re.compile(r"^\d{4}PR", re.IGNORECASE), "probate"),
    # Old Ventura format: long digit string + PR + subtype
    (re.compile(r"^\d{8,}PR", re.IGNORECASE), "probate"),
    # P-prefix: P + 5-6 digits
    (re.compile(r"^P\d{5,}$", re.IGNORECASE), "probate"),
]

# Title patterns that indicate probate.
_PROBATE_TITLE_RE = re.compile(
    r"(?:"
    r"In\s+(?:the\s+)?(?:Matter|Re)\s+(?:of\b)"
    r"|Conservatorship\s+(?:of\b)"
    r"|Guardianship\s+(?:of\b)"
    r"|Estate\s+(?:of\b)"
    r"|Trust\s+(?:of\b)"
    r"|Petition\s+(?:for\s+)?(?:Probate|Letters)"
    r")",
    re.IGNORECASE,
)


def _infer_case_type(case_number: str, case_title: str | None) -> str | None:
    """Infer case type from a Ventura case number or title."""
    if case_number:
        for pattern, case_type in _PROBATE_PATTERNS:
            if pattern.match(case_number):
                return case_type
    if case_title and _PROBATE_TITLE_RE.search(case_title):
        return "probate"
    return None


def run_backfill(conn: psycopg.Connection, *, dry_run: bool = True) -> int:
    """Set case_type for Ventura cases where it is NULL and inferable.

    Returns the number of rows updated.
    """
    with conn.cursor() as cur:
        # Report current state.
        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN c.case_type IS NOT NULL THEN 1 END) AS with_type
            FROM cases c
            JOIN courts ct ON ct.id = c.court_id
            WHERE ct.county = 'Ventura'
            """
        )
        row = cur.fetchone()
        total, with_type = row[0], row[1]
        logger.info(
            "Ventura cases: %d total, %d with case_type, %d missing (%.1f%% complete)",
            total,
            with_type,
            total - with_type,
            100.0 * with_type / total if total > 0 else 0,
        )

        if total == with_type:
            logger.info("All Ventura cases already have case_type. Nothing to do.")
            return 0

        # Fetch cases with NULL case_type.
        cur.execute(
            """
            SELECT c.id, c.case_number, c.case_title
            FROM cases c
            JOIN courts ct ON ct.id = c.court_id
            WHERE ct.county = 'Ventura'
              AND c.case_type IS NULL
            """
        )
        rows = cur.fetchall()
        logger.info("Found %d Ventura cases with NULL case_type", len(rows))

        updated = 0
        skipped = 0
        for case_id, case_number, case_title in rows:
            inferred = _infer_case_type(case_number, case_title)
            if inferred is None:
                logger.warning(
                    "Cannot infer case_type: case_number=%s case_title=%s (case_id=%s)",
                    case_number,
                    case_title,
                    case_id,
                )
                skipped += 1
                continue
            logger.info(
                "%s case_type=%s for case_number=%s (case_title=%s)",
                "Would set" if dry_run else "Setting",
                inferred,
                case_number,
                case_title,
            )
            if not dry_run:
                cur.execute(
                    "UPDATE cases SET case_type = %s WHERE id = %s",
                    (inferred, case_id),
                )
            updated += 1

        logger.info(
            "%s %d Ventura cases, skipped %d (unrecognised format)",
            "Would update" if dry_run else "Updated",
            updated,
            skipped,
        )

        # Post-update report.
        if not dry_run:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(CASE WHEN c.case_type IS NOT NULL THEN 1 END) AS with_type
                FROM cases c
                JOIN courts ct ON ct.id = c.court_id
                WHERE ct.county = 'Ventura'
                """
            )
            row = cur.fetchone()
            total_after, with_type_after = row[0], row[1]
            completeness = (
                100.0 * with_type_after / total_after if total_after > 0 else 0
            )
            logger.info(
                "After update: %d/%d Ventura cases have case_type (%.1f%%)",
                with_type_after,
                total_after,
                completeness,
            )

    if dry_run:
        conn.rollback()
        logger.info("DRY RUN — no changes committed.")
    else:
        conn.commit()
        logger.info("Changes committed.")

    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill case_type for Ventura probate cases (#2062)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        updated = run_backfill(conn, dry_run=args.dry_run)

    mode = "DRY RUN" if args.dry_run else "APPLIED"
    logger.info("Backfill complete (%s): %d cases updated", mode, updated)


if __name__ == "__main__":
    main()
