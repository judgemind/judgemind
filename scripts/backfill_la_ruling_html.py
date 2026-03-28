#!/usr/bin/env python3
"""Backfill ruling_text_html for LA rulings from archived S3 HTML.

LA rulings are captured as HTML.  This script reads the archived HTML
from S3, extracts and sanitizes the per-case HTML fragment, and updates
the ``ruling_text_html`` column.  Unlike the generic LLM-based backfill
(``backfill_ruling_html.py``), this script is free — no LLM calls needed.

Usage:
    scripts/ecs-run-task.sh scripts/backfill_la_ruling_html.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_la_ruling_html.py -- --limit 100

Options:
    --dry-run       Print what would be updated without writing to the database.
    --limit N       Process at most N rulings (default: unlimited).
    --batch-size N  Number of rulings to fetch per batch (default: 50).
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import psycopg  # noqa: E402

from courts.ca.la_tentatives import (  # noqa: E402
    _CASE_NUMBER_RE,
    _split_cases_html,
    sanitize_ruling_html,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT r.id, d.s3_key, d.s3_bucket, r.created_at,
           c.case_number
    FROM rulings r
    JOIN documents d ON r.document_id = d.id
    JOIN cases c ON r.case_id = c.id
    JOIN courts ct ON c.court_id = ct.id
    WHERE ct.county = 'Los Angeles'
      AND r.ruling_text_html IS NULL
      AND r.ruling_text IS NOT NULL
      AND d.s3_key IS NOT NULL
      AND d.format = 'html'
      AND (r.created_at, r.id) > (%s, %s)
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE rulings
    SET ruling_text_html = %s
    WHERE id = %s::uuid
      AND ruling_text_html IS NULL
"""

COUNT_QUERY = """
    SELECT COUNT(*)
    FROM rulings r
    JOIN documents d ON r.document_id = d.id
    JOIN cases c ON r.case_id = c.id
    JOIN courts ct ON c.court_id = ct.id
    WHERE ct.county = 'Los Angeles'
      AND r.ruling_text_html IS NULL
      AND r.ruling_text IS NOT NULL
      AND d.s3_key IS NOT NULL
      AND d.format = 'html'
"""

_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"


def _extract_html_for_case(
    raw_html: str,
    case_number: str | None,
) -> str | None:
    """Extract and sanitize HTML for a specific case from a department page.

    If case_number is provided, matches the correct section by case number.
    Otherwise falls back to the first section.
    """
    case_htmls = _split_cases_html(raw_html)
    if not case_htmls:
        return None

    from bs4 import BeautifulSoup

    for case_html in case_htmls:
        soup = BeautifulSoup(case_html, "lxml")
        content = soup.find("div", id="speechSynthesis")
        if not content:
            continue
        text = content.get_text(separator="\n", strip=True)
        match = _CASE_NUMBER_RE.search(text)
        chunk_case_num = match.group(1) if match else None
        if case_number and chunk_case_num == case_number:
            inner_html = content.decode_contents()
            return sanitize_ruling_html(inner_html)

    # Fallback: if only one section, use it regardless of case number match.
    if len(case_htmls) == 1:
        soup = BeautifulSoup(case_htmls[0], "lxml")
        content = soup.find("div", id="speechSynthesis")
        if content:
            inner_html = content.decode_contents()
            return sanitize_ruling_html(inner_html)

    return None


def main() -> None:
    """Run the backfill."""
    parser = argparse.ArgumentParser(
        description="Backfill LA ruling_text_html from S3 HTML"
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument(
        "--limit", type=int, default=0, help="Max rulings (0=unlimited)"
    )
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    s3 = boto3.client("s3")
    conn = psycopg.connect(database_url)

    # Count pending
    with conn.cursor() as cur:
        cur.execute(COUNT_QUERY)
        row = cur.fetchone()
    total = row[0] if row else 0
    logger.info("Pending LA rulings without ruling_text_html: %d", total)

    if total == 0:
        logger.info("Nothing to do")
        return

    processed = 0
    updated = 0
    skipped = 0
    cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    while True:
        with conn.cursor() as cur:
            cur.execute(FETCH_QUERY, (cursor[0], cursor[1], args.batch_size))
            rows = cur.fetchall()

        if not rows:
            break

        for ruling_id, s3_key, s3_bucket, created_at, case_number in rows:
            cursor = (created_at, str(ruling_id))
            processed += 1

            if args.limit and processed > args.limit:
                break

            try:
                obj = s3.get_object(Bucket=s3_bucket, Key=s3_key)
                raw_html = obj["Body"].read().decode("utf-8", errors="replace")
            except Exception:
                logger.warning("Failed to read S3 object %s/%s", s3_bucket, s3_key)
                skipped += 1
                continue

            sanitized = _extract_html_for_case(raw_html, case_number)
            if not sanitized:
                logger.debug("No HTML extracted for ruling %s", ruling_id)
                skipped += 1
                continue

            if args.dry_run:
                logger.info(
                    "[DRY RUN] Would update ruling %s (html_len=%d)",
                    ruling_id,
                    len(sanitized),
                )
            else:
                with conn.cursor() as cur:
                    cur.execute(UPDATE_QUERY, (sanitized, str(ruling_id)))
                conn.commit()
                updated += 1

            if processed % 100 == 0:
                logger.info(
                    "Progress: processed=%d updated=%d skipped=%d",
                    processed,
                    updated,
                    skipped,
                )

        if args.limit and processed >= args.limit:
            break

    conn.close()
    logger.info(
        "Done: processed=%d updated=%d skipped=%d",
        processed,
        updated,
        skipped,
    )


if __name__ == "__main__":
    main()
