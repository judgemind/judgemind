#!/usr/bin/env python3
# venv: scripts
# permanent: true
"""CLI helper — persist a per-iteration ralph patch to postgres (#3026).

Called by the ralph skill at end-of-iteration to save a cumulative
patch snapshot to ``dispatcher.ralph_patches`` so a daemon crash or
retry mid-loop can resume from the most recent iteration's state
instead of re-ralph'ing from scratch.

Design
------
Ralph runs as a Task-tool subagent inside the daemon's ``/task-v2-ralph``
subprocess. The daemon cannot directly hook into ralph's per-iteration
boundaries (the inner ``/ralph`` skill owns the iteration loop), so
this helper bridges the gap: ralph shells out to this script at the
end of each iteration, and the script does the DB write against the
shared ``DATABASE_URL`` env var.

The script is deliberately narrow and idempotent-ish:

* Captures the cumulative patch via
  ``git format-patch origin/main..HEAD --stdout``.
* INSERTs a fresh row (no DELETE supersede — additive per-iteration
  semantics from #3026).
* Exits 0 on all failure paths so a persistence hiccup never kills
  ralph. The happy path emits ``persisted=<patch_id>`` on stdout;
  failures emit ``skipped=<reason>`` or ``error=<reason>`` for log
  forensics.

Usage
-----
::

    scripts/dispatcher/persist_ralph_iteration.py \\
        --worktree "{worktree}" \\
        --agent-id "{agent_id}" \\
        --issue-number "{N}" \\
        --iteration "{iter_n}" \\
        --verdict LOOP|SHIP|ABORT

Env
---
* ``DATABASE_URL`` — postgres connection string. Required. Absent →
  stdout ``skipped=no_database_url``, exit 0.

See ``.claude/skills/ralph/SKILL.md`` §"Per-iteration patch persistence"
for where this hooks into the ralph loop, and
``scripts/dispatcher/daemon.py::_persist_ralph_iteration_patch`` for the
daemon-side helper (same semantics, different code path — the daemon
uses its own connection pool when the ralph subprocess is not
available, e.g. for unit tests).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _emit(status: str, detail: str = "") -> int:
    """Write a single status line to stdout and return exit code 0.

    The ralph skill swallows stdout — emitting a diagnostic is purely
    for the agent log / CloudWatch forensics. Exit 0 even on errors
    so ralph never gets a non-zero return code that would pollute its
    control flow.
    """
    if detail:
        sys.stdout.write(f"{status}={detail}\n")
    else:
        sys.stdout.write(f"{status}\n")
    sys.stdout.flush()
    return 0


def _capture_patch(worktree: Path) -> tuple[str | None, str | None]:
    """Return (patch_content, commit_sha). Both are None on failure."""
    try:
        patch_result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "format-patch",
                "origin/main..HEAD",
                "--stdout",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:
        return None, None
    if patch_result.returncode != 0 or not (patch_result.stdout or "").strip():
        return None, None

    commit_sha: str | None = None
    try:
        sha_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if sha_result.returncode == 0:
            candidate = (sha_result.stdout or "").strip()
            if candidate:
                commit_sha = candidate
    except Exception:
        pass
    return patch_result.stdout, commit_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Persist a per-iteration ralph patch to postgres (#3026)",
    )
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--iteration", required=True, type=int)
    parser.add_argument(
        "--verdict",
        required=True,
        help="Iteration verdict: LOOP, SHIP, ABORT, or a custom string.",
    )
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return _emit("skipped", "no_database_url")

    if not args.worktree.exists():
        return _emit("skipped", f"worktree_missing:{args.worktree}")

    patch_content, commit_sha = _capture_patch(args.worktree)
    if not patch_content:
        return _emit("skipped", "empty_or_failed_format_patch")

    # Import psycopg lazily — only needed on the happy path.
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        return _emit("skipped", "psycopg_not_installed")

    try:
        with psycopg.connect(database_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.ralph_patches "
                    "    (agent_id, issue_number, patch_content, commit_sha, "
                    "     iteration_n, verdict) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "RETURNING patch_id",
                    (
                        args.agent_id,
                        args.issue_number,
                        patch_content,
                        commit_sha,
                        args.iteration,
                        args.verdict,
                    ),
                )
                row = cur.fetchone()
                patch_id = str(row[0]) if row and row[0] else ""
            conn.commit()
    except Exception as exc:
        return _emit("error", str(exc)[:200])

    return _emit("persisted", patch_id)


if __name__ == "__main__":
    sys.exit(main())
