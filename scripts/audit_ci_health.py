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
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

REPO = "judgemind/judgemind"
WORKFLOW = "ci.yml"

# Threshold constants — documented here for discoverability.
SINGLE_JOB_SECONDS_THRESHOLD = 10 * 60  # 10 minutes
TOTAL_WALL_CLOCK_THRESHOLD = 15 * 60  # 15 minutes
TREND_PERCENT_THRESHOLD = 20.0  # +20% mean delta
TREND_ABSOLUTE_SECONDS_THRESHOLD = 15.0  # +15s absolute delta
MIN_SAMPLES_PER_GROUP = 3  # require ≥3 ran-samples in BOTH halves


@dataclass
class JobRun:
    """One job's timing in one CI run."""

    run_id: str
    name: str
    conclusion: str  # "success", "failure", "skipped"
    started_at: datetime | None
    completed_at: datetime | None
    test_step_seconds: float | None = None

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
        else:
            print("No findings — CI health is within thresholds.")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
