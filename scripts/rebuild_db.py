#!/usr/bin/env python3
# venv: scraper-framework
"""Rebuild the entire database from S3 archived content.
#
# Lists S3 objects (or local cache), derives courts from key prefixes,
# and feeds each document through the ingestion pipeline.
#
# Usage (local, with S3 cache):
#   S3_CACHE_DIR=/tmp/judgemind-archive \
#   DATABASE_URL=postgres://judgemind:localdev@localhost:5432/judgemind \
#   REDIS_URL=redis://localhost:6379 \
#   scripts/run-py.sh scripts/rebuild_db.py
#
# Usage (ECS, against dev):
#   scripts/ecs-run-task.sh scripts/rebuild_db.py
#
# Options:
#   --county NAME     Only process documents from this county (e.g. Ventura)
#   --state CODE      State code for --county filtering (default: ca)
#   --concurrency N   Number of parallel processes (default: 64)
#   --reset           Truncate derived tables before rebuilding
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import structlog

from framework.s3_cache import make_s3_client

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger(__name__)

BUCKET = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev")

# Content-addressed key pattern: {state}/{county}/{court}/raw/{content_hash}.{ext}
KEY_PATTERN = re.compile(
    r"^(?P<state>[^/]+)/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/(?P<content_hash>[0-9a-f]+)\.(?P<ext>\w+)$"
)

EXT_TO_FORMAT = {"html": "html", "pdf": "pdf", "docx": "docx", "txt": "txt"}

# Timezone lookup by state (expand as we add states)
STATE_TIMEZONES = {
    "ca": "America/Los_Angeles",
    "tx": "America/Chicago",
    "ny": "America/New_York",
}


def unsluggify(s: str) -> str:
    """Convert 'los_angeles' → 'Los Angeles', 'ca' → 'CA'."""
    if len(s) <= 2:
        return s.upper()
    return s.replace("_", " ").title()


def sluggify(s: str) -> str:
    """Convert 'Los Angeles' → 'los_angeles', 'CA' → 'ca'."""
    return s.lower().replace(" ", "_")


def parse_s3_key(key: str) -> dict[str, str] | None:
    """Extract metadata from a content-addressed S3 key."""
    m = KEY_PATTERN.match(key)
    if not m:
        return None
    return m.groupdict()


def discover_courts(keys: list[str]) -> list[dict[str, str]]:
    """Derive unique courts from S3 key prefixes."""
    seen: set[str] = set()
    courts: list[dict[str, str]] = []
    for key in keys:
        parsed = parse_s3_key(key)
        if not parsed:
            continue
        court_code = f"{parsed['state']}_{parsed['county']}_{parsed['court']}"
        if court_code in seen:
            continue
        seen.add(court_code)
        courts.append(
            {
                "state": unsluggify(parsed["state"]),
                "county": unsluggify(parsed["county"]),
                "court_name": unsluggify(parsed["court"]),
                "court_code": court_code,
                "timezone": STATE_TIMEZONES.get(parsed["state"], "America/Los_Angeles"),
            }
        )
    return courts


def seed_courts(
    conn: psycopg.Connection, courts: list[dict[str, str]]
) -> dict[str, str]:
    """Insert courts and return {court_code: court_id} mapping."""
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


def list_s3_keys(s3_client: Any, bucket: str, prefix: str = "ca/") -> list[str]:
    """List all content-addressed keys from S3 (or local cache)."""
    paginator = s3_client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if KEY_PATTERN.match(key):
                keys.append(key)
    return keys


def list_local_keys(cache_dir: Path, prefix: str = "ca/") -> list[str]:
    """List content-addressed keys from local cache directory."""
    keys: list[str] = []
    base = cache_dir / prefix.rstrip("/")
    if not base.exists():
        return keys
    for path in base.rglob("*"):
        if path.is_file():
            key = str(path.relative_to(cache_dir))
            if KEY_PATTERN.match(key):
                keys.append(key)
    return keys


def build_event(
    key: str,
    content: bytes,
    parsed: dict[str, str],
    bucket: str,
) -> dict[str, Any]:
    """Construct an ingestion event dict from an S3 object.

    For HTML documents, attempts to extract ``hearing_date`` from the text
    using the same regex patterns the ingestion worker uses.  This gives the
    worker a head-start so ruling rows can be created even when LLM extraction
    is unavailable.  For PDFs the regex rarely works on raw binary content, so
    the worker's LLM extraction path handles hearing_date for those formats.
    """
    content_hash = parsed["content_hash"]
    document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, content_hash))
    content_format = EXT_TO_FORMAT.get(parsed["ext"], "bin")

    event: dict[str, Any] = {
        "document_id": document_id,
        "state": unsluggify(parsed["state"]),
        "county": unsluggify(parsed["county"]),
        "court": unsluggify(parsed["court"]),
        "content_format": content_format,
        "content_hash": content_hash,
        "s3_key": key,
        "s3_bucket": bucket,
        "scraper_id": f"rebuild-{parsed['state']}-{parsed['county']}",
        "source_url": "",
        "capture_timestamp": datetime.now(UTC).isoformat(),
    }

    # For text-based formats (HTML, TXT), pass content as ruling_text.
    # For binary formats (PDF, DOCX), pass raw bytes as latin-1 string
    # (the worker handles extraction from binary formats).
    if content_format in ("html", "txt"):
        text = content.decode("utf-8", errors="replace")
        event["ruling_text"] = text

        # Try to extract hearing_date from HTML text using the ingestion
        # regex patterns.  This is cheap and reliable for HTML counties
        # (LA, CC, Fresno).  Only applied to HTML — the regex patterns are
        # tuned for HTML structure and may produce false positives on plain
        # text.  Lazy import to avoid top-level dependency on the ingestion
        # package (which is only available in the scraper-framework venv,
        # not in the main process for all callers).
        if content_format == "html":
            try:
                from ingestion.extract import extract_hearing_date

                hearing_dt = extract_hearing_date(text)
                if hearing_dt is not None:
                    event["hearing_date"] = str(hearing_dt)
            except ImportError:
                pass
    elif content_format in ("pdf", "docx"):
        event["ruling_text"] = content.decode("latin-1")

    return event


def _process_one_document(
    key: str,
    cache_dir: str,
    bucket: str,
    database_url: str,
    redis_url: str,
    os_url: str,
) -> dict[str, Any]:
    """Process a single document in a child process.

    Creates its own DB connection, Redis client, and IngestionWorker.
    Designed for ProcessPoolExecutor — each call is fully independent.

    Returns a dict with:
      - ``status``: ``"ok"``, ``"skip"``, or ``"error"``
      - ``content_format``: the document format (``"html"``, ``"pdf"``, etc.)
      - ``had_hearing_date``: whether ``hearing_date`` was present in the event
    """
    parsed = parse_s3_key(key)
    if not parsed:
        return {"status": "skip", "content_format": "", "had_hearing_date": False}

    content_format = EXT_TO_FORMAT.get(parsed["ext"], "bin")

    if cache_dir:
        content = (Path(cache_dir) / key).read_bytes()
    else:
        from framework.s3_cache import make_s3_client as _make_s3

        s3 = _make_s3()
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()

    if not content:
        return {
            "status": "skip",
            "content_format": content_format,
            "had_hearing_date": False,
        }

    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != parsed["content_hash"]:
        logger.error(
            "S3 content hash mismatch, skipping document",
            s3_key=key,
            key_hash=parsed["content_hash"],
            content_hash=actual_hash,
        )
        return {
            "status": "error",
            "content_format": content_format,
            "had_hearing_date": False,
        }

    event = build_event(key, content, parsed, bucket)
    had_hearing_date = bool(event.get("hearing_date"))

    # Lazy per-process worker — cached on the function object.
    worker = getattr(_process_one_document, "_worker", None)
    if worker is None:
        import redis as redis_lib
        from unittest.mock import MagicMock

        from framework.s3_cache import make_s3_client as _make_s3
        from ingestion.worker import IngestionWorker

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
        s3 = _make_s3()
        worker = IngestionWorker(
            redis_client=rc,
            pg_dsn=database_url,
            opensearch_client=os_client,
            s3_client=s3,
            archive_bucket=bucket,
        )
        _process_one_document._worker = worker

    try:
        worker.process_event(event)
        return {
            "status": "ok",
            "content_format": content_format,
            "had_hearing_date": had_hearing_date,
        }
    except Exception:
        logger.error(
            "Error processing document",
            key=key,
            document_id=event.get("document_id"),
            exc_info=True,
        )
        return {
            "status": "error",
            "content_format": content_format,
            "had_hearing_date": had_hearing_date,
        }


def reset_derived_tables(conn: psycopg.Connection) -> None:
    """Truncate all tables in the derived schema, preserving public/telemetry data."""
    logger.info("Resetting derived schema — truncating all derived tables...")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'derived' ORDER BY tablename"
        )
        tables = [row[0] for row in cur.fetchall()]
    if not tables:
        logger.warning("No tables found in derived schema")
        return
    qualified = ", ".join(f"derived.{t}" for t in tables)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {qualified} CASCADE")
    conn.commit()
    logger.info("Truncated derived tables", tables=tables)


def reset_opensearch_index(os_url: str) -> None:
    """Delete the OpenSearch tentative_rulings index so it's rebuilt from scratch."""
    from opensearchpy import OpenSearch

    os_kwargs: dict = {"hosts": [os_url]}
    os_user = os.environ.get("OPENSEARCH_USERNAME", "")
    os_pass = os.environ.get("OPENSEARCH_PASSWORD", "")
    if os_user and os_pass:
        os_kwargs["http_auth"] = (os_user, os_pass)
    client = OpenSearch(**os_kwargs)

    index_name = "tentative_rulings_v1"
    if client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
        logger.info("Deleted OpenSearch index", index=index_name)
    else:
        logger.info(
            "OpenSearch index does not exist, nothing to delete", index=index_name
        )


def _fetch_rosters(
    conn: psycopg.Connection,
    s3_client: Any,
    bucket: str,
) -> None:
    """Fetch court directory rosters and store snapshots in DB.

    Imports and runs each CourtDirectory subclass to populate
    court_directory_snapshots.  This ensures the ingestion worker's
    dept-to-judge fallback has roster data available during rebuilds.
    """
    import importlib

    DIRECTORIES = [
        ("courts.ca.oc_dept_judges", "OCCourtDirectory", "ca_orange"),
        ("courts.ca.la_dept_judges", "LACourtDirectory", "ca_los_angeles"),
        ("courts.ca.fresno_dept_judges", "FresnoCourtDirectory", "ca_fresno"),
        ("courts.ca.kern_dept_judges", "KernCourtDirectory", "ca_kern"),
        ("courts.ca.sd_dept_judges", "SanDiegoCourtDirectory", "ca_san_diego"),
        (
            "courts.ca.sb_dept_judges",
            "SanBernardinoCourtDirectory",
            "ca_san_bernardino",
        ),
        ("courts.ca.ventura_dept_judges", "VenturaCourtDirectory", "ca_ventura"),
        ("courts.ca.sf_dept_judges", "SFCourtDirectory", "ca_san_francisco"),
    ]

    fetched = 0
    for module_path, class_name, court_id in DIRECTORIES:
        try:
            module = importlib.import_module(module_path)
            directory_cls = getattr(module, class_name)
            directory = directory_cls(s3_client, bucket, conn)
            directory.fetch_and_snapshot(court_id)
            fetched += 1
            logger.info("Fetched roster", court_id=court_id)
            time.sleep(1)  # Polite delay between fetches
        except Exception:
            logger.warning(
                "Failed to fetch roster for %s — skipping",
                court_id,
                exc_info=True,
            )

    logger.info("Roster fetch complete", fetched=fetched, total=len(DIRECTORIES))


def _write_rebuild_marker(
    conn: psycopg.Connection,
    *,
    in_progress: bool,
) -> None:
    """Write a rebuild-in-progress marker to the data_quality_metrics table.

    The data quality check reads this marker to suppress P1 alerts during
    rebuilds.  See #2222.

    Args:
        conn: Database connection.
        in_progress: True when rebuild starts, False when it completes.
    """
    metric_value = 1.0 if in_progress else 0.0
    now = datetime.now(UTC)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_quality_metrics
                    (recorded_at, county, metric_name, metric_value, metadata)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (now, "_system", "rebuild_in_progress", metric_value, None),
            )
        conn.commit()
        status = "started" if in_progress else "completed"
        logger.info("Rebuild marker written: %s", status)
    except Exception:
        logger.warning(
            "Failed to write rebuild marker — data quality alerts may fire "
            "during rebuild",
            exc_info=True,
        )
        try:
            conn.rollback()
        except Exception:
            pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild DB from S3")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("REBUILD_CONCURRENCY", "64")),
        help="Number of parallel processes (default: 64, or REBUILD_CONCURRENCY env)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate all derived tables and delete OpenSearch index before rebuilding",
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Only process documents from this county (e.g. 'Ventura', 'Los Angeles')",
    )
    parser.add_argument(
        "--state",
        type=str,
        default="ca",
        help="State code for --county filtering (default: ca)",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    cache_dir = os.environ.get("S3_CACHE_DIR", "")

    s3 = make_s3_client()
    conn = psycopg.connect(database_url, autocommit=False)

    os_url = os.environ.get("OPENSEARCH_URL", "")

    # Step 0 (optional): Reset derived data for a clean rebuild
    if args.reset:
        reset_derived_tables(conn)
        if os_url:
            reset_opensearch_index(os_url)
        else:
            logger.info("OPENSEARCH_URL not set — skipping OpenSearch index reset")

        # Write rebuild-in-progress marker so the data quality check
        # downgrades P1 alerts during the rebuild window (#2222).
        _write_rebuild_marker(conn, in_progress=True)

    # Wrap the rebuild in try/finally so the completion marker is always
    # written when --reset was used, even if the rebuild fails partway (#2222).
    try:
        # Step 0b: Fetch court directory rosters so that dept-to-judge lookups
        # work during processing.  Without this, the universal dept-to-judge
        # fallback (#2269) in the ingestion worker has no snapshot data and
        # all counties drop to ~15-35% judge resolution.
        logger.info("Fetching court directory rosters...")
        try:
            _fetch_rosters(conn, s3, BUCKET)
        except Exception:
            logger.warning(
                "Roster fetch failed — continuing without roster data", exc_info=True
            )

        # Step 1: Discover keys (local cache or S3)
        # Build S3 prefix — default "ca/", narrowed by --county if given.
        s3_prefix = f"{sluggify(args.state)}/"
        if args.county:
            s3_prefix = f"{sluggify(args.state)}/{sluggify(args.county)}/"
        logger.info("Discovering S3 objects...", prefix=s3_prefix)
        if cache_dir:
            keys = list_local_keys(Path(cache_dir), prefix=s3_prefix)
            logger.info(
                "Found keys from local cache", count=len(keys), cache_dir=cache_dir
            )
        else:
            keys = list_s3_keys(s3, BUCKET, prefix=s3_prefix)
            logger.info("Found keys from S3", count=len(keys))

        if not keys:
            logger.error("No content-addressed keys found.")
            sys.exit(1)

        # Step 2: Seed courts from key prefixes
        courts = discover_courts(keys)
        logger.info(
            "Discovered courts",
            count=len(courts),
            courts=[c["court_code"] for c in courts],
        )
        court_ids = seed_courts(conn, courts)
        logger.info("Courts seeded", court_ids=court_ids)

        # Step 3: Process documents using child processes.
        # pdfplumber/pdfminer C extensions are not thread-safe — ProcessPoolExecutor
        # gives each worker its own address space. Each child process lazily creates
        # its own DB connection, Redis client, and IngestionWorker.
        if not os_url:
            logger.info("OPENSEARCH_URL not set — skipping search indexing")

        from concurrent.futures import ProcessPoolExecutor, as_completed

        concurrency = args.concurrency
        logger.info("Processing documents", concurrency=concurrency, total=len(keys))

        # Step 4: Process documents concurrently using processes (not threads).
        # pdfplumber/pdfminer use C extensions that segfault under heavy threading.
        # Each process gets its own address space — no shared-memory corruption.
        t_start = time.monotonic()
        processed = 0
        errors = 0
        skipped = 0
        # Track documents where hearing_date was NOT available in the event.
        # The worker may still extract it via LLM or regex, but when that also
        # fails, the ruling row is skipped.  This counter helps diagnose
        # missing-ruling issues for PDF counties.
        no_hearing_date = 0
        # Per-format counters for the summary.
        format_counts: dict[str, int] = {}

        with ProcessPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    _process_one_document,
                    key,
                    cache_dir,
                    BUCKET,
                    database_url,
                    redis_url,
                    os_url,
                ): key
                for key in keys
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    result = future.result()
                    status = (
                        result.get("status", "error")
                        if isinstance(result, dict)
                        else result
                    )
                    content_format = (
                        result.get("content_format", "")
                        if isinstance(result, dict)
                        else ""
                    )
                    had_hearing_date = (
                        result.get("had_hearing_date", False)
                        if isinstance(result, dict)
                        else False
                    )

                    if status == "ok":
                        processed += 1
                    elif status == "skip":
                        skipped += 1
                    else:
                        errors += 1

                    if content_format:
                        format_counts[content_format] = (
                            format_counts.get(content_format, 0) + 1
                        )
                    if status == "ok" and not had_hearing_date:
                        no_hearing_date += 1

                    total_done = processed + errors + skipped
                    if processed > 0 and processed % 50 == 0:
                        elapsed = time.monotonic() - t_start
                        elapsed_min = elapsed / 60
                        keys_per_min = total_done / elapsed * 60 if elapsed > 0 else 0
                        eta_min = (
                            (len(keys) - total_done) / (total_done / elapsed) / 60
                            if total_done > 0 and elapsed > 0
                            else 0
                        )
                        logger.info(
                            "Progress",
                            keys_done=total_done,
                            keys_total=len(keys),
                            pct=round(100 * total_done / len(keys), 1),
                            keys_per_min=round(keys_per_min, 1),
                            elapsed_min=round(elapsed_min, 1),
                            eta_min=round(eta_min, 1),
                            errors=errors,
                        )
                except Exception as exc:
                    errors += 1
                    logger.error("Failed to process", key=key, error=str(exc))

        logger.info(
            "Rebuild complete",
            processed=processed,
            errors=errors,
            skipped=skipped,
            total=len(keys),
            format_counts=format_counts,
        )

        if no_hearing_date > 0:
            logger.warning(
                "%d documents had no pre-extracted hearing_date — rulings may have "
                "been skipped if worker-side extraction (LLM/regex) also failed",
                no_hearing_date,
                no_hearing_date=no_hearing_date,
                processed=processed,
            )
    finally:
        # Clear the rebuild-in-progress marker so the data quality check
        # resumes normal P1 alerting.  Runs even if the rebuild fails
        # partway through (#2222).
        if args.reset:
            _write_rebuild_marker(conn, in_progress=False)

    conn.close()


if __name__ == "__main__":
    main()
