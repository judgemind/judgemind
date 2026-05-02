#!/usr/bin/env python3
"""Backfill CourtListener documents to correct (state, county) jurisdiction.

Selects all documents rows whose scraper_id starts with 'federal-courtlistener'
then reads the raw JSON from S3, extracts cluster.court, resolves (state, county)
via _CL_COURT_ID_TO_JURISDICTION, and updates documents.court_id (plus the
joined rulings.court_id) inside a transaction.

Emits per-court rebucket counts to stdout for the verify-phase evidence comment.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_courtlistener_jurisdiction.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_courtlistener_jurisdiction.py

Usage (local):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -e S3_BUCKET=judgemind-dev-scraper-data \\
        -- packages/scraper-framework/.venv/bin/python3 \\
           scripts/backfill_courtlistener_jurisdiction.py --dry-run

Options:
    --dry-run     Show what would change without writing to DB.
    --limit N     Maximum number of documents to process (default: unbounded).
    --batch-size  Documents per transaction batch (default: 100).
"""

# venv: scraper-framework
# one-off: true

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import psycopg  # noqa: E402
import structlog  # noqa: E402

from courts.federal.courtlistener import _CL_COURT_ID_TO_JURISDICTION  # noqa: E402
from framework.logging import configure_structlog  # noqa: E402
from ingestion.db import upsert_court  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill CourtListener document jurisdictions."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without modifying the DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of documents to process (0 = no limit).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Documents per transaction batch (default: 100).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def _extract_court_id_from_s3(
    s3_client: Any, s3_bucket: str, s3_key: str
) -> str | None:
    """Read the raw JSON from S3 and extract the cluster.court short-id."""
    try:
        response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
        raw = response["Body"].read()
        data = json.loads(raw)
        cluster = data.get("cluster", {})
        court_raw = cluster.get("court", "") or ""
        if "/" in court_raw:
            parts = court_raw.rstrip("/").split("/")
            court_id = parts[-1] if parts else court_raw
        else:
            court_id = court_raw
        return court_id if court_id else None
    except Exception as exc:
        logger.warning("Failed to read S3 object", s3_bucket=s3_bucket, s3_key=s3_key, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def fetch_courtlistener_documents(
    conn: psycopg.Connection, limit: int
) -> list[dict[str, Any]]:
    """Fetch documents rows for federal-courtlistener scrapers."""
    query = """
        SELECT
            d.id,
            d.scraper_id,
            d.s3_bucket,
            d.s3_key,
            d.court_id,
            c.state AS current_state,
            c.county AS current_county
        FROM derived.documents d
        JOIN derived.courts c ON d.court_id = c.id
        WHERE d.scraper_id LIKE 'federal-courtlistener%'
        ORDER BY d.id
    """
    if limit > 0:
        query += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description]

    return [dict(zip(cols, row)) for row in rows]


def update_document_court(
    conn: psycopg.Connection,
    doc_id: str,
    new_court_id: str,
) -> None:
    """Update documents.court_id and the corresponding rulings.court_id."""
    with conn.cursor() as cur:
        # Update documents
        cur.execute(
            "UPDATE derived.documents SET court_id = %s WHERE id = %s",
            (new_court_id, doc_id),
        )
        # Update rulings that reference this document
        cur.execute(
            "UPDATE derived.rulings SET court_id = %s WHERE document_id = %s",
            (new_court_id, doc_id),
        )


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------


def run_backfill(
    conn: psycopg.Connection,
    s3_client: Any,
    *,
    dry_run: bool,
    limit: int,
    batch_size: int,
) -> dict[str, dict[str, int]]:
    """Run the backfill and return per-court rebucket counts.

    Returns a dict: {court_id: {"from_federal": N, "rebucketed": N, "skipped": N}}
    """
    docs = fetch_courtlistener_documents(conn, limit)
    logger.info("Found documents to process", count=len(docs))

    # Per-court stats: court_id -> {original_state: count}
    rebucket_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    batch: list[tuple[str, str, str, str]] = []  # (doc_id, new_court_id, new_state, new_county)

    def _flush_batch(batch: list[tuple[str, str, str, str]]) -> None:
        if not batch or dry_run:
            return
        with conn.transaction():
            for doc_id, new_court_id, _state, _county in batch:
                update_document_court(conn, doc_id, new_court_id)

    for doc in docs:
        doc_id = doc["id"]
        s3_bucket = doc["s3_bucket"]
        s3_key = doc["s3_key"]
        current_state = doc["current_state"]
        current_county = doc["current_county"]

        if not s3_key or not s3_bucket:
            logger.warning("Document has no S3 key/bucket — skipping", doc_id=doc_id)
            rebucket_counts["_no_s3"]["skipped"] += 1
            continue

        court_id = _extract_court_id_from_s3(s3_client, s3_bucket, s3_key)
        if not court_id:
            logger.warning("Could not extract court_id from S3 — skipping", doc_id=doc_id)
            rebucket_counts["_unknown"]["skipped"] += 1
            continue

        if court_id in _CL_COURT_ID_TO_JURISDICTION:
            new_state, new_county = _CL_COURT_ID_TO_JURISDICTION[court_id]
        else:
            new_state, new_county = "Unknown", "Unknown"
            logger.warning(
                "Unknown court_id during backfill — setting Unknown",
                courtlistener_court_id=court_id,
                doc_id=doc_id,
            )

        # Skip if already correct
        if current_state == new_state and current_county == new_county:
            rebucket_counts[court_id]["already_correct"] += 1
            continue

        # Obtain or create the target court row
        if not dry_run:
            new_court_id = upsert_court(
                conn,
                state=new_state,
                county=new_county,
                court_name="CourtListener",
            )
        else:
            new_court_id = "(dry-run)"

        rebucket_counts[court_id]["rebucketed"] += 1
        rebucket_counts[court_id][f"from_{current_state}/{current_county}"] += 1

        logger.info(
            "Rebucketing document",
            doc_id=doc_id,
            court_id=court_id,
            from_state=current_state,
            from_county=current_county,
            to_state=new_state,
            to_county=new_county,
            dry_run=dry_run,
        )

        batch.append((doc_id, new_court_id, new_state, new_county))

        if len(batch) >= batch_size:
            _flush_batch(batch)
            batch.clear()

    # Flush remaining
    _flush_batch(batch)
    batch.clear()

    return {k: dict(v) for k, v in rebucket_counts.items()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable not set")
        sys.exit(1)

    s3_bucket_env = os.environ.get("S3_BUCKET", "")
    s3_client = boto3.client("s3")

    logger.info(
        "Starting CourtListener jurisdiction backfill",
        dry_run=args.dry_run,
        limit=args.limit,
        batch_size=args.batch_size,
    )

    with psycopg.connect(database_url) as conn:
        counts = run_backfill(
            conn,
            s3_client,
            dry_run=args.dry_run,
            limit=args.limit,
            batch_size=args.batch_size,
        )

    # Print summary table
    print("\n=== CourtListener Jurisdiction Backfill Summary ===")
    print(f"{'Court ID':<30} {'Action':<40} {'Count':>8}")
    print("-" * 80)
    total_rebucketed = 0
    for court_id in sorted(counts):
        for action, n in sorted(counts[court_id].items()):
            print(f"{court_id:<30} {action:<40} {n:>8}")
            if action == "rebucketed":
                total_rebucketed += n
    print("-" * 80)
    print(f"Total rebucketed: {total_rebucketed}")
    if args.dry_run:
        print("\n*** DRY RUN — no changes written to DB ***")


if __name__ == "__main__":
    main()
