#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill CSS inlining for existing archived HTML documents in S3.

Fetches HTML documents from S3, inlines any external CSS references,
and re-uploads the self-contained HTML. Documents that already contain
inline <style> blocks are skipped.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- scripts/run-py.sh scripts/backfill_css_inlining.py

Options:
    --dry-run       Report what would be updated without modifying S3.
    --limit N       Process at most N documents (default: unlimited).
    --county "Name" Process only documents from a specific county.
    --batch-size N  Number of documents to fetch per batch (default: 50).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import httpx  # noqa: E402
import psycopg  # noqa: E402

from framework.css_inliner import inline_css  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT d.id, d.s3_key, d.s3_bucket, d.source_url
    FROM documents d
    WHERE d.format = 'html'
      AND d.s3_key IS NOT NULL
      AND d.id > %s
    ORDER BY d.id
    LIMIT %s
"""

FETCH_QUERY_WITH_COUNTY = """
    SELECT d.id, d.s3_key, d.s3_bucket, d.source_url
    FROM documents d
    WHERE d.format = 'html'
      AND d.s3_key IS NOT NULL
      AND d.county = %s
      AND d.id > %s
    ORDER BY d.id
    LIMIT %s
"""

COUNT_QUERY = """
    SELECT COUNT(*)
    FROM documents d
    WHERE d.format = 'html'
      AND d.s3_key IS NOT NULL
"""

COUNT_QUERY_WITH_COUNTY = """
    SELECT COUNT(*)
    FROM documents d
    WHERE d.format = 'html'
      AND d.s3_key IS NOT NULL
      AND d.county = %s
"""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _has_inline_styles(html_bytes: bytes) -> bool:
    """Check if HTML already contains inline <style> blocks."""
    return b"<style>" in html_bytes.lower() or b"<style " in html_bytes.lower()


def count_pending(
    conn: psycopg.Connection,
    county: str | None = None,
) -> int:
    """Count HTML documents in the database."""
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
    cursor_id: str,
    county: str | None = None,
) -> list[tuple[str, str, str, str]]:
    """Fetch a batch of HTML documents.

    Returns list of (document_id, s3_key, s3_bucket, source_url) tuples.
    """
    with conn.cursor() as cur:
        if county:
            cur.execute(
                FETCH_QUERY_WITH_COUNTY,
                (county, cursor_id, batch_size),
            )
        else:
            cur.execute(FETCH_QUERY, (cursor_id, batch_size))
        return [(str(row[0]), row[1], row[2], row[3]) for row in cur.fetchall()]


def process_document(
    s3_client: object,
    http_client: httpx.Client,
    doc_id: str,
    s3_key: str,
    s3_bucket: str,
    source_url: str,
    dry_run: bool = False,
) -> str:
    """Process a single document: download, inline CSS, re-upload.

    Returns one of: "inlined", "skipped_has_styles", "skipped_no_change", "error".
    """
    try:
        # Download from S3
        response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
        html_bytes = response["Body"].read()

        # Skip if already has inline styles
        if _has_inline_styles(html_bytes):
            return "skipped_has_styles"

        # Inline CSS
        inlined = inline_css(html_bytes, base_url=source_url, http_client=http_client)

        # Check if anything changed
        if inlined == html_bytes:
            return "skipped_no_change"

        if dry_run:
            logger.info(
                "Would re-upload %s (%d -> %d bytes)",
                s3_key,
                len(html_bytes),
                len(inlined),
            )
            return "inlined"

        # Re-upload to S3
        s3_client.put_object(
            Bucket=s3_bucket,
            Key=s3_key,
            Body=inlined,
            ContentType="text/html",
        )
        logger.info(
            "Re-uploaded %s (%d -> %d bytes)",
            s3_key,
            len(html_bytes),
            len(inlined),
        )
        return "inlined"

    except Exception as exc:
        logger.warning("Error processing document %s: %s", doc_id, exc)
        return "error"


def run_backfill(
    dsn: str,
    *,
    batch_size: int = 50,
    limit: int | None = None,
    county: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the CSS inlining backfill. Returns summary stats."""
    stats = {
        "total_processed": 0,
        "total_inlined": 0,
        "total_skipped_has_styles": 0,
        "total_skipped_no_change": 0,
        "total_errors": 0,
    }

    cursor_id = "00000000-0000-0000-0000-000000000000"
    s3_client = boto3.client("s3")

    with (
        psycopg.connect(dsn) as conn,
        httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
            timeout=15.0,
        ) as http_client,
    ):
        pending = count_pending(conn, county)
        logger.info("Found %d HTML documents", pending)

        if limit is not None:
            pending = min(pending, limit)

        while stats["total_processed"] < pending:
            remaining = pending - stats["total_processed"]
            effective_batch = min(batch_size, remaining)
            if limit is not None:
                effective_batch = min(effective_batch, limit - stats["total_processed"])
                if effective_batch <= 0:
                    break

            rows = fetch_batch(conn, effective_batch, cursor_id, county)
            if not rows:
                break

            cursor_id = rows[-1][0]

            for doc_id, s3_key, s3_bucket, source_url in rows:
                result = process_document(
                    s3_client,
                    http_client,
                    doc_id,
                    s3_key,
                    s3_bucket,
                    source_url,
                    dry_run,
                )
                stats["total_processed"] += 1

                if result == "inlined":
                    stats["total_inlined"] += 1
                elif result == "skipped_has_styles":
                    stats["total_skipped_has_styles"] += 1
                elif result == "skipped_no_change":
                    stats["total_skipped_no_change"] += 1
                elif result == "error":
                    stats["total_errors"] += 1

            logger.info(
                "Batch complete: processed=%d inlined=%d skipped_styles=%d "
                "skipped_nochange=%d errors=%d (total: %d/%d)%s",
                len(rows),
                stats["total_inlined"],
                stats["total_skipped_has_styles"],
                stats["total_skipped_no_change"],
                stats["total_errors"],
                stats["total_processed"],
                pending,
                " [dry-run]" if dry_run else "",
            )

            if len(rows) < effective_batch:
                break

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill CSS inlining for archived HTML documents in S3.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be updated without modifying S3.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total documents to process.",
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Process only documents from a specific county.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of documents per batch (default: 50).",
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
        limit=args.limit,
        county=args.county,
        dry_run=args.dry_run,
    )
    elapsed = time.monotonic() - t0

    logger.info(
        "Backfill complete in %.1fs: %d processed, %d inlined, "
        "%d skipped (has styles), %d skipped (no change), %d errors",
        elapsed,
        stats["total_processed"],
        stats["total_inlined"],
        stats["total_skipped_has_styles"],
        stats["total_skipped_no_change"],
        stats["total_errors"],
    )

    if stats["total_errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
