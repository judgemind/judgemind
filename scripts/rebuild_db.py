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
#   --reset           Truncate derived tables before rebuilding.  When combined
#                     with --county, the reset is scoped to just that county's
#                     rows (per-county reset); otherwise it truncates the
#                     derived schema globally.
#   --force-split-child-loss
#                     Override the split-child preflight guard (see #2494,
#                     #2496) and proceed with --reset --county even when the
#                     rebuild cannot restore all deleted rows.  Expect data
#                     loss on multi-case-PDF counties.
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
        **Split-child hazard.** Counties whose scrapers split multi-case PDFs
        into multiple ``derived.documents`` rows per raw S3 object (currently
        Santa Clara, and any other county whose raws are multi-case PDFs —
        Orange, Riverside, Fresno) fan out N:1 at ingest time.  On rebuild,
        each raw PDF re-derives **only one** row, not N, because of the
        ``content_hash`` / ``key_hash`` mismatch in the rebuild pipeline
        (see #2494).  Running ``--reset --county`` on such a county will
        therefore delete all the split-children and the rebuild cannot
        restore them — the county ends up with only the raw-aligned subset.
        Callers must clear ``preflight_split_child_guard`` (or pass
        ``--force-split-child-loss`` on the CLI) before invoking this
        function on a multi-case-PDF county.

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


# Fanout ratio at or above which the split-child guard fires.  Chosen to
# leave headroom for normal drift (staging rows, accidental dupes, edits
# that add a handful of rows per raw) while still catching the N:1 pattern
# (Santa Clara pre-reset was ~20x).
_SPLIT_CHILD_FANOUT_RATIO = 1.5


class SplitChildDataLossError(RuntimeError):
    """Raised when ``--reset --county`` would delete more rows than rebuild
    can restore from S3 — the split-child data-loss hazard from #2494.

    See ``preflight_split_child_guard`` for details and the override
    mechanism (``--force-split-child-loss``).
    """


def preflight_split_child_guard(
    conn: psycopg.Connection,
    state: str,
    county: str,
    s3_key_count: int,
    force: bool,
) -> None:
    """Refuse per-county reset when rebuild cannot restore what reset deletes.

    Background (#2494, #2496): when the ingestion pipeline splits a single
    multi-case raw PDF into N ``derived.documents`` rows (one per extracted
    case), the rows share the raw's S3 key but carry different content
    hashes.  The rebuild path hashes the raw bytes and tries to match against
    the S3 key filename — that only reproduces **one** row per raw PDF, not
    N.  So a ``--reset --county`` on a multi-case-PDF county (e.g. Santa
    Clara, Orange, Riverside, Fresno) will delete all N split-children and
    rebuild will only recreate the 1 raw-aligned row.  Result: silent data
    loss equal to ``(N-1) * raw_count`` per county on every run.

    The guard compares the DB's ``derived.documents`` count for the county
    against the raw-object count in S3.  If the DB has meaningfully more
    rows than S3 (ratio >= ``_SPLIT_CHILD_FANOUT_RATIO``), the fanout
    pattern is present and the reset is refused unless ``force`` is True.

    Edge cases:
    * Both counts zero → no data to lose, allow.
    * DB count lower than S3 count → no fanout, allow (this is the initial-
      population path before the worker has caught up).
    * S3 count zero but DB has rows → worst case, reset would nuke everything
      with no rebuild capacity to restore; refuse unless forced.

    Args:
        conn: Open DB connection (used to resolve court ids and count rows).
        state: Two-letter state code (case-insensitive).
        county: County name (case-insensitive, unnormalised).
        s3_key_count: Number of content-addressed raw objects under
            ``{state}/{county}/`` in S3 (or the local cache) — caller is
            responsible for counting these.
        force: Caller opted into the data loss (``--force-split-child-loss``).

    Raises:
        ValueError: no courts match ``(state, county)`` (typo guard — mirrors
            ``reset_derived_tables_for_county``).
        SplitChildDataLossError: fanout detected and ``force`` is False.
    """
    court_ids = _resolve_county_court_ids(conn, state, county)
    if not court_ids:
        raise ValueError(
            f"No courts found in derived.courts for state={state!r}, county={county!r}. "
            "Refusing to run split-child preflight against a nonexistent county."
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.documents WHERE court_id = ANY(%s::uuid[])",
            (court_ids,),
        )
        db_doc_count = int(cur.fetchone()[0])

    # Ratio is undefined if s3_key_count == 0 but db_doc_count > 0.  That's
    # the worst case (reset would nuke everything, rebuild restores nothing)
    # so treat it as an unconditional fanout violation.
    if s3_key_count == 0 and db_doc_count == 0:
        ratio: float = 0.0
    elif s3_key_count == 0:
        ratio = float("inf")
    else:
        ratio = db_doc_count / s3_key_count

    logger.info(
        "Split-child preflight",
        state=state,
        county=county,
        s3_key_count=s3_key_count,
        db_doc_count=db_doc_count,
        fanout_ratio=round(ratio, 2) if ratio != float("inf") else "inf",
        threshold=_SPLIT_CHILD_FANOUT_RATIO,
        force_split_child_loss=force,
    )

    fanout_detected = ratio >= _SPLIT_CHILD_FANOUT_RATIO

    if not fanout_detected:
        logger.info(
            "Split-child preflight passed — DB row count within fanout tolerance",
            state=state,
            county=county,
            db_doc_count=db_doc_count,
            s3_key_count=s3_key_count,
        )
        return

    # Fanout detected.  Either refuse, or warn and proceed under --force.
    msg = (
        f"Split-child data-loss hazard detected for {state}/{county}: "
        f"derived.documents has {db_doc_count} rows but S3 has only "
        f"{s3_key_count} raw objects (fanout ratio "
        f"{'inf' if ratio == float('inf') else f'{ratio:.2f}x'} "
        f">= {_SPLIT_CHILD_FANOUT_RATIO}x threshold). "
        "Running --reset --county will delete the split-children and the "
        "rebuild cannot restore them (see #2494). Pass "
        "--force-split-child-loss to proceed anyway."
    )

    if not force:
        logger.error(
            "Split-child preflight FAILED — refusing to reset",
            state=state,
            county=county,
            db_doc_count=db_doc_count,
            s3_key_count=s3_key_count,
            fanout_ratio=round(ratio, 2) if ratio != float("inf") else "inf",
        )
        raise SplitChildDataLossError(msg)

    logger.warning(
        "Split-child preflight overridden by --force-split-child-loss — "
        "proceeding with reset. Expected data loss: approximately "
        "%d rows that rebuild cannot restore.",
        max(0, db_doc_count - s3_key_count),
        state=state,
        county=county,
        db_doc_count=db_doc_count,
        s3_key_count=s3_key_count,
    )


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
    parser.add_argument(
        "--force-split-child-loss",
        action="store_true",
        help=(
            "Override the split-child preflight guard on --reset --county. "
            "Proceed with reset even when the rebuild cannot restore all "
            "deleted rows (see #2494, #2496).  Expect data loss."
        ),
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
        if args.county:
            # Per-county reset: only delete rows belonging to this county.
            # Before deleting, run the split-child preflight so we don't
            # destroy rows that rebuild cannot restore (see #2494, #2496).
            # We need an S3 key count for the preflight — this is the same
            # list that step 1 builds later, but we need it up front here.
            preflight_prefix = f"{sluggify(args.state)}/{sluggify(args.county)}/"
            if cache_dir:
                preflight_s3_key_count = len(
                    list_local_keys(Path(cache_dir), prefix=preflight_prefix)
                )
            else:
                preflight_s3_key_count = len(
                    list_s3_keys(s3, BUCKET, prefix=preflight_prefix)
                )

            try:
                preflight_split_child_guard(
                    conn,
                    state=args.state,
                    county=args.county,
                    s3_key_count=preflight_s3_key_count,
                    force=args.force_split_child_loss,
                )
            except SplitChildDataLossError as exc:
                logger.error(
                    "Aborting per-county reset — pass --force-split-child-loss "
                    "to override: %s",
                    exc,
                )
                conn.close()
                sys.exit(2)

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
