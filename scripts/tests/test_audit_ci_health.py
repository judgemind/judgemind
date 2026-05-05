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
    DRIFT_FACTOR_THRESHOLD,
    MIN_SAMPLES_PER_GROUP,
    SINGLE_JOB_SECONDS_THRESHOLD,
    SPLIT_SAVINGS_MIN_SECONDS,
    SPLIT_SLOWEST_DOMINANCE_THRESHOLD,
    TOTAL_WALL_CLOCK_THRESHOLD,
    TREND_ABSOLUTE_SECONDS_THRESHOLD,
    TREND_PERCENT_THRESHOLD,
    DedupMatch,
    Finding,
    JobRun,
    attach_drill_down,
    build_runs_from_json,
    classify_finding_against_issues,
    compute_drill_down,
    compute_threshold_findings,
    compute_trend_findings,
    extract_step_estimates,
    main,
    parse_group_durations,
    render_finding_issue_body,
)


def _make_job(
    name: str,
    *,
    start_offset_s: float,
    duration_s: float,
    conclusion: str = "success",
    test_step_duration_s: float | None = None,
    database_id: int | None = None,
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
    job: dict[str, object] = {
        "name": name,
        "conclusion": conclusion,
        "startedAt": t0.isoformat().replace("+00:00", "Z"),
        "completedAt": t1.isoformat().replace("+00:00", "Z"),
        "steps": steps,
    }
    if database_id is not None:
        job["databaseId"] = database_id
    return job


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

    def test_drill_down_constants_present(self) -> None:
        assert DRIFT_FACTOR_THRESHOLD >= 1.0
        assert SPLIT_SAVINGS_MIN_SECONDS > 0
        assert 0 < SPLIT_SLOWEST_DOMINANCE_THRESHOLD < 1


# ---------------------------------------------------------------------------
# §4070 drill-down tests
# ---------------------------------------------------------------------------


def _log_line(job: str, step: str, ts: str, content: str) -> str:
    """Build one line of `gh run view --log` output."""
    return f"{job}\t{step}\t{ts} {content}"


def _shell_log_fixture() -> str:
    """A small log resembling `gh run view --log` for the shell shard.

    Three `::group::test_X` runs:
      * test_agent_runner_entrypoint.sh — 663s
      * test_check_dispatcher_image_versions.sh — 66s
      * test_other_quick.sh — 5s
    """
    job = "scripts-tests (shell)"
    step = "Run all scripts/tests shell tests"
    lines = [
        _log_line(
            job,
            step,
            "2026-05-05T19:00:11.000Z",
            "##[group]scripts/tests/test_agent_runner_entrypoint.sh",
        ),
        _log_line(job, step, "2026-05-05T19:05:00.000Z", "PASS: t_first"),
        _log_line(job, step, "2026-05-05T19:11:14.000Z", "##[endgroup]"),
        _log_line(
            job,
            step,
            "2026-05-05T19:11:14.500Z",
            "##[group]scripts/tests/test_check_dispatcher_image_versions.sh",
        ),
        _log_line(job, step, "2026-05-05T19:12:20.500Z", "##[endgroup]"),
        _log_line(
            job,
            step,
            "2026-05-05T19:12:21.000Z",
            "##[group]scripts/tests/test_other_quick.sh",
        ),
        _log_line(job, step, "2026-05-05T19:12:26.000Z", "##[endgroup]"),
    ]
    return "\n".join(lines) + "\n"


class TestParseGroupDurations:
    def test_parses_paired_group_endgroup(self) -> None:
        log = _shell_log_fixture()
        groups = parse_group_durations(log)
        names = [g["name"] for g in groups]
        # All three test groups should appear, in encounter order.
        assert names == [
            "scripts/tests/test_agent_runner_entrypoint.sh",
            "scripts/tests/test_check_dispatcher_image_versions.sh",
            "scripts/tests/test_other_quick.sh",
        ]
        # Durations match the timestamps in the fixture (within ±0.5s).
        secs = {g["name"]: g["seconds"] for g in groups}
        assert secs["scripts/tests/test_agent_runner_entrypoint.sh"] == pytest.approx(
            663, abs=1
        )
        assert secs[
            "scripts/tests/test_check_dispatcher_image_versions.sh"
        ] == pytest.approx(66, abs=1)
        assert secs["scripts/tests/test_other_quick.sh"] == pytest.approx(5, abs=1)

    def test_log_with_no_group_markers_returns_empty(self) -> None:
        log = (
            "job-x\tstep-y\t2026-05-05T19:00:00Z setup\n"
            "job-x\tstep-y\t2026-05-05T19:00:01Z all good\n"
        )
        assert parse_group_durations(log) == []

    def test_handles_truncated_log_open_group_at_eof(self) -> None:
        # `##[group]` without a closing `##[endgroup]` should not crash;
        # the open group is silently dropped (better undercount than crash).
        log = (
            _log_line("j", "s", "2026-05-05T00:00:00Z", "##[group]hanging-group") + "\n"
        )
        assert parse_group_durations(log) == []

    def test_consecutive_groups_close_implicitly(self) -> None:
        # Two `##[group]` lines in a row (missing first endgroup): the
        # first group should be implicitly closed at the second start.
        log = "\n".join(
            [
                _log_line("j", "s", "2026-05-05T00:00:00Z", "##[group]first"),
                _log_line("j", "s", "2026-05-05T00:00:10Z", "##[group]second"),
                _log_line("j", "s", "2026-05-05T00:00:15Z", "##[endgroup]"),
            ]
        )
        groups = parse_group_durations(log)
        assert [g["name"] for g in groups] == ["first", "second"]
        assert groups[0]["seconds"] == pytest.approx(10, abs=1)
        assert groups[1]["seconds"] == pytest.approx(5, abs=1)

    def test_ignores_lines_without_log_prefix(self) -> None:
        # Headers, banners, etc. without the `<job>\t<step>\t<ts> ...` shape
        # should be ignored, not crash.
        log = "Run started at 19:00:00\n" + _shell_log_fixture()
        groups = parse_group_durations(log)
        assert len(groups) == 3


CIYML_FIXTURE = """
  scripts-tests:
    needs: detect-changes
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          # Shard python: pytest suites + inline shell guards.
          # pytest-xdist parallelises the suite. Estimated wall-clock ~200s.
          - shard: python
          # Shard shell: scripts/run-scripts-tests.sh only.
          # Dominated by test_agent_runner_entrypoint.sh (~275s) and
          # test_check_dispatcher_image_versions.sh (~58s).
          # Estimated wall-clock ~395s. See issue #3307.
          - shard: shell
"""


class TestExtractStepEstimates:
    def test_extracts_test_filename_estimates(self) -> None:
        estimates = extract_step_estimates(CIYML_FIXTURE, "shard: shell")
        # Test-filename keys take priority over the job-level estimate.
        assert estimates["test_agent_runner_entrypoint.sh"] == 275.0
        assert estimates["test_check_dispatcher_image_versions.sh"] == 58.0

    def test_returns_empty_when_no_inline_estimate(self) -> None:
        ci = """
        - shard: never-mentioned-with-estimate
"""
        # No comment block above → empty dict, no crash.
        assert extract_step_estimates(ci, "never-mentioned-with-estimate") == {}

    def test_handles_minutes_unit(self) -> None:
        ci = """
        # Estimated wall-clock ~5 min total.
        - shard: minute-form
"""
        e = extract_step_estimates(ci, "shard: minute-form")
        # Job-level estimate keyed under the searched name.
        assert e["shard: minute-form"] == 300.0

    def test_handles_gh_display_name_form(self) -> None:
        # `gh run view --json jobs` returns "<job-id> (<matrix-value>)";
        # extract_step_estimates must derive `shard: <value>` to find the
        # YAML matrix line above the comment block.
        e = extract_step_estimates(CIYML_FIXTURE, "scripts-tests (shell)")
        assert e["test_agent_runner_entrypoint.sh"] == 275.0


class TestComputeDrillDown:
    def test_returns_top3_slowest_with_estimates(self) -> None:
        log = _shell_log_fixture()
        estimates = {
            "test_agent_runner_entrypoint.sh": 275.0,
            "test_check_dispatcher_image_versions.sh": 58.0,
        }
        job = JobRun(
            run_id="r1",
            name="scripts-tests (shell)",
            conclusion="success",
            started_at=None,
            completed_at=None,
        )
        drill = compute_drill_down(job, log_text=log, step_estimates=estimates)
        assert drill is not None
        names = [s["name"] for s in drill["slowest_steps"]]
        assert names[0] == "scripts/tests/test_agent_runner_entrypoint.sh"
        # Drift factor for the long-pole step is ~2.4× (663/275).
        long_pole = drill["slowest_steps"][0]
        assert long_pole["drift_factor"] == pytest.approx(2.41, abs=0.05)
        assert long_pole["estimate_seconds"] == 275.0
        # Re-shard math: total ~734s, isolating long pole gives wall-clock 663s
        # (the long-pole's own seconds, since the remaining ~71s < long pole).
        assert drill["if_split_wall_clock_seconds"] == pytest.approx(663, abs=1)
        assert drill["split_savings_seconds"] == pytest.approx(71, abs=1)
        # Suggested fixes include trim + split.
        assert any(
            "trim" in s and "test_agent_runner_entrypoint" in s
            for s in drill["suggested_fixes"]
        )
        assert any("split" in s for s in drill["suggested_fixes"])

    def test_returns_none_when_log_has_no_group_markers(self) -> None:
        # §4070 backstop AC: silently omit drill-down on marker-less logs.
        log = "job-x\tstep-y\t2026-05-05T19:00:00Z normal output line\n"
        job = JobRun(
            run_id="r1",
            name="quick-job",
            conclusion="success",
            started_at=None,
            completed_at=None,
        )
        assert compute_drill_down(job, log_text=log) is None

    def test_returns_none_when_log_text_is_none(self) -> None:
        job = JobRun(
            run_id="r1",
            name="job",
            conclusion="success",
            started_at=None,
            completed_at=None,
        )
        assert compute_drill_down(job, log_text=None) is None

    def test_no_split_suggestion_when_savings_below_threshold(self) -> None:
        # Three roughly-balanced groups → splitting the slowest doesn't help.
        log = "\n".join(
            [
                _log_line("j", "s", "2026-05-05T00:00:00Z", "##[group]a"),
                _log_line("j", "s", "2026-05-05T00:01:40Z", "##[endgroup]"),
                _log_line("j", "s", "2026-05-05T00:01:41Z", "##[group]b"),
                _log_line("j", "s", "2026-05-05T00:03:21Z", "##[endgroup]"),
                _log_line("j", "s", "2026-05-05T00:03:22Z", "##[group]c"),
                _log_line("j", "s", "2026-05-05T00:05:02Z", "##[endgroup]"),
            ]
        )
        job = JobRun(
            run_id="r1",
            name="balanced",
            conclusion="success",
            started_at=None,
            completed_at=None,
        )
        drill = compute_drill_down(job, log_text=log)
        assert drill is not None
        # No split suggestion — savings would be too small.
        assert not any("split" in s for s in drill.get("suggested_fixes") or [])


class TestAttachDrillDown:
    def test_attaches_drill_down_to_single_job_finding(self, tmp_path: Path) -> None:
        ciyml = tmp_path / "ci.yml"
        ciyml.write_text(CIYML_FIXTURE)
        # Build a run with a > 10 min job.
        raw = [
            _make_run(
                "rX",
                "2026-05-05T19:00:00Z",
                [
                    _make_job(
                        "shard: shell",
                        start_offset_s=0,
                        duration_s=SINGLE_JOB_SECONDS_THRESHOLD + 100,
                        database_id=99,
                    )
                ],
            )
        ]
        runs = build_runs_from_json(raw)
        findings = compute_threshold_findings(runs)
        assert findings, "expected a single-job finding above 10 min"
        log_text = _shell_log_fixture()
        attach_drill_down(
            findings,
            runs,
            log_fetcher=lambda _r, _j: log_text,
            ciyml_path=ciyml,
        )
        single_jobs = [f for f in findings if f.kind == "single-job"]
        assert single_jobs[0].details.get("drill_down") is not None
        drill = single_jobs[0].details["drill_down"]
        assert (
            drill["slowest_steps"][0]["name"]
            == "scripts/tests/test_agent_runner_entrypoint.sh"
        )

    def test_silently_omits_when_no_log(self, tmp_path: Path) -> None:
        ciyml = tmp_path / "ci.yml"
        ciyml.write_text(CIYML_FIXTURE)
        raw = [
            _make_run(
                "rY",
                "2026-05-05T19:00:00Z",
                [
                    _make_job(
                        "no-marker-job",
                        start_offset_s=0,
                        duration_s=SINGLE_JOB_SECONDS_THRESHOLD + 1,
                        database_id=10,
                    )
                ],
            )
        ]
        runs = build_runs_from_json(raw)
        findings = compute_threshold_findings(runs)
        attach_drill_down(
            findings,
            runs,
            log_fetcher=lambda _r, _j: None,
            ciyml_path=ciyml,
        )
        # No drill_down on the finding — graceful degradation.
        assert "drill_down" not in findings[0].details


class TestRenderFindingIssueBody:
    def test_body_contains_drill_down_sections(self, tmp_path: Path) -> None:
        ciyml = tmp_path / "ci.yml"
        ciyml.write_text(CIYML_FIXTURE)
        raw = [
            _make_run(
                "rX",
                "2026-05-05T19:00:00Z",
                [
                    _make_job(
                        "shard: shell",
                        start_offset_s=0,
                        duration_s=SINGLE_JOB_SECONDS_THRESHOLD + 100,
                        database_id=99,
                    )
                ],
            )
        ]
        runs = build_runs_from_json(raw)
        findings = compute_threshold_findings(runs)
        attach_drill_down(
            findings,
            runs,
            log_fetcher=lambda _r, _j: _shell_log_fixture(),
            ciyml_path=ciyml,
        )
        body = render_finding_issue_body(findings[0])
        assert "Slowest steps inside the flagged job" in body
        assert "Estimate drift" in body
        assert "Suggested fixes" in body
        assert "test_agent_runner_entrypoint.sh" in body
        # 2.4× drift factor surfaces in the drift section.
        assert "2.4×" in body or "2.41×" in body

    def test_body_omits_drill_sections_when_no_drill_down(self) -> None:
        f = Finding(
            kind="single-job",
            job_name="quick-job",
            message="Job 'quick-job' took 700s",
            details={"run_id": "r1", "seconds": 700},
        )
        body = render_finding_issue_body(f)
        assert "Slowest steps" not in body
        assert "Estimate drift" not in body

    def test_body_includes_recurrence_line_for_closed_match(self) -> None:
        f = Finding(
            kind="single-job",
            job_name="quick-job",
            message="Job 'quick-job' took 700s",
            details={"run_id": "r1", "seconds": 700},
        )
        body = render_finding_issue_body(
            f, dedup=DedupMatch(kind="recurrence", issue_number=4067)
        )
        assert "Recurrence of #4067" in body


class TestDedupClassification:
    def _step_finding(self, job: str, step: str | None) -> Finding:
        details: dict[str, object] = {"run_id": "r1", "seconds": 700}
        if step:
            details["drill_down"] = {
                "slowest_steps": [{"name": step, "seconds": 663.0}],
            }
        return Finding(
            kind="single-job",
            job_name=job,
            message=f"Job '{job}' took 700s",
            details=details,
        )

    def test_open_match_returns_duplicate(self) -> None:
        finding = self._step_finding("scripts-tests (shell)", "test_X.sh")
        issues = [
            {
                "number": 999,
                "state": "open",
                "title": "perf(ci): scripts-tests (shell) too slow",
                "body": "Inside that shard, test_X.sh is the long pole.",
            }
        ]
        m = classify_finding_against_issues(finding, issues)
        assert m.kind == "duplicate"
        assert m.issue_number == 999

    def test_closed_match_returns_recurrence(self) -> None:
        # §4070 AC: closed issues do NOT silence regressions; they trigger
        # a recurrence path with a `Recurrence of #N` body line.
        finding = self._step_finding("scripts-tests (shell)", "test_X.sh")
        issues = [
            {
                "number": 3313,
                "state": "closed",
                "title": "scripts-tests (shell) flagged > 10 min",
                "body": "Was fixed by sharding. test_X.sh is still in the shard.",
            }
        ]
        m = classify_finding_against_issues(finding, issues)
        assert m.kind == "recurrence"
        assert m.issue_number == 3313

    def test_no_match_returns_new(self) -> None:
        finding = self._step_finding("unique-job", "unique_test.sh")
        m = classify_finding_against_issues(finding, [])
        assert m.kind == "new"

    def test_dedup_key_includes_step_name(self) -> None:
        # Two findings against the same job but different slowest steps
        # should both be treated as new — dedup key is (job, step).
        f_a = self._step_finding("scripts-tests (shell)", "test_a.sh")
        f_b = self._step_finding("scripts-tests (shell)", "test_b.sh")
        issues = [
            {
                "number": 100,
                "state": "open",
                "title": "scripts-tests (shell) — test_a.sh is the long pole",
                "body": "test_a.sh is slow.",
            }
        ]
        # f_a matches the existing test_a.sh issue; f_b does not.
        assert classify_finding_against_issues(f_a, issues).kind == "duplicate"
        assert classify_finding_against_issues(f_b, issues).kind == "new"

    def test_falls_back_to_job_name_when_no_drill_down(self) -> None:
        # No drill_down on the finding → match on job name alone.
        f = self._step_finding("misc-job", None)
        issues = [
            {
                "number": 50,
                "state": "open",
                "title": "misc-job is slow",
                "body": "see attached.",
            }
        ]
        assert classify_finding_against_issues(f, issues).kind == "duplicate"

    def test_open_overrides_closed_when_both_match(self) -> None:
        f = self._step_finding("job-x", "step-y")
        issues = [
            {
                "number": 1,
                "state": "closed",
                "title": "job-x slow (step-y)",
                "body": "old",
            },
            {
                "number": 2,
                "state": "open",
                "title": "job-x slow (step-y)",
                "body": "current",
            },
        ]
        m = classify_finding_against_issues(f, issues)
        assert m.kind == "duplicate"
        assert m.issue_number == 2


class TestMainCliDrillDown:
    def test_json_output_contains_drill_down(self, tmp_path: Path, capsys) -> None:
        # Build a runs file with a > 10 min job, plus a fixture log + ci.yml.
        runs_data: list[dict[str, object]] = [
            _make_run(
                "rX",
                "2026-05-05T19:00:00Z",
                [
                    _make_job(
                        "shard: shell",
                        start_offset_s=0,
                        duration_s=SINGLE_JOB_SECONDS_THRESHOLD + 100,
                        database_id=99,
                    )
                ],
            )
        ]
        runs_file = tmp_path / "runs.json"
        runs_file.write_text(json.dumps(runs_data))
        ciyml = tmp_path / "ci.yml"
        ciyml.write_text(CIYML_FIXTURE)
        log_file = tmp_path / "joblog.txt"
        log_file.write_text(_shell_log_fixture())

        rc = main(
            [
                "--from-file",
                str(runs_file),
                "--ciyml",
                str(ciyml),
                "--drill-down-log-file",
                str(log_file),
                "--json",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 1
        payload = json.loads(out)
        single_jobs = [f for f in payload["findings"] if f["kind"] == "single-job"]
        assert single_jobs, "expected at least one single-job finding"
        drill = single_jobs[0]["details"].get("drill_down")
        assert drill is not None
        assert "slowest_steps" in drill
        assert "if_split_wall_clock_seconds" in drill
        assert "split_savings_seconds" in drill

    def test_no_drill_down_flag_skips_drill_down(self, tmp_path: Path, capsys) -> None:
        runs_data: list[dict[str, object]] = [
            _make_run(
                "rY",
                "2026-05-05T19:00:00Z",
                [
                    _make_job(
                        "slow-job",
                        start_offset_s=0,
                        duration_s=SINGLE_JOB_SECONDS_THRESHOLD + 1,
                        database_id=10,
                    )
                ],
            )
        ]
        runs_file = tmp_path / "runs.json"
        runs_file.write_text(json.dumps(runs_data))
        rc = main(["--from-file", str(runs_file), "--no-drill-down", "--json"])
        out = capsys.readouterr().out
        assert rc == 1
        payload = json.loads(out)
        assert payload["findings"]
        # No drill_down attached when the flag is set.
        assert "drill_down" not in payload["findings"][0]["details"]
