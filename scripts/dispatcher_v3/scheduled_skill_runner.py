"""Scheduled-skill entrypoint for the dispatcher-v3 ECS task.

Invoked as ``python -m dispatcher_v3.scheduled_skill_runner`` by F2's
``scheduled-skill`` ECS task definition
(``infra/terraform/modules/dispatcher-v3-task-defs/main.tf``).

The EventBridge → ECS RunTask → SKILL_NAME env override →
``claude -p /$SKILL_NAME`` chain is described in
``docs/specs/dispatcher-v3-spec.md`` §4.4.

Unlike ``dispatcher_v3.agent_runner`` this module needs no S3 upload,
no session-log tee, and no runners-dispatch dict — every scheduled skill
is invoked with the same minimal argv: ``["claude", "-p", f"/{skill}"]``.
stdout/stderr are inherited so the awslogs log driver ships them directly
to CloudWatch without an intermediate tee.

``subprocess.Popen`` (not ``subprocess.run``) is used so we avoid the
``check-subprocess-timeouts.sh`` ``timeout=`` requirement; the ECS
task-def ``stopTimeout`` (2 h) is the wall-clock bound.

Emits single-line JSON log records (``skill_started``, ``skill_completed``,
``skill_failed``) to stdout so a future CloudWatch log-metric-filter alarm
can detect silent failures without custom instrumentation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

RECOGNIZED_SKILLS: frozenset[str] = frozenset(
    {
        "audit",
        "dispatcher-audit",
        "dispatcher-daily-report",
        "spotcheck",
    }
)


def main() -> int:
    """Read SKILL_NAME, validate it, spawn ``claude -p /<skill>``, return rc."""
    skill = os.environ.get("SKILL_NAME")

    if not skill:
        print("SKILL_NAME env var required", file=sys.stderr)
        return 2

    if skill not in RECOGNIZED_SKILLS:
        print(f"unknown skill: {skill}", file=sys.stderr)
        return 2

    argv = ["claude", "-p", f"/{skill}"]

    print(
        json.dumps({"event": "skill_started", "skill": skill}),
        flush=True,
    )

    start = time.monotonic()
    proc = subprocess.Popen(argv)
    rc = proc.wait()
    elapsed = round(time.monotonic() - start, 2)

    event = "skill_completed" if rc == 0 else "skill_failed"
    print(
        json.dumps(
            {
                "event": event,
                "skill": skill,
                "exit_code": rc,
                "elapsed_seconds": elapsed,
            }
        ),
        flush=True,
    )

    return rc


if __name__ == "__main__":
    sys.exit(main())
