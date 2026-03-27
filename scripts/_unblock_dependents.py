#!/usr/bin/env python3
"""Helper for unblock-dependents.sh — processes blocked issues via GitHub API.

Not meant to be run directly. Called by unblock-dependents.sh with environment
variables: REPO, CLOSED_ISSUE, DRY_RUN, BLOCKED_ISSUES, WORK_TMPDIR.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def gh(*args: str) -> str | None:
    """Run a gh CLI command and return stdout, or None on error."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)  # noqa: S603, S607
    if r.returncode != 0:
        print(f"  gh error: {r.stderr.strip()}", file=sys.stderr)
        return None
    return r.stdout.strip()


def gh_json(*args: str) -> dict | list | None:
    """Run a gh CLI command and parse stdout as JSON."""
    out = gh(*args)
    if not out:
        return None
    return json.loads(out)


def strip_blocked_lines(body: str) -> str:
    """Remove 'Blocked by #N' lines and empty '## Dependencies' sections."""
    lines = body.splitlines()

    # Remove all "Blocked by #N" lines
    lines = [line for line in lines if not re.match(r"\s*Blocked by #\d+", line)]

    # Remove empty "## Dependencies" section
    result: list[str] = []
    i = 0
    while i < len(lines):
        if re.match(r"^## Dependencies\s*$", lines[i]):
            # Look ahead: skip blank lines; if next content is a heading or EOF, drop section
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines) or lines[j].startswith("#"):
                # Empty section — drop the heading and blank lines
                i = j
                continue
            # Section has remaining content — keep the heading
        result.append(lines[i])
        i += 1

    # Remove trailing blank lines that might be left over
    while result and not result[-1].strip():
        result.pop()

    return "\n".join(result)


def main() -> None:
    """Process blocked issues and unblock those with all blockers resolved."""
    repo = os.environ.get("REPO", "judgemind/judgemind")
    closed_issue = os.environ.get("CLOSED_ISSUE", "")
    dry_run = os.environ.get("DRY_RUN", "false") == "true"
    blocked_json = os.environ.get("BLOCKED_ISSUES", "[]")
    tmpdir = os.environ.get("WORK_TMPDIR", ".")

    issues: list[dict] = json.loads(blocked_json)
    unblocked_count = 0
    skipped_count = 0

    for issue in issues:
        n = issue["number"]
        body = issue.get("body") or ""

        # Verify this issue actually has "Blocked by #N" for our closed issue
        if not re.search(rf"Blocked by #{closed_issue}\b", body):
            continue

        print(f"Issue #{n}:")

        # Find ALL blockers in the body
        blockers = re.findall(r"Blocked by #(\d+)", body)
        if not blockers:
            print("  No 'Blocked by' lines found (unexpected). Skipping.")
            skipped_count += 1
            continue

        # Check if all blockers are resolved
        all_resolved = True
        for b in blockers:
            info = gh_json("issue", "view", b, "--repo", repo, "--json", "state")
            if not info or info.get("state") != "CLOSED":
                state = info.get("state", "UNKNOWN") if info else "FETCH_ERROR"
                print(f"  Blocker #{b} is {state} — cannot unblock yet.")
                all_resolved = False
                break
            print(f"  Blocker #{b} is CLOSED.")

        if not all_resolved:
            print(f"  Skipping #{n} (not all blockers resolved).")
            print()
            skipped_count += 1
            continue

        print("  All blockers resolved!")

        updated_body = strip_blocked_lines(body)

        if dry_run:
            print("  [DRY RUN] Would update body (removed Blocked by lines)")
            print("  [DRY RUN] Would remove label: status/blocked")
            print("  [DRY RUN] Would add label: agent/ready")
        else:
            # Write updated body to temp file and update the issue
            body_file = os.path.join(tmpdir, f"_unblock_body_{n}.txt")
            with open(body_file, "w") as f:
                f.write(updated_body)

            gh("issue", "edit", str(n), "--repo", repo, "--body-file", body_file)
            print("  Updated issue body (removed Blocked by lines).")

            # Update labels
            gh(
                "issue", "edit", str(n), "--repo", repo,
                "--remove-label", "status/blocked", "--add-label", "agent/ready",
            )
            print("  Labels updated: -status/blocked, +agent/ready.")

        unblocked_count += 1
        print()

    # Summary
    print("---")
    if dry_run:
        print(f"[DRY RUN] Would unblock {unblocked_count} issue(s), skipped {skipped_count}.")
    else:
        print(f"Unblocked {unblocked_count} issue(s), skipped {skipped_count}.")


if __name__ == "__main__":
    main()
