"""Durable records of raw keys whose rows are not a complete split (#5008).

Some ingestion outcomes leave a key with incomplete rows, or none:

- ``single_document_fallback``: a live capture of a new key whose
  extraction failed writes the whole document as one ruling (#4948);
- ``partial_split``: a live capture of a new key whose extraction was
  partial writes only the rulings the parts that answered yielded (#5000);
- ``exhausted_child``: a split child whose per-child LLM enrichment was
  exhausted keeps its slot as it was (on a new key: missing, #4878).  A
  whole-document child (the split's only one) raises before any write, so
  it is recorded on its own transaction before the error propagates, on a
  reingest as on a live capture (#5035);
- ``dead_letter``: the live worker gave up on the event (retries exhausted,
  or a schema error), so nothing reached ``derived.*``;
- ``reingest_failed``: a reingest / rebuild refused an incomplete
  extraction and left the key's rows as they were (#4948, #5000).  Before
  #5008 the key was only listed in the reingest's failed manifest, under
  ``tmp/keys/``, which expires after 30 days.
- ``stale_validation``: written by the raw-key reconciler, not the worker
  (#5027).  Every ruling of the key failed validation under a version of a
  rule since relaxed (``validation.rule_versions``), so the key is
  re-ingested for the current rules to judge.  A reingest that writes the
  key, or fails it again under the current rules, resolves it.

Each upserts one ``telemetry.ingest_incomplete`` row per key, keyed by
``s3_key`` (the latest reason wins, ``occurrences`` counts them).  A later
complete write of the key (a split-set write with every child extracted, a
whole document, a recorded skip verdict) deletes it.  The daily raw-key
reconciler (``scripts/reconcile_raw_keys.py``) re-ingests marked keys and
counts its attempts in ``retry_attempts``; past its cap a key is reported
as stuck instead of retried forever.

The table is in ``telemetry.*``, not a status column on
``derived.documents``: a dead-lettered key has no document row to carry a
status, and the attempt count must survive a rebuild of ``derived.*``.

Writes are best-effort and savepoint-protected: a failure (for example the
table not existing yet because the worker deployed before the migration)
never aborts the caller's transaction or the event.  The caller commits.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

INCOMPLETE_SINGLE_DOCUMENT_FALLBACK = "single_document_fallback"
INCOMPLETE_PARTIAL_SPLIT = "partial_split"
INCOMPLETE_EXHAUSTED_CHILD = "exhausted_child"
INCOMPLETE_DEAD_LETTER = "dead_letter"
INCOMPLETE_REINGEST_FAILED = "reingest_failed"
#: Recorded by ``scripts/reconcile_raw_keys.py`` (#5027), never the worker.
INCOMPLETE_STALE_VALIDATION = "stale_validation"

INCOMPLETE_REASONS = frozenset(
    {
        INCOMPLETE_SINGLE_DOCUMENT_FALLBACK,
        INCOMPLETE_PARTIAL_SPLIT,
        INCOMPLETE_EXHAUSTED_CHILD,
        INCOMPLETE_DEAD_LETTER,
        INCOMPLETE_REINGEST_FAILED,
        INCOMPLETE_STALE_VALIDATION,
    }
)

_UPSERT_SQL = """
    INSERT INTO telemetry.ingest_incomplete
        (s3_key, reason, county, scraper_id, document_id, detail)
    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (s3_key) DO UPDATE SET
        reason = EXCLUDED.reason,
        county = COALESCE(EXCLUDED.county, telemetry.ingest_incomplete.county),
        scraper_id = COALESCE(EXCLUDED.scraper_id, telemetry.ingest_incomplete.scraper_id),
        document_id = COALESCE(EXCLUDED.document_id, telemetry.ingest_incomplete.document_id),
        detail = EXCLUDED.detail,
        last_recorded_at = now(),
        occurrences = telemetry.ingest_incomplete.occurrences + 1
"""

_DELETE_SQL = "DELETE FROM telemetry.ingest_incomplete WHERE s3_key = %s"

#: The clear runs on every complete write, so a failure that repeats on
#: every write (the table not migrated yet) is logged with its traceback
#: once per process, then at debug level.
_clear_failure_logged = False


def _run_protected(conn: Any, sql: str, params: tuple[Any, ...], what: str, s3_key: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT ingest_incomplete")
            try:
                cur.execute(sql, params)
                cur.execute("RELEASE SAVEPOINT ingest_incomplete")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT ingest_incomplete")
                raise
    except Exception:  # noqa: BLE001 — the record is best-effort
        global _clear_failure_logged
        quiet = what == "clear" and _clear_failure_logged
        if what == "clear":
            _clear_failure_logged = True
        logger.log(
            logging.DEBUG if quiet else logging.WARNING,
            "ingest incomplete record %s failed",
            what,
            exc_info=not quiet,
            extra={"s3_key": s3_key},
        )
        return False
    return True


def record_ingest_incomplete(
    conn: Any,
    *,
    s3_key: str | None,
    reason: str,
    county: str | None = None,
    scraper_id: str | None = None,
    document_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Upsert the incomplete record for *s3_key*; return True when written.

    Runs inside the caller's transaction under a savepoint and never raises.
    Nothing is written for an event without an ``s3_key``, nor for a reason
    outside :data:`INCOMPLETE_REASONS`.
    """
    if not s3_key:
        return False
    if reason not in INCOMPLETE_REASONS:
        logger.warning(
            "unknown ingest incomplete reason; not recorded",
            extra={"s3_key": s3_key, "incomplete_reason": reason},
        )
        return False
    params = (
        s3_key,
        reason,
        county,
        scraper_id,
        document_id,
        json.dumps(detail or {}, default=str),
    )
    ok = _run_protected(conn, _UPSERT_SQL, params, "write", s3_key)
    if ok:
        logger.info(
            "Recorded an incomplete ingest outcome for the key",
            extra={
                "telemetry_event": "ingest_incomplete.recorded",
                "s3_key": s3_key,
                "incomplete_reason": reason,
            },
        )
    return ok


def clear_ingest_incomplete(conn: Any, s3_key: str | None) -> bool:
    """Delete *s3_key*'s incomplete record after a complete write.

    Runs inside the caller's transaction under a savepoint and never raises;
    a no-op when the key has no record.
    """
    if not s3_key:
        return False
    return _run_protected(conn, _DELETE_SQL, (s3_key,), "clear", s3_key)
