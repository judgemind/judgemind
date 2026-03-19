#!/usr/bin/env python3
"""Backfill ruling_text_html for existing rulings using LLM formatting.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and processes ruling_text through
the LLM formatting pipeline to populate ruling_text_html.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -e ANTHROPIC_API_KEY=judgemind/dev/anthropic-api-key \
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_ruling_html.py

Each ruling is committed independently so that progress is saved
incrementally.  If the connection drops mid-run, already-committed
rulings are preserved and the script can be safely re-run (it is
idempotent — it only updates rows where ruling_text_html IS NULL).

Options:
    --dry-run       Print what would be updated without writing to the database.
    --limit N       Process at most N rulings (default: unlimited).
    --county "Name" Process only rulings from a specific county.
    --batch-size N  Number of rulings to fetch per batch (default: 50).
    --concurrency N Number of concurrent LLM requests (default: 5).
"""

from __future__ import annotations

# Ensure we are running inside the scraper-framework venv (re-execs if not).
from _venv_helper import ensure_venv

ensure_venv("scraper-framework")

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402

from ingestion.llm_providers import create_client  # noqa: E402
from ingestion.ruling_formatter import format_ruling_text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT r.id, r.ruling_text, r.created_at
    FROM rulings r
    WHERE r.ruling_text IS NOT NULL
      AND r.ruling_text_html IS NULL
      AND (r.created_at, r.id) > (%s, %s)
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

FETCH_QUERY_WITH_COUNTY = """
    SELECT r.id, r.ruling_text, r.created_at
    FROM rulings r
    JOIN courts c ON r.court_id = c.id
    WHERE r.ruling_text IS NOT NULL
      AND r.ruling_text_html IS NULL
      AND c.county = %s
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
    WHERE r.ruling_text IS NOT NULL
      AND r.ruling_text_html IS NULL
"""

COUNT_QUERY_WITH_COUNTY = """
    SELECT COUNT(*)
    FROM rulings r
    JOIN courts c ON r.court_id = c.id
    WHERE r.ruling_text IS NOT NULL
      AND r.ruling_text_html IS NULL
      AND c.county = %s
"""


# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------


def count_pending(
    conn: psycopg.Connection,
    county: str | None = None,
) -> int:
    """Count rulings that need formatting."""
    with conn.cursor() as cur:
        if county:
            cur.execute(COUNT_QUERY_WITH_COUNTY, (county,))
        else:
            cur.execute(COUNT_QUERY)
        row = cur.fetchone()
    return row[0] if row else 0


def fetch_batch(
    conn: psycopg.Connection,
    batch_size: int,
    cursor: tuple[datetime, str],
    county: str | None = None,
) -> list[tuple[str, str, datetime]]:
    """Fetch a batch of rulings that need formatting.

    Returns list of (ruling_id, ruling_text, created_at) tuples.
    """
    with conn.cursor() as cur:
        if county:
            cur.execute(
                FETCH_QUERY_WITH_COUNTY,
                (county, cursor[0], cursor[1], batch_size),
            )
        else:
            cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        return [(str(row[0]), row[1], row[2]) for row in cur.fetchall()]


def format_one_ruling(
    ruling_id: str,
    ruling_text: str,
    client: object | None = None,
) -> tuple[str, str | None, str | None]:
    """Format a single ruling through the LLM pipeline.

    Returns (ruling_id, formatted_html_or_none, error_message_or_none).
    """
    try:
        html = format_ruling_text(ruling_text, client=client)
        return (ruling_id, html, None)
    except Exception as exc:  # noqa: BLE001
        return (ruling_id, None, str(exc))


def process_batch(
    conn: psycopg.Connection,
    rows: list[tuple[str, str, datetime]],
    *,
    client: object | None = None,
    concurrency: int = 5,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Process a batch of rulings through the LLM formatter.

    Returns (formatted_count, skipped_count, error_count).
    """
    formatted = 0
    skipped = 0
    errors = 0

    # Format rulings concurrently
    results: list[tuple[str, str | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(format_one_ruling, rid, text, client): rid
            for rid, text, _created_at in rows
        }
        for future in as_completed(futures):
            ruling_id = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                results.append((ruling_id, None, str(exc)))

    # Write results to DB
    for ruling_id, html, error in results:
        if error:
            logger.warning("Failed to format ruling %s: %s", ruling_id, error)
            errors += 1
            continue

        if html is None:
            skipped += 1
            continue

        if dry_run:
            logger.info("Would update ruling %s (dry-run)", ruling_id)
            formatted += 1
            continue

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (html, ruling_id))
            if cur.rowcount > 0:
                formatted += 1
            else:
                # Already formatted by a concurrent run
                skipped += 1

    if not dry_run:
        conn.commit()
    else:
        conn.rollback()

    return formatted, skipped, errors


def run_backfill(
    dsn: str,
    *,
    batch_size: int = 50,
    concurrency: int = 5,
    limit: int | None = None,
    county: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full backfill.  Returns summary stats."""
    total_formatted = 0
    total_skipped = 0
    total_errors = 0
    total_processed = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    # Create a reusable LLM client for connection pooling
    client = create_client(provider="anthropic")
    if client is None and not dry_run:
        logger.warning(
            "Could not create Anthropic client — "
            "set ANTHROPIC_API_KEY env var. Formatting will use fallback."
        )

    with psycopg.connect(dsn) as conn:
        # Count total pending for progress reporting
        pending = count_pending(conn, county)
        logger.info("Found %d rulings pending formatting", pending)

        if limit is not None:
            pending = min(pending, limit)

        while True:
            remaining = pending - total_processed
            if remaining <= 0:
                break

            effective_batch = min(batch_size, remaining)
            if limit is not None:
                effective_batch = min(effective_batch, limit - total_processed)
                if effective_batch <= 0:
                    break

            rows = fetch_batch(conn, effective_batch, cursor, county)
            if not rows:
                break

            # Update cursor to last row in batch
            last_row = rows[-1]
            cursor = (last_row[2], last_row[0])

            formatted, skipped, errs = process_batch(
                conn,
                rows,
                client=client,
                concurrency=concurrency,
                dry_run=dry_run,
            )

            total_formatted += formatted
            total_skipped += skipped
            total_errors += errs
            total_processed += len(rows)

            logger.info(
                "Batch: processed=%d formatted=%d skipped=%d errors=%d "
                "(total: %d/%d)%s",
                len(rows),
                formatted,
                skipped,
                errs,
                total_processed,
                pending,
                " [dry-run]" if dry_run else "",
            )

            if len(rows) < effective_batch:
                # Last batch — no more rows
                break

    return {
        "total_processed": total_processed,
        "total_formatted": total_formatted,
        "total_skipped": total_skipped,
        "total_errors": total_errors,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill ruling_text_html for existing rulings using LLM formatting.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing to the database.",
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
        help="Process only rulings from a specific county.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of rulings per batch (default: 50).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Number of concurrent LLM requests (default: 5).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    t0 = time.monotonic()
    stats = run_backfill(
        dsn,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        limit=args.limit,
        county=args.county,
        dry_run=args.dry_run,
    )
    elapsed = time.monotonic() - t0

    logger.info(
        "Backfill complete in %.1fs: %d processed, %d formatted, %d skipped, %d errors",
        elapsed,
        stats["total_processed"],
        stats["total_formatted"],
        stats["total_skipped"],
        stats["total_errors"],
    )

    if stats["total_errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
