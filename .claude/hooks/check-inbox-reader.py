#!/usr/bin/env python3
"""Read, echo, and truncate the subagent inbox file.

Called by ``check-inbox.sh`` (PostToolUse hook).  This helper handles JSON
parsing and file locking so the shell hook can remain simple and fast.

Usage::

    python3 check-inbox-reader.py /path/to/inbox.json

Prints each message prefixed with ``[orchestrator]`` to stdout, then truncates
the file.  If the file is empty, contains invalid JSON, or has no messages,
it exits silently.

Exit 0 always — errors are swallowed to avoid blocking tool use.
"""

from __future__ import annotations

import fcntl
import json
import sys


def main() -> None:
    """Read messages from the inbox file, print them, and truncate."""
    if len(sys.argv) < 2:
        return

    inbox_path = sys.argv[1]

    try:
        with open(inbox_path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            content = f.read()

            if not content.strip():
                # Whitespace-only — truncate to avoid repeated invocations
                f.seek(0)
                f.truncate()
                return

            try:
                messages = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                # Malformed JSON — truncate to avoid repeated errors
                f.seek(0)
                f.truncate()
                return

            if not isinstance(messages, list) or len(messages) == 0:
                f.seek(0)
                f.truncate()
                return

            # Print messages to stdout
            for msg in messages:
                if isinstance(msg, dict) and "message" in msg:
                    print(f"[orchestrator] {msg['message']}")

            # Truncate the file
            f.seek(0)
            f.truncate()
    except Exception:
        pass


if __name__ == "__main__":
    main()
