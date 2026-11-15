#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit, and optionally migrate, legacy dated raw keys in the archive (#4988).

Before #2193 (2026-03-28) the archiver wrote raw captures under a dated
layout, ``<state>/<county>/<court>/raw/YYYY/MM/DD/<uuid>.<ext>``.  #2193
switched to content-addressed keys (``.../raw/<sha256>.<ext>``) and its
one-off ``migrate_s3_keys.py`` copied only the objects a ``documents`` row
pointed at.  A dated object no row referenced was never copied, and
``rebuild_db.py`` / prefix-mode ``reingest_from_s3.py`` list only
content-addressed keys, so such an object can never reach the database.

For every dated raw object this script SHA-256s the bytes and classifies it:

- ``dup_same_prefix``   ``<same prefix>/raw/<sha>.<ext>`` exists: a copy of
                        a content-addressed object, nothing is lost.
- ``dup_other_prefix``  the hash exists under another court prefix only.
- ``unique``            no content-addressed object has these bytes.

It also reports how many hashes ``derived.documents.content_hash`` holds
(when ``DATABASE_URL`` is set).  Output: one ``UNIQUE`` line per unique key,
``SAMPLE`` lines with the start of the text of ``--samples`` unique keys per
county, and ``SUMMARY <county>: {json}`` per county plus ``SUMMARY_ALL``.

``--migrate`` copies each unique object to its content-addressed key
(``<prefix>/raw/<sha>.<ext>``) with the archiver's metadata
(``content-hash``, ``capture-timestamp`` = the original capture time) and a
``migrated-from`` pointer.  It never deletes or overwrites anything: the
dated original stays, and a target that already exists is left untouched.
``--write-keys s3://bucket/key`` writes the content-addressed keys of the
unique objects, one per line, for ``reingest_from_s3.py --s3-key-list``.

Usage (run on ECS, the archive role can list/get/put)::

    scripts/ecs-run-task.sh --detach scripts/audit_dated_raw_keys.py
    scripts/ecs-run-task.sh --detach scripts/audit_dated_raw_keys.py -- \\
        --migrate --write-keys s3://judgemind-document-archive-dev/tmp/keys/dated-4988.txt
    scripts/ecs-wait-task.sh

Options: ``--prefix P`` (default ``ca/``), ``--workers N`` (default 16),
``--samples N`` (default 3), ``--migrate``, ``--write-keys URI``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

DATED_KEY_RE = re.compile(
    r"^(?P<prefix>(?P<state>[^/]+)/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/)"
    r"(?P<y>\d{4})/(?P<m>\d{2})/(?P<d>\d{2})/(?P<name>[^/]+)\.(?P<ext>\w+)$"
)
CONTENT_KEY_RE = re.compile(
    r"^(?P<prefix>[^/]+/[^/]+/[^/]+/raw/)(?P<hash>[0-9a-f]{64})\.(?P<ext>\w+)$"
)

# ``framework.storage.CAPTURE_TIMESTAMP_METADATA_KEY``: the archiver's stamp
# that rebuild/reingest read back as the capture time (#4661).
CAPTURE_TIMESTAMP_METADATA_KEY = "capture-timestamp"

# A dated object rewritten long after its path date (a copy, a restore) has
# a LastModified that is not its capture time; its path date is.
_REWRITE_SLACK = timedelta(days=2)


@dataclass(frozen=True)
class DatedObject:
    key: str
    sha256: str
    size: int
    last_modified: datetime | None
    metadata: dict[str, str]
    content_type: str | None


@dataclass
class CountyReport:
    county: str
    counts: Counter[str] = field(default_factory=Counter)
    in_db: int = 0
    unique_keys: list[DatedObject] = field(default_factory=list)
    path_dates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        unique_dates = sorted(path_date(o.key) for o in self.unique_keys)
        all_dates = sorted(self.path_dates)
        return {
            "dated": sum(self.counts.values()) + len(self.errors),
            "dup_same_prefix": self.counts.get("dup_same_prefix", 0),
            "dup_other_prefix": self.counts.get("dup_other_prefix", 0),
            "unique": self.counts.get("unique", 0),
            "error": len(self.errors),
            "hash_in_db": self.in_db,
            "date_range": [all_dates[0], all_dates[-1]] if all_dates else None,
            "unique_date_range": (
                [unique_dates[0], unique_dates[-1]] if unique_dates else None
            ),
            "unique_bytes": sum(o.size for o in self.unique_keys),
        }


def path_date(key: str) -> str:
    m = DATED_KEY_RE.match(key)
    if not m:
        raise ValueError(f"not a dated raw key: {key}")
    return f"{m['y']}-{m['m']}-{m['d']}"


def content_key_for(dated_key: str, sha256: str) -> str:
    """The content-addressed key a dated object's bytes belong at."""
    m = DATED_KEY_RE.match(dated_key)
    if not m:
        raise ValueError(f"not a dated raw key: {dated_key}")
    return f"{m['prefix']}{sha256}.{m['ext'].lower()}"


def split_keys(keys: Iterable[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Dated raw keys, and content-addressed keys indexed by hash."""
    dated: list[str] = []
    by_hash: dict[str, list[str]] = defaultdict(list)
    for key in keys:
        if DATED_KEY_RE.match(key):
            dated.append(key)
            continue
        m = CONTENT_KEY_RE.match(key)
        if m:
            by_hash[m["hash"]].append(key)
    return dated, by_hash


def classify(obj: DatedObject, by_hash: dict[str, list[str]]) -> str:
    matches = by_hash.get(obj.sha256, [])
    prefix = DATED_KEY_RE.match(obj.key)["prefix"]  # type: ignore[index]
    if any(k.startswith(prefix) for k in matches):
        return "dup_same_prefix"
    if matches:
        return "dup_other_prefix"
    return "unique"


def capture_timestamp(obj: DatedObject) -> str:
    """The object's original capture time, ISO-8601 (see ``framework.storage``).

    Metadata first, then LastModified, except that a LastModified more than
    ``_REWRITE_SLACK`` after the path date means the object was rewritten, so
    the path date (UTC midnight) is the better capture time.
    """
    stamped = obj.metadata.get(CAPTURE_TIMESTAMP_METADATA_KEY)
    if stamped:
        return stamped
    day = datetime.strptime(path_date(obj.key), "%Y-%m-%d").replace(tzinfo=UTC)
    lm = obj.last_modified
    if lm is not None:
        lm = lm if lm.tzinfo else lm.replace(tzinfo=UTC)
        if lm - day <= _REWRITE_SLACK + timedelta(days=1):
            return lm.isoformat()
    return day.isoformat()


def migration_metadata(obj: DatedObject) -> dict[str, str]:
    return {
        **obj.metadata,
        "content-hash": obj.sha256,
        CAPTURE_TIMESTAMP_METADATA_KEY: capture_timestamp(obj),
        "migrated-from": obj.key,
    }


def migrate_one(s3: Any, bucket: str, obj: DatedObject) -> str:
    """Copy one unique dated object to its content-addressed key.

    Returns ``copied`` or ``exists``.  Never overwrites: an existing target
    is left alone.  Raises when the copy's size does not match.
    """
    target = content_key_for(obj.key, obj.sha256)
    try:
        s3.head_object(Bucket=bucket, Key=target)
        return "exists"
    except Exception as exc:  # botocore ClientError; only "not found" proceeds
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
        if code not in ("404", "NoSuchKey"):
            raise
    kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Key": target,
        "CopySource": {"Bucket": bucket, "Key": obj.key},
        "MetadataDirective": "REPLACE",
        "Metadata": migration_metadata(obj),
    }
    if obj.content_type:
        kwargs["ContentType"] = obj.content_type
    s3.copy_object(**kwargs)
    head = s3.head_object(Bucket=bucket, Key=target)
    if int(head.get("ContentLength", -1)) != obj.size:
        raise RuntimeError(
            f"copy size mismatch for {target}: {head.get('ContentLength')} != {obj.size}"
        )
    return "copied"


def fetch_dated(s3: Any, bucket: str, key: str) -> DatedObject:
    resp = s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()
    return DatedObject(
        key=key,
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        last_modified=resp.get("LastModified"),
        metadata=dict(resp.get("Metadata") or {}),
        content_type=resp.get("ContentType"),
    )


def sample_text(body: bytes, key: str, limit: int = 300) -> str:
    text = ""
    if key.lower().endswith(".pdf"):
        try:
            from ingestion.llm_extract import extract_text_from_pdf

            text = extract_text_from_pdf(body) or ""
        except Exception as exc:  # noqa: BLE001 — a sample is best-effort
            text = f"<extract failed: {type(exc).__name__}>"
    else:
        text = re.sub(r"<[^>]+>", " ", body.decode("utf-8", errors="replace"))
    return " ".join(text.split())[:limit]


def list_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]


def fetch_db_hashes(database_url: str) -> set[str]:
    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT content_hash FROM derived.documents "
            "WHERE content_hash IS NOT NULL"
        )
        return {row[0] for row in cur.fetchall()}


def write_s3_uri(s3: Any, uri: str, text: str) -> None:
    if not uri.startswith("s3://"):
        with open(uri, "w") as fh:
            fh.write(text)
        return
    bucket, _, key = uri[len("s3://") :].partition("/")
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode())


def run(
    args: argparse.Namespace,
    s3: Any,
    db_hashes: set[str] | None,
    out: Callable[[str], None] = print,
) -> dict[str, CountyReport]:
    keys = list_keys(s3, args.bucket, args.prefix)
    dated, by_hash = split_keys(keys)
    out(
        f"LISTED {len(keys)} keys under {args.prefix}: {len(dated)} dated, "
        f"{sum(len(v) for v in by_hash.values())} content-addressed"
    )

    reports: dict[str, CountyReport] = {}

    def report_for(key: str) -> CountyReport:
        county = DATED_KEY_RE.match(key)["county"]  # type: ignore[index]
        return reports.setdefault(county, CountyReport(county))

    def work(key: str) -> tuple[str, DatedObject | None, str | None]:
        try:
            return key, fetch_dated(s3, args.bucket, key), None
        except Exception as exc:  # noqa: BLE001 — report and continue
            return key, None, f"{type(exc).__name__}: {exc}"

    unique: list[DatedObject] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for done, (key, obj, err) in enumerate(pool.map(work, dated), start=1):
            rep = report_for(key)
            rep.path_dates.append(path_date(key))
            if obj is None:
                rep.errors.append(key)
                out(f"ERROR key={key} error={err}")
                continue
            verdict = classify(obj, by_hash)
            rep.counts[verdict] += 1
            if db_hashes is not None and obj.sha256 in db_hashes:
                rep.in_db += 1
            if verdict == "unique":
                rep.unique_keys.append(obj)
                unique.append(obj)
                out(
                    f"UNIQUE key={key} sha256={obj.sha256} bytes={obj.size} "
                    f"in_db={db_hashes is not None and obj.sha256 in db_hashes}"
                )
            if done % 500 == 0:
                out(f"PROGRESS {done}/{len(dated)}")

    for county in sorted(reports):
        for obj in reports[county].unique_keys[: args.samples]:
            body = s3.get_object(Bucket=args.bucket, Key=obj.key)["Body"].read()
            out(f"SAMPLE {county} key={obj.key} text={sample_text(body, obj.key)!r}")

    # Several dated keys can hold the same bytes: one target each.
    targets: dict[str, DatedObject] = {}
    for obj in unique:
        targets.setdefault(content_key_for(obj.key, obj.sha256), obj)

    if args.migrate:
        results: Counter[str] = Counter()
        for target, obj in sorted(targets.items()):
            try:
                results[migrate_one(s3, args.bucket, obj)] += 1
            except Exception as exc:  # noqa: BLE001 — report and continue
                results["error"] += 1
                out(f"MIGRATE_ERROR key={obj.key} target={target} error={exc}")
        out(f"MIGRATED {json.dumps(dict(results), sort_keys=True)}")

    if args.write_keys:
        write_s3_uri(s3, args.write_keys, "".join(f"{t}\n" for t in sorted(targets)))
        out(f"WROTE_KEYS {len(targets)} -> {args.write_keys}")

    total: Counter[str] = Counter()
    for county in sorted(reports):
        summary = reports[county].summary()
        out(f"SUMMARY {county}: {json.dumps(summary, sort_keys=True)}")
        for k in ("dated", "dup_same_prefix", "dup_other_prefix", "unique", "error"):
            total[k] += summary[k]
    out(f"SUMMARY_ALL: {json.dumps(dict(total), sort_keys=True)}")
    return reports


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--prefix", default="ca/")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--write-keys", metavar="URI")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import boto3

    args = parse_args(argv)
    s3 = boto3.client("s3")
    database_url = os.environ.get("DATABASE_URL")
    db_hashes = fetch_db_hashes(database_url) if database_url else None
    run(args, s3, db_hashes, out=lambda line: print(line, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
