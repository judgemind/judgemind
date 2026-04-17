#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
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
#   --max-worker-memory-mb N
#                     Reserve ~N MB of RAM per worker when auto-scaling
#                     concurrency.  Computes ``min(--concurrency,
#                     available_memory_mb / N)`` and uses the smaller value.
#                     Use this after bumping Fargate memory with
#                     ``scripts/ecs-run-task.sh --memory <mb>`` so concurrency
#                     adapts to the new allocation.  Set to 0 (default) to
#                     disable auto-scaling and use --concurrency as-is.
#                     See #2495.
#   --reset           Truncate derived tables before rebuilding.  When combined
#                     with --county, the reset is scoped to just that county's
#                     rows (per-county reset); otherwise it truncates the
#                     derived schema globally.
#   --force-split-child-loss
#                     Deprecated no-op kept for CLI/tooling compatibility.  As
#                     of #2494, the rebuild re-derives split-children from the
#                     raw PDF via the ingestion worker's LLM split path, so no
#                     override is required.  Passing this flag logs a warning
#                     and is otherwise ignored.
#
# Worker-crash resilience (see #2495):
#   * When a child process in the pool dies mid-task (segfault from
#     pdfplumber/pdfminer C extensions, OOM, etc.), every in-flight future
#     gets a ``BrokenProcessPool`` exception — masking *which* PDF actually
#     killed the worker.  The orchestrator now:
#       1. Logs the specific S3 key for every future that was in flight when
#          the pool broke (``in-flight at pool break`` log).
#       2. After the concurrent pass, retries each crashed key serially in
#          a fresh ``max_workers=1`` pool so segfaults only kill the retry
#          worker, not the whole rebuild.
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


def _derive_court_code(state: str, county: str) -> str:
    """Derive a URL-safe court code from state + county.

    Must match the canonical format in ``ingestion.db._derive_court_code``
    so that ``ON CONFLICT (court_code)`` upserts hit the same row the
    ingestion worker creates.  See #2373.

    Examples:
        "CA", "Los Angeles"  -> "ca-los-angeles"
        "CA", "Orange"       -> "ca-orange"
    """
    return f"{state.lower()}-{county.lower().replace(' ', '-')}"


def discover_courts(keys: list[str]) -> list[dict[str, str]]:
    """Derive unique courts from S3 key prefixes.

    Uses the same ``{state}-{county}`` court_code format as the ingestion
    worker (``ingestion.db._derive_court_code``) so that the ``ON CONFLICT
    (court_code)`` upsert in ``seed_courts`` merges with existing rows
    instead of creating duplicates.  See #2373.
    """
    seen: set[str] = set()
    courts: list[dict[str, str]] = []
    for key in keys:
        parsed = parse_s3_key(key)
        if not parsed:
            continue
        state = unsluggify(parsed["state"])
        county = unsluggify(parsed["county"])
        court_name = unsluggify(parsed["court"])
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
      - ``hash_mismatch``: whether the S3 key hash did not match the object
        bytes' SHA-256.  Non-fatal — rebuild proceeds using the key hash as
        the canonical ``content_hash`` so the worker's LLM split path can
        re-derive any split-children from the raw PDF (see #2494).
    """
    parsed = parse_s3_key(key)
    if not parsed:
        return {
            "status": "skip",
            "content_format": "",
            "had_hearing_date": False,
            "hash_mismatch": False,
        }

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
            "hash_mismatch": False,
        }

    # Byte-integrity check.  A mismatch used to short-circuit with
    # ``status="error"``, but that caused multi-case-PDF counties (Santa
    # Clara, Orange, Riverside, Fresno) to lose all their split-children on
    # rebuild — #2494.  Now we log a warning and let the worker process the
    # raw PDF; the LLM split path derives N split-children from the content
    # and stores them with hashes derived from the canonical key hash.
    actual_hash = hashlib.sha256(content).hexdigest()
    hash_mismatch = actual_hash != parsed["content_hash"]
    if hash_mismatch:
        logger.warning(
            "S3 content hash mismatch — proceeding with rebuild using key "
            "hash as canonical content_hash (see #2494)",
            s3_key=key,
            key_hash=parsed["content_hash"],
            actual_content_hash=actual_hash,
        )

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

            # 30s timeout + 3 retries: opensearchpy defaults to 10s and no
            # retries, which produces sporadic ``ConnectionTimeout`` entries
            # during rebuild under load (#2481).  ``IndexingConsumer`` also
            # swallows terminal timeouts as a warning so a missed index
            # never fails document processing.
            os_kwargs: dict = {
                "hosts": [os_url],
                "timeout": 30,
                "max_retries": 3,
                "retry_on_timeout": True,
            }
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
            "hash_mismatch": hash_mismatch,
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
            "hash_mismatch": hash_mismatch,
        }


def _available_memory_mb() -> int:
    """Return the memory available to this process in MB.

    Reads the Linux cgroup memory limit (Fargate sets this) when available,
    then falls back to ``psutil.virtual_memory()`` or the host total.  Returns
    0 when no reliable value can be resolved — the caller treats 0 as "skip
    autoscaling."  Never raises — failure returns 0.  See #2495.
    """
    # Prefer cgroup v2, then v1, then psutil.  Fargate containers expose
    # their memory limit via the memory cgroup — the host's psutil value
    # would overcount on a shared host.
    for cgroup_path in (
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    ):
        try:
            with open(cgroup_path) as f:
                raw = f.read().strip()
            if raw == "max":
                continue
            limit_bytes = int(raw)
            # Kernel sentinel for "no limit" — huge value ≈ 2^63.  Fall through.
            if limit_bytes <= 0 or limit_bytes > (1 << 60):
                continue
            return limit_bytes // (1024 * 1024)
        except (OSError, ValueError):
            continue

    try:
        import psutil

        return int(psutil.virtual_memory().total) // (1024 * 1024)
    except Exception:
        return 0


def _autoscale_concurrency(
    requested: int,
    max_worker_memory_mb: int,
    available_memory_mb: int | None = None,
) -> int:
    """Compute the effective worker count given a per-worker memory budget.

    When ``max_worker_memory_mb`` is 0 or negative, returns ``requested``
    unchanged (autoscaling disabled).  Otherwise returns
    ``min(requested, available_memory_mb // max_worker_memory_mb)`` with a
    floor of 1 so the rebuild always makes forward progress, even on tiny
    allocations.  Exposed as a separate function so tests can stub available
    memory without touching /sys or psutil.  See #2495.
    """
    if max_worker_memory_mb <= 0:
        return requested
    if available_memory_mb is None:
        available_memory_mb = _available_memory_mb()
    if available_memory_mb <= 0:
        # Could not resolve — fall back to requested, don't silently drop to 1.
        return requested
    capped = available_memory_mb // max_worker_memory_mb
    return max(1, min(requested, capped))


def _retry_crashed_keys_serially(
    crashed_keys: list[str],
    cache_dir: str,
    bucket: str,
    database_url: str,
    redis_url: str,
    os_url: str,
) -> dict[str, Any]:
    """Retry keys that crashed a worker in the concurrent pass.

    Launches a fresh ``ProcessPoolExecutor(max_workers=1)`` per key so a
    segfault or OOM only kills the single retry worker — the orchestrator
    survives and keeps going.  Returns an aggregate result dict with the
    same counters shape the main loop accumulates (``processed``, ``errors``,
    ``skipped``, ``no_hearing_date``, ``hash_mismatch_warnings``,
    ``format_counts``) plus a list of keys that crashed on the retry too.
    See #2495.
    """
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    processed = 0
    errors = 0
    skipped = 0
    no_hearing_date = 0
    hash_mismatch_warnings = 0
    format_counts: dict[str, int] = {}
    still_crashed: list[str] = []

    logger.info(
        "Retrying keys that crashed a worker in the concurrent pass "
        "(serial, max_workers=1 per key)",
        retry_count=len(crashed_keys),
    )

    for idx, key in enumerate(crashed_keys, 1):
        logger.info(
            "Retry in progress",
            retry_index=idx,
            retry_total=len(crashed_keys),
            s3_key=key,
        )
        with ProcessPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                _process_one_document,
                key,
                cache_dir,
                bucket,
                database_url,
                redis_url,
                os_url,
            )
            try:
                result = future.result()
            except BrokenProcessPool:
                # The PDF crashed even a dedicated single-worker subprocess —
                # it's almost certainly a C-extension segfault on this file.
                logger.error(
                    "Retry also crashed the worker — giving up on this key",
                    s3_key=key,
                )
                errors += 1
                still_crashed.append(key)
                continue
            except Exception as exc:
                logger.error(
                    "Retry raised unexpected exception",
                    s3_key=key,
                    error=str(exc),
                    exc_info=True,
                )
                errors += 1
                continue

        status = result.get("status", "error") if isinstance(result, dict) else result
        content_format = (
            result.get("content_format", "") if isinstance(result, dict) else ""
        )
        had_hearing_date = (
            result.get("had_hearing_date", False) if isinstance(result, dict) else False
        )
        hash_mismatch = (
            result.get("hash_mismatch", False) if isinstance(result, dict) else False
        )

        if status == "ok":
            processed += 1
            logger.info("Retry succeeded", s3_key=key)
        elif status == "skip":
            skipped += 1
        else:
            errors += 1
            logger.warning("Retry produced error status", s3_key=key)

        if content_format:
            format_counts[content_format] = format_counts.get(content_format, 0) + 1
        if status == "ok" and not had_hearing_date:
            no_hearing_date += 1
        if hash_mismatch:
            hash_mismatch_warnings += 1

    return {
        "processed": processed,
        "errors": errors,
        "skipped": skipped,
        "no_hearing_date": no_hearing_date,
        "hash_mismatch_warnings": hash_mismatch_warnings,
        "format_counts": format_counts,
        "still_crashed": still_crashed,
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


# Derived tables keyed directly by ``court_id`` (UUID → derived.courts.id).
# These are deleted scope-down via ``WHERE court_id IN (...)``.
#
# ``derived.courts`` itself is intentionally absent — we leave the court rows
# intact so the rebuild's ``ON CONFLICT (court_code)`` upsert re-uses the same
# UUIDs.  Dropping them would re-issue UUIDs and orphan rows in
# ``telemetry.scraper_runs`` which FKs to ``derived.courts(id)``.  See #2465.
_PER_COUNTY_COURT_ID_TABLES: tuple[str, ...] = (
    "documents",
    "rulings",
    "cases",
    "judges",
)

# Join tables that have no ``court_id`` column — scoped via ``case_id``.
# Deleting the cases themselves cascades into these via ON DELETE CASCADE,
# but we delete them first so the ``cases`` DELETE can report its own count
# without surprising cascades.  (Explicit > implicit in a one-shot tool.)
_PER_COUNTY_CASE_JOIN_TABLES: tuple[str, ...] = (
    "case_judges",
    "case_parties",
    "case_attorneys",
)


def _resolve_county_court_ids(
    conn: psycopg.Connection,
    state: str,
    county: str,
) -> list[str]:
    """Look up ``derived.courts.id`` values for a given state + county.

    A single county may have more than one court row (e.g. a superior court
    plus an appellate court) and the per-county reset must delete rows for
    all of them.  Match is case-insensitive on the stored ``state`` / ``county``
    columns so "Santa Clara" and "santa clara" both resolve.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
              FROM derived.courts
             WHERE LOWER(state) = LOWER(%s)
               AND LOWER(county) = LOWER(%s)
            """,
            (state, county),
        )
        return [str(row[0]) for row in cur.fetchall()]


def reset_derived_tables_for_county(
    conn: psycopg.Connection,
    state: str,
    county: str,
) -> dict[str, int]:
    """Delete per-county rows from derived tables without touching other counties.

    Scoped reset used when ``--reset`` is combined with ``--county <name>``.
    See #2465.

    Behaviour:
    * Resolves all ``derived.courts.id`` values for ``(state, county)``.
    * Logs a pre-delete row count for every affected table so operators can
      eyeball the scope before DELETEs fire.
    * Deletes in one transaction.  Join tables (``case_judges``,
      ``case_parties``, ``case_attorneys``) are deleted first via
      ``case_id IN (SELECT id FROM cases WHERE court_id IN (...))`` so their
      parent ``cases`` rows can be removed without relying on cascade.
    * Then deletes rows keyed directly by ``court_id`` from ``documents``,
      ``rulings``, ``cases``, and ``judges``.
    * Does **not** touch ``derived.courts`` — the rebuild reseeds on
      ``ON CONFLICT (court_code)`` and preserving these rows keeps UUIDs
      stable for cross-schema references like ``telemetry.scraper_runs``.
    * Does **not** touch county-agnostic entity tables (``attorneys``,
      ``parties``, ``judge_aliases``, ``attorney_aliases``, ``party_aliases``).
      These are reseeded by the ingestion pipeline during rebuild.
    * Does **not** touch OpenSearch — the global index self-heals as new
      rulings are indexed.

    .. note::
        **Split-children are re-derived on rebuild (as of #2494).** Counties
        whose scrapers split multi-case PDFs into multiple ``derived.documents``
        rows per raw S3 object (Santa Clara, Orange, Riverside, Fresno) fan
        out N:1 at ingest time.  Rebuild re-runs the ingestion worker's LLM
        split path for each raw PDF, so the deleted rows are reconstructed
        from the raw content.  No preflight guard is required, and
        ``--force-split-child-loss`` is a deprecated no-op kept only for CLI
        compatibility.

    Raises:
        ValueError: if no courts match ``(state, county)``.  Silently
            matching zero rows would let a typo nuke nothing and rebuild
            nothing, which is worse than a hard error.

    Returns:
        ``{table_name: rows_deleted}`` for logging / test assertions.
    """
    court_ids = _resolve_county_court_ids(conn, state, county)
    if not court_ids:
        raise ValueError(
            f"No courts found in derived.courts for state={state!r}, county={county!r}. "
            "Refusing to reset to avoid silently deleting nothing."
        )

    logger.info(
        "Per-county reset — resolved courts",
        state=state,
        county=county,
        court_ids=court_ids,
    )

    deleted: dict[str, int] = {}
    # Wrap the reset in a single transaction: psycopg with ``autocommit=False``
    # treats the whole block as one transaction and commits at the end.  A
    # mid-DELETE failure rolls everything back so we can't land in a
    # half-reset state.
    with conn.cursor() as cur:
        # Pre-delete row counts (what we're about to remove).
        for table in _PER_COUNTY_CASE_JOIN_TABLES:
            cur.execute(
                f"""
                SELECT COUNT(*)
                  FROM derived.{table} jt
                  JOIN derived.cases c ON jt.case_id = c.id
                 WHERE c.court_id = ANY(%s::uuid[])
                """,
                (court_ids,),
            )
            count = cur.fetchone()[0]
            deleted[table] = count
            logger.info(
                "Pre-delete row count",
                table=f"derived.{table}",
                rows=count,
                court_ids=court_ids,
            )

        for table in _PER_COUNTY_COURT_ID_TABLES:
            cur.execute(
                f"SELECT COUNT(*) FROM derived.{table} WHERE court_id = ANY(%s::uuid[])",
                (court_ids,),
            )
            count = cur.fetchone()[0]
            deleted[table] = count
            logger.info(
                "Pre-delete row count",
                table=f"derived.{table}",
                rows=count,
                court_ids=court_ids,
            )

        # Actual deletes.  Order: join tables first (case-scoped), then
        # court-scoped tables from most dependent to least dependent.
        # ``rulings`` references ``documents`` AND ``cases`` AND ``judges``,
        # so it must be deleted before any of those.
        for table in _PER_COUNTY_CASE_JOIN_TABLES:
            logger.info(
                "Deleting rows",
                table=f"derived.{table}",
                rows=deleted[table],
                court_ids=court_ids,
            )
            cur.execute(
                f"""
                DELETE FROM derived.{table}
                 WHERE case_id IN (
                    SELECT id FROM derived.cases WHERE court_id = ANY(%s::uuid[])
                 )
                """,
                (court_ids,),
            )

        # Order among the court-id-keyed tables:
        #   rulings  → references documents, cases, judges
        #   documents → references cases
        #   cases    → referenced by rulings, documents, join tables
        #   judges   → referenced by rulings (nullable FK) and case_judges
        # We delete rulings first, then documents, then cases, then judges.
        for table in ("rulings", "documents", "cases", "judges"):
            logger.info(
                "Deleting rows",
                table=f"derived.{table}",
                rows=deleted[table],
                court_ids=court_ids,
            )
            cur.execute(
                f"DELETE FROM derived.{table} WHERE court_id = ANY(%s::uuid[])",
                (court_ids,),
            )

    conn.commit()
    logger.info(
        "Per-county reset complete",
        state=state,
        county=county,
        court_ids=court_ids,
        deleted=deleted,
    )
    return deleted


def reset_opensearch_index(os_url: str) -> None:
    """Delete the OpenSearch tentative_rulings index so it's rebuilt from scratch."""
    from opensearchpy import OpenSearch

    # 30s timeout + 3 retries — same rationale as the per-worker client
    # constructed in ``_process_one_document``.  See #2481.
    os_kwargs: dict = {
        "hosts": [os_url],
        "timeout": 30,
        "max_retries": 3,
        "retry_on_timeout": True,
    }
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
        "--max-worker-memory-mb",
        type=int,
        default=int(os.environ.get("REBUILD_MAX_WORKER_MEMORY_MB", "0")),
        help=(
            "Reserve ~N MB of RAM per worker when auto-scaling concurrency "
            "(default: 0, disabled).  Computes min(--concurrency, "
            "available_memory_mb / N) and uses the smaller value so "
            "concurrency adapts to the Fargate allocation.  See #2495."
        ),
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
    parser.add_argument(
        "--force-split-child-loss",
        action="store_true",
        help=(
            "Deprecated no-op kept for CLI/tooling compatibility.  As of "
            "#2494, rebuild re-derives split-children from the raw PDF via "
            "the worker's LLM split path, so no override is required.  "
            "Passing this flag logs a deprecation warning and is otherwise "
            "ignored."
        ),
    )
    args = parser.parse_args()

    if args.force_split_child_loss:
        logger.warning(
            "--force-split-child-loss is deprecated and has no effect: "
            "split-children are re-derived from the raw PDF on rebuild "
            "(see #2494).  You can stop passing this flag."
        )

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    cache_dir = os.environ.get("S3_CACHE_DIR", "")

    s3 = make_s3_client()
    conn = psycopg.connect(database_url, autocommit=False)

    os_url = os.environ.get("OPENSEARCH_URL", "")

    # Step 0 (optional): Reset derived data for a clean rebuild.  As of
    # #2494 the split-child preflight guard is gone: rebuild re-derives
    # split-children from the raw PDF via the ingestion worker's LLM split
    # path, so ``--reset --county`` on multi-case-PDF counties (Santa Clara,
    # Orange, Riverside, Fresno) is safe.
    if args.reset:
        if args.county:
            # Skip the OpenSearch index reset — the global index self-heals
            # as new rulings are indexed during the rebuild.  See #2465.
            reset_derived_tables_for_county(conn, args.state, args.county)
            logger.info(
                "Per-county reset — skipping OpenSearch index reset "
                "(global index self-heals on re-index)"
            )
        else:
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
        from concurrent.futures.process import BrokenProcessPool

        concurrency_requested = args.concurrency
        concurrency = _autoscale_concurrency(
            concurrency_requested, args.max_worker_memory_mb
        )
        if concurrency != concurrency_requested:
            logger.info(
                "Autoscaled concurrency to fit memory budget",
                requested=concurrency_requested,
                effective=concurrency,
                max_worker_memory_mb=args.max_worker_memory_mb,
                available_memory_mb=_available_memory_mb(),
            )
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
        # Track raw S3 objects whose bytes did not hash to the hash in their
        # S3 key (see #2494).  Non-fatal — rebuild proceeds — but a spike
        # indicates an S3 integrity or upload-path issue worth investigating.
        hash_mismatch_warnings = 0
        # Per-format counters for the summary.
        format_counts: dict[str, int] = {}

        # Track keys that were in flight when a worker crashed the pool so we
        # can (a) log exactly which raws were affected and (b) retry them
        # serially after the concurrent pass.  See #2495.
        crashed_keys: list[str] = []
        pool_break_events = 0

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
                    hash_mismatch = (
                        result.get("hash_mismatch", False)
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
                    if hash_mismatch:
                        hash_mismatch_warnings += 1

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
                except BrokenProcessPool as exc:
                    # A worker subprocess died mid-task (C-extension segfault,
                    # OOM kill, etc.) and the pool propagates that to every
                    # future that was running or pending.  We can't tell from
                    # the exception alone which PDF triggered the crash, but
                    # we *can* enumerate every key whose future never
                    # completed — one of them is the culprit, all of them
                    # deserve a retry.  See #2495.
                    pool_break_events += 1
                    errors += 1
                    crashed_keys.append(key)
                    logger.error(
                        "in-flight at pool break",
                        s3_key=key,
                        error=str(exc),
                    )
                except Exception as exc:
                    errors += 1
                    logger.error("Failed to process", key=key, error=str(exc))

        # Step 4b: Retry the keys that were in flight when the pool broke.
        # Each retry runs in its own ``max_workers=1`` subprocess so a
        # segfault only kills the single retry worker.  See #2495.
        retry_summary: dict[str, Any] | None = None
        if crashed_keys:
            logger.warning(
                "Pool break detected during concurrent pass — %d key(s) "
                "affected across %d distinct crash event(s).  Retrying "
                "serially.",
                len(crashed_keys),
                pool_break_events,
                crashed_keys_count=len(crashed_keys),
                pool_break_events=pool_break_events,
            )
            retry_summary = _retry_crashed_keys_serially(
                crashed_keys,
                cache_dir,
                BUCKET,
                database_url,
                redis_url,
                os_url,
            )
            # Fold retry counters back into the overall totals.  We already
            # counted ``len(crashed_keys)`` errors during the concurrent pass
            # (one per future that got ``BrokenProcessPool``).  Each retry
            # resolves one of those: success → convert error into processed,
            # skip → convert error into skipped, error → error stays.  Net
            # effect: subtract the whole ``len(crashed_keys)`` from errors
            # and re-add only the retry-level errors.
            errors = max(0, errors - len(crashed_keys)) + retry_summary["errors"]
            processed += retry_summary["processed"]
            skipped += retry_summary["skipped"]
            no_hearing_date += retry_summary["no_hearing_date"]
            hash_mismatch_warnings += retry_summary["hash_mismatch_warnings"]
            for fmt, count in retry_summary["format_counts"].items():
                format_counts[fmt] = format_counts.get(fmt, 0) + count
            logger.info(
                "Retry pass complete",
                retry_count=len(crashed_keys),
                retry_processed=retry_summary["processed"],
                retry_skipped=retry_summary["skipped"],
                retry_errors=retry_summary["errors"],
                still_crashed=retry_summary["still_crashed"],
            )

        logger.info(
            "Rebuild complete",
            processed=processed,
            errors=errors,
            skipped=skipped,
            total=len(keys),
            format_counts=format_counts,
            hash_mismatch_warnings=hash_mismatch_warnings,
            pool_break_events=pool_break_events,
            pool_break_keys_recovered=(
                (retry_summary["processed"] + retry_summary["skipped"])
                if retry_summary is not None
                else 0
            ),
            pool_break_keys_unrecovered=(
                len(retry_summary["still_crashed"]) if retry_summary is not None else 0
            ),
        )

        if no_hearing_date > 0:
            logger.warning(
                "%d documents had no pre-extracted hearing_date — rulings may have "
                "been skipped if worker-side extraction (LLM/regex) also failed",
                no_hearing_date,
                no_hearing_date=no_hearing_date,
                processed=processed,
            )

        if hash_mismatch_warnings > 0:
            logger.warning(
                "%d raw S3 objects had content-hash mismatches — rebuild "
                "proceeded using the S3 key hash as canonical content_hash "
                "(see #2494).  Investigate the S3 upload path if this count "
                "is non-trivial.",
                hash_mismatch_warnings,
                hash_mismatch_warnings=hash_mismatch_warnings,
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
