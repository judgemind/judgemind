"""Capture-path content-hash deduplication helpers.

These helpers short-circuit the scraper before archival when the same
content (identified by SHA-256 hash) was already captured within the last
N days for the same county.  The check is best-effort: any failure
(conn is None, DB error) returns None and the scraper continues normally.

Telemetry:
    ``record_capture_dedup_skipped`` writes one row to
    ``telemetry.data_quality_metrics`` with
    ``metric_name='capture_content_hash_dedup_skipped'``.  Modelled on
    the existing ``content_hash_dedup_supersede`` telemetry in
    ``ingestion/db.py:1811-1831``.  Exceptions are swallowed — telemetry
    must never block the scraper.
"""

from __future__ import annotations

import json

_SEEN_AT_SQL = """
SELECT d.id::text
FROM derived.documents d
JOIN derived.courts c ON d.court_id = c.id
WHERE c.county = %s
  AND d.content_hash = %s
  AND d.status = 'active'
  AND d.captured_at >= now() - make_interval(days => %s)
ORDER BY d.captured_at DESC
LIMIT 1
"""

_INSERT_METRIC_SQL = """
INSERT INTO telemetry.data_quality_metrics
    (recorded_at, county, metric_name, metric_value, metadata)
VALUES (now(), %s, %s, %s, %s::jsonb)
"""


def content_hash_seen_at(
    conn: object,
    county: str,
    content_hash: str,
    days: int = 14,
) -> str | None:
    """Return the winner document id if the content hash was seen within *days* days.

    Parameters
    ----------
    conn:
        A ``psycopg.Connection`` (autocommit) or ``None``.  ``None`` is
        treated as "no DB available" and returns ``None`` immediately.
    county:
        County name matching ``courts.county`` (e.g. ``"Los Angeles"``).
    content_hash:
        SHA-256 hex digest of the document's raw content.
    days:
        Look-back window in days.  Default 14.

    Returns
    -------
    str | None
        The ``documents.id`` UUID string of the earliest winner captured
        within the window, or ``None`` on miss / error / no connection.
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(_SEEN_AT_SQL, (county, content_hash, days))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:  # noqa: BLE001 — best-effort, never blocks scraper
        return None


def record_capture_dedup_skipped(
    conn: object,
    *,
    county: str,
    content_hash: str,
    winner_document_id: str,
    new_source_url: str,
    scraper_id: str,
) -> None:
    """Insert a telemetry row recording that a capture was skipped due to dedup.

    Mirrors the ``content_hash_dedup_supersede`` telemetry written in
    ``ingestion/db.py``.  All exceptions are swallowed — telemetry is
    best-effort and must never block the scraper.

    Parameters
    ----------
    conn:
        A ``psycopg.Connection`` or ``None``.  ``None`` is a no-op.
    county:
        County name for the metric row.
    content_hash:
        SHA-256 hex digest (informational — not stored in this row).
    winner_document_id:
        The existing document that won the dedup check.
    new_source_url:
        The source URL of the document that was about to be captured.
    scraper_id:
        Scraper identifier (e.g. ``"ca-cc-tentatives"``).
    """
    if conn is None:
        return
    try:
        metadata = json.dumps(
            {
                "winner_document_id": winner_document_id,
                "new_source_url": new_source_url,
                "scraper_id": scraper_id,
            }
        )
        with conn.cursor() as cur:
            cur.execute(
                _INSERT_METRIC_SQL,
                (county, "capture_content_hash_dedup_skipped", 1, metadata),
            )
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        pass
