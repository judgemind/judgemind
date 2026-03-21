#!/usr/bin/env python3
"""Backfill: split existing multi-case rulings into individual per-case records.

Identifies existing multi-case ruling records (e.g. OC calendar pages containing
multiple cases) and re-processes them through the document splitting framework to
create individual per-case ruling records.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/backfill_split_rulings.py

    # Dry-run (default): report what would be split
    scripts/run-py.sh scripts/backfill_split_rulings.py

    # Apply changes
    scripts/run-py.sh scripts/backfill_split_rulings.py --apply

Options:
    --apply         Execute changes (default is dry-run).
    --batch-size N  Number of rulings to process per batch (default: 50).
    --limit N       Maximum total rulings to process (default: unlimited).
    --county NAME   Restrict to a specific county (default: all counties with splitters).
    --min-length N  Minimum ruling_text length to consider (default: 5000).
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
import psycopg.errors

# Import from the installed scraper-framework package.
from ingestion.db import normalize_ruling_text_hash
from ingestion.splitter import SplitResult, make_split_document_id, split_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CandidateRuling:
    """A ruling record that is a candidate for splitting."""

    ruling_id: str
    document_id: str
    case_id: str
    court_id: str
    judge_id: str | None
    hearing_date: str  # ISO date string
    ruling_text: str
    ruling_text_html: str | None
    department: str | None
    outcome: str | None
    motion_type: str | None
    state: str
    county: str
    case_number: str
    case_title: str | None
    source_url: str
    scraper_id: str
    content_format: str
    content_hash: str
    s3_key: str | None
    s3_bucket: str | None
    captured_at: str  # ISO datetime string
    created_at: datetime  # for cursor-based pagination


@dataclass
class SplitAction:
    """Describes what would happen (or did happen) when splitting a ruling."""

    original_ruling_id: str
    original_document_id: str
    original_case_number: str
    split_count: int
    split_document_ids: list[str]
    split_case_numbers: list[str | None]
    split_case_titles: list[str | None]


# ---------------------------------------------------------------------------
# Candidate identification
# ---------------------------------------------------------------------------

# Minimum cursor values for the first batch.
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

CANDIDATE_QUERY = """
    SELECT
        r.id AS ruling_id,
        r.document_id,
        r.case_id,
        r.court_id,
        r.judge_id,
        r.hearing_date,
        r.ruling_text,
        r.ruling_text_html,
        r.department,
        r.outcome::text,
        r.motion_type,
        ct.state,
        ct.county,
        c.case_number,
        c.case_title,
        d.source_url,
        d.scraper_id,
        d.format::text AS content_format,
        d.content_hash,
        d.s3_key,
        d.s3_bucket,
        d.captured_at,
        r.created_at
    FROM rulings r
    JOIN courts ct ON ct.id = r.court_id
    JOIN cases c ON c.id = r.case_id
    JOIN documents d ON d.id = r.document_id
    WHERE LENGTH(r.ruling_text) >= %s
    AND (r.created_at, r.id) > (%s, %s)
    {county_filter}
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

# Counties that have registered splitters.
SPLITTABLE_COUNTIES = [
    ("CA", "Orange"),
]


def build_candidate_query(county: str | None = None) -> tuple[str, bool]:
    """Build the candidate query with optional county filter.

    Returns (query_string, has_county_param) — if has_county_param is True,
    the caller must include county as an extra parameter.
    """
    if county:
        county_filter = "AND ct.county = %s"
        return CANDIDATE_QUERY.format(county_filter=county_filter), True
    # Default: restrict to counties that have splitters.
    counties = [c[1] for c in SPLITTABLE_COUNTIES]
    placeholders = ", ".join(["%s"] * len(counties))
    county_filter = f"AND ct.county IN ({placeholders})"
    return CANDIDATE_QUERY.format(county_filter=county_filter), False


def fetch_candidates(
    conn: psycopg.Connection,
    batch_size: int,
    min_length: int,
    county: str | None,
    cursor: tuple[datetime, str],
) -> list[CandidateRuling]:
    """Fetch one batch of candidate rulings for splitting."""
    query, has_single_county = build_candidate_query(county)

    params: list[Any] = [min_length, cursor[0], cursor[1]]
    if has_single_county:
        params.append(county)
    else:
        params.extend(c[1] for c in SPLITTABLE_COUNTIES)
    params.append(batch_size)

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    candidates = []
    for row in rows:
        candidates.append(
            CandidateRuling(
                ruling_id=str(row[0]),
                document_id=str(row[1]),
                case_id=str(row[2]),
                court_id=str(row[3]),
                judge_id=str(row[4]) if row[4] else None,
                hearing_date=str(row[5]),
                ruling_text=row[6],
                ruling_text_html=row[7],
                department=row[8],
                outcome=row[9],
                motion_type=row[10],
                state=row[11],
                county=row[12],
                case_number=row[13],
                case_title=row[14],
                source_url=row[15] or "",
                scraper_id=row[16] or "",
                content_format=row[17] or "html",
                content_hash=row[18] or "",
                s3_key=row[19],
                s3_bucket=row[20],
                captured_at=str(row[21]) if row[21] else "",
                created_at=row[22],
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Splitting and record creation
# ---------------------------------------------------------------------------


def try_split(candidate: CandidateRuling) -> list[SplitResult] | None:
    """Attempt to split a candidate ruling using the splitting framework.

    Returns the split results if the ruling was split into 2+ parts, or None
    if no splitting occurred (single case or no splitter registered).
    """
    event_data: dict[str, Any] = {
        "document_id": candidate.document_id,
        "state": candidate.state,
        "county": candidate.county,
        "ruling_text": candidate.ruling_text,
        "case_number": candidate.case_number,
        "case_title": candidate.case_title,
        "department": candidate.department,
        "hearing_date": candidate.hearing_date,
    }
    results = split_document(event_data)
    if len(results) < 2:
        return None
    return results


def apply_split(
    conn: psycopg.Connection,
    candidate: CandidateRuling,
    splits: list[SplitResult],
) -> SplitAction:
    """Create individual ruling records for each split, and delete the original.

    All changes happen within the caller's transaction.
    """
    split_doc_ids: list[str] = []
    split_case_numbers: list[str | None] = []
    split_case_titles: list[str | None] = []
    skipped = 0

    for idx, split in enumerate(splits):
        split_doc_id = make_split_document_id(candidate.document_id, idx)
        split_doc_ids.append(split_doc_id)

        # Determine case title for this split.
        case_title = split.case_title or candidate.case_title

        # Determine which case this split belongs to and what case_number
        # to use for the case record.
        #
        # Three scenarios:
        # 1. Splitter provided a case_number different from the original
        #    -> create/find a case by that case_number.
        # 2. Splitter provided NO case_number but a DIFFERENT case_title
        #    (North JC: no case numbers in PDF, only case titles)
        #    -> each unique case_title is a distinct legal case; use the
        #       case_title as a synthetic case_number to create a separate
        #       case record, matching how the live ingestion worker routes
        #       North JC splits via UNKNOWN-{document_id}.
        # 3. Neither case_number nor case_title differ from the original
        #    -> this split belongs to the same case as the original.
        if split.case_number and split.case_number != candidate.case_number:
            case_number = split.case_number
            case_id = _upsert_case(conn, case_number, candidate.court_id, case_title)
        elif (
            not split.case_number
            and split.case_title
            and split.case_title != candidate.case_title
        ):
            # North JC: use case_title as synthetic case_number so each
            # split with a unique title gets its own case record.
            case_number = split.case_title
            case_id = _upsert_case(conn, case_number, candidate.court_id, case_title)
        else:
            case_number = split.case_number or candidate.case_number
            case_id = candidate.case_id

        split_case_numbers.append(case_number)
        split_case_titles.append(case_title)

        # Upsert the document row for the split (shares original doc for archive).
        _upsert_split_document(conn, split_doc_id, case_id, candidate)

        # Insert the split ruling.  Returns False if a duplicate already exists
        # (same case_id + ruling_text_hash from a previous backfill run).
        outcome = split.outcome or candidate.outcome
        motion_type = split.motion_type or candidate.motion_type
        inserted = _insert_split_ruling(
            conn,
            document_id=split_doc_id,
            case_id=case_id,
            court_id=candidate.court_id,
            judge_id=candidate.judge_id,
            hearing_date=candidate.hearing_date,
            ruling_text=split.ruling_text,
            department=candidate.department,
            outcome=outcome,
            motion_type=motion_type,
        )
        if not inserted:
            skipped += 1

    if skipped:
        logger.info(
            "Skipped %d/%d splits for %s (duplicates already exist)",
            skipped,
            len(splits),
            candidate.ruling_id,
        )

    # Delete the original combined ruling.
    _delete_original_ruling(conn, candidate.ruling_id)

    return SplitAction(
        original_ruling_id=candidate.ruling_id,
        original_document_id=candidate.document_id,
        original_case_number=candidate.case_number,
        split_count=len(splits),
        split_document_ids=split_doc_ids,
        split_case_numbers=split_case_numbers,
        split_case_titles=split_case_titles,
    )


def _upsert_case(
    conn: psycopg.Connection,
    case_number: str,
    court_id: str,
    case_title: str | None,
) -> str:
    """Upsert a case record and return its UUID."""
    normalized = case_number.strip().lower().replace(" ", "").replace("-", "")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (case_number, case_number_normalized, court_id, case_title)
            VALUES (%s, %s, %s::uuid, %s)
            ON CONFLICT (court_id, case_number) DO UPDATE
                SET case_title = COALESCE(EXCLUDED.case_title, cases.case_title)
            RETURNING id
            """,
            (case_number, normalized, court_id, case_title),
        )
        row = cur.fetchone()
    if row is None:
        msg = f"upsert_case returned no row for case_number={case_number!r}"
        raise RuntimeError(msg)
    return str(row[0])


def _upsert_split_document(
    conn: psycopg.Connection,
    split_doc_id: str,
    case_id: str,
    candidate: CandidateRuling,
) -> None:
    """Upsert a document row for a split ruling.

    Uses the split document_id as PK. Copies metadata from the original document.
    """
    format_map = {
        "html": "html",
        "pdf": "pdf",
        "docx": "docx",
        "text": "txt",
        "txt": "txt",
    }
    pg_format = format_map.get(candidate.content_format.lower(), "html")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (
                id, case_id, court_id,
                document_type, format,
                s3_key, s3_bucket,
                content_hash, source_url, scraper_id,
                captured_at, last_seen_at, hearing_date, status
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid,
                'ruling', %s::document_format,
                %s, %s,
                %s, %s, %s,
                %s, NOW(), %s, 'active'
            )
            ON CONFLICT (id) DO UPDATE SET
                case_id = EXCLUDED.case_id,
                last_seen_at = NOW()
            """,
            (
                split_doc_id,
                case_id,
                candidate.court_id,
                pg_format,
                candidate.s3_key,
                candidate.s3_bucket,
                candidate.content_hash,
                candidate.source_url,
                candidate.scraper_id,
                candidate.captured_at or datetime.utcnow().isoformat(),
                candidate.hearing_date,
            ),
        )


def _insert_split_ruling(
    conn: psycopg.Connection,
    *,
    document_id: str,
    case_id: str,
    court_id: str,
    judge_id: str | None,
    hearing_date: str,
    ruling_text: str,
    department: str | None,
    outcome: str | None,
    motion_type: str | None,
) -> bool:
    """Insert a ruling row for a split ruling.

    Uses ON CONFLICT (document_id) DO UPDATE for idempotency on same-document
    reruns.  Also handles the (case_id, ruling_text_hash) unique constraint
    to prevent duplicates when the same content is re-ingested with a different
    parent document_id (the root cause of #1233).

    Returns True if the ruling was inserted/updated, False if it was skipped
    because an identical ruling already exists for the same case.
    """
    text_hash = normalize_ruling_text_hash(ruling_text)
    with conn.cursor() as cur:
        # Use a savepoint so a UniqueViolation on the (case_id, ruling_text_hash)
        # index doesn't abort the entire transaction.
        cur.execute("SAVEPOINT insert_split_ruling")
        try:
            cur.execute(
                """
                INSERT INTO rulings (
                    document_id, case_id, court_id, judge_id,
                    hearing_date, ruling_text, ruling_text_hash,
                    department, is_tentative,
                    outcome, motion_type
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s::date, %s, %s,
                    %s, TRUE,
                    %s::ruling_outcome, %s
                )
                ON CONFLICT (document_id) DO UPDATE SET
                    ruling_text = EXCLUDED.ruling_text,
                    ruling_text_hash = EXCLUDED.ruling_text_hash,
                    judge_id = COALESCE(EXCLUDED.judge_id, rulings.judge_id),
                    outcome = COALESCE(EXCLUDED.outcome, rulings.outcome),
                    motion_type = COALESCE(EXCLUDED.motion_type, rulings.motion_type),
                    department = COALESCE(EXCLUDED.department, rulings.department)
                """,
                (
                    document_id,
                    case_id,
                    court_id,
                    judge_id,
                    hearing_date,
                    ruling_text,
                    text_hash,
                    department,
                    outcome,
                    motion_type,
                ),
            )
            cur.execute("RELEASE SAVEPOINT insert_split_ruling")
        except psycopg.errors.UniqueViolation:
            # A ruling with the same (case_id, ruling_text_hash) already exists
            # from a previous backfill run with a different parent document_id.
            # This is expected — just skip the duplicate insert.
            cur.execute("ROLLBACK TO SAVEPOINT insert_split_ruling")
            logger.info(
                "Skipping duplicate ruling for case_id=%s (text_hash=%s already exists)",
                case_id,
                text_hash,
            )
            return False
    return True


def _delete_original_ruling(
    conn: psycopg.Connection,
    ruling_id: str,
) -> None:
    """Delete the original combined ruling record."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM rulings WHERE id = %s::uuid",
            (ruling_id,),
        )


# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------


def process_batch(
    conn: psycopg.Connection,
    batch_size: int,
    min_length: int,
    county: str | None,
    cursor: tuple[datetime, str],
    dry_run: bool,
) -> tuple[int, int, int, list[SplitAction], tuple[datetime, str]]:
    """Process one batch of candidate rulings.

    Returns (candidates_checked, split_count, new_rulings_count, actions, next_cursor).
    """
    candidates = fetch_candidates(conn, batch_size, min_length, county, cursor)
    if not candidates:
        return 0, 0, 0, [], cursor

    checked = 0
    split_count = 0
    new_rulings = 0
    actions: list[SplitAction] = []
    next_cursor = cursor

    for candidate in candidates:
        checked += 1
        next_cursor = (candidate.created_at, candidate.ruling_id)

        splits = try_split(candidate)
        if splits is None:
            continue

        logger.info(
            "Candidate %s (%s) -> %d splits",
            candidate.ruling_id,
            candidate.case_number,
            len(splits),
        )

        if dry_run:
            split_doc_ids = [
                make_split_document_id(candidate.document_id, i)
                for i in range(len(splits))
            ]
            action = SplitAction(
                original_ruling_id=candidate.ruling_id,
                original_document_id=candidate.document_id,
                original_case_number=candidate.case_number,
                split_count=len(splits),
                split_document_ids=split_doc_ids,
                split_case_numbers=[s.case_number for s in splits],
                split_case_titles=[s.case_title for s in splits],
            )
        else:
            action = apply_split(conn, candidate, splits)

        actions.append(action)
        split_count += 1
        new_rulings += action.split_count

    return checked, split_count, new_rulings, actions, next_cursor


def run_backfill(
    dsn: str,
    *,
    batch_size: int = 50,
    limit: int | None = None,
    min_length: int = 5000,
    county: str | None = None,
    dry_run: bool = True,
) -> dict[str, int]:
    """Run the full backfill. Returns summary stats."""
    total_checked = 0
    total_split = 0
    total_new_rulings = 0
    all_actions: list[SplitAction] = []
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_checked
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            checked, split_count, new_rulings, actions, cursor = process_batch(
                conn, effective_batch, min_length, county, cursor, dry_run
            )
            total_checked += checked
            total_split += split_count
            total_new_rulings += new_rulings
            all_actions.extend(actions)

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            mode = "[dry-run]" if dry_run else "[committed]"
            logger.info(
                "Batch: checked=%d split=%d new_rulings=%d (total: %d/%d/%d) %s",
                checked,
                split_count,
                new_rulings,
                total_checked,
                total_split,
                total_new_rulings,
                mode,
            )

            if checked < effective_batch:
                break

    # Print summary of actions.
    if all_actions:
        logger.info("--- Split Summary ---")
        for action in all_actions:
            titles = [t or "?" for t in action.split_case_titles]
            logger.info(
                "  %s (%s) -> %d splits: %s",
                action.original_ruling_id,
                action.original_case_number,
                action.split_count,
                ", ".join(titles),
            )

    return {
        "total_checked": total_checked,
        "total_split": total_split,
        "total_new_rulings": total_new_rulings,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill: split existing multi-case rulings into individual records.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute changes (default is dry-run).",
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
        "--county",
        type=str,
        default=None,
        help="Restrict to a specific county.",
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=5000,
        help="Minimum ruling_text length to consider (default: 5000).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    dry_run = not args.apply
    if dry_run:
        logger.info("DRY-RUN mode — no changes will be written")
    else:
        logger.info("APPLY mode — changes will be committed")

    stats = run_backfill(
        dsn,
        batch_size=args.batch_size,
        limit=args.limit,
        min_length=args.min_length,
        county=args.county,
        dry_run=dry_run,
    )

    logger.info(
        "Backfill complete: %d checked, %d split, %d new rulings created",
        stats["total_checked"],
        stats["total_split"],
        stats["total_new_rulings"],
    )


if __name__ == "__main__":
    main()
