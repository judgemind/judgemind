#!/usr/bin/env python3
"""Log a Claude review record to the ralph review log.

Called by the ralph loop after the Claude reviewer subagent completes.
Reads the verdict and feedback from the ralph state directory and appends
a structured JSONL record to review-log.jsonl.

Usage:
    python3 scripts/log_ralph_review.py <worktree-path>

The script reads:
  {worktree}/tmp/ralph/review-result.txt   — Claude verdict (SHIP or REVISE)
  {worktree}/tmp/ralph/feedback.md         — Claude feedback text
  {worktree}/tmp/ralph/iteration.txt       — Current iteration number

It appends a record to:
  {worktree}/tmp/ralph/review-log.jsonl
"""
# permanent: true

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing ralph_review_log from the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ralph_review_log import compute_diff_stats, log_review


def main() -> None:
    """Entry point."""
    if len(sys.argv) < 2:
        print(
            "Usage: python3 scripts/log_ralph_review.py <worktree-path>",
            file=sys.stderr,
        )
        sys.exit(1)

    worktree = Path(sys.argv[1])
    state_dir = worktree / "tmp" / "ralph"

    if not state_dir.is_dir():
        print(f"ERROR: Ralph state directory not found: {state_dir}", file=sys.stderr)
        sys.exit(1)

    # Read verdict
    result_path = state_dir / "review-result.txt"
    if not result_path.exists():
        print("ERROR: review-result.txt not found.", file=sys.stderr)
        sys.exit(1)
    verdict = result_path.read_text(encoding="utf-8").strip()

    # Read feedback
    feedback_path = state_dir / "feedback.md"
    feedback = ""
    if feedback_path.exists():
        feedback = feedback_path.read_text(encoding="utf-8")

    # Read iteration
    iteration_path = state_dir / "iteration.txt"
    iteration = 0
    if iteration_path.exists():
        try:
            iteration = int(iteration_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pass

    # Compute diff stats
    diff_stats = compute_diff_stats(worktree)

    # Log the record
    log_review(
        state_dir,
        iteration=iteration,
        model="claude",
        verdict=verdict,
        feedback=feedback,
        diff_stats=diff_stats,
    )

    print(f"Claude review logged: iteration={iteration}, verdict={verdict}")


if __name__ == "__main__":
    main()
