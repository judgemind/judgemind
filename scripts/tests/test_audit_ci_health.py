"""Tests for the audit_ci_health script.

Primary regression target: the bug described in #2401, where skipped jobs
were counted as duration = 0 in the trend-regression detector, producing
false positives for `ingestion-tests` and `scraper-framework-tests`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit_ci_health import (  # noqa: E402 — sys.path manipulation above
    MIN_SAMPLES_PER_GROUP,
    SINGLE_JOB_SECONDS_THRESHOLD,
    TOTAL_WALL_CLOCK_THRESHOLD,
    TREND_ABSOLUTE_SECONDS_THRESHOLD,
    TREND_PERCENT_THRESHOLD,
    build_runs_from_json,
    compute_threshold_findings,
    compute_trend_findings,
    main,
)


def _make_job(
    name: str,
    *,
    start_offset_s: float,
    duration_s: float,
    conclusion: str = "success",
    test_step_duration_s: float | None = None,
) -> dict[str, object]:
    """Build a job-record dict matching gh run view --json jobs output."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=start_offset_s)
    t1 = t0 + timedelta(seconds=duration_s)
    steps: list[dict[str, object]] = []
    if test_step_duration_s is not None:
        test_start = t0 + timedelta(seconds=3)  # after setup
        test_end = test_start + timedelta(seconds=test_step_duration_s)
        steps.append(
            {
                "name": "Test",
                "startedAt": test_start.isoformat().replace("+00:00", "Z"),
                "completedAt": test_end.isoformat().replace("+00:00", "Z"),
            }
        )
    return {
        "name": name,
        "conclusion": conclusion,
        "startedAt": t0.isoformat().replace("+00:00", "Z"),
        "completedAt": t1.isoformat().replace("+00:00", "Z"),
        "steps": steps,
    }


def _make_skipped_job(name: str) -> dict[str, object]:
    """Build a skipped job — missing timestamps is valid for skipped jobs."""
    return {
        "name": name,
        "conclusion": "skipped",
        "startedAt": None,
        "completedAt": None,
        "steps": [],
    }


def _make_run(
    run_id: str, created_iso: str, jobs: list[dict[str, object]]
) -> dict[str, object]:
    return {"run_id": run_id, "created_at": created_iso, "jobs": jobs}


class TestBuildRunsFromJson:
    def test_parses_basic_run(self) -> None:
        raw = [
            _make_run(
                "1",
                "2026-01-01T00:00:00Z",
                [_make_job("ingestion-tests", start_offset_s=0, duration_s=70)],
            )
        ]
        runs = build_runs_from_json(raw)
        assert len(runs) == 1
        assert runs[0].run_id == "1"
        assert len(runs[0].jobs) == 1
        assert runs[0].jobs[0].duration_seconds == 70

    def test_sorts_oldest_to_newest(self) -> None:
        raw = [
            _make_run("newer", "2026-01-02T00:00:00Z", []),
            _make_run("older", "2026-01-01T00:00:00Z", []),
        ]
        runs = build_runs_from_json(raw)
        assert [r.run_id for r in runs] == ["older", "newer"]

    def test_handles_skipped_jobs(self) -> None:
        raw = [
            _make_run(
                "1", "2026-01-01T00:00:00Z", [_make_skipped_job("ingestion-tests")]
            ),
        ]
        runs = build_runs_from_json(raw)
        assert runs[0].jobs[0].skipped is True
        assert runs[0].jobs[0].duration_seconds is None

    def test_captures_test_step_duration(self) -> None:
        raw = [
            _make_run(
                "1",
                "2026-01-01T00:00:00Z",
                [
                    _make_job(
                        "ingestion-tests",
                        start_offset_s=0,
                        duration_s=70,
                        test_step_duration_s=58,
                    )
                ],
            )
        ]
        runs = build_runs_from_json(raw)
        assert runs[0].jobs[0].test_step_seconds == 58


class TestTrendFindingsSkippedJobsBug:
    """#2401 regression target — skipped jobs must not be counted as zero."""

    def test_skipped_jobs_do_not_trigger_false_regression(self) -> None:
        # Five oldest runs: job skipped in 3 of them, ran at ~70s in 2.
        # Five newest runs: job ran at ~70s in all 5.
        # Buggy behaviour: prior mean ≈ 28s (3 zeros + 2×70s), recent mean = 70s,
        # delta = +150% → false positive.
        # Correct behaviour: prior mean = 70s (only ran samples), delta ≈ 0%.
        raw: list[dict[str, object]] = []
        # 5 oldest — mostly skipped
        for i, skipped in enumerate([True, True, True, False, False]):
            if skipped:
                jobs: list[dict[str, object]] = [_make_skipped_job("ingestion-tests")]
            else:
                jobs = [_make_job("ingestion-tests", start_offset_s=0, duration_s=70)]
            raw.append(_make_run(f"old-{i}", f"2026-01-0{i + 1}T00:00:00Z", jobs))
        # 5 newest — all ran
        for i in range(5):
            jobs = [_make_job("ingestion-tests", start_offset_s=0, duration_s=70)]
            raw.append(_make_run(f"new-{i}", f"2026-01-1{i}T00:00:00Z", jobs))
        runs = build_runs_from_json(raw)
        findings = compute_trend_findings(runs)
        trend_findings = [f for f in findings if f.job_name == "ingestion-tests"]
        assert trend_findings == [], (
            "Skipped jobs must be excluded from trend computation. Buggy math would "
            f"have reported regressions: {[f.message for f in trend_findings]}"
        )

    def test_real_regression_is_still_detected(self) -> None:
        # Ten runs, job ran in all of them — old 30s, new 70s. Real +133% regression.
        raw: list[dict[str, object]] = []
        for i in range(5):
            jobs = [_make_job("ingestion-tests", start_offset_s=0, duration_s=30)]
            raw.append(_make_run(f"old-{i}", f"2026-01-0{i + 1}T00:00:00Z", jobs))
        for i in range(5):
            jobs = [_make_job("ingestion-tests", start_offset_s=0, duration_s=70)]
            raw.append(_make_run(f"new-{i}", f"2026-01-1{i}T00:00:00Z", jobs))
        runs = build_runs_from_json(raw)
        findings = compute_trend_findings(runs)
        trend_findings = [f for f in findings if f.job_name == "ingestion-tests"]
        assert len(trend_findings) == 1
        assert (
            "+40.0s" in trend_findings[0].message
            or "40.0s" in trend_findings[0].message
        )
        assert trend_findings[0].details["delta_percent"] == pytest.approx(
            133.33, rel=0.01
        )

    def test_minimum_sample_size_enforced(self) -> None:
        # Only 4 of 10 runs executed the job — not enough to split into halves
        # of MIN_SAMPLES_PER_GROUP each.
        assert MIN_SAMPLES_PER_GROUP >= 2
        raw: list[dict[str, object]] = []
        for i in range(10):
            if i % 3 == 0:
                jobs: list[dict[str, object]] = [
                    _make_job(
                        "ingestion-tests",
                        start_offset_s=0,
                        duration_s=150 if i > 5 else 30,
                    )
                ]
            else:
                jobs = [_make_skipped_job("ingestion-tests")]
            raw.append(_make_run(f"r-{i}", f"2026-01-{i + 1:02d}T00:00:00Z", jobs))
        runs = build_runs_from_json(raw)
        findings = compute_trend_findings(runs)
        trend_findings = [f for f in findings if f.job_name == "ingestion-tests"]
        # 4 ran samples total; 4 < 2 * MIN_SAMPLES_PER_GROUP = 6, so no finding emitted.
        assert trend_findings == []

    def test_small_absolute_delta_does_not_trigger(self) -> None:
        # Trivially short job: 3s → 4s is +33% but < 15s absolute threshold.
        raw: list[dict[str, object]] = []
        for i in range(5):
            jobs = [_make_job("tiny-check", start_offset_s=0, duration_s=3)]
            raw.append(_make_run(f"old-{i}", f"2026-01-0{i + 1}T00:00:00Z", jobs))
        for i in range(5):
            jobs = [_make_job("tiny-check", start_offset_s=0, duration_s=4)]
            raw.append(_make_run(f"new-{i}", f"2026-01-1{i}T00:00:00Z", jobs))
        runs = build_runs_from_json(raw)
        findings = compute_trend_findings(runs)
        assert findings == []


class TestThresholdFindings:
    def test_single_job_exceeds_10_minutes(self) -> None:
        # One run with a job > 10 minutes.
        jobs = [
            _make_job(
                "slow-tests",
                start_offset_s=0,
                duration_s=SINGLE_JOB_SECONDS_THRESHOLD + 1,
            )
        ]
        raw = [_make_run("r1", "2026-01-01T00:00:00Z", jobs)]
        runs = build_runs_from_json(raw)
        findings = compute_threshold_findings(runs)
        assert any(
            f.kind == "single-job" and f.job_name == "slow-tests" for f in findings
        )

    def test_job_under_threshold_does_not_trigger(self) -> None:
        jobs = [_make_job("fast-tests", start_offset_s=0, duration_s=60)]
        raw = [_make_run("r1", "2026-01-01T00:00:00Z", jobs)]
        runs = build_runs_from_json(raw)
        findings = compute_threshold_findings(runs)
        assert findings == []

    def test_total_wall_clock_threshold(self) -> None:
        # One job, running for > 15 minutes — forces wall clock > threshold.
        jobs = [
            _make_job(
                "long-job",
                start_offset_s=0,
                duration_s=TOTAL_WALL_CLOCK_THRESHOLD + 1,
            )
        ]
        raw = [_make_run("r1", "2026-01-01T00:00:00Z", jobs)]
        runs = build_runs_from_json(raw)
        findings = compute_threshold_findings(runs)
        # single-job AND wall-clock both fire in this case.
        assert any(f.kind == "wall-clock" for f in findings)

    def test_skipped_jobs_not_counted_in_wall_clock(self) -> None:
        jobs = [
            _make_job("quick", start_offset_s=0, duration_s=30),
            _make_skipped_job("nope"),
        ]
        raw = [_make_run("r1", "2026-01-01T00:00:00Z", jobs)]
        runs = build_runs_from_json(raw)
        assert runs[0].wall_clock_seconds == 30


class TestMainCli:
    def test_from_file_produces_json_output(self, tmp_path: Path, capsys) -> None:
        # Ten runs, no findings expected.
        raw: list[dict[str, object]] = []
        for i in range(10):
            jobs = [_make_job("ingestion-tests", start_offset_s=0, duration_s=70)]
            raw.append(_make_run(f"r-{i}", f"2026-01-{i + 1:02d}T00:00:00Z", jobs))
        runs_file = tmp_path / "runs.json"
        runs_file.write_text(json.dumps(raw))
        rc = main(["--from-file", str(runs_file), "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out)
        assert payload["findings"] == []
        assert len(payload["runs"]) == 10

    def test_from_file_reports_real_regression(self, tmp_path: Path, capsys) -> None:
        raw: list[dict[str, object]] = []
        for i in range(5):
            raw.append(
                _make_run(
                    f"old-{i}",
                    f"2026-01-0{i + 1}T00:00:00Z",
                    [_make_job("ingestion-tests", start_offset_s=0, duration_s=30)],
                )
            )
        for i in range(5):
            raw.append(
                _make_run(
                    f"new-{i}",
                    f"2026-01-1{i}T00:00:00Z",
                    [_make_job("ingestion-tests", start_offset_s=0, duration_s=70)],
                )
            )
        runs_file = tmp_path / "runs.json"
        runs_file.write_text(json.dumps(raw))
        rc = main(["--from-file", str(runs_file), "--json"])
        out = capsys.readouterr().out
        assert rc == 1
        payload = json.loads(out)
        assert len(payload["findings"]) == 1
        assert payload["findings"][0]["kind"] == "trend"
        assert payload["findings"][0]["job_name"] == "ingestion-tests"


class TestConstants:
    """Pin threshold constants so the SKILL.md documentation stays in sync."""

    def test_single_job_threshold_matches_skill(self) -> None:
        assert SINGLE_JOB_SECONDS_THRESHOLD == 10 * 60

    def test_wall_clock_threshold_matches_skill(self) -> None:
        assert TOTAL_WALL_CLOCK_THRESHOLD == 15 * 60

    def test_trend_percent_threshold_matches_skill(self) -> None:
        assert TREND_PERCENT_THRESHOLD == 20.0

    def test_trend_absolute_threshold_is_nonzero(self) -> None:
        assert TREND_ABSOLUTE_SECONDS_THRESHOLD > 0
