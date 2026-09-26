#!/usr/bin/env python3
"""Audit split-child ids on pre-split counties (Fresno, Contra Costa, LA) — #4796.

Read-only.  For every Fresno S3 key (``ca/fresno/superior_court/raw/<sha>.pdf``)
the script downloads the PDF, runs the deterministic Fresno split exactly as
the ingestion worker does, and compares the expected ruling ids with the
``derived.documents`` rows on that key:

- ``expected``    — ruling count from the deterministic split (1 when the PDF
  does not split; the worker then keeps the parent id).
- ``positional``  — rows with the canonical id ``make_split(P, 0..N-1)``.
- ``entry``       — rows with the old live id ``make_split(P, <entry number>)``
  that are not also a positional id.
- ``missing``     — rulings covered by neither a positional nor an entry row.
- ``other``       — rows on the key that are neither (grandchildren, ids from
  an earlier split, or a parent row in a multi split).

A key needs repair when it is not exactly the positional set.  The affected
keys are printed between ``AFFECTED_KEYS_BEGIN`` / ``AFFECTED_KEYS_END`` for
``reingest_from_s3.py --prefix-key-list``.

Contra Costa and Los Angeles have no deterministic raw split to compare with,
so for them the script reports keys whose rows are not a clean
``{P}`` / ``make_split(P, 0..N-1)`` set.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/audit_presplit_split_ids_4796.py
"""

# venv: scraper-framework
# one-off: true

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3
import psycopg
from courts.ca.fresno_tentatives import _split_rulings
from ingestion.llm_extract import extract_text_from_pdf
from ingestion.split_ids import (
    derive_parent_document_id,
    make_split_document_id,
)

_MAX_SLOT = 400


def _rows_by_key(
    conn: psycopg.Connection, prefix: str
) -> dict[str, list[tuple[str, bool]]]:
    rows: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.s3_key, d.id::text,
                   EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = d.id)
              FROM documents d
             WHERE d.s3_key LIKE %s
            """,
            (prefix + "%",),
        )
        for key, doc_id, has_ruling in cur.fetchall():
            rows[key].append((doc_id, has_ruling))
    return rows


def _key_hash(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
        return stem
    return None


def _slot_map(parent: str) -> dict[str, int]:
    return {make_split_document_id(parent, i): i for i in range(_MAX_SLOT)}


def audit_fresno(conn: psycopg.Connection, s3, bucket: str) -> list[str]:
    prefix = "ca/fresno/superior_court/raw/"
    rows = _rows_by_key(conn, prefix)
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".pdf") and _key_hash(key):
                keys.append(key)

    totals: Counter[str] = Counter()
    affected: list[str] = []
    for key in sorted(keys):
        content_hash = _key_hash(key)
        parent = derive_parent_document_id(content_hash)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        text = extract_text_from_pdf(body) or ""
        rulings = _split_rulings(text.replace("\n\f\n", "\n"))
        n = len(rulings)
        present = {doc_id for doc_id, _ in rows.get(key, [])}
        with_ruling = sum(1 for _, has in rows.get(key, []) if has)

        if n <= 1:
            expected_ids = [parent]
            entry_ids: list[str] = []
        else:
            expected_ids = [make_split_document_id(parent, i) for i in range(n)]
            entry_ids = [
                make_split_document_id(parent, r.ruling_index) for r in rulings
            ]

        positional = sum(1 for i in expected_ids if i in present)
        covered = sum(
            1
            for i, pos_id in enumerate(expected_ids)
            if pos_id in present or (entry_ids and entry_ids[i] in present)
        )
        entry_only = len((set(entry_ids) - set(expected_ids)) & present)
        other = len(present - set(expected_ids) - set(entry_ids))
        missing = len(expected_ids) - covered
        clean = present == set(expected_ids)

        totals["keys"] += 1
        totals["expected_rulings"] += len(expected_ids)
        totals["rows"] += len(present)
        totals["rows_with_ruling"] += with_ruling
        totals["positional_rows"] += positional
        totals["entry_rows"] += entry_only
        totals["other_rows"] += other
        totals["missing_rulings"] += missing
        if missing:
            totals["keys_missing_siblings"] += 1
        if not present:
            totals["keys_with_no_rows"] += 1
        if not clean:
            totals["keys_not_clean"] += 1
            affected.append(key)
            print(
                json.dumps(
                    {
                        "key": key,
                        "expected": len(expected_ids),
                        "rows": len(present),
                        "positional": positional,
                        "entry": entry_only,
                        "other": other,
                        "missing": missing,
                    }
                )
            )

    print("FRESNO_TOTALS " + json.dumps(dict(totals), sort_keys=True))
    return affected


def audit_db_only(conn: psycopg.Connection, county: str) -> None:
    prefix = f"ca/{county}/"
    rows = _rows_by_key(conn, prefix)
    totals: Counter[str] = Counter()
    examples: list[dict] = []
    for key, docs in rows.items():
        totals["keys"] += 1
        totals["rows"] += len(docs)
        content_hash = _key_hash(key)
        if not content_hash or len(docs) == 1:
            continue
        parent = derive_parent_document_id(content_hash)
        slots = _slot_map(parent)
        ids = {doc_id for doc_id, _ in docs}
        idx = sorted(slots[i] for i in ids if i in slots)
        other = [i for i in ids if i not in slots and i != parent]
        totals["multi_row_keys"] += 1
        if idx != list(range(len(idx))) or other or (parent in ids and idx):
            totals["keys_not_clean"] += 1
            totals["other_rows"] += len(other)
            if len(examples) < 10:
                examples.append(
                    {
                        "key": key,
                        "slots": idx[:20],
                        "other": len(other),
                        "parent": parent in ids,
                    }
                )
    print(f"{county.upper()}_TOTALS " + json.dumps(dict(totals), sort_keys=True))
    for ex in examples:
        print(f"{county.upper()}_EXAMPLE " + json.dumps(ex))


def main() -> None:
    bucket = os.environ.get(
        "JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev"
    )
    s3 = boto3.client("s3")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        affected = audit_fresno(conn, s3, bucket)
        audit_db_only(conn, "contra_costa")
        audit_db_only(conn, "los_angeles")
    print("AFFECTED_KEYS_BEGIN")
    for key in affected:
        print(key)
    print("AFFECTED_KEYS_END")


if __name__ == "__main__":
    main()
