#!/usr/bin/env python3
"""Safely clean up a completed agent worktree.

Validates that the agent has truly finished by checking its JSONL session
log before removing the worktree directory.

Usage:
    python3 scripts/cleanup_worktree.py <worktree-path>
    python3 scripts/cleanup_worktree.py .claude/worktrees/agent-a9d29d9e

Exit codes:
    0: success (removed or already gone)
    1: error (agent still running, no session log, bad path, removal failed)

Safety checks:
    1. Path must be under .claude/worktrees/agent-*
    2. Agent session JSONL must exist
    3. Last JSONL entry must be an assistant message (= agent finished)
    4. If checks pass, remove with git worktree remove --force,
       falling back to rm -rf if git fails
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def find_repo_root() -> Path:
    """Walk up from script location to find the repo root."""
    # Try git first
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
            cwd="/",  # Avoid getcwd errors
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Fallback: walk up from this script
    p = Path(__file__).resolve()
    while p != p.parent:
        if (p / "CLAUDE.md").exists():
            return p
        p = p.parent
    print("ERROR: cannot find repo root", file=sys.stderr)
    sys.exit(1)


def find_session_jsonl(repo_root: Path, agent_id: str) -> Path | None:
    """Find the JSONL session log for an agent ID (prefix match)."""
    subagents_dir = Path.home() / ".claude" / "projects"
    if not subagents_dir.exists():
        return None

    # Structure: .claude/projects/<project-slug>/<session-id>/subagents/agent-*.jsonl
    # Agent ID in worktree path is truncated (8 chars).
    # JSONL filename uses the full ID. Use recursive glob for prefix match.
    for jsonl in subagents_dir.rglob(f"agent-{agent_id}*.jsonl"):
        return jsonl
    return None


def agent_is_finished(jsonl_path: Path) -> tuple[bool, str]:
    """Check if the agent's last JSONL entry indicates completion.

    Returns (is_finished, reason).
    """
    try:
        # Read last non-empty line
        with open(jsonl_path, "rb") as f:
            # Seek backwards to find last line efficiently
            f.seek(0, 2)  # End of file
            file_size = f.tell()
            if file_size == 0:
                return False, "session log is empty"

            # Read last 64KB (should be more than enough for last entry)
            read_size = min(file_size, 65536)
            f.seek(-read_size, 2)
            chunk = f.read().decode("utf-8", errors="replace")

        lines = [l for l in chunk.strip().split("\n") if l.strip()]
        if not lines:
            return False, "no entries in session log"

        last_entry = json.loads(lines[-1])

        # Check if last entry is an assistant message (final result)
        msg = last_entry.get("message", {})
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            # Verify it has text content (not just tool calls)
            has_text = any(
                c.get("type") == "text" and c.get("text", "").strip()
                for c in content
                if isinstance(c, dict)
            )
            if has_text:
                return True, "last entry is assistant message with text"
            # Assistant message with only tool_use blocks = still working
            return False, "last entry is assistant message with only tool calls (still working)"

        # Any other type (progress, tool_result, etc.) = still running
        entry_type = last_entry.get("type", msg.get("role", "unknown"))
        return False, f"last entry type is '{entry_type}' (agent may still be running)"

    except (json.JSONDecodeError, KeyError, OSError) as e:
        return False, f"error reading session log: {e}"


def remove_worktree(repo_root: Path, worktree_path: Path) -> bool:
    """Remove the worktree. Returns True on success."""
    # cd to repo root first so the subprocess doesn't have its cwd inside
    # the directory being deleted (which causes spurious pwd errors).
    os.chdir(repo_root)

    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force",
             str(worktree_path)],
            capture_output=True, text=True, check=True,
        )
        print(f"Removed worktree via git: {worktree_path}", file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"git worktree remove failed: {e.stderr.strip()}", file=sys.stderr)

    # Fallback: rm -rf
    try:
        import shutil
        shutil.rmtree(worktree_path)
        # Also prune git's worktree metadata
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "prune"],
            capture_output=True, text=True,
        )
        print(f"Removed worktree via rm + prune: {worktree_path}", file=sys.stderr)
        return True
    except OSError as e:
        print(f"ERROR: failed to remove worktree: {e}", file=sys.stderr)
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: cleanup-worktree.sh <worktree-path>", file=sys.stderr)
        return 1

    worktree_arg = sys.argv[1]

    repo_root = find_repo_root()

    # Resolve worktree path (allow relative to repo root)
    worktree_path = Path(worktree_arg)
    if not worktree_path.is_absolute():
        worktree_path = repo_root / worktree_arg
    worktree_path = worktree_path.resolve()

    # Safety check 1: must be under .claude/worktrees/agent-*
    try:
        rel = worktree_path.relative_to(repo_root / ".claude" / "worktrees")
    except ValueError:
        print(f"ERROR: path is not under .claude/worktrees/: {worktree_path}", file=sys.stderr)
        return 1

    if not rel.parts[0].startswith("agent-"):
        print(f"ERROR: path does not match agent-* pattern: {worktree_path}", file=sys.stderr)
        return 1

    # Extract agent ID (truncated, 8 hex chars)
    agent_id = rel.parts[0].removeprefix("agent-")

    # Check if worktree still exists
    if not worktree_path.exists():
        print(f"Worktree already removed: {worktree_path}", file=sys.stderr)
        return 0

    # Safety check 2: find session log
    jsonl_path = find_session_jsonl(repo_root, agent_id)
    if jsonl_path is None:
        print(f"ERROR: no session log found for agent-{agent_id}. "
              f"Cannot verify agent completion.",
              file=sys.stderr)
        return 1

    # Safety check 3: verify agent is finished
    finished, reason = agent_is_finished(jsonl_path)
    if not finished:
        print(f"ERROR: agent appears to still be running: {reason}",
              file=sys.stderr)
        return 1

    print(f"Agent confirmed finished: {reason}", file=sys.stderr)

    # Remove the worktree
    if remove_worktree(repo_root, worktree_path):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
