#!/usr/bin/env python3
# venv: none
# permanent: true
"""Markdown for the "raw-key reconciler failed to run" issue (#5095).

``.github/workflows/raw-key-reconcile.yml`` runs this from its ``if:
failure()`` step.  A failed run (the ECS task crashed, never launched, or
wrote no ``RECONCILE_RESULT`` line) records nothing, so the data-gap issue
and the /admin/data-quality panel both stay silent: the reconciler is blind
until someone fixes it.  The workflow files one open issue labelled
``raw-reconcile-failure`` (``--mode issue``), comments on it for each later
failure (``--mode comment``), and closes it on the next successful run.

Issue and dashboard only, never a human alert (CLAUDE.md "Human Alerts").

Inputs are files the workflow writes; every one is optional so a failure
early in the job (before the ECS task exists) still produces a useful body:

- ``--steps-json``: ``${{ toJSON(steps) }}``, the outcome of each step id.
- ``--log-events-json``: ``aws logs get-log-events --query 'events[].message'
  --output json`` for the task's log stream (the failure tail).

Standard library only: the workflow runs it with the runner's python3.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

LOG_GROUP = "/ecs/judgemind-ingestion-worker-dev"
#: Lines of the ECS log stream quoted in the issue.
TAIL_LINES = 40
#: Characters per quoted line, so one huge JSON line cannot swamp the issue.
MAX_LINE_CHARS = 500


def _load_json(path: str | None) -> Any:
    if not path:
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def failed_steps(steps: Any) -> list[str]:
    """Step ids whose outcome is ``failure``, in workflow order."""
    if not isinstance(steps, dict):
        return []
    return [
        sid
        for sid, info in steps.items()
        if isinstance(info, dict) and info.get("outcome") == "failure"
    ]


def log_tail(events: Any, limit: int = TAIL_LINES) -> list[str]:
    """The last ``limit`` non-empty log lines, each cut to MAX_LINE_CHARS."""
    if not isinstance(events, list):
        return []
    lines: list[str] = []
    for message in events:
        for line in str(message).splitlines():
            line = line.rstrip()
            if not line:
                continue
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " …"
            lines.append(line)
    return lines[-limit:]


def _fence(lines: list[str]) -> str:
    # A tail that itself contains ``` would end the block early.
    body = "\n".join(lines).replace("```", "'''")
    return f"```\n{body}\n```"


def details(
    *,
    run_url: str,
    event: str,
    task_id: str,
    exit_code: str,
    steps: Any,
    events: Any,
) -> str:
    """The per-failure block shared by the issue body and each comment."""
    failed = failed_steps(steps)
    out = [
        f"- Workflow run: {run_url}",
        f"- Trigger: `{event or 'unknown'}`",
        f"- Failed step(s): {', '.join(f'`{s}`' for s in failed) or 'unknown (see the run log)'}",
    ]
    if task_id:
        out.append(
            f"- ECS task: `{task_id}` (exit `{exit_code or 'unknown'}`; log group "
            f"`{LOG_GROUP}`, stream `oneshot/oneshot/{task_id}`)"
        )
    else:
        out.append(
            "- ECS task: none (the job failed before the reconciler task launched)"
        )
    tail = log_tail(events)
    out.append("")
    if tail:
        out += [f"**Last {len(tail)} lines of the task log:**", "", _fence(tail)]
    elif task_id:
        out.append("The task log stream is empty or could not be read.")
    else:
        out.append("No task log: open the workflow run for the failing step's output.")
    return "\n".join(out) + "\n"


def issue_body(detail_block: str) -> str:
    return (
        "## The raw-key reconciler failed to run\n"
        "\n"
        "The daily raw-key reconciler (`scripts/reconcile_raw_keys.py`, "
        "`.github/workflows/raw-key-reconcile.yml`) is the backstop for every "
        "capture-to-DB loss class. This run failed before it recorded a result, "
        "so nothing was reconciled and no gap could be reported: **the reconciler "
        "is blind until this is fixed.**\n"
        "\n" + detail_block + "\n"
        "### Next steps\n"
        "\n"
        "1. Check the run log and the task log above for the error (image drift, a "
        "changed CLI flag, a DB or S3 permission break).\n"
        "2. Fix the cause, then re-run the workflow (`gh workflow run "
        "raw-key-reconcile.yml`).\n"
        "3. The /admin/data-quality Raw-Key Reconciliation panel shows the last "
        "completed run and turns stale after 36h.\n"
        "\n"
        "Each later failed run comments here; the next successful run closes this "
        "issue automatically.\n"
    )


def comment_body(detail_block: str, when: str) -> str:
    return f"### Failed again: {when}\n\n{detail_block}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--mode", choices=("issue", "comment"), required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--event", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--exit-code", default="")
    parser.add_argument("--steps-json", default=None)
    parser.add_argument("--log-events-json", default=None)
    parser.add_argument("--when", default="", help="Timestamp for --mode comment.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    block = details(
        run_url=args.run_url,
        event=args.event,
        task_id=args.task_id,
        exit_code=args.exit_code,
        steps=_load_json(args.steps_json),
        events=_load_json(args.log_events_json),
    )
    if args.mode == "issue":
        sys.stdout.write(issue_body(block))
    else:
        sys.stdout.write(comment_body(block, args.when or "unknown time"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
