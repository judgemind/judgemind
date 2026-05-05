#!/usr/bin/env python3
"""Audit CI pipeline health: job durations, threshold checks, and trend regression.

Implements §1.8 of the /audit skill. Key correctness rules:

1.  **Skipped jobs must not be counted as zero duration.** GitHub path filters
    (`dorny/paths-filter`) skip conditional jobs when their file patterns do
    not match.  If a skipped run is treated as duration = 0, the mean of a
    sample that mixes skipped and ran runs is artificially dragged down.
    When the next sample window happens to contain mostly runs that ran,
    the trend detector reports a massive false positive regression. See #2401.

2.  **Require a minimum sample size per job** before emitting a trend
    regression.  A job that only executed in 2–3 of the 10 sampled runs does
    not have enough data to detect a trend.

3.  **Require a minimum absolute delta** in addition to a percentage delta.
    A job that goes from 3s → 4s is +33% but not a real regression.

The script reads JSON from `gh run list` and `gh run view` (or from stdin for
tests) and prints per-job timing plus any trend-regression findings.

Usage:
    scripts/audit_ci_health.py                              # fetch last 10 CI runs
    scripts/audit_ci_health.py --limit 20                   # sample 20 runs
    scripts/audit_ci_health.py --json                       # machine-readable output
    scripts/audit_ci_health.py --from-file runs.json        # replay from file (testing)

Exit codes:
    0 — no findings
    1 — at least one threshold violation or trend regression detected
    2 — error (network, auth, malformed data)
"""
# permanent: true

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

REPO = "judgemind/judgemind"
WORKFLOW = "ci.yml"

# Threshold constants — documented here for discoverability.
SINGLE_JOB_SECONDS_THRESHOLD = 10 * 60  # 10 minutes
TOTAL_WALL_CLOCK_THRESHOLD = 15 * 60  # 15 minutes
TREND_PERCENT_THRESHOLD = 20.0  # +20% mean delta
TREND_ABSOLUTE_SECONDS_THRESHOLD = 15.0  # +15s absolute delta
MIN_SAMPLES_PER_GROUP = 3  # require ≥3 ran-samples in BOTH halves

# Drill-down constants.
DRILL_DOWN_TOP_N = 3  # report top-N slowest steps inside a flagged job
DRIFT_FACTOR_THRESHOLD = 1.5  # measured ≥ 1.5× estimate flags drift
# Suggest re-shard when the slowest step alone accounts for ≥ this fraction
# of the parsed `##[group]` total. The intuition: a long pole that dominates
# the test execution time is a great candidate for parallelism — isolating it
# brings the post-split wall-clock down to roughly the long pole alone.
# A balanced job (slowest step ≈ remaining steps) gains far less from
# splitting and should not trigger the suggestion. 0.5 means "slowest step
# accounts for at least half of the total" — generous to avoid missing real
# wins, strict enough to filter balanced jobs.
SPLIT_SLOWEST_DOMINANCE_THRESHOLD = 0.5
# Belt-and-suspenders: even when dominance clears the gate, require some
# absolute savings so we don't fire on tiny groups (e.g. slowest 10s, total 12s).
SPLIT_SAVINGS_MIN_SECONDS = 30


@dataclass
class JobRun:
    """One job's timing in one CI run."""

    run_id: str
    name: str
    conclusion: str  # "success", "failure", "skipped"
    started_at: datetime | None
    completed_at: datetime | None
    test_step_seconds: float | None = None
    job_id: str | None = None  # gh job databaseId; needed for log fetch

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def skipped(self) -> bool:
        return self.conclusion == "skipped"


@dataclass
class RunSummary:
    """Summary of one CI run."""

    run_id: str
    created_at: datetime
    jobs: list[JobRun] = field(default_factory=list)

    @property
    def wall_clock_seconds(self) -> float | None:
        """Earliest startedAt → latest completedAt, ignoring skipped jobs."""
        started = [j.started_at for j in self.jobs if j.started_at and not j.skipped]
        completed = [
            j.completed_at for j in self.jobs if j.completed_at and not j.skipped
        ]
        if not started or not completed:
            return None
        return (max(completed) - min(started)).total_seconds()


@dataclass
class Finding:
    """A single CI health finding."""

    kind: str  # "single-job", "wall-clock", "trend"
    job_name: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def build_runs_from_json(data: list[dict[str, Any]]) -> list[RunSummary]:
    """Build RunSummary objects from a list of {run_id, created_at, jobs} dicts."""
    runs: list[RunSummary] = []
    for entry in data:
        run_id = entry["run_id"]
        created_at = parse_iso(entry.get("created_at"))
        if created_at is None:
            raise ValueError(f"Run {run_id} missing created_at")
        job_runs: list[JobRun] = []
        for job in entry.get("jobs", []):
            test_step_seconds = None
            for step in job.get("steps", []):
                if step.get("name") == "Test":
                    s = parse_iso(step.get("startedAt"))
                    e = parse_iso(step.get("completedAt"))
                    if s and e:
                        test_step_seconds = (e - s).total_seconds()
                    break
            job_runs.append(
                JobRun(
                    run_id=run_id,
                    name=job["name"],
                    conclusion=job.get("conclusion") or "",
                    started_at=parse_iso(job.get("startedAt")),
                    completed_at=parse_iso(job.get("completedAt")),
                    test_step_seconds=test_step_seconds,
                    job_id=str(job["databaseId"]) if job.get("databaseId") else None,
                )
            )
        runs.append(RunSummary(run_id=run_id, created_at=created_at, jobs=job_runs))
    # Sort oldest → newest
    runs.sort(key=lambda r: r.created_at)
    return runs


def fetch_runs_via_gh(limit: int) -> list[dict[str, Any]]:
    """Use `gh` to fetch the last `limit` successful main CI runs."""
    list_proc = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            REPO,
            "--branch",
            "main",
            "--status",
            "success",
            "--workflow",
            WORKFLOW,
            "--limit",
            str(limit),
            "--json",
            "databaseId,createdAt",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    runs_meta = json.loads(list_proc.stdout)
    results: list[dict[str, Any]] = []
    for meta in runs_meta:
        run_id = str(meta["databaseId"])
        view_proc = subprocess.run(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                REPO,
                "--json",
                "jobs",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        detail = json.loads(view_proc.stdout)
        results.append(
            {
                "run_id": run_id,
                "created_at": meta["createdAt"],
                "jobs": detail.get("jobs", []),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Drill-down support — parse gh run view --log + ci.yml inline estimates so
# audit findings carry "which step inside the slow job" plus drift / re-shard
# recommendations. See issue #4070 for the spec.
# ---------------------------------------------------------------------------


# A line in `gh run view --log` looks like:
#   <job-name>\t<step-name>\t2026-05-05T19:00:11.123Z <content>
# or, for steps emitted with `::group::` in a shell script:
#   <job-name>\t<step-name>\t2026-05-05T19:00:11.123Z ##[group]<group-name>
#   <job-name>\t<step-name>\t2026-05-05T19:00:13.456Z ##[endgroup]
# We parse `##[group]<name>` start timestamps and pair each with the next
# `##[endgroup]` (or the next `##[group]` start, if endgroup is missing —
# defensive against truncated logs).

_GROUP_START_RE = re.compile(r"##\[group\](?P<name>.*)$")
_GROUP_END_RE = re.compile(r"##\[endgroup\]")
_LOG_LINE_RE = re.compile(
    r"^(?P<job>[^\t]+)\t(?P<step>[^\t]*)\t(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(?P<content>.*)$"
)


def parse_group_durations(log_text: str) -> list[dict[str, Any]]:
    """Parse `##[group]<name>` / `##[endgroup]` paired timestamps from a log.

    Returns a list of dicts ``{"name": str, "seconds": float, "step": str}``
    in encounter order. Each entry is the wall-clock between a `##[group]<name>`
    line and the next `##[endgroup]` line (or, if the log is truncated, the
    next `##[group]` line — better to undercount than crash).

    Lines that do not match the expected `<job>\\t<step>\\t<ts> <content>`
    shape are silently ignored. A log with zero `##[group]` markers returns
    an empty list — callers should treat this as "drill-down unavailable"
    rather than a failure.
    """
    durations: list[dict[str, Any]] = []
    open_group: dict[str, Any] | None = None
    for raw in log_text.splitlines():
        m = _LOG_LINE_RE.match(raw)
        if not m:
            continue
        ts = parse_iso(m.group("ts"))
        if ts is None:
            continue
        content = m.group("content")
        step = m.group("step")
        start_match = _GROUP_START_RE.match(content)
        end_match = _GROUP_END_RE.match(content)
        if start_match:
            # Close any prior open group as a defensive truncation guard.
            if open_group is not None:
                seconds = (ts - open_group["start"]).total_seconds()
                durations.append(
                    {
                        "name": open_group["name"],
                        "seconds": seconds,
                        "step": open_group["step"],
                    }
                )
            open_group = {
                "name": start_match.group("name"),
                "start": ts,
                "step": step,
            }
        elif end_match and open_group is not None:
            seconds = (ts - open_group["start"]).total_seconds()
            durations.append(
                {
                    "name": open_group["name"],
                    "seconds": seconds,
                    "step": open_group["step"],
                }
            )
            open_group = None
    return durations


# Match common inline estimate forms inside the matrix block of a job:
#   ~275s
#   ~275 seconds
#   ~5 min
#   Estimated wall-clock ~395s
#   roughly 660s
_ESTIMATE_RE = re.compile(
    r"~?\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m)\b",
    re.IGNORECASE,
)


def _estimate_to_seconds(value: float, unit: str) -> float:
    u = unit.lower()
    if u in ("s", "sec", "secs", "second", "seconds"):
        return value
    if u in ("m", "min", "mins", "minute", "minutes"):
        return value * 60.0
    return value


def _job_name_search_terms(job_name: str) -> list[str]:
    """Derive substring patterns that locate ``job_name`` inside ci.yml.

    `gh run view --json jobs` returns the display name (e.g.
    ``"scripts-tests (shell)"``) which never appears verbatim in ci.yml.
    The matrix-expanded display name is ``<job-id> (<matrix-value>)``; we
    extract the matrix value and synthesize a pattern that matches the YAML
    matrix line (e.g. ``- shard: shell``).

    Returns the patterns to try in priority order — most specific first.
    """
    candidates: list[str] = []
    m = re.match(r"^(?P<base>[^ (]+)\s*\((?P<value>[^)]+)\)\s*$", job_name)
    if m:
        candidates.append(f"shard: {m.group('value').strip()}")
        candidates.append(m.group("base").strip() + ":")
    else:
        candidates.append(f"shard: {job_name}")
        candidates.append(job_name + ":")
    candidates.append(job_name)
    # De-dup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def extract_step_estimates(ciyml_text: str, job_name: str) -> dict[str, float]:
    """Find inline `~Ns` / `~N seconds` estimates near ``job_name`` in ci.yml.

    The convention in `.github/workflows/ci.yml` is to leave a comment block
    like::

        # Shard shell: scripts/run-scripts-tests.sh only.
        # Dominated by test_agent_runner_entrypoint.sh (~275s) and
        # test_check_dispatcher_image_versions.sh (~58s).
        # Estimated wall-clock ~395s. See issue #3307.
        - shard: shell

    Returns a dict mapping step/test names that appear adjacent to a numeric
    estimate to the estimate in seconds.  We scan the comment block (any run
    of `#`-prefixed lines) immediately preceding a line that contains the
    job name (matrix shard line, job key, or job ``name:`` line).  Matching
    is intentionally loose — drift detection is a heuristic, not a
    correctness gate, so false positives just produce no drift signal.

    ``job_name`` accepts the gh-display form (``"scripts-tests (shell)"``);
    we synthesize candidate patterns via :func:`_job_name_search_terms`.

    A job that has no inline estimate returns an empty dict and the caller
    silently omits drift detection for that job.
    """
    estimates: dict[str, float] = {}
    lines = ciyml_text.splitlines()

    def _scan_at(i: int) -> bool:
        """Walk up contiguous comment lines above ``lines[i]`` and harvest estimates.

        Returns True iff at least one estimate was harvested.
        """
        comment_lines: list[str] = []
        j = i - 1
        while j >= 0:
            stripped = lines[j].strip()
            if stripped.startswith("#"):
                comment_lines.append(stripped.lstrip("#").strip())
                j -= 1
            elif stripped == "":
                break
            else:
                break
        comment_lines.reverse()
        harvested = False
        for cl in comment_lines:
            for m in _ESTIMATE_RE.finditer(cl):
                value = float(m.group("value"))
                unit = m.group("unit")
                seconds = _estimate_to_seconds(value, unit)
                pre = cl[: m.start()].rstrip()
                # Try last `*.sh` / `*.py` token before the estimate (test file).
                file_tokens = re.findall(r"[A-Za-z0-9_./-]+\.(?:sh|py)", pre)
                if file_tokens:
                    if estimates.setdefault(file_tokens[-1], seconds) == seconds:
                        harvested = True
                    continue
                # Job-level estimate: keyed under the search pattern.
                if (
                    "estimated wall-clock" in cl.lower()
                    or "estimated wall clock" in cl.lower()
                ):
                    if estimates.setdefault(job_name, seconds) == seconds:
                        harvested = True
                    continue
        return harvested

    # Try patterns in priority order; stop at the first pattern that yields
    # any estimate (avoids false hits from less-specific patterns).
    for pattern in _job_name_search_terms(job_name):
        any_harvest = False
        for i, line in enumerate(lines):
            if pattern not in line:
                continue
            if _scan_at(i):
                any_harvest = True
                # First good hit wins; stop searching this pattern.
                break
        if any_harvest:
            break
    return estimates


def fetch_job_log_via_gh(run_id: str, job_id: str, repo: str = REPO) -> str | None:
    """Fetch a single job's log via `gh run view --log --job=<id>`.

    Returns ``None`` if the call fails (auth, transient API error, missing
    job).  Drill-down is best-effort — the caller silently degrades to no
    drill-down on ``None``.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repo,
                "--log",
                "--job",
                str(job_id),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
        return proc.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def compute_drill_down(
    job: JobRun,
    *,
    log_text: str | None,
    step_estimates: dict[str, float] | None = None,
    top_n: int = DRILL_DOWN_TOP_N,
) -> dict[str, Any] | None:
    """Compute drill-down stats for a single job: slowest steps + re-shard math.

    Returns ``None`` when the log has no parseable `##[group]` markers, so
    callers can silently omit the drill-down rather than emit an empty
    section. This is the §4070 backstop AC: the script must keep working on
    short / unusual jobs that emit no markers.
    """
    if log_text is None:
        return None
    durations = parse_group_durations(log_text)
    if not durations:
        return None
    # Sort all groups by duration descending.
    sorted_groups = sorted(durations, key=lambda g: g["seconds"], reverse=True)
    top = sorted_groups[:top_n]
    estimates = step_estimates or {}
    slowest_steps: list[dict[str, Any]] = []
    for entry in top:
        step_obj: dict[str, Any] = {
            "name": entry["name"],
            "seconds": round(entry["seconds"], 1),
        }
        # Match the entry name against the estimate dict — accept either a
        # direct hit, or a basename hit (estimate keyed under "test_X.sh" but
        # the group name is "scripts/tests/test_X.sh").
        est_seconds = None
        if entry["name"] in estimates:
            est_seconds = estimates[entry["name"]]
        else:
            for key, val in estimates.items():
                if entry["name"].endswith(key) or key.endswith(entry["name"]):
                    est_seconds = val
                    break
        if est_seconds is not None and est_seconds > 0:
            drift = entry["seconds"] / est_seconds
            step_obj["estimate_seconds"] = round(est_seconds, 1)
            step_obj["drift_factor"] = round(drift, 2)
        slowest_steps.append(step_obj)
    # Re-shard math: if we extracted the slowest step into its own shard,
    # the remaining shard runs at sum_of_remaining_seconds; the new wall
    # clock is max(slowest_step, sum_of_remaining).
    total_seconds = sum(g["seconds"] for g in durations)
    slowest_seconds = sorted_groups[0]["seconds"]
    remaining_seconds = total_seconds - slowest_seconds
    if_split_wall_clock = max(slowest_seconds, remaining_seconds)
    split_savings = total_seconds - if_split_wall_clock
    drill: dict[str, Any] = {
        "slowest_steps": slowest_steps,
        "if_split_wall_clock_seconds": round(if_split_wall_clock, 1),
        "split_savings_seconds": round(split_savings, 1),
    }
    suggested: list[str] = []
    # Drift suggestion: trim any step with drift ≥ DRIFT_FACTOR_THRESHOLD.
    for step in slowest_steps:
        if step.get("drift_factor", 0) >= DRIFT_FACTOR_THRESHOLD:
            suggested.append(f"trim {step['name']}")
    # Re-shard suggestion: only when the slowest step dominates the total
    # group runtime (long-pole pattern) AND we'd save a meaningful absolute
    # number of seconds. Balanced jobs (slowest ≈ remaining) gain less from
    # splitting and don't warrant the suggestion.
    if (
        total_seconds > 0
        and (slowest_seconds / total_seconds) >= SPLIT_SLOWEST_DOMINANCE_THRESHOLD
        and split_savings >= SPLIT_SAVINGS_MIN_SECONDS
    ):
        suggested.append(f"split {slowest_steps[0]['name']} into separate shard")
    if suggested:
        drill["suggested_fixes"] = suggested
    return drill


def attach_drill_down(
    findings: list[Finding],
    runs: list[RunSummary],
    *,
    log_fetcher: Callable[[str, str], str | None] | None = None,
    ciyml_path: Path | None = None,
) -> None:
    """For each single-job finding, attach a `drill_down` to its details.

    ``log_fetcher`` defaults to :func:`fetch_job_log_via_gh`; tests inject a
    fake. ``ciyml_path`` defaults to ``.github/workflows/ci.yml`` resolved
    against the script's repo root; tests inject a fixture.
    """
    if log_fetcher is None:
        log_fetcher = fetch_job_log_via_gh
    ciyml_text = ""
    if ciyml_path is None:
        ciyml_path = (
            Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
        )
    try:
        ciyml_text = ciyml_path.read_text()
    except OSError:
        ciyml_text = ""

    # Build a quick lookup: run_id -> job_name -> JobRun.
    by_id: dict[str, dict[str, JobRun]] = {}
    for run in runs:
        by_id.setdefault(run.run_id, {})
        for j in run.jobs:
            by_id[run.run_id][j.name] = j

    for finding in findings:
        if finding.kind != "single-job":
            continue
        run_id = finding.details.get("run_id")
        job_name = finding.job_name
        if not run_id or not job_name:
            continue
        job = by_id.get(str(run_id), {}).get(job_name)
        if job is None or job.job_id is None:
            continue
        log_text = log_fetcher(str(run_id), job.job_id)
        estimates = extract_step_estimates(ciyml_text, job_name) if ciyml_text else {}
        drill = compute_drill_down(job, log_text=log_text, step_estimates=estimates)
        if drill is not None:
            finding.details["drill_down"] = drill


# ---------------------------------------------------------------------------
# Dedup helpers — match findings against existing GitHub issues.
#
# Spec (issue #4070):
#   - Match OPEN issues only. A previously-closed issue must not silently
#     suppress a regression on the same job.
#   - Match on (job_name, slowest_step_name) when drill-down identifies a
#     specific step; otherwise fall back to job_name alone.
#   - When a CLOSED prior issue matches, file a fresh issue with a
#     `Recurrence of #N` note rather than reopening or commenting.
# ---------------------------------------------------------------------------


@dataclass
class DedupMatch:
    """Outcome of matching a finding against existing issues."""

    kind: str  # "duplicate", "recurrence", "new"
    issue_number: int | None = None  # set for duplicate/recurrence


def _slowest_step_from_finding(finding: Finding) -> str | None:
    drill = finding.details.get("drill_down") or {}
    steps = drill.get("slowest_steps") or []
    if not steps:
        return None
    return steps[0].get("name")


def _issue_matches_finding(issue: dict[str, Any], finding: Finding) -> bool:
    """True if the issue's title/body mentions the finding's job (and step, if any)."""
    title = issue.get("title", "") or ""
    body = issue.get("body", "") or ""
    haystack = f"{title}\n{body}"
    if finding.job_name not in haystack:
        return False
    step = _slowest_step_from_finding(finding)
    if step:
        # Step-aware key: require both job and step to appear in the issue
        # text. Two distinct slow steps in the same job get distinct issues.
        return step in haystack
    return True


def classify_finding_against_issues(
    finding: Finding, issues: list[dict[str, Any]]
) -> DedupMatch:
    """Return a DedupMatch indicating whether to skip / file-as-recurrence / file-fresh.

    Filing rules (issue #4070):
      * If any OPEN issue matches the (job, slowest-step) key — return
        ``duplicate`` with the issue number; the caller skips filing.
      * Else if any CLOSED issue matches — return ``recurrence`` with the
        issue number; the caller files a NEW issue with a
        ``Recurrence of #N`` line in the body.
      * Else — return ``new``.
    """
    open_match: int | None = None
    closed_match: int | None = None
    for issue in issues:
        if not _issue_matches_finding(issue, finding):
            continue
        state = (issue.get("state") or "").lower()
        number = issue.get("number")
        if state == "open" and open_match is None:
            open_match = number
        elif state == "closed" and closed_match is None:
            closed_match = number
    if open_match is not None:
        return DedupMatch(kind="duplicate", issue_number=open_match)
    if closed_match is not None:
        return DedupMatch(kind="recurrence", issue_number=closed_match)
    return DedupMatch(kind="new")


def render_finding_issue_body(finding: Finding, dedup: DedupMatch | None = None) -> str:
    """Render the issue body the /audit skill should post for a CI-health finding.

    Includes the drill-down sections (`Slowest steps`, `Estimate drift`,
    `Suggested fixes`) when the finding has a `drill_down` payload, and a
    `Recurrence of #N` line when the dedup pass found a closed prior issue.
    """
    parts: list[str] = []
    parts.append("## Found by")
    parts.append("`/audit` skill (CI health, §1.8)")
    parts.append("")
    parts.append("## Finding")
    parts.append(finding.message)
    parts.append("")
    drill = finding.details.get("drill_down") or {}
    slowest = drill.get("slowest_steps") or []
    if slowest:
        parts.append("## Slowest steps inside the flagged job")
        parts.append("")
        parts.append("| Step | Measured (s) | Estimate (s) | Drift |")
        parts.append("|---|---:|---:|---:|")
        for step in slowest:
            est = step.get("estimate_seconds")
            drift = step.get("drift_factor")
            est_cell = f"{est:.0f}" if isinstance(est, (int, float)) else "—"
            drift_cell = f"{drift:.1f}×" if isinstance(drift, (int, float)) else "—"
            parts.append(
                f"| `{step['name']}` | {step['seconds']:.0f} | {est_cell} | {drift_cell} |"
            )
        parts.append("")
        # Estimate drift section.
        drifted = [
            s
            for s in slowest
            if isinstance(s.get("drift_factor"), (int, float))
            and s["drift_factor"] >= DRIFT_FACTOR_THRESHOLD
        ]
        if drifted:
            parts.append("## Estimate drift")
            parts.append("")
            for step in drifted:
                parts.append(
                    f"- `{step['name']}` measured {step['seconds']:.0f}s vs. "
                    f"~{step['estimate_seconds']:.0f}s estimate "
                    f"({step['drift_factor']:.1f}× drift, ≥ {DRIFT_FACTOR_THRESHOLD:.1f}× threshold)"
                )
            parts.append("")
        # Re-shard math.
        if "if_split_wall_clock_seconds" in drill:
            parts.append("## Re-shard math")
            parts.append("")
            parts.append(
                f"- If `{slowest[0]['name']}` were split into its own shard, "
                f"wall-clock would be {drill['if_split_wall_clock_seconds']:.0f}s "
                f"(savings: {drill['split_savings_seconds']:.0f}s)."
            )
            parts.append("")
        # Suggested fixes.
        suggested = drill.get("suggested_fixes") or []
        if suggested:
            parts.append("## Suggested fixes")
            parts.append("")
            for s in suggested:
                parts.append(f"- {s}")
            parts.append("")
    parts.append("## Run details")
    parts.append("")
    parts.append(f"- run_id: {finding.details.get('run_id', '?')}")
    parts.append(f"- duration: {finding.details.get('seconds', '?')}s")
    parts.append("")
    if dedup is not None and dedup.kind == "recurrence" and dedup.issue_number:
        parts.append(f"Recurrence of #{dedup.issue_number}.")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def compute_threshold_findings(runs: list[RunSummary]) -> list[Finding]:
    """Emit findings for single-job and total wall-clock thresholds (latest run)."""
    findings: list[Finding] = []
    if not runs:
        return findings
    latest = runs[-1]

    # Single-job threshold: any non-skipped job > 10 min in latest run.
    for job in latest.jobs:
        if job.skipped:
            continue
        dur = job.duration_seconds
        if dur is not None and dur > SINGLE_JOB_SECONDS_THRESHOLD:
            findings.append(
                Finding(
                    kind="single-job",
                    job_name=job.name,
                    message=f"Job '{job.name}' took {dur:.0f}s in run {latest.run_id} "
                    f"(> {SINGLE_JOB_SECONDS_THRESHOLD}s threshold)",
                    details={"run_id": latest.run_id, "seconds": dur},
                )
            )

    # Total wall-clock threshold: latest run's wall clock > 15 min.
    wall = latest.wall_clock_seconds
    if wall is not None and wall > TOTAL_WALL_CLOCK_THRESHOLD:
        findings.append(
            Finding(
                kind="wall-clock",
                job_name="(total)",
                message=f"Total CI wall clock was {wall:.0f}s in run {latest.run_id} "
                f"(> {TOTAL_WALL_CLOCK_THRESHOLD}s threshold)",
                details={"run_id": latest.run_id, "seconds": wall},
            )
        )

    return findings


def compute_trend_findings(runs: list[RunSummary]) -> list[Finding]:
    """Emit findings for per-job trend regressions across the sample window.

    Skipped jobs are **excluded** from the trend computation.  We require at
    least MIN_SAMPLES_PER_GROUP ran-samples in BOTH halves before reporting a
    trend regression, and both a percentage AND absolute delta must be
    exceeded.  This is the fix for #2401 — see module docstring.
    """
    findings: list[Finding] = []
    if len(runs) < 2 * MIN_SAMPLES_PER_GROUP:
        return findings

    # Collect per-job ran-duration sequences, ordered oldest → newest.
    all_job_names = sorted({job.name for run in runs for job in run.jobs})
    for job_name in all_job_names:
        durations: list[float] = []
        for run in runs:
            matches = [j for j in run.jobs if j.name == job_name]
            if not matches:
                continue
            job = matches[0]
            if job.skipped or job.duration_seconds is None:
                continue
            durations.append(job.duration_seconds)
        n = len(durations)
        if n < 2 * MIN_SAMPLES_PER_GROUP:
            # Not enough ran-samples to split into halves; skip.
            continue
        half = n // 2
        prior = durations[:half]
        recent = durations[-half:]
        prior_mean = sum(prior) / len(prior)
        recent_mean = sum(recent) / len(recent)
        if prior_mean <= 0:
            continue
        delta = recent_mean - prior_mean
        pct = (delta / prior_mean) * 100.0
        if pct >= TREND_PERCENT_THRESHOLD and delta >= TREND_ABSOLUTE_SECONDS_THRESHOLD:
            findings.append(
                Finding(
                    kind="trend",
                    job_name=job_name,
                    message=f"Job '{job_name}' trend regression: "
                    f"prior mean={prior_mean:.1f}s, recent mean={recent_mean:.1f}s, "
                    f"Δ={delta:+.1f}s ({pct:+.1f}%)",
                    details={
                        "prior_mean": prior_mean,
                        "recent_mean": recent_mean,
                        "delta_seconds": delta,
                        "delta_percent": pct,
                        "n_samples": n,
                    },
                )
            )

    return findings


def print_per_job_summary(runs: list[RunSummary]) -> None:
    """Print a compact per-run, per-job summary."""
    all_job_names = sorted({job.name for run in runs for job in run.jobs})
    # Compute per-job means (excluding skipped).
    print("CI runs analyzed (oldest → newest):")
    for run in runs:
        print(f"  {run.run_id}  {run.created_at.isoformat()}")
    print()
    print(f"{'Job':<40} {'N':>4} {'Mean':>8} {'Max':>8} {'Skipped':>8}")
    for job_name in all_job_names:
        durations = []
        skipped = 0
        for run in runs:
            matches = [j for j in run.jobs if j.name == job_name]
            if not matches:
                continue
            job = matches[0]
            if job.skipped:
                skipped += 1
                continue
            if job.duration_seconds is not None:
                durations.append(job.duration_seconds)
        if not durations:
            continue
        mean = sum(durations) / len(durations)
        maxd = max(durations)
        print(
            f"{job_name:<40} {len(durations):>4} {mean:>7.1f}s {maxd:>7.1f}s {skipped:>8}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--limit", type=int, default=10, help="Number of CI runs to sample"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument(
        "--from-file",
        type=str,
        default=None,
        help="Read run data from JSON file (for tests / replay)",
    )
    parser.add_argument(
        "--no-drill-down",
        action="store_true",
        help="Skip per-step drill-down for jobs > 10 min "
        "(otherwise we fetch each flagged job's log via `gh run view --log` "
        "and parse `##[group]` markers; opt out for speed during ad-hoc runs)",
    )
    parser.add_argument(
        "--ciyml",
        type=str,
        default=None,
        help="Path to .github/workflows/ci.yml for inline-estimate extraction "
        "(defaults to repo root; tests override this)",
    )
    parser.add_argument(
        "--drill-down-log-file",
        type=str,
        default=None,
        help="Replay a single job's log from a file rather than fetching via gh "
        "(testing / debugging); applied to every single-job finding",
    )
    args = parser.parse_args(argv)

    try:
        if args.from_file:
            with open(args.from_file) as f:
                raw = json.load(f)
        else:
            raw = fetch_runs_via_gh(args.limit)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    runs = build_runs_from_json(raw)
    findings = compute_threshold_findings(runs) + compute_trend_findings(runs)

    if not args.no_drill_down and findings:
        log_fetcher: Callable[[str, str], str | None] | None = None
        if args.drill_down_log_file:
            replay_text = Path(args.drill_down_log_file).read_text()

            def _replay(_run_id: str, _job_id: str) -> str | None:
                return replay_text

            log_fetcher = _replay
        ciyml_path = Path(args.ciyml) if args.ciyml else None
        attach_drill_down(
            findings,
            runs,
            log_fetcher=log_fetcher,
            ciyml_path=ciyml_path,
        )

    if args.json:
        payload = {
            "runs": [r.run_id for r in runs],
            "findings": [
                {
                    "kind": f.kind,
                    "job_name": f.job_name,
                    "message": f.message,
                    "details": f.details,
                }
                for f in findings
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_per_job_summary(runs)
        print()
        if findings:
            print(f"Findings ({len(findings)}):")
            for f in findings:
                print(f"  [{f.kind}] {f.message}")
                drill = f.details.get("drill_down") if hasattr(f, "details") else None
                if drill:
                    for step in drill.get("slowest_steps", []):
                        drift = step.get("drift_factor")
                        drift_str = (
                            f" (drift {drift:.1f}× vs ~{step.get('estimate_seconds', 0):.0f}s)"
                            if drift is not None
                            else ""
                        )
                        print(
                            f"      step '{step['name']}' "
                            f"{step['seconds']:.0f}s{drift_str}"
                        )
                    if drill.get("suggested_fixes"):
                        print(f"      suggested: {', '.join(drill['suggested_fixes'])}")
        else:
            print("No findings — CI health is within thresholds.")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
