#!/usr/bin/env python3
"""Post and read structured checkpoint comments on GitHub issues.

Used by the ralph loop to persist state at key milestones (SHIP)
so that crash recovery can detect what stage a task reached.

This script is stdlib-only — no venv is needed.  It shells out to ``gh``
for all GitHub API calls.

Usage:
    # Post ralph SHIP completion
    scripts/ralph_checkpoint.py ship \
        --issue 42 --branch worker-3/session-... --worktree {worktree}

    # Check if a checkpoint exists (exit 0 = yes, 1 = no)
    scripts/ralph_checkpoint.py check ship --issue 42

    # Read content from existing checkpoint
    scripts/ralph_checkpoint.py read ship --issue 42
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = "judgemind/judgemind"

# Machine-readable HTML markers embedded in checkpoint comments.
MARKER_SHIP = "<!-- ralph-checkpoint:ship -->"

MARKERS: dict[str, str] = {
    "ship": MARKER_SHIP,
}


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` CLI command and return the result.

    Raises ``SystemExit`` on failure so callers don't need to check.
    """
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"ERROR: gh {' '.join(args[:3])}... failed:", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return result


def _fetch_issue_comments(issue: int) -> list[dict[str, Any]]:
    """Fetch all comments on an issue via ``gh api``."""
    result = _run_gh(
        [
            "api",
            f"repos/{REPO}/issues/{issue}/comments",
            "--paginate",
        ]
    )
    # The paginated result may be a JSON array or multiple arrays.
    text = result.stdout.strip()
    if not text:
        return []
    comments: list[dict[str, Any]] = []
    # gh api --paginate may return multiple JSON arrays concatenated
    for match in re.finditer(r"\[.*?\]", text, re.DOTALL):
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                comments.extend(parsed)
        except json.JSONDecodeError:
            continue
    return comments


def _post_comment(issue: int, body: str) -> None:
    """Post a comment to a GitHub issue."""
    # Write body to a temp file to avoid shell escaping issues.
    # Use a well-known location based on the issue number.
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(body)
        tmp_path = f.name

    try:
        _run_gh(
            [
                "issue",
                "comment",
                str(issue),
                "--repo",
                REPO,
                "--body-file",
                tmp_path,
            ]
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Checkpoint detection and reading
# ---------------------------------------------------------------------------


def find_checkpoint_comment(
    issue: int,
    checkpoint_type: str,
) -> str | None:
    """Find the body of a checkpoint comment on an issue.

    Returns the full comment body if found, or ``None``.
    """
    marker = MARKERS.get(checkpoint_type)
    if marker is None:
        print(
            f"ERROR: Unknown checkpoint type: {checkpoint_type}",
            file=sys.stderr,
        )
        sys.exit(1)

    comments = _fetch_issue_comments(issue)
    for comment in comments:
        body = comment.get("body", "")
        if marker in body:
            return body
    return None


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------


def _git_diff_stat(worktree: str) -> str:
    """Get the abbreviated diff stat for a worktree."""
    try:
        # Combine staged + unstaged changes
        result = subprocess.run(
            ["git", "-C", worktree, "diff", "--stat", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        stat = result.stdout.strip()
        if stat:
            return stat

        # If HEAD diff is empty, try against origin/main
        result = subprocess.run(
            ["git", "-C", worktree, "diff", "--stat", "origin/main"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "(diff stats unavailable)"


def _git_shortstat(worktree: str) -> str:
    """Get the short stat summary (N files changed, +X, -Y)."""
    try:
        result = subprocess.run(
            ["git", "-C", worktree, "diff", "--shortstat", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        stat = result.stdout.strip()
        if stat:
            return stat

        result = subprocess.run(
            ["git", "-C", worktree, "diff", "--shortstat", "origin/main"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "(stats unavailable)"


def _count_passing_tests(worktree: str) -> str:
    """Try to determine passing test count from recent pytest output.

    This is best-effort — returns a placeholder if not determinable.
    """
    # Look for pytest output in ralph state
    state_dir = Path(worktree) / "tmp" / "ralph"
    # Check for a test-output.txt file that ralph might write
    for candidate in ["test-output.txt", "pytest-output.txt"]:
        path = state_dir / candidate
        if path.exists():
            text = path.read_text(encoding="utf-8")
            # Look for "N passed" in pytest output
            match = re.search(r"(\d+)\s+passed", text)
            if match:
                return f"{match.group(1)} passing"
    return "(test count not available)"


def format_ship_comment(
    branch: str,
    worktree: str,
) -> str:
    """Format the SHIP checkpoint comment."""
    shortstat = _git_shortstat(worktree)
    diff_stat = _git_diff_stat(worktree)
    tests = _count_passing_tests(worktree)

    return (
        "<details>\n"
        f"<summary>\u2705 Ralph SHIP \u2014 ready for commit/push/PR"
        f"</summary>\n\n"
        f"**Branch:** `{branch}`\n"
        f"**Files changed:** {shortstat}\n"
        f"**Tests:** {tests}\n\n"
        f"```\n{diff_stat}\n```\n\n"
        f"</details>\n\n"
        f"{MARKER_SHIP}"
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_ship(args: argparse.Namespace) -> None:
    """Post a SHIP checkpoint comment."""
    body = format_ship_comment(args.branch, args.worktree)
    _post_comment(args.issue, body)
    print(f"Posted SHIP checkpoint on issue #{args.issue}")


def cmd_check(args: argparse.Namespace) -> None:
    """Check if a checkpoint comment exists on an issue."""
    result = find_checkpoint_comment(args.issue, args.checkpoint_type)
    if result is not None:
        print(f"Checkpoint '{args.checkpoint_type}' found on issue #{args.issue}")
        sys.exit(0)
    else:
        print(
            f"Checkpoint '{args.checkpoint_type}' NOT found on issue #{args.issue}",
        )
        sys.exit(1)


def cmd_read(args: argparse.Namespace) -> None:
    """Read content from a checkpoint comment."""
    body = find_checkpoint_comment(args.issue, args.checkpoint_type)
    if body is None:
        print(
            f"ERROR: Checkpoint '{args.checkpoint_type}' not found "
            f"on issue #{args.issue}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        description="Post and read ralph checkpoint comments on GitHub issues.",
        prog="scripts/ralph_checkpoint.py",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ship
    p_ship = subparsers.add_parser(
        "ship",
        help="Post a SHIP checkpoint comment.",
    )
    p_ship.add_argument("--issue", type=int, required=True, help="Issue number.")
    p_ship.add_argument(
        "--branch",
        required=True,
        help="Branch name.",
    )
    p_ship.add_argument(
        "--worktree",
        required=True,
        help="Path to the worktree.",
    )
    p_ship.set_defaults(func=cmd_ship)

    # check
    p_check = subparsers.add_parser(
        "check",
        help="Check if a checkpoint exists (exit 0 = yes, 1 = no).",
    )
    p_check.add_argument(
        "checkpoint_type",
        choices=["ship"],
        help="Type of checkpoint to check.",
    )
    p_check.add_argument("--issue", type=int, required=True, help="Issue number.")
    p_check.set_defaults(func=cmd_check)

    # read
    p_read = subparsers.add_parser(
        "read",
        help="Read content from a checkpoint comment.",
    )
    p_read.add_argument(
        "checkpoint_type",
        choices=["ship"],
        help="Type of checkpoint to read.",
    )
    p_read.add_argument("--issue", type=int, required=True, help="Issue number.")
    p_read.set_defaults(func=cmd_read)

    return parser


def main() -> None:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
