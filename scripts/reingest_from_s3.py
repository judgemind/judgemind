#!/usr/bin/env python3
"""Re-ingest existing documents from S3 through the (now-idempotent) pipeline.

For each document in the database, fetches the raw content from S3, re-runs
the scraper's parse_document() to extract fields with the current (improved)
extraction logic, and pushes a synthetic DocumentCapturedEvent through the
ingestion worker.

This is the "fix and re-run" mechanism: after improving a scraper's extraction
logic, run this script for the affected court/date range to update all records.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/reingest_from_s3.py \
            --county "Los Angeles" --date-from 2026-01-01

Options:
    --county NAME       Scope to documents from this county.
    --date-from DATE    Only re-ingest documents captured on or after this date.
    --date-to DATE      Only re-ingest documents captured on or before this date.
    --dry-run           Parse and show what would be updated, but don't write to DB.
    --batch-size N      Number of documents per batch (default: 50).
    --limit N           Maximum total documents to re-ingest.
    --concurrency N     Number of parallel S3 fetch threads (default: 10).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import psycopg  # noqa: E402

from framework.models import CapturedDocument, ContentFormat  # noqa: E402
from ingestion.db import (  # noqa: E402
    insert_document,
    insert_ruling,
    resolve_judge,
    upsert_case,
    upsert_case_judge,
    upsert_case_party,
    upsert_party,
)
from ingestion.extract import (  # noqa: E402
    extract_case_number,
    extract_judge_name,
    extract_motion_type,
    extract_outcome,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# Registry mapping scraper_id to scraper class for parse_document()
_SCRAPER_REGISTRY: dict[str, type] = {}


def _load_scraper_registry() -> None:
    """Lazily populate the scraper registry from known court modules."""
    if _SCRAPER_REGISTRY:
        return
    try:
        from courts.ca.la_tentatives import LATentativesScraper

        _SCRAPER_REGISTRY["ca-la-tentatives-civil"] = LATentativesScraper
    except ImportError:
        pass
    try:
        from courts.ca.oc_tentatives import OCTentativesScraper

        _SCRAPER_REGISTRY["ca-oc-tentatives"] = OCTentativesScraper
    except ImportError:
        pass
    try:
        from courts.ca.sb_tentatives import SBTentativesScraper

        _SCRAPER_REGISTRY["ca-sb-tentatives"] = SBTentativesScraper
    except ImportError:
        pass
    try:
        from courts.ca.sf_tentatives import SFTentativesScraper

        _SCRAPER_REGISTRY["ca-sf-tentatives"] = SFTentativesScraper
    except ImportError:
        pass
    try:
        from courts.ca.sc_tentatives import SCTentativesScraper

        _SCRAPER_REGISTRY["ca-sc-tentatives"] = SCTentativesScraper
    except ImportError:
        pass
    try:
        from courts.ca.riverside_tentatives import RiversideTentativesScraper

        _SCRAPER_REGISTRY["ca-riverside-tentatives"] = RiversideTentativesScraper
    except ImportError:
        pass


FETCH_DOCUMENTS_QUERY = """
    SELECT
        d.id, d.case_id, d.court_id, d.s3_key, d.s3_bucket,
        d.content_hash, d.source_url, d.scraper_id, d.captured_at,
        d.hearing_date, d.format,
        ct.state, ct.county, ct.court_name,
        c.case_number, c.case_title
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN cases c ON c.id = d.case_id
    WHERE d.status = 'active'
    {filters}
    AND (d.captured_at, d.id) > (%s, %s)
    ORDER BY d.captured_at, d.id
    LIMIT %s
"""

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"


def _build_filters(
    county: str | None,
    date_from: date | None,
    date_to: date | None,
) -> tuple[str, list]:
    """Build WHERE clause fragments and params for the document query."""
    clauses = []
    params: list = []
    if county:
        clauses.append("AND ct.county = %s")
        params.append(county)
    if date_from:
        clauses.append("AND d.captured_at >= %s")
        params.append(datetime.combine(date_from, datetime.min.time()))
    if date_to:
        clauses.append("AND d.captured_at <= %s")
        params.append(datetime.combine(date_to, datetime.max.time()))
    return " ".join(clauses), params


def _fetch_s3_content(s3_client: object, bucket: str, key: str) -> bytes:
    """Fetch raw content from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
    return response["Body"].read()  # type: ignore[index]


def _reparse_document(
    raw_content: bytes,
    scraper_id: str,
    doc_meta: dict,
) -> dict:
    """Re-parse a document using the scraper's parse_document method.

    Falls back to regex extraction if no scraper class is available.
    Returns a dict of extracted fields.
    """
    _load_scraper_registry()

    text = raw_content.decode("utf-8", errors="replace")
    extracted: dict = {
        "ruling_text": text,
        "case_number": doc_meta.get("case_number"),
        "case_title": doc_meta.get("case_title"),
        "judge_name": None,
        "outcome": None,
        "motion_type": None,
        "department": None,
        "parties": [],
        "hearing_date": doc_meta.get("hearing_date"),
    }

    scraper_cls = _SCRAPER_REGISTRY.get(scraper_id)
    if scraper_cls:
        # Create a CapturedDocument and run parse_document
        try:
            from framework.models import ScraperConfig

            config = ScraperConfig(
                scraper_id=scraper_id,
                state=doc_meta["state"],
                county=doc_meta["county"],
                court=doc_meta["court_name"],
                target_urls=[],
            )
            scraper = scraper_cls(config=config)
            cap_doc = CapturedDocument(
                document_id=doc_meta["document_id"],
                scraper_id=scraper_id,
                state=doc_meta["state"],
                county=doc_meta["county"],
                court=doc_meta["court_name"],
                source_url=doc_meta["source_url"],
                capture_timestamp=doc_meta["captured_at"],
                content_format=ContentFormat(doc_meta["format"]),
                raw_content=raw_content,
                content_hash=doc_meta["content_hash"],
            )
            parsed = scraper.parse_document(cap_doc)
            extracted["ruling_text"] = parsed.ruling_text or text
            extracted["case_number"] = parsed.case_number or extracted["case_number"]
            extracted["case_title"] = parsed.case_title or extracted["case_title"]
            extracted["judge_name"] = parsed.judge_name
            extracted["outcome"] = parsed.outcome
            extracted["motion_type"] = parsed.motion_type
            extracted["department"] = parsed.department
            extracted["parties"] = parsed.parties
            if parsed.hearing_date:
                extracted["hearing_date"] = (
                    parsed.hearing_date.date()
                    if isinstance(parsed.hearing_date, datetime)
                    else parsed.hearing_date
                )
        except Exception:
            logger.warning(
                "Scraper parse_document failed for %s, falling back to regex",
                doc_meta["document_id"],
                exc_info=True,
            )

    # Fill in any remaining gaps with regex extraction
    if not extracted["judge_name"]:
        extracted["judge_name"] = extract_judge_name(text)
    if not extracted["outcome"]:
        extracted["outcome"] = extract_outcome(text)
    if not extracted["motion_type"]:
        extracted["motion_type"] = extract_motion_type(text)
    if not extracted["case_number"]:
        extracted["case_number"] = extract_case_number(text)

    return extracted


def reingest_batch(
    conn: psycopg.Connection,
    s3_client: object,
    batch_size: int,
    cursor: tuple[datetime, str],
    filters: str,
    filter_params: list,
    dry_run: bool = False,
    concurrency: int = 10,
) -> tuple[int, int, tuple[datetime, str]]:
    """Process one batch. Returns (processed, updated, next_cursor).

    S3 objects are fetched in parallel using a thread pool (controlled by
    ``concurrency``).  DB writes use psycopg3's pipeline mode to amortise
    network round-trips — multiple queries are sent without waiting for
    individual responses at the protocol level.
    """
    processed = 0
    updated = 0
    next_cursor = cursor

    params = filter_params + [cursor[0], cursor[1], batch_size]

    with conn.cursor() as cur:
        cur.execute(
            FETCH_DOCUMENTS_QUERY.format(filters=filters),
            params,
        )
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    # --- Prefetch S3 content in parallel -----------------------------------
    # Parallel S3 fetch — submit all rows with valid s3_key + s3_bucket,
    # then collect results keyed by row index.
    s3_results: dict[int, bytes] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        for idx, row in enumerate(rows):
            s3_key = row[3]
            s3_bucket = row[4]
            if s3_key and s3_bucket:
                future = pool.submit(_fetch_s3_content, s3_client, s3_bucket, s3_key)
                futures[future] = idx

        for future in as_completed(futures):
            idx = futures[future]
            doc_id_str = str(rows[idx][0])
            try:
                s3_results[idx] = future.result()
            except Exception:
                logger.warning(
                    "Failed to fetch S3 content for %s — skipping",
                    doc_id_str,
                    exc_info=True,
                )

    # --- Process rows with pipelined DB writes ------------------------------
    # psycopg3's pipeline mode batches protocol-level messages so that
    # multiple queries are sent without waiting for individual round-trip
    # responses.  Even though some queries within a single document depend
    # on results of prior queries (e.g. upsert_case returns case_id used
    # by insert_document), the pipeline still amortises TCP round-trips
    # across the batch because libpq groups the send/recv cycles.
    with conn.pipeline():
        for idx, row in enumerate(rows):
            (
                doc_id,
                case_id,
                court_id,
                s3_key,
                s3_bucket,
                content_hash,
                source_url,
                scraper_id,
                captured_at,
                hearing_date,
                doc_format,
                state,
                county,
                court_name,
                case_number,
                case_title,
            ) = row
            processed += 1
            doc_id_str = str(doc_id)
            next_cursor = (captured_at, doc_id_str)

            if not s3_key or not s3_bucket:
                logger.warning(
                    "Document %s has no S3 key/bucket — skipping", doc_id_str
                )
                continue

            raw_content = s3_results.get(idx)
            if raw_content is None:
                # S3 fetch failed or was not attempted
                continue

            doc_meta = {
                "document_id": doc_id_str,
                "state": state,
                "county": county,
                "court_name": court_name,
                "source_url": source_url,
                "captured_at": captured_at,
                "content_hash": content_hash,
                "format": doc_format,
                "case_number": case_number,
                "case_title": case_title,
                "hearing_date": hearing_date,
            }

            extracted = _reparse_document(raw_content, scraper_id, doc_meta)

            if dry_run:
                logger.info(
                    "DRY-RUN: %s county=%s judge=%s outcome=%s motion=%s title=%s case=%s parties=%d",
                    doc_id_str,
                    county,
                    extracted["judge_name"],
                    extracted["outcome"],
                    extracted["motion_type"],
                    extracted["case_title"],
                    extracted["case_number"],
                    len(extracted["parties"]),
                )
                continue

            # Re-run through the ingestion pipeline (now with upsert semantics)
            effective_case_number = (
                extracted["case_number"] or case_number or f"UNKNOWN-{doc_id_str}"
            )
            new_case_id = upsert_case(
                conn,
                effective_case_number,
                str(court_id),
                case_title=extracted["case_title"],
            )

            effective_hearing = extracted["hearing_date"] or hearing_date
            insert_document(
                conn,
                document_id=doc_id_str,
                case_id=new_case_id,
                court_id=str(court_id),
                content_format=doc_format,
                content_hash=content_hash,
                s3_key=s3_key,
                s3_bucket=s3_bucket,
                source_url=source_url,
                scraper_id=scraper_id,
                captured_at=captured_at,
                hearing_date=effective_hearing,
            )

            # Resolve judge
            judge_id = None
            if extracted["judge_name"]:
                judge_id = resolve_judge(conn, extracted["judge_name"], str(court_id))

            # Upsert ruling
            if effective_hearing is not None:
                ruling_text = extracted["ruling_text"]
                # Clean ruling text if it's the full raw HTML — take first 50k chars
                if ruling_text and len(ruling_text) > 50000:
                    ruling_text = ruling_text[:50000]

                insert_ruling(
                    conn,
                    document_id=doc_id_str,
                    case_id=new_case_id,
                    court_id=str(court_id),
                    hearing_date=effective_hearing,
                    ruling_text=ruling_text,
                    department=extracted["department"],
                    judge_id=judge_id,
                    outcome=extracted["outcome"],
                    motion_type=extracted["motion_type"],
                )

            if judge_id:
                upsert_case_judge(conn, new_case_id, judge_id, effective_hearing)

            # Parties
            for party_info in extracted.get("parties", []):
                party_name = party_info.get("name", "")
                party_role = party_info.get("role", "")
                if party_name:
                    party_id = upsert_party(conn, party_name)
                    if party_role:
                        upsert_case_party(conn, new_case_id, party_id, party_role)

            updated += 1

    return processed, updated, next_cursor


def run_reingest(
    dsn: str,
    *,
    county: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    batch_size: int = 50,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
) -> dict[str, int]:
    """Run the full reingest. Returns summary stats."""
    filters, filter_params = _build_filters(county, date_from, date_to)

    s3_client = boto3.client("s3")
    total_processed = 0
    total_updated = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated, cursor = reingest_batch(
                conn,
                s3_client,
                effective_batch,
                cursor,
                filters,
                filter_params,
                dry_run=dry_run,
                concurrency=concurrency,
            )
            total_processed += processed
            total_updated += updated

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d updated=%d (total: %d/%d)%s",
                processed,
                updated,
                total_processed,
                total_updated,
                " [dry-run]" if dry_run else " [committed]",
            )

            if processed < effective_batch:
                break

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-ingest documents from S3 with improved extraction.",
    )
    parser.add_argument(
        "--county", type=str, default=None, help="Scope to this county."
    )
    parser.add_argument(
        "--date-from", type=str, default=None, help="YYYY-MM-DD start date."
    )
    parser.add_argument(
        "--date-to", type=str, default=None, help="YYYY-MM-DD end date."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse but don't update DB."
    )
    parser.add_argument(
        "--batch-size", type=int, default=50, help="Batch size (default: 50)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max documents to process."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Number of parallel S3 fetch threads (default: 10).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    date_from = date.fromisoformat(args.date_from) if args.date_from else None
    date_to = date.fromisoformat(args.date_to) if args.date_to else None

    stats = run_reingest(
        dsn,
        county=args.county,
        date_from=date_from,
        date_to=date_to,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
    )

    logger.info(
        "Reingest complete: %d documents processed, %d updated",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
