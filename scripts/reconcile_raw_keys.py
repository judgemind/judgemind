#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Reconcile every archived raw key against the database (#5014, #5015).

Every content-addressed raw key in the archive (``<state>/<county>/<court>/
raw/<sha256>.<ext>``) should either be represented in ``derived.documents``
or have a recorded reason it is not.  Ingestion has lost events twice (the
May-June and October 2026 ElastiCache windows, #4470 / #4474), and neither
loss was caught automatically.  This script lists the raw keys (S3 listing
only: no object is read) and sorts each one into a category:

- ``represented``    a ``derived.documents`` row (active or superseded) has
                     this ``s3_key``.  ``s3_key`` is authoritative for
                     content-addressed keys; ``content_hash`` is not (split
                     children carry other hashes, #4988).
- ``skipped``        no document row, but a durable reason is recorded:
                     a ``telemetry.ingest_skips`` row (governor bio route,
                     Santa Clara placeholder, zero rulings with no text, no
                     extractable text, no ruling text), a failed
                     ``telemetry.validation_results`` row, or the all-NULL
                     guard's ``non_ruling_pdf_dropped`` metric.  Keys under
                     ``ca/statewide/governor/`` count as skipped too (they
                     were routed away before skip records existed, #5016).
                     A failed validation row written before the table had
                     an ``s3_key`` column (#4773, 2026-09-25) is matched by
                     the key's document id, ``uuid5(NAMESPACE_URL,
                     <sha256>)``, and counted as
                     ``validation_fail_by_document_id`` (#5023).
- ``pending``        captured within ``--grace-hours`` (default 6): the
                     worker may not have processed it yet.
- ``unrepresented``  none of the above: a capture that never reached the
                     database.  These are the keys to reingest.

Keys under ``raw/`` in the legacy dated layout (``raw/YYYY/MM/DD/``) are
counted as ``dated_excluded`` and not reconciled: #4988 copied every dated
object whose bytes had no content-addressed copy to its content-addressed
key, which this script does reconcile.  Any other ``raw/`` key is counted
as ``other_layout``.

By default every top-level prefix of the bucket is reconciled except the
non-capture ones (``tmp/``, ``llm-cache/``, ``directories/`` ...; see
``--exclude-prefix``).  The listing runs in parallel, one task per
``<state>/<county>/`` prefix.

Output: ``SUMMARY <scope>: {json}`` per ``<state>/<county>`` scope,
``UNREPRESENTED_DAYS <scope>: {json}``, ``SUMMARY_ALL: {json}`` and, last,
one ``RECONCILE_RESULT {json}`` line that the scheduled workflow parses
(``.github/workflows/raw-key-reconcile.yml``).

Modes:

- ``--record`` writes the run and its per-scope counts to
  ``telemetry.raw_reconcile_runs`` / ``raw_reconcile_counts`` (the
  ``/admin/data-quality`` panel reads them) and decides the run's action:

  - ``none``             nothing unrepresented;
  - ``over_threshold``   more than ``--max-auto-reingest`` keys (default
                         500): too many to heal blindly, a GitHub issue is
                         filed or updated instead (by the workflow);
  - ``reingest``         with ``--reingest``: the unrepresented keys are
                         written to ``s3://<bucket>/tmp/keys/`` and
                         re-ingested by ``reingest_from_s3.py --prefix
                         <state>/ --s3-key-list <uri> --concurrency 4``;
  - ``persistent``       a ``reingest`` whose previous ``--persist-runs - 1``
                         runs (default 2) also re-ingested and still found
                         keys: it re-ingests again (new losses keep
                         healing) and the issue is filed or updated, since
                         something is not healing;
  - ``reingest_failed``  the reingest could not be launched (exit 1);
  - ``reingest_deferred``  ``--defer-reingest``: another reingest is
                         running, so this run only reports;
  - ``report_only``      unrepresented keys, no ``--reingest``.

  The workflow files or updates the issue for ``over_threshold``,
  ``persistent`` and ``reingest_failed`` (``issue_needed`` in the result).
  No human alert.

- ``--write-keys URI_STEM`` writes the unrepresented keys per scope to
  ``<stem>-<state>_<county>.txt`` (or ``-NN.txt`` chunks of
  ``--chunk-size``) for a manual ``reingest_from_s3.py --s3-key-list``.

Usage (dev DB is private, so run it on ECS)::

    scripts/ecs-run-task.sh --detach scripts/reconcile_raw_keys.py -- --record
    scripts/ecs-wait-task.sh

    # the #5014 window, per county key lists:
    scripts/ecs-run-task.sh --detach scripts/reconcile_raw_keys.py -- \\
        --prefix ca/ --since 2026-05-06 --until 2026-06-19 \\
        --write-keys s3://judgemind-document-archive-dev/tmp/keys/5014-unexplained \\
        --chunk-size 1500

Options: ``--prefix P`` (default: every capture prefix), ``--county NAME``
(S3 county segment, e.g. ``los_angeles``; repeatable), ``--since``,
``--until`` (inclusive ``YYYY-MM-DD`` LastModified window),
``--grace-hours``, ``--exclude-prefix``, ``--workers``,
``--classify-placeholders`` (fetch unrepresented Santa Clara PDFs and count
"not available" notices as skipped; reads objects, so not for the daily
run), ``--write-keys``, ``--chunk-size``, ``--sample``, ``--record``,
``--reingest``, ``--defer-reingest``, ``--max-auto-reingest``,
``--persist-runs``, ``--reingest-concurrency``, ``--reingest-script``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
DATED_KEY_RE = re.compile(
    r"^(?P<state>[^/]+)/(?P<county>[^/]+)/[^/]+/raw/\d{4}/\d{2}/\d{2}/[^/]+$"
)
RAW_SEGMENT_RE = re.compile(r"^[^/]+/[^/]+/[^/]+/raw/")

#: Top-level prefixes that hold no captures (ops data, caches, key lists,
#: court directories, scratch space).
DEFAULT_EXCLUDE_PREFIXES = (
    "_ops/",
    "ops/",
    "tmp/",
    "llm-cache/",
    "keylists/",
    "scratch/",
    "spotcheck/",
    "ralph-reviews/",
    "directories/",
)

#: Prefixes whose raws are never meant to produce a document row; counted
#: as skipped even without a skip record (written before #5015).
INTENTIONAL_SKIP_PREFIXES: dict[str, str] = {
    "ca/statewide/governor/": "governor_bio_route",
}

#: ``ingestion.ruling_guards.NON_RULING_PDF_DROPPED_METRIC`` (all-NULL guard).
NON_RULING_PDF_DROPPED_METRIC = "non_ruling_pdf_dropped"

CATEGORIES = ("represented", "skipped", "pending", "unrepresented")

#: Where the oneshot task role may read and write key lists (#4803).
KEY_LIST_DIR = "tmp/keys/"

DEFAULT_REINGEST_SCRIPTS = (
    "/app/scripts/reingest_from_s3.py",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "reingest_from_s3.py"),
)


@dataclass(frozen=True)
class RawKey:
    key: str
    state: str
    county: str
    last_modified: datetime

    @property
    def scope(self) -> str:
        return f"{self.state}/{self.county}"

    @property
    def utc(self) -> datetime:
        lm = self.last_modified
        lm = lm if lm.tzinfo else lm.replace(tzinfo=UTC)
        return lm.astimezone(UTC)

    @property
    def day(self) -> str:
        return self.utc.date().isoformat()


@dataclass
class Explained:
    """The database's view of which raw keys are accounted for."""

    documents: set[str] = field(default_factory=set)
    #: s3_key -> reason, from ``telemetry.ingest_skips``.
    skips: dict[str, str] = field(default_factory=dict)
    validation_fail: set[str] = field(default_factory=set)
    all_null_guard: set[str] = field(default_factory=set)
    #: document ids of failed validation rows with no ``s3_key`` (written
    #: before #4773 added the column, #5023).
    validation_fail_documents: set[str] = field(default_factory=set)


@dataclass
class ScopeReport:
    scope: str
    counts: Counter[str] = field(default_factory=Counter)
    skipped_by_reason: Counter[str] = field(default_factory=Counter)
    dated_excluded: int = 0
    unrepresented: list[RawKey] = field(default_factory=list)

    def unrepresented_by_day(self) -> dict[str, int]:
        return dict(sorted(Counter(k.day for k in self.unrepresented).items()))

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {c: self.counts.get(c, 0) for c in CATEGORIES}
        out["raw"] = sum(self.counts.get(c, 0) for c in CATEGORIES)
        out["dated_excluded"] = self.dated_excluded
        out["skipped_by_reason"] = dict(sorted(self.skipped_by_reason.items()))
        out["unrepresented_by_month"] = dict(
            sorted(Counter(k.day[:7] for k in self.unrepresented).items())
        )
        return out


# ---------------------------------------------------------------------------
# S3 listing
# ---------------------------------------------------------------------------


def _list_level(
    s3: Any, bucket: str, prefix: str
) -> tuple[list[str], list[tuple[str, Any]]]:
    """The sub-prefixes and the objects directly under *prefix*."""
    prefixes: list[str] = []
    objects: list[tuple[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        prefixes.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
        objects.extend((o["Key"], o["LastModified"]) for o in page.get("Contents", []))
    return prefixes, objects


def list_raw_objects(s3: Any, bucket: str, prefix: str) -> list[tuple[str, Any]]:
    """Every object under *prefix* (no delimiter)."""
    out: list[tuple[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        out.extend((o["Key"], o["LastModified"]) for o in page.get("Contents", []))
    return out


def listing_units(
    s3: Any, bucket: str, prefix: str, exclude: Sequence[str]
) -> tuple[list[str], list[tuple[str, Any]]]:
    """The ``<state>/<county>/`` prefixes to list in parallel under *prefix*
    (``""``: the whole bucket minus *exclude*), and the stray objects found
    while descending (keys too shallow to be raws)."""
    stray: list[tuple[str, Any]] = []
    if prefix:
        states = [prefix]
    else:
        states, top = _list_level(s3, bucket, "")
        stray.extend(top)
        states = [p for p in states if p not in exclude]
    units: list[str] = []
    for state in states:
        if state.count("/") >= 2:
            units.append(state)  # already a county (or deeper) prefix
            continue
        counties, objs = _list_level(s3, bucket, state)
        stray.extend(objs)
        units.extend(counties)
    return units, stray


def list_all_raw_objects(
    s3: Any, bucket: str, prefix: str, exclude: Sequence[str], workers: int
) -> list[tuple[str, Any]]:
    units, stray = listing_units(s3, bucket, prefix, exclude)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda u: list_raw_objects(s3, bucket, u), units))
    out = list(stray)
    for r in results:
        out.extend(r)
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def partition_keys(
    objects: Iterable[tuple[str, Any]],
    *,
    since: date | None = None,
    until: date | None = None,
    counties: set[str] | None = None,
) -> tuple[list[RawKey], Counter[str], Counter[str]]:
    """Content-addressed raw keys in the window, counts of the rest, and the
    dated (legacy layout) keys per scope."""
    keys: list[RawKey] = []
    other: Counter[str] = Counter()
    dated: Counter[str] = Counter()
    for key, last_modified in objects:
        if not RAW_SEGMENT_RE.match(key):
            other["not_raw"] += 1
            continue
        m = CONTENT_KEY_RE.match(key)
        if not m:
            d = DATED_KEY_RE.match(key)
            if d:
                if not counties or d["county"] in counties:
                    other["dated"] += 1
                    dated[f"{d['state']}/{d['county']}"] += 1
            else:
                other["other_layout"] += 1
            continue
        if counties and m["county"] not in counties:
            continue
        raw = RawKey(
            key=key, state=m["state"], county=m["county"], last_modified=last_modified
        )
        day = raw.utc.date()
        if (since and day < since) or (until and day > until):
            other["outside_window"] += 1
            continue
        keys.append(raw)
    return keys, other, dated


def raw_key_document_id(key: str) -> str | None:
    """The document id the worker gives a content-addressed raw key:
    ``uuid5(NAMESPACE_URL, <sha256>)``, as the live scraper
    (``framework.base``) and a prefix reingest (``reingest_from_s3``)
    derive it.  ``None`` for any other key layout.

    A split child has its own id (``split_child_document_id``), so a
    pre-#4773 validation row for a child is not matched; only the parent's.
    """
    m = CONTENT_KEY_RE.match(key)
    if not m:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, m["hash"]))


def classify(
    raw: RawKey, explained: Explained, *, now: datetime, grace: timedelta
) -> tuple[str, str | None]:
    """``(category, skip reason)`` for one raw key."""
    key = raw.key
    if key in explained.documents:
        return "represented", None
    reason = explained.skips.get(key)
    if reason:
        return "skipped", reason
    if key in explained.validation_fail:
        return "skipped", "validation_fail"
    if (
        explained.validation_fail_documents
        and raw_key_document_id(key) in explained.validation_fail_documents
    ):
        return "skipped", "validation_fail_by_document_id"
    if key in explained.all_null_guard:
        return "skipped", "all_null_guard"
    for prefix, prefix_reason in INTENTIONAL_SKIP_PREFIXES.items():
        if key.startswith(prefix):
            return "skipped", prefix_reason
    if now - raw.utc < grace:
        return "pending", None
    return "unrepresented", None


def is_sc_placeholder_pdf(body: bytes) -> bool:
    """The worker's own Santa Clara placeholder test on the PDF's text layer."""
    from courts.ca.sc_tentatives import is_placeholder_notice
    from ingestion.llm_extract import extract_text_layer

    return is_placeholder_notice(extract_text_layer(body))


def reconcile(
    keys: Iterable[RawKey],
    explained: Explained,
    *,
    now: datetime,
    grace: timedelta,
    dated: Counter[str] | None = None,
    placeholder_check: Callable[[RawKey], bool] | None = None,
) -> dict[str, ScopeReport]:
    reports: dict[str, ScopeReport] = {}
    for raw in keys:
        rep = reports.setdefault(raw.scope, ScopeReport(raw.scope))
        category, reason = classify(raw, explained, now=now, grace=grace)
        if (
            category == "unrepresented"
            and placeholder_check is not None
            and raw.county == "santa_clara"
            and raw.key.endswith(".pdf")
            and placeholder_check(raw)
        ):
            category, reason = "skipped", "santa_clara_placeholder"
        rep.counts[category] += 1
        if reason:
            rep.skipped_by_reason[reason] += 1
        if category == "unrepresented":
            rep.unrepresented.append(raw)
    for scope, n in (dated or {}).items():
        reports.setdefault(scope, ScopeReport(scope)).dated_excluded = n
    for rep in reports.values():
        rep.unrepresented.sort(key=lambda k: (k.utc, k.key))
    return reports


def totals(reports: dict[str, ScopeReport]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for rep in reports.values():
        s = rep.summary()
        for c in (*CATEGORIES, "raw", "dated_excluded"):
            total[c] += s[c]
    return {c: total.get(c, 0) for c in (*CATEGORIES, "raw", "dated_excluded")}


# ---------------------------------------------------------------------------
# Key lists
# ---------------------------------------------------------------------------


def chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0 or len(items) <= size:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def key_list_uris(stem: str, name: str, n_chunks: int) -> list[str]:
    if n_chunks == 1:
        return [f"{stem}-{name}.txt"]
    return [f"{stem}-{name}-{i:02d}.txt" for i in range(1, n_chunks + 1)]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")


def write_text(s3: Any, uri: str, text: str) -> None:
    if not uri.startswith("s3://"):
        with open(uri, "w") as fh:
            fh.write(text)
        return
    bucket, _, key = uri[len("s3://") :].partition("/")
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode())


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _fetch_set(cur: Any, sql: str) -> set[str]:
    cur.execute(sql)
    return {row[0] for row in cur.fetchall() if row[0]}


def fetch_explained(conn: Any) -> Explained:
    """Every s3_key the database accounts for, from all four sources."""
    explained = Explained()
    with conn.cursor() as cur:
        explained.documents = _fetch_set(
            cur,
            "SELECT DISTINCT s3_key FROM derived.documents WHERE s3_key IS NOT NULL",
        )
        explained.validation_fail = _fetch_set(
            cur,
            "SELECT DISTINCT s3_key FROM telemetry.validation_results "
            "WHERE result = 'fail' AND s3_key IS NOT NULL",
        )
        explained.validation_fail_documents = _fetch_set(
            cur,
            "SELECT DISTINCT document_id::text FROM telemetry.validation_results "
            "WHERE result = 'fail' AND s3_key IS NULL AND document_id IS NOT NULL",
        )
        explained.all_null_guard = _fetch_set(
            cur,
            "SELECT DISTINCT metadata->>'s3_key' FROM telemetry.data_quality_metrics "
            f"WHERE metric_name = '{NON_RULING_PDF_DROPPED_METRIC}' "
            "AND metadata->>'s3_key' IS NOT NULL",
        )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT s3_key, reason FROM telemetry.ingest_skips")
            explained.skips = {row[0]: row[1] for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 — table not migrated yet
        conn.rollback()
        print(
            f"WARNING telemetry.ingest_skips unreadable ({exc}); no skip records used"
        )
    return explained


def fetch_recent_runs(conn: Any, n: int) -> list[tuple[int, str]]:
    """``(unrepresented, action)`` of the last *n* recorded runs, newest first."""
    if n <= 0:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT unrepresented, action FROM telemetry.raw_reconcile_runs "
            "ORDER BY finished_at DESC LIMIT %s",
            (n,),
        )
        return [(int(row[0]), str(row[1])) for row in cur.fetchall()]


def record_run(
    conn: Any,
    *,
    started_at: datetime,
    reports: dict[str, ScopeReport],
    action: str,
    key_list_uri: str | None,
    detail: dict[str, Any],
) -> str:
    """Insert the run and its per-scope counts; return the run id."""
    total = totals(reports)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO telemetry.raw_reconcile_runs
                (started_at, raw_keys, represented, skipped, unrepresented,
                 pending, dated_excluded, action, key_list_uri, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING run_id
            """,
            (
                started_at,
                total["raw"],
                total["represented"],
                total["skipped"],
                total["unrepresented"],
                total["pending"],
                total["dated_excluded"],
                action,
                key_list_uri,
                json.dumps(detail, default=str),
            ),
        )
        row = cur.fetchone()
        run_id = str(row[0])
        for scope in sorted(reports):
            rep = reports[scope]
            s = rep.summary()
            cur.execute(
                """
                INSERT INTO telemetry.raw_reconcile_counts
                    (run_id, scope, raw_keys, represented, skipped, unrepresented,
                     pending, dated_excluded, skipped_by_reason, unrepresented_by_day)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (
                    run_id,
                    scope,
                    s["raw"],
                    s["represented"],
                    s["skipped"],
                    s["unrepresented"],
                    s["pending"],
                    s["dated_excluded"],
                    json.dumps(s["skipped_by_reason"]),
                    json.dumps(rep.unrepresented_by_day()),
                ),
            )
    conn.commit()
    return run_id


def update_run(conn: Any, run_id: str, action: str, detail: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE telemetry.raw_reconcile_runs SET action = %s, detail = %s::jsonb "
            "WHERE run_id = %s",
            (action, json.dumps(detail, default=str), run_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------


#: Actions that launched a reingest of the run's unrepresented keys.
REINGEST_ACTIONS = frozenset({"reingest", "persistent"})
#: Actions the workflow files or updates a GitHub issue for.
ISSUE_ACTIONS = frozenset({"over_threshold", "persistent", "reingest_failed"})


def decide_action(
    unrepresented: int,
    previous: Sequence[tuple[int, str]],
    *,
    max_auto_reingest: int,
    persist_runs: int,
    reingest: bool,
    defer: bool,
) -> str:
    """What to do about *unrepresented* keys, given ``(unrepresented,
    action)`` of the previous recorded runs, newest first.

    ``persistent`` still re-ingests (new losses keep healing), and also
    files the issue: each of the previous ``persist_runs - 1`` runs already
    re-ingested and the keys are back.  Runs that did not re-ingest
    (deferred, report-only, over the threshold) do not count as a failed
    heal.
    """
    if unrepresented == 0:
        return "none"
    if unrepresented > max_auto_reingest:
        return "over_threshold"
    if not reingest:
        return "report_only"
    if defer:
        return "reingest_deferred"
    earlier = list(previous[: max(0, persist_runs - 1)])
    if (
        persist_runs > 1
        and len(earlier) == persist_runs - 1
        and all(n > 0 and action in REINGEST_ACTIONS for n, action in earlier)
    ):
        return "persistent"
    return "reingest"


def reingest_groups(reports: dict[str, ScopeReport]) -> dict[str, list[str]]:
    """Unrepresented keys grouped by top-level ``<state>/`` prefix."""
    groups: dict[str, list[str]] = {}
    for scope in sorted(reports):
        for raw in reports[scope].unrepresented:
            groups.setdefault(f"{raw.state}/", []).append(raw.key)
    return groups


def find_reingest_script(explicit: str | None) -> str:
    candidates = [explicit] if explicit else list(DEFAULT_REINGEST_SCRIPTS)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(f"reingest_from_s3.py not found (tried {candidates})")


def launch_reingest(
    args: argparse.Namespace,
    s3: Any,
    groups: dict[str, list[str]],
    script: str,
    stamp: str,
    out: Callable[[str], None],
    runner: Callable[..., Any] = subprocess.run,
) -> list[dict[str, Any]]:
    """Write one key list per prefix and re-ingest it; return what ran."""
    ran: list[dict[str, Any]] = []
    for prefix, keys in groups.items():
        name = _slug(prefix.rstrip("/"))
        uri = f"s3://{args.bucket}/{KEY_LIST_DIR}raw-reconcile-{stamp}-{name}.txt"
        failed_uri = (
            f"s3://{args.bucket}/{KEY_LIST_DIR}raw-reconcile-{stamp}-{name}-failed.txt"
        )
        write_text(s3, uri, "".join(f"{k}\n" for k in keys))
        cmd = [
            sys.executable,
            script,
            "--prefix",
            prefix,
            "--s3-key-list",
            uri,
            "--concurrency",
            str(args.reingest_concurrency),
            "--write-failed-manifest",
            failed_uri,
            "--no-fail-on-errors",
        ]
        out(f"REINGEST_START prefix={prefix} keys={len(keys)} list={uri}")
        result = runner(cmd, check=False)
        code = getattr(result, "returncode", None)
        out(f"REINGEST_DONE prefix={prefix} exit={code}")
        ran.append({"prefix": prefix, "keys": len(keys), "key_list": uri, "exit": code})
    return ran


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(
    args: argparse.Namespace,
    s3: Any,
    explained: Explained,
    out: Callable[[str], None] = print,
    *,
    now: datetime | None = None,
    conn: Any = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    started_at = now or datetime.now(UTC)
    objects = list_all_raw_objects(
        s3, args.bucket, args.prefix, args.exclude_prefix, args.workers
    )
    keys, other, dated = partition_keys(
        objects,
        since=args.since,
        until=args.until,
        counties=set(args.county) or None,
    )
    out(
        f"LISTED {len(keys)} content-addressed raw keys under {args.prefix or '<all>'} "
        f"(since={args.since} until={args.until}); other: "
        f"{json.dumps(dict(sorted(other.items())))}"
    )
    out(
        f"DB documents={len(explained.documents)} skips={len(explained.skips)} "
        f"validation_fail={len(explained.validation_fail)} "
        f"validation_fail_by_document_id={len(explained.validation_fail_documents)} "
        f"all_null_guard={len(explained.all_null_guard)}"
    )

    placeholder_check = None
    if args.classify_placeholders:

        def placeholder_check(raw: RawKey) -> bool:
            try:
                body = s3.get_object(Bucket=args.bucket, Key=raw.key)["Body"].read()
                return is_sc_placeholder_pdf(body)
            except Exception as exc:  # noqa: BLE001 — leave it unrepresented
                out(f"PLACEHOLDER_CHECK_ERROR key={raw.key} error={exc}")
                return False

    reports = reconcile(
        keys,
        explained,
        now=started_at,
        grace=timedelta(hours=args.grace_hours),
        dated=dated,
        placeholder_check=placeholder_check,
    )

    for scope in sorted(reports):
        rep = reports[scope]
        out(f"SUMMARY {scope}: {json.dumps(rep.summary(), sort_keys=True)}")
        if rep.unrepresented:
            out(f"UNREPRESENTED_DAYS {scope}: {json.dumps(rep.unrepresented_by_day())}")
        for raw in rep.unrepresented[: args.sample]:
            out(f"UNREPRESENTED {scope} day={raw.day} key={raw.key}")
        if args.write_keys and rep.unrepresented:
            chunks = chunked([k.key for k in rep.unrepresented], args.chunk_size)
            for uri, chunk in zip(
                key_list_uris(
                    args.write_keys, _slug(scope.replace("/", "_")), len(chunks)
                ),
                chunks,
                strict=True,
            ):
                write_text(s3, uri, "".join(f"{k}\n" for k in chunk))
                out(f"WROTE_KEYS {scope} {len(chunk)} -> {uri}")

    total = totals(reports)
    out(f"SUMMARY_ALL: {json.dumps(total, sort_keys=True)}")

    previous: list[tuple[int, str]] = []
    if conn is not None:
        previous = fetch_recent_runs(conn, max(0, args.persist_runs - 1))
    action = decide_action(
        total["unrepresented"],
        previous,
        max_auto_reingest=args.max_auto_reingest,
        persist_runs=args.persist_runs if conn is not None else 0,
        reingest=args.reingest,
        defer=args.defer_reingest,
    )
    top = sorted(
        ((rep.scope, rep.counts.get("unrepresented", 0)) for rep in reports.values()),
        key=lambda t: (-t[1], t[0]),
    )
    detail: dict[str, Any] = {
        "other": dict(sorted(other.items())),
        "previous_unrepresented": [n for n, _ in previous],
        "previous_actions": [a for _, a in previous],
        "top_scopes": [[s, n] for s, n in top if n > 0][:10],
        "max_auto_reingest": args.max_auto_reingest,
    }

    script = None
    if action in REINGEST_ACTIONS:
        try:
            script = find_reingest_script(args.reingest_script)
        except FileNotFoundError as exc:
            action = "reingest_failed"
            detail["reingest_error"] = str(exc)

    run_id = None
    if conn is not None:
        run_id = record_run(
            conn,
            started_at=started_at,
            reports=reports,
            action=action,
            key_list_uri=None,
            detail=detail,
        )
        out(f"RECORDED run_id={run_id}")

    if script is not None:
        stamp = started_at.strftime("%Y%m%dT%H%MZ")
        try:
            detail["reingest"] = launch_reingest(
                args, s3, reingest_groups(reports), script, stamp, out, runner=runner
            )
        except Exception as exc:  # noqa: BLE001 — recorded, then reported as failed
            action = "reingest_failed"
            detail["reingest_error"] = f"{type(exc).__name__}: {exc}"
        if conn is not None and run_id is not None:
            update_run(conn, run_id, action, detail)

    result = {
        "run_id": run_id,
        "action": action,
        "issue_needed": action in ISSUE_ACTIONS,
        **total,
        **detail,
    }
    out(f"RECONCILE_RESULT {json.dumps(result, sort_keys=True, default=str)}")
    return {"reports": reports, "result": result}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument(
        "--prefix",
        default="",
        help="Reconcile only this prefix, e.g. ca/ (default: every capture prefix).",
    )
    parser.add_argument(
        "--exclude-prefix",
        action="append",
        default=None,
        help="Top-level prefix to skip in a whole-bucket run (repeatable; "
        "replaces the defaults).",
    )
    parser.add_argument(
        "--county",
        action="append",
        default=[],
        help="S3 county segment to reconcile, e.g. los_angeles (repeatable).",
    )
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument("--until", type=date.fromisoformat, default=None)
    parser.add_argument("--grace-hours", type=float, default=6.0)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--classify-placeholders",
        action="store_true",
        help="Fetch unrepresented Santa Clara PDFs and count placeholder notices as skipped.",
    )
    parser.add_argument(
        "--write-keys",
        metavar="URI_STEM",
        help="Write unrepresented keys to <stem>-<state>_<county>[-NN].txt (s3:// or local).",
    )
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--sample", type=int, default=5)
    parser.add_argument(
        "--record",
        action="store_true",
        help="Write the run to telemetry.raw_reconcile_runs / raw_reconcile_counts.",
    )
    parser.add_argument(
        "--reingest",
        action="store_true",
        help="Re-ingest the unrepresented keys when there are few enough (needs --record).",
    )
    parser.add_argument(
        "--defer-reingest",
        action="store_true",
        help="Another reingest is running: report only (action reingest_deferred).",
    )
    parser.add_argument("--max-auto-reingest", type=int, default=500)
    parser.add_argument("--persist-runs", type=int, default=3)
    parser.add_argument("--reingest-concurrency", type=int, default=4)
    parser.add_argument("--reingest-script", default=None)
    args = parser.parse_args(argv)
    if args.since and args.until and args.since > args.until:
        parser.error("--since is after --until")
    if args.prefix and not args.prefix.endswith("/"):
        parser.error("--prefix must end with /")
    if args.reingest and not args.record:
        parser.error("--reingest needs --record (the threshold reads the run history)")
    if args.record and (args.prefix or args.county or args.since or args.until):
        parser.error(
            "--record is for full runs only (no --prefix/--county/--since/--until)"
        )
    if args.exclude_prefix is None:
        args.exclude_prefix = list(DEFAULT_EXCLUDE_PREFIXES)
    return args


def main(argv: list[str] | None = None) -> int:
    import boto3
    import psycopg

    args = parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    s3 = boto3.client("s3")
    with psycopg.connect(database_url) as conn:
        explained = fetch_explained(conn)
        conn.commit()
        outcome = run(
            args,
            s3,
            explained,
            out=lambda line: print(line, flush=True),
            conn=conn if args.record else None,
        )
    return 1 if outcome["result"]["action"] == "reingest_failed" else 0


if __name__ == "__main__":
    sys.exit(main())
