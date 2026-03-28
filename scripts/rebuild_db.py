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


def main() -> None:
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

    # Step 3: Set up ingestion worker
    import redis as redis_lib
    from unittest.mock import MagicMock

    redis_client = redis_lib.Redis.from_url(redis_url, decode_responses=False)

    # OpenSearch is optional for rebuild — use a mock if not available.
    # Search indexing can be done as a separate pass later.
    os_url = os.environ.get("OPENSEARCH_URL", "")
    if os_url:
        from opensearchpy import OpenSearch
        opensearch_client = OpenSearch(hosts=[os_url])
    else:
        logger.info("OPENSEARCH_URL not set — skipping search indexing")
        opensearch_client = MagicMock()

    from ingestion.worker import IngestionWorker

    worker = IngestionWorker(
        redis_client=redis_client,
        pg_dsn=database_url,
        opensearch_client=opensearch_client,
        s3_client=s3,
        archive_bucket=BUCKET,
    )

    # Step 4: Process each document
    processed = 0
    errors = 0
    skipped = 0

    for i, key in enumerate(keys):
        parsed = parse_s3_key(key)
        if not parsed:
            skipped += 1
            continue

        try:
            if cache_dir:
                content = (Path(cache_dir) / key).read_bytes()
            else:
                response = s3.get_object(Bucket=BUCKET, Key=key)
                content = response["Body"].read()

            if not content:
                logger.warning("Skipping empty document", key=key)
                skipped += 1
                continue

            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != parsed["content_hash"]:
                logger.error(
                    "Content hash mismatch",
                    key=key,
                    expected=parsed["content_hash"][:12],
                    actual=actual_hash[:12],
                )
                errors += 1
                continue

            event = build_event(key, content, parsed, BUCKET)
            worker.process_event(event)
            processed += 1

            if processed % 100 == 0:
                logger.info("Progress", processed=processed, errors=errors, total=len(keys))

        except Exception as exc:
            errors += 1
            logger.error("Failed to process", key=key, error=str(exc), exc_info=True)

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
