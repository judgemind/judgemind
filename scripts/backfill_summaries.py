#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill AI-generated summaries for existing rulings.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and generates plain-English summaries
for rulings that have ruling_text but no summary, using Claude Haiku.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -e ANTHROPIC_API_KEY=judgemind/dev/anthropic-api-key \
        -- packages/scraper-framework/.venv/bin/python3 scripts/backfill_summaries.py

Each ruling is committed independently so that progress is saved
incrementally.  If the connection drops mid-run, already-committed
rulings are preserved and the script can be safely re-run (it is
idempotent — it only updates rows where summary IS NULL).

Options:
    --dry-run       Print what would be updated without writing to the database.
    --limit N       Process at most N rulings (default: unlimited).
    --county "Name" Process only rulings from a specific county.
    --batch-size N  Number of rulings to fetch per batch (default: 50).
    --concurrency N Number of concurrent LLM requests (default: 5).
"""

from __future__ import annotations

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

import anthropic  # noqa: E402
import psycopg  # noqa: E402
from judgemind_config import DEFAULT_HAIKU_MODEL  # noqa: E402

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

# Summarization prompt — matches the one in packages/nlp-pipeline/src/summarization/summarizer.py
_SYSTEM_PROMPT = (
    "You are summarizing a court tentative ruling for a legal research platform.\n"
    "Write 2-4 sentences summarizing: what motion was before the court, "
    "what the judge's tentative decision is, and the key reason(s). "
    "Use plain language that a non-lawyer can understand. "
    "Do not include case numbers or procedural boilerplate.\n\n"
    "If optional case context is provided (e.g. case title, motion type), "
    "use it to make the summary more specific, but do not repeat it verbatim."
)

_SUMMARY_MODEL = DEFAULT_HAIKU_MODEL

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT r.id, r.ruling_text, r.created_at, r.motion_type,
           c2.case_number, c2.case_title
    FROM rulings r
    JOIN cases c2 ON r.case_id = c2.id
    WHERE r.ruling_text IS NOT NULL
      AND r.summary IS NULL
      AND (r.created_at, r.id) > (%s, %s)
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

FETCH_QUERY_WITH_COUNTY = """
    SELECT r.id, r.ruling_text, r.created_at, r.motion_type,
           c2.case_number, c2.case_title
    FROM rulings r
    JOIN courts c ON r.court_id = c.id
    JOIN cases c2 ON r.case_id = c2.id
    WHERE r.ruling_text IS NOT NULL
      AND r.summary IS NULL
      AND c.county = %s
      AND (r.created_at, r.id) > (%s, %s)
    ORDER BY r.created_at, r.id
    LIMIT %s
"""

UPDATE_QUERY = """
    UPDATE rulings
    SET summary = %s,
        summary_model = %s,
        summary_generated_at = NOW()
    WHERE id = %s::uuid
      AND summary IS NULL
"""

COUNT_QUERY = """
    SELECT COUNT(*)
    FROM rulings r
    WHERE r.ruling_text IS NOT NULL
      AND r.summary IS NULL
"""

COUNT_QUERY_WITH_COUNTY = """
    SELECT COUNT(*)
    FROM rulings r
    JOIN courts c ON r.court_id = c.id
    WHERE r.ruling_text IS NOT NULL
      AND r.summary IS NULL
      AND c.county = %s
"""


# ---------------------------------------------------------------------------
# Summarization helper (inlined for ECS oneshot compatibility)
# ---------------------------------------------------------------------------


def generate_summary(
    ruling_text: str,
    client: anthropic.Anthropic,
    case_context: dict[str, str] | None = None,
) -> str:
    """Generate a plain-English summary of a tentative ruling using Claude Haiku.

    Args:
        ruling_text: The full text of a tentative ruling.
        client: An Anthropic API client instance.
        case_context: Optional dict with case metadata (e.g. case_title,
            motion_type) to improve summary specificity.

    Returns:
        A 2-4 sentence plain-English summary of the ruling.
    """
    user_content = "Summarize this tentative ruling:\n\n"
    if case_context:
        context_lines = [f"- {k}: {v}" for k, v in case_context.items()]
        user_content += "Case context:\n" + "\n".join(context_lines) + "\n\n"
    user_content += ruling_text

    response = client.messages.create(
        model=_SUMMARY_MODEL,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": user_content,
            }
        ],
    )

    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------


def count_pending(
    conn: psycopg.Connection,
    county: str | None = None,
) -> int:
    """Count rulings that need summaries."""
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
) -> list[tuple[str, str, datetime, str | None, str | None, str | None]]:
    """Fetch a batch of rulings that need summaries.

    Returns list of (ruling_id, ruling_text, created_at, motion_type,
    case_number, case_title) tuples.
    """
    with conn.cursor() as cur:
        if county:
            cur.execute(
                FETCH_QUERY_WITH_COUNTY,
                (county, cursor[0], cursor[1], batch_size),
            )
        else:
            cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        return [
            (str(row[0]), row[1], row[2], row[3], row[4], row[5])
            for row in cur.fetchall()
        ]


def summarize_one_ruling(
    ruling_id: str,
    ruling_text: str,
    client: anthropic.Anthropic,
    case_context: dict[str, str] | None = None,
) -> tuple[str, str | None, str | None]:
    """Summarize a single ruling through the LLM.

    Returns (ruling_id, summary_or_none, error_message_or_none).
    """
    try:
        summary = generate_summary(ruling_text, client, case_context=case_context)
        return (ruling_id, summary, None)
    except Exception as exc:  # noqa: BLE001
        return (ruling_id, None, str(exc))


def process_batch(
    conn: psycopg.Connection,
    rows: list[tuple[str, str, datetime, str | None, str | None, str | None]],
    *,
    client: anthropic.Anthropic,
    concurrency: int = 5,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Process a batch of rulings through the LLM summarizer.

    Returns (summarized_count, skipped_count, error_count).
    """
    summarized = 0
    skipped = 0
    errors = 0

    # Build case context for each ruling
    def _build_context(
        row: tuple[str, str, datetime, str | None, str | None, str | None],
    ) -> dict[str, str] | None:
        _rid, _text, _ts, motion_type, _case_number, case_title = row
        ctx: dict[str, str] = {}
        if case_title:
            ctx["case_title"] = case_title
        if motion_type:
            ctx["motion_type"] = motion_type
        return ctx if ctx else None

    # Summarize rulings concurrently
    results: list[tuple[str, str | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                summarize_one_ruling, rid, text, client, _build_context(row)
            ): rid
            for row in rows
            for rid, text, _created_at, _mt, _cn, _ct in [row]
        }
        for future in as_completed(futures):
            ruling_id = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                results.append((ruling_id, None, str(exc)))

    # Write results to DB
    for ruling_id, summary, error in results:
        if error:
            logger.warning("Failed to summarize ruling %s: %s", ruling_id, error)
            errors += 1
            continue

        if summary is None:
            skipped += 1
            continue

        if dry_run:
            logger.info("Would update ruling %s (dry-run)", ruling_id)
            summarized += 1
            continue

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (summary, _SUMMARY_MODEL, ruling_id))
            if cur.rowcount > 0:
                summarized += 1
            else:
                # Already summarized by a concurrent run
                skipped += 1

    if not dry_run:
        conn.commit()
    else:
        conn.rollback()

    return summarized, skipped, errors


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
    total_summarized = 0
    total_skipped = 0
    total_errors = 0
    total_processed = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    # Create a reusable Anthropic client for connection pooling
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not dry_run:
        logger.error("ANTHROPIC_API_KEY environment variable is required")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key or "dry-run-key")

    with psycopg.connect(dsn) as conn:
        # Count total pending for progress reporting
        pending = count_pending(conn, county)
        logger.info("Found %d rulings pending summarization", pending)

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

            fmt, skip, errs = process_batch(
                conn,
                rows,
                client=client,
                concurrency=concurrency,
                dry_run=dry_run,
            )

            total_summarized += fmt
            total_skipped += skip
            total_errors += errs
            total_processed += len(rows)

            logger.info(
                "Batch: processed=%d summarized=%d skipped=%d errors=%d "
                "(total: %d/%d)%s",
                len(rows),
                fmt,
                skip,
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
        "total_summarized": total_summarized,
        "total_skipped": total_skipped,
        "total_errors": total_errors,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill AI-generated summaries for existing rulings.",
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
        "Backfill complete in %.1fs: %d processed, %d summarized, %d skipped, %d errors",
        elapsed,
        stats["total_processed"],
        stats["total_summarized"],
        stats["total_skipped"],
        stats["total_errors"],
    )

    if stats["total_errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
