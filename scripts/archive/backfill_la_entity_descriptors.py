#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill LA case titles that contain entity descriptors (#1267).

Finds LA cases with case_title longer than 150 characters (caused by entity
descriptors like "An Individual", "A California Corporation", etc.) and cleans
them using the same sanitization logic as the live scraper.

Strategy:
  1. Apply ``_sanitize_title()`` directly to the existing title.
  2. If that fails (returns None), re-extract from ruling_text using the
     caption block and moving/responding party patterns.

Usage:
    scripts/ecs-run.sh --script scripts/backfill_la_entity_descriptors.py
    scripts/ecs-run.sh --script scripts/backfill_la_entity_descriptors.py -- --dry-run
    scripts/ecs-run.sh --script scripts/backfill_la_entity_descriptors.py -- --threshold 120

Options:
    --dry-run      Print what would be updated without writing to the database.
    --threshold N  Title length threshold (default: 150).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg
from courts.ca.la_tentatives import _sanitize_title
from courts.ca.la_title_utils import extract_clean_title

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# For the backfill, we use a more lenient length limit.  The live scraper
# enforces 120 chars, but for existing data we accept anything that is at
# least shorter than the original title after stripping entity descriptors.
_BACKFILL_MAX_TITLE_LENGTH = 250


def sanitize_title(raw_title: str | None) -> str | None:
    """Clean a raw case title: strip entity descriptors, excess length.

    Returns ``None`` if the title is invalid (contains department header
    boilerplate, is too short after cleaning, or is empty).

    Delegates to ``la_tentatives._sanitize_title()`` with the default max length.
    """
    return _sanitize_title(raw_title)


def sanitize_title_lenient(raw_title: str | None) -> str | None:
    """Like ``sanitize_title`` but with a lenient max-length for backfill use.

    Accepts titles up to ``_BACKFILL_MAX_TITLE_LENGTH`` after cleaning,
    because many multi-party LA cases are legitimately > 120 chars even
    after stripping entity descriptors.
    """
    return _sanitize_title(raw_title, max_length=_BACKFILL_MAX_TITLE_LENGTH)


def clean_case_title(
    old_title: str,
    ruling_text: str | None,
    *,
    max_length: int | None = None,
) -> str | None:
    """Determine the best cleaned title for a case.

    Strategy 1: Apply ``_sanitize_title()`` directly to the existing title.
    Strategy 2: Re-extract from ruling text using caption/party patterns.

    Args:
        old_title: The current case title.
        ruling_text: The ruling text for fallback extraction, or None.
        max_length: Maximum allowed title length after cleaning.  Defaults to
            ``_BACKFILL_MAX_TITLE_LENGTH`` (250).

    Returns ``None`` if no clean title can be determined.
    """
    target = max_length if max_length is not None else _BACKFILL_MAX_TITLE_LENGTH

    # Strategy 1: sanitize the existing title directly
    cleaned = _sanitize_title(old_title, max_length=target)
    if cleaned is not None:
        return cleaned

    # Strategy 2: re-extract from ruling text (use lenient max length so the
    # fallback path accepts the same range of titles as strategy 1)
    if ruling_text:
        return extract_clean_title(ruling_text, max_length=_BACKFILL_MAX_TITLE_LENGTH)

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Find and fix LA case titles with entity descriptors (> threshold chars)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--threshold",
        type=int,
        default=150,
        help="Title length threshold for fetching cases (default: 150)",
    )
    parser.add_argument(
        "--target-length",
        type=int,
        default=150,
        help="Max allowed title length after cleaning (default: 150). "
        "Titles still exceeding this after descriptor stripping are "
        "truncated to first party + et al.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.case_number, c.case_title
                FROM cases c
                JOIN courts ct ON c.court_id = ct.id
                WHERE ct.county = 'Los Angeles'
                  AND length(c.case_title) > %s
                ORDER BY c.case_number
                """,
                (args.threshold,),
            )
            long_cases = cur.fetchall()

        logger.info(
            "Found %d LA cases with title > %d chars",
            len(long_cases),
            args.threshold,
        )

        fixed = 0
        skipped = 0
        for case_id, case_number, old_title in long_cases:
            # Fetch the ruling text for fallback extraction
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.ruling_text
                    FROM rulings r
                    JOIN documents d ON r.document_id = d.id
                    WHERE d.case_id = %s
                    ORDER BY r.hearing_date DESC NULLS LAST
                    LIMIT 1
                    """,
                    (case_id,),
                )
                row = cur.fetchone()

            ruling_text = row[0] if row and row[0] else None
            new_title = clean_case_title(
                old_title, ruling_text, max_length=args.target_length
            )

            if new_title is None:
                logger.warning(
                    "Could not clean title for case %s (%s): %s",
                    case_id,
                    case_number,
                    old_title[:100],
                )
                skipped += 1
                continue

            if new_title == old_title:
                logger.info(
                    "Title unchanged for case %s (%s), skipping",
                    case_id,
                    case_number,
                )
                skipped += 1
                continue

            logger.info(
                "Case %s (%s):\n  OLD: %s\n  NEW: %s",
                case_id,
                case_number,
                old_title[:120],
                new_title,
            )

            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cases SET case_title = %s WHERE id = %s",
                        (new_title, case_id),
                    )
                conn.commit()
            fixed += 1

        logger.info(
            "Done: %d fixed, %d skipped%s",
            fixed,
            skipped,
            " (dry-run)" if args.dry_run else "",
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
