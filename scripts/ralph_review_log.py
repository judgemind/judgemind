"""Structured logging for the /ralph dual-review loop.

Appends JSONL records to {worktree}/tmp/ralph/review-log.jsonl so review
verdicts, feedback, agreement patterns, and token usage can be analyzed.

This module is intentionally dependency-free (stdlib only) to avoid requiring
a venv for the scripts/ directory.
"""
# permanent: true

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
    agent_id: str | None = None,
    issue_number: int | None = None,
) -> None:
    """Log a summary record at the end of the ralph loop.

    Computes agreement rate and catch patterns from the review log
    if individual reviews are provided.

    After writing the summary record, if both ``agent_id`` and
    ``issue_number`` are provided, mirrors the complete review-log.jsonl
    to S3 under ``ralph-reviews/<YYYY-MM-DD>/<agent_id>-<issue_number>.jsonl``.
    The upload is best-effort: S3 failures are logged as warnings and never
    abort the function.  ``telemetry_upload`` is lazy-imported so this module
    remains stdlib-only when the upload path is not exercised.

    Args:
        state_dir: Path to {worktree}/tmp/ralph/.
        total_iterations: Total number of iterations completed.
        final_verdict: Final loop outcome — "SHIP" or "MAX_ITERATIONS".
        reviews: Optional list of review records to compute agreement stats.
            If not provided, reads them from the log file.
        agent_id: Agent identifier (e.g. ``agent-ab4722a2``).  When provided
            together with ``issue_number``, triggers the S3 upload.
        issue_number: GitHub issue number the ralph run addressed.  When
            provided together with ``agent_id``, triggers the S3 upload.
    """
    if reviews is None:
        reviews = read_reviews(state_dir)

    # Compute agreement stats
    agreement_count = 0
    disagreement_count = 0
    gemini_only_catches: list[int] = []
    claude_only_catches: list[int] = []
    adversarial_only_catches: list[int] = []

    by_iteration = _group_reviews_by_iteration(reviews)

    for it, raw_verdicts in sorted(by_iteration.items()):
        normalized = _normalize_reviewer_verdicts(raw_verdicts)

        # Skip iterations with fewer than 2 active reviewers
        if len(normalized) < 2:
            continue

        # All active reviewers agree?
        all_verdicts = list(normalized.values())
        if len(set(all_verdicts)) == 1:
            agreement_count += 1
        else:
            disagreement_count += 1
            # Track which reviewer(s) uniquely caught issues
            revisers = {name for name, v in normalized.items() if v == "REVISE"}
            if revisers == {"gemini"}:
                gemini_only_catches.append(it)
            elif revisers == {"claude"}:
                claude_only_catches.append(it)
            elif revisers == {"adversarial"}:
                adversarial_only_catches.append(it)

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
    if adversarial_only_catches:
        record["adversarial_only_catches"] = adversarial_only_catches

    _append_record(state_dir, record)

    # Mirror the complete review-log.jsonl to S3 as a telemetry artifact.
    # Best-effort: S3 failure never aborts the function.
    # Lazy-import telemetry_upload so this module stays stdlib-only when
    # the upload path is not exercised (e.g. detect_persistent_dissent reads).
    if agent_id is not None and issue_number is not None:
        try:
            import telemetry_upload as _tu  # noqa: PLC0415

            log_file = _log_path(state_dir)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            s3_key = f"ralph-reviews/{today}/{agent_id}-{issue_number}.jsonl"
            _tu.mirror_to_s3(Path(log_file), s3_key)
        except Exception:  # noqa: BLE001
            # Swallow any unexpected error so telemetry never blocks the loop.
            pass


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


def _normalize_reviewer_verdicts(
    verdicts: dict[str, str],
) -> dict[str, str]:
    """Map raw model names to canonical reviewer names with their verdicts.

    Returns a dict with keys from {"gemini", "adversarial", "claude"} mapped
    to their verdict strings.  Only includes reviewers that are present and
    not SKIPPED.
    """
    gemini_v = verdicts.get("gemini-2.5-pro", verdicts.get("gemini", "SKIPPED"))
    adversarial_v = verdicts.get(
        "gemini-2.5-pro-adversarial",
        verdicts.get("gemini-adversarial", "SKIPPED"),
    )
    claude_v = "SKIPPED"
    for k, v in verdicts.items():
        if k.startswith("claude") or k == "claude":
            claude_v = v
            break

    result: dict[str, str] = {}
    if gemini_v != "SKIPPED":
        result["gemini"] = gemini_v
    if adversarial_v != "SKIPPED":
        result["adversarial"] = adversarial_v
    if claude_v != "SKIPPED":
        result["claude"] = claude_v
    return result


def _group_reviews_by_iteration(
    reviews: list[dict[str, Any]],
) -> dict[int, dict[str, str]]:
    """Group review records by iteration, mapping model -> verdict."""
    by_iteration: dict[int, dict[str, str]] = {}
    for review in reviews:
        it = review.get("iteration", 0)
        model = review.get("model", "")
        verdict = review.get("verdict", "")
        if it not in by_iteration:
            by_iteration[it] = {}
        by_iteration[it][model] = verdict
    return by_iteration


def detect_persistent_dissent(
    state_dir: str | Path,
    *,
    current_iteration: int,
    reviews: list[dict[str, Any]] | None = None,
    min_consecutive: int = 2,
) -> dict[str, Any] | None:
    """Detect if exactly one reviewer has been solo-dissenting for N+ iterations.

    Checks the review history for this pattern: one reviewer says REVISE for
    ``min_consecutive`` or more consecutive recent iterations while the other
    active reviewers all say SHIP.  Only triggers when exactly one reviewer
    dissents — if two reviewers say REVISE, that is a genuine concern.

    Args:
        state_dir: Path to {worktree}/tmp/ralph/.
        current_iteration: The iteration that just completed (1-based).
        reviews: Optional pre-loaded review records.  If not provided, reads
            from the log file.
        min_consecutive: Minimum consecutive solo-REVISE iterations required
            to trigger the override.  Default is 2.

    Returns:
        A dict describing the dissent if detected, with keys:
            - ``dissenter``: canonical reviewer name ("gemini", "adversarial",
              or "claude")
            - ``consecutive_count``: number of consecutive solo-REVISE
              iterations
            - ``iterations``: list of iteration numbers where the dissent
              occurred
        Returns ``None`` if no persistent solo-dissent is detected.
    """
    if reviews is None:
        reviews = read_reviews(state_dir)

    by_iteration = _group_reviews_by_iteration(reviews)

    # Walk backwards from current_iteration to find consecutive solo-dissent
    # We need at least min_consecutive iterations of data
    if current_iteration < min_consecutive:
        return None

    # For each iteration from current back, check if exactly one reviewer
    # said REVISE while all others said SHIP
    consecutive_dissenter: str | None = None
    consecutive_count = 0
    dissent_iterations: list[int] = []

    for it in range(current_iteration, 0, -1):
        if it not in by_iteration:
            break

        raw_verdicts = by_iteration[it]
        normalized = _normalize_reviewer_verdicts(raw_verdicts)

        # Need at least 2 active reviewers to detect dissent
        if len(normalized) < 2:
            break

        # Find who said REVISE
        revisers = {name for name, v in normalized.items() if v == "REVISE"}
        shippers = {name for name, v in normalized.items() if v == "SHIP"}

        # Must be exactly 1 dissenter with everyone else saying SHIP
        if len(revisers) != 1 or len(shippers) < 1:
            break

        dissenter = next(iter(revisers))

        # All non-REVISE active reviewers must be SHIP (not some other verdict)
        non_revisers = {name for name, v in normalized.items() if name != dissenter}
        if not all(normalized[name] == "SHIP" for name in non_revisers):
            break

        if consecutive_dissenter is None:
            consecutive_dissenter = dissenter
        elif consecutive_dissenter != dissenter:
            # Different dissenter than previous iteration — streak broken
            break

        consecutive_count += 1
        dissent_iterations.append(it)

    if consecutive_count >= min_consecutive and consecutive_dissenter is not None:
        return {
            "dissenter": consecutive_dissenter,
            "consecutive_count": consecutive_count,
            "iterations": sorted(dissent_iterations),
        }

    return None


def log_dissent_override(
    state_dir: str | Path,
    *,
    iteration: int,
    dissenter: str,
    consecutive_count: int,
    dissent_iterations: list[int],
) -> None:
    """Log a persistent-dissent override to the review log.

    Records that the decision logic overrode a solo-dissenter's REVISE verdict
    because they had been the only reviewer saying REVISE for multiple
    consecutive iterations while all others said SHIP.

    Args:
        state_dir: Path to {worktree}/tmp/ralph/.
        iteration: The current iteration number where the override was applied.
        dissenter: Canonical reviewer name that was overridden.
        consecutive_count: Number of consecutive solo-REVISE iterations.
        dissent_iterations: List of iteration numbers where dissent occurred.
    """
    record: dict[str, Any] = {
        "type": "dissent_override",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "iteration": iteration,
        "dissenter": dissenter,
        "consecutive_count": consecutive_count,
        "dissent_iterations": dissent_iterations,
    }
    _append_record(state_dir, record)


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
