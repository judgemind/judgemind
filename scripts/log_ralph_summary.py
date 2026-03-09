#!/usr/bin/env python3
"""Log a summary record at the end of the ralph loop.

Called by the ralph loop after both reviewers agree to SHIP (or max
iterations are reached). Computes agreement rates and catch patterns
from the review log and appends a summary JSONL record.

Usage:
    python3 scripts/log_ralph_summary.py <worktree-path> [--verdict SHIP|MAX_ITERATIONS]

The script reads:
  {worktree}/tmp/ralph/iteration.txt       — Final iteration number
  {worktree}/tmp/ralph/review-log.jsonl    — All prior review records

It appends a summary record to:
  {worktree}/tmp/ralph/review-log.jsonl
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing ralph_review_log from the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ralph_review_log import log_summary


def main() -> None:
    """Entry point."""
    if len(sys.argv) < 2:
        print(
            "Usage: python3 scripts/log_ralph_summary.py <worktree-path> "
            "[--verdict SHIP|MAX_ITERATIONS]",
            file=sys.stderr,
        )
        sys.exit(1)

    worktree = Path(sys.argv[1])
    state_dir = worktree / "tmp" / "ralph"

    if not state_dir.is_dir():
        print(f"ERROR: Ralph state directory not found: {state_dir}", file=sys.stderr)
        sys.exit(1)

    # Parse optional --verdict flag
    final_verdict = "SHIP"
    for i, arg in enumerate(sys.argv[2:], start=2):
        if arg == "--verdict" and i + 1 < len(sys.argv):
            final_verdict = sys.argv[i + 1]

    # Read iteration
    iteration_path = state_dir / "iteration.txt"
    total_iterations = 0
    if iteration_path.exists():
        try:
            total_iterations = int(iteration_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pass

    # Log summary (reads review records from the log file internally)
    log_summary(
        state_dir,
        total_iterations=total_iterations,
        final_verdict=final_verdict,
    )

    print(
        f"Ralph summary logged: iterations={total_iterations}, verdict={final_verdict}"
    )


if __name__ == "__main__":
    main()
