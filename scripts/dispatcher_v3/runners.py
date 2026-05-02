"""Runner argv builders for the dispatcher-v3 task-runner.

The v3 task-runner ECS task launches one of several CLI agents (claude,
gemini, opencode, cursor) per dispatched issue. Argv construction is
centralized here so adding a new runner is one ``RUNNERS`` dict entry
plus one test, and so the empirically-discovered ``--worktree=NAME``
foot-gun (see PR #3868 and issue #3835) is documented in code rather
than re-discovered at the call site.

This module is intentionally tiny and dependency-free at import time so
the dispatcher daemon, the task-runner entrypoint, and unit tests can
all import it without dragging in boto3 or psycopg.
"""

from __future__ import annotations

import os
import sys
from typing import Callable

# IMPORTANT: ``--worktree`` takes an OPTIONAL value, so passing it as
# ``-w "<prompt>"`` causes the prompt to be consumed as the worktree
# name. Always use ``--worktree=NAME`` form (with the ``=``), with the
# prompt as the trailing positional argument. Established empirically
# via PR #3868 / issue #3835 — do not "simplify" by switching to ``-w``.
RUNNERS: dict[str, Callable[[str, str], list[str]]] = {
    "claude": lambda n, agent_id: [
        "claude",
        "-p",
        f"--worktree=agent-{agent_id}",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        f"/task #{n}",
    ],
    "gemini": lambda n, agent_id: [
        "gemini",
        "-p",
        f"Run /task for issue #{n}",
        "--output-format",
        "stream-json",
    ],
    "opencode": lambda n, agent_id: [
        "timeout",
        "6h",
        "opencode",
        "run",
        f"Run /task for issue #{n}",
    ],
    "cursor": lambda n, agent_id: [
        "timeout",
        "6h",
        "cursor-agent",
        "-p",
        f"Run /task for issue #{n}",
        "-m",
        os.environ.get("MODEL", ""),
    ],
}


def build_argv(runner: str, issue_number: str, agent_id: str) -> list[str]:
    """Return the argv list for ``runner`` with the given issue + agent id.

    Exits the process with a usage error if ``runner`` is not registered
    in ``RUNNERS``. The dispatcher's task-runner is a one-shot subprocess
    so ``sys.exit`` is the right escalation here — there is no reason to
    raise and unwind when we cannot proceed.
    """
    if runner not in RUNNERS:
        sys.exit(f"unknown runner: {runner}")
    return RUNNERS[runner](issue_number, agent_id)
