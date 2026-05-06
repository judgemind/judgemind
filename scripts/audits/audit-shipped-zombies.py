#!/usr/bin/env python3
# venv: none
# permanent: true
"""One-shot scan of the ``agent/ready`` queue for shipped zombies.

A "shipped zombie" is an open ``agent/ready`` issue whose code already
landed in a merged PR — typically a pre-#3994 placeholder-titled PR whose
body lacked a ``Closes #N`` keyword, so the GitHub auto-close never fired
and the issue stayed in the queue. Each zombie costs ~15 minutes of agent
wall time + one ``/task`` slot when re-claimed; cleaning the backlog
up-front keeps subsequent ``/task`` dispatches productive (#4210).

This is a one-shot audit, not a scheduled cron, because the bug class is
shrinking — ``.github/workflows/pr-title-check.yml`` (#3994) blocks new
placeholder-titled PRs, so the zombie population is bounded by the
pre-#3994 backlog.

Manual review steps for the operator
====================================

1. Run the audit:

       python3 scripts/audits/audit-shipped-zombies.py

2. Skim the markdown report on stdout. Each match line names:

   * the open ``agent/ready`` issue number + title,
   * the merged PR that landed the issue's code,
   * the file overlap (paths the PR's diff and the issue body both cite).

3. For each match, run ``/task #N`` from a fresh session. ``/task`` will
   itself re-run ``scripts/check-shipped-pr.sh`` as part of Step 4a.2 and
   pivot to the verify-and-close path automatically — no manual close
   required here. (See ``.claude/skills/task/SKILL.md`` §4a.2.)

4. False positives are rare but possible (e.g. an issue that legitimately
   extends a file an earlier PR shipped). The verify-and-close pivot in
   ``/task`` requires the agent to confirm the issue's acceptance criteria
   were actually satisfied by the named PR before closing — that is the
   final guardrail.

This script does NOT auto-close anything. It is a read-only report — the
operator (or a follow-up ``/task`` invocation) drives the close.

CLI
===

::

    scripts/audits/audit-shipped-zombies.py [--limit N] [--dry-run]
                                            [--repo OWNER/NAME]

* ``--limit N`` — pass-through to ``gh issue list --limit N`` (default
  200, the GitHub page-size cap).
* ``--dry-run`` — accepted for backwards compatibility with the
  acceptance criterion verb; the script is read-only by construction so
  the flag is a no-op.
* ``--repo OWNER/NAME`` — repository slug; defaults to
  ``judgemind/judgemind``. Test fixtures override this.

Exit codes
==========

* 0 — Audit completed (whether or not any zombies were found).
* 1 — Pre-flight failure (``gh`` CLI unavailable, ``gh issue list``
  failed, ``scripts/check-shipped-pr.sh`` not found).

Environment hooks (for tests)
=============================

* ``AUDIT_SHIPPED_GH_BIN`` — override ``gh`` binary path.
* ``AUDIT_SHIPPED_CHECK_BIN`` — override path to
  ``scripts/check-shipped-pr.sh``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = "judgemind/judgemind"
DEFAULT_LIMIT = 200
GH_LIST_TIMEOUT_SEC = 60
CHECK_SHIPPED_TIMEOUT_SEC = 120


def _resolve_check_shipped_bin() -> str:
    """Return the path to ``scripts/check-shipped-pr.sh``.

    Honours the ``AUDIT_SHIPPED_CHECK_BIN`` env var (used by tests). Falls
    back to the sibling path ``../check-shipped-pr.sh`` relative to this
    module so the script can be invoked from any cwd.
    """

    override = os.environ.get("AUDIT_SHIPPED_CHECK_BIN")
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / "check-shipped-pr.sh")


def _resolve_gh_bin() -> str:
    """Return the ``gh`` binary path, honouring ``AUDIT_SHIPPED_GH_BIN``."""

    return os.environ.get("AUDIT_SHIPPED_GH_BIN", "gh")


def list_agent_ready_issues(repo: str, limit: int, gh_bin: str) -> list[dict]:
    """Return the list of open ``agent/ready`` issues from ``repo``.

    Calls ``gh issue list --label agent/ready --state open --json
    number,title --limit <limit>`` and parses the JSON output.

    Raises ``RuntimeError`` if the ``gh`` invocation fails or the output
    cannot be parsed. The caller maps that to exit 1.
    """

    cmd = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        "agent/ready",
        "--state",
        "open",
        "--json",
        "number,title",
        "--limit",
        str(limit),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GH_LIST_TIMEOUT_SEC,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gh issue list invocation failed: {exc}") from exc

    if result.returncode != 0:
        raise RuntimeError(
            "gh issue list exited "
            f"{result.returncode}: {result.stderr.strip() or '(no stderr)'}"
        )

    try:
        issues = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"gh issue list returned non-JSON output: {exc}") from exc

    if not isinstance(issues, list):
        raise RuntimeError("gh issue list JSON was not a list")
    return issues


def check_one_issue(issue_number: int, check_bin: str) -> dict | None:
    """Run ``scripts/check-shipped-pr.sh`` for one issue.

    Returns the parsed JSON summary dict on a high-confidence shipped
    match (exit 0). Returns ``None`` for any other outcome (no match,
    transient error, malformed output) — the audit is best-effort and
    does not surface per-issue errors as a failure.

    The wrapper's stdout on a match is two lines:

    1. ``shipped: PR #N merged to main with K file overlap(s) ...``
    2. A pretty-printed JSON object (the summary).

    We slice off the leading ``shipped:`` line and parse the rest as
    JSON. Robust against whitespace and additional trailing lines.
    """

    cmd = [check_bin, str(issue_number)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CHECK_SHIPPED_TIMEOUT_SEC,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        # Exit 1 (no match) or exit 2 (error) — neither is a zombie.
        return None

    return _parse_shipped_summary(result.stdout)


def _parse_shipped_summary(stdout: str) -> dict | None:
    """Extract the JSON summary from a ``check-shipped-pr.sh`` exit-0 stdout.

    Output shape (from the wrapper):

    ::

        shipped: PR #1234 merged to main with 1 file overlap(s) for issue #5678 (exit 0)
        {
          "issue": 5678,
          "shipped_pr": 1234,
          ...
        }

    We strip up to the first ``{`` and parse from there to end-of-input.
    Returns ``None`` on any parse failure.
    """

    if not stdout:
        return None
    brace_idx = stdout.find("{")
    if brace_idx < 0:
        return None
    candidate = stdout[brace_idx:]
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def render_report(
    *,
    issues_scanned: int,
    matches: list[dict],
    repo: str,
) -> str:
    """Render a markdown report summarising the audit run.

    Always emits a header + scan summary. If ``matches`` is empty, prints
    "no zombies found" with the scan count. If non-empty, prints one
    table row per match.
    """

    lines: list[str] = []
    lines.append("# Shipped-zombie audit report")
    lines.append("")
    lines.append(f"- Repository: `{repo}`")
    lines.append(f"- Issues scanned: **{issues_scanned}**")
    lines.append(f"- Zombies found: **{len(matches)}**")
    lines.append("")

    if not matches:
        lines.append("No zombies found.")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Issue | Title | Shipped PR | Overlap (files) |")
    lines.append("|---|---|---|---|")
    for match in matches:
        issue_num = match.get("issue", "?")
        issue_title = match.get("issue_title", "")
        pr_num = match.get("shipped_pr", "?")
        overlap_files = match.get("overlap_files") or []
        overlap_repr = ", ".join(f"`{f}`" for f in overlap_files) or "—"
        lines.append(f"| #{issue_num} | {issue_title} | #{pr_num} | {overlap_repr} |")
    lines.append("")
    lines.append("## Next steps")
    lines.append("")
    lines.append(
        "Run `/task #N` on each match. `/task`'s Step 4a.2 will re-run "
        "`scripts/check-shipped-pr.sh` and pivot to the verify-and-close "
        "path automatically. See `.claude/skills/task/SKILL.md` §4a.2."
    )
    lines.append("")
    return "\n".join(lines)


def run_audit(
    *,
    repo: str,
    limit: int,
    gh_bin: str,
    check_bin: str,
) -> tuple[int, str]:
    """Run the audit end-to-end.

    Returns ``(exit_code, report)``. ``exit_code`` is 0 for a successful
    scan (regardless of how many zombies were found) and 1 for a
    pre-flight failure (gh missing, gh issue list failed,
    check-shipped-pr.sh not executable).
    """

    if shutil.which(gh_bin) is None:
        return (
            1,
            f"error: '{gh_bin}' CLI not found on PATH — cannot run audit\n",
        )
    if not Path(check_bin).exists() or not os.access(check_bin, os.X_OK):
        return (
            1,
            f"error: '{check_bin}' is not executable or does not exist\n",
        )

    try:
        issues = list_agent_ready_issues(repo, limit, gh_bin)
    except RuntimeError as exc:
        return (1, f"error: {exc}\n")

    matches: list[dict] = []
    for issue in issues:
        number = issue.get("number")
        title = issue.get("title", "")
        if not isinstance(number, int):
            continue
        summary = check_one_issue(number, check_bin)
        if summary is None:
            continue
        # Stitch the issue title into the summary so the report shows it.
        summary = dict(summary)
        summary.setdefault("issue", number)
        summary["issue_title"] = title
        matches.append(summary)

    report = render_report(
        issues_scanned=len(issues),
        matches=matches,
        repo=repo,
    )
    return (0, report)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audit-shipped-zombies",
        description=(
            "One-shot scan of the agent/ready queue for shipped zombies "
            "(open issues whose code already landed in a merged PR)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=(
            "Max issues to scan (passed through to "
            "`gh issue list --limit`). Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Accepted for backwards compatibility with the AC verb; the "
            "script is read-only by construction so this is a no-op."
        ),
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="Repository slug (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv) if argv is not None else sys.argv[1:])
    exit_code, report = run_audit(
        repo=args.repo,
        limit=args.limit,
        gh_bin=_resolve_gh_bin(),
        check_bin=_resolve_check_shipped_bin(),
    )
    sys.stdout.write(report)
    if not report.endswith("\n"):
        sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
