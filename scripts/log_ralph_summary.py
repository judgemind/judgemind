#!/usr/bin/env python3
"""Log a summary record at the end of the ralph loop.

Called by the ralph loop after both reviewers agree to SHIP (or max
iterations are reached). Computes agreement rates and catch patterns
from the review log and appends a summary JSONL record.

Usage:
    python3 scripts/log_ralph_summary.py <worktree-path> [--verdict SHIP|MAX_ITERATIONS]
        [--agent-id <agent-id>] [--issue-number <N>]

The script reads:
  {worktree}/tmp/ralph/iteration.txt       — Final iteration number
  {worktree}/tmp/ralph/review-log.jsonl    — All prior review records

It appends a summary record to:
  {worktree}/tmp/ralph/review-log.jsonl

When --agent-id and --issue-number are both provided (or successfully derived
from the worktree path / task.md), the complete review-log.jsonl is also
mirrored to S3 under ralph-reviews/<YYYY-MM-DD>/<agent-id>-<issue>.jsonl.
If either flag is absent, they are derived from:
  - agent_id: basename of <worktree-path> (e.g. "agent-ab4722a2")
  - issue_number: first line of {worktree}/tmp/ralph/task.md
    (expected format: "# Issue #<N> — <title>")
Both fallbacks return None on miss; missing inputs skip the upload.
"""
# permanent: true

from __future__ import annotations

import re
import sys
from pathlib import Path

# Allow importing ralph_review_log from the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ralph_review_log import log_summary


def _derive_agent_id(worktree: Path) -> str | None:
    """Derive agent_id from the worktree basename.

    Expected basename pattern: ``agent-<id>`` (e.g. ``agent-ab4722a2``).
    Returns the full basename (e.g. ``agent-ab4722a2``) or None on miss.
    """
    name = worktree.name
    if re.match(r"^agent-[0-9a-f]+$", name):
        return name
    return None


def _derive_issue_number(state_dir: Path) -> int | None:
    """Derive issue_number from the first line of task.md.

    Expected format: ``# Issue #<N> — <title>``
    Returns the integer issue number or None on miss.
    """
    task_md = state_dir / "task.md"
    if not task_md.exists():
        return None
    try:
        first_line = task_md.read_text(encoding="utf-8").splitlines()[0]
        m = re.match(r"^#\s+Issue\s+#(\d+)", first_line)
        if m:
            return int(m.group(1))
    except (OSError, IndexError, ValueError):
        pass
    return None


def main() -> None:
    """Entry point."""
    if len(sys.argv) < 2:
        print(
            "Usage: python3 scripts/log_ralph_summary.py <worktree-path> "
            "[--verdict SHIP|MAX_ITERATIONS] [--agent-id <id>] [--issue-number <N>]",
            file=sys.stderr,
        )
        sys.exit(1)

    worktree = Path(sys.argv[1])
    state_dir = worktree / "tmp" / "ralph"

    if not state_dir.is_dir():
        print(f"ERROR: Ralph state directory not found: {state_dir}", file=sys.stderr)
        sys.exit(1)

    # Parse optional flags
    final_verdict = "SHIP"
    agent_id_arg: str | None = None
    issue_number_arg: int | None = None

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--verdict" and i + 1 < len(args):
            final_verdict = args[i + 1]
            i += 2
        elif arg == "--agent-id" and i + 1 < len(args):
            agent_id_arg = args[i + 1]
            i += 2
        elif arg == "--issue-number" and i + 1 < len(args):
            try:
                issue_number_arg = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    # Derive agent_id / issue_number from context when not explicitly supplied
    agent_id = agent_id_arg if agent_id_arg is not None else _derive_agent_id(worktree)
    issue_number = (
        issue_number_arg
        if issue_number_arg is not None
        else _derive_issue_number(state_dir)
    )

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
        agent_id=agent_id,
        issue_number=issue_number,
    )

    upload_note = ""
    if agent_id is not None and issue_number is not None:
        upload_note = f" (S3 upload attempted: agent={agent_id}, issue={issue_number})"

    print(
        f"Ralph summary logged: iterations={total_iterations}, verdict={final_verdict}"
        f"{upload_note}"
    )


if __name__ == "__main__":
    main()
