#!/usr/bin/env python3
"""Backfill CourtListener documents to correct (state, county) jurisdiction.

Selects all documents rows whose scraper_id starts with 'federal-courtlistener'
then reads the raw JSON from S3, extracts the court reference (preferring
docket.court over cluster.court — see #4247), resolves (state, county) via
_CL_COURT_ID_TO_JURISDICTION, and updates documents.court_id (plus the
joined rulings.court_id) inside a transaction.

For old envelopes that pre-date docket capture (#4043 / #4247) the script
falls back to the live CourtListener ``/api/rest/v4/dockets/<id>/`` endpoint
using ``cluster.docket_id`` (or the absolute docket URL on ``cluster.docket``)
when ``--fetch-missing-dockets`` is passed. This is opt-in because each
fallback fetch costs an API call against the CourtListener daily quota.

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
    --dry-run                  Show what would change without writing to DB.
    --limit N                  Maximum number of documents to process (default: unbounded).
    --batch-size               Documents per transaction batch (default: 100).
    --fetch-missing-dockets    For old envelopes without a docket key, fetch the
                               docket sub-resource live from CourtListener.
                               Each fetch costs one API request.
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

from courts.federal.courtlistener import (  # noqa: E402
    _CL_COURT_ID_TO_JURISDICTION,
    CourtListenerClient,
    _resolve_court_id,
)
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
    parser.add_argument(
        "--fetch-missing-dockets",
        action="store_true",
        help=(
            "For old envelopes without a 'docket' key, fetch the docket "
            "sub-resource live from CourtListener (one API call per doc)."
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def _read_envelope_from_s3(
    s3_client: Any, s3_bucket: str, s3_key: str
) -> dict[str, Any] | None:
    """Read and parse the raw JSON envelope from S3.

    Returns the parsed dict, or None on any failure (logged as a warning).
    """
    try:
        response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
        raw = response["Body"].read()
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return None
    except Exception as exc:
        logger.warning(
            "Failed to read S3 object",
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            error=str(exc),
        )
        return None


def _extract_court_id_from_envelope(
    envelope: dict[str, Any],
    *,
    cl_client: CourtListenerClient | None = None,
) -> str | None:
    """Extract the resolved CourtListener court short-id from an S3 envelope.

    Resolution order (#4247):
      1. ``envelope['docket']['court']`` if the docket sub-resource is in the
         envelope (newer captures after #4247 store it alongside cluster+opinion).
      2. ``envelope['cluster']['court']`` for backward compatibility.
      3. If ``cl_client`` is provided AND neither of the above yields a court,
         fall back to fetching the docket live from CourtListener using
         ``cluster.docket_id`` or the absolute URL on ``cluster.docket``.

    Returns the short-id (e.g. ``"texapp14"``) or ``None`` on miss.
    """
    cluster = envelope.get("cluster") or {}
    docket = (
        envelope.get("docket") if isinstance(envelope.get("docket"), dict) else None
    )

    court_id = _resolve_court_id(cluster, docket)
    if court_id:
        return court_id

    if cl_client is None:
        return None

    # Old envelope (pre-#4247) — no docket captured.  Live-fetch.
    docket_url = cluster.get("docket")
    if not docket_url:
        docket_id = cluster.get("docket_id")
        if not docket_id:
            return None
        # Build the absolute URL — the API base lives on the client.
        from courts.federal.courtlistener import API_BASE_URL  # noqa: E402

        docket_url = f"{API_BASE_URL}/dockets/{docket_id}/"

    try:
        fetched_docket = cl_client.fetch_docket(docket_url)
    except Exception as exc:
        logger.warning(
            "Live docket fetch failed during backfill",
            docket_url=docket_url,
            error=str(exc),
        )
        return None

    court_id = _resolve_court_id(cluster, fetched_docket)
    return court_id if court_id else None


def _extract_court_id_from_s3(
    s3_client: Any,
    s3_bucket: str,
    s3_key: str,
    *,
    cl_client: CourtListenerClient | None = None,
) -> str | None:
    """Read the raw JSON from S3 and extract the resolved court short-id.

    Reads the envelope from S3 and delegates to ``_extract_court_id_from_envelope``.
    """
    envelope = _read_envelope_from_s3(s3_client, s3_bucket, s3_key)
    if envelope is None:
        return None
    return _extract_court_id_from_envelope(envelope, cl_client=cl_client)


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
    cl_client: CourtListenerClient | None = None,
) -> dict[str, dict[str, int]]:
    """Run the backfill and return per-court rebucket counts.

    Returns a dict: {court_id: {"from_federal": N, "rebucketed": N, "skipped": N}}
    """
    docs = fetch_courtlistener_documents(conn, limit)
    logger.info("Found documents to process", count=len(docs))

    # Per-court stats: court_id -> {original_state: count}
    rebucket_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    batch: list[
        tuple[str, str, str, str]
    ] = []  # (doc_id, new_court_id, new_state, new_county)

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

        court_id = _extract_court_id_from_s3(
            s3_client, s3_bucket, s3_key, cl_client=cl_client
        )
        if not court_id:
            logger.warning(
                "Could not extract court_id from S3 — skipping", doc_id=doc_id
            )
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

    s3_client = boto3.client("s3")

    cl_client: CourtListenerClient | None = None
    if args.fetch_missing_dockets:
        cl_client = CourtListenerClient()

    logger.info(
        "Starting CourtListener jurisdiction backfill",
        dry_run=args.dry_run,
        limit=args.limit,
        batch_size=args.batch_size,
        fetch_missing_dockets=args.fetch_missing_dockets,
    )

    with psycopg.connect(database_url) as conn:
        counts = run_backfill(
            conn,
            s3_client,
            dry_run=args.dry_run,
            limit=args.limit,
            batch_size=args.batch_size,
            cl_client=cl_client,
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
