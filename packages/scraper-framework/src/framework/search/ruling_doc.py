"""Build search documents from the committed ``derived.*`` ruling row.

OpenSearch is fully derivable from ``derived.*``.  Every writer of the
``tentative_rulings`` index builds its documents here, from the row Postgres
actually kept — never from raw scraper or extraction values:

* the ingestion worker, right after it commits a ruling;
* ``reindex_dependent_rulings``, after a write changed a case, judge or
  court field that other rulings' docs show (#4920, #4935);
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
from dataclasses import dataclass, field
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


@dataclass
class ShownRowChanges:
    """The ids of joined rows whose shown fields a write changed.

    A search doc shows fields of rows other rulings share (see
    ``RULING_SEARCH_SQL``): the case's ``case_title`` / ``case_type`` /
    ``case_number``, the judge's ``canonical_name``, and the court's
    ``court_name`` / ``county`` / ``state``.  A write that changes one of
    them makes every search doc of that row's rulings stale, not only the
    ruling being written.  Each such write adds the row's id here while the
    transaction runs, and the writer passes the whole set to
    :func:`reindex_dependent_rulings` once it commits (#4920, #4935).

    The writers that record into it: ``ingestion.db.upsert_case`` /
    ``upsert_case_returning_title`` (case title or type), ``resolve_judge``
    (judge rename) and ``upsert_court`` (court name).
    """

    case_ids: set[str] = field(default_factory=set)
    judge_ids: set[str] = field(default_factory=set)
    court_ids: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.case_ids or self.judge_ids or self.court_ids)

    def update(self, other: ShownRowChanges) -> None:
        """Add *other*'s ids to this set."""
        self.case_ids |= other.case_ids
        self.judge_ids |= other.judge_ids
        self.court_ids |= other.court_ids

    def summary(self) -> dict[str, list[str]]:
        """The ids, sorted, for a log line."""
        return {
            "case_ids": sorted(self.case_ids),
            "judge_ids": sorted(self.judge_ids),
            "court_ids": sorted(self.court_ids),
        }


DEPENDENT_RULING_DOCUMENTS_SQL = """
    SELECT DISTINCT r.document_id::text
    FROM derived.rulings r
    WHERE r.case_id = ANY(%s::uuid[])
       OR r.judge_id = ANY(%s::uuid[])
       OR r.court_id = ANY(%s::uuid[])
    ORDER BY 1
"""

REINDEX_BATCH = 200


def reindex_dependent_rulings(
    conn: Any,
    indexer: Any,
    changes: ShownRowChanges,
    *,
    skip_document_ids: Iterable[str] = (),
    batch_size: int = REINDEX_BATCH,
) -> int:
    """Re-index every ruling that shows a row in *changes*, from its
    committed row.

    The one mechanism for a write that changes a field shown on other
    rulings' search docs: a case retitle (#4935), a judge rename (#4920), a
    court rename.  Callers run this after the transaction that made the
    change commits, so every ruling of the row committed before then is
    re-indexed here, and a ruling committed later is indexed with the new
    value by its own writer.

    *skip_document_ids* are docs the caller has just indexed from the same
    committed state (the rulings it wrote), so they are not written twice.

    The docs are written with ``force=True`` through the bulk API, one
    request per *batch_size* rulings: the change is known to alter every one
    of them, so the per-doc idempotency read would only add round trips.
    Returns the number of docs the indexer reports written.
    """
    case_ids = sorted(set(_valid_uuids(changes.case_ids)))
    judge_ids = sorted(set(_valid_uuids(changes.judge_ids)))
    court_ids = sorted(set(_valid_uuids(changes.court_ids)))
    if not (case_ids or judge_ids or court_ids):
        return 0
    with conn.cursor() as cur:
        cur.execute(DEPENDENT_RULING_DOCUMENTS_SQL, (case_ids, judge_ids, court_ids))
        skip = {str(d) for d in skip_document_ids}
        document_ids = [rec[0] for rec in cur.fetchall() or () if rec[0] not in skip]
    indexed = 0
    for start in range(0, len(document_ids), batch_size):
        events = load_search_events(conn, document_ids[start : start + batch_size])
        if events:
            indexed += indexer.index_batch(list(events.values()), force=True)
    return indexed


def reindex_dependent_rulings_at(
    dsn: str, opensearch_url: str, bucket: str, changes: ShownRowChanges
) -> int:
    """:func:`reindex_dependent_rulings` on a fresh connection and indexer.

    For the batch scripts (``scripts/reingest_from_s3.py``,
    ``scripts/rebuild_db.py``), whose court seeding and judge pre-pass
    commit outside any worker.  Returns 0 without connecting when *changes*
    is empty; raises on failure (callers log the repair command).
    """
    if not changes:
        return 0
    import psycopg

    from framework.opensearch_client import make_opensearch_client
    from framework.s3_cache import make_s3_client

    from .indexer import IndexingConsumer

    indexer = IndexingConsumer(
        make_opensearch_client(opensearch_url), make_s3_client(), bucket, ensure_index=False
    )
    with psycopg.connect(dsn) as conn:
        return reindex_dependent_rulings(conn, indexer, changes)
