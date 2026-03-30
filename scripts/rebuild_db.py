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
        courts.append({
            "state": unsluggify(parsed["state"]),
            "county": unsluggify(parsed["county"]),
            "court_name": unsluggify(parsed["court"]),
            "court_code": court_code,
            "timezone": STATE_TIMEZONES.get(parsed["state"], "America/Los_Angeles"),
        })
    return courts


def seed_courts(conn: psycopg.Connection, courts: list[dict[str, str]]) -> dict[str, str]:
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
    """Construct an ingestion event dict from an S3 object."""
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

    # For HTML, pass content as ruling_text. For PDF, pass raw bytes
    # (the worker handles PDF text extraction).
    if content_format == "html":
        event["ruling_text"] = content.decode("utf-8", errors="replace")
    elif content_format == "pdf":
        event["ruling_text"] = content.decode("latin-1")

    return event


def _process_one_document(
    key: str,
    cache_dir: str,
    bucket: str,
    database_url: str,
    redis_url: str,
    os_url: str,
) -> str:
    """Process a single document in a child process.

    Creates its own DB connection, Redis client, and IngestionWorker.
    Designed for ProcessPoolExecutor — each call is fully independent.
    """
    parsed = parse_s3_key(key)
    if not parsed:
        return "skip"

    if cache_dir:
        content = (Path(cache_dir) / key).read_bytes()
    else:
        from framework.s3_cache import make_s3_client as _make_s3

        s3 = _make_s3()
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()

    if not content:
        return "skip"

    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != parsed["content_hash"]:
        parsed["content_hash"] = actual_hash

    event = build_event(key, content, parsed, bucket)

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
        return "ok"
    except Exception:
        return "error"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild DB from S3")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("REBUILD_CONCURRENCY", "64")),
        help="Number of parallel processes (default: 64, or REBUILD_CONCURRENCY env)",
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

    # Step 1: Discover keys (local cache or S3)
    logger.info("Discovering S3 objects...")
    if cache_dir:
        keys = list_local_keys(Path(cache_dir))
        logger.info("Found keys from local cache", count=len(keys), cache_dir=cache_dir)
    else:
        keys = list_s3_keys(s3, BUCKET)
        logger.info("Found keys from S3", count=len(keys))

    if not keys:
        logger.error("No content-addressed keys found.")
        sys.exit(1)

    # Step 2: Seed courts from key prefixes
    courts = discover_courts(keys)
    logger.info("Discovered courts", count=len(courts), courts=[c["court_code"] for c in courts])
    court_ids = seed_courts(conn, courts)
    logger.info("Courts seeded", court_ids=court_ids)

    # Step 3: Process documents using child processes.
    # pdfplumber/pdfminer C extensions are not thread-safe — ProcessPoolExecutor
    # gives each worker its own address space. Each child process lazily creates
    # its own DB connection, Redis client, and IngestionWorker.
    os_url = os.environ.get("OPENSEARCH_URL", "")
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
                if result == "ok":
                    processed += 1
                elif result == "skip":
                    skipped += 1
                else:
                    errors += 1
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
    )
    conn.close()


if __name__ == "__main__":
    main()
