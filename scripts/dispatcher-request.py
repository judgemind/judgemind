#!/usr/bin/env python3
"""Append an instruction to the dispatcher inbox file.

Subagents use this script to request actions from the dispatcher, such as
restarting the Telegram responder, applying Terraform, or sending
notifications.

Usage::

    scripts/dispatcher-request.py restart_responder --reason "telegram-bridge code updated"
    scripts/dispatcher-request.py notify --message "Found regression in OC scraper"
    scripts/dispatcher-request.py terraform_apply --module telegram-bot
    scripts/dispatcher-request.py run_script --script scripts/tg-set-webhook.sh
    scripts/dispatcher-request.py file_issue --title "Bug report" --description "Details..."

The script is stdlib-only and does not require any venv.  It uses file
locking (``fcntl.flock``) to safely append entries even when the
dispatcher is reading from the same file concurrently.

The default inbox file is ``tmp/orchestrator_inbox.json`` relative to the
repo root.  Override with ``--inbox-file``.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import sys
from pathlib import Path

# Allowed action types -- kept in sync with InstructionKind in
# packages/telegram-bridge/src/telegram_bridge/dispatcher.py.
ALLOWED_ACTIONS = frozenset(
    {
        "restart_responder",
        "terraform_apply",
        "notify",
        "run_script",
        "file_issue",
    }
)


def _find_repo_root() -> Path:
    """Walk up from the script location to find the repo root.

    The repo root is the first ancestor that contains a ``.git`` file or
    directory.  For worktrees, ``.git`` is a file (not a directory) -- this
    handles both cases.
    """
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    # Fallback: assume scripts/ is one level below the repo root.
    return Path(__file__).resolve().parents[1]


def _append_to_inbox(entry: dict[str, object], inbox_file: str) -> None:
    """Atomically append *entry* to the JSON array in *inbox_file*.

    Uses ``fcntl.flock`` for mutual exclusion with the dispatcher's
    ``read_dispatcher_inbox()`` method.
    """
    path = Path(inbox_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    try:
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                content = f.read()
                existing: list[dict[str, object]] = (
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
        description="Append an instruction to the dispatcher inbox."
    )
    parser.add_argument(
        "action",
        choices=sorted(ALLOWED_ACTIONS),
        help="The action type to request.",
    )
    parser.add_argument(
        "--reason",
        default="",
        help="Reason for the request (used by restart_responder).",
    )
    parser.add_argument(
        "--message",
        default="",
        help="Message text (used by notify).",
    )
    parser.add_argument(
        "--module",
        default="",
        help="Terraform module name (used by terraform_apply).",
    )
    parser.add_argument(
        "--script",
        default="",
        help="Script path to execute (used by run_script).",
    )
    parser.add_argument(
        "--args",
        nargs="*",
        default=[],
        help="Arguments for run_script.",
    )
    parser.add_argument(
        "--title",
        default="",
        help="Issue title (used by file_issue).",
    )
    parser.add_argument(
        "--description",
        default="",
        help="Issue description (used by file_issue).",
    )
    parser.add_argument(
        "--priority",
        default="p2",
        help="Issue priority (used by file_issue, default: p2).",
    )
    parser.add_argument(
        "--labels",
        nargs="*",
        default=[],
        help="Issue labels (used by file_issue).",
    )
    parser.add_argument(
        "--from-issue",
        type=int,
        default=None,
        help="The issue number the subagent is working on.",
    )
    parser.add_argument(
        "--inbox-file",
        default=None,
        help="Path to the inbox file (default: tmp/orchestrator_inbox.json).",
    )

    args = parser.parse_args()

    # Determine inbox file path.
    if args.inbox_file:
        inbox_file = args.inbox_file
    else:
        repo_root = _find_repo_root()
        inbox_file = str(repo_root / "tmp" / "orchestrator_inbox.json")

    # Build the entry.
    entry: dict[str, object] = {
        "action": args.action,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }

    if args.from_issue is not None:
        entry["from_issue"] = args.from_issue

    if args.reason:
        entry["reason"] = args.reason
    if args.message:
        entry["message"] = args.message
    if args.module:
        entry["module"] = args.module
    if args.script:
        entry["script"] = args.script
    if args.args:
        entry["args"] = args.args
    if args.title:
        entry["title"] = args.title
    if args.description:
        entry["description"] = args.description
    if args.priority and args.priority != "p2":
        entry["priority"] = args.priority
    if args.labels:
        entry["labels"] = args.labels

    _append_to_inbox(entry, inbox_file)
    print(f"Queued {args.action} instruction to {inbox_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
