"""Tests for ralph_review_log module."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralph_review_log import (
    ReviewTimer,
    compute_diff_stats,
    log_review,
    log_summary,
    read_reviews,
)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """Create a temporary ralph state directory."""
    state = tmp_path / "ralph"
    state.mkdir()
    return state


class TestLogReview:
    """Tests for log_review function."""

    def test_creates_log_file(self, state_dir: Path) -> None:
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="Looks good.",
        )
        log_path = state_dir / "review-log.jsonl"
        assert log_path.exists()

    def test_writes_valid_jsonl(self, state_dir: Path) -> None:
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="Looks good.",
        )
        log_path = state_dir / "review-log.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "review"
        assert record["iteration"] == 1
        assert record["model"] == "gemini-2.5-pro"
        assert record["verdict"] == "SHIP"
        assert record["feedback"] == "Looks good."
        assert "timestamp" in record

    def test_appends_multiple_records(self, state_dir: Path) -> None:
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="Looks good.",
        )
        log_review(
            state_dir,
            iteration=1,
            model="claude",
            verdict="REVISE",
            feedback="Needs more tests.",
        )
        log_path = state_dir / "review-log.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        r1 = json.loads(lines[0])
        r2 = json.loads(lines[1])
        assert r1["model"] == "gemini-2.5-pro"
        assert r2["model"] == "claude"

    def test_includes_optional_fields(self, state_dir: Path) -> None:
        diff_stats = {"files_changed": 3, "insertions": 50, "deletions": 10}
        log_review(
            state_dir,
            iteration=2,
            model="gemini-2.5-pro",
            verdict="REVISE",
            feedback="Fix the edge case.",
            input_tokens=1500,
            output_tokens=800,
            latency_ms=4523,
            diff_stats=diff_stats,
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["input_tokens"] == 1500
        assert record["output_tokens"] == 800
        assert record["latency_ms"] == 4523
        assert record["diff_stats"] == diff_stats

    def test_omits_none_optional_fields(self, state_dir: Path) -> None:
        log_review(
            state_dir,
            iteration=1,
            model="claude",
            verdict="SHIP",
            feedback="LGTM.",
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert "input_tokens" not in record
        assert "output_tokens" not in record
        assert "latency_ms" not in record
        assert "diff_stats" not in record


class TestReadReviews:
    """Tests for read_reviews function."""

    def test_returns_empty_for_missing_file(self, state_dir: Path) -> None:
        assert read_reviews(state_dir) == []

    def test_returns_only_review_records(self, state_dir: Path) -> None:
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="Good.",
        )
        log_summary(
            state_dir,
            total_iterations=1,
            final_verdict="SHIP",
        )
        reviews = read_reviews(state_dir)
        assert len(reviews) == 1
        assert reviews[0]["type"] == "review"

    def test_skips_malformed_lines(self, state_dir: Path) -> None:
        log_path = state_dir / "review-log.jsonl"
        log_path.write_text("not json\n", encoding="utf-8")
        log_review(
            state_dir,
            iteration=1,
            model="claude",
            verdict="SHIP",
            feedback="OK.",
        )
        reviews = read_reviews(state_dir)
        assert len(reviews) == 1


class TestLogSummary:
    """Tests for log_summary function."""

    def test_writes_summary_record(self, state_dir: Path) -> None:
        log_summary(
            state_dir,
            total_iterations=2,
            final_verdict="SHIP",
            reviews=[],
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["type"] == "summary"
        assert record["total_iterations"] == 2
        assert record["final_verdict"] == "SHIP"

    def test_computes_agreement_rate(self, state_dir: Path) -> None:
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "REVISE"},
            {"iteration": 1, "model": "claude", "verdict": "REVISE"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        log_summary(
            state_dir,
            total_iterations=2,
            final_verdict="SHIP",
            reviews=reviews,
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["agreement_rate"] == 1.0
        assert record["agreement_count"] == 2
        assert record["disagreement_count"] == 0

    def test_detects_disagreement(self, state_dir: Path) -> None:
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "REVISE"},
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        log_summary(
            state_dir,
            total_iterations=2,
            final_verdict="SHIP",
            reviews=reviews,
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["agreement_rate"] == 0.5
        assert record["agreement_count"] == 1
        assert record["disagreement_count"] == 1
        assert record["gemini_only_catches"] == [1]

    def test_detects_claude_only_catches(self, state_dir: Path) -> None:
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 1, "model": "claude", "verdict": "REVISE"},
        ]
        log_summary(
            state_dir,
            total_iterations=1,
            final_verdict="SHIP",
            reviews=reviews,
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["claude_only_catches"] == [1]

    def test_skips_skipped_gemini(self, state_dir: Path) -> None:
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SKIPPED"},
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
        ]
        log_summary(
            state_dir,
            total_iterations=1,
            final_verdict="SHIP",
            reviews=reviews,
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["agreement_rate"] is None
        assert record["agreement_count"] == 0
        assert record["disagreement_count"] == 0

    def test_reads_from_log_file(self, state_dir: Path) -> None:
        """When no reviews argument provided, reads from the log file."""
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="Good.",
        )
        log_review(
            state_dir,
            iteration=1,
            model="claude",
            verdict="SHIP",
            feedback="LGTM.",
        )
        log_summary(
            state_dir,
            total_iterations=1,
            final_verdict="SHIP",
        )
        log_path = state_dir / "review-log.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        summary = json.loads(lines[-1])
        assert summary["type"] == "summary"
        assert summary["agreement_rate"] == 1.0

    def test_max_iterations_verdict(self, state_dir: Path) -> None:
        log_summary(
            state_dir,
            total_iterations=5,
            final_verdict="MAX_ITERATIONS",
            reviews=[],
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["final_verdict"] == "MAX_ITERATIONS"


class TestReviewTimer:
    """Tests for ReviewTimer context manager."""

    def test_measures_latency(self) -> None:
        import time

        timer = ReviewTimer()
        with timer:
            time.sleep(0.05)  # 50ms
        assert timer.latency_ms >= 40  # Allow some tolerance
        assert timer.latency_ms < 200

    def test_zero_before_use(self) -> None:
        timer = ReviewTimer()
        assert timer.latency_ms == 0


class TestComputeDiffStats:
    """Tests for compute_diff_stats function."""

    def test_returns_zeros_for_nonexistent_path(self, tmp_path: Path) -> None:
        result = compute_diff_stats(tmp_path / "nonexistent")
        assert result == {"files_changed": 0, "insertions": 0, "deletions": 0}

    def test_returns_dict_structure(self, tmp_path: Path) -> None:
        result = compute_diff_stats(tmp_path)
        assert "files_changed" in result
        assert "insertions" in result
        assert "deletions" in result
