#!/usr/bin/env python3
# venv: none
# permanent: true
"""_gh_pr_with_retry_payload.py — Build REST payloads for gh-pr-with-retry.sh.

Reads inputs from environment variables (set by the calling shell
wrapper), writes a JSON object to ``$GH_PR_PAYLOAD_FILE`` suitable for
``gh api -X POST/PATCH /repos/.../pulls[/<N>] --input <file>``.

Why a sibling helper instead of an inline ``python -c``
-------------------------------------------------------
Same reasoning as scripts/_gh_comment_with_retry_match.py: keeps multi-
line JSON-building unit-testable on its own, avoids the operator-laptop
preflight hook's friction with inline ``python -c`` content (the hook
fires on the agent's Bash tool calls; sibling .py files are fine), and
honors the project's "Multi-line Python always goes in a .py file"
rule for consistency.

Usage (called by gh-pr-with-retry.sh)
-------------------------------------
    GH_PR_TITLE="..." \
        GH_PR_BODY_FILE=/path/to/body.txt \
        GH_PR_HEAD=branch-name \
        GH_PR_BASE=main \
        GH_PR_PAYLOAD_FILE=/path/to/out.json \
        python3 _gh_pr_with_retry_payload.py create

    GH_PR_TITLE="..." \
        GH_PR_BODY_FILE=/path/to/body.txt \
        GH_PR_PAYLOAD_FILE=/path/to/out.json \
        python3 _gh_pr_with_retry_payload.py edit

Subcommands
-----------
    create   Builds {"title", "body", "head", "base"} for
             POST /repos/<owner>/<repo>/pulls.
    edit     Builds {"title"?, "body"?} for
             PATCH /repos/<owner>/<repo>/pulls/<N>. Both fields are
             optional individually but at least one must be present —
             the wrapper validates this before invoking the helper.

Env contract
------------
    GH_PR_PAYLOAD_FILE (str, required): destination path. The helper
        writes a JSON object to this path (UTF-8). Existing content is
        overwritten.

    GH_PR_TITLE (str): the PR title. Required for ``create``; optional
        for ``edit`` (omit by leaving the env var empty/unset).
    GH_PR_BODY_FILE (str): path to a UTF-8 file containing the PR body.
        Required for ``create``; optional for ``edit``.
    GH_PR_HEAD (str, required for ``create``): the PR's head branch
        (typically ``worktree-agent-<id>`` or the ralph branch).
    GH_PR_BASE (str, required for ``create``): the PR's base branch
        (typically ``main``).

Output
------
On success: writes JSON to ``$GH_PR_PAYLOAD_FILE``, exits 0 with no
stdout output.

On failure: prints an error to stderr and exits 1. The wrapper then
falls through to the original-failure passthrough path.

Tracking: issue #4527.
"""

from __future__ import annotations

import json
import os
import sys


def _read_body_file(path: str) -> str:
    """Read the PR body file as UTF-8 and return it verbatim.

    The GitHub REST API accepts ``body`` as a JSON string field; we
    don't strip trailing newlines or normalize whitespace — the body
    file is the source of truth, same as ``gh pr create --body-file``.
    """
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write_payload(path: str, payload: dict) -> None:
    """Write the payload JSON to ``path`` (UTF-8, compact).

    `gh api --input <file>` accepts either compact or pretty JSON;
    compact keeps the file small and readable in tests.
    """
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def _build_create_payload() -> dict:
    """Build the POST /pulls payload from env vars.

    All four fields (title, body, head, base) are required by the
    GitHub REST API. The wrapper validates their presence in shell
    before invoking the helper, so a missing env var here is a bug —
    we still validate to fail closed.
    """
    title = os.environ.get("GH_PR_TITLE", "")
    body_file = os.environ.get("GH_PR_BODY_FILE", "")
    head = os.environ.get("GH_PR_HEAD", "")
    base = os.environ.get("GH_PR_BASE", "")

    missing = []
    if not title:
        missing.append("GH_PR_TITLE")
    if not body_file:
        missing.append("GH_PR_BODY_FILE")
    if not head:
        missing.append("GH_PR_HEAD")
    if not base:
        missing.append("GH_PR_BASE")
    if missing:
        print(
            f"ERROR: missing required env vars for create: {', '.join(missing)}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    body = _read_body_file(body_file)

    return {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }


def _build_edit_payload() -> dict:
    """Build the PATCH /pulls/<N> payload from env vars.

    Both ``title`` and ``body`` are optional individually — the GitHub
    REST API accepts a partial PATCH that updates only the fields
    present. The wrapper guarantees at least one is set before invoking
    the helper; we double-check defensively.
    """
    title = os.environ.get("GH_PR_TITLE", "")
    body_file = os.environ.get("GH_PR_BODY_FILE", "")

    payload: dict = {}
    if title:
        payload["title"] = title
    if body_file:
        payload["body"] = _read_body_file(body_file)

    if not payload:
        print(
            "ERROR: edit requires at least one of GH_PR_TITLE or GH_PR_BODY_FILE",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return payload


def main() -> int:
    """Dispatch on argv[1] (subcommand) and write the payload."""
    if len(sys.argv) != 2:
        print(
            "usage: _gh_pr_with_retry_payload.py <create|edit>",
            file=sys.stderr,
        )
        return 1

    subcommand = sys.argv[1]

    payload_file = os.environ.get("GH_PR_PAYLOAD_FILE", "")
    if not payload_file:
        print(
            "ERROR: GH_PR_PAYLOAD_FILE must be set",
            file=sys.stderr,
        )
        return 1

    if subcommand == "create":
        payload = _build_create_payload()
    elif subcommand == "edit":
        payload = _build_edit_payload()
    else:
        print(f"ERROR: unknown subcommand: {subcommand}", file=sys.stderr)
        return 1

    _write_payload(payload_file, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
