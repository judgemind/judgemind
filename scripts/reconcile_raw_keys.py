#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Reconcile archived raw keys against the database (#5014, start of #5015).

Every content-addressed raw key in the archive (``<state>/<county>/<court>/
raw/<sha256>.<ext>``) should either have a ``derived.documents`` row or a
recorded reason it has none.  Ingestion has lost events twice (the May–June
and October 2026 ElastiCache windows, #4470 / #4474) and neither loss was
caught automatically.  This script lists the raw keys and sorts each one into
the first bucket that explains it:

- ``in_documents``      a ``derived.documents`` row (active or superseded)
                        has this ``s3_key``.  ``s3_key`` is authoritative for
                        content-addressed keys; ``content_hash`` is not (split
                        children carry other hashes, #4988).
- ``validation_only``   no document row, but ``telemetry.validation_results``
                        has the key (a validation-fail drop).
- ``dq_only``           no document row, but a ``telemetry.data_quality_metrics``
                        row's ``metadata.s3_key`` is the key (all-null guard).
- ``skip_governor``     under ``ca/statewide/governor/``: judge-bio press
                        releases the worker routes away from ruling
                        extraction on purpose (#3688, #5016).
- ``skip_sc_placeholder`` (with ``--classify-placeholders``) a Santa Clara PDF
                        whose text layer is the court's "tentative rulings are
                        not available" notice, which the worker skips (#4725).
- ``unexplained``       none of the above: a capture that never reached the
                        database.  These are the keys to reingest.

Keys under ``raw/`` in the legacy dated layout (``raw/YYYY/MM/DD/``) are
counted as ``dated`` and not reconciled here; ``audit_dated_raw_keys.py``
handles them (#4988).  Any other ``raw/`` key is counted as ``other_layout``.

The capture day of a key is its S3 ``LastModified`` (UTC).  ``--since`` and
``--until`` (inclusive, ``YYYY-MM-DD``) restrict the reconciliation to keys
captured in that window.

Output: one ``SUMMARY <county>: {json}`` line per county (bucket counts plus
``unexplained_by_month``), ``UNEXPLAINED_DAYS <county>: {json}``, and
``SUMMARY_ALL: {json}``.  ``--write-keys s3://bucket/tmp/keys/<stem>`` writes
the unexplained keys per county to ``<stem>-<county>.txt`` (or
``<stem>-<county>-NN.txt`` chunks of ``--chunk-size`` keys) for
``reingest_from_s3.py --prefix ca/<county>/ --s3-key-list <uri>``.  The
script reads only; it never writes to the database or to a raw object.

Usage (dev DB is private, so run it on ECS)::

    scripts/ecs-run-task.sh --detach scripts/reconcile_raw_keys.py -- \\
        --since 2026-05-06 --until 2026-06-19 \\
        --write-keys s3://judgemind-document-archive-dev/tmp/keys/5014-unexplained \\
        --chunk-size 1500
    scripts/ecs-wait-task.sh

Options: ``--prefix P`` (default ``ca/``), ``--county NAME`` (S3 county
segment, e.g. ``los_angeles``; repeatable), ``--since``, ``--until``,
``--classify-placeholders``, ``--write-keys URI``, ``--chunk-size N``,
``--sample N`` (unexplained keys printed per county, default 5).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

# The scraper-framework source: repo checkout locally, /app in the ECS image.
for _path in (
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "packages",
        "scraper-framework",
        "src",
    ),
    "/app/packages/scraper-framework/src",
    "/app/src",
    "/app",
):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

BUCKET = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev")

CONTENT_KEY_RE = re.compile(
    r"^(?P<state>[^/]+)/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/"
    r"(?P<hash>[0-9a-f]{64})\.(?P<ext>\w+)$"
)
DATED_KEY_RE = re.compile(r"^[^/]+/[^/]+/[^/]+/raw/\d{4}/\d{2}/\d{2}/[^/]+$")
RAW_SEGMENT_RE = re.compile(r"^[^/]+/[^/]+/[^/]+/raw/")

#: Prefixes whose raws are never meant to produce a document row.
INTENTIONAL_SKIP_PREFIXES: dict[str, str] = {
    "ca/statewide/governor/": "skip_governor",
}

BUCKETS = (
    "in_documents",
    "validation_only",
    "dq_only",
    "skip_governor",
    "skip_sc_placeholder",
    "unexplained",
)


@dataclass(frozen=True)
class RawKey:
    key: str
    county: str
    last_modified: datetime

    @property
    def day(self) -> str:
        lm = self.last_modified
        lm = lm if lm.tzinfo else lm.replace(tzinfo=UTC)
        return lm.astimezone(UTC).date().isoformat()


@dataclass
class Explained:
    """The database's view of which raw keys are accounted for."""

    documents: set[str] = field(default_factory=set)
    validation: set[str] = field(default_factory=set)
    dq_metrics: set[str] = field(default_factory=set)


@dataclass
class CountyReport:
    county: str
    counts: Counter[str] = field(default_factory=Counter)
    unexplained: list[RawKey] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        by_month: Counter[str] = Counter(k.day[:7] for k in self.unexplained)
        out: dict[str, Any] = {b: self.counts.get(b, 0) for b in BUCKETS}
        out["raw"] = sum(self.counts.get(b, 0) for b in BUCKETS)
        out["unexplained_by_month"] = dict(sorted(by_month.items()))
        return out


def list_raw_objects(
    s3: Any, bucket: str, prefix: str
) -> Iterable[tuple[str, datetime]]:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"], obj["LastModified"]


def partition_keys(
    objects: Iterable[tuple[str, datetime]],
    *,
    since: date | None = None,
    until: date | None = None,
    counties: set[str] | None = None,
) -> tuple[list[RawKey], Counter[str]]:
    """Content-addressed raw keys in the window, and counts of the rest."""
    keys: list[RawKey] = []
    other: Counter[str] = Counter()
    for key, last_modified in objects:
        if not RAW_SEGMENT_RE.match(key):
            other["not_raw"] += 1
            continue
        m = CONTENT_KEY_RE.match(key)
        if not m:
            other["dated" if DATED_KEY_RE.match(key) else "other_layout"] += 1
            continue
        if counties and m["county"] not in counties:
            continue
        raw = RawKey(key=key, county=m["county"], last_modified=last_modified)
        day = date.fromisoformat(raw.day)
        if (since and day < since) or (until and day > until):
            other["outside_window"] += 1
            continue
        keys.append(raw)
    return keys, other


def classify(key: str, explained: Explained) -> str:
    if key in explained.documents:
        return "in_documents"
    if key in explained.validation:
        return "validation_only"
    if key in explained.dq_metrics:
        return "dq_only"
    for prefix, bucket in INTENTIONAL_SKIP_PREFIXES.items():
        if key.startswith(prefix):
            return bucket
    return "unexplained"


def is_sc_placeholder_pdf(body: bytes) -> bool:
    """The worker's own Santa Clara placeholder test on the PDF's text layer."""
    from courts.ca.sc_tentatives import is_placeholder_notice
    from ingestion.llm_extract import extract_text_layer

    return is_placeholder_notice(extract_text_layer(body))


def chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0 or len(items) <= size:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def key_list_uris(stem: str, county: str, n_chunks: int) -> list[str]:
    if n_chunks == 1:
        return [f"{stem}-{county}.txt"]
    return [f"{stem}-{county}-{i:02d}.txt" for i in range(1, n_chunks + 1)]


def write_text(s3: Any, uri: str, text: str) -> None:
    if not uri.startswith("s3://"):
        with open(uri, "w") as fh:
            fh.write(text)
        return
    bucket, _, key = uri[len("s3://") :].partition("/")
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode())


def fetch_explained(database_url: str, prefix: str) -> Explained:
    import psycopg

    like = prefix.replace("%", r"\%").replace("_", r"\_") + "%"
    explained = Explained()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT s3_key FROM derived.documents WHERE s3_key LIKE %s",
            (like,),
        )
        explained.documents = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT DISTINCT s3_key FROM telemetry.validation_results "
            "WHERE s3_key LIKE %s",
            (like,),
        )
        explained.validation = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT DISTINCT metadata->>'s3_key' FROM telemetry.data_quality_metrics "
            "WHERE metadata->>'s3_key' LIKE %s",
            (like,),
        )
        explained.dq_metrics = {row[0] for row in cur.fetchall()}
    return explained


def run(
    args: argparse.Namespace,
    s3: Any,
    explained: Explained,
    out: Callable[[str], None] = print,
) -> dict[str, CountyReport]:
    keys, other = partition_keys(
        list_raw_objects(s3, args.bucket, args.prefix),
        since=args.since,
        until=args.until,
        counties=set(args.county) or None,
    )
    out(
        f"LISTED {len(keys)} content-addressed raw keys under {args.prefix} "
        f"(since={args.since} until={args.until}); other: "
        f"{json.dumps(dict(sorted(other.items())))}"
    )
    out(
        f"DB documents={len(explained.documents)} "
        f"validation={len(explained.validation)} dq_metrics={len(explained.dq_metrics)}"
    )

    reports: dict[str, CountyReport] = {}
    for raw in keys:
        rep = reports.setdefault(raw.county, CountyReport(raw.county))
        bucket = classify(raw.key, explained)
        if (
            bucket == "unexplained"
            and args.classify_placeholders
            and raw.county == "santa_clara"
            and raw.key.endswith(".pdf")
        ):
            try:
                body = s3.get_object(Bucket=args.bucket, Key=raw.key)["Body"].read()
                if is_sc_placeholder_pdf(body):
                    bucket = "skip_sc_placeholder"
            except Exception as exc:  # noqa: BLE001 — leave it unexplained
                out(f"PLACEHOLDER_CHECK_ERROR key={raw.key} error={exc}")
        rep.counts[bucket] += 1
        if bucket == "unexplained":
            rep.unexplained.append(raw)

    total: Counter[str] = Counter()
    for county in sorted(reports):
        rep = reports[county]
        rep.unexplained.sort(key=lambda k: (k.last_modified, k.key))
        summary = rep.summary()
        out(f"SUMMARY {county}: {json.dumps(summary, sort_keys=True)}")
        days = Counter(k.day for k in rep.unexplained)
        out(f"UNEXPLAINED_DAYS {county}: {json.dumps(dict(sorted(days.items())))}")
        for raw in rep.unexplained[: args.sample]:
            out(f"UNEXPLAINED {county} day={raw.day} key={raw.key}")
        for b in (*BUCKETS, "raw"):
            total[b] += summary[b]

        if args.write_keys and rep.unexplained:
            chunks = chunked([k.key for k in rep.unexplained], args.chunk_size)
            for uri, chunk in zip(
                key_list_uris(args.write_keys, county, len(chunks)), chunks, strict=True
            ):
                write_text(s3, uri, "".join(f"{k}\n" for k in chunk))
                out(f"WROTE_KEYS {county} {len(chunk)} -> {uri}")

    out(f"SUMMARY_ALL: {json.dumps(dict(total), sort_keys=True)}")
    return reports


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--prefix", default="ca/")
    parser.add_argument(
        "--county",
        action="append",
        default=[],
        help="S3 county segment to reconcile, e.g. los_angeles (repeatable).",
    )
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument("--until", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--classify-placeholders",
        action="store_true",
        help="Fetch unexplained Santa Clara PDFs and bucket placeholder notices.",
    )
    parser.add_argument(
        "--write-keys",
        metavar="URI_STEM",
        help="Write unexplained keys to <stem>-<county>[-NN].txt (s3:// or local).",
    )
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--sample", type=int, default=5)
    args = parser.parse_args(argv)
    if args.since and args.until and args.since > args.until:
        parser.error("--since is after --until")
    if not args.prefix.endswith("/"):
        parser.error("--prefix must end with /")
    return args


def main(argv: list[str] | None = None) -> int:
    import boto3

    args = parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    s3 = boto3.client("s3")
    explained = fetch_explained(database_url, args.prefix)
    run(args, s3, explained, out=lambda line: print(line, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
