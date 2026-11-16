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

- ``represented``    a ``derived.documents`` row has this ``s3_key`` and
                     its ruling is reachable (#5093): a row on the key is
                     ``active`` or holds a ruling, or a superseded row lost
                     content-hash dedup to a ruling that still exists (see
                     ``orphaned_superseded`` below).  ``s3_key`` is
                     authoritative for content-addressed keys;
                     ``content_hash`` is not (split children carry other
                     hashes, #4988).
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
- ``incomplete``     a ``telemetry.ingest_incomplete`` row (#5008): the key's
                     rows are not a complete split (a live new key written
                     whole or partially after a failed extraction, an
                     exhausted split child, a dead-lettered event, a reingest
                     that refused an incomplete extraction).  Checked first:
                     a fallback row would otherwise count as represented.
                     Re-ingested with the unrepresented keys, oldest first,
                     as many as fit beside them under ``--max-auto-reingest``
                     (they never make a run ``over_threshold``; the rest
                     wait for the next run).  On an ``over_threshold`` run
                     the unrepresented keys wait for a human, but up to
                     ``--max-auto-reingest`` incomplete keys still
                     re-ingest on their own (``reingest_scope``
                     ``incomplete_only``).  Every launch of a reingest
                     with the key counts one ``retry_attempts`` (at most
                     one per run), committed before the subprocess starts,
                     so a reingest that crashes, is OOM-killed or takes the
                     whole task down still counts (#5054); only a launch
                     that never happened is not counted.  A complete
                     reingest deletes the row (the worker).  See "Crash
                     isolation" below.
                     A key whose only explanation is a validation failure
                     judged by an older version of a since-relaxed rule
                     (``validation.rule_versions.RULE_VERSIONS``, #5027) is
                     incomplete too: with ``--record`` this script writes
                     its row, reason ``stale_validation``, and deletes it
                     once the key is represented or the current rules
                     failed it again.
                     A key whose document rows are all superseded, hold no
                     ruling, and whose dedup ruling is gone is incomplete
                     too, reason ``orphaned_superseded`` (#5093; the #4982
                     path deletes the dedup winner and nulls the loser's
                     link).  A superseded-only key is covered, so still
                     ``represented``, when one of its rows lost dedup to a
                     ruling that still exists: a ruling with the
                     ``content_hash_dedup_supersede`` metric's
                     ``(case_id, ruling_text_hash)``, or with that hash on
                     the row's ``previous_version_id`` winner (the winner's
                     case can be re-keyed); with no metric row, any ruling
                     on the winner.  A ruling with the same hash in another
                     case does not count (short boilerplate repeats across
                     cases).  A recorded skip or validation failure still
                     explains the key.  With ``--record`` this script
                     writes the row (deferred backlogs excepted) and
                     deletes it once the key is covered again.
                     Likewise a key whose only explanation is a stale
                     extraction skip verdict (``no_ruling_text`` /
                     ``zero_rulings_no_text`` recorded under an older
                     ``validation.skip_versions`` rule version, or over
                     rulings the key held, #5094): reason ``stale_skip``,
                     re-ingested with ``--bust-llm-cache`` so the LLM cache
                     does not replay the verdict, and resolved once the
                     key is represented or the verdict is recorded again
                     under the current rules.
- ``incomplete_stuck``  an incomplete key at ``--max-incomplete-attempts``
                     (default 3): reported, not retried.  Keys that just
                     reached the cap set ``issue_needed`` once (``stuck_at``
                     is stamped); ``--reset-incomplete-attempts`` makes them
                     retryable again after a fix.
- ``unrepresented``  none of the above: a capture that never reached the
                     database.  These are the keys to reingest.

A ``telemetry.ingest_incomplete`` row whose raw key is gone from the
archive (not in the listing, and a ``HEAD`` answers 404) can never be
re-ingested.  It is reported as ``orphan_incomplete`` (#5093) and, with
``--record``, closed out: the row moves to ``telemetry.ingest_skips`` with
reason ``raw_missing`` (its incomplete reason, detail and attempts kept in
the skip's detail), so it is reported once, files the issue once, and is
never retried.  Only keys inside the run's listing scope are checked.

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
  ``/admin/data-quality`` panel reads them) and decides an action for each
  ``<state>/<county>`` scope with unrepresented keys (``scope_actions`` in
  the result, ``SCOPE_ACTION`` lines; #5036):

  - ``over_threshold``   more than ``--max-auto-reingest`` keys in the scope
                         (default 500): too many to heal blindly, a GitHub
                         issue is filed or updated instead (by the
                         workflow).  Other scopes still heal;
  - ``reingest``         with ``--reingest``: the scope's unrepresented keys
                         are written to ``s3://<bucket>/tmp/keys/`` and
                         re-ingested by ``reingest_from_s3.py --prefix
                         <state>/ --s3-key-list <uri> --concurrency 4``;
  - ``persistent``       a ``reingest`` of a scope that each of the previous
                         ``--persist-runs - 1`` runs (default 2) also
                         re-ingested: it re-ingests again (new losses keep
                         healing) and the issue is filed or updated, since
                         something is not healing;
  - ``reingest_deferred``  ``--defer-reingest``: another reingest is
                         running, so this run only reports;
  - ``report_only``      no ``--reingest``.

  The run's own action: ``none`` (nothing to do), ``reingest`` /
  ``persistent`` (some scope healed), else ``over_threshold``,
  ``report_only`` or ``reingest_deferred``; ``reingest_failed`` when the
  reingest could not be launched (exit 1).

  Deferred backlogs (:data:`DEFERRED_BACKLOGS`, #5036) are unrepresented
  keys whose reingest is deliberately on hold behind an issue (the May-June
  CourtListener raws, #3954).  They stay ``unrepresented`` in the per-scope
  counts but are reported as "deferred (blocked on #N)" (``DEFERRED``
  lines, ``deferred_backlogs`` in the result): never re-ingested, never
  ``over_threshold``, and not counted in ``actionable_unrepresented``,
  which decides whether the issue can close.

  The workflow files or updates the issue when the run or any scope is
  ``over_threshold``, ``persistent`` or ``reingest_failed``
  (``issue_needed`` in the result).  No human alert.

  Crash isolation (#5054).  One key that kills the reingest (a document
  that exhausts memory) must not sink the other keys of its run:

  - each ``<state>/`` prefix's keys run in one ``reingest_from_s3.py``
    subprocess, except incomplete keys recorded ``dead_letter`` (they killed
    the live worker, #5010), which run one key per subprocess after the
    batched launches;
  - a launch that does not come back clean is bisected in the same run:
    a non-zero exit (a crash, or a signal: ``-9`` / ``137`` for an OOM kill)
    implicates every key of the launch, an exit 0 with keys in its
    ``--write-crashed-manifest`` (a pool worker died) implicates those keys.
    The implicated keys re-run in halves until each crashing key is alone;
    the halves that come back clean heal as usual.  No key is counted twice
    in one run.  The keys that crash alone are ``REINGEST_CRASH`` lines and
    ``reingest_crashed`` in the result; at the attempt cap they become
    ``incomplete_stuck`` and are reported once;
  - the reingest runs with ``oom_score_adj`` 1000, so the kernel's OOM
    killer picks it (or its pool workers) before this process, which then
    records the outcome.  If the whole task dies anyway, the attempt was
    already counted, so the loop still ends at the cap.

  Bisection only re-runs keys a crash implicated, so it costs nothing on a
  clean run and about ``2 * log2(n)`` extra launches per crashing key.
  ``--simulate-crash-key KEY`` (manual verification only, never in the
  scheduled workflow) replaces any launch holding KEY with a subprocess that
  SIGKILLs itself, to exercise this path on dev without crashing a worker.

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
``--persist-runs``, ``--reingest-concurrency``, ``--reingest-script``,
``--simulate-crash-key``.
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

from validation.deferred_backlogs import (
    DEFERRED_BACKLOGS,
    DeferredBacklog,
)
from validation.rule_versions import (
    FailRow,
    fail_row_from_db,
    stale_rules,
)
from validation.skip_versions import EXTRACTION_SKIP_REASONS, stale_skip_reason

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


# Deferred backlogs (#5036) have one definition, shared with
# reingest_from_s3.py and rebuild_db.py, which skip their keys by default
# (#5043): ``validation.deferred_backlogs``.


#: ``ingestion.ruling_guards.NON_RULING_PDF_DROPPED_METRIC`` (all-NULL guard).
NON_RULING_PDF_DROPPED_METRIC = "non_ruling_pdf_dropped"

CATEGORIES = (
    "represented",
    "skipped",
    "pending",
    "incomplete",
    "incomplete_stuck",
    "unrepresented",
)

#: Reingest attempts after which an incomplete key is reported as stuck
#: rather than retried again (#5008).
DEFAULT_MAX_INCOMPLETE_ATTEMPTS = 3

#: ``ingestion.ingest_incomplete.INCOMPLETE_STALE_VALIDATION``: the
#: incomplete reason this script records for a key whose validation failure
#: was judged by a since-relaxed rule version (#5027).
STALE_VALIDATION = "stale_validation"

#: ``ingestion.ingest_incomplete.INCOMPLETE_ORPHANED_SUPERSEDED``: the
#: incomplete reason this script records for a key whose rows are all
#: superseded and whose dedup ruling is gone (#5093).
ORPHANED_SUPERSEDED = "orphaned_superseded"

#: Incomplete reasons this script records (and resolves) itself.
RECONCILER_REASONS = frozenset({STALE_VALIDATION, ORPHANED_SUPERSEDED})

#: ``ingestion.ingest_skips.SKIP_RAW_MISSING``: the skip reason an
#: incomplete key is closed out with once its raw object is gone (#5093).
RAW_MISSING = "raw_missing"

#: ``HEAD`` error codes that mean the object does not exist.
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

#: ``ingestion.ingest_incomplete.INCOMPLETE_STALE_SKIP``: the incomplete
#: reason this script records for a key whose only explanation is an
#: extraction skip verdict that must be re-judged (#5094).
STALE_SKIP = "stale_skip"

#: Incomplete reasons whose keys re-ingest with ``--bust-llm-cache``
#: (#5094): the LLM cache would replay the verdict being re-judged.
BUST_CACHE_REASONS = frozenset({STALE_SKIP})

#: Incomplete reasons whose keys re-ingest one key per subprocess, after the
#: batched launches (#5054).  A ``dead_letter`` key killed the live worker
#: ``MAX_MESSAGE_CRASHES`` times (#5010, #5055), so it is the likeliest to kill a
#: reingest too.
ISOLATED_REASONS = frozenset({"dead_letter"})

#: ``--simulate-crash-key``: the subprocess that stands in for a reingest
#: holding the key.  It SIGKILLs itself, like the OOM killer would.
SIMULATED_CRASH_ARGS = (
    "-c",
    "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
)

#: Where the oneshot task role may read and write key lists (#4803).
KEY_LIST_DIR = "tmp/keys/"

DEFAULT_REINGEST_SCRIPTS = (
    "/app/scripts/reingest_from_s3.py",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "reingest_from_s3.py"),
)

#: Every flag :meth:`Reingester.launch` passes to ``reingest_from_s3.py``.
#: This script is uploaded fresh per run, but the reingest CLI it runs is
#: the copy baked into the scraper image, so the two can be different
#: versions (#5060).  A flag the baked copy does not know makes argparse
#: exit 2 on every launch, which would read as a crash and charge every
#: incomplete key an attempt toward stuck.  :func:`check_reingest_cli`
#: refuses up front instead.
REINGEST_CLI_FLAGS = (
    "--prefix",
    "--s3-key-list",
    "--concurrency",
    "--write-failed-manifest",
    "--write-crashed-manifest",
    "--no-fail-on-errors",
    "--bust-llm-cache",
)


class ReingestCliMismatch(RuntimeError):
    """The reingest CLI on disk predates a flag this reconciler passes."""


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
    #: s3_key -> (reason, retry_attempts), from ``telemetry.ingest_incomplete``:
    #: keys whose rows are not a complete split (#5008).
    incomplete: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: The failed validation rows behind ``validation_fail`` (by s3_key) and
    #: ``validation_fail_documents`` (by document id), with the rule versions
    #: that judged them (#5027).
    validation_rows: dict[str, list[FailRow]] = field(default_factory=dict)
    validation_rows_by_document: dict[str, list[FailRow]] = field(default_factory=dict)
    #: Keys in ``documents`` whose rows are all superseded, hold no ruling,
    #: and whose dedup ruling is gone (#5093): not represented.
    orphaned_superseded: set[str] = field(default_factory=set)
    #: s3_key -> detail of an extraction skip verdict's ``telemetry.ingest_skips``
    #: row (rule version, extractor, rulings held; #5094).
    skip_details: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ScopeReport:
    scope: str
    counts: Counter[str] = field(default_factory=Counter)
    skipped_by_reason: Counter[str] = field(default_factory=Counter)
    #: Incomplete keys (retryable and stuck) by recorded reason (#5008).
    incomplete_by_reason: Counter[str] = field(default_factory=Counter)
    dated_excluded: int = 0
    unrepresented: list[RawKey] = field(default_factory=list)
    #: Incomplete keys under the attempt cap: re-ingested with the
    #: unrepresented ones (#5008).
    incomplete: list[RawKey] = field(default_factory=list)
    #: Keys found pending re-validation this run, with no incomplete record
    #: yet, and the since-relaxed rules that failed them (#5027).
    new_stale: list[tuple[RawKey, list[str]]] = field(default_factory=list)
    #: Keys whose ``stale_validation`` record is resolved: the key is now
    #: represented, or the current rules failed it again (#5027).
    resolved_stale: list[RawKey] = field(default_factory=list)
    #: Orphaned superseded keys found this run with no incomplete record
    #: yet, and keys whose ``orphaned_superseded`` record is resolved (the
    #: key is covered again, #5093).
    new_orphaned: list[RawKey] = field(default_factory=list)
    resolved_orphaned: list[RawKey] = field(default_factory=list)
    #: Keys found with a stale extraction skip this run, with no incomplete
    #: record yet, and why it is stale (#5094).
    new_stale_skips: list[tuple[RawKey, str]] = field(default_factory=list)
    #: Keys whose ``stale_skip`` record is resolved (#5094).
    resolved_stale_skips: list[RawKey] = field(default_factory=list)

    def unrepresented_by_day(self) -> dict[str, int]:
        return dict(sorted(Counter(k.day for k in self.unrepresented).items()))

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {c: self.counts.get(c, 0) for c in CATEGORIES}
        out["raw"] = sum(self.counts.get(c, 0) for c in CATEGORIES)
        out["dated_excluded"] = self.dated_excluded
        out["skipped_by_reason"] = dict(sorted(self.skipped_by_reason.items()))
        out["incomplete_by_reason"] = dict(sorted(self.incomplete_by_reason.items()))
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


def key_stale_rules(raw: RawKey, explained: Explained) -> list[str]:
    """The since-relaxed rules that failed *raw*'s rulings and have not
    judged it since (``validation.rule_versions.stale_rules``, #5027)."""
    rows = list(explained.validation_rows.get(raw.key, ()))
    document_id = raw_key_document_id(raw.key)
    if document_id:
        rows.extend(explained.validation_rows_by_document.get(document_id, ()))
    return sorted(stale_rules(rows)) if rows else []


def _intentional_skip(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in INTENTIONAL_SKIP_PREFIXES)


def is_represented(key: str, explained: Explained) -> bool:
    """*key* has document rows and its ruling is reachable (#5093)."""
    return key in explained.documents and key not in explained.orphaned_superseded


def _reconciler_record_resolved(raw: RawKey, explained: Explained) -> bool:
    """A record this script wrote whose cause is gone.

    ``stale_validation`` (#5027): the key is represented, or its failures
    stand under the current rules.  ``orphaned_superseded`` (#5093): the key
    is no longer orphaned (revived, covered by its dedup ruling, or its
    rows are gone, which makes it unrepresented instead)."""
    record = explained.incomplete.get(raw.key)
    if record is None:
        return False
    if record[0] == STALE_VALIDATION:
        return is_represented(raw.key, explained) or not key_stale_rules(raw, explained)
    if record[0] == ORPHANED_SUPERSEDED:
        return raw.key not in explained.orphaned_superseded
    return False


def key_stale_skip(raw: RawKey, explained: Explained) -> str | None:
    """Why *raw*'s skip record must be re-judged, or ``None`` (#5094).

    Only an extraction verdict (``no_ruling_text``, ``zero_rulings_no_text``)
    is ever stale: recorded under an older rule version
    (``validation.skip_versions``), or over rulings the key held without
    ``--allow-ruling-removal``.  A deterministic verdict (a placeholder
    notice, the governor route, nothing readable) stands."""
    reason = explained.skips.get(raw.key)
    if reason not in EXTRACTION_SKIP_REASONS:
        return None
    return stale_skip_reason(reason, explained.skip_details.get(raw.key))


def _stale_skip_record_resolved(raw: RawKey, explained: Explained) -> bool:
    """A ``stale_skip`` record whose re-judgement is done (#5094): the key
    is represented, or its skip verdict was recorded again under the
    current rules (or replaced by a deterministic one)."""
    record = explained.incomplete.get(raw.key)
    if record is None or record[0] != STALE_SKIP:
        return False
    return is_represented(raw.key, explained) or key_stale_skip(raw, explained) is None


def _incomplete_record_resolved(raw: RawKey, explained: Explained) -> bool:
    """A record this script wrote whose cause is gone (#5027, #5093, #5094)."""
    return _reconciler_record_resolved(raw, explained) or _stale_skip_record_resolved(
        raw, explained
    )


def classify(
    raw: RawKey,
    explained: Explained,
    *,
    now: datetime,
    grace: timedelta,
    max_incomplete_attempts: int = DEFAULT_MAX_INCOMPLETE_ATTEMPTS,
) -> tuple[str, str | None]:
    """``(category, reason)`` for one raw key.

    An incomplete record comes first (#5008): a fallback or partial
    document row makes the key look represented, and a dead-lettered key
    is otherwise just unrepresented.  Under the attempt cap it is
    ``incomplete`` (re-ingested); at the cap, ``incomplete_stuck``
    (reported, not retried).  The reason is the recorded one.

    A key whose only explanation is a validation failure judged by an older
    version of a since-relaxed rule is ``incomplete`` too, with reason
    ``stale_validation`` (#5027): it is re-ingested for the current rules
    to judge.  Its record is resolved, and the key classified as usual,
    once the key is represented or no failure is stale any more (the
    current rules failed it again).

    A key whose rows are all superseded with no reachable ruling
    (:attr:`Explained.orphaned_superseded`, #5093) is not represented: it
    is ``incomplete`` with reason ``orphaned_superseded`` unless a skip
    record or validation failure explains it.

    A key whose only explanation is a stale extraction skip verdict
    (:func:`key_stale_skip`) is ``incomplete`` too, with reason
    ``stale_skip`` (#5094): it is re-ingested, with ``--bust-llm-cache``,
    for the current extraction to judge it.  Its record is resolved once
    the key is represented or the verdict is recorded again under the
    current rules.
    """
    key = raw.key
    incomplete = explained.incomplete.get(key)
    if incomplete is not None and not _incomplete_record_resolved(raw, explained):
        reason, attempts = incomplete
        if attempts >= max_incomplete_attempts:
            return "incomplete_stuck", reason
        return "incomplete", reason
    if is_represented(key, explained):
        return "represented", None
    reason = explained.skips.get(key)
    if reason:
        if key_stale_skip(raw, explained):
            return "incomplete", STALE_SKIP
        return "skipped", reason
    validation_reason = None
    if key in explained.validation_fail:
        validation_reason = "validation_fail"
    elif (
        explained.validation_fail_documents
        and raw_key_document_id(key) in explained.validation_fail_documents
    ):
        validation_reason = "validation_fail_by_document_id"
    if validation_reason:
        if not _intentional_skip(key) and key_stale_rules(raw, explained):
            return "incomplete", STALE_VALIDATION
        return "skipped", validation_reason
    if key in explained.all_null_guard:
        return "skipped", "all_null_guard"
    for prefix, prefix_reason in INTENTIONAL_SKIP_PREFIXES.items():
        if key.startswith(prefix):
            return "skipped", prefix_reason
    if key in explained.orphaned_superseded:
        return "incomplete", ORPHANED_SUPERSEDED
    if now - raw.utc < grace:
        return "pending", None
    return "unrepresented", None


def is_sc_placeholder_pdf(body: bytes) -> bool:
    """The worker's own Santa Clara placeholder test on the PDF's text layer."""
    from courts.ca.sc_tentatives import is_placeholder_notice
    from ingestion.llm_extract import extract_text_layer

    return is_placeholder_notice(extract_text_layer(body))


def _track_stale_skip(
    rep: ScopeReport,
    raw: RawKey,
    category: str,
    reason: str | None,
    explained: Explained,
) -> None:
    """Note a key newly found with a stale extraction skip, or whose
    ``stale_skip`` record is resolved (#5094)."""
    record = explained.incomplete.get(raw.key)
    if record is not None and record[0] == STALE_SKIP:
        if category not in ("incomplete", "incomplete_stuck"):
            rep.resolved_stale_skips.append(raw)
    elif record is None and reason == STALE_SKIP:
        rep.new_stale_skips.append((raw, key_stale_skip(raw, explained) or ""))


def reconcile(
    keys: Iterable[RawKey],
    explained: Explained,
    *,
    now: datetime,
    grace: timedelta,
    dated: Counter[str] | None = None,
    placeholder_check: Callable[[RawKey], bool] | None = None,
    max_incomplete_attempts: int = DEFAULT_MAX_INCOMPLETE_ATTEMPTS,
) -> dict[str, ScopeReport]:
    reports: dict[str, ScopeReport] = {}
    for raw in keys:
        rep = reports.setdefault(raw.scope, ScopeReport(raw.scope))
        category, reason = classify(
            raw,
            explained,
            now=now,
            grace=grace,
            max_incomplete_attempts=max_incomplete_attempts,
        )
        if (
            category == "unrepresented"
            and placeholder_check is not None
            and raw.county == "santa_clara"
            and raw.key.endswith(".pdf")
            and placeholder_check(raw)
        ):
            category, reason = "skipped", "santa_clara_placeholder"
        record = explained.incomplete.get(raw.key)
        if record is not None and _reconciler_record_resolved(raw, explained):
            if record[0] == STALE_VALIDATION:
                rep.resolved_stale.append(raw)
            else:
                rep.resolved_orphaned.append(raw)
            record = None
        if record is None and reason == STALE_VALIDATION:
            rep.new_stale.append((raw, key_stale_rules(raw, explained)))
        elif record is None and reason == ORPHANED_SUPERSEDED:
            rep.new_orphaned.append(raw)
        _track_stale_skip(rep, raw, category, reason, explained)
        rep.counts[category] += 1
        if category in ("incomplete", "incomplete_stuck"):
            rep.incomplete_by_reason[reason or "unknown"] += 1
        elif reason:
            rep.skipped_by_reason[reason] += 1
        if category == "unrepresented":
            rep.unrepresented.append(raw)
        elif category == "incomplete":
            rep.incomplete.append(raw)
    for scope, n in (dated or {}).items():
        reports.setdefault(scope, ScopeReport(scope)).dated_excluded = n
    for rep in reports.values():
        rep.unrepresented.sort(key=lambda k: (k.utc, k.key))
        rep.incomplete.sort(key=lambda k: (k.utc, k.key))
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


#: Failed validation rows with an s3_key, or (before #4773) a document id.
_VALIDATION_FAIL_SQL = (
    "SELECT s3_key, document_id::text, created_at, reason, {versions} "
    "FROM telemetry.validation_results "
    "WHERE result = 'fail' AND (s3_key IS NOT NULL OR document_id IS NOT NULL)"
)
_VERSION_COLUMNS = "failed_rules, rule_versions"
_NO_VERSION_COLUMNS = "NULL::text[], NULL::jsonb"


def _fetch_validation_fails(conn: Any, explained: Explained) -> None:
    """The failed validation rows, by s3_key and (rows written before #4773
    added the column) by document id, with the rule versions that judged
    them (#5027; before migration 73 there are none)."""
    try:
        with conn.cursor() as cur:
            cur.execute(_VALIDATION_FAIL_SQL.format(versions=_VERSION_COLUMNS))
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — migration 73 not applied yet
        conn.rollback()
        print(f"WARNING validation rule versions unreadable ({exc}); inferring them")
        with conn.cursor() as cur:
            cur.execute(_VALIDATION_FAIL_SQL.format(versions=_NO_VERSION_COLUMNS))
            rows = cur.fetchall()
    for s3_key, document_id, created_at, reason, failed, versions in rows:
        row = fail_row_from_db(created_at, reason, failed, versions)
        if s3_key:
            explained.validation_rows.setdefault(s3_key, []).append(row)
        elif document_id:
            explained.validation_rows_by_document.setdefault(document_id, []).append(
                row
            )
    explained.validation_fail = set(explained.validation_rows)
    explained.validation_fail_documents = set(explained.validation_rows_by_document)


def record_stale_validation(
    conn: Any, stale: Sequence[tuple[RawKey, list[str]]]
) -> None:
    """Record keys pending re-validation in ``telemetry.ingest_incomplete``
    (reason ``stale_validation``, #5027), so their reingest goes through the
    #5008 attempt count and cap.  A record the worker already wrote for the
    key is left alone."""
    if not stale:
        return
    with conn.cursor() as cur:
        for raw, rules in stale:
            cur.execute(
                "INSERT INTO telemetry.ingest_incomplete "
                "(s3_key, reason, document_id, detail) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (s3_key) DO NOTHING",
                (
                    raw.key,
                    STALE_VALIDATION,
                    raw_key_document_id(raw.key),
                    json.dumps({"stale_rules": rules, "scope": raw.scope}),
                ),
            )
    conn.commit()


def record_stale_skips(conn: Any, stale: Sequence[tuple[RawKey, str]]) -> None:
    """Record keys whose extraction skip verdict must be re-judged in
    ``telemetry.ingest_incomplete`` (reason ``stale_skip``, #5094), so their
    reingest goes through the #5008 attempt count and cap.  A record the
    worker already wrote for the key is left alone."""
    if not stale:
        return
    with conn.cursor() as cur:
        for raw, why in stale:
            cur.execute(
                "INSERT INTO telemetry.ingest_incomplete "
                "(s3_key, reason, document_id, detail) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (s3_key) DO NOTHING",
                (
                    raw.key,
                    STALE_SKIP,
                    raw_key_document_id(raw.key),
                    json.dumps({"stale": why, "scope": raw.scope}),
                ),
            )
    conn.commit()


def clear_stale_skips(conn: Any, keys: Sequence[str]) -> None:
    """Delete the resolved ``stale_skip`` records (#5094)."""
    clear_stale_validation(conn, keys, STALE_SKIP)


def _fetch_skip_details(conn: Any, explained: Explained) -> None:
    """The detail of every extraction skip verdict's row (#5094): its rule
    version, extractor and the rulings the key held.  Best-effort: without
    it every such skip reads as version 1, so it is re-judged once."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s3_key, detail FROM telemetry.ingest_skips "
                "WHERE reason = ANY(%s)",
                (sorted(EXTRACTION_SKIP_REASONS),),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — table not migrated yet
        conn.rollback()
        print(f"WARNING ingest_skips detail unreadable ({exc}); no skip versions used")
        return
    for s3_key, detail in rows:
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                continue
        if s3_key and isinstance(detail, dict):
            explained.skip_details[s3_key] = detail


def clear_stale_validation(
    conn: Any, keys: Sequence[str], reason: str = STALE_VALIDATION
) -> None:
    """Delete the resolved records of *reason* (``stale_validation``,
    #5027; ``orphaned_superseded``, #5093; ``stale_skip``, #5094) for
    *keys*."""
    if not keys:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM telemetry.ingest_incomplete "
            "WHERE s3_key = ANY(%s) AND reason = %s",
            (list(keys), reason),
        )
    conn.commit()


def record_orphaned_superseded(conn: Any, keys: Sequence[RawKey]) -> None:
    """Record orphaned superseded keys in ``telemetry.ingest_incomplete``
    (reason ``orphaned_superseded``, #5093), so their reingest goes through
    the #5008 attempt count and cap.  A record already there is left
    alone."""
    if not keys:
        return
    with conn.cursor() as cur:
        for raw in keys:
            cur.execute(
                "INSERT INTO telemetry.ingest_incomplete "
                "(s3_key, reason, county, document_id, detail) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (s3_key) DO NOTHING",
                (
                    raw.key,
                    ORPHANED_SUPERSEDED,
                    raw.county,
                    raw_key_document_id(raw.key),
                    json.dumps({"scope": raw.scope}),
                ),
            )
    conn.commit()


#: Keys whose document rows are all superseded, hold no ruling, and whose
#: dedup ruling is gone (#5093).  A superseded row is covered when the
#: ruling it lost content-hash dedup to still exists: a ruling with the
#: ``content_hash_dedup_supersede`` metric's (case_id, ruling_text_hash), or
#: with that hash on the row's ``previous_version_id`` winner (a rebuild can
#: re-key the winner's case); with no metric row for the loser, any ruling
#: on the winner.  The same hash in another case does not count: short
#: boilerplate rulings repeat across cases.
ORPHANED_SUPERSEDED_SQL = """
WITH inactive AS (
    SELECT s3_key FROM derived.documents
    WHERE s3_key IS NOT NULL
    GROUP BY s3_key
    HAVING NOT bool_or(status = 'active')
), dedup AS (
    SELECT metadata->>'loser_document_id' AS loser_id,
           CASE WHEN metadata->>'case_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (metadata->>'case_id')::uuid END AS case_id,
           metadata->>'ruling_text_hash' AS text_hash
    FROM telemetry.data_quality_metrics
    WHERE metric_name = 'content_hash_dedup_supersede'
      AND metadata->>'loser_document_id' IS NOT NULL
)
SELECT d.s3_key
FROM derived.documents d
JOIN inactive USING (s3_key)
GROUP BY d.s3_key
HAVING NOT bool_or(
    EXISTS (SELECT 1 FROM derived.rulings r WHERE r.document_id = d.id)
    OR EXISTS (
        SELECT 1 FROM dedup m
        JOIN derived.rulings r
          ON r.case_id = m.case_id AND r.ruling_text_hash = m.text_hash
        WHERE m.loser_id = d.id::text
    )
    OR EXISTS (
        SELECT 1 FROM dedup m
        JOIN derived.rulings r
          ON r.document_id = d.previous_version_id
         AND r.ruling_text_hash = m.text_hash
        WHERE m.loser_id = d.id::text
    )
    OR (
        NOT EXISTS (SELECT 1 FROM dedup m WHERE m.loser_id = d.id::text)
        AND EXISTS (
            SELECT 1 FROM derived.rulings r
            WHERE r.document_id = d.previous_version_id
        )
    )
)
"""


def fetch_orphaned_superseded(conn: Any) -> set[str]:
    """:data:`ORPHANED_SUPERSEDED_SQL`; empty (with a warning) when it
    cannot run, so the reconciler still reports everything else."""
    try:
        with conn.cursor() as cur:
            return _fetch_set(cur, ORPHANED_SUPERSEDED_SQL)
    except Exception as exc:  # noqa: BLE001 — degrade to the pre-#5093 view
        conn.rollback()
        print(f"WARNING orphaned superseded keys unreadable ({exc}); none used")
        return set()


def raw_missing(s3: Any, bucket: str, key: str) -> bool:
    """``True`` only when a ``HEAD`` of *key* says the object does not
    exist; any other error (throttling, access) is not proof (#5093)."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 — classified by its error code
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in _MISSING_CODES
    return False


def in_listing_scope(
    key: str,
    *,
    prefix: str,
    exclude: Sequence[str],
    counties: set[str] | None,
) -> bool:
    """Whether *key* is inside what this run listed (``--prefix``,
    ``--exclude-prefix``, ``--county``)."""
    if prefix:
        if not key.startswith(prefix):
            return False
    elif any(key.startswith(e) for e in exclude):
        return False
    if counties:
        m = RAW_SEGMENT_RE.match(key)
        parts = key.split("/")
        if not m or parts[1] not in counties:
            return False
    return True


def close_out_raw_missing(conn: Any, keys: Sequence[str]) -> None:
    """Move the incomplete records of *keys*, whose raw objects are gone, to
    ``telemetry.ingest_skips`` with reason ``raw_missing`` (#5093): the
    incomplete reason, detail and attempts go in the skip's detail.  Never
    retried again, and reported once."""
    if not keys:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO telemetry.ingest_skips
                (s3_key, reason, county, scraper_id, document_id, detail)
            SELECT s3_key, %s, county, scraper_id, document_id,
                   jsonb_build_object(
                       'incomplete_reason', reason,
                       'incomplete_detail', detail,
                       'retry_attempts', retry_attempts,
                       'first_recorded_at', first_recorded_at,
                       'closed_by', 'reconcile_raw_keys'
                   )
            FROM telemetry.ingest_incomplete
            WHERE s3_key = ANY(%s)
            ON CONFLICT (s3_key) DO UPDATE SET
                reason = EXCLUDED.reason,
                detail = EXCLUDED.detail,
                last_recorded_at = now(),
                occurrences = telemetry.ingest_skips.occurrences + 1
            """,
            (RAW_MISSING, list(keys)),
        )
        cur.execute(
            "DELETE FROM telemetry.ingest_incomplete WHERE s3_key = ANY(%s)",
            (list(keys),),
        )
    conn.commit()


def fetch_explained(conn: Any) -> Explained:
    """Every s3_key the database accounts for, from all four sources, and
    the keys whose rows hold no reachable ruling (#5093)."""
    explained = Explained()
    with conn.cursor() as cur:
        explained.documents = _fetch_set(
            cur,
            "SELECT DISTINCT s3_key FROM derived.documents WHERE s3_key IS NOT NULL",
        )
        explained.all_null_guard = _fetch_set(
            cur,
            "SELECT DISTINCT metadata->>'s3_key' FROM telemetry.data_quality_metrics "
            f"WHERE metric_name = '{NON_RULING_PDF_DROPPED_METRIC}' "
            "AND metadata->>'s3_key' IS NOT NULL",
        )
    explained.orphaned_superseded = fetch_orphaned_superseded(conn)
    _fetch_validation_fails(conn, explained)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT s3_key, reason FROM telemetry.ingest_skips")
            explained.skips = {row[0]: row[1] for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 — table not migrated yet
        conn.rollback()
        print(
            f"WARNING telemetry.ingest_skips unreadable ({exc}); no skip records used"
        )
    else:
        if any(r in EXTRACTION_SKIP_REASONS for r in explained.skips.values()):
            _fetch_skip_details(conn, explained)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s3_key, reason, retry_attempts FROM telemetry.ingest_incomplete"
            )
            explained.incomplete = {
                row[0]: (str(row[1]), int(row[2] or 0)) for row in cur.fetchall()
            }
    except Exception as exc:  # noqa: BLE001 — table not migrated yet
        conn.rollback()
        print(
            f"WARNING telemetry.ingest_incomplete unreadable ({exc}); "
            "no incomplete records used"
        )
    return explained


def count_incomplete_attempts(conn: Any, keys: Sequence[str]) -> None:
    """Count one reingest attempt for each incomplete key about to be
    re-ingested (#5008), committed before the launch so a crash still
    counts (#5054).  A complete reingest deletes the record (the worker),
    so the count only survives for a key that stays incomplete."""
    if not keys:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE telemetry.ingest_incomplete "
            "SET retry_attempts = retry_attempts + 1, last_retry_at = now() "
            "WHERE s3_key = ANY(%s)",
            (list(keys),),
        )
    conn.commit()


def refund_incomplete_attempts(conn: Any, keys: Sequence[str]) -> None:
    """Take back the attempt :func:`count_incomplete_attempts` counted for
    *keys* when their reingest could not be launched at all (#5054)."""
    if not keys:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE telemetry.ingest_incomplete "
            "SET retry_attempts = GREATEST(retry_attempts - 1, 0) "
            "WHERE s3_key = ANY(%s)",
            (list(keys),),
        )
    conn.commit()


def mark_newly_stuck(conn: Any, keys: Sequence[str]) -> list[str]:
    """Stamp ``stuck_at`` on the stuck keys not stamped yet; return them.

    The issue is filed or updated once when keys become stuck, not on every
    run they stay stuck (#5008).
    """
    if not keys:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE telemetry.ingest_incomplete SET stuck_at = now() "
            "WHERE s3_key = ANY(%s) AND stuck_at IS NULL RETURNING s3_key",
            (list(keys),),
        )
        newly = [row[0] for row in cur.fetchall()]
    conn.commit()
    return newly


def reset_incomplete_attempts(conn: Any, max_attempts: int) -> int:
    """Make every stuck incomplete key (at *max_attempts*, or stamped
    ``stuck_at``) retryable again after a fix (e.g. #5007); return how many
    were reset.  Keys still under the cap keep their count."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE telemetry.ingest_incomplete "
            "SET retry_attempts = 0, stuck_at = NULL "
            "WHERE retry_attempts >= %s OR stuck_at IS NOT NULL RETURNING s3_key",
            (max_attempts,),
        )
        n = len(cur.fetchall())
    conn.commit()
    return n


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


def fetch_recent_scope_actions(conn: Any, n: int) -> list[dict[str, str]]:
    """Each of the last *n* recorded runs' per-scope actions
    (``detail.scope_actions``, #5036), newest first; ``{}`` for a run
    recorded before scopes had their own action."""
    if n <= 0:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail->'scope_actions' FROM telemetry.raw_reconcile_runs "
            "ORDER BY started_at DESC LIMIT %s",
            (n,),
        )
        rows = cur.fetchall()
    out: list[dict[str, str]] = []
    for row in rows:
        value = row[0]
        if isinstance(value, str):
            value = json.loads(value)
        out.append(
            {str(k): str(v) for k, v in value.items()}
            if isinstance(value, dict)
            else {}
        )
    return out


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
                 pending, dated_excluded, action, key_list_uri, detail,
                 incomplete, incomplete_stuck)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
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
                total["incomplete"],
                total["incomplete_stuck"],
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
                     pending, dated_excluded, skipped_by_reason, unrepresented_by_day,
                     incomplete, incomplete_stuck)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
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
                    s["incomplete"],
                    s["incomplete_stuck"],
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


def split_deferred(
    reports: dict[str, ScopeReport], backlogs: Sequence[DeferredBacklog]
) -> tuple[dict[str, list[RawKey]], dict[str, Counter[str]]]:
    """The unrepresented keys per scope that the run acts on, and the
    deferred ones counted per backlog name and scope (#5036).  A key goes to
    the first backlog that matches it."""
    actionable: dict[str, list[RawKey]] = {}
    deferred: dict[str, Counter[str]] = {}
    for scope in sorted(reports):
        for raw in reports[scope].unrepresented:
            backlog = next((b for b in backlogs if b.matches(raw)), None)
            if backlog is None:
                actionable.setdefault(scope, []).append(raw)
            else:
                deferred.setdefault(backlog.name, Counter())[scope] += 1
    return actionable, deferred


def deferred_report(
    backlogs: Sequence[DeferredBacklog], deferred: dict[str, Counter[str]]
) -> list[dict[str, Any]]:
    """One entry per backlog with deferred keys this run: its blocking
    issue, key and scope counts, and its ten largest scopes."""
    out: list[dict[str, Any]] = []
    for backlog in backlogs:
        counts = deferred.get(backlog.name)
        if not counts:
            continue
        top = sorted(counts.items(), key=lambda t: (-t[1], t[0]))
        out.append(
            {
                "name": backlog.name,
                "issue": backlog.issue,
                "label": backlog.label,
                "keys": sum(counts.values()),
                "scope_count": len(counts),
                "top_scopes": [[s, n] for s, n in top[:10]],
            }
        )
    return out


def decide_scope_action(
    unrepresented: int,
    previous: Sequence[str | None],
    *,
    max_auto_reingest: int,
    persist_runs: int,
    reingest: bool,
    defer: bool,
) -> str:
    """What to do about one scope's *unrepresented* (non-deferred) keys,
    given the scope's action in each previous recorded run, newest first
    (``None``: the scope had nothing to act on in that run).

    The threshold is per scope (#5036): a county with a dozen lost keys
    heals while another scope's backlog waits for a human.

    ``persistent`` still re-ingests (new losses keep healing), and also
    files the issue: the scope was re-ingested in each of the previous
    ``persist_runs - 1`` runs and its keys are back.  Runs that did not
    re-ingest the scope (deferred, report-only, over the threshold, nothing
    to do) do not count as a failed heal.
    """
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
        and all(action in REINGEST_ACTIONS for action in earlier)
    ):
        return "persistent"
    return "reingest"


def decide_scope_actions(
    actionable: dict[str, list[RawKey]],
    previous: Sequence[dict[str, str]],
    *,
    max_auto_reingest: int,
    persist_runs: int,
    reingest: bool,
    defer: bool,
) -> dict[str, str]:
    """:func:`decide_scope_action` for every scope with keys to act on;
    *previous* is each earlier run's ``scope_actions``, newest first."""
    return {
        scope: decide_scope_action(
            len(keys),
            [run.get(scope) for run in previous],
            max_auto_reingest=max_auto_reingest,
            persist_runs=persist_runs,
            reingest=reingest,
            defer=defer,
        )
        for scope, keys in sorted(actionable.items())
        if keys
    }


def mark_scopes_failed(scope_actions: dict[str, str], healing: Iterable[str]) -> None:
    """The reingest could not be launched: the healing scopes were not
    re-ingested, so a later run must not count this one as a heal attempt
    (``persistent``)."""
    for scope in healing:
        scope_actions[scope] = "reingest_failed"


def overall_action(
    scope_actions: dict[str, str], *, incomplete: int, reingest: bool, defer: bool
) -> str:
    """The run's action from its per-scope actions and its *incomplete*
    batch size (#5008, #5036).

    Any scope healing makes the run ``reingest`` (``persistent`` when one
    of them is).  Otherwise a scope over the threshold makes it
    ``over_threshold`` (the incomplete batch still re-ingests on its own).
    The incomplete batch alone is a ``reingest``.
    """
    if not scope_actions and incomplete == 0:
        return "none"
    acts = set(scope_actions.values())
    if acts & REINGEST_ACTIONS:
        return "persistent" if "persistent" in acts else "reingest"
    if "over_threshold" in acts:
        return "over_threshold"
    if not reingest:
        return "report_only"
    if defer:
        return "reingest_deferred"
    return "reingest"


def incomplete_batch(
    reports: dict[str, ScopeReport],
    unrepresented: int,
    max_auto_reingest: int,
    backlogs: Sequence[DeferredBacklog] = (),
) -> list[RawKey]:
    """The incomplete (retryable) keys this run re-ingests, oldest first
    (#5008): as many as fit beside the *unrepresented* keys being re-ingested
    under *max_auto_reingest*, or, when that count is over it, up to
    *max_auto_reingest* on their own.  An
    incomplete key is a known key with a recorded reason and an attempt
    cap, so it keeps draining while a lost-key backlog waits for a human.
    The rest wait for the next run.  Keys of a deferred backlog are left
    out: ``reingest_from_s3.py`` would skip them (#5043), and their retry
    attempts must not count."""
    if unrepresented > max_auto_reingest:
        room = max_auto_reingest
    else:
        room = max(0, max_auto_reingest - unrepresented)
    keys = sorted(
        (
            raw
            for rep in reports.values()
            for raw in rep.incomplete
            if not any(b.matches(raw) for b in backlogs)
        ),
        key=lambda k: (k.utc, k.key),
    )
    return keys[:room]


def reingest_groups(
    reports: dict[str, ScopeReport], incomplete: Sequence[RawKey] = ()
) -> dict[str, list[str]]:
    """Unrepresented keys, then the run's *incomplete* batch, grouped by
    top-level ``<state>/`` prefix."""
    groups: dict[str, list[str]] = {}
    for scope in sorted(reports):
        for raw in reports[scope].unrepresented:
            groups.setdefault(f"{raw.state}/", []).append(raw.key)
    for raw in incomplete:
        groups.setdefault(f"{raw.state}/", []).append(raw.key)
    return groups


def find_reingest_script(explicit: str | None) -> str:
    candidates = [explicit] if explicit else list(DEFAULT_REINGEST_SCRIPTS)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(f"reingest_from_s3.py not found (tried {candidates})")


def check_reingest_cli(script: str) -> None:
    """Raise :class:`ReingestCliMismatch` when *script* does not declare
    every flag in :data:`REINGEST_CLI_FLAGS` (#5060).

    Reads the source instead of running ``--help``: no subprocess, no
    import cost, and each flag is declared as a string literal in its
    ``add_argument`` call.
    """
    with open(script, encoding="utf-8") as fh:
        text = fh.read()
    missing = [f for f in REINGEST_CLI_FLAGS if f'"{f}"' not in text]
    if missing:
        raise ReingestCliMismatch(
            f"{script} does not accept {', '.join(missing)}: the scraper image's "
            "reingest CLI is older than this reconciler. Rebuild the image "
            "(run deploy-scraper.yml), then re-run the reconciler."
        )


def split_isolated(
    batch: Sequence[RawKey], explained: Explained
) -> tuple[list[RawKey], list[RawKey]]:
    """The incomplete *batch* split into keys that share their prefix's
    reingest and keys that run alone (:data:`ISOLATED_REASONS`, #5054)."""
    batched: list[RawKey] = []
    isolated: list[RawKey] = []
    for raw in batch:
        reason = explained.incomplete.get(raw.key, ("", 0))[0]
        (isolated if reason in ISOLATED_REASONS else batched).append(raw)
    return batched, isolated


def split_bust_cache(
    batch: Sequence[RawKey], explained: Explained
) -> tuple[list[RawKey], list[RawKey]]:
    """The batched incomplete keys split into keys that re-ingest as usual
    and keys that re-ingest with ``--bust-llm-cache``
    (:data:`BUST_CACHE_REASONS`, #5094): a stale extraction skip would
    otherwise replay from the LLM cache."""
    kept: list[RawKey] = []
    busted: list[RawKey] = []
    for raw in batch:
        reason = explained.incomplete.get(raw.key, ("", 0))[0]
        (busted if reason in BUST_CACHE_REASONS else kept).append(raw)
    return kept, busted


def apply_stale_skips(
    reports: dict[str, ScopeReport],
    explained: Explained,
    conn: Any,
    out: Callable[[str], None],
    sample: int,
) -> tuple[list[tuple[RawKey, str]], list[str]]:
    """Fold this run's stale extraction skips into the incomplete keys
    (#5094): new ones are recorded (with ``--record``) so their reingest
    counts attempts against the cap; resolved records go."""
    new = [item for rep in reports.values() for item in rep.new_stale_skips]
    resolved = [raw.key for rep in reports.values() for raw in rep.resolved_stale_skips]
    for raw, _why in new:
        explained.incomplete[raw.key] = (STALE_SKIP, 0)
    for key in resolved:
        explained.incomplete.pop(key, None)
    if conn is not None:
        record_stale_skips(conn, new)
        clear_stale_skips(conn, resolved)
    for raw, why in new[:sample]:
        out(
            f"STALE_SKIP {raw.scope} skip={explained.skips.get(raw.key)} "
            f"stale={why} key={raw.key}"
        )
    return new, resolved


def read_keys(s3: Any, uri: str) -> set[str]:
    """The keys listed one per line at *uri* (``s3://`` or local); empty when
    it was never written (the reingest writes a manifest only when it has
    keys to list)."""
    try:
        if uri.startswith("s3://"):
            bucket, _, key = uri[len("s3://") :].partition("/")
            text = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        else:
            with open(uri) as fh:
                text = fh.read()
    except Exception:  # noqa: BLE001 — no manifest: nothing to list
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _prefer_oom_kill() -> None:
    """Subprocess pre-exec: make the reingest, and the pool workers that
    inherit the setting, the OOM killer's first choice in the task's memory
    cgroup, so a document that exhausts memory kills the reingest rather
    than this process, which then records the outcome (#5054).  Raising
    one's own score needs no privilege; a non-Linux host has no file."""
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass


def _no_db(keys: list[str]) -> None:
    return None


@dataclass
class Reingester:
    """Runs ``reingest_from_s3.py`` over key lists, isolating the keys that
    crash it (#5054, see "Crash isolation" in the module docstring).

    *charge* counts one attempt for incomplete keys (*chargeable*) before
    the first launch that holds them in this run; *refund* takes it back
    when the launch could not start.
    """

    args: argparse.Namespace
    s3: Any
    script: str
    stamp: str
    out: Callable[[str], None]
    runner: Callable[..., Any] = subprocess.run
    charge: Callable[[list[str]], None] = _no_db
    refund: Callable[[list[str]], None] = _no_db
    chargeable: frozenset[str] = frozenset()
    simulate_crash: frozenset[str] = frozenset()
    #: One entry per launch: prefix, key count, key list, exit (``crashed``:
    #: keys of an exit-0 launch whose worker process died).
    ran: list[dict[str, Any]] = field(default_factory=list)
    #: Keys that crashed the reingest alone.
    crashed: list[str] = field(default_factory=list)
    charged: set[str] = field(default_factory=set)
    _seq: int = 0

    def launch(
        self,
        prefix: str,
        keys: list[str],
        *,
        primary: bool = False,
        bust: bool = False,
    ) -> tuple[Any, set[str]]:
        """Re-ingest *keys* under *prefix* once; return the exit code and
        the keys a crash implicated (every key on a non-zero exit).  With
        *bust*, the reingest skips LLM cache reads (``--bust-llm-cache``,
        #5094)."""
        state = _slug(keys[0].split("/", 1)[0])
        if primary:
            name = state
        else:
            self._seq += 1
            name = f"{state}-{self._seq:03d}"
        base = (
            f"s3://{self.args.bucket}/{KEY_LIST_DIR}raw-reconcile-{self.stamp}-{name}"
        )
        uri = f"{base}.txt"
        crashed_uri = f"{base}-crashed.txt"
        write_text(self.s3, uri, "".join(f"{k}\n" for k in keys))
        cmd = [
            sys.executable,
            self.script,
            "--prefix",
            prefix,
            "--s3-key-list",
            uri,
            "--concurrency",
            str(self.args.reingest_concurrency if len(keys) > 1 else 1),
            "--write-failed-manifest",
            f"{base}-failed.txt",
            "--write-crashed-manifest",
            crashed_uri,
            "--no-fail-on-errors",
        ]
        if bust:
            cmd.append("--bust-llm-cache")
        simulated = sorted(self.simulate_crash.intersection(keys))
        if simulated:
            cmd = [sys.executable, *SIMULATED_CRASH_ARGS]
            self.out(f"REINGEST_SIMULATED_CRASH keys={','.join(simulated)}")
        to_charge = [k for k in keys if k in self.chargeable and k not in self.charged]
        if to_charge:
            self.charge(to_charge)
            self.charged.update(to_charge)
        self.out(f"REINGEST_START prefix={prefix} keys={len(keys)} list={uri}")
        try:
            result = self.runner(cmd, check=False, preexec_fn=_prefer_oom_kill)
        except Exception:
            # Never started: not an attempt.
            if to_charge:
                self.refund(to_charge)
                self.charged.difference_update(to_charge)
            raise
        code = getattr(result, "returncode", None)
        crashed = (
            read_keys(self.s3, crashed_uri).intersection(keys)
            if code == 0
            else set(keys)
        )
        self.out(f"REINGEST_DONE prefix={prefix} exit={code} crashed={len(crashed)}")
        entry: dict[str, Any] = {
            "prefix": prefix,
            "keys": len(keys),
            "key_list": uri,
            "exit": code,
        }
        if bust:
            entry["bust_llm_cache"] = True
        if code == 0 and crashed:
            entry["crashed"] = len(crashed)
        self.ran.append(entry)
        return code, crashed

    def heal(
        self,
        prefix: str,
        keys: list[str],
        *,
        primary: bool = False,
        bust: bool = False,
    ) -> None:
        """:meth:`launch`, then bisect the keys a crash implicated until each
        crashing key is alone."""
        code, crashed = self.launch(prefix, keys, primary=primary, bust=bust)
        if not crashed:
            return
        if len(keys) == 1:
            self.crashed.append(keys[0])
            self.out(f"REINGEST_CRASH exit={code} key={keys[0]}")
            return
        implicated = [k for k in keys if k in crashed]
        if len(implicated) == 1:
            self.heal(implicated[0], implicated, bust=bust)
            return
        mid = len(implicated) // 2
        for half in (implicated[:mid], implicated[mid:]):
            self.heal(os.path.commonprefix(half), half, bust=bust)


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
    deferrals: Sequence[DeferredBacklog] | None = None,
) -> dict[str, Any]:
    """One reconcile pass; *deferrals* defaults to :data:`DEFERRED_BACKLOGS`."""
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
        "validation_rows="
        f"{sum(len(v) for v in explained.validation_rows.values())}"
        f"+{sum(len(v) for v in explained.validation_rows_by_document.values())} "
        f"all_null_guard={len(explained.all_null_guard)} "
        f"incomplete={len(explained.incomplete)}"
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
        max_incomplete_attempts=args.max_incomplete_attempts,
    )

    # Keys pending re-validation under a relaxed rule (#5027) join the
    # incomplete keys: recorded (with --record) so the reingest counts its
    # attempts against the cap.  Resolved records go.
    # Orphaned superseded keys (#5093) are recorded the same way, except
    # deferred backlog keys: they are never re-ingested, so no record.
    backlogs = DEFERRED_BACKLOGS if deferrals is None else tuple(deferrals)
    new_stale = [item for rep in reports.values() for item in rep.new_stale]
    resolved_stale = [raw.key for rep in reports.values() for raw in rep.resolved_stale]
    new_orphaned = [raw for rep in reports.values() for raw in rep.new_orphaned]
    resolved_orphaned = [
        raw.key for rep in reports.values() for raw in rep.resolved_orphaned
    ]
    for key in (*resolved_stale, *resolved_orphaned):
        explained.incomplete.pop(key, None)
    for raw, _rules in new_stale:
        explained.incomplete[raw.key] = (STALE_VALIDATION, 0)
    for raw in new_orphaned:
        explained.incomplete[raw.key] = (ORPHANED_SUPERSEDED, 0)
    if conn is not None:
        clear_stale_validation(conn, resolved_stale)
        clear_stale_validation(conn, resolved_orphaned, ORPHANED_SUPERSEDED)
        record_stale_validation(conn, new_stale)
        record_orphaned_superseded(
            conn,
            [raw for raw in new_orphaned if not any(b.matches(raw) for b in backlogs)],
        )
    for raw, rules in new_stale[: args.sample]:
        out(f"STALE_VALIDATION {raw.scope} rules={','.join(rules)} key={raw.key}")
    for raw in new_orphaned[: args.sample]:
        out(f"ORPHANED_SUPERSEDED {raw.scope} day={raw.day} key={raw.key}")
    new_stale_skips, resolved_stale_skips = apply_stale_skips(
        reports, explained, conn, out, args.sample
    )

    # Incomplete records whose raw object is gone can never heal (#5093):
    # reported, and closed out with --record.
    listed = {key for key, _lm in objects}
    counties = set(args.county) or None
    candidates = sorted(
        key
        for key in explained.incomplete
        if key not in listed
        and in_listing_scope(
            key, prefix=args.prefix, exclude=args.exclude_prefix, counties=counties
        )
    )
    orphan_incomplete = [k for k in candidates if raw_missing(s3, args.bucket, k)]
    orphan_by_reason = Counter(explained.incomplete[k][0] for k in orphan_incomplete)
    for key in orphan_incomplete[: args.sample]:
        out(f"ORPHAN_INCOMPLETE reason={explained.incomplete[key][0]} key={key}")
    if conn is not None:
        close_out_raw_missing(conn, orphan_incomplete)
    for key in orphan_incomplete:
        explained.incomplete.pop(key, None)

    for scope in sorted(reports):
        rep = reports[scope]
        out(f"SUMMARY {scope}: {json.dumps(rep.summary(), sort_keys=True)}")
        if rep.unrepresented:
            out(f"UNREPRESENTED_DAYS {scope}: {json.dumps(rep.unrepresented_by_day())}")
        for raw in rep.unrepresented[: args.sample]:
            out(f"UNREPRESENTED {scope} day={raw.day} key={raw.key}")
        for raw in rep.incomplete[: args.sample]:
            reason, attempts = explained.incomplete[raw.key]
            out(f"INCOMPLETE {scope} reason={reason} attempts={attempts} key={raw.key}")
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
    previous_scopes: list[dict[str, str]] = []
    if conn is not None:
        previous = fetch_recent_runs(conn, max(0, args.persist_runs - 1))
        previous_scopes = fetch_recent_scope_actions(
            conn, max(0, args.persist_runs - 1)
        )
    actionable, deferred = split_deferred(reports, backlogs)
    scope_actions = decide_scope_actions(
        actionable,
        previous_scopes,
        max_auto_reingest=args.max_auto_reingest,
        persist_runs=args.persist_runs if conn is not None else 0,
        reingest=args.reingest,
        defer=args.defer_reingest,
    )
    healing = [s for s, a in scope_actions.items() if a in REINGEST_ACTIONS]
    heal_count = sum(len(actionable[s]) for s in healing)
    batch = incomplete_batch(
        reports,
        min(heal_count, args.max_auto_reingest),
        args.max_auto_reingest,
        backlogs,
    )
    action = overall_action(
        scope_actions,
        incomplete=len(batch),
        reingest=args.reingest,
        defer=args.defer_reingest,
    )
    for scope, scope_action in scope_actions.items():
        out(
            f"SCOPE_ACTION {scope} action={scope_action} "
            f"unrepresented={len(actionable[scope])}"
        )
    deferred_detail = deferred_report(backlogs, deferred)
    for d in deferred_detail:
        out(
            f"DEFERRED {d['name']} {d['label']} keys={d['keys']} "
            f"scopes={d['scope_count']}"
        )
    top = sorted(
        ((scope, len(keys)) for scope, keys in actionable.items()),
        key=lambda t: (-t[1], t[0]),
    )
    incomplete_by_reason: Counter[str] = Counter()
    for rep in reports.values():
        incomplete_by_reason.update(rep.incomplete_by_reason)
    stuck_keys = [
        raw.key
        for raw in keys
        if raw.key in explained.incomplete
        and explained.incomplete[raw.key][1] >= args.max_incomplete_attempts
    ]
    newly_stuck = mark_newly_stuck(conn, stuck_keys) if conn is not None else []
    for key in newly_stuck[: args.sample]:
        out(f"INCOMPLETE_STUCK reason={explained.incomplete[key][0]} key={key}")
    detail: dict[str, Any] = {
        "other": dict(sorted(other.items())),
        "previous_unrepresented": [n for n, _ in previous],
        "previous_actions": [a for _, a in previous],
        "top_scopes": [[s, n] for s, n in top if n > 0][:10],
        # Per-scope threshold and deferred backlogs (#5036).
        "scope_actions": scope_actions,
        "scope_unrepresented": {s: len(actionable[s]) for s in scope_actions},
        "actionable_unrepresented": sum(len(k) for k in actionable.values()),
        "deferred": sum(d["keys"] for d in deferred_detail),
        "deferred_backlogs": deferred_detail,
        "max_auto_reingest": args.max_auto_reingest,
        "max_incomplete_attempts": args.max_incomplete_attempts,
        "incomplete_by_reason": dict(sorted(incomplete_by_reason.items())),
        "incomplete_batch": len(batch),
        "newly_stuck": len(newly_stuck),
        "newly_stuck_sample": newly_stuck[:10],
        "stale_validation_new": len(new_stale),
        "stale_validation_resolved": len(resolved_stale),
        # Superseded-only keys with no reachable ruling (#5093).
        "orphaned_superseded": sum(
            1 for raw in keys if raw.key in explained.orphaned_superseded
        ),
        "orphaned_superseded_new": len(new_orphaned),
        "orphaned_superseded_resolved": len(resolved_orphaned),
        # Incomplete records whose raw object is gone (#5093).
        "orphan_incomplete": len(orphan_incomplete),
        "orphan_incomplete_by_reason": dict(sorted(orphan_by_reason.items())),
        "orphan_incomplete_sample": orphan_incomplete[:10],
        "orphan_incomplete_closed": len(orphan_incomplete) if conn is not None else 0,
        # Extraction skip verdicts re-judged (#5094).
        "stale_skip_new": len(new_stale_skips),
        "stale_skip_resolved": len(resolved_stale_skips),
    }

    script = None
    # Over the threshold the unrepresented keys wait for a human (the issue),
    # but the incomplete batch still re-ingests on its own (#5008).
    incomplete_only = (
        action == "over_threshold"
        and bool(batch)
        and args.reingest
        and not args.defer_reingest
        and conn is not None
    )
    if action in REINGEST_ACTIONS or incomplete_only:
        try:
            script = find_reingest_script(args.reingest_script)
            check_reingest_cli(script)
        except (FileNotFoundError, ReingestCliMismatch) as exc:
            script = None
            if not incomplete_only:
                action = "reingest_failed"
                mark_scopes_failed(scope_actions, healing)
            detail["reingest_error"] = str(exc)
    if incomplete_only:
        detail["reingest_scope"] = "incomplete_only"

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
        # Each launch counts an attempt for its incomplete keys before it
        # starts, whatever its exit (#5054); keys that crash are bisected
        # out so the rest of the run still heals.  Past the cap a key is
        # reported as stuck (#5008).
        batched, isolated = split_isolated(batch, explained)
        batched, busted = split_bust_cache(batched, explained)
        reingester = Reingester(
            args,
            s3,
            script,
            started_at.strftime("%Y%m%dT%H%MZ"),
            out,
            runner=runner,
            charge=(lambda keys: count_incomplete_attempts(conn, keys))
            if conn is not None
            else _no_db,
            refund=(lambda keys: refund_incomplete_attempts(conn, keys))
            if conn is not None
            else _no_db,
            chargeable=frozenset(raw.key for raw in batch),
            simulate_crash=frozenset(args.simulate_crash_key),
        )
        try:
            groups = reingest_groups(
                {
                    s: ScopeReport(s, unrepresented=actionable[s])
                    for s in ([] if incomplete_only else healing)
                },
                batched,
            )
            for prefix, group in groups.items():
                reingester.heal(prefix, group, primary=True)
            for raw in isolated:
                reingester.heal(raw.key, [raw.key])
            for prefix, group in reingest_groups({}, busted).items():
                reingester.heal(prefix, group, bust=True)
        except Exception as exc:  # noqa: BLE001 — recorded, then reported as failed
            if not incomplete_only:
                action = "reingest_failed"
                mark_scopes_failed(scope_actions, healing)
            detail["reingest_error"] = f"{type(exc).__name__}: {exc}"
        detail["reingest"] = reingester.ran
        detail["reingest_isolated"] = len(isolated)
        detail["reingest_bust_cache"] = len(busted)
        detail["reingest_crashed"] = len(reingester.crashed)
        detail["reingest_crashed_sample"] = reingester.crashed[:10]
        if args.simulate_crash_key:
            detail["simulated_crash_keys"] = list(args.simulate_crash_key)
        if conn is not None and run_id is not None:
            update_run(conn, run_id, action, detail)

    result = {
        "run_id": run_id,
        "action": action,
        # Keys that just reached the attempt cap are reported once (#5008);
        # a scope over the threshold or not healing files it even while
        # other scopes heal (#5036).
        "issue_needed": action in ISSUE_ACTIONS
        or bool(set(scope_actions.values()) & ISSUE_ACTIONS)
        or bool(newly_stuck)
        # A lost raw object is irrecoverable; closed out, so reported once.
        or bool(orphan_incomplete),
        **total,
        **detail,
    }
    out(f"RECONCILE_RESULT {json.dumps(result, sort_keys=True, default=str)}")
    return {"reports": reports, "result": result}


def issue_summary(result: dict[str, Any]) -> str:
    """The Markdown the workflow posts on the reconciler issue (and the job
    summary) for one ``RECONCILE_RESULT`` (#5036): totals, the action per
    scope, and the deferred backlogs on their own lines."""
    g = result.get
    unrep = g("unrepresented", 0)
    actionable = g("actionable_unrepresented", unrep)
    cap = g("max_incomplete_attempts", DEFAULT_MAX_INCOMPLETE_ATTEMPTS)
    reasons = json.dumps(g("incomplete_by_reason") or {}, sort_keys=True)
    lines = [
        f"**Action:** `{g('action')}`",
        (
            f"**Unrepresented:** {actionable} to act on, of {g('raw', 0)} raw keys "
            f"({unrep} including deferred backlogs)"
        ),
        (
            f"(represented {g('represented', 0)}, skipped {g('skipped', 0)}, "
            f"pending {g('pending', 0)})"
        ),
        (
            f"**Incomplete (rows not a complete split, #5008):** "
            f"{g('incomplete', 0)} retrying, {g('incomplete_stuck', 0)} stuck at "
            f"{cap} attempts ({g('newly_stuck', 0)} newly stuck); by reason {reasons}"
        ),
    ]
    if g("newly_stuck"):
        lines.append(
            f"Newly stuck (sample): {', '.join(g('newly_stuck_sample') or [])}"
        )
    if g("orphaned_superseded"):
        lines.append(
            f"**Superseded with no reachable ruling (#5093):** "
            f"{g('orphaned_superseded')} keys ({g('orphaned_superseded_new', 0)} new, "
            f"{g('orphaned_superseded_resolved', 0)} healed since the last run); "
            "retried as incomplete `orphaned_superseded`"
        )
    if g("orphan_incomplete"):
        by_reason = json.dumps(g("orphan_incomplete_by_reason") or {}, sort_keys=True)
        lines.append(
            f"**Raw object missing (#5093):** {g('orphan_incomplete')} incomplete "
            f"keys whose raw is gone from the archive, by reason {by_reason}; "
            f"{g('orphan_incomplete_closed', 0)} closed out as skip `raw_missing` "
            f"(sample: {', '.join(g('orphan_incomplete_sample') or [])})"
        )
    lines.append(
        "**Previous runs (unrepresented, newest first):** "
        f"{json.dumps(g('previous_unrepresented') or [])}"
    )
    for d in g("deferred_backlogs") or []:
        scopes = ", ".join(f"{s} {n}" for s, n in d.get("top_scopes", []))
        lines.append(
            f"**Deferred (blocked on #{d['issue']}):** {d['keys']} keys in "
            f"{d['scope_count']} scopes, not re-ingested and not counted toward "
            f"the threshold (largest: {scopes})"
        )
    scope_actions = g("scope_actions") or {}
    counts = g("scope_unrepresented") or dict(g("top_scopes") or [])
    lines += ["", f"Threshold: {g('max_auto_reingest')} keys per scope.", ""]
    if scope_actions:
        lines += ["| Scope | Unrepresented | Action |", "|---|---|---|"]
        order = sorted(scope_actions, key=lambda s: (-counts.get(s, 0), s))
        for scope in order:
            n = counts.get(scope, "")
            lines.append(f"| {scope} | {n} | `{scope_actions[scope]}` |")
    else:
        lines.append("No scope has unrepresented keys to act on.")
    return "\n".join(lines) + "\n"


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
    parser.add_argument(
        "--max-incomplete-attempts",
        type=int,
        default=DEFAULT_MAX_INCOMPLETE_ATTEMPTS,
        help="Reingest attempts after which an incomplete key is reported as stuck.",
    )
    parser.add_argument(
        "--simulate-crash-key",
        action="append",
        default=[],
        metavar="KEY",
        help="Manual verification only (#5054): any reingest launch holding KEY "
        "runs a subprocess that SIGKILLs itself instead.  Never set by the "
        "scheduled workflow.",
    )
    parser.add_argument(
        "--issue-summary",
        metavar="RESULT_JSON",
        help="Print the issue Markdown for a saved RECONCILE_RESULT JSON and exit "
        "(the workflow; no database or S3).",
    )
    parser.add_argument(
        "--reset-incomplete-attempts",
        action="store_true",
        help="Make every stuck incomplete key retryable again (after a fix) before the run.",
    )
    args = parser.parse_args(argv)
    if args.since and args.until and args.since > args.until:
        parser.error("--since is after --until")
    if args.prefix and not args.prefix.endswith("/"):
        parser.error("--prefix must end with /")
    if args.reingest and not args.record:
        parser.error("--reingest needs --record (the threshold reads the run history)")
    if args.simulate_crash_key and not args.reingest:
        parser.error("--simulate-crash-key needs --reingest (it replaces a launch)")
    if args.reset_incomplete_attempts and not args.record:
        parser.error(
            "--reset-incomplete-attempts needs --record (it writes the database)"
        )
    if args.record and (args.prefix or args.county or args.since or args.until):
        parser.error(
            "--record is for full runs only (no --prefix/--county/--since/--until)"
        )
    if args.exclude_prefix is None:
        args.exclude_prefix = list(DEFAULT_EXCLUDE_PREFIXES)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.issue_summary:
        with open(args.issue_summary) as fh:
            print(issue_summary(json.load(fh)), end="")
        return 0

    import boto3
    import psycopg

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    s3 = boto3.client("s3")
    with psycopg.connect(database_url) as conn:
        if args.reset_incomplete_attempts:
            n = reset_incomplete_attempts(conn, args.max_incomplete_attempts)
            print(f"RESET_INCOMPLETE_ATTEMPTS {n}", flush=True)
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
