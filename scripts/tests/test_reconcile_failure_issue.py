"""Tests for scripts/reconcile_failure_issue.py (#5095)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_failure_issue as rfi

RUN_URL = "https://github.com/judgemind/judgemind/actions/runs/123"
STEPS = {
    "busy": {"outcome": "success", "conclusion": "success", "outputs": {}},
    "run": {"outcome": "success", "conclusion": "success", "outputs": {}},
    "result": {"outcome": "failure", "conclusion": "failure", "outputs": {}},
}


def test_failed_steps_lists_failures_in_order() -> None:
    steps = {**STEPS, "file_issue": {"outcome": "failure"}}
    assert rfi.failed_steps(steps) == ["result", "file_issue"]


@pytest.mark.parametrize("steps", [None, [], "x", {"a": "not-a-dict"}])
def test_failed_steps_tolerates_bad_input(steps: object) -> None:
    assert rfi.failed_steps(steps) == []


def test_log_tail_keeps_last_lines_and_splits_multiline() -> None:
    events = [f"line {i}" for i in range(50)] + ["Traceback\n  boom\n", ""]
    tail = rfi.log_tail(events, limit=5)
    assert tail == ["line 47", "line 48", "line 49", "Traceback", "  boom"]


def test_log_tail_truncates_long_lines() -> None:
    tail = rfi.log_tail(["x" * 2000])
    assert len(tail[0]) == rfi.MAX_LINE_CHARS + 2
    assert tail[0].endswith(" …")


@pytest.mark.parametrize("events", [None, {}, "text"])
def test_log_tail_tolerates_bad_input(events: object) -> None:
    assert rfi.log_tail(events) == []


def test_details_with_task_and_tail() -> None:
    block = rfi.details(
        run_url=RUN_URL,
        event="workflow_dispatch",
        task_id="abc123",
        exit_code="3",
        steps=STEPS,
        events=["starting", "SIMULATED FAILURE ```"],
    )
    assert RUN_URL in block
    assert "`workflow_dispatch`" in block
    assert "Failed step(s): `result`" in block
    assert "ECS task: `abc123` (exit `3`" in block
    assert "oneshot/oneshot/abc123" in block
    assert "**Last 2 lines of the task log:**" in block
    # A ``` inside the tail must not close the code fence early.
    assert block.count("```") == 2
    assert "SIMULATED FAILURE '''" in block


def test_details_without_task() -> None:
    block = rfi.details(
        run_url=RUN_URL,
        event="schedule",
        task_id="",
        exit_code="",
        steps=None,
        events=None,
    )
    assert "failed before the reconciler task launched" in block
    assert "Failed step(s): unknown (see the run log)" in block
    assert "No task log" in block


def test_details_task_with_empty_log() -> None:
    block = rfi.details(
        run_url=RUN_URL,
        event="schedule",
        task_id="t1",
        exit_code="",
        steps={},
        events=[],
    )
    assert "exit `unknown`" in block
    assert "empty or could not be read" in block


def test_issue_body_has_cta_and_autoclose_note() -> None:
    body = rfi.issue_body("DETAILS\n")
    assert body.startswith("## The raw-key reconciler failed to run")
    assert "the reconciler is blind until this is fixed" in body
    assert "DETAILS" in body
    assert "closes this issue automatically" in body
    assert "36h" in body


def test_comment_body() -> None:
    assert rfi.comment_body("D\n", "2026-11-16T13:34Z") == (
        "### Failed again: 2026-11-16T13:34Z\n\nD\n"
    )


def test_main_issue_mode_reads_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps(STEPS))
    events = tmp_path / "events.json"
    events.write_text(json.dumps(["boom"]))
    rc = rfi.main(
        [
            "--mode",
            "issue",
            "--run-url",
            RUN_URL,
            "--event",
            "schedule",
            "--task-id",
            "t9",
            "--exit-code",
            "1",
            "--steps-json",
            str(steps),
            "--log-events-json",
            str(events),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("## The raw-key reconciler failed to run")
    assert "`result`" in out
    assert "boom" in out


def test_main_comment_mode_missing_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rc = rfi.main(
        [
            "--mode",
            "comment",
            "--run-url",
            RUN_URL,
            "--steps-json",
            str(tmp_path / "missing.json"),
            "--log-events-json",
            str(bad),
            "--when",
            "2026-11-16T13:34Z",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("### Failed again: 2026-11-16T13:34Z")
    assert "No task log" in out


def test_main_comment_mode_default_when(capsys: pytest.CaptureFixture[str]) -> None:
    rfi.main(["--mode", "comment", "--run-url", RUN_URL])
    assert "Failed again: unknown time" in capsys.readouterr().out
