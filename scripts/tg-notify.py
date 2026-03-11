#!/usr/bin/env python3
"""Send a Telegram lifecycle notification for the orchestrator.

This is a thin CLI wrapper around the ``telegram_bridge`` package's
:class:`~telegram_bridge.client.TelegramBridge` and
:class:`~telegram_bridge.orchestrator.OrchestratorBridge` classes.

Usage::

    scripts/tg-notify.py task_started <issue> <title> <worker>
    scripts/tg-notify.py task_completed <issue> <summary> <worker>
    scripts/tg-notify.py task_failed <issue> <error> <worker>
    scripts/tg-notify.py pr_merged <pr_number> <title>
    scripts/tg-notify.py session_started
    scripts/tg-notify.py session_ended
    scripts/tg-notify.py notify <message>

The script exits silently (exit 0) when Telegram is not configured,
so it is safe to call unconditionally.

Environment:
    AWS credentials must be available (profile, env vars, or instance role).
    The bot token and chat IDs are read from Secrets Manager at startup.
"""

from __future__ import annotations

# Ensure we are running inside the telegram-bridge venv (re-execs if not).
from _venv_helper import ensure_venv  # noqa: E402

ensure_venv("telegram-bridge")

import asyncio
import sys


def _usage() -> str:
    return (
        "Usage:\n"
        "  scripts/tg-notify.py task_started <issue> <title> <worker>\n"
        "  scripts/tg-notify.py task_completed <issue> <summary> <worker>\n"
        "  scripts/tg-notify.py task_failed <issue> <error> <worker>\n"
        "  scripts/tg-notify.py pr_merged <pr_number> <title>\n"
        "  scripts/tg-notify.py session_started\n"
        "  scripts/tg-notify.py session_ended\n"
        "  scripts/tg-notify.py notify <message>\n"
    )


async def _main(args: list[str]) -> int:
    """Dispatch the notification based on the command-line action."""
    # Import here (after venv activation) so missing deps produce a
    # clear error from ensure_venv rather than an ImportError traceback.
    from telegram_bridge.orchestrator import create_orchestrator_bridge

    if len(args) < 1:
        print(_usage(), file=sys.stderr)
        return 1

    action = args[0]

    bridge = create_orchestrator_bridge(
        state_file="tmp/orchestrator_state.json",
        status_file="tmp/orchestrator_status.json",
        inbox_path="tmp/tg_inbox.json",
        stop_requests_path="tmp/stop_requests.json",
    )

    try:
        if action == "session_started":
            await bridge.session_started()

        elif action == "session_ended":
            await bridge.session_ended()

        elif action == "task_started":
            if len(args) < 4:
                print(
                    "Error: task_started requires <issue> <title> <worker>",
                    file=sys.stderr,
                )
                return 1
            issue = int(args[1])
            title = args[2]
            worker = int(args[3])
            await bridge.task_started(issue_number=issue, title=title, worker=worker)

        elif action == "task_completed":
            if len(args) < 4:
                print(
                    "Error: task_completed requires <issue> <summary> <worker>",
                    file=sys.stderr,
                )
                return 1
            issue = int(args[1])
            summary = args[2]
            worker = int(args[3])
            await bridge.task_completed(issue_number=issue, summary=summary, worker=worker)

        elif action == "task_failed":
            if len(args) < 4:
                print(
                    "Error: task_failed requires <issue> <title> <worker>",
                    file=sys.stderr,
                )
                return 1
            issue = int(args[1])
            error = args[2]
            worker = int(args[3])
            await bridge.task_failed(issue_number=issue, error=error, worker=worker)

        elif action == "pr_merged":
            if len(args) < 3:
                print(
                    "Error: pr_merged requires <pr_number> <title>",
                    file=sys.stderr,
                )
                return 1
            pr_number = args[1]
            title = args[2]
            await bridge.notify(f"PR #{pr_number} merged: {title}")
            bridge.write_status()

        elif action == "notify":
            if len(args) < 2:
                print("Error: notify requires <message>", file=sys.stderr)
                return 1
            message = " ".join(args[1:])
            await bridge.notify(message)

        else:
            print(f"Unknown action: {action}\n\n{_usage()}", file=sys.stderr)
            return 1

    finally:
        await bridge.bridge.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
