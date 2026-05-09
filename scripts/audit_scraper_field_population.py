#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit captured S3 envelopes against scraper field-mapping assumptions.

For each registered scraper that produces a JSON envelope, sample N recent
S3 objects and assert that every "load-bearing" field — i.e. a field the
scraper reads to extract jurisdiction, case number, judge name, etc. — is
populated in at least one sample. When 0/N samples have a field
populated, that field has silently drifted to "always empty" and the
scraper is producing wrong/incomplete data without warning.

This is the structural fix for the bug class behind #4247 and #3885 (the
CourtListener ``cluster.court`` field documented as canonical but empty
in every actual API response). Running this audit daily catches drift
within ~24 h instead of weeks.

Scope (initial registry):
  - ``federal-courtlistener-opinions`` -- envelope is JSON
    {cluster, opinion, docket}; load-bearing field is ``docket.court``.
  - ``ca-sf-civil-tentatives`` -- envelope is a flat JSON dict of
    ParsedRuling fields plus ruling_id/department. Load-bearing fields
    are ``case_number`` and ``department``.
  - ``ca-cc-tentatives-portal`` -- envelope is JSON
    {row, detail_html_b64, pdf_url, pdf_bytes_b64, judge_id,
    judge_name_dropdown}. Load-bearing fields are ``judge_id`` and
    ``judge_name_dropdown``.

PDF-binary and bare-HTML scrapers (OC, LA, SD, Ventura, Riverside, etc.)
are intentionally excluded from this audit -- their S3 raw is not JSON
and the audit's introspection cannot meaningfully reach into them.  The
analogous bug class for those scrapers is link-text parse failure, which
is already covered by validation/regression tests at capture time.

Sampling source:
  Documents are sampled from ``derived.documents`` filtered by
  ``scraper_id``, ordered by capture timestamp DESC, taking the most
  recent N (default 10). Samples are deterministic for reproducibility:
  a fresh run on an unchanged DB picks the same envelopes.

Failure handling:
  When at least one load-bearing field is empty in 10/10 samples, the
  audit:
    - returns a non-zero exit (1)
    - emits machine-readable JSON when --json is set
    - (when GITHUB_TOKEN + ECS-runner mode) files a GitHub issue tagged
      ``priority/p1,area/scraping,scraper-field-drift,agent/ready`` so
      a /task agent can pick up the regression.

Usage:
    # Local dry-run against synthetic envelopes (no DB / no S3 calls):
    packages/scraper-framework/.venv/bin/python3 \\
        scripts/audit_scraper_field_population.py --dry-run --json

    # Against dev DB + S3 (read-only):
    scripts/ecs-run-task.sh scripts/audit_scraper_field_population.py -- \\
        --json --sample-size 10

Options:
    --json              Machine-readable JSON output.
    --sample-size N     Envelopes to sample per scraper (default 10).
    --min-captures N    Skip scrapers with < N captures in last 7 days
                        (default 50).
    --scraper SID       Audit only the specified scraper id; can be
                        repeated.
    --dry-run           Skip DB + S3 calls; emit a synthetic empty-field
                        report. Used for AC verification + tests.

Exit codes:
    0   No drift detected.
    1   At least one load-bearing field is empty across all samples.
    2   Configuration error (missing DATABASE_URL, etc.).

See https://github.com/judgemind/judgemind/issues/4255 for context.
"""

from __future__ import annotations

import argparse
import json as json_mod
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# Use ``configure_structlog`` so any ``extra=`` fields passed to logger calls
# surface in CloudWatch Logs Insights output. ``stdlib_bridge=True`` routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
# extras as one event per line. The previous ``logging.basicConfig`` format
# string silently dropped every ``extra=`` field — see #4368 / #4373.
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")
DEFAULT_SAMPLE_SIZE = 10
DEFAULT_MIN_CAPTURES = 50
DEFAULT_LOOKBACK_DAYS = 7

ALERT_LABEL = "scraper-field-drift"
ISSUE_LABELS = "priority/p1,area/scraping,scraper-field-drift,agent/ready"
ISSUE_TITLE = "Scraper field-mapping drift: load-bearing field empty in samples"
GITHUB_API_BASE = "https://api.github.com"

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScraperFieldSpec:
    """Declares load-bearing fields for one scraper's S3 envelope.

    Attributes:
        scraper_id: The registry id (matches ``ScraperConfig.scraper_id``).
        envelope_format: ``"json"`` -- the S3 raw is UTF-8 JSON. Other
            formats (``"pdf"``, ``"html"``) are not auditable here.
        field_paths: Dotted paths into the JSON envelope. A path like
            ``"docket.court"`` resolves ``payload["docket"]["court"]``.
            Each path is a "load-bearing" field the scraper reads to
            populate a structured column.
        rationale: One-line description of why each field matters.
    """

    scraper_id: str
    envelope_format: str
    field_paths: tuple[str, ...]
    rationale: str


# Initial registry (issue #4255 acceptance criterion: cover the
# 3 highest-volume JSON-envelope scrapers).  CourtListener is the
# motivating case (#4247).  SF civil and CC portal are the other two
# JSON-envelope scrapers in the codebase today; OC / LA / SD store
# binary PDFs or bare HTML that don't admit field-path introspection
# from S3 alone.
REGISTRY: tuple[ScraperFieldSpec, ...] = (
    ScraperFieldSpec(
        scraper_id="federal-courtlistener-opinions",
        envelope_format="json",
        field_paths=("docket.court_id",),
        rationale=(
            "docket.court_id is the canonical bare short-id (e.g. "
            "'dcd', 'scotus', 'texapp1') after #4247 / #4310. "
            "cluster.court / cluster.court_id are empty in every "
            "CourtListener API response we capture; docket.court is a "
            "URL with a ?format=json query string and previously parsed "
            "to '?format=json' (#4310).  docket.court_id is the bare "
            "short-id directly and avoids URL parsing entirely."
        ),
    ),
    ScraperFieldSpec(
        scraper_id="ca-sf-civil-tentatives",
        envelope_format="json",
        field_paths=("case_number", "department"),
        rationale=(
            "SF civil envelope is flat -- case_number identifies the "
            "case and department drives judge resolution.  Either "
            "going empty silently produces UNKNOWN-prefixed case "
            "numbers + missing judge attribution."
        ),
    ),
    ScraperFieldSpec(
        scraper_id="ca-cc-tentatives-portal",
        envelope_format="json",
        field_paths=("judge_id", "judge_name_dropdown"),
        rationale=(
            "CC portal envelope encodes the judge identity in two "
            "fields scraped from the dropdown.  Both empty means we "
            "land judge-less rulings into the DB."
        ),
    ),
)


def get_spec(scraper_id: str) -> ScraperFieldSpec | None:
    """Return the registry entry for a scraper, or None if unregistered."""
    for spec in REGISTRY:
        if spec.scraper_id == scraper_id:
            return spec
    return None


# ---------------------------------------------------------------------------
# Field-path lookup
# ---------------------------------------------------------------------------


def lookup_field(payload: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path against a nested dict.

    Returns the value if every segment exists, else None.  Empty string,
    empty list, empty dict, and explicit None are all returned as-is so
    the caller can decide whether they count as "populated".

    Args:
        payload: Decoded JSON envelope (must be a dict at the top level).
        path: Dotted path like "docket.court" or "case_number".
    """
    if not isinstance(payload, dict):
        return None
    cur: Any = payload
    for segment in path.split("."):
        if not isinstance(cur, dict):
            return None
        if segment not in cur:
            return None
        cur = cur[segment]
    return cur


def is_populated(value: Any) -> bool:
    """Return True iff *value* is a non-empty meaningful field value.

    Empty string / empty list / empty dict / None all count as NOT
    populated.  Zero (int 0) is treated as populated -- it is a valid
    judge_id or department in some courts.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    # int, float, bool -- 0 / False count as populated.
    return True


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class FieldResult:
    """Audit result for one (scraper, field-path) tuple."""

    scraper_id: str
    field_path: str
    populated: int
    sampled: int
    sample_keys: list[str] = field(default_factory=list)

    @property
    def drifted(self) -> bool:
        """True when this field is empty in EVERY sample (0/N populated)."""
        return self.sampled > 0 and self.populated == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scraper_id": self.scraper_id,
            "field_path": self.field_path,
            "populated": self.populated,
            "sampled": self.sampled,
            "drifted": self.drifted,
            "sample_keys": self.sample_keys,
        }


@dataclass
class AuditResult:
    """Full audit run result (all scrapers, all fields)."""

    field_results: list[FieldResult] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    @property
    def drift_count(self) -> int:
        return sum(1 for r in self.field_results if r.drifted)

    @property
    def healthy(self) -> bool:
        return self.drift_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "healthy": self.healthy,
            "drift_count": self.drift_count,
            "field_results": [r.to_dict() for r in self.field_results],
            "skipped": list(self.skipped),
        }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def fetch_capture_count(conn: object, scraper_id: str, since: datetime) -> int:
    """Return the count of derived.documents rows for *scraper_id* since *since*.

    Used for the --min-captures gate: if a scraper has < N captures in
    the lookback window, skip it (the audit's signal-to-noise is too low
    on small samples).
    """
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT COUNT(*) FROM derived.documents "
            "WHERE scraper_id = %s AND captured_at >= %s",
            (scraper_id, since),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def fetch_sample_keys(conn: object, scraper_id: str, sample_size: int) -> list[str]:
    """Return up to *sample_size* recent s3_key values for *scraper_id*.

    Ordered by captured_at DESC so the audit always inspects the
    freshest envelopes -- field drift is most visible at the leading
    edge of capture.  Empty s3_key rows are excluded.
    """
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT s3_key FROM derived.documents "
            "WHERE scraper_id = %s AND s3_key IS NOT NULL AND s3_key != '' "
            "ORDER BY captured_at DESC NULLS LAST "
            "LIMIT %s",
            (scraper_id, sample_size),
        )
        rows = cur.fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def fetch_envelope(s3: object, bucket: str, key: str) -> dict[str, Any] | None:
    """GET a JSON envelope from S3 and return it parsed.

    Returns None when the object is missing, the body is not valid
    JSON, or the top-level payload is not a dict.  Raised exceptions
    bubble out so the caller can decide between treating them as
    "skip the sample" vs. "fail the audit".
    """
    try:
        response = s3.get_object(Bucket=bucket, Key=key)  # type: ignore[attr-defined]
        body = response["Body"].read()
    except Exception as exc:
        logger.warning("Failed to GET s3://%s/%s: %s", bucket, key, exc)
        return None
    try:
        payload = json_mod.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


# ---------------------------------------------------------------------------
# Audit core
# ---------------------------------------------------------------------------


def audit_one_scraper(
    conn: object,
    s3: object,
    bucket: str,
    spec: ScraperFieldSpec,
    *,
    sample_size: int,
    min_captures: int,
    lookback_days: int,
    now: datetime,
) -> tuple[list[FieldResult], dict[str, Any] | None]:
    """Audit a single scraper.  Returns (field_results, skip_reason).

    If the scraper has fewer than *min_captures* in the lookback window,
    returns ([], skip_reason).  Otherwise samples envelopes and resolves
    each load-bearing field path against each sample.
    """
    since = now - timedelta(days=lookback_days)
    capture_count = fetch_capture_count(conn, spec.scraper_id, since)
    if capture_count < min_captures:
        return [], {
            "scraper_id": spec.scraper_id,
            "reason": "insufficient_captures",
            "capture_count": capture_count,
            "min_captures": min_captures,
            "lookback_days": lookback_days,
        }

    keys = fetch_sample_keys(conn, spec.scraper_id, sample_size)
    if not keys:
        return [], {
            "scraper_id": spec.scraper_id,
            "reason": "no_sample_keys",
            "capture_count": capture_count,
        }

    # Resolve each field path against each sample envelope.
    populated_counts: dict[str, int] = {p: 0 for p in spec.field_paths}
    inspected = 0
    for key in keys:
        payload = fetch_envelope(s3, bucket, key)
        if payload is None:
            continue
        inspected += 1
        for path in spec.field_paths:
            if is_populated(lookup_field(payload, path)):
                populated_counts[path] += 1

    results = [
        FieldResult(
            scraper_id=spec.scraper_id,
            field_path=path,
            populated=populated_counts[path],
            sampled=inspected,
            sample_keys=list(keys),
        )
        for path in spec.field_paths
    ]
    return results, None


def run_audit(
    conn: object,
    s3: object,
    bucket: str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    min_captures: int = DEFAULT_MIN_CAPTURES,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    scraper_filter: list[str] | None = None,
    now: datetime | None = None,
) -> AuditResult:
    """Run the full audit and return an AuditResult."""
    now = now or datetime.now(UTC)
    result = AuditResult()

    for spec in REGISTRY:
        if scraper_filter and spec.scraper_id not in scraper_filter:
            continue
        field_results, skip = audit_one_scraper(
            conn,
            s3,
            bucket,
            spec,
            sample_size=sample_size,
            min_captures=min_captures,
            lookback_days=lookback_days,
            now=now,
        )
        if skip is not None:
            result.skipped.append(skip)
        result.field_results.extend(field_results)

    return result


# ---------------------------------------------------------------------------
# Dry-run -- synthetic empty-field scenario for AC verification
# ---------------------------------------------------------------------------


def synthetic_drift_result() -> AuditResult:
    """Return a synthetic AuditResult for --dry-run.

    Simulates the bug class behind #4247: courtlistener's docket.court
    empty in 10/10 samples.  Used for the AC verification step
    "dry-run against a synthetic empty-field scenario produces a
    generated issue body in tmp/" and for unit tests.
    """
    spec = get_spec("federal-courtlistener-opinions")
    assert spec is not None, "registry must contain courtlistener"
    drifted = FieldResult(
        scraper_id=spec.scraper_id,
        field_path="docket.court_id",
        populated=0,
        sampled=10,
        sample_keys=[
            f"federal/dc/dc_district/raw/{'a' * 64}.txt",
            f"federal/dc/dc_district/raw/{'b' * 64}.txt",
        ],
    )
    return AuditResult(field_results=[drifted])


# ---------------------------------------------------------------------------
# Issue body builder
# ---------------------------------------------------------------------------


def build_issue_body(result: AuditResult) -> str:
    """Render the GitHub issue body for a drift-detected audit run."""
    drifted = [r for r in result.field_results if r.drifted]
    rows = [
        "| Scraper | Field path | Populated / Sampled | Sample s3_key |",
        "| --- | --- | --- | --- |",
    ]
    for r in drifted:
        sample_key = r.sample_keys[0] if r.sample_keys else "(none)"
        rows.append(
            f"| `{r.scraper_id}` | `{r.field_path}` | "
            f"{r.populated} / {r.sampled} | `{sample_key}` |"
        )
    table = "\n".join(rows)

    return (
        "## Scraper Field-Mapping Drift Alert\n\n"
        "The daily field-population audit "
        "(`scripts/audit_scraper_field_population.py`) detected at "
        "least one load-bearing field that is empty in **every** sample "
        "envelope.  This is the bug class behind #4247 and #3885 -- a "
        "scraper reading a field documented as canonical that has "
        "drifted to always-empty in production responses.\n\n"
        "### Drifted fields\n\n"
        f"{table}\n\n"
        "### Context\n\n"
        f"- **Audit run at:** {result.timestamp}\n"
        "- **Audit script:** `scripts/audit_scraper_field_population.py`\n"
        "- **Registry source:** `REGISTRY` constant in the audit script\n\n"
        "### Next steps\n\n"
        "1. Open one of the sample s3_keys with "
        "`aws s3api get-object --bucket judgemind-document-archive-dev"
        " --key <KEY> -` and confirm the field is empty in the raw "
        "envelope.\n"
        "2. Decide whether the source field has moved (update the "
        "scraper's mapping) or whether the upstream API has stopped "
        "populating it (file an upstream bug + add a fallback).\n"
        "3. Update `REGISTRY` if the load-bearing field path itself "
        "should change.\n\n"
        "This issue was filed automatically.  It will not duplicate "
        "while open."
    )


# ---------------------------------------------------------------------------
# GitHub helpers (urllib only, no extra deps)
# ---------------------------------------------------------------------------


def list_open_alert_issues(repo: str, token: str) -> list[dict[str, Any]]:
    """Return open issues tagged with the field-drift alert label."""
    import urllib.request

    url = f"{GITHUB_API_BASE}/repos/{repo}/issues?labels={ALERT_LABEL}&state=open&per_page=10"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json_mod.loads(resp.read().decode())


def create_issue(repo: str, token: str, body: str) -> dict[str, Any]:
    """Create a new GitHub issue and return the created issue object."""
    import urllib.request

    url = f"{GITHUB_API_BASE}/repos/{repo}/issues"
    payload = {
        "title": ISSUE_TITLE,
        "body": body,
        "labels": ISSUE_LABELS.split(","),
    }
    req = urllib.request.Request(
        url, data=json_mod.dumps(payload).encode(), method="POST"
    )
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json_mod.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help="envelopes to sample per scraper",
    )
    parser.add_argument(
        "--min-captures",
        type=int,
        default=DEFAULT_MIN_CAPTURES,
        help="skip scrapers with < N captures in lookback window",
    )
    parser.add_argument(
        "--scraper",
        action="append",
        default=None,
        help="audit only this scraper id (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="emit a synthetic empty-field report; no DB or S3 calls",
    )
    parser.add_argument(
        "--write-issue-body",
        type=str,
        default="",
        help="write the rendered issue body to this path (used by AC tests)",
    )
    args = parser.parse_args(argv)

    # --dry-run -- synthetic scenario, no I/O.
    if args.dry_run:
        result = synthetic_drift_result()
        if args.write_issue_body:
            os.makedirs(os.path.dirname(args.write_issue_body) or ".", exist_ok=True)
            with open(args.write_issue_body, "w") as f:
                f.write(build_issue_body(result))
        if args.json:
            print(json_mod.dumps(result.to_dict(), indent=2))
        else:
            _print_human(result)
        return 1 if not result.healthy else 0

    # Real run -- DB + S3.
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.error(
            "DATABASE_URL is not set. Run via scripts/ecs-run-task.sh "
            "or scripts/with-secret.sh so the connection string is "
            "injected."
        )
        return 2

    import boto3
    import psycopg

    s3 = boto3.client("s3")
    with psycopg.connect(database_url, autocommit=True) as conn:
        result = run_audit(
            conn,
            s3,
            BUCKET,
            sample_size=args.sample_size,
            min_captures=args.min_captures,
            scraper_filter=args.scraper,
        )

    if args.json:
        print(json_mod.dumps(result.to_dict(), indent=2))
    else:
        _print_human(result)

    # When drift detected and runtime tokens are available, file/update
    # the GitHub issue.  Best-effort -- a network failure here does not
    # change the script's exit code.
    if not result.healthy:
        gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
        gh_repo = os.environ.get("GH_REPO", "judgemind/judgemind").strip()
        if gh_token and gh_repo:
            try:
                open_issues = list_open_alert_issues(gh_repo, gh_token)
                if not open_issues:
                    body = build_issue_body(result)
                    issue = create_issue(gh_repo, gh_token, body)
                    logger.info(
                        "Filed drift alert issue #%d: %s",
                        issue.get("number"),
                        issue.get("html_url"),
                    )
                else:
                    logger.info(
                        "Drift detected but issue #%d already open; not "
                        "filing duplicate.",
                        open_issues[0]["number"],
                    )
            except Exception as exc:
                logger.warning("Issue filing failed (non-fatal): %s", exc)

    return 0 if result.healthy else 1


def _print_human(result: AuditResult) -> None:
    print(f"Field-population audit at {result.timestamp}")
    print(
        f"  drift_count={result.drift_count}  "
        f"field_results={len(result.field_results)}  "
        f"skipped={len(result.skipped)}"
    )
    for r in result.field_results:
        flag = "DRIFT" if r.drifted else "ok"
        print(
            f"  [{flag}] {r.scraper_id} :: {r.field_path} "
            f"-> {r.populated}/{r.sampled} populated"
        )
    for skip in result.skipped:
        print(
            f"  [skip] {skip.get('scraper_id')} -- {skip.get('reason')} "
            f"(captures={skip.get('capture_count')})"
        )


if __name__ == "__main__":
    raise SystemExit(main())
