#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Drain splitter-carry-forward clusters in derived.* (#4321).

When a new pre-LLM splitter ships for a CA county that previously relied on
the all-in-one LLM extraction path (Riverside #3649, Fresno #3534, SF #4304,
Santa Clara #4303), the post-deploy verification step needs to re-run the
new splitter against EXISTING multi-case documents that were originally
split by the legacy LLM-only path. The legacy path produced
``all_same_case_title_cluster`` rows in
``scripts/audit_llm_carry_forward.py``: same ``documents.s3_key`` →
N>=2 rulings, all sharing one ``case_title`` (the LLM violated rule 5b of
its own prompt and copied page-1 metadata onto every entry).

The standard reingest CLI cannot drain those clusters because
``scripts/reingest_from_s3.py`` ``_full_reparse_document`` skips split
children at line 2580 (``is_split_child_id`` guard, added in #2416 to
prevent N×R Cartesian explosions). Without this script, every new splitter
PR has to file a follow-up issue (#4320 was the trigger filing) to manually
restore parent doc rows so the new splitter can re-run.

This script does that work generically:

  1. Identifies clusters using the same query shape as the audit's
     ``_CLUSTER_QUERY``: ``(d.s3_key, c.case_title)`` tuples with
     ruling_count >= 2 and exactly one distinct case_title.
  2. For each cluster, in a single transaction with ``SELECT ... FOR
     UPDATE`` row locks:
       a. Fetches the parent PDF from S3 using the cluster's ``s3_key`` /
          ``s3_bucket`` (shared by every child) and computes the *real*
          parent ``content_hash = sha256(raw_bytes)``.
       b. Runs the registered ``_split_rulings()`` to confirm the new
          splitter produces N>=2 distinct ``case_title`` values. If not
          (e.g. the splitter still folds the PDF into one entry), the
          cluster is skipped and logged — draining wouldn't help.
       c. Deletes the existing children's ``derived.rulings`` and
          ``derived.documents`` rows (cluster-scoped: same ``s3_key`` AND
          ``status = 'active'``).
       d. UPSERTs a parent doc row keyed on
          ``derive_parent_document_id(content_hash)`` (the v5-from-content_hash
          form ``BaseScraper._process_document`` uses, NOT the v5-from-
          (parent,index) form ``is_split_child_id`` matches against).
  3. After the cluster restoration sweep, dispatches one
     ``run_reingest(..., full_reparse=True, bust_llm_cache=True)`` call
     for the affected county. ``--full-reparse`` re-runs the registered
     splitter against the freshly-restored parent rows; the line-2580
     guard correctly does NOT skip them because their IDs match
     ``derive_parent_document_id(content_hash)``.

Idempotency
-----------
After a successful drain + reingest, the cluster query returns 0 rows,
so re-running this script is a no-op. The only DB writes are the
delete-then-restore inside step 2 and the reingest writes inside step 3,
both of which are upserts against deterministic IDs. Re-running is safe.

Usage
-----
::

    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
        scripts/drain_splitter_carry_forward_clusters.py \\
        --county "Santa Clara"

Run via ``scripts/ecs-run-task.sh`` against dev for the post-deploy
verification of any new splitter PR.

Acceptance criteria — directly satisfies AC #1-#4 of issue #4321:

  * AC #1 — script exists, ``--county <C>`` and ``--dry-run`` flags.
  * AC #2 — ``--county "Santa Clara"`` drains the SC cluster bucket.
  * AC #3 — ``--county "San Francisco"`` drains the analogous SF clusters.
  * AC #4 — ``SELECT ... FOR UPDATE`` row locks prevent races with the
    live ingestion worker.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

# psycopg is imported lazily inside ``_load_psycopg`` so this module — and
# its dataclass / SQL primitives — can be imported in a test environment
# that doesn't have psycopg installed (mirrors the audit script's pattern).
psycopg: Any = None

# Make the scraper-framework src importable when running standalone (not
# via ``packages/scraper-framework/.venv/bin/python3``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRAPER_SRC = _REPO_ROOT / "packages" / "scraper-framework" / "src"
if str(_SCRAPER_SRC) not in sys.path:
    sys.path.insert(0, str(_SCRAPER_SRC))

# When run via ``scripts/ecs-run-task.sh``, the script is uploaded to
# ``/tmp/_oneshot_script`` and __file__ resolves there — but its sibling
# ``reingest_from_s3.py`` lives at ``/app/scripts/`` (baked into the
# ingestion-worker image; see ``packages/scraper-framework/Dockerfile``).
# Add ``/app/scripts`` to sys.path so the lazy ``import reingest_from_s3``
# inside ``resolve_split_fn`` / ``extract_pdf_text`` /
# ``_maybe_invoke_reingest`` resolves at runtime. No-op outside ECS (the
# directory simply doesn't exist on a developer laptop or CI runner).
_ECS_SCRIPTS_DIR = Path("/app/scripts")
if _ECS_SCRIPTS_DIR.is_dir() and str(_ECS_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ECS_SCRIPTS_DIR))

# Always include the directory the file lives in (for the developer-laptop
# pytest path where __file__ is in scripts/) — Python normally adds this
# automatically, but ECS runs the script from /tmp/_oneshot_script where
# adding /tmp doesn't help, and the ``Path(__file__).resolve().parent``
# above is what actually does the heavy lifting on a developer laptop.
_SCRIPT_DIR = Path(__file__).resolve().parent
if _SCRIPT_DIR.is_dir() and str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


def _load_psycopg() -> Any:
    """Lazy-load psycopg. Cached on the module to avoid repeated importlib
    overhead and to give tests a single attribute to monkey-patch.
    """
    global psycopg
    if psycopg is None:
        psycopg = importlib.import_module("psycopg")
    return psycopg


# Use the same structlog configuration as scripts/reingest_from_s3.py so the
# ``extra=`` fields passed to ``logger.info(...)`` calls surface in CloudWatch
# Logs Insights output. ``stdlib_bridge=True`` routes stdlib
# ``logging.getLogger(__name__)`` calls through structlog's ProcessorFormatter
# + ExtraAdder, JSON-encoding the LogRecord plus its extras as one event per
# line. Without this, the previous ``logging.basicConfig`` format string
# (``"%(asctime)s %(levelname)-8s %(message)s"``) silently dropped every
# ``extra=`` field — see #4368 for the post-deploy-verification incident on
# #4360 that motivated the switch.
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cluster discovery
# ---------------------------------------------------------------------------

# Same shape as ``audit_llm_carry_forward.py`` ``_CLUSTER_QUERY`` but adds
# ``s3_bucket`` (needed to fetch the PDF) and ``ORDER BY`` for deterministic
# iteration. The HAVING clauses match the audit's check 4 exactly.
_CLUSTER_QUERY = """
    SELECT
        ct.county          AS county,
        d.s3_key           AS s3_key,
        d.scraper_id       AS scraper_id,
        d.s3_bucket        AS s3_bucket,
        c.case_title       AS case_title,
        COUNT(*)           AS ruling_count
    FROM derived.rulings r
    JOIN derived.cases     c  ON c.id = r.case_id
    JOIN derived.courts    ct ON ct.id = r.court_id
    JOIN derived.documents d  ON d.id = r.document_id
    WHERE ct.state = 'CA'
      AND d.status = 'active'
      {county_filter}
      AND c.case_title IS NOT NULL
      AND c.case_title <> ''
    GROUP BY ct.county, d.s3_key, d.scraper_id, d.s3_bucket, c.case_title
    HAVING COUNT(*) >= 2
       AND COUNT(DISTINCT r.id) = COUNT(*)
    ORDER BY ct.county, d.s3_key, c.case_title
"""


@dataclasses.dataclass(frozen=True)
class Cluster:
    """A single splitter-carry-forward cluster — one (s3_key, case_title)
    bucket whose existing children all share the same case_title."""

    county: str
    s3_key: str
    s3_bucket: str
    scraper_id: str
    case_title: str
    ruling_count: int


def find_clusters(conn: Any, *, county: str | None) -> list[Cluster]:
    """Return one ``Cluster`` per same-s3_key + same-case_title bucket.

    The query mirrors ``scripts/audit_llm_carry_forward.py`` check 4
    (``all_same_case_title_cluster``), so any cluster reported by the
    audit is eligible for draining here.

    Args:
        conn: Active psycopg connection.
        county: Optional county filter (case-insensitive UPPER match).

    Returns:
        List of Cluster records, sorted by (county, s3_key, case_title).
    """
    if county:
        county_filter = "AND UPPER(ct.county) = UPPER(%s)"
        params: list[Any] = [county]
    else:
        county_filter = ""
        params = []

    sql = _CLUSTER_QUERY.format(county_filter=county_filter)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        Cluster(
            county=row[0],
            s3_key=row[1],
            scraper_id=row[2],
            s3_bucket=row[3],
            case_title=row[4],
            ruling_count=int(row[5]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Splitter validation gate
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ClusterPlan:
    """Outcome of running the new splitter against a cluster's parent PDF.

    ``status`` values:
      - ``ready``           — splitter produces >=2 distinct case_titles.
      - ``skip_no_split``   — splitter is None or returns <2 entries.
      - ``skip_single_title`` — splitter returns >=2 entries but they all
        share one case_title (draining + re-running would just recreate
        the same cluster).
    """

    status: str
    distinct_titles: int


def plan_cluster_drain(
    cluster: Cluster,
    *,
    pdf_bytes: bytes,
    split_fn: Callable[..., list[Any]] | None,
    extract_text_fn: Callable[[bytes], str],
) -> ClusterPlan:
    """Run the registered splitter to validate that draining will help.

    Args:
        cluster: The cluster being considered.
        pdf_bytes: Raw bytes of the parent PDF (already fetched from S3).
        split_fn: The registered ``_split_rulings`` callable for the
            cluster's ``scraper_id``, or ``None`` if no splitter is
            registered (in which case draining is a no-op).  Conforms to
            the unified ``_SPLIT_REGISTRY`` contract introduced in #4360:
            ``(text: str, pdf_bytes: bytes | None = None) -> list[SplitRuling]``.
        extract_text_fn: Callable that turns PDF bytes into text. Tests
            stub this; production passes the same helper used by
            ``_full_reparse_document``.

    Returns:
        A ClusterPlan indicating whether the cluster is drainable.
    """
    if split_fn is None:
        return ClusterPlan(status="skip_no_split", distinct_titles=0)

    text = extract_text_fn(pdf_bytes)
    # Pass ``pdf_bytes`` to the registered splitter so bytes-aware paths
    # (e.g. SC's format-B ``pdfplumber.extract_tables()``) can fire.
    # Without this, dept-6 SC PDFs report ``skip_no_split`` because
    # format A returns ``[]`` and format B can't run (#4360).
    split_results = split_fn(text, pdf_bytes=pdf_bytes)
    if len(split_results) < 2:
        return ClusterPlan(status="skip_no_split", distinct_titles=len(split_results))

    distinct = {
        getattr(sr, "case_title", None)
        for sr in split_results
        if getattr(sr, "case_title", None)
    }
    if len(distinct) < 2:
        return ClusterPlan(status="skip_single_title", distinct_titles=len(distinct))

    return ClusterPlan(status="ready", distinct_titles=len(distinct))


# ---------------------------------------------------------------------------
# Per-cluster DB mutation
# ---------------------------------------------------------------------------


# Lock the active child rows for this cluster's s3_key. ``FOR UPDATE``
# prevents the live ingestion worker from racing the delete-then-restore
# inside the same transaction (AC #4). The status filter intentionally
# excludes any pre-existing ``superseded`` parent row from the same
# s3_key — we want to UPSERT that parent row in step 2d, not delete it.
_LOCK_CHILDREN_SQL = """
    SELECT
        d.id::text         AS id,
        d.content_hash     AS content_hash,
        d.court_id::text   AS court_id,
        d.document_type    AS document_type,
        d.format::text     AS format,
        d.captured_at      AS captured_at,
        d.source_url       AS source_url,
        d.scraper_id       AS scraper_id
    FROM derived.documents d
    WHERE d.s3_key = %s
      AND d.status = 'active'
    ORDER BY d.id
    FOR UPDATE
"""


_DELETE_RULINGS_SQL = """
    DELETE FROM derived.rulings
    WHERE document_id = ANY(%s::uuid[])
"""


_DELETE_DOCUMENTS_SQL = """
    DELETE FROM derived.documents
    WHERE id = ANY(%s::uuid[])
"""


# UPSERT the parent doc row with the canonical
# ``derive_parent_document_id(content_hash)`` UUID. ON CONFLICT (id) covers
# the case where a pre-existing superseded parent row already lives at the
# same UUID (set by the legacy single-doc path before the LLM split fired);
# we resurrect it to ``status='active'`` and clear ``change_type`` so
# ``reingest_from_s3.py --full-reparse`` will pick it up.
_UPSERT_PARENT_SQL = """
    INSERT INTO derived.documents (
        id, court_id, document_type, s3_key, s3_bucket,
        format, content_hash, source_url, scraper_id,
        captured_at, status, change_type
    )
    VALUES (
        %s::uuid, %s::uuid, %s, %s, %s,
        %s::document_format, %s, %s, %s,
        %s, 'active', NULL
    )
    ON CONFLICT (id) DO UPDATE SET
        status = 'active',
        change_type = NULL,
        content_hash = EXCLUDED.content_hash,
        s3_key = EXCLUDED.s3_key,
        s3_bucket = EXCLUDED.s3_bucket
"""


def _load_split_ids_module() -> Any:
    """Load ``ingestion.split_ids`` directly from its file path.

    Going through ``ingestion/__init__.py`` would transitively import
    ``ingestion.worker``, which depends on ``psycopg`` and the rest of
    the scraper-framework stack — overkill (and not available in the
    scripts-tests CI shard). Loading the file directly via importlib's
    file-spec API gives us the two pure-Python helpers we need
    (``derive_parent_document_id``, ``is_split_child_id``) without the
    transitive dependency cost.
    """
    import importlib.util

    cached = getattr(_load_split_ids_module, "_cached", None)
    if cached is not None:
        return cached

    path = _SCRAPER_SRC / "ingestion" / "split_ids.py"
    spec = importlib.util.spec_from_file_location("_drain_split_ids", str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        msg = f"Could not load split_ids module from {path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _load_split_ids_module._cached = module  # type: ignore[attr-defined]
    return module


def compute_parent_doc_id(content_hash: str) -> str:
    """Compute the canonical parent ``document_id`` from a content hash.

    Wraps ``ingestion.split_ids.derive_parent_document_id`` so callers can
    use it without importing the scraper-framework module directly. Used
    by the restore step and by tests that verify the v5-from-content_hash
    form (NOT the v5-from-(parent,index) form ``is_split_child_id``
    matches against).
    """
    split_ids = _load_split_ids_module()
    return split_ids.derive_parent_document_id(content_hash)


def restore_parent_for_cluster(
    conn: Any,
    cluster: Cluster,
    *,
    parent_content_hash: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete the cluster's existing children and UPSERT a parent doc row.

    Runs entirely within the caller's transaction. The four steps:

      1. Lock every active child row for this s3_key with a row-level
         ``FOR UPDATE``-style read against ``derived.documents``.
      2. Delete child rulings by id.
      3. Delete child documents by id.
      4. Upsert the parent document row keyed on
         ``derive_parent_document_id(parent_content_hash)``.

    Args:
        conn: Active psycopg connection. The caller manages the
            transaction (this function does not commit).
        cluster: The cluster to drain.
        parent_content_hash: SHA-256 hex digest of the parent PDF's raw
            bytes (computed by ``hashlib.sha256(raw_bytes).hexdigest()``
            after fetching from S3).
        dry_run: When True, runs only the SELECT lock and skips the
            DELETE / INSERT statements. The lock is preserved so the
            dry-run preview reflects the same snapshot the real run
            would see.

    Returns:
        A dict with keys ``children_deleted`` (int), ``parent_doc_id``
        (str), ``court_id`` (str). The parent_doc_id is the canonical
        ``derive_parent_document_id`` UUID written to the docs table.
    """
    with conn.cursor() as cur:
        cur.execute(_LOCK_CHILDREN_SQL, (cluster.s3_key,))
        child_rows = cur.fetchall()

    if not child_rows:
        return {
            "children_deleted": 0,
            "parent_doc_id": None,
            "court_id": None,
            "skipped_reason": "no-active-children",
        }

    # All child rows share the cluster's s3_key, so they all reference
    # the same physical S3 object. Pick one to seed the parent doc row's
    # carry-forward fields (court_id, document_type, format, captured_at,
    # source_url, scraper_id). Picking the first one keyed by id keeps
    # the choice deterministic across re-runs.
    seed = child_rows[0]
    (
        _seed_id,
        _seed_hash,
        court_id,
        document_type,
        fmt,
        captured_at,
        source_url,
        scraper_id,
    ) = seed

    child_ids = [str(row[0]) for row in child_rows]
    parent_doc_id = compute_parent_doc_id(parent_content_hash)

    if dry_run:
        logger.info(
            "DRY RUN — would drain cluster",
            extra={
                "s3_key": cluster.s3_key,
                "child_count": len(child_ids),
                "parent_doc_id": parent_doc_id,
            },
        )
        return {
            "children_deleted": 0,
            "parent_doc_id": parent_doc_id,
            "court_id": court_id,
            "skipped_reason": "dry-run",
        }

    with conn.cursor() as cur:
        # DELETE order matters — rulings reference documents.id via FK.
        cur.execute(_DELETE_RULINGS_SQL, (child_ids,))
        cur.execute(_DELETE_DOCUMENTS_SQL, (child_ids,))

        cur.execute(
            _UPSERT_PARENT_SQL,
            (
                parent_doc_id,
                court_id,
                document_type,
                cluster.s3_key,
                cluster.s3_bucket,
                fmt,
                parent_content_hash,
                source_url,
                scraper_id,
                captured_at,
            ),
        )

    logger.info(
        "Drained cluster",
        extra={
            "s3_key": cluster.s3_key,
            "case_title": cluster.case_title,
            "children_deleted": len(child_ids),
            "parent_doc_id": parent_doc_id,
        },
    )
    return {
        "children_deleted": len(child_ids),
        "parent_doc_id": parent_doc_id,
        "court_id": court_id,
    }


# ---------------------------------------------------------------------------
# Splitter resolver / S3 fetcher (production wiring)
# ---------------------------------------------------------------------------


def resolve_split_fn(scraper_id: str) -> Callable[..., list[Any]] | None:
    """Return the registered ``_split_rulings`` callable for a scraper_id.

    Mirrors the lookup ``reingest_from_s3.py:1479`` does against its
    ``_SPLIT_REGISTRY``. We import that registry lazily so this module
    can be imported in a test environment without the scraper-framework
    venv (the test suite stubs this resolver out).

    The returned callable conforms to the unified ``_SPLIT_REGISTRY``
    contract (#4360): ``(text: str, pdf_bytes: bytes | None = None) ->
    list[SplitRuling]``.
    """
    # Late import — reingest_from_s3 pulls in boto3, structlog, etc.,
    # which we don't want as test-time dependencies for the per-cluster
    # planning unit tests. The module-level sys.path additions at the top
    # make this import resolve both on a developer laptop (scripts/ on
    # path) and inside the ECS oneshot container (/app/scripts/ on path).
    import reingest_from_s3  # type: ignore[import-not-found]

    reingest_from_s3._load_scraper_registry()
    return reingest_from_s3._SPLIT_REGISTRY.get(scraper_id)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF using the same pdfplumber-subprocess helper
    ``reingest_from_s3._extract_text_from_content`` uses on PDFs.

    Returns an empty string on failure — ``plan_cluster_drain`` treats
    that as a 0-result split (skip_no_split).
    """
    import reingest_from_s3  # type: ignore[import-not-found]

    try:
        return reingest_from_s3._extract_text_from_content(
            pdf_bytes, "pdf", pdf_timeout=60.0
        ).replace("\x00", "")
    except Exception:
        logger.warning("PDF text extraction failed", exc_info=True)
        return ""


def make_s3_fetcher() -> Callable[[str, str], bytes]:
    """Return a ``(bucket, key) -> bytes`` callable bound to a boto3 client."""
    import boto3  # type: ignore[import-not-found]

    s3 = boto3.client("s3")

    def _fetch(bucket: str, key: str) -> bytes:
        response = s3.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    return _fetch


# ---------------------------------------------------------------------------
# Drain driver
# ---------------------------------------------------------------------------


def run_drain(
    *,
    dsn: str,
    county: str | None,
    s3_fetcher: Callable[[str, str], bytes],
    splitter_resolver: Callable[[str], Callable[..., list[Any]] | None],
    text_extractor: Callable[[bytes], str],
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Execute the full drain workflow.

    Steps:
      1. Discover clusters (optionally county-filtered).
      2. For each cluster (up to ``limit``):
           a. Fetch parent PDF, compute real content_hash.
           b. Run the splitter — if not ready, skip.
           c. Inside one transaction: lock children, delete, UPSERT parent.
      3. Return summary stats. The caller is responsible for invoking
         ``run_reingest(..., full_reparse=True, bust_llm_cache=True)``
         (or the equivalent CLI) to actually re-extract per-entry
         children from the restored parent rows.

    Returns:
        A dict with ``clusters_found``, ``clusters_drained``,
        ``clusters_skipped``, ``children_deleted_total``, and a
        ``per_cluster`` list for the human-readable report.
    """
    pg = _load_psycopg()
    stats: dict[str, Any] = {
        "clusters_found": 0,
        "clusters_drained": 0,
        "clusters_skipped": 0,
        "children_deleted_total": 0,
        "per_cluster": [],
    }

    with pg.connect(dsn) as conn:
        clusters = find_clusters(conn, county=county)
        stats["clusters_found"] = len(clusters)

        if limit is not None:
            clusters = clusters[:limit]

        for cluster in clusters:
            entry: dict[str, Any] = {
                "s3_key": cluster.s3_key,
                "county": cluster.county,
                "case_title": cluster.case_title,
                "ruling_count": cluster.ruling_count,
            }
            try:
                pdf_bytes = s3_fetcher(cluster.s3_bucket, cluster.s3_key)
            except Exception as exc:
                logger.warning(
                    "S3 fetch failed for cluster",
                    extra={"s3_key": cluster.s3_key, "error": str(exc)},
                )
                entry["status"] = "skip_s3_fetch_failed"
                stats["clusters_skipped"] += 1
                stats["per_cluster"].append(entry)
                continue

            split_fn = splitter_resolver(cluster.scraper_id)
            plan = plan_cluster_drain(
                cluster,
                pdf_bytes=pdf_bytes,
                split_fn=split_fn,
                extract_text_fn=text_extractor,
            )
            entry["plan_status"] = plan.status
            entry["distinct_titles"] = plan.distinct_titles
            if plan.status != "ready":
                logger.info(
                    "Skipping cluster — splitter would not improve it",
                    extra={
                        "s3_key": cluster.s3_key,
                        "plan_status": plan.status,
                        "distinct_titles": plan.distinct_titles,
                    },
                )
                entry["status"] = plan.status
                stats["clusters_skipped"] += 1
                stats["per_cluster"].append(entry)
                continue

            parent_hash = hashlib.sha256(pdf_bytes).hexdigest()
            try:
                with conn.transaction():
                    result = restore_parent_for_cluster(
                        conn,
                        cluster,
                        parent_content_hash=parent_hash,
                        dry_run=dry_run,
                    )
            except Exception as exc:
                logger.error(
                    "Restore transaction failed for cluster",
                    extra={"s3_key": cluster.s3_key, "error": str(exc)},
                )
                entry["status"] = "error"
                entry["error"] = str(exc)
                stats["clusters_skipped"] += 1
                stats["per_cluster"].append(entry)
                continue

            entry["status"] = "drained"
            entry["children_deleted"] = result["children_deleted"]
            entry["parent_doc_id"] = result["parent_doc_id"]
            stats["clusters_drained"] += 1
            stats["children_deleted_total"] += int(result["children_deleted"])
            stats["per_cluster"].append(entry)

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Drain splitter-carry-forward clusters: delete LLM-only-split "
            "children whose case_titles all collapsed to one, then UPSERT "
            "a parent doc row so the new pre-LLM splitter can re-extract."
        ),
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help=(
            "Restrict to one CA county (case-insensitive). Recommended; "
            "running across all counties at once is supported but slow."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Lock and discover children, but skip DELETE / INSERT writes. "
            "Useful to preview the count of clusters that would be drained "
            "before committing to the full transaction."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of clusters to drain in one run (default: all).",
    )
    parser.add_argument(
        "--no-reingest",
        action="store_true",
        help=(
            "Skip the post-drain ``run_reingest --full-reparse "
            "--bust-llm-cache`` invocation. Use this in CI / unit tests, "
            "or when running the script in stages (drain in one ECS task, "
            "reingest in a separate one)."
        ),
    )
    return parser


def _resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        msg = (
            "DATABASE_URL is not set. Run via scripts/with-secret.sh -e "
            "DATABASE_URL=judgemind/dev/db/connection:.url, or export it "
            "manually."
        )
        raise SystemExit(msg)
    return dsn


def _maybe_invoke_reingest(*, county: str | None, dry_run: bool) -> dict[str, Any]:
    """Invoke ``reingest_from_s3.run_reingest`` for the affected county.

    Mirrors the parameters used by the post-deploy verification path of
    every splitter PR (#4286, #4303, #4304): ``--full-reparse``,
    ``--bust-llm-cache``, county-scoped. The line-2580 guard correctly
    does NOT skip the freshly-restored parent rows because their IDs
    match ``derive_parent_document_id(content_hash)``.
    """
    if dry_run:
        logger.info("DRY RUN — skipping post-drain reingest invocation")
        return {"skipped": "dry-run"}

    import reingest_from_s3  # type: ignore[import-not-found]

    return reingest_from_s3.run_reingest(
        dsn=_resolve_dsn(),
        county=county,
        full_reparse=True,
        bust_llm_cache=True,
    )


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    dsn = _resolve_dsn()
    s3_fetcher = make_s3_fetcher()
    drain_stats = run_drain(
        dsn=dsn,
        county=args.county,
        s3_fetcher=s3_fetcher,
        splitter_resolver=resolve_split_fn,
        text_extractor=extract_pdf_text,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    logger.info(
        "Drain summary",
        extra={
            "clusters_found": drain_stats["clusters_found"],
            "clusters_drained": drain_stats["clusters_drained"],
            "clusters_skipped": drain_stats["clusters_skipped"],
            "children_deleted_total": drain_stats["children_deleted_total"],
        },
    )

    if args.no_reingest:
        logger.info("--no-reingest set — skipping post-drain reingest")
        return 0

    if drain_stats["clusters_drained"] == 0:
        logger.info(
            "No clusters drained — skipping post-drain reingest "
            "(nothing for the new splitter to re-extract)"
        )
        return 0

    reingest_stats = _maybe_invoke_reingest(county=args.county, dry_run=args.dry_run)
    logger.info("Reingest stats: %s", reingest_stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
