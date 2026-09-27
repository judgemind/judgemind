#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Re-ingest archived documents from S3 through the ingestion worker.

There is one write path (#4845).  Every S3 object this script re-ingests is
turned into a split-set-replacement event and run through
``IngestionWorker.process_event``: the same code the live worker and
``rebuild_db.py`` use to transcribe, split, enrich, validate, write
``derived.*`` and index search.  This script only chooses *which* objects to
re-ingest.  It has no split, dedup, supersede, relink, validation or search
logic of its own.  (Before #4845, DB-row mode re-implemented all of it, and
every worker fix had to be ported by hand; each missed port was a bug:
#4801, #4807, #4813, #4838, #4839.)

Two ways to choose the objects:

- **DB-row mode** (default): select the S3 keys of existing ``documents``
  rows that match the filters (``--county``, ``--date-from`` /
  ``--date-to``, ``--case-number-like``, ``--case-title-regex``,
  ``--null-motion-type``, ``--filter-null-outcome``, ``--orphaned-only``,
  ``--department-in``, ``--s3-key-list``).  A key whose split children
  match is re-ingested once, as a whole object.  Keys with no rows in the
  database are not found; use ``--prefix`` or ``rebuild_db.py`` for those.
- **Prefix mode** (``--prefix``): list the S3 objects under a key prefix,
  optionally narrowed by ``--s3-key-list``.

Each object's event carries ``_replace_split_set``, so the worker re-derives
the object's split set and replaces the previous one (#4700): rows the new
split no longer produces are removed (alerts detached, never deleted) and a
reused split slot takes the re-derived case.  A splitter or prompt change
therefore needs only a reingest of the affected keys (with
``--bust-llm-cache`` for a prompt change; see ``docs/agent/llm-cache.md``),
not ``rebuild_db.py --reset``.  Old ``cases`` rows that lose their last
ruling are left in place.

Search is indexed by the worker from the committed rows, in both modes.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- packages/scraper-framework/.venv/bin/python3 scripts/reingest_from_s3.py \
            --county "Los Angeles" --date-from 2026-01-01

    On dev, run it through ECS: ``scripts/ecs-run-task.sh
    scripts/reingest_from_s3.py -- --county Orange``.

Options:
    --county NAME       DB-row mode: keys of documents in this county.
    --date-from DATE    DB-row mode: documents captured on or after DATE.
    --date-to DATE      DB-row mode: documents captured on or before DATE.
    --case-number-like PATTERN
                        DB-row mode: documents whose case_number matches this
                        PostgreSQL LIKE pattern, e.g. 'UNKNOWN-%%'.
    --case-title-regex PATTERN
                        DB-row mode: documents whose case_title matches this
                        PostgreSQL regex.
    --null-motion-type  DB-row mode: documents with a ruling whose
                        motion_type is NULL.
    --filter-null-outcome
                        DB-row mode: documents with a ruling whose outcome
                        is NULL.
    --orphaned-only     DB-row mode: documents with no ruling.
    --department-in D [D ...]
                        DB-row mode: documents with a ruling in one of these
                        departments (exact match).
    --prefix PREFIX     Prefix mode: re-ingest the S3 objects under PREFIX
                        (e.g. ``orange/``), whether or not the database has
                        rows for them.
    --s3-key-list PATH  File listing S3 keys (one per line, blank lines and
                        ``#`` comments ignored); a container-local path or an
                        ``s3://bucket/key`` URI (#4606).  DB-row mode: only
                        these keys.  Prefix mode: the listed keys under the
                        prefix (#3855).
    --limit N           Re-ingest at most N keys.
    --dry-run           Log the keys that would be re-ingested; write nothing.
    --concurrency N     Parallel worker processes (default: 10).
    --parse-timeout N   Per-document PDF text-extraction timeout for the judge
                        pre-pass, in seconds (default: 60).
    --bust-llm-cache    Skip LLM cache reads for this run; writes still happen
                        (#2424).
    --skip-judge-prepass
                        Skip the pre-pass that seeds full-name judges before
                        the per-key pool runs (#4408, #4419).  Emergency
                        rollback only.
    --max-error-ratio R Exit non-zero when the fraction of keys that failed
                        exceeds R (#4619).  A ``--bust-llm-cache`` prefix run
                        defaults to 0.10 (#4624).
    --no-fail-on-errors Never exit non-zero on partial failure (#4624).
    --write-failed-manifest s3://bucket/key
                        When keys fail, write them (one per line) to this S3
                        object, for a retry with ``--s3-key-list`` (#4619).

Removed with the DB-row write path (#4845): ``--full-reparse`` and
``--multimodal`` (the worker always re-derives the split, with the county's
configured extraction), ``--no-llm``, ``--force-llm``, ``--llm-timeout``,
``--force-retranscribe`` (the worker re-transcribes from the raw object),
``--batch-size``, ``--parse-workers``, ``--checkpoint-file`` / ``--resume``
(re-running is idempotent; scope a retry with ``--write-failed-manifest``)
and ``--report-metrics``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, datetime
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
from framework.storage import (  # noqa: E402
    capture_provenance_from_s3_object,
    capture_timestamp_from_s3_object,
)
from ingestion.db import _looks_like_valid_judge_name, resolve_judge  # noqa: E402
from ingestion.extract import extract_judge_name  # noqa: E402
from ingestion.llm_extract import extract_text_from_pdf  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()


def _build_filters(
    county: str | None,
    date_from: date | None,
    date_to: date | None,
    case_title_regex: str | None = None,
    null_motion_type: bool = False,
    orphaned_only: bool = False,
    case_number_like: str | None = None,
    filter_null_outcome: bool = False,
    department_in: list[str] | None = None,
    s3_key_list: list[str] | None = None,
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
    if filter_null_outcome:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM rulings r"
            " WHERE r.document_id = d.id AND r.outcome IS NULL)"
        )
    if orphaned_only:
        clauses.append(
            "AND NOT EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = d.id)"
        )
    if department_in:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM rulings r"
            " WHERE r.document_id = d.id AND r.department = ANY(%s))"
        )
        params.append(department_in)
    if s3_key_list:
        # Surgical scoping: limit reingest to a hand-picked set of S3 keys.
        # Useful for targeted backfills (e.g. #3659 — re-resolve a small set
        # of pre-#3655 cached SC PDFs) where county-wide rescope would
        # over-shoot 10-100x and broader date filters lack sub-day granularity.
        clauses.append("AND d.s3_key = ANY(%s)")
        params.append(list(s3_key_list))
    return " ".join(clauses), params


def _parse_s3_key_list_text(raw: str, source: str) -> list[str]:
    """Parse keylist text into a de-duplicated, order-preserving list of keys.

    Blank lines and lines starting with ``#`` are skipped, leading/trailing
    whitespace on each entry is stripped, and duplicates are de-duplicated
    while preserving first-occurrence order. Shared by the local-file and
    ``s3://`` branches of :func:`_read_s3_key_list_file`.

    Parameters
    ----------
    raw:
        The full keylist text (decoded UTF-8).
    source:
        Human-readable origin (local path or s3:// URI) used in the
        empty-list error message.

    Raises
    ------
    ValueError
        If *raw* produces an empty key list (every line was blank or a
        comment) — fail loud rather than silently match every document.
    """
    seen: set[str] = set()
    keys: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        keys.append(stripped)
    if not keys:
        msg = f"--s3-key-list source is empty after stripping blanks/comments: {source}"
        raise ValueError(msg)
    return keys


def _read_s3_key_list_file(path: str) -> list[str]:
    """Read a list of S3 keys, one per line, from a local file or S3 object.

    *path* may be either a container-local filesystem path or an
    ``s3://bucket/key`` URI. The latter form lets the surgical reingest path
    run from ECS, where companion files cannot be staged into the task
    (see #4606): upload the keylist with ``aws s3 cp`` and pass the URI.

    Blank lines and lines starting with ``#`` are skipped, leading/trailing
    whitespace on each entry is stripped, and duplicates are de-duplicated
    while preserving first-occurrence order.

    Raises
    ------
    FileNotFoundError
        If *path* is a local path that does not exist.
    ValueError
        If *path* is a malformed ``s3://`` URI with no key component, or if
        the source produces an empty key list (every line was blank or a
        comment) — fail loud rather than silently match every document.
    """
    if path.startswith("s3://"):
        without_scheme = path[len("s3://") :]
        bucket, _, key = without_scheme.partition("/")
        if not bucket or not key:
            msg = (
                "--s3-key-list s3:// URI must include both a bucket and a key "
                f"(e.g. s3://my-bucket/path/keys.txt): {path}"
            )
            raise ValueError(msg)
        s3_client = boto3.client("s3")
        response = s3_client.get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read().decode("utf-8")
        return _parse_s3_key_list_text(raw, path)
    raw = Path(path).read_text(encoding="utf-8")
    return _parse_s3_key_list_text(raw, path)


def _fetch_s3_content(s3_client: object, bucket: str, key: str) -> bytes:
    """Fetch raw content from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)  # type: ignore[union-attr]
    return response["Body"].read()  # type: ignore[index]


def _extract_pdf_text_subprocess(
    raw_content: bytes,
    timeout: float = 30.0,
) -> str | None:
    """Extract text from PDF using pdfplumber in a subprocess with hard timeout.

    Runs pdfplumber in a separate process so that if the C PDF parser hangs,
    the OS can kill it.  ``PyThreadState_SetAsyncExc`` does not work for C
    extensions — this subprocess approach is the only reliable timeout.

    Returns the extracted text, or ``None`` if extraction failed or timed out.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        os.write(tmp_fd, raw_content)
        os.close(tmp_fd)

        # Find the Python interpreter from the current environment.
        python = sys.executable

        result = subprocess.run(
            [
                python,
                "-c",
                (
                    "import pdfplumber,sys\n"
                    "pdf=pdfplumber.open(sys.argv[1])\n"
                    "for p in pdf.pages:\n"
                    "    t=p.extract_text()\n"
                    "    if t:\n"
                    "        print(t)\n"
                ),
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return None
    except subprocess.TimeoutExpired:
        logger.debug("PDF subprocess timed out", timeout_seconds=timeout)
        return None
    except Exception:
        logger.debug("PDF subprocess extraction failed", exc_info=True)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _extract_text_from_content(
    raw_content: bytes,
    doc_format: str,
    pdf_timeout: float = 30.0,
) -> str:
    """Extract readable text from raw document content.

    For PDF documents, uses pdfplumber in a **subprocess** with a hard
    timeout to prevent hangs from C extensions.  The previous in-process
    approach using ``PyThreadState_SetAsyncExc`` could not interrupt
    pdfplumber's C PDF parser, leading to hung threads and blocked batches.

    When pdfplumber returns no text (image-only PDFs), falls back to OCR
    via the framework's ``extract_text_from_pdf()`` which uses LLM vision.

    For other formats (HTML, plain text), decodes as UTF-8.
    """
    if doc_format == "pdf":
        text = _extract_pdf_text_subprocess(raw_content, timeout=pdf_timeout)
        if text and text.strip():
            return text
        # Subprocess returned no text — likely an image-only PDF.
        # Try OCR fallback via the framework's extract_text_from_pdf(),
        # which renders pages and uses LLM vision for OCR (#1334).
        # Pass the original raw bytes to avoid lossy UTF-8 round-trip.
        logger.debug("PDF subprocess extraction returned no text, trying OCR fallback")
        ocr_text = extract_text_from_pdf(raw_content)
        if ocr_text and ocr_text.strip():
            return ocr_text
        # OCR also failed — fall back to UTF-8 decode as last resort.
        logger.debug("OCR fallback returned no text, falling back to UTF-8")
    return raw_content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Prefix mode — scan S3 directly for documents not in the DB
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
    capture_timestamp: datetime | None = None,
    provenance: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Construct an ingestion event dict from an S3 object for prefix mode.

    ``capture_timestamp`` is the object's original capture time (from
    :func:`framework.storage.capture_timestamp_from_s3_object`), or ``None``
    when unknown — never ``now()``, which made the deterministic
    ``hearing_date_in_range`` rule reject every ruling heard >180 days
    before the reingest (#4661).

    ``provenance`` is :func:`framework.storage.capture_provenance_from_s3_object`
    of the object: the captured ``source_url`` and the live
    ``capture_scraper_id``, which the worker's ``hearing_date_for_raw``
    hooks need to reproduce the live scraper's hearing date (#4774).
    """
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
        "capture_timestamp": capture_timestamp.isoformat()
        if capture_timestamp
        else None,
        # Prefix reingest re-derives the split set, so reused split-child
        # ids may take the re-derived (real) case link (#4700, #4788).
        # ``ingestion.worker.REPLACE_SPLIT_SET_KEY``.
        "_replace_split_set": True,
    }
    event.update(provenance or {})

    # For text-based formats (HTML, TXT), pass content as ruling_text.
    # For binary formats (PDF, DOCX), pass raw bytes as latin-1 string
    # (the worker handles extraction from binary formats).
    if content_format in ("html", "txt"):
        event["ruling_text"] = content.decode("utf-8", errors="replace")
    elif content_format in ("pdf", "docx"):
        event["ruling_text"] = content.decode("latin-1")

    return event


#: Keys the judge pre-pass fetches and scans at a time.
_PREPASS_CHUNK = 200


def _seed_judges_from_keys(
    conn: psycopg.Connection,
    s3_client: object,
    keys: list[str],
    bucket: str,
    court_ids: dict[str, str],
    *,
    parse_timeout: float,
    concurrency: int,
) -> dict[str, int]:
    """Walk S3 keys once and seed full-name judges into ``derived.judges``.

    The #4408 judge pre-pass, run by both modes before the per-key pool
    (extended from DB-row mode to prefix mode in #4419).

    Why the pre-pass is needed
    --------------------------
    Each S3 key is submitted to a ``ProcessPoolExecutor`` and
    consumes results via :func:`concurrent.futures.as_completed`.  The
    completion order is non-deterministic and parallel.  When LA per-case
    docs carrying only a surname (``JUDGE/DEPT: <Surname>/<dept>``) finish
    their DB write *before* the boilerplate doc carrying the full
    ``JUDGE <FIRST> <LAST>`` string has had time to upsert into
    ``derived.judges``, the per-case docs would commit ``judge_id = NULL``
    until a second reingest pass fills the gap.  This is the same
    chronological-resolver-race class surfaced by #4397.

    The pre-pass walks every key in ``keys`` once before the main
    :class:`ProcessPoolExecutor` runs.  For every key with valid raw S3
    content, it runs ``extract_judge_name`` against the document text and,
    when a *full* name (≥2 words, passing ``_looks_like_valid_judge_name``)
    is found, calls ``resolve_judge`` to upsert the judge into
    ``derived.judges``.  The per-document write loop's
    ``_expand_single_word_judge_surname`` Step 4 lookup
    (``packages/scraper-framework/src/ingestion/db.py``) finds the seeded
    judge via the suffix-LIKE match regardless of which doc the worker pool
    completes first, so single-word surnames resolve on the first prefix-mode
    invocation.

    Operational notes:
      * Skipped under ``dry_run=True`` (caller's responsibility — the helper
        always commits when it seeds).
      * Skipped under ``--skip-judge-prepass`` (caller's responsibility).
      * Operates on RAW S3 content + ``_extract_text_from_content`` regex,
        so it works for HTML, PDF, and other formats equally —
        ``extract_judge_name`` is regex-only and incurs no LLM cost.
      * S3 content is re-fetched by :func:`_process_prefix_document`.
      * Keys are fetched and scanned ``_PREPASS_CHUNK`` at a time, with a
        commit after each chunk that seeded a judge.
      * Keys whose ``(state, county)`` cannot be mapped to an entry in
        ``court_ids`` are skipped with a debug log — the main pass would
        skip them too via ``_parse_s3_key``.

    Parameters
    ----------
    conn:
        Open psycopg connection.  Pre-pass commits once per chunk that
        seeded a judge.
    s3_client:
        boto3 S3 client (mockable in tests).
    keys:
        Pre-listed content-addressed S3 keys (output of ``_list_s3_keys``).
        The pre-pass walks the same key list the main pool consumes.
    bucket:
        S3 bucket name; passed through to ``_fetch_s3_content``.
    court_ids:
        Mapping ``{court_code: court_id}`` returned by ``_seed_courts``.
        ``court_code`` is built via ``_derive_court_code`` from the parsed
        ``(state, county)``; the mapping must already include every court
        the prefix walk encounters or the helper will skip those docs.
    parse_timeout:
        Per-document text-extraction timeout (seconds).  Forwarded to
        ``_extract_text_from_content``.
    concurrency:
        Thread pool size for parallel S3 fetches inside the pre-pass.

    Returns
    -------
    dict[str, int]
        ``{"docs_scanned": N, "judges_seeded": M, "judges_skipped_invalid": K}``
        — N is the number of documents the pre-pass observed (after
        unparseable-key / no-S3-content skips), M is the number of
        ``resolve_judge`` calls that returned a judge_id (i.e. successfully
        upserted), K is the number of judge names rejected by
        ``_looks_like_valid_judge_name`` (typically bare surnames).
    """
    docs_scanned = 0
    judges_seeded = 0
    judges_skipped_invalid = 0

    # Resolve each key to a small per-doc descriptor up front; skip any
    # key whose pattern doesn't match (the main pool also skips these via
    # ``_parse_s3_key``) or whose court is missing from ``court_ids``.
    scan_targets: list[dict[str, str]] = []
    for key in keys:
        parsed = _parse_s3_key(key)
        if not parsed:
            continue
        state = _unsluggify(parsed["state"])
        county = _unsluggify(parsed["county"])
        court_code = _derive_court_code(state, county)
        court_id = court_ids.get(court_code)
        if not court_id:
            logger.debug(
                "judge prepass: court_id missing for key, skipping",
                s3_key=key,
                court_code=court_code,
            )
            continue
        scan_targets.append(
            {
                "s3_key": key,
                "court_id": court_id,
                "format": _EXT_TO_FORMAT.get(parsed["ext"], "bin"),
            }
        )

    if not scan_targets:
        logger.info(
            "judge_prepass_no_keys",
            total_keys=len(keys),
        )
        return {
            "docs_scanned": 0,
            "judges_seeded": 0,
            "judges_skipped_invalid": 0,
        }

    # Fetch and scan in chunks of ``_PREPASS_CHUNK`` keys, committing after
    # each chunk that seeded a judge, so a county-wide DB-row run never
    # holds every object in memory and a crash keeps the chunks already
    # done.  Only commit when something was seeded: an empty commit is a
    # redundant round-trip.
    seeded_any = False
    for chunk_start in range(0, len(scan_targets), _PREPASS_CHUNK):
        chunk = scan_targets[chunk_start : chunk_start + _PREPASS_CHUNK]
        chunk_seeded = False
        s3_results: dict[int, bytes] = {}
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    _fetch_s3_content,
                    s3_client,
                    bucket,
                    target["s3_key"],
                ): idx
                for idx, target in enumerate(chunk)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    s3_results[idx] = future.result()
                except Exception:
                    # Best-effort: missing-S3-content just skips the doc for
                    # the pre-pass.  The main pool logs and accounts for it
                    # on its own re-fetch.
                    logger.debug(
                        "judge prepass: S3 fetch failed",
                        s3_key=chunk[idx]["s3_key"],
                        exc_info=True,
                    )

        for idx, target in enumerate(chunk):
            raw_content = s3_results.get(idx)
            if raw_content is None:
                continue

            docs_scanned += 1

            try:
                text = _extract_text_from_content(
                    raw_content,
                    target["format"],
                    pdf_timeout=parse_timeout,
                )
            except Exception:
                logger.debug(
                    "judge prepass: text extraction failed",
                    s3_key=target["s3_key"],
                    exc_info=True,
                )
                continue

            judge_name = extract_judge_name(text)
            if not judge_name:
                continue

            # Only seed FULL names — single-word surnames would just be
            # rejected by ``resolve_judge``'s ``_looks_like_valid_judge_name``
            # guard (which requires ≥2 words).
            if not _looks_like_valid_judge_name(judge_name):
                judges_skipped_invalid += 1
                continue

            try:
                judge_id = resolve_judge(conn, judge_name, target["court_id"])
            except Exception:
                logger.warning(
                    "judge prepass: resolve_judge failed",
                    s3_key=target["s3_key"],
                    judge_name=judge_name,
                    exc_info=True,
                )
                continue

            if judge_id:
                judges_seeded += 1
                chunk_seeded = True
                logger.debug(
                    "judge prepass: seeded judge",
                    s3_key=target["s3_key"],
                    judge_name=judge_name,
                    judge_id=judge_id,
                )

        if chunk_seeded:
            conn.commit()
            seeded_any = True

    logger.info(
        "judge_prepass_complete_prefix",
        total_keys=len(keys),
        scan_targets=len(scan_targets),
        docs_scanned=docs_scanned,
        judges_seeded=judges_seeded,
        judges_skipped_invalid=judges_skipped_invalid,
        committed=seeded_any,
    )

    return {
        "docs_scanned": docs_scanned,
        "judges_seeded": judges_seeded,
        "judges_skipped_invalid": judges_skipped_invalid,
    }


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key`` URI into ``(bucket, key)``.

    Raises ``ValueError`` if *uri* is not an ``s3://`` URI or is missing
    either the bucket or the key component.  Used by the prefix path to
    resolve ``--write-failed-manifest s3://...`` destinations (#4619).
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an s3:// URI: {uri}")
    without_scheme = uri[len("s3://") :]
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        raise ValueError(
            "s3:// URI must include both a bucket and a key "
            f"(e.g. s3://my-bucket/path/keys.txt): {uri}"
        )
    return bucket, key


def _emit_partial_failure_warning(
    *,
    errors: int,
    total: int,
    error_classes: Counter[str] | None = None,
    context: str = "reingest",
) -> float:
    """Emit a WARNING-level summary when ``errors > 0``; return the error ratio.

    Returns ``errors / total`` (0.0 when ``total == 0``).  No-ops (returns the
    ratio without logging) when ``errors == 0`` so all-success runs stay quiet.
    When ``error_classes`` is provided, the most common class is surfaced as
    ``top_error_class`` so ops can see the dominant failure mode (e.g.
    ``BrokenProcessPool`` OOM-kills).  See #4619.
    """
    error_ratio = errors / total if total else 0.0
    if errors <= 0:
        return error_ratio
    top_error_class: str | None = None
    if error_classes:
        most_common = error_classes.most_common(1)
        if most_common:
            top_error_class = most_common[0][0]
    logger.warning(
        "Partial failure during %s — %d of %d documents failed (%.1f%%)",
        context,
        errors,
        total,
        100 * error_ratio,
        errors=errors,
        total=total,
        error_ratio=round(error_ratio, 4),
        top_error_class=top_error_class,
        context=context,
    )
    return error_ratio


def _process_prefix_document(
    key: str,
    bucket: str,
    database_url: str,
    redis_url: str,
    os_url: str,
    bust_llm_cache: bool = False,
) -> dict[str, Any]:
    """Process a single S3 object through the ingestion pipeline.

    Creates its own DB connection, Redis client, and IngestionWorker.
    Designed for ProcessPoolExecutor — each call is fully independent.

    Parameters
    ----------
    bust_llm_cache:
        When True, the per-process ``IngestionWorker`` is constructed with
        ``bust_llm_cache=True`` so every ``LlmExtractor`` it creates skips
        cache reads (#4049).

    Returns a dict with:
      - ``status``: ``"ok"``, ``"skip"``, or ``"error"``
      - ``error_class``: ``None`` for ok/skip results; the exception class
        name (e.g. ``"BrokenProcessPool"``, ``"ValueError"``) for the two
        error-return branches (S3 fetch failure and ``worker.process_event``
        raising).  Surfaced so the prefix path can report the dominant
        failure class in its partial-failure WARNING (#4619).
      - ``hash_mismatch``: whether the S3 key hash did not match the object
        bytes' SHA-256.  Non-fatal — reingest proceeds using the key hash as
        the canonical ``content_hash`` so the worker's LLM split path can
        re-derive any split-children from the raw content.  Mirrors the
        behavior already in ``rebuild_db._process_one_document`` (see #2494,
        #2628).
    """
    parsed = _parse_s3_key(key)
    if not parsed:
        return {"status": "skip", "hash_mismatch": False, "error_class": None}

    from framework.s3_cache import make_s3_client as _make_s3

    s3 = _make_s3()
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()
        capture_timestamp = capture_timestamp_from_s3_object(response)
        provenance = capture_provenance_from_s3_object(response)
    except Exception as exc:
        logger.warning("Failed to fetch S3 object, skipping", s3_key=key, exc_info=True)
        return {
            "status": "error",
            "hash_mismatch": False,
            "error_class": type(exc).__name__,
        }

    if not content:
        return {"status": "skip", "hash_mismatch": False, "error_class": None}

    # Byte-integrity check.  A mismatch used to short-circuit with
    # ``status="error"``, but that caused the ~4% flat-hash orphan rate on
    # Santa Clara (#2628): raws stored under a wrong content-hash filename
    # were permanently unreingestable.  Now we log a warning and let the
    # worker process the raw; the LLM split path derives split-children from
    # the content and stores them with hashes derived from the canonical key
    # hash.  Matches ``rebuild_db._process_one_document`` (#2494).
    #
    # Root cause of the mislabeled filenames: the 2026-03-28 one-time
    # migration script ``scripts/archive/migrate_s3_keys.py`` used DB
    # ``content_hash`` values to build new S3 keys, but for multi-case-PDF
    # split-child rows that value is synthetic (not the hash of any bytes).
    # Live scraper writes are correct.  See #2638 and
    # ``docs/investigations/mislabeled-s3-writes-2026-04.md``.
    actual_hash = hashlib.sha256(content).hexdigest()
    hash_mismatch = actual_hash != parsed["content_hash"]
    if hash_mismatch:
        logger.warning(
            "S3 content hash mismatch — proceeding with reingest using key "
            "hash as canonical content_hash (see #2494, #2628)",
            s3_key=key,
            key_hash=parsed["content_hash"],
            actual_content_hash=actual_hash,
        )

    event = _build_prefix_event(
        key,
        content,
        parsed,
        bucket,
        capture_timestamp=capture_timestamp,
        provenance=provenance,
    )

    # Lazy per-process worker — cached on the function object.
    worker = getattr(_process_prefix_document, "_worker", None)
    if worker is None:
        import redis as redis_lib
        from unittest.mock import MagicMock

        from ingestion.worker import IngestionWorker

        rc = redis_lib.Redis.from_url(redis_url, decode_responses=False)
        if os_url:
            from framework.opensearch_client import make_opensearch_client

            # SigV4-preferred client with local-dev basic-auth fallback; keeps
            # the 30s timeout + 3 retries that make reingest self-healing under
            # load (#2481).  See framework.opensearch_client (#4040).
            os_client = make_opensearch_client(os_url)
        else:
            os_client = MagicMock()
        s3_for_worker = _make_s3()
        # ``bust_llm_cache=...`` propagates the operator's --bust-llm-cache
        # flag through this worker into every ``LlmExtractor`` it creates
        # (multimodal + framework).  This is the only way to re-extract
        # already-split parent PDFs with a fresh LLM call — see #4049.
        worker = IngestionWorker(
            redis_client=rc,
            pg_dsn=database_url,
            opensearch_client=os_client,
            s3_client=s3_for_worker,
            archive_bucket=bucket,
            bust_llm_cache=bust_llm_cache,
        )
        _process_prefix_document._worker = worker  # type: ignore[attr-defined]

    try:
        worker.process_event(event)
        return {"status": "ok", "hash_mismatch": hash_mismatch, "error_class": None}
    except Exception as exc:
        logger.warning("Failed to process document", s3_key=key, exc_info=True)
        return {
            "status": "error",
            "hash_mismatch": hash_mismatch,
            "error_class": type(exc).__name__,
        }


def run_reingest_from_prefix(
    dsn: str,
    *,
    prefix: str,
    concurrency: int = 10,
    limit: int | None = None,
    dry_run: bool = False,
    bust_llm_cache: bool = False,
    parse_timeout: float = 60.0,
    skip_judge_prepass: bool = False,
    s3_key_list: list[str] | None = None,
    write_failed_manifest: str | None = None,
) -> dict[str, Any]:
    """Prefix mode: re-ingest the S3 objects under *prefix*.

    Lists the objects (optionally narrowed to *s3_key_list*, #3855, then
    truncated to *limit*) and runs each through the worker via
    :func:`_reingest_keys`, the same path as DB-row mode
    (:func:`run_reingest`).  Unlike DB-row mode it also finds objects the
    database has no rows for (e.g. dead-lettered events).

    Parameters
    ----------
    dsn:
        PostgreSQL connection string.
    prefix:
        S3 key prefix to scan (e.g. ``"federal/"``).
    concurrency:
        Number of parallel worker processes.
    limit:
        Maximum number of keys to process.
    dry_run:
        If True, log the keys and write nothing: no court seeding, no
        judge pre-pass, no document processing.
    bust_llm_cache:
        If True, every ``LlmExtractor`` the worker builds skips cache reads
        (#4049).
    parse_timeout:
        Judge pre-pass per-document PDF text-extraction timeout (seconds).
    skip_judge_prepass:
        Skip the #4408 / #4419 judge pre-pass.  Emergency rollback only.
    s3_key_list:
        Optional list of full S3 keys to intersect with the listing (S3
        order kept), applied before *limit*, court discovery and the
        pre-pass.  Listed keys not under the prefix are dropped with a
        warning.
    write_failed_manifest:
        Optional ``s3://bucket/key``: when keys fail, write them there (one
        per line) for a retry with ``--s3-key-list`` (#4619).

    Returns
    -------
    dict with keys: ``total_keys``, ``processed``, ``errors``, ``skipped``,
    ``hash_mismatch_warnings``, ``wall_time_seconds``, ``error_ratio``,
    ``top_error_class``, ``failed_keys``, and (when the judge pre-pass ran)
    ``judge_prepass_docs_scanned``, ``judge_prepass_judges_seeded``,
    ``judge_prepass_judges_skipped_invalid``.
    """
    bucket = os.environ.get(
        "JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev"
    )
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

    # Step 1b: Narrow to the --s3-key-list intersection (#3855/#4049).
    # Applied before --limit truncation and before court discovery / the
    # judge pre-pass so every downstream step sees the narrowed set.
    if s3_key_list:
        requested = set(s3_key_list)
        matched = [key for key in keys if key in requested]
        missing = requested - set(matched)
        logger.info(
            "Filtered prefix keys by --s3-key-list",
            requested=len(requested),
            matched=len(matched),
            missing=len(missing),
        )
        if missing:
            logger.warning(
                "Some --s3-key-list keys were not found under the prefix",
                missing_count=len(missing),
                prefix=prefix,
            )
        keys = matched
        if not keys:
            logger.warning(
                "No --s3-key-list keys matched under prefix",
                prefix=prefix,
            )
            return {
                "total_keys": 0,
                "processed": 0,
                "errors": 0,
                "skipped": 0,
                "hash_mismatch_warnings": 0,
                "wall_time_seconds": 0.0,
            }

    if limit is not None:
        total_available = len(keys)
        keys = keys[:limit]
        logger.info(
            "Limited to first N keys", limit=limit, total_available=total_available
        )

    return _reingest_keys(
        dsn,
        keys,
        bucket=bucket,
        concurrency=concurrency,
        dry_run=dry_run,
        bust_llm_cache=bust_llm_cache,
        parse_timeout=parse_timeout,
        skip_judge_prepass=skip_judge_prepass,
        write_failed_manifest=write_failed_manifest,
        s3_client=s3_client,
    )


#: DB-row mode: the S3 keys of the ``documents`` rows that match the filters,
#: oldest capture first.  ``{filters}`` is ``_build_filters`` output.
DB_KEYS_QUERY = """
    SELECT d.s3_key
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN cases c ON c.id = d.case_id
    WHERE d.status = 'active'
      AND d.s3_key IS NOT NULL
    {filters}
    GROUP BY d.s3_key
    ORDER BY min(d.captured_at), d.s3_key
"""


def select_db_keys(
    conn: psycopg.Connection,
    filters: str,
    params: list,
    *,
    limit: int | None = None,
) -> list[str]:
    """Return the S3 keys of the ``documents`` rows matching *filters*.

    A split key has one row per ruling; it is returned once and re-ingested
    as one object, so the worker re-derives its whole split set.
    """
    sql = DB_KEYS_QUERY.format(filters=filters)
    query_params = list(params)
    if limit is not None:
        sql += " LIMIT %s"
        query_params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, query_params)
        return [str(row[0]) for row in cur.fetchall()]


def run_reingest(
    dsn: str,
    *,
    county: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int = 10,
    parse_timeout: float = 60.0,
    case_title_regex: str | None = None,
    null_motion_type: bool = False,
    orphaned_only: bool = False,
    case_number_like: str | None = None,
    filter_null_outcome: bool = False,
    bust_llm_cache: bool = False,
    department_in: list[str] | None = None,
    s3_key_list: list[str] | None = None,
    skip_judge_prepass: bool = False,
    write_failed_manifest: str | None = None,
) -> dict[str, Any]:
    """DB-row mode: re-ingest the S3 objects behind the matching rows.

    Selects the keys (:func:`select_db_keys`), then runs each object through
    the worker exactly like prefix mode (:func:`_reingest_keys`).  Returns
    the same stats as :func:`run_reingest_from_prefix`.
    """
    filters, filter_params = _build_filters(
        county,
        date_from,
        date_to,
        case_title_regex=case_title_regex,
        null_motion_type=null_motion_type,
        orphaned_only=orphaned_only,
        case_number_like=case_number_like,
        filter_null_outcome=filter_null_outcome,
        department_in=department_in,
        s3_key_list=s3_key_list,
    )
    with psycopg.connect(dsn) as conn:
        keys = select_db_keys(conn, filters, filter_params, limit=limit)
    logger.info("Selected S3 keys from the database", count=len(keys), county=county)
    unparseable = [key for key in keys if not _parse_s3_key(key)]
    if unparseable:
        # Only content-addressed keys can be re-ingested (the worker event
        # is built from the key); these are counted as skipped.
        logger.warning(
            "Selected keys that are not content-addressed will be skipped",
            count=len(unparseable),
            sample=unparseable[:10],
        )
    if not keys:
        return {"total_keys": 0, "processed": 0, "errors": 0, "skipped": 0}
    return _reingest_keys(
        dsn,
        keys,
        bucket=os.environ.get(
            "JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev"
        ),
        concurrency=concurrency,
        dry_run=dry_run,
        bust_llm_cache=bust_llm_cache,
        parse_timeout=parse_timeout,
        skip_judge_prepass=skip_judge_prepass,
        write_failed_manifest=write_failed_manifest,
        mode="db",
    )


def _reingest_keys(
    dsn: str,
    keys: list[str],
    *,
    bucket: str,
    concurrency: int,
    dry_run: bool,
    bust_llm_cache: bool,
    parse_timeout: float,
    skip_judge_prepass: bool,
    write_failed_manifest: str | None,
    s3_client: object | None = None,
    mode: str = "prefix",
) -> dict[str, Any]:
    """Run each S3 object in *keys* through ``IngestionWorker.process_event``.

    Seeds the courts, runs the judge pre-pass (#4408 / #4419), then
    processes the keys in a process pool.  Shared by both modes; *mode*
    (``prefix`` / ``db``) only labels the logs.  A dry run logs the keys and
    writes nothing.
    """
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    os_url = os.environ.get("OPENSEARCH_URL", "")
    if s3_client is None:
        s3_client = boto3.client("s3")

    # Step 2: Discover and seed courts from key prefixes.
    courts = _discover_courts(keys)
    logger.info(
        "Discovered courts from S3 keys",
        count=len(courts),
        courts=[c["court_code"] for c in courts],
    )

    if dry_run:
        logger.info(
            "Dry run — skipping court seeding and document processing",
            mode=mode,
            total_keys=len(keys),
            keys_sample=keys[:50],
        )
        return {
            "total_keys": len(keys),
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }

    with psycopg.connect(dsn) as conn:
        court_ids = _seed_courts(conn, courts)
    logger.info("Courts seeded", court_ids=court_ids)

    # Step 2b: Judge pre-pass (#4408 / #4419) — seed full-name judges
    # before the per-doc ``ProcessPoolExecutor`` runs so single-word
    # JUDGE/DEPT surnames resolve on the first invocation regardless of
    # worker-pool completion order (the #4397 chronological-resolver race).
    prepass_stats: dict[str, int] | None = None
    if skip_judge_prepass:
        logger.warning(
            "judge_prepass_skipped",
            reason="--skip-judge-prepass flag set (emergency rollback)",
        )
    else:
        prepass_t0 = time.monotonic()
        with psycopg.connect(dsn) as prepass_conn:
            prepass_stats = _seed_judges_from_keys(
                prepass_conn,
                s3_client,
                keys,
                bucket,
                court_ids,
                parse_timeout=parse_timeout,
                concurrency=concurrency,
            )
        logger.info(
            "judge_prepass_complete",
            mode=mode,
            docs_scanned=prepass_stats["docs_scanned"],
            judges_seeded=prepass_stats["judges_seeded"],
            judges_skipped_invalid=prepass_stats["judges_skipped_invalid"],
            wall_time_seconds=round(time.monotonic() - prepass_t0, 2),
        )

    # Step 3: Process documents using child processes.
    # Uses ProcessPoolExecutor (like rebuild_db.py) because pdfplumber/pdfminer
    # C extensions are not thread-safe.
    database_url = dsn
    if not os_url:
        logger.info("OPENSEARCH_URL not set — skipping search indexing")

    t_start = time.monotonic()
    processed = 0
    errors = 0
    skipped = 0
    hash_mismatch_warnings = 0
    failed_keys: list[str] = []
    error_classes: Counter[str] = Counter()

    logger.info(
        "Processing documents from S3",
        concurrency=concurrency,
        total=len(keys),
    )

    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _process_prefix_document,
                key,
                bucket,
                database_url,
                redis_url,
                os_url,
                bust_llm_cache,
            ): key
            for key in keys
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
                # ``_process_prefix_document`` returns a dict with ``status``
                # and ``hash_mismatch`` keys.  Older versions returned a bare
                # string — tolerate that shape for one release to avoid
                # breaking any callers that pickled intermediate results.
                if isinstance(result, dict):
                    status = result.get("status", "error")
                    if result.get("hash_mismatch"):
                        hash_mismatch_warnings += 1
                else:
                    status = result
                if status == "ok":
                    processed += 1
                elif status == "skip":
                    skipped += 1
                else:
                    errors += 1
                    failed_keys.append(key)
                    if isinstance(result, dict):
                        error_classes[result.get("error_class") or "unknown"] += 1
                    else:
                        error_classes["unknown"] += 1
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
                failed_keys.append(key)
                error_classes[type(exc).__name__] += 1
                logger.error("Failed to process", key=key, error=str(exc))

    wall_time = round(time.monotonic() - t_start, 2)

    # Partial-failure surfacing (#4619).  Denominator is the full key set so
    # the ratio reflects fraction-of-attempted, and the WARNING names the
    # dominant failure class (e.g. BrokenProcessPool OOM-kills).
    error_ratio = _emit_partial_failure_warning(
        errors=errors,
        total=len(keys),
        error_classes=error_classes,
        context="reingest",
    )
    top_error_class: str | None = None
    if error_classes:
        top_error_class = error_classes.most_common(1)[0][0]

    stats: dict[str, Any] = {
        "total_keys": len(keys),
        "processed": processed,
        "errors": errors,
        "skipped": skipped,
        "hash_mismatch_warnings": hash_mismatch_warnings,
        "wall_time_seconds": wall_time,
        "error_ratio": error_ratio,
        "top_error_class": top_error_class,
        "failed_keys": failed_keys,
    }
    if prepass_stats is not None:
        stats["judge_prepass_docs_scanned"] = prepass_stats["docs_scanned"]
        stats["judge_prepass_judges_seeded"] = prepass_stats["judges_seeded"]
        stats["judge_prepass_judges_skipped_invalid"] = prepass_stats[
            "judges_skipped_invalid"
        ]

    # Spread the stats into the completion log but report ``failed_keys`` as a
    # count rather than dumping the full list (which can be hundreds of keys).
    log_stats = {k: v for k, v in stats.items() if k != "failed_keys"}
    logger.info(
        "Key reingest complete",
        mode=mode,
        failed_key_count=len(failed_keys),
        **log_stats,
    )

    if write_failed_manifest and failed_keys:
        manifest_bucket, manifest_key = _parse_s3_uri(write_failed_manifest)
        body = ("\n".join(failed_keys) + "\n").encode("utf-8")
        s3_client.put_object(Bucket=manifest_bucket, Key=manifest_key, Body=body)
        logger.info(
            "Wrote failed-keys manifest",
            manifest_uri=write_failed_manifest,
            failed_key_count=len(failed_keys),
        )

    if hash_mismatch_warnings > 0:
        logger.warning(
            "%d raw S3 objects had content-hash mismatches — reingest "
            "proceeded using the S3 key hash as canonical content_hash "
            "(see #2494, #2628).  Investigate the scraper S3 upload path "
            "if this count is non-trivial.",
            hash_mismatch_warnings,
            hash_mismatch_warnings=hash_mismatch_warnings,
            processed=processed,
        )

    return stats


# Default partial-failure exit-gate threshold applied automatically to
# ``--bust-llm-cache`` prefix reingests when the operator does not pass an
# explicit ``--max-error-ratio`` and does not opt out via
# ``--no-fail-on-errors`` (#4624).  A cache-bust prefix reingest is the exact
# path that recreated the #3855 incident (Orange residual OOM-killed 167/768
# PDFs, exited 0).  ``--bust-llm-cache`` runs re-extract every key through the
# LLM, so a non-trivial error fraction is always a signal that the run
# silently lost data — failing loud by default closes the "operator forgot
# --max-error-ratio" gap that is the same root cause as the original
# incident.  0.10 (10%) is deliberately permissive: it does not trip on the
# occasional single-key transcription error in a small prefix, but it does
# catch the bulk-OOM / bulk-timeout failure mode #3855 produced.
_DEFAULT_CACHE_BUST_PREFIX_MAX_ERROR_RATIO = 0.10


def _resolve_effective_max_error_ratio(
    *,
    explicit_max_error_ratio: float | None,
    no_fail_on_errors: bool,
    prefix: str | None,
    bust_llm_cache: bool,
) -> float | None:
    """Decide the effective partial-failure exit threshold for this run (#4624).

    Resolution order (first match wins):

    1.  ``--no-fail-on-errors`` → return ``None`` (explicit opt-out).  The run
        exits 0 on partial failure exactly like every pre-#4619 caller.  This
        is the escape hatch for the rare run where partial failure is expected
        (e.g. a known-bad prefix being drained best-effort).
    2.  An explicit ``--max-error-ratio`` → return it verbatim (operator chose
        their own threshold; their choice always wins, including ``0.0`` for
        a fail-on-any-error run or a deliberately high ceiling).
    3.  A ``--bust-llm-cache`` **prefix** reingest with no explicit threshold →
        return :data:`_DEFAULT_CACHE_BUST_PREFIX_MAX_ERROR_RATIO`.  This is the
        new default-on gate: the cache-bust prefix path that recreated #3855
        now fails loud unless the operator opts out.
    4.  Anything else (standard DB-row mode, non-cache-bust prefix runs) →
        return ``None``, preserving the existing opt-in exit-code contract so
        no existing caller's exit behavior changes (#4619).

    Returning ``None`` means :func:`_enforce_max_error_ratio` is a no-op and
    the run exits 0 regardless of error ratio — the backward-compatible path.
    """
    if no_fail_on_errors:
        return None
    if explicit_max_error_ratio is not None:
        return explicit_max_error_ratio
    if prefix and bust_llm_cache:
        return _DEFAULT_CACHE_BUST_PREFIX_MAX_ERROR_RATIO
    return None


def _enforce_max_error_ratio(max_error_ratio: float | None, error_ratio: float) -> None:
    """Exit non-zero when ``error_ratio`` strictly exceeds the threshold.

    No-op when ``max_error_ratio`` is ``None`` (no gate is in effect for this
    run — neither an explicit ``--max-error-ratio`` nor the #4624 cache-bust
    prefix default applied).  This preserves the ECS-oneshot exit-code contract
    for existing callers, which exit 0 on partial failure.  Only a set
    threshold that is strictly exceeded triggers ``sys.exit(1)``; all-success
    and below-threshold both return.  See #4619 and #4624.
    """
    if max_error_ratio is None:
        return
    if error_ratio > max_error_ratio:
        logger.warning(
            "Error ratio %.4f exceeds --max-error-ratio %.4f — failing run",
            error_ratio,
            max_error_ratio,
            error_ratio=round(error_ratio, 4),
            max_error_ratio=max_error_ratio,
        )
        sys.exit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-ingest archived S3 objects through the ingestion worker. "
            "DB-row mode (default) selects keys from the documents table; "
            "--prefix lists them from S3."
        ),
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
        "--dry-run",
        action="store_true",
        help="Log the keys that would be re-ingested (count + sample); write nothing.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max S3 keys to re-ingest."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Number of parallel worker processes (default: 10).",
    )
    parser.add_argument(
        "--parse-timeout",
        type=float,
        default=60.0,
        help="Judge pre-pass per-document PDF text timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--case-number-like",
        type=str,
        default=None,
        help="DB-row mode: documents whose case_number matches this LIKE pattern.",
    )
    parser.add_argument(
        "--case-title-regex",
        type=str,
        default=None,
        help="DB-row mode: documents whose case_title matches this regex.",
    )
    parser.add_argument(
        "--null-motion-type",
        action="store_true",
        help="DB-row mode: documents with a ruling whose motion_type is NULL.",
    )
    parser.add_argument(
        "--filter-null-outcome",
        action="store_true",
        help="DB-row mode: documents with a ruling whose outcome is NULL.",
    )
    parser.add_argument(
        "--orphaned-only",
        action="store_true",
        help="DB-row mode: documents with no ruling.",
    )
    parser.add_argument(
        "--department-in",
        nargs="+",
        type=str,
        default=None,
        dest="department_in",
        help="DB-row mode: documents with a ruling in one of these departments.",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help=(
            "Prefix mode: re-ingest the S3 objects under this key prefix "
            "(e.g. orange/), whether or not the database has rows for them."
        ),
    )
    parser.add_argument(
        "--bust-llm-cache",
        action="store_true",
        help="Skip LLM cache reads for this run; cache writes still happen (#2424).",
    )
    parser.add_argument(
        "--s3-key-list",
        type=str,
        default=None,
        dest="s3_key_list",
        help=(
            "File (local path or s3:// URI, #4606) listing S3 keys, one per "
            "line.  DB-row mode: only these keys.  Prefix mode: the listed "
            "keys under the prefix (#3855)."
        ),
    )
    parser.add_argument(
        "--skip-judge-prepass",
        action="store_true",
        dest="skip_judge_prepass",
        help="Skip the judge pre-pass (#4408, #4419).  Emergency rollback only.",
    )
    parser.add_argument(
        "--max-error-ratio",
        type=float,
        default=None,
        help=(
            "Exit non-zero when the fraction of keys that failed strictly "
            "exceeds this value (0.0-1.0).  See #4619."
        ),
    )
    parser.add_argument(
        "--no-fail-on-errors",
        action="store_true",
        dest="no_fail_on_errors",
        help="Never exit non-zero on partial failure (#4624).",
    )
    parser.add_argument(
        "--write-failed-manifest",
        type=str,
        default=None,
        dest="write_failed_manifest",
        help=(
            "An s3://bucket/key destination.  When keys fail, write them "
            "(one per line) there for a retry with --s3-key-list (#4619)."
        ),
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Resolve --s3-key-list path → list of keys (or None when not provided).
    # Loaded eagerly so a missing file or empty list fails fast, before any
    # DB connect or S3 client setup.
    s3_key_list_values: list[str] | None = None
    if args.s3_key_list:
        try:
            s3_key_list_values = _read_s3_key_list_file(args.s3_key_list)
        except FileNotFoundError:
            parser.error(f"--s3-key-list file not found: {args.s3_key_list}")
        except ValueError as err:
            parser.error(str(err))
        logger.info(
            "Loaded --s3-key-list",
            path=args.s3_key_list,
            count=len(s3_key_list_values),
        )

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    common: dict[str, Any] = {
        "concurrency": args.concurrency,
        "limit": args.limit,
        "dry_run": args.dry_run,
        "bust_llm_cache": args.bust_llm_cache,
        "parse_timeout": args.parse_timeout,
        "skip_judge_prepass": args.skip_judge_prepass,
        "s3_key_list": s3_key_list_values,
        "write_failed_manifest": args.write_failed_manifest,
    }
    if args.prefix:
        stats = run_reingest_from_prefix(dsn, prefix=args.prefix, **common)
    else:
        stats = run_reingest(
            dsn,
            county=args.county,
            date_from=date.fromisoformat(args.date_from) if args.date_from else None,
            date_to=date.fromisoformat(args.date_to) if args.date_to else None,
            case_title_regex=args.case_title_regex,
            null_motion_type=args.null_motion_type,
            orphaned_only=args.orphaned_only,
            case_number_like=args.case_number_like,
            filter_null_outcome=args.filter_null_outcome,
            department_in=args.department_in,
            **common,
        )
    logger.info(
        "Reingest complete",
        mode="prefix" if args.prefix else "db",
        total_keys=stats["total_keys"],
        processed=stats["processed"],
        errors=stats["errors"],
        skipped=stats["skipped"],
        judge_prepass_judges_seeded=stats.get("judge_prepass_judges_seeded"),
    )
    effective_max_error_ratio = _resolve_effective_max_error_ratio(
        explicit_max_error_ratio=args.max_error_ratio,
        no_fail_on_errors=args.no_fail_on_errors,
        prefix=args.prefix,
        bust_llm_cache=args.bust_llm_cache,
    )
    _enforce_max_error_ratio(effective_max_error_ratio, stats.get("error_ratio", 0.0))


if __name__ == "__main__":
    main()
