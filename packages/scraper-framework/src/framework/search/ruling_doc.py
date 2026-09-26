"""Build search documents from the committed ``derived.*`` ruling row.

OpenSearch is fully derivable from ``derived.*``.  Every writer of the
``tentative_rulings`` index builds its documents here, from the row Postgres
actually kept — never from raw scraper or extraction values:

* the ingestion worker, right after it commits a ruling;
* ``scripts/reindex_search_from_db.py``, when it audits or repairs the index.

The ruling upsert is not a plain overwrite.  ``case_id`` and ``judge_id``
keep their first value, and ``hearing_date`` / ``motion_type`` / ``outcome``
/ ``summary`` keep the stored value when a re-extraction yields NULL.  A doc
built from the event would therefore drift from Postgres on re-ingest (a
missed hearing date drops the ruling out of default search, which filters on
``hearing_date <= today``).  Building from the stored row, through one shared
function, keeps the two writers from disagreeing (#4785).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from .indexer import normalize_hearing_date

# derived.* → search-event projection.  One ruling per document
# (``uq_rulings_document_id``); DISTINCT ON keeps the result deterministic
# even if that ever stops holding.
RULING_SEARCH_SQL = """
    SELECT DISTINCT ON (r.document_id)
        r.document_id::text     AS document_id,
        c.case_number           AS case_number,
        co.court_name           AS court,
        co.county               AS county,
        co.state                AS state,
        j.canonical_name        AS judge_name,
        r.hearing_date          AS hearing_date,
        r.motion_type           AS motion_type,
        r.outcome::text         AS outcome,
        c.case_title            AS case_title,
        c.case_type             AS case_type,
        r.summary               AS summary,
        r.ruling_text           AS ruling_text,
        d.s3_key                AS s3_key,
        d.content_hash          AS content_hash,
        d.format::text          AS content_format
    FROM derived.rulings r
    JOIN derived.documents d ON d.id = r.document_id
    JOIN derived.cases c ON c.id = r.case_id
    JOIN derived.courts co ON co.id = r.court_id
    LEFT JOIN derived.judges j ON j.id = r.judge_id
    WHERE r.document_id = ANY(%s::uuid[])
    ORDER BY r.document_id, r.id
"""

FETCH_BATCH = 1000
SUMMARY_FALLBACK_CHARS = 500


def _valid_uuids(document_ids: Iterable[str]) -> list[str]:
    """Drop ids that are not UUIDs (they cannot name a ruling's document)."""
    valid: list[str] = []
    for doc_id in document_ids:
        try:
            uuid.UUID(str(doc_id))
        except ValueError:
            continue
        valid.append(str(doc_id))
    return valid


def fetch_ruling_search_rows(
    conn: Any, document_ids: Iterable[str], *, batch_size: int = FETCH_BATCH
) -> dict[str, dict[str, Any]]:
    """Return ``{document_id: row}`` for the committed rulings of *document_ids*.

    A document with no ruling row (e.g. one superseded by content-hash
    dedup) is absent from the result.
    """
    ids = _valid_uuids(document_ids)
    rows: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        for start in range(0, len(ids), batch_size):
            cur.execute(RULING_SEARCH_SQL, (ids[start : start + batch_size],))
            cols = [col.name for col in cur.description or ()]
            for rec in cur.fetchall() or ():
                row = dict(zip(cols, rec, strict=True))
                rows.setdefault(row["document_id"], row)
    return rows


def build_search_event(row: dict[str, Any]) -> dict[str, Any]:
    """Build an ``IndexingConsumer`` event from a ``RULING_SEARCH_SQL`` row.

    ``hearing_date`` becomes a ``YYYY-MM-DD`` string.  A ruling with no
    stored summary gets the first 500 characters of its ruling text, as the
    worker has always done.
    """
    event = dict(row)
    event["hearing_date"] = normalize_hearing_date(row.get("hearing_date"))
    ruling_text = row.get("ruling_text") or ""
    if not event.get("summary"):
        event["summary"] = ruling_text[:SUMMARY_FALLBACK_CHARS] if ruling_text else None
    return event


def load_search_events(conn: Any, document_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Return ``{document_id: search event}`` built from the committed rows."""
    return {
        doc_id: build_search_event(row)
        for doc_id, row in fetch_ruling_search_rows(conn, document_ids).items()
    }
