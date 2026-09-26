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

The expected search doc comes from ``framework.search.ruling_doc`` — the
same builder the ingestion worker uses after it commits a ruling (#4785) —
so a doc this script repairs is exactly what the next re-ingest writes.

Drift classes it detects:

* ``hearing_date`` stored in a non ``YYYY-MM-DD`` shape (e.g. a datetime
  ``2026-07-28T00:00:00``) — the web search card renders these as
  "Date unknown".
* ``<field>_mismatch`` for every indexed metadata field (hearing_date,
  case_title, case_number, case_type, judge_name, motion_type, outcome,
  summary, ...) whose search value differs from the doc built from
  ``derived.rulings`` + ``derived.cases`` + ``derived.judges``.
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
DEFAULT_BUCKET = "judgemind-document-archive-dev"
INDEX_BATCH = 200
SAMPLES_PER_CLASS = 10

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]")


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
    os_src: dict[str, Any] | None, expected: dict[str, Any] | None
) -> list[str]:
    """Return the drift classes between a search document and its expected metadata.

    *expected* is the metadata of the doc built from the Postgres ruling
    (``expected_metadata``), or None when there is no ruling.  An empty list
    means the document is in sync.
    """
    if os_src is None and expected is None:
        return []
    if os_src is None:
        return ["missing_in_os"]
    if expected is None:
        return ["orphan_in_os"]

    classes: list[str] = []
    shape = classify_hearing_date(os_src.get("hearing_date"))
    if shape not in ("date_only", "null"):
        classes.append(f"hearing_date_shape_{shape}")
    for field, want in expected.items():
        have = os_src.get(field)
        if field == "hearing_date":
            differs = date_part(have) != date_part(want)
        else:
            differs = (have or None) != (want or None)
        if differs:
            classes.append(f"{field}_mismatch")
    return classes


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
# Shared search-doc builder (framework.search — the worker uses it too)
# ---------------------------------------------------------------------------


def load_events(conn: Any, document_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return ``{document_id: search event}`` built from the committed rows."""
    from framework.search.ruling_doc import load_search_events

    return load_search_events(conn, document_ids)


def expected_metadata(event: dict[str, Any]) -> dict[str, Any]:
    """Return the indexed metadata the search doc for *event* should carry."""
    from framework.search.indexer import search_doc_metadata

    return search_doc_metadata(event)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def scan_os_docs(os_client: Any, county: str | None) -> dict[str, dict[str, Any]]:
    """Return ``{_id: _source}`` for every search doc (optionally one county)."""
    from framework.search.indexer import INDEXED_METADATA_FIELDS
    from opensearchpy import helpers

    query: dict[str, Any] = {"match_all": {}}
    if county:
        query = {"term": {"county": county}}
    docs: dict[str, dict[str, Any]] = {}
    for hit in helpers.scan(
        os_client,
        index=INDEX_ALIAS,
        query={"query": query},
        _source=[*INDEXED_METADATA_FIELDS, "document_id"],
        size=1000,
    ):
        docs[hit["_id"]] = hit.get("_source") or {}
    return docs


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
    events = load_events(conn, all_ids)

    shapes = Counter(
        classify_hearing_date(src.get("hearing_date")) for src in os_docs.values()
    )
    drift_counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    drifted: list[str] = []
    orphans: list[str] = []
    for doc_id in all_ids:
        event = events.get(doc_id)
        expected = expected_metadata(event) if event is not None else None
        classes = drift_classes(os_docs.get(doc_id), expected)
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
                for k, v in (events.get(doc_id) or {}).items()
                if k not in ("ruling_text", "summary")
            },
        }
    print("AUDIT " + json.dumps(report, default=str, sort_keys=True))

    if not args.apply:
        return 0

    from framework.s3_cache import make_s3_client
    from framework.search.indexer import IndexingConsumer

    # Rulings with no ruling_text in derived.* (e.g. statewide governor
    # pages) fall back to the raw S3 object, as the ingestion worker does.
    consumer = IndexingConsumer(
        opensearch_client=os_client,
        s3_client=make_s3_client(),
        bucket=os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", DEFAULT_BUCKET),
        ensure_index=False,
    )
    target_ids = [d for d in all_ids if d in events] if args.all else drifted
    indexed = 0
    for batch in chunked(target_ids, INDEX_BATCH):
        batch_events = [events[d] for d in batch if d in events]
        indexed += consumer.index_batch(batch_events, force=True)
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
