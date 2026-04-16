#!/usr/bin/env python3
# venv: scraper-framework
"""Re-ingest existing documents from S3 through the ingestion worker pipeline.

**This script operates on existing database records only.** It queries the
``documents`` table for matching rows, then re-processes each one through the
``IngestionWorker.process_event()`` pipeline.  If the county/court has no
records in the database yet (e.g. a newly added county with S3 data but no
prior ingestion), this script will process 0 documents.  For initial
population from S3, use ``rebuild_db.py --county <name>`` instead -- it
discovers documents directly from S3 keys and does not require pre-existing
database records.

For each document in the database, fetches the raw content from S3, builds
a synthetic event dict, and pushes it through
``IngestionWorker.process_event()`` -- the same code path used by the live
ingestion worker.  This ensures that any improvements to the worker
(extraction logic, enrichment, judge resolution, etc.) automatically apply
to reingests.

This is the "fix and re-run" mechanism: after improving extraction logic,
run this script for the affected court/date range to update all records.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/reingest_from_s3.py \\
            --county "Los Angeles" --date-from 2026-01-01

Options:
    --county NAME       Scope to documents from this county.
    --date-from DATE    Only re-ingest documents captured on or after this date.
    --date-to DATE      Only re-ingest documents captured on or before this date.
    --dry-run           Show what would be updated, but don't write to DB.
    --batch-size N      Number of documents per batch (default: 25).
    --limit N           Maximum total documents to re-ingest.
    --concurrency N     Number of parallel S3 fetch threads (default: 10).
    --case-number-like PATTERN
                        Only re-ingest documents whose associated case_number
                        matches this PostgreSQL LIKE pattern.  Useful for
                        targeting placeholder case numbers, e.g.
                        --case-number-like 'UNKNOWN-%%'
    --case-title-regex PATTERN
                        Only re-ingest documents whose current case_title
                        matches this PostgreSQL regex (~ operator).  Useful
                        for targeting garbled titles, e.g.
                        --case-title-regex 'vs\\.?\\s*$|(?i)(Before the Court|moves the)'
    --null-motion-type  Only re-ingest documents whose associated rulings
                        have NULL motion_type.  Useful after a normalization
                        backfill that set unmappable motion types to NULL --
                        re-ingestion lets the enrichment pipeline extract
                        motion_type from ruling text.
    --orphaned-only     Only re-ingest documents that have no associated
                        ruling records. Useful after a backfill that created
                        document records but did not process them through
                        transcription/enrichment.
    --no-llm            Disable LLM extraction, use regex-only mode.
    --checkpoint-file PATH
                        Write cursor position and cumulative stats to this
                        JSON file after each batch. Enables resumption on
                        interruption via --resume.
    --resume            Resume from the checkpoint saved by --checkpoint-file.
                        Reads the saved cursor and stats, then continues from
                        where the previous run left off. Requires
                        --checkpoint-file to point to an existing file.
    --prefix PREFIX     Scan S3 directly by key prefix instead of querying the
                        database.  Useful for ingesting documents that were
                        never written to the DB (e.g. dead-lettered events).
                        Lists S3 objects under the prefix, seeds court records,
                        and processes each document through the ingestion
                        pipeline via IngestionWorker.process_event().
                        Example: --prefix federal/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
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

from framework.logging import configure_structlog  # noqa: E402
from ingestion.worker import IngestionWorker  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()


FETCH_DOCUMENTS_QUERY = """
    SELECT
        d.id, d.case_id, d.court_id, d.s3_key, d.s3_bucket,
        d.content_hash, d.source_url, d.scraper_id, d.captured_at,
        d.hearing_date, d.format,
        ct.state, ct.county, ct.court_name,
        c.case_number, c.case_title,
        (SELECT r.hearing_date FROM rulings r
         WHERE r.document_id = d.id LIMIT 1) AS ruling_hearing_date,
        (SELECT r.ruling_text FROM rulings r
         WHERE r.document_id = d.id LIMIT 1) AS stored_ruling_text
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


# ---------------------------------------------------------------------------
# Checkpoint / resume helpers
# ---------------------------------------------------------------------------

_CHECKPOINT_VERSION = 1


def _write_checkpoint(
    checkpoint_path: Path,
    cursor: tuple[datetime, str],
    stats: dict[str, Any],
) -> None:
    """Write a checkpoint file with the current cursor and cumulative stats.

    The checkpoint is written atomically: first to a temporary sibling file,
    then renamed into place.  This prevents a crash during write from leaving
    a truncated (unreadable) checkpoint file.

    Parameters
    ----------
    checkpoint_path:
        Destination file path.
    cursor:
        Current ``(captured_at, document_id)`` keyset pagination cursor.
    stats:
        Cumulative processing stats to persist (processed, updated, etc.).
    """
    data = {
        "version": _CHECKPOINT_VERSION,
        "cursor": {
            "captured_at": cursor[0].isoformat(),
            "document_id": cursor[1],
        },
        "stats": stats,
        "updated_at": datetime.now(tz=None).isoformat(),
    }
    tmp_path = checkpoint_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.rename(checkpoint_path)


def _read_checkpoint(
    checkpoint_path: Path,
) -> tuple[tuple[datetime, str], dict[str, Any]]:
    """Read a checkpoint file and return ``(cursor, stats)``.

    Parameters
    ----------
    checkpoint_path:
        Path to the checkpoint JSON file written by ``_write_checkpoint``.

    Returns
    -------
    tuple
        ``(cursor, stats)`` where *cursor* is ``(captured_at, document_id)``
        and *stats* is the cumulative stats dict from the checkpoint.

    Raises
    ------
    FileNotFoundError
        If the checkpoint file does not exist.
    ValueError
        If the file is not valid checkpoint JSON or has an unsupported version.
    """
    raw = checkpoint_path.read_text(encoding="utf-8")
    data = json.loads(raw)

    if data.get("version") != _CHECKPOINT_VERSION:
        msg = (
            f"Unsupported checkpoint version {data.get('version')}; "
            f"expected {_CHECKPOINT_VERSION}"
        )
        raise ValueError(msg)

    cursor_data = data["cursor"]
    captured_at = datetime.fromisoformat(cursor_data["captured_at"])
    document_id = cursor_data["document_id"]
    stats = data.get("stats", {})
    return (captured_at, document_id), stats


def _build_filters(
    county: str | None,
    date_from: date | None,
    date_to: date | None,
    case_title_regex: str | None = None,
    null_motion_type: bool = False,
    orphaned_only: bool = False,
    case_number_like: str | None = None,
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
    if case_number_like:
        clauses.append("AND c.case_number LIKE %s")
        params.append(case_number_like)
    if case_title_regex:
        clauses.append("AND c.case_title ~ %s")
        params.append(case_title_regex)
    if null_motion_type:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM rulings r"
            " WHERE r.document_id = d.id AND r.motion_type IS NULL)"
        )
    if orphaned_only:
        clauses.append(
            "AND NOT EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = d.id)"
        )
    return " ".join(clauses), params


def _fetch_s3_content(s3_client: object, bucket: str, key: str) -> bytes:
    """Fetch raw content from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
    return response["Body"].read()  # type: ignore[index]


def _build_reingest_event(
    doc_meta: dict[str, Any],
    raw_content: bytes,
) -> dict[str, Any]:
    """Build an ingestion event dict from a DB document row and S3 content.

    Constructs the event dict in the same format that
    ``IngestionWorker.process_event()`` expects, matching the schema used by
    live scrapers when they publish ``document.captured`` events.

    Parameters
    ----------
    doc_meta:
        Document metadata from the ``FETCH_DOCUMENTS_QUERY`` row.
    raw_content:
        Raw document content fetched from S3.

    Returns
    -------
    dict
        Event dict ready for ``process_event()``.
    """
    doc_format = doc_meta.get("format", "html")

    # For text-based formats (HTML, TXT), pass content as ruling_text decoded
    # as UTF-8.  For binary formats (PDF), pass raw bytes as latin-1 string
    # -- the worker handles binary PDF detection and text extraction.
    if doc_format in ("html", "txt"):
        ruling_text = raw_content.decode("utf-8", errors="replace")
    elif doc_format == "pdf":
        ruling_text = raw_content.decode("latin-1")
    else:
        ruling_text = raw_content.decode("utf-8", errors="replace")

    # Build the capture timestamp.
    captured_at = doc_meta.get("captured_at")
    capture_ts = (
        captured_at.isoformat() if captured_at else datetime.now(UTC).isoformat()
    )

    # Use document hearing_date, falling back to the ruling's hearing_date.
    # documents.hearing_date is nullable; some scrapers don't provide it in
    # the capture event.  The ruling's hearing_date (NOT NULL in DB) is the
    # fallback.
    effective_hearing = doc_meta.get("hearing_date") or doc_meta.get(
        "ruling_hearing_date"
    )
    hearing_str = str(effective_hearing) if effective_hearing else None

    event: dict[str, Any] = {
        "document_id": doc_meta["document_id"],
        "state": doc_meta["state"],
        "county": doc_meta["county"],
        "court": doc_meta["court_name"],
        "case_number": doc_meta.get("case_number"),
        "case_title": doc_meta.get("case_title"),
        "content_format": doc_format,
        "content_hash": doc_meta.get("content_hash", ""),
        "s3_key": doc_meta.get("s3_key"),
        "s3_bucket": doc_meta.get("s3_bucket"),
        "source_url": doc_meta.get("source_url", ""),
        "scraper_id": doc_meta.get("scraper_id", ""),
        "capture_timestamp": capture_ts,
        "ruling_text": ruling_text,
        "hearing_date": hearing_str,
    }

    return event


def _create_worker(
    pg_dsn: str,
    *,
    no_llm: bool = False,
) -> IngestionWorker:
    """Create an IngestionWorker for reingest use.

    Sets up Redis, OpenSearch, and S3 clients matching the environment
    variables.  The worker manages its own DB connection via ``pg_dsn``.

    Parameters
    ----------
    pg_dsn:
        PostgreSQL connection string.
    no_llm:
        If True, pass ``llm_client=None`` to disable LLM extraction.
    """
    import redis as redis_lib
    from unittest.mock import MagicMock

    from framework.s3_cache import make_s3_client as _make_s3

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    os_url = os.environ.get("OPENSEARCH_URL", "")
    bucket = os.environ.get(
        "JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev"
    )

    rc = redis_lib.Redis.from_url(redis_url, decode_responses=False)

    if os_url:
        from opensearchpy import OpenSearch

        os_kwargs: dict = {"hosts": [os_url]}
        os_user = os.environ.get("OPENSEARCH_USERNAME", "")
        os_pass = os.environ.get("OPENSEARCH_PASSWORD", "")
        if os_user and os_pass:
            os_kwargs["http_auth"] = (os_user, os_pass)
        os_client = OpenSearch(**os_kwargs)
    else:
        os_client = MagicMock()

    s3_for_worker = _make_s3()

    # When --no-llm is specified, we need to prevent the worker from
    # auto-creating an LLM client from environment variables.  The
    # constructor calls ``create_llm_client()`` when llm_client is None.
    # Pass a MagicMock as a sentinel, then immediately override to None
    # after construction so the worker treats LLM as disabled.
    worker = IngestionWorker(
        redis_client=rc,
        pg_dsn=pg_dsn,
        opensearch_client=os_client,
        s3_client=s3_for_worker,
        archive_bucket=bucket,
        **({"llm_client": MagicMock()} if no_llm else {}),
    )
    if no_llm:
        worker._llm_client = None

    return worker


def reingest_batch(
    conn: psycopg.Connection,
    s3_client: object,
    worker: IngestionWorker,
    batch_size: int,
    cursor: tuple[datetime, str],
    filters: str,
    filter_params: list,
    dry_run: bool = False,
    concurrency: int = 10,
    running_processed: int = 0,
    running_updated: int = 0,
    batch_number: int = 0,
) -> dict[str, Any]:
    """Process one batch of documents through the ingestion worker.

    Fetches documents from the DB, pre-fetches S3 content in parallel,
    builds event dicts, and passes each through
    ``IngestionWorker.process_event()``.

    Returned dict keys:
      - ``processed``: number of documents iterated over
      - ``updated``: number of documents successfully processed
      - ``next_cursor``: cursor for the next batch
      - ``failed``: number of documents that failed
      - ``skipped``: number of documents skipped (no S3 key, fetch failure)
      - ``batch_number``: batch sequence number

    S3 objects are fetched in parallel using a thread pool (controlled by
    ``concurrency``).  Document processing is sequential since each
    ``process_event()`` call manages its own DB transaction.

    *running_processed* and *running_updated* are cumulative totals from
    prior batches, used for progress logging.
    """
    processed = 0
    updated = 0
    failed = 0
    skipped = 0
    next_cursor = cursor

    params = filter_params + [cursor[0], cursor[1], batch_size]

    with conn.cursor() as cur:
        cur.execute(
            FETCH_DOCUMENTS_QUERY.format(filters=filters),
            params,
        )
        rows = cur.fetchall()

    if not rows:
        return {
            "processed": 0,
            "updated": 0,
            "next_cursor": cursor,
            "failed": 0,
            "skipped": 0,
            "batch_number": batch_number,
        }

    # --- Prefetch S3 content in parallel -----------------------------------
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
                content = future.result()
                s3_results[idx] = content
            except Exception:
                logger.warning(
                    "Failed to fetch S3 content, skipping",
                    document_id=doc_id_str,
                    exc_info=True,
                )

    # --- Process each document through the worker --------------------------
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
            ruling_hearing_date,
            stored_ruling_text,
        ) = row
        processed += 1
        doc_id_str = str(doc_id)
        next_cursor = (captured_at, doc_id_str)

        if not s3_key or not s3_bucket:
            logger.warning(
                "Document has no S3 key/bucket, skipping", document_id=doc_id_str
            )
            skipped += 1
            continue

        raw_content = s3_results.get(idx)
        if raw_content is None:
            # S3 fetch failed or was not attempted
            skipped += 1
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
            "ruling_hearing_date": ruling_hearing_date,
            "court_id": str(court_id),
            "scraper_id": scraper_id,
            "s3_key": s3_key,
            "s3_bucket": s3_bucket,
        }

        if dry_run:
            logger.info(
                "DRY-RUN",
                document_id=doc_id_str,
                county=county,
                case_number=case_number,
                case_title=case_title,
                scraper_id=scraper_id,
            )
            continue

        # Build event dict and process through the worker
        event = _build_reingest_event(doc_meta, raw_content)

        try:
            worker.process_event(event)
            updated += 1

            logger.info(
                "Committed document",
                document_id=doc_id_str,
                total_processed=running_processed + processed,
                total_updated=running_updated + updated,
            )
        except Exception:
            failed += 1
            logger.error(
                "process_event failed, skipping (batch continues)",
                document_id=doc_id_str,
                exc_info=True,
            )

    return {
        "processed": processed,
        "updated": updated,
        "next_cursor": next_cursor,
        "failed": failed,
        "skipped": skipped,
        "batch_number": batch_number,
    }


# ---------------------------------------------------------------------------
# Quality metrics queries -- spotcheck data quality before/after reingest
# ---------------------------------------------------------------------------

# SQL queries for data quality metrics.  Each returns a single integer count.
# All queries filter on active documents only.
_QUALITY_QUERIES: dict[str, str] = {
    "truncated_vs_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title ~ 'vs\\.?\\s*$'
        {county_filter}
    """,
    "header_merge_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title ~ '(?i)(Before the Court|moves the|Hearing on|Motion for)'
          AND c.case_title ~ ' vs?\\.? '
          AND length(c.case_title) > 100
        {county_filter}
    """,
    "null_case_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title IS NULL
        {county_filter}
    """,
    "missing_parties": """
        SELECT COUNT(DISTINCT c.id) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        LEFT JOIN case_parties cp ON cp.case_id = c.id
        WHERE cp.id IS NULL
        {county_filter}
    """,
    "all_caps_titles": """
        SELECT COUNT(*) FROM cases c
        JOIN documents d ON d.case_id = c.id AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE c.case_title = upper(c.case_title)
          AND c.case_title IS NOT NULL
          AND length(c.case_title) > 5
        {county_filter}
    """,
    "short_ruling_text": """
        SELECT COUNT(*) FROM rulings r
        JOIN documents d ON d.id::text = r.document_id::text AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE length(r.ruling_text) < 20
        {county_filter}
    """,
    "long_ruling_text": """
        SELECT COUNT(*) FROM rulings r
        JOIN documents d ON d.id::text = r.document_id::text AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE length(r.ruling_text) > 30000
        {county_filter}
    """,
    "total_rulings": """
        SELECT COUNT(*) FROM rulings r
        JOIN documents d ON d.id::text = r.document_id::text AND d.status = 'active'
        JOIN courts ct ON ct.id = d.court_id
        WHERE 1=1
        {county_filter}
    """,
}


def _run_quality_queries(
    conn: psycopg.Connection,
    county: str | None = None,
) -> dict[str, int]:
    """Run all quality metric queries and return a dict of counts."""
    county_filter = ""
    params: list = []
    if county:
        county_filter = "AND ct.county = %s"
        params.append(county)

    results: dict[str, int] = {}
    with conn.cursor() as cur:
        for name, query in _QUALITY_QUERIES.items():
            cur.execute(query.format(county_filter=county_filter), params)
            row = cur.fetchone()
            results[name] = row[0] if row else 0
    return results


# ---------------------------------------------------------------------------
# Prefix mode -- scan S3 directly for documents not in the DB
# ---------------------------------------------------------------------------

# S3 content-addressed key pattern: {state}/{county}/{court}/raw/{content_hash}.{ext}
_S3_KEY_PATTERN = re.compile(
    r"^(?P<state>[^/]+)/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/"
    r"(?P<content_hash>[0-9a-f]+)\.(?P<ext>\w+)$"
)

_EXT_TO_FORMAT = {"html": "html", "pdf": "pdf", "docx": "docx", "txt": "txt"}

# Timezone lookup by state (expand as states are added)
_STATE_TIMEZONES = {
    "ca": "America/Los_Angeles",
    "tx": "America/Chicago",
    "ny": "America/New_York",
}


def _unsluggify(s: str) -> str:
    """Convert slug to display name: 'los_angeles' -> 'Los Angeles', 'ca' -> 'CA'."""
    if len(s) <= 2:
        return s.upper()
    return s.replace("_", " ").title()


def _parse_s3_key(key: str) -> dict[str, str] | None:
    """Extract metadata from a content-addressed S3 key.

    Returns a dict with keys ``state``, ``county``, ``court``,
    ``content_hash``, and ``ext``, or ``None`` if the key doesn't
    match the expected pattern.
    """
    m = _S3_KEY_PATTERN.match(key)
    if not m:
        return None
    return m.groupdict()


def _list_s3_keys(s3_client: object, bucket: str, prefix: str) -> list[str]:
    """List all content-addressed S3 keys under *prefix*."""
    paginator = s3_client.get_paginator("list_objects_v2")  # type: ignore[union-attr]
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if _S3_KEY_PATTERN.match(key):
                keys.append(key)
    return keys


def _derive_court_code(state: str, county: str) -> str:
    """Derive a URL-safe court code from state + county.

    Must match the canonical format in ``ingestion.db._derive_court_code``
    so that ``ON CONFLICT (court_code)`` upserts hit the same row the
    ingestion worker creates.  See #2373.
    """
    return f"{state.lower()}-{county.lower().replace(' ', '-')}"


def _discover_courts(keys: list[str]) -> list[dict[str, str]]:
    """Derive unique courts from S3 key prefixes.

    Uses the same ``{state}-{county}`` court_code format as the ingestion
    worker so that ``ON CONFLICT (court_code)`` in ``_seed_courts`` merges
    with existing rows instead of creating duplicates.  See #2373.
    """
    seen: set[str] = set()
    courts: list[dict[str, str]] = []
    for key in keys:
        parsed = _parse_s3_key(key)
        if not parsed:
            continue
        state = _unsluggify(parsed["state"])
        county = _unsluggify(parsed["county"])
        court_name = _unsluggify(parsed["court"])
        court_code = _derive_court_code(state, county)
        if court_code in seen:
            continue
        seen.add(court_code)
        courts.append(
            {
                "state": state,
                "county": county,
                "court_name": f"{court_name}, County of {county}",
                "court_code": court_code,
                "timezone": _STATE_TIMEZONES.get(
                    parsed["state"], "America/Los_Angeles"
                ),
            }
        )
    return courts


def _seed_courts(
    conn: psycopg.Connection, courts: list[dict[str, str]]
) -> dict[str, str]:
    """Insert or update court records. Returns ``{court_code: court_id}``."""
    court_ids: dict[str, str] = {}
    with conn.cursor() as cur:
        for court in courts:
            cur.execute(
                """
                INSERT INTO courts (state, county, court_name, court_code, timezone)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (court_code) DO UPDATE
                    SET state = EXCLUDED.state,
                        county = EXCLUDED.county,
                        court_name = EXCLUDED.court_name
                RETURNING id
                """,
                (
                    court["state"],
                    court["county"],
                    court["court_name"],
                    court["court_code"],
                    court["timezone"],
                ),
            )
            row = cur.fetchone()
            court_ids[court["court_code"]] = str(row[0])
    conn.commit()
    return court_ids


def _build_prefix_event(
    key: str,
    content: bytes,
    parsed: dict[str, str],
    bucket: str,
) -> dict[str, Any]:
    """Construct an ingestion event dict from an S3 object for prefix mode."""
    content_hash = parsed["content_hash"]
    document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, content_hash))
    content_format = _EXT_TO_FORMAT.get(parsed["ext"], "bin")

    event: dict[str, Any] = {
        "document_id": document_id,
        "state": _unsluggify(parsed["state"]),
        "county": _unsluggify(parsed["county"]),
        "court": _unsluggify(parsed["court"]),
        "content_format": content_format,
        "content_hash": content_hash,
        "s3_key": key,
        "s3_bucket": bucket,
        "scraper_id": f"reingest-{parsed['state']}-{parsed['county']}",
        "source_url": "",
        "capture_timestamp": datetime.now(UTC).isoformat(),
    }

    # For text-based formats (HTML, TXT), pass content as ruling_text.
    # For binary formats (PDF, DOCX), pass raw bytes as latin-1 string
    # (the worker handles extraction from binary formats).
    if content_format in ("html", "txt"):
        event["ruling_text"] = content.decode("utf-8", errors="replace")
    elif content_format in ("pdf", "docx"):
        event["ruling_text"] = content.decode("latin-1")

    return event


def _process_prefix_document(
    key: str,
    bucket: str,
    database_url: str,
    redis_url: str,
    os_url: str,
) -> str:
    """Process a single S3 object through the ingestion pipeline.

    Creates its own DB connection, Redis client, and IngestionWorker.
    Designed for ProcessPoolExecutor -- each call is fully independent.
    """
    parsed = _parse_s3_key(key)
    if not parsed:
        return "skip"

    from framework.s3_cache import make_s3_client as _make_s3

    s3 = _make_s3()
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()
    except Exception:
        logger.warning("Failed to fetch S3 object, skipping", s3_key=key, exc_info=True)
        return "error"

    if not content:
        return "skip"

    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != parsed["content_hash"]:
        logger.error(
            "S3 content hash mismatch, skipping document",
            s3_key=key,
            key_hash=parsed["content_hash"],
            content_hash=actual_hash,
        )
        return "error"

    event = _build_prefix_event(key, content, parsed, bucket)

    # Lazy per-process worker -- cached on the function object.
    worker = getattr(_process_prefix_document, "_worker", None)
    if worker is None:
        import redis as redis_lib
        from unittest.mock import MagicMock

        rc = redis_lib.Redis.from_url(redis_url, decode_responses=False)
        if os_url:
            from opensearchpy import OpenSearch

            os_kwargs: dict = {"hosts": [os_url]}
            os_user = os.environ.get("OPENSEARCH_USERNAME", "")
            os_pass = os.environ.get("OPENSEARCH_PASSWORD", "")
            if os_user and os_pass:
                os_kwargs["http_auth"] = (os_user, os_pass)
            os_client = OpenSearch(**os_kwargs)
        else:
            os_client = MagicMock()
        s3_for_worker = _make_s3()
        worker = IngestionWorker(
            redis_client=rc,
            pg_dsn=database_url,
            opensearch_client=os_client,
            s3_client=s3_for_worker,
            archive_bucket=bucket,
        )
        _process_prefix_document._worker = worker  # type: ignore[attr-defined]

    try:
        worker.process_event(event)
        return "ok"
    except Exception:
        logger.warning("Failed to process document", s3_key=key, exc_info=True)
        return "error"


def run_reingest_from_prefix(
    dsn: str,
    *,
    prefix: str,
    concurrency: int = 10,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Scan S3 by key prefix and ingest documents not in the DB.

    Unlike :func:`run_reingest`, which queries existing DB records for
    re-processing, this function lists S3 objects directly and feeds each
    one through ``IngestionWorker.process_event()``.  This is the correct
    approach when documents were captured and archived to S3 but never
    written to the database (e.g. dead-lettered events).

    Parameters
    ----------
    dsn:
        PostgreSQL connection string.
    prefix:
        S3 key prefix to scan (e.g. ``"federal/"``).
    concurrency:
        Number of parallel worker processes.
    limit:
        Maximum number of documents to process.
    dry_run:
        If True, list keys and seed courts but skip document processing.

    Returns
    -------
    dict with keys: ``total_keys``, ``processed``, ``errors``, ``skipped``.
    """
    bucket = os.environ.get(
        "JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev"
    )
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    os_url = os.environ.get("OPENSEARCH_URL", "")

    s3_client = boto3.client("s3")

    # Step 1: List S3 keys matching the prefix.
    logger.info("Listing S3 objects...", prefix=prefix, bucket=bucket)
    keys = _list_s3_keys(s3_client, bucket, prefix)
    logger.info("Found S3 objects", count=len(keys), prefix=prefix)

    if not keys:
        logger.warning("No content-addressed keys found under prefix", prefix=prefix)
        return {
            "total_keys": 0,
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }

    if limit is not None:
        total_available = len(keys)
        keys = keys[:limit]
        logger.info(
            "Limited to first N keys", limit=limit, total_available=total_available
        )

    # Step 2: Discover and seed courts from key prefixes.
    courts = _discover_courts(keys)
    logger.info(
        "Discovered courts from S3 keys",
        count=len(courts),
        courts=[c["court_code"] for c in courts],
    )

    with psycopg.connect(dsn) as conn:
        _seed_courts(conn, courts)

    if dry_run:
        logger.info("DRY-RUN: would process %d documents", len(keys))
        return {
            "total_keys": len(keys),
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }

    # Step 3: Process documents in parallel.
    processed = 0
    errors = 0
    skipped_count = 0

    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        future_to_key = {
            pool.submit(
                _process_prefix_document,
                key,
                bucket,
                dsn,
                redis_url,
                os_url,
            ): key
            for key in keys
        }

        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                result = future.result()
            except Exception:
                logger.error("Unhandled error processing %s", key, exc_info=True)
                errors += 1
                continue

            if result == "ok":
                processed += 1
            elif result == "error":
                errors += 1
            else:
                skipped_count += 1

    return {
        "total_keys": len(keys),
        "processed": processed,
        "errors": errors,
        "skipped": skipped_count,
    }


def run_reingest(
    dsn: str,
    *,
    county: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    batch_size: int = 25,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
    no_llm: bool = False,
    case_title_regex: str | None = None,
    null_motion_type: bool = False,
    orphaned_only: bool = False,
    report_metrics: bool = False,
    checkpoint_file: str | None = None,
    resume: bool = False,
    case_number_like: str | None = None,
) -> dict[str, Any]:
    """Run the full reingest. Returns summary stats.

    Parameters
    ----------
    checkpoint_file:
        If provided, the cursor position and cumulative stats are written to
        this file after each batch.  On interruption, the run can be resumed
        from the checkpoint by passing ``resume=True``.
    resume:
        When *True* **and** *checkpoint_file* points to an existing file,
        the cursor and cumulative stats are restored from the checkpoint
        instead of starting from the beginning.  Requires *checkpoint_file*.
    """
    filters, filter_params = _build_filters(
        county,
        date_from,
        date_to,
        case_title_regex=case_title_regex,
        null_motion_type=null_motion_type,
        orphaned_only=orphaned_only,
        case_number_like=case_number_like,
    )

    s3_client = boto3.client("s3")

    # Create the IngestionWorker that will process all documents.  This is
    # the key change from the old architecture: instead of reimplementing
    # extraction and DB writes, we delegate to the same worker used by the
    # live ingestion pipeline.
    worker = _create_worker(dsn, no_llm=no_llm)

    if no_llm:
        logger.info("LLM extraction disabled via --no-llm flag")
    else:
        logger.info("LLM extraction enabled (via IngestionWorker)")

    total_processed = 0
    total_updated = 0
    total_failed = 0
    total_skipped = 0
    total_batches = 0
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    # Resolve checkpoint path (if provided).
    cp_path: Path | None = None
    if checkpoint_file:
        cp_path = Path(checkpoint_file)

    # Resume from checkpoint if requested.
    if resume and cp_path is not None and cp_path.exists():
        restored_cursor, restored_stats = _read_checkpoint(cp_path)
        cursor = restored_cursor
        total_processed = restored_stats.get("total_processed", 0)
        total_updated = restored_stats.get("total_updated", 0)
        total_failed = restored_stats.get("total_failed", 0)
        total_skipped = restored_stats.get("total_skipped", 0)
        total_batches = restored_stats.get("total_batches", 0)
        logger.info(
            "Resumed from checkpoint",
            checkpoint_file=str(cp_path),
            cursor_captured_at=cursor[0].isoformat(),
            cursor_document_id=cursor[1],
            total_processed=total_processed,
        )

    t0 = time.monotonic()

    # Collect quality metrics before reingest if requested.
    before_metrics: dict[str, int] | None = None
    if report_metrics:
        with psycopg.connect(dsn) as metrics_conn:
            before_metrics = _run_quality_queries(metrics_conn, county)
            logger.info("quality_metrics_before", **before_metrics)

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            total_batches += 1

            logger.info(
                "batch_start",
                batch_number=total_batches,
                batch_size=effective_batch,
            )

            batch_result = reingest_batch(
                conn,
                s3_client,
                worker,
                effective_batch,
                cursor,
                filters,
                filter_params,
                dry_run=dry_run,
                concurrency=concurrency,
                running_processed=total_processed,
                running_updated=total_updated,
                batch_number=total_batches,
            )
            processed = batch_result["processed"]
            updated = batch_result["updated"]
            cursor = batch_result["next_cursor"]
            total_processed += processed
            total_updated += updated
            total_failed += batch_result["failed"]
            total_skipped += batch_result["skipped"]

            if dry_run:
                conn.rollback()

            logger.info(
                "batch_complete",
                batch_number=total_batches,
                processed=processed,
                updated=updated,
                total_processed=total_processed,
                total_updated=total_updated,
                mode="dry-run" if dry_run else "committed",
            )

            # Persist checkpoint after each batch so we can resume on
            # interruption without re-processing already-finished documents.
            # Skip checkpoint writes in dry-run mode -- a dry run should not
            # produce side effects that could cause a subsequent real run
            # with --resume to skip documents (#1925).
            if cp_path is not None and not dry_run:
                _write_checkpoint(
                    cp_path,
                    cursor,
                    {
                        "total_processed": total_processed,
                        "total_updated": total_updated,
                        "total_failed": total_failed,
                        "total_skipped": total_skipped,
                        "total_batches": total_batches,
                    },
                )

            if processed < effective_batch:
                break

    wall_time = round(time.monotonic() - t0, 2)

    # Collect quality metrics after reingest if requested.
    after_metrics: dict[str, int] | None = None
    metrics_delta: dict[str, int] | None = None
    if report_metrics:
        with psycopg.connect(dsn) as metrics_conn:
            after_metrics = _run_quality_queries(metrics_conn, county)
            logger.info("quality_metrics_after", **after_metrics)
        if before_metrics is not None and after_metrics is not None:
            metrics_delta = {
                k: after_metrics.get(k, 0) - before_metrics.get(k, 0)
                for k in before_metrics
            }
            logger.info("quality_metrics_delta", **metrics_delta)

    # Clean up the worker's DB connection.
    worker.close()

    logger.info(
        "reingest_complete",
        total_processed=total_processed,
        total_updated=total_updated,
        total_failed=total_failed,
        total_skipped=total_skipped,
        total_batches=total_batches,
        wall_time_seconds=wall_time,
    )

    result: dict[str, Any] = {
        "total_processed": total_processed,
        "total_updated": total_updated,
        "total_failed": total_failed,
        "total_skipped": total_skipped,
        "total_batches": total_batches,
        "wall_time_seconds": wall_time,
    }
    if before_metrics is not None:
        result["quality_before"] = before_metrics
    if after_metrics is not None:
        result["quality_after"] = after_metrics
    if metrics_delta is not None:
        result["quality_delta"] = metrics_delta
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-ingest documents from S3 through the ingestion worker.",
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
        "--batch-size", type=int, default=25, help="Batch size (default: 25)."
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
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM extraction, use regex-only mode.",
    )
    parser.add_argument(
        "--case-number-like",
        type=str,
        default=None,
        help=(
            "Only re-ingest documents whose associated case_number matches "
            "this PostgreSQL LIKE pattern. Useful for targeting placeholder "
            "case numbers, e.g. 'UNKNOWN-%%' to find UNKNOWN-UUID cases."
        ),
    )
    parser.add_argument(
        "--case-title-regex",
        type=str,
        default=None,
        help=(
            "Only re-ingest documents whose current case_title matches this "
            "PostgreSQL regex (~ operator). Useful for targeting garbled "
            "titles, e.g. 'vs\\.?\\s*$' to find truncated titles."
        ),
    )
    parser.add_argument(
        "--null-motion-type",
        action="store_true",
        help=(
            "Only re-ingest documents whose associated rulings have NULL "
            "motion_type. Useful after a normalization backfill that set "
            "unmappable motion types to NULL -- re-ingestion lets the "
            "enrichment pipeline extract motion_type from ruling text."
        ),
    )
    parser.add_argument(
        "--orphaned-only",
        action="store_true",
        help=(
            "Only re-ingest documents that have no associated ruling "
            "records. Useful after a backfill that created document "
            "records but did not process them through transcription "
            "and enrichment."
        ),
    )
    parser.add_argument(
        "--report-metrics",
        action="store_true",
        help=(
            "Run data quality spotcheck queries before and after reingest "
            "and report the comparison. Checks truncated titles, header-merge "
            "titles, null titles, missing parties, ALL CAPS titles, "
            "short/long ruling text, and total ruling counts."
        ),
    )
    parser.add_argument(
        "--checkpoint-file",
        type=str,
        default=None,
        help=(
            "Path to a JSON checkpoint file. After each batch, the current "
            "cursor position and cumulative stats are written here. Use with "
            "--resume to restart from the last checkpoint on interruption."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume from the checkpoint file specified by --checkpoint-file. "
            "Reads the saved cursor position and cumulative stats, then "
            "continues processing from where the previous run left off. "
            "Requires --checkpoint-file to point to an existing checkpoint."
        ),
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help=(
            "Scan S3 directly by key prefix instead of querying the database. "
            "Useful for ingesting documents that were never written to the DB "
            "(e.g. dead-lettered events). Lists S3 objects under the prefix, "
            "seeds court records, and processes each document through the "
            "ingestion pipeline via IngestionWorker.process_event(). "
            "Example: --prefix federal/"
        ),
    )
    args = parser.parse_args()

    if args.resume and not args.checkpoint_file:
        parser.error("--resume requires --checkpoint-file to be specified.")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    # Prefix mode: scan S3 directly for documents not in the DB.
    if args.prefix:
        stats = run_reingest_from_prefix(
            dsn,
            prefix=args.prefix,
            concurrency=args.concurrency,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        logger.info(
            "Prefix reingest complete",
            total_keys=stats["total_keys"],
            processed=stats["processed"],
            errors=stats["errors"],
            skipped=stats["skipped"],
        )
        return

    # Standard mode: query the database for existing document records.
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
        no_llm=args.no_llm,
        case_title_regex=args.case_title_regex,
        null_motion_type=args.null_motion_type,
        orphaned_only=args.orphaned_only,
        report_metrics=args.report_metrics,
        checkpoint_file=args.checkpoint_file,
        resume=args.resume,
        case_number_like=args.case_number_like,
    )

    logger.info(
        "Reingest complete",
        total_processed=stats["total_processed"],
        total_updated=stats["total_updated"],
        total_failed=stats.get("total_failed", 0),
        total_skipped=stats.get("total_skipped", 0),
    )


if __name__ == "__main__":
    main()
