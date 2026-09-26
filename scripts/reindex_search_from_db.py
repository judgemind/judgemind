#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit and repair the OpenSearch ``tentative_rulings`` index from ``derived.*``.

OpenSearch is fully derivable from ``derived.*`` (see
``docs/specs/architecture-spec-v1.md``).  This script compares every search
document against the Postgres ruling it represents and, with ``--apply``,
re-indexes the documents that have drifted.  It never re-runs the ingestion
pipeline (no S3 parse, no LLM), so it is cheap enough to run county-wide or
index-wide.  See #4712.

Drift classes it detects:

* ``hearing_date`` stored in a non ``YYYY-MM-DD`` shape (e.g. a datetime
  ``2026-07-28T00:00:00``) or null while Postgres has a date — the web
  search card renders these as "Date unknown".
* ``hearing_date`` / ``case_title`` / ``case_number`` differing from
  ``derived.rulings`` + ``derived.cases`` (stale after a case relink or a
  preserve-first title upsert).
* Postgres rulings with no search document (missing).
* Search documents with no Postgres ruling (orphans; deleted only with
  ``--delete-orphans``).

Usage (ECS, against dev — the DB and OpenSearch are VPC-only):
    scripts/ecs-run-task.sh scripts/reindex_search_from_db.py
    scripts/ecs-run-task.sh scripts/reindex_search_from_db.py -- --apply
    scripts/ecs-run-task.sh scripts/reindex_search_from_db.py -- --apply --county "Los Angeles"
    scripts/ecs-run-task.sh scripts/reindex_search_from_db.py -- --apply --all

Without ``--apply`` the script only reports (audit mode).  The report lists
the hearing_date shape distribution and per-class drift counts plus a few
sample document ids per class.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from typing import Any

INDEX_ALIAS = "tentative_rulings"
PG_BATCH = 1000
INDEX_BATCH = 200
SAMPLES_PER_CLASS = 10

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]")

# derived.* row → search-document shape.  Mirrors the fields the ingestion
# worker passes to ``IndexingConsumer.index_document``.
_RULINGS_SQL = """
    SELECT
        d.id::text              AS document_id,
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
"""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in scripts/tests/test_reindex_search_from_db.py)
# ---------------------------------------------------------------------------


def classify_hearing_date(value: Any) -> str:
    """Return ``date_only`` / ``datetime`` / ``null`` / ``other`` for a stored value."""
    if value is None or value == "":
        return "null"
    if not isinstance(value, str):
        return "other"
    if _DATE_ONLY_RE.match(value):
        return "date_only"
    if _DATE_PREFIX_RE.match(value):
        return "datetime"
    return "other"


def date_part(value: Any) -> str | None:
    """Return the ``YYYY-MM-DD`` calendar date carried by *value*, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        if _DATE_ONLY_RE.match(value):
            return value
        m = _DATE_PREFIX_RE.match(value)
        if m:
            return m.group(1)
    return None


def drift_classes(
    os_src: dict[str, Any] | None, pg_row: dict[str, Any] | None
) -> list[str]:
    """Return the drift classes between a search document and its PG ruling.

    An empty list means the document is in sync.
    """
    if os_src is None and pg_row is None:
        return []
    if os_src is None:
        return ["missing_in_os"]
    if pg_row is None:
        return ["orphan_in_os"]

    classes: list[str] = []
    shape = classify_hearing_date(os_src.get("hearing_date"))
    if shape not in ("date_only", "null"):
        classes.append(f"hearing_date_shape_{shape}")
    pg_date = date_part(pg_row.get("hearing_date"))
    if date_part(os_src.get("hearing_date")) != pg_date:
        classes.append("hearing_date_mismatch")
    if (os_src.get("case_title") or None) != (pg_row.get("case_title") or None):
        classes.append("case_title_mismatch")
    if (os_src.get("case_number") or None) != (pg_row.get("case_number") or None):
        classes.append("case_number_mismatch")
    return classes


def build_event(pg_row: dict[str, Any]) -> dict[str, Any]:
    """Build an ``IndexingConsumer`` event from a ``_RULINGS_SQL`` row."""
    event = dict(pg_row)
    event["hearing_date"] = date_part(pg_row.get("hearing_date"))
    ruling_text = pg_row.get("ruling_text") or ""
    if not event.get("summary"):
        event["summary"] = ruling_text[:500] if ruling_text else None
    return event


def chunked(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def scan_os_docs(os_client: Any, county: str | None) -> dict[str, dict[str, Any]]:
    """Return ``{_id: _source}`` for every search doc (optionally one county)."""
    from opensearchpy import helpers

    query: dict[str, Any] = {"match_all": {}}
    if county:
        query = {"term": {"county": county}}
    docs: dict[str, dict[str, Any]] = {}
    for hit in helpers.scan(
        os_client,
        index=INDEX_ALIAS,
        query={"query": query},
        _source=["hearing_date", "case_title", "case_number", "county", "document_id"],
        size=1000,
    ):
        docs[hit["_id"]] = hit.get("_source") or {}
    return docs


def fetch_pg_rows(conn: Any, document_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return ``{document_id: row}`` for the given ids (first ruling wins)."""
    rows: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        for batch in chunked(document_ids, PG_BATCH):
            cur.execute(_RULINGS_SQL + " WHERE d.id::text = ANY(%s)", (batch,))
            cols = [c.name for c in cur.description]
            for rec in cur.fetchall():
                row = dict(zip(cols, rec, strict=True))
                rows.setdefault(row["document_id"], row)
    return rows


def fetch_pg_document_ids(conn: Any, county: str | None) -> list[str]:
    sql = (
        "SELECT DISTINCT r.document_id::text FROM derived.rulings r"
        " JOIN derived.courts co ON co.id = r.court_id"
    )
    params: tuple[Any, ...] = ()
    if county:
        sql += " WHERE co.county = %s"
        params = (county,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--county", help="Limit to one county (e.g. 'Los Angeles').")
    parser.add_argument(
        "--apply", action="store_true", help="Re-index drifted documents."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="With --apply, re-index every PG ruling, not just drifted ones.",
    )
    parser.add_argument(
        "--delete-orphans",
        action="store_true",
        help="With --apply, delete search docs that have no PG ruling.",
    )
    parser.add_argument(
        "--document-id", action="append", default=[], help="Print one doc."
    )
    args = parser.parse_args(argv)

    import psycopg
    from framework.opensearch_client import make_opensearch_client

    database_url = os.environ.get("DATABASE_URL")
    os_url = os.environ.get("OPENSEARCH_URL")
    if not database_url or not os_url:
        print("DATABASE_URL and OPENSEARCH_URL must be set", file=sys.stderr)
        return 2

    os_client = make_opensearch_client(os_url)
    conn = psycopg.connect(database_url, autocommit=True)

    os_docs = scan_os_docs(os_client, args.county)
    pg_ids = fetch_pg_document_ids(conn, args.county)
    all_ids = sorted(set(os_docs) | set(pg_ids))
    pg_rows = fetch_pg_rows(conn, all_ids)

    shapes = Counter(
        classify_hearing_date(src.get("hearing_date")) for src in os_docs.values()
    )
    drift_counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    drifted: list[str] = []
    orphans: list[str] = []
    for doc_id in all_ids:
        classes = drift_classes(os_docs.get(doc_id), pg_rows.get(doc_id))
        if not classes:
            continue
        if classes == ["orphan_in_os"]:
            orphans.append(doc_id)
        else:
            drifted.append(doc_id)
        for cls in classes:
            drift_counts[cls] += 1
            bucket = samples.setdefault(cls, [])
            if len(bucket) < SAMPLES_PER_CLASS:
                bucket.append(doc_id)

    report = {
        "county": args.county,
        "os_docs": len(os_docs),
        "pg_rulings": len(pg_ids),
        "hearing_date_shape": dict(shapes),
        "drift_counts": dict(drift_counts),
        "drifted_docs": len(drifted),
        "samples": samples,
    }
    for doc_id in args.document_id:
        report.setdefault("documents", {})[doc_id] = {
            "os": os_docs.get(doc_id),
            "pg": {
                k: str(v) if v is not None else None
                for k, v in (pg_rows.get(doc_id) or {}).items()
                if k not in ("ruling_text", "summary")
            },
        }
    print("AUDIT " + json.dumps(report, default=str, sort_keys=True))

    if not args.apply:
        return 0

    from framework.search.indexer import IndexingConsumer

    consumer = IndexingConsumer(
        opensearch_client=os_client, s3_client=None, bucket="", ensure_index=False
    )
    target_ids = [d for d in all_ids if d in pg_rows] if args.all else drifted
    indexed = 0
    for batch in chunked(target_ids, INDEX_BATCH):
        events = [build_event(pg_rows[d]) for d in batch if d in pg_rows]
        indexed += consumer.index_batch(events, force=True)
    deleted = consumer.delete_documents(orphans) if args.delete_orphans else 0
    print(
        "APPLY "
        + json.dumps(
            {
                "reindexed": indexed,
                "targets": len(target_ids),
                "orphans_deleted": deleted,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
