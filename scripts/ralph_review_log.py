"""Structured logging for the /ralph dual-review loop.

Appends JSONL records to {worktree}/tmp/ralph/review-log.jsonl so review
verdicts, feedback, agreement patterns, and token usage can be analyzed.

This module is intentionally dependency-free (stdlib only) to avoid requiring
a venv for the scripts/ directory.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _log_path(state_dir: str | Path) -> Path:
    """Return the path to the review log JSONL file."""
    return Path(state_dir) / "review-log.jsonl"


def _append_record(state_dir: str | Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to the review log."""
    path = _log_path(state_dir)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def log_review(
    state_dir: str | Path,
    *,
    iteration: int,
    model: str,
    verdict: str,
    feedback: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    diff_stats: dict[str, int] | None = None,
) -> None:
    """Log a single review record (Gemini or Claude).

    Args:
        state_dir: Path to {worktree}/tmp/ralph/.
        iteration: Current ralph loop iteration (1-based).
        model: Model identifier (e.g. "gemini-2.5-pro", "claude").
        verdict: Review verdict — "SHIP", "REVISE", or "SKIPPED".
        feedback: Full review feedback text.
        input_tokens: Input token count (if available from API response).
        output_tokens: Output token count (if available from API response).
        latency_ms: API call latency in milliseconds.
        diff_stats: Dict with keys "files_changed", "insertions", "deletions".
    """
    record: dict[str, Any] = {
        "type": "review",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "iteration": iteration,
        "model": model,
        "verdict": verdict,
        "feedback": feedback,
    }

    if input_tokens is not None:
        record["input_tokens"] = input_tokens
    if output_tokens is not None:
        record["output_tokens"] = output_tokens
    if latency_ms is not None:
        record["latency_ms"] = latency_ms
    if diff_stats is not None:
        record["diff_stats"] = diff_stats

    _append_record(state_dir, record)


def log_summary(
    state_dir: str | Path,
    *,
    total_iterations: int,
    final_verdict: str,
    reviews: list[dict[str, Any]] | None = None,
) -> None:
    """Log a summary record at the end of the ralph loop.

    Computes agreement rate and catch patterns from the review log
    if individual reviews are provided.

    Args:
        state_dir: Path to {worktree}/tmp/ralph/.
        total_iterations: Total number of iterations completed.
        final_verdict: Final loop outcome — "SHIP" or "MAX_ITERATIONS".
        reviews: Optional list of review records to compute agreement stats.
            If not provided, reads them from the log file.
    """
    if reviews is None:
        reviews = read_reviews(state_dir)

    # Compute agreement stats
    agreement_count = 0
    disagreement_count = 0
    gemini_only_catches: list[int] = []
    claude_only_catches: list[int] = []

    # Group reviews by iteration
    by_iteration: dict[int, dict[str, str]] = {}
    for review in reviews:
        it = review.get("iteration", 0)
        model = review.get("model", "")
        verdict = review.get("verdict", "")
        if it not in by_iteration:
            by_iteration[it] = {}
        by_iteration[it][model] = verdict

    for it, verdicts in sorted(by_iteration.items()):
        gemini_v = verdicts.get("gemini-2.5-pro", verdicts.get("gemini", "SKIPPED"))
        adversarial_v = verdicts.get(
            "gemini-2.5-pro-adversarial",
            verdicts.get("gemini-adversarial", "SKIPPED"),
        )
        # Find claude verdict (any key starting with "claude")
        claude_v = "SKIPPED"
        for k, v in verdicts.items():
            if k.startswith("claude") or k == "claude":
                claude_v = v
                break

        # Compare each non-SKIPPED reviewer pair for agreement stats
        active_verdicts: list[tuple[str, str]] = []
        if gemini_v != "SKIPPED":
            active_verdicts.append(("gemini", gemini_v))
        if adversarial_v != "SKIPPED":
            active_verdicts.append(("adversarial", adversarial_v))
        if claude_v != "SKIPPED":
            active_verdicts.append(("claude", claude_v))

        # Skip iterations with fewer than 2 active reviewers
        if len(active_verdicts) < 2:
            continue

        # All active reviewers agree?
        all_verdicts = [v for _, v in active_verdicts]
        if len(set(all_verdicts)) == 1:
            agreement_count += 1
        else:
            disagreement_count += 1
            # Track which reviewer(s) uniquely caught issues
            revivers = {name for name, v in active_verdicts if v == "REVISE"}
            shippers = {name for name, v in active_verdicts if v == "SHIP"}
            if revivers == {"gemini"}:
                gemini_only_catches.append(it)
            elif revivers == {"claude"}:
                claude_only_catches.append(it)

    total_compared = agreement_count + disagreement_count
    agreement_rate = (
        round(agreement_count / total_compared, 2) if total_compared > 0 else None
    )

    record: dict[str, Any] = {
        "type": "summary",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_iterations": total_iterations,
        "final_verdict": final_verdict,
        "agreement_rate": agreement_rate,
        "agreement_count": agreement_count,
        "disagreement_count": disagreement_count,
    }

    if gemini_only_catches:
        record["gemini_only_catches"] = gemini_only_catches
    if claude_only_catches:
        record["claude_only_catches"] = claude_only_catches

    _append_record(state_dir, record)


def read_reviews(state_dir: str | Path) -> list[dict[str, Any]]:
    """Read all review records from the log file.

    Returns only records with type="review", not summary records.
    """
    path = _log_path(state_dir)
    if not path.exists():
        return []

    reviews = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if record.get("type") == "review":
                reviews.append(record)
        except json.JSONDecodeError:
            continue

    return reviews


def compute_diff_stats(worktree_path: str | Path) -> dict[str, int]:
    """Compute diff stats (files changed, insertions, deletions) from a worktree.

    Uses git diff --stat to get the numbers. Returns a dict with keys
    "files_changed", "insertions", "deletions".

    This is a best-effort helper — returns zeros if git is unavailable.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--shortstat"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        cached_result = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--cached", "--shortstat"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"files_changed": 0, "insertions": 0, "deletions": 0}

    files_changed = 0
    insertions = 0
    deletions = 0

    for output in [result.stdout.strip(), cached_result.stdout.strip()]:
        if not output:
            continue
        # Parse "N files changed, N insertions(+), N deletions(-)"
        for part in output.split(","):
            part = part.strip()
            if "file" in part and "changed" in part:
                files_changed += int(part.split()[0])
            elif "insertion" in part:
                insertions += int(part.split()[0])
            elif "deletion" in part:
                deletions += int(part.split()[0])

    return {
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
    }


class ReviewTimer:
    """Context manager for timing review API calls.

    Usage:
        timer = ReviewTimer()
        with timer:
            response = client.models.generate_content(...)
        print(timer.latency_ms)  # e.g. 4523
    """

    def __init__(self) -> None:
        self.latency_ms: int = 0
        self._start: float = 0.0

    def __enter__(self) -> "ReviewTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed = time.monotonic() - self._start
        self.latency_ms = int(elapsed * 1000)
