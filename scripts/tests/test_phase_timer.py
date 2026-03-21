"""Tests for phase_timer module."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phase_timer import (
    _read_records,
    _timings_path,
    end_phase,
    generate_summary,
    start_phase,
)


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    """Create a temporary worktree directory with tmp/ subdir."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / "tmp").mkdir()
    return wt


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Create a temporary repo root directory with tmp/ subdir."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tmp").mkdir()
    return root


class TestStartPhase:
    """Tests for start_phase function."""

    def test_creates_timings_file(self, worktree: Path) -> None:
        start_phase(worktree, "setup")
        assert _timings_path(worktree).exists()

    def test_writes_valid_jsonl(self, worktree: Path) -> None:
        start_phase(worktree, "setup")
        records = _read_records(worktree)
        assert len(records) == 1
        assert records[0]["type"] == "start"
        assert records[0]["phase"] == "setup"
        assert "timestamp" in records[0]

    def test_auto_closes_open_phase(self, worktree: Path) -> None:
        start_phase(worktree, "setup")
        start_phase(worktree, "claiming")
        records = _read_records(worktree)
        # Should have: start(setup), end(setup), start(claiming)
        types = [r["type"] for r in records]
        assert types == ["start", "end", "start"]
        assert records[1]["phase"] == "setup"
        assert records[2]["phase"] == "claiming"


class TestEndPhase:
    """Tests for end_phase function."""

    def test_ends_open_phase(self, worktree: Path) -> None:
        start_phase(worktree, "setup")
        end_phase(worktree)
        records = _read_records(worktree)
        assert len(records) == 2
        end_record = records[1]
        assert end_record["type"] == "end"
        assert end_record["phase"] == "setup"
        assert "seconds" in end_record
        assert end_record["seconds"] >= 0

    def test_noop_when_no_open_phase(self, worktree: Path) -> None:
        end_phase(worktree)
        records = _read_records(worktree)
        assert len(records) == 0

    def test_includes_detail(self, worktree: Path) -> None:
        start_phase(worktree, "ralph-reviewer (1)")
        detail = {"gemini_standard": 90, "gemini_adversarial": 95, "claude": 120}
        end_phase(worktree, detail=detail)
        records = _read_records(worktree)
        end_record = records[1]
        assert end_record["detail"] == detail

    def test_computes_duration(self, worktree: Path) -> None:
        start_phase(worktree, "setup")
        # Sleep briefly to get a non-zero duration
        time.sleep(0.1)
        end_phase(worktree)
        records = _read_records(worktree)
        end_record = records[1]
        assert end_record["seconds"] >= 0


class TestGenerateSummary:
    """Tests for generate_summary function."""

    def test_generates_timing_json(self, worktree: Path, repo_root: Path) -> None:
        start_phase(worktree, "setup")
        end_phase(worktree)
        start_phase(worktree, "claiming")
        end_phase(worktree)

        summary = generate_summary(worktree, repo_root, 1207)

        assert summary["issue"] == 1207
        assert summary["total_seconds"] >= 0
        assert len(summary["phases"]) == 2
        assert summary["phases"][0]["phase"] == "setup"
        assert summary["phases"][1]["phase"] == "claiming"

    def test_writes_timing_json_file(self, worktree: Path, repo_root: Path) -> None:
        start_phase(worktree, "setup")
        end_phase(worktree)

        generate_summary(worktree, repo_root, 42)

        timing_path = worktree / "tmp" / "timing.json"
        assert timing_path.exists()
        data = json.loads(timing_path.read_text(encoding="utf-8"))
        assert data["issue"] == 42
        assert "phases" in data

    def test_appends_to_aggregate_jsonl(self, worktree: Path, repo_root: Path) -> None:
        start_phase(worktree, "setup")
        end_phase(worktree)

        generate_summary(worktree, repo_root, 42)

        aggregate_path = repo_root / "tmp" / "task-timings.jsonl"
        assert aggregate_path.exists()
        lines = aggregate_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["issue"] == 42

    def test_multiple_summaries_append(self, worktree: Path, repo_root: Path) -> None:
        # First task
        start_phase(worktree, "setup")
        end_phase(worktree)
        generate_summary(worktree, repo_root, 42)

        # Clear the worktree timings for a "second task"
        _timings_path(worktree).unlink()

        start_phase(worktree, "setup")
        end_phase(worktree)
        generate_summary(worktree, repo_root, 43)

        aggregate_path = repo_root / "tmp" / "task-timings.jsonl"
        lines = aggregate_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["issue"] == 42
        assert json.loads(lines[1])["issue"] == 43

    def test_auto_closes_open_phase_on_summarize(
        self, worktree: Path, repo_root: Path
    ) -> None:
        start_phase(worktree, "verifying")
        # Don't explicitly end — summarize should auto-close it

        summary = generate_summary(worktree, repo_root, 100)
        assert len(summary["phases"]) == 1
        assert summary["phases"][0]["phase"] == "verifying"

    def test_includes_detail_in_summary(self, worktree: Path, repo_root: Path) -> None:
        start_phase(worktree, "ralph-reviewer (1)")
        detail = {"gemini_standard": 90, "gemini_adversarial": 95, "claude": 120}
        end_phase(worktree, detail=detail)

        summary = generate_summary(worktree, repo_root, 1207)
        assert summary["phases"][0]["detail"] == detail

    def test_completed_at_timestamp(self, worktree: Path, repo_root: Path) -> None:
        start_phase(worktree, "setup")
        end_phase(worktree)

        summary = generate_summary(worktree, repo_root, 42)
        assert "completed_at" in summary


class TestReadRecords:
    """Tests for _read_records function."""

    def test_empty_when_no_file(self, worktree: Path) -> None:
        records = _read_records(worktree)
        assert records == []

    def test_skips_invalid_json(self, worktree: Path) -> None:
        path = _timings_path(worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"start","phase":"x","timestamp":"t"}\nBAD JSON\n')
        records = _read_records(worktree)
        assert len(records) == 1


class TestMultiplePhases:
    """Integration tests for realistic multi-phase workflows."""

    def test_full_task_workflow(self, worktree: Path, repo_root: Path) -> None:
        """Simulate a complete /task workflow with all phases."""
        start_phase(worktree, "claiming")
        end_phase(worktree)

        start_phase(worktree, "setup")
        end_phase(worktree)

        start_phase(worktree, "ralph-worker (1)")
        end_phase(worktree)

        start_phase(worktree, "ralph-reviewer (1)")
        detail = {"gemini_standard": 90, "gemini_adversarial": 95, "claude": 120}
        end_phase(worktree, detail=detail)

        start_phase(worktree, "pushing")
        end_phase(worktree)

        start_phase(worktree, "ci-watch (1)")
        end_phase(worktree)

        start_phase(worktree, "merging")
        end_phase(worktree)

        start_phase(worktree, "deploying")
        end_phase(worktree)

        start_phase(worktree, "verifying")
        end_phase(worktree)

        start_phase(worktree, "retrospective")
        end_phase(worktree)

        summary = generate_summary(worktree, repo_root, 1207)

        assert summary["issue"] == 1207
        assert len(summary["phases"]) == 10
        phase_names = [p["phase"] for p in summary["phases"]]
        assert phase_names == [
            "claiming",
            "setup",
            "ralph-worker (1)",
            "ralph-reviewer (1)",
            "pushing",
            "ci-watch (1)",
            "merging",
            "deploying",
            "verifying",
            "retrospective",
        ]
        # Only ralph-reviewer has detail
        for phase in summary["phases"]:
            if phase["phase"] == "ralph-reviewer (1)":
                assert "detail" in phase
            else:
                assert "detail" not in phase

    def test_ci_fix_retry_workflow(self, worktree: Path, repo_root: Path) -> None:
        """Simulate a workflow with CI failure and retry."""
        start_phase(worktree, "pushing")
        end_phase(worktree)

        start_phase(worktree, "ci-watch (1)")
        end_phase(worktree)

        start_phase(worktree, "ci-fix")
        end_phase(worktree)

        start_phase(worktree, "ci-watch (2)")
        end_phase(worktree)

        summary = generate_summary(worktree, repo_root, 42)
        assert len(summary["phases"]) == 4
