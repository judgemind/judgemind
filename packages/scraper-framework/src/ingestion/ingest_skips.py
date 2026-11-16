"""Durable records of raw keys the worker deliberately writes no document for (#5015).

Every archived raw key should either be represented in ``derived.documents``
or have a recorded reason it is not.  ``scripts/reconcile_raw_keys.py``
diffs the archive against the database daily; without a durable reason it
cannot tell a deliberate skip from a lost event.  Two non-write paths were
already durable before #5015 and stay where they are:

- a validation failure: ``telemetry.validation_results.s3_key``;
- the all-NULL metadata guard: ``telemetry.data_quality_metrics`` metric
  ``non_ruling_pdf_dropped`` with ``metadata.s3_key``.

The rest were log-only.  They now upsert one ``telemetry.ingest_skips`` row
per key, keyed by ``s3_key`` with the reason (the latest verdict wins).

The write is best-effort and savepoint-protected: a failure (for example the
table not existing yet because the worker deployed before the migration)
never aborts the caller's transaction or the event.  The caller commits.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: ca-governor-appointments press releases, routed away from ruling
#: extraction (#3688, #5016).
SKIP_GOVERNOR_BIO_ROUTE = "governor_bio_route"
#: Santa Clara "tentative rulings are not available" notice (#4725).
SKIP_SANTA_CLARA_PLACEHOLDER = "santa_clara_placeholder"
#: A multimodal PDF with no rulings and no text layer to fall back on (#4714).
SKIP_ZERO_RULINGS_NO_TEXT = "zero_rulings_no_text"
#: A PDF nothing could read; a retry cannot succeed (#4983, #4987).
SKIP_NO_EXTRACTABLE_TEXT = "no_extractable_text"
#: Every row the multimodal split found had no ruling text (calendar rows
#: with an empty tentative cell, fused tail rows; #4714).
SKIP_NO_RULING_TEXT = "no_ruling_text"
#: Written by ``scripts/reconcile_raw_keys.py``, never the worker (#5093): a
#: ``telemetry.ingest_incomplete`` key whose raw object is gone from the
#: archive, closed out so it is reported once and never retried.  The
#: detail keeps the incomplete reason, detail and attempts.
SKIP_RAW_MISSING = "raw_missing"

SKIP_REASONS = frozenset(
    {
        SKIP_RAW_MISSING,
        SKIP_GOVERNOR_BIO_ROUTE,
        SKIP_SANTA_CLARA_PLACEHOLDER,
        SKIP_ZERO_RULINGS_NO_TEXT,
        SKIP_NO_EXTRACTABLE_TEXT,
        SKIP_NO_RULING_TEXT,
    }
)

_UPSERT_SQL = """
    INSERT INTO telemetry.ingest_skips
        (s3_key, reason, county, scraper_id, document_id, detail)
    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (s3_key) DO UPDATE SET
        reason = EXCLUDED.reason,
        county = EXCLUDED.county,
        scraper_id = EXCLUDED.scraper_id,
        document_id = EXCLUDED.document_id,
        detail = EXCLUDED.detail,
        last_recorded_at = now(),
        occurrences = telemetry.ingest_skips.occurrences + 1
"""


def record_ingest_skip(
    conn: Any,
    *,
    s3_key: str | None,
    reason: str,
    county: str | None = None,
    scraper_id: str | None = None,
    document_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Upsert the skip record for *s3_key*; return True when it was written.

    Runs inside the caller's transaction under a savepoint and never raises.
    Nothing is written for an event without an ``s3_key`` (there is no raw
    key to explain), nor for a reason outside :data:`SKIP_REASONS`.
    """
    if not s3_key:
        return False
    if reason not in SKIP_REASONS:
        logger.warning(
            "unknown ingest skip reason; not recorded",
            extra={"s3_key": s3_key, "skip_reason": reason},
        )
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT ingest_skip")
            try:
                cur.execute(
                    _UPSERT_SQL,
                    (
                        s3_key,
                        reason,
                        county,
                        scraper_id,
                        document_id,
                        json.dumps(detail or {}, default=str),
                    ),
                )
                cur.execute("RELEASE SAVEPOINT ingest_skip")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT ingest_skip")
                raise
    except Exception:  # noqa: BLE001 — the record is best-effort
        logger.warning(
            "ingest skip record write failed",
            exc_info=True,
            extra={"s3_key": s3_key, "skip_reason": reason},
        )
        return False
    return True
