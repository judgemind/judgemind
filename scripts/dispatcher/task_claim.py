#!/usr/bin/env python3
"""Deprecated stub — ``/task`` skill no longer writes to ``dispatcher.agents``.

Originally (#2866) this helper wrote ``kind='task-skill'`` rows into
``dispatcher.agents`` so the ``/task`` skill and the Fargate daemon shared
a single atomic claim slot (the partial UNIQUE INDEX
``idx_dispatcher_agents_active_issue``). The interlock worked, but the
cumulative complexity — six distinct ``kind != 'task-skill'`` carve-outs
scattered across the daemon, plus admin-cockpit badge plumbing, plus
tooltip templates that had to cope with ``phase='claiming'`` forever,
plus retry-marker and circuit-breaker guards — was large (see #2927 for
the full ledger). Two different lifecycles in one table was the wrong
data model.

Issue #2927 simplified the design: ``/task`` switched to **label-only
coordination** (``status/in-progress`` on claim, remove on terminal) and
stopped writing to the database entirely. The daemon's queue scan
already filters on that label set, so one primitive covers the
subagent↔daemon interlock with a ~100ms race window that the operator
accepted as worth the complexity reduction. See the ``Claim interlock``
section of ``CLAUDE.md`` and ``.claude/skills/task/SKILL.md`` §Step 2a
for the shipped protocol.

This file remains as a deprecation stub so any lingering CLAUDE.md copy,
older agent transcript, or stale retrospective script that still calls
``scripts/dispatcher/task_claim.py claim`` / ``terminal`` does not crash
mid-task. Both subcommands:

* accept the same CLI flags as the pre-#2927 helper (so argparse does
  not reject them),
* emit a one-line deprecation notice on stderr explaining the new
  label-only flow,
* print a minimal JSON payload on stdout for callers that parse it,
* exit 0.

No database access, no subprocess shell-outs, no ``dispatcher.agents``
writes. Callers must migrate to the label-only flow — this stub is a
compatibility shim, not a supported entry point.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

import argparse
import json
import sys


#: One-line message emitted on stderr for any invocation. Keeps the
#: /task skill quiet during normal runs (stderr is not consulted on exit
#: 0 by the skill) but gives a human running the CLI by hand a clear
#: pointer to the new protocol.
DEPRECATION_NOTICE = (
    "task_claim: deprecated no-op (issue #2927). /task now uses "
    "label-only coordination via status/in-progress — see "
    ".claude/skills/task/SKILL.md §Step 2a and CLAUDE.md §Claim "
    "interlock for the current protocol. This stub exits 0 without "
    "touching dispatcher.agents."
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments for the two subcommands.

    The flag signature matches the pre-#2927 helper exactly so
    historical invocations from older agent transcripts or stale
    retrospective scripts do not crash on argparse rejection. All
    argument values are ignored by the stub — only the subcommand
    name is used to decide which no-op payload to emit.
    """
    parser = argparse.ArgumentParser(
        description="Deprecated no-op stub (see module docstring + #2927).",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    claim = sub.add_parser("claim", help="Deprecated no-op.")
    claim.add_argument("--issue", required=True, type=int)
    claim.add_argument("--agent-id", required=True)
    claim.add_argument("--worktree-path", required=True)
    claim.add_argument("--issue-title", default=None)

    term = sub.add_parser("terminal", help="Deprecated no-op.")
    term.add_argument("--agent-id", required=True)
    term.add_argument("--status", required=True)
    term.add_argument("--pr-number", default=None, type=int)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — emits deprecation notice + no-op JSON + exit 0."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    sys.stderr.write(DEPRECATION_NOTICE + "\n")
    if args.subcommand == "claim":
        print(
            json.dumps(
                {
                    "status": "deprecated_noop",
                    "subcommand": "claim",
                    "issue_number": args.issue,
                }
            )
        )
        return 0
    if args.subcommand == "terminal":
        print(
            json.dumps(
                {
                    "status": "deprecated_noop",
                    "subcommand": "terminal",
                    "agent_id": args.agent_id,
                }
            )
        )
        return 0
    # argparse's ``required=True`` subparser rejects unknown subcommands
    # before we get here, but the explicit branch keeps the type checker
    # happy and documents the invariant.
    sys.stderr.write(f"Unknown subcommand: {args.subcommand}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
