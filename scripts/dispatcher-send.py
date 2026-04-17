#!/usr/bin/env python3
"""Send a message from the dispatcher to a running subagent's worktree inbox.

The dispatcher uses this script to communicate with running ``/task`` agents.
Messages are written to ``{worktree}/tmp/inbox.json`` and picked up by the
subagent's PostToolUse hook (``check-inbox.sh``).

Usage::

    scripts/dispatcher-send.py --worktree /path/to/worktree "Rebase onto latest main"
    scripts/dispatcher-send.py --worktree /path/to/worktree "Issue #42 was deprioritized"

The script is stdlib-only and does not require any venv.  It uses file
locking (``fcntl.flock``) to safely append entries even when the subagent's
hook is reading from the same file concurrently.
"""
# permanent: true

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import sys
from pathlib import Path


def _append_to_inbox(entry: dict[str, str], inbox_file: str) -> None:
    """Atomically append *entry* to the JSON array in *inbox_file*.

    Uses ``fcntl.flock`` for mutual exclusion with the subagent's
    ``check-inbox.sh`` hook.
    """
    path = Path(inbox_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    try:
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                content = f.read()
                existing: list[dict[str, str]] = (
                    json.loads(content) if content.strip() else []
                )
            except (json.JSONDecodeError, ValueError):
                existing = []

            existing.append(entry)
            f.seek(0)
            json.dump(existing, f, indent=2, default=str)
            f.write("\n")
            f.truncate()
    except Exception as exc:
        print(f"ERROR: Failed to write inbox file: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Send a message from the dispatcher to a subagent's worktree inbox."
    )
    parser.add_argument(
        "--worktree",
        required=True,
        help="Path to the subagent's worktree directory.",
    )
    parser.add_argument(
        "message",
        help="The message to send to the subagent.",
    )

    args = parser.parse_args()

    worktree = Path(args.worktree)
    if not worktree.is_dir():
        print(
            f"ERROR: Worktree directory does not exist: {worktree}",
            file=sys.stderr,
        )
        return 1

    inbox_file = str(worktree / "tmp" / "inbox.json")

    entry: dict[str, str] = {
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "message": args.message,
    }

    _append_to_inbox(entry, inbox_file)
    print(f"Sent message to {inbox_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
