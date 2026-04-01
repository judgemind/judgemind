#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill LA County rulings after multi-case split and cleanup improvements.

Re-processes existing LA County ruling records by:
  1. Fetching raw HTML from S3 archives
  2. Re-running multi-case splitting (records with multiple case numbers)
  3. Re-cleaning ruling_text with the updated cleanup pipeline
  4. Re-extracting fields (judge, parties, case title, motion type, outcome)

This script is idempotent: it only processes rulings whose documents have
an S3 key (archived content), and uses COALESCE/ON CONFLICT patterns so
re-running produces the same result.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_la_rulings.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_la_rulings.py --dry-run

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of rulings to process per batch (default: 50).
    --limit N       Maximum total rulings to process (default: unlimited).
    --bucket NAME   S3 bucket name (default: judgemind-document-archive-dev).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import uuid
from datetime import UTC, datetime

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import psycopg  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from courts.ca.la_tentatives import (  # noqa: E402
    _extract_ruling_fields,
    _split_cases_html,
)
from ingestion.db import (  # noqa: E402
    insert_document_and_ruling,
    resolve_judge,
    upsert_case,
    upsert_case_judge,
    upsert_case_party,
    upsert_party,
)
# NOTE: extract_motion_type and extract_outcome were removed in #2178.
# This archived script is no longer functional.  # noqa: E402
from ingestion.text_cleanup import clean_ruling_text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "judgemind-document-archive-dev"

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

# Fetch LA County rulings that have an S3 archive (so we can re-process from raw HTML).
# We join documents to get the s3_key/s3_bucket, and filter for CA / Los Angeles.
FETCH_QUERY = """
    SELECT r.id AS ruling_id,
           r.case_id,
           r.court_id,
           r.hearing_date,
           r.department,
           d.id AS document_id,
           d.s3_key,
           d.s3_bucket,
           d.source_url,
           d.scraper_id,
           d.captured_at,
           c.case_number,
           r.created_at
    FROM rulings r
    JOIN documents d ON d.id = r.document_id
    JOIN courts ct ON ct.id = r.court_id
    JOIN cases c ON c.id = r.case_id
    WHERE ct.state = 'CA'
      AND ct.county = 'Los Angeles'
      AND d.s3_key IS NOT NULL
    AND (r.created_at, r.id) > (%s, %s)
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

UPDATE_RULING_TEXT_QUERY = """
    UPDATE rulings
    SET ruling_text = %s,
        judge_id    = COALESCE(%s::uuid, judge_id),
        outcome     = COALESCE(%s::ruling_outcome, outcome),
        motion_type = COALESCE(%s, motion_type),
        updated_at  = NOW()
    WHERE id = %s::uuid
"""

UPDATE_CASE_TITLE_QUERY = """
    UPDATE cases
    SET case_title = COALESCE(%s, case_title),
        updated_at = NOW()
    WHERE id = %s::uuid
"""


# ---------------------------------------------------------------------------
# S3 helper
# ---------------------------------------------------------------------------


def fetch_s3_content(
    s3_client: object,
    bucket: str,
    key: str,
) -> bytes | None:
    """Fetch raw content from S3. Returns None on error."""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
        return response["Body"].read()  # type: ignore[index]
    except Exception as exc:
        logger.warning("Failed to fetch s3://%s/%s: %s", bucket, key, exc)
        return None


# ---------------------------------------------------------------------------
# CapturedDocument-like helper for field extraction
# ---------------------------------------------------------------------------


class _FieldBag:
    """Lightweight container matching CapturedDocument's field interface.

    _extract_ruling_fields writes to doc attributes; this provides the
    same writable interface without requiring the full Pydantic model.
    """

    def __init__(self) -> None:
        self.ruling_text: str | None = None
        self.case_number: str | None = None
        self.case_title: str | None = None
        self.judge_name: str | None = None
        self.parties: list[dict[str, str]] = []
        self.extra: dict[str, object] = {}


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------


def process_ruling(
    conn: psycopg.Connection,
    s3_client: object,
    default_bucket: str,
    ruling_id: str,
    case_id: str,
    court_id: str,
    hearing_date: object,
    department: str | None,
    document_id: str,
    s3_key: str,
    s3_bucket: str | None,
    source_url: str,
    scraper_id: str,
    captured_at: datetime | None,
    existing_case_number: str | None,
) -> dict[str, int]:
    """Re-process a single LA ruling from its S3 archive.

    Returns a stats dict with keys: updated, split_created, skipped.
    """
    stats: dict[str, int] = {"updated": 0, "split_created": 0, "skipped": 0}

    bucket = s3_bucket or default_bucket
    raw_html_bytes = fetch_s3_content(s3_client, bucket, s3_key)
    if raw_html_bytes is None:
        stats["skipped"] = 1
        return stats

    raw_html = raw_html_bytes.decode("utf-8", errors="replace")

    # Step 1: Split multi-case responses
    case_htmls = _split_cases_html(raw_html)

    if len(case_htmls) <= 1:
        # Single case — just re-clean and re-extract on the existing record
        _update_existing_ruling(
            conn,
            court_id,
            case_id,
            ruling_id,
            raw_html,
            hearing_date,
            department,
        )
        stats["updated"] = 1
    else:
        # Multi-case: update the original ruling with the first case's data,
        # then create new records for the remaining cases.
        logger.info(
            "Splitting ruling %s into %d cases",
            ruling_id,
            len(case_htmls),
        )

        # First case -> update original ruling
        _update_existing_ruling(
            conn,
            court_id,
            case_id,
            ruling_id,
            case_htmls[0],
            hearing_date,
            department,
        )
        stats["updated"] = 1

        # Remaining cases -> create new records
        for case_html in case_htmls[1:]:
            _create_split_ruling(
                conn,
                court_id,
                case_html,
                hearing_date,
                department,
                source_url,
                scraper_id,
                captured_at,
                bucket,
            )
            stats["split_created"] += 1

    return stats


def _update_existing_ruling(
    conn: psycopg.Connection,
    court_id: str,
    case_id: str,
    ruling_id: str,
    html: str,
    hearing_date: object,
    department: str | None,
) -> None:
    """Re-extract fields from HTML and update the existing ruling row."""
    bag = _FieldBag()
    soup = BeautifulSoup(html, "lxml")
    _extract_ruling_fields(soup, bag)  # type: ignore[arg-type]

    # Clean the ruling text
    cleaned_text = clean_ruling_text(bag.ruling_text)

    # Extract outcome and motion_type from raw text (before cleanup)
    outcome = extract_outcome(bag.ruling_text) if bag.ruling_text else None
    motion_type = extract_motion_type(bag.ruling_text) if bag.ruling_text else None

    # Resolve judge
    judge_id: str | None = None
    if bag.judge_name:
        judge_id = resolve_judge(conn, bag.judge_name, court_id)

    # Update ruling row
    with conn.cursor() as cur:
        cur.execute(
            UPDATE_RULING_TEXT_QUERY,
            (cleaned_text, judge_id, outcome, motion_type, ruling_id),
        )

    # Update case title
    if bag.case_title:
        with conn.cursor() as cur:
            cur.execute(UPDATE_CASE_TITLE_QUERY, (bag.case_title, case_id))

    # Update case number if extracted and different
    if bag.case_number:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE cases
                   SET case_number = COALESCE(case_number, %s),
                       updated_at = NOW()
                   WHERE id = %s::uuid""",
                (bag.case_number, case_id),
            )

    # Link judge to case
    if judge_id:
        upsert_case_judge(conn, case_id, judge_id, hearing_date)

    # Upsert parties
    for party_info in bag.parties:
        party_name = party_info.get("name", "")
        party_role = party_info.get("role", "")
        if party_name:
            party_id = upsert_party(conn, party_name)
            if party_role:
                upsert_case_party(conn, case_id, party_id, party_role)


def _create_split_ruling(
    conn: psycopg.Connection,
    court_id: str,
    html: str,
    hearing_date: object,
    department: str | None,
    source_url: str,
    scraper_id: str,
    captured_at: datetime | None,
    bucket: str,
) -> None:
    """Create a new document/case/ruling for a split-out case from a multi-case response."""
    bag = _FieldBag()
    soup = BeautifulSoup(html, "lxml")
    _extract_ruling_fields(soup, bag)  # type: ignore[arg-type]

    # We need a case_number to create a case record
    case_number = bag.case_number
    if not case_number:
        logger.warning("Split case has no case number — skipping")
        return

    # Clean the ruling text
    cleaned_text = clean_ruling_text(bag.ruling_text)

    # Extract outcome and motion_type from raw text
    outcome = extract_outcome(bag.ruling_text) if bag.ruling_text else None
    motion_type = extract_motion_type(bag.ruling_text) if bag.ruling_text else None

    # Resolve judge
    judge_id: str | None = None
    if bag.judge_name:
        judge_id = resolve_judge(conn, bag.judge_name, court_id)

    # Upsert case
    case_id = upsert_case(conn, case_number, court_id, case_title=bag.case_title)

    # Create document + ruling via shared helper (#1790).
    # The helper guarantees the same document_id is passed to both
    # insert_document and insert_ruling, preventing FK divergence (#1775).
    new_doc_id = str(uuid.uuid4())
    content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()

    insert_document_and_ruling(
        conn,
        document_id=new_doc_id,
        case_id=case_id,
        court_id=court_id,
        content_format="html",
        content_hash=content_hash,
        s3_key=None,  # split fragments don't have their own S3 key
        s3_bucket=bucket,
        source_url=source_url,
        scraper_id=scraper_id,
        captured_at=captured_at or datetime.now(UTC),
        hearing_date=hearing_date,
        ruling_text=cleaned_text,
        department=department,
        judge_id=judge_id,
        outcome=outcome,
        motion_type=motion_type,
    )

    # Link judge to case
    if judge_id:
        upsert_case_judge(conn, case_id, judge_id, hearing_date)

    # Upsert parties
    for party_info in bag.parties:
        party_name = party_info.get("name", "")
        party_role = party_info.get("role", "")
        if party_name:
            party_id = upsert_party(conn, party_name)
            if party_role:
                upsert_case_party(conn, case_id, party_id, party_role)

    logger.info(
        "Created split ruling: case=%s doc=%s judge=%s",
        case_number,
        new_doc_id,
        bag.judge_name,
    )


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


def backfill_batch(
    conn: psycopg.Connection,
    s3_client: object,
    default_bucket: str,
    batch_size: int = 50,
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID),
) -> tuple[dict[str, int], tuple[datetime, str]]:
    """Process one batch of LA County rulings.

    Returns (stats_dict, next_cursor) where stats has keys:
    processed, updated, split_created, skipped.
    """
    stats: dict[str, int] = {
        "processed": 0,
        "updated": 0,
        "split_created": 0,
        "skipped": 0,
    }
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        rows = cur.fetchall()

    if not rows:
        return stats, cursor

    for (
        ruling_id,
        case_id,
        court_id,
        hearing_date,
        department,
        document_id,
        s3_key,
        s3_bucket,
        source_url,
        scraper_id,
        captured_at,
        existing_case_number,
        created_at,
    ) in rows:
        stats["processed"] += 1
        next_cursor = (created_at, str(ruling_id))
        try:
            result = process_ruling(
                conn,
                s3_client,
                default_bucket,
                str(ruling_id),
                str(case_id),
                str(court_id),
                hearing_date,
                department,
                str(document_id),
                s3_key,
                s3_bucket,
                source_url,
                scraper_id,
                captured_at,
                existing_case_number,
            )
            stats["updated"] += result["updated"]
            stats["split_created"] += result["split_created"]
            stats["skipped"] += result["skipped"]
        except Exception as exc:
            logger.error(
                "Error processing ruling %s: %s",
                ruling_id,
                exc,
                exc_info=True,
            )
            stats["skipped"] += 1

    return stats, next_cursor


def run_backfill(
    dsn: str,
    *,
    bucket: str = DEFAULT_BUCKET,
    batch_size: int = 50,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full backfill. Returns summary stats."""
    total: dict[str, int] = {
        "total_processed": 0,
        "total_updated": 0,
        "total_split_created": 0,
        "total_skipped": 0,
    }
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)
    s3_client = boto3.client("s3")

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total["total_processed"]
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            batch_stats, cursor = backfill_batch(
                conn,
                s3_client,
                bucket,
                effective_batch,
                cursor,
            )
            total["total_processed"] += batch_stats["processed"]
            total["total_updated"] += batch_stats["updated"]
            total["total_split_created"] += batch_stats["split_created"]
            total["total_skipped"] += batch_stats["skipped"]

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d updated=%d split=%d skipped=%d "
                "(total: %d/%d/%d/%d)%s",
                batch_stats["processed"],
                batch_stats["updated"],
                batch_stats["split_created"],
                batch_stats["skipped"],
                total["total_processed"],
                total["total_updated"],
                total["total_split_created"],
                total["total_skipped"],
                " [dry-run, rolled back]" if dry_run else " [committed]",
            )

            if batch_stats["processed"] < effective_batch:
                break

    return total


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill LA County rulings after multi-case split and cleanup improvements."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing to the database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of rulings per batch (default: 50).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total rulings to process.",
    )
    parser.add_argument(
        "--bucket",
        type=str,
        default=DEFAULT_BUCKET,
        help="S3 bucket for archived content (default: judgemind-document-archive-dev).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_backfill(
        dsn,
        bucket=args.bucket,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Backfill complete: %d processed, %d updated, %d split-created, %d skipped",
        stats["total_processed"],
        stats["total_updated"],
        stats["total_split_created"],
        stats["total_skipped"],
    )


if __name__ == "__main__":
    main()
