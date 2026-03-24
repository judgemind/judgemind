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
    detect_persistent_dissent,
    log_dissent_override,
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

    def test_detects_adversarial_only_catches(self, state_dir: Path) -> None:
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
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
        assert record["adversarial_only_catches"] == [1]
        assert "gemini_only_catches" not in record
        assert "claude_only_catches" not in record

    def test_adversarial_catch_not_counted_as_gemini(self, state_dir: Path) -> None:
        """Adversarial-only REVISE should not be attributed to gemini or claude."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 1, "model": "gemini-adversarial", "verdict": "REVISE"},
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-adversarial", "verdict": "SHIP"},
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
        assert record["adversarial_only_catches"] == [1]
        assert record["agreement_count"] == 1
        assert record["disagreement_count"] == 1

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


class TestDetectPersistentDissent:
    """Tests for detect_persistent_dissent function."""

    def test_no_dissent_when_all_agree(self, state_dir: Path) -> None:
        """All reviewers agree to SHIP — no dissent detected."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 1, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is None

    def test_detects_adversarial_solo_dissent(self, state_dir: Path) -> None:
        """Adversarial says REVISE for 2 consecutive iterations, others SHIP."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 2,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is not None
        assert result["dissenter"] == "adversarial"
        assert result["consecutive_count"] == 2
        assert result["iterations"] == [1, 2]

    def test_detects_gemini_solo_dissent(self, state_dir: Path) -> None:
        """Gemini standard says REVISE for 2 iterations, others SHIP."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "REVISE"},
            {"iteration": 1, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "REVISE"},
            {"iteration": 2, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is not None
        assert result["dissenter"] == "gemini"
        assert result["consecutive_count"] == 2

    def test_detects_claude_solo_dissent(self, state_dir: Path) -> None:
        """Claude says REVISE for 2 iterations, others SHIP."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 1, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 1, "model": "claude", "verdict": "REVISE"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 2, "model": "claude", "verdict": "REVISE"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is not None
        assert result["dissenter"] == "claude"
        assert result["consecutive_count"] == 2

    def test_no_dissent_when_two_reviewers_revise(self, state_dir: Path) -> None:
        """Two reviewers say REVISE — this is a genuine concern, not solo dissent."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "REVISE"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "REVISE"},
            {
                "iteration": 2,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is None

    def test_no_dissent_with_only_one_iteration(self, state_dir: Path) -> None:
        """Single iteration of solo dissent is not enough to trigger override."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=1, reviews=reviews
        )
        assert result is None

    def test_dissent_broken_by_different_dissenter(self, state_dir: Path) -> None:
        """Different dissenter each iteration — no consecutive pattern."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 2, "model": "claude", "verdict": "REVISE"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is None

    def test_dissent_with_three_consecutive_iterations(self, state_dir: Path) -> None:
        """Three consecutive iterations of solo dissent."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 2,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
            {"iteration": 3, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 3,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 3, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=3, reviews=reviews
        )
        assert result is not None
        assert result["dissenter"] == "adversarial"
        assert result["consecutive_count"] == 3
        assert result["iterations"] == [1, 2, 3]

    def test_reads_from_log_file(self, state_dir: Path) -> None:
        """When reviews not passed, reads from the log file."""
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="OK",
        )
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro-adversarial",
            verdict="REVISE",
            feedback="Nope",
        )
        log_review(
            state_dir, iteration=1, model="claude", verdict="SHIP", feedback="OK"
        )
        log_review(
            state_dir,
            iteration=2,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="OK",
        )
        log_review(
            state_dir,
            iteration=2,
            model="gemini-2.5-pro-adversarial",
            verdict="REVISE",
            feedback="Still nope",
        )
        log_review(
            state_dir, iteration=2, model="claude", verdict="SHIP", feedback="OK"
        )

        result = detect_persistent_dissent(state_dir, current_iteration=2)
        assert result is not None
        assert result["dissenter"] == "adversarial"

    def test_dissent_with_skipped_reviewer(self, state_dir: Path) -> None:
        """Solo dissent still detected when one reviewer is SKIPPED."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SKIPPED"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SKIPPED"},
            {
                "iteration": 2,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is not None
        assert result["dissenter"] == "adversarial"
        assert result["consecutive_count"] == 2

    def test_no_dissent_with_all_skipped_except_one(self, state_dir: Path) -> None:
        """Need at least 2 active reviewers to detect dissent."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SKIPPED"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "SKIPPED",
            },
            {"iteration": 1, "model": "claude", "verdict": "REVISE"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SKIPPED"},
            {
                "iteration": 2,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "SKIPPED",
            },
            {"iteration": 2, "model": "claude", "verdict": "REVISE"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is None

    def test_dissent_uses_gemini_adversarial_model_name(self, state_dir: Path) -> None:
        """Works with the alternate 'gemini-adversarial' model name."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 1, "model": "gemini-adversarial", "verdict": "REVISE"},
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-adversarial", "verdict": "REVISE"},
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result is not None
        assert result["dissenter"] == "adversarial"

    def test_custom_min_consecutive(self, state_dir: Path) -> None:
        """Custom min_consecutive=3 requires 3 consecutive iterations."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 2,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
        ]
        # Default min_consecutive=2 would detect it
        result_default = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews
        )
        assert result_default is not None

        # min_consecutive=3 should not detect it
        result_strict = detect_persistent_dissent(
            state_dir, current_iteration=2, reviews=reviews, min_consecutive=3
        )
        assert result_strict is None

    def test_dissent_not_detected_when_streak_breaks_mid_history(
        self, state_dir: Path
    ) -> None:
        """Streak broken in the middle: iter 1 adversarial REVISE, iter 2 all SHIP,
        iter 3 adversarial REVISE — no 2-consecutive pattern from iter 3."""
        reviews = [
            {"iteration": 1, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 1,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 1, "model": "claude", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {"iteration": 2, "model": "gemini-2.5-pro-adversarial", "verdict": "SHIP"},
            {"iteration": 2, "model": "claude", "verdict": "SHIP"},
            {"iteration": 3, "model": "gemini-2.5-pro", "verdict": "SHIP"},
            {
                "iteration": 3,
                "model": "gemini-2.5-pro-adversarial",
                "verdict": "REVISE",
            },
            {"iteration": 3, "model": "claude", "verdict": "SHIP"},
        ]
        result = detect_persistent_dissent(
            state_dir, current_iteration=3, reviews=reviews
        )
        assert result is None


class TestLogDissentOverride:
    """Tests for log_dissent_override function."""

    def test_writes_override_record(self, state_dir: Path) -> None:
        log_dissent_override(
            state_dir,
            iteration=3,
            dissenter="adversarial",
            consecutive_count=2,
            dissent_iterations=[2, 3],
        )
        log_path = state_dir / "review-log.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["type"] == "dissent_override"
        assert record["iteration"] == 3
        assert record["dissenter"] == "adversarial"
        assert record["consecutive_count"] == 2
        assert record["dissent_iterations"] == [2, 3]
        assert "timestamp" in record

    def test_override_coexists_with_reviews(self, state_dir: Path) -> None:
        """Override record is appended after review records."""
        log_review(
            state_dir,
            iteration=1,
            model="gemini-2.5-pro",
            verdict="SHIP",
            feedback="OK",
        )
        log_review(
            state_dir,
            iteration=1,
            model="claude",
            verdict="REVISE",
            feedback="Nope",
        )
        log_dissent_override(
            state_dir,
            iteration=2,
            dissenter="claude",
            consecutive_count=2,
            dissent_iterations=[1, 2],
        )
        log_path = state_dir / "review-log.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["type"] == "review"
        assert json.loads(lines[1])["type"] == "review"
        assert json.loads(lines[2])["type"] == "dissent_override"
