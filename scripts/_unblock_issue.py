#!/usr/bin/env python3
"""Helper for unblock-issue.sh — manually unblock a single issue via GitHub API.

Not meant to be run directly. Called by unblock-issue.sh with environment
variables: REPO, ISSUE, DRY_RUN, DATA_FILE, WORK_TMPDIR.

DATA_FILE is a JSON file containing {"body": "...", "labels": [...]} as returned
by ``gh issue view <N> --json body,labels``.

Logic:
- Parse all blockers from body using ``Blocked by:? #N`` pattern.
- For each blocker, check if it's closed via ``gh issue view``.
- Partition into closed_blockers vs open_blockers.
- Drop only lines for closed blockers from the body (never strip open-blocker lines).
- If the Dependencies section becomes empty after pruning, drop it.
- If closed_blockers is non-empty: write updated body via ``gh issue edit --body-file``.
- If ALL blockers are now closed AND status/blocked is in labels: flip to agent/ready.
- Honour DRY_RUN=true env var — print intended actions, no writes.
"""

# permanent: true
from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def gh(*args: str) -> str | None:
    """Run a gh CLI command and return stdout, or None on error."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)  # noqa: S603, S607
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


def strip_closed_blocker_lines(body: str, closed_numbers: set[str]) -> str:
    """Remove 'Blocked by #N' lines only for closed blockers.

    Accepts both the canonical ``Blocked by #N`` form and the colon variant
    ``Blocked by: #N`` (consistent with _unblock_dependents.py).

    Removes empty ``## Dependencies`` sections after pruning (reuses the same
    look-ahead logic from strip_blocked_lines in _unblock_dependents.py).
    """
    lines = body.splitlines()

    # Remove only the lines for the specified closed-blocker numbers
    def is_closed_blocker_line(line: str) -> bool:
        m = re.match(r"\s*Blocked by:?\s+#(\d+)", line)
        if m and m.group(1) in closed_numbers:
            return True
        return False

    lines = [line for line in lines if not is_closed_blocker_line(line)]

    # Remove empty "## Dependencies" section (same look-ahead logic as
    # _unblock_dependents.strip_blocked_lines)
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

    # Remove trailing blank lines
    while result and not result[-1].strip():
        result.pop()

    return "\n".join(result)


def main() -> None:
    """Process a single issue and prune stale closed-blocker lines."""
    repo = os.environ.get("REPO", "judgemind/judgemind")
    issue = os.environ.get("ISSUE", "")
    dry_run = os.environ.get("DRY_RUN", "false") == "true"
    data_file = os.environ.get("DATA_FILE", "")
    tmpdir = os.environ.get("WORK_TMPDIR", ".")

    if not issue:
        print("Error: ISSUE env var not set.", file=sys.stderr)
        sys.exit(1)

    if not data_file:
        print("Error: DATA_FILE env var not set.", file=sys.stderr)
        sys.exit(1)

    # Read the JSON data file written by the shell wrapper
    with open(data_file) as f:
        data = json.load(f)

    body: str = data.get("body") or ""
    raw_labels: list = data.get("labels") or []

    # Labels may be dicts {"name": "..."} (gh JSON output) or plain strings
    label_names: set[str] = set()
    for lbl in raw_labels:
        if isinstance(lbl, dict):
            label_names.add(lbl.get("name", ""))
        elif isinstance(lbl, str):
            label_names.add(lbl)

    # Find all blockers in the body (canonical and colon-variant)
    blockers = re.findall(r"Blocked by:?\s+#(\d+)", body)

    if not blockers:
        print(f"Issue #{issue}: no 'Blocked by' lines found — nothing to do.")
        sys.exit(0)

    print(f"Issue #{issue}: found blocker(s): {', '.join('#' + b for b in blockers)}")

    closed_blockers: list[str] = []
    open_blockers: list[str] = []

    for b in blockers:
        info = gh_json("issue", "view", b, "--repo", repo, "--json", "state")
        if not info:
            state = "FETCH_ERROR"
        else:
            state = info.get("state", "UNKNOWN")
        if state == "CLOSED":
            print(f"  Blocker #{b} is CLOSED.")
            closed_blockers.append(b)
        else:
            print(f"  Blocker #{b} is {state} — keeping line.")
            open_blockers.append(b)

    if not closed_blockers:
        print("No closed blockers to prune — nothing to do.")
        sys.exit(0)

    print(f"  Pruning {len(closed_blockers)} closed blocker(s): {', '.join('#' + b for b in closed_blockers)}")

    updated_body = strip_closed_blocker_lines(body, set(closed_blockers))

    if dry_run:
        print("[DRY RUN] Would update issue body (removed closed Blocked by lines).")
        if not open_blockers and "status/blocked" in label_names:
            print("[DRY RUN] Would remove label: status/blocked")
            print("[DRY RUN] Would add label: agent/ready")
        elif open_blockers:
            print(
                f"[DRY RUN] Keeping status/blocked — open blocker(s) remain: "
                f"{', '.join('#' + b for b in open_blockers)}"
            )
        sys.exit(0)

    # Write updated body to temp file and edit the issue
    body_file = os.path.join(tmpdir, f"_unblock_issue_body_{issue}.txt")
    with open(body_file, "w") as f:
        f.write(updated_body)

    result = gh("issue", "edit", issue, "--repo", repo, "--body-file", body_file)
    if result is None:
        print("Error: failed to update issue body.", file=sys.stderr)
        sys.exit(1)
    print("  Updated issue body (removed closed Blocked by lines).")

    # Flip labels only when ALL blockers are now closed
    if not open_blockers and "status/blocked" in label_names:
        flip = gh(
            "issue",
            "edit",
            issue,
            "--repo",
            repo,
            "--remove-label",
            "status/blocked",
            "--add-label",
            "agent/ready",
        )
        if flip is None:
            print("Warning: body updated but label flip failed.", file=sys.stderr)
        else:
            print("  Labels updated: -status/blocked, +agent/ready.")
    elif open_blockers:
        print(
            f"  Keeping status/blocked — open blocker(s) remain: "
            f"{', '.join('#' + b for b in open_blockers)}"
        )


if __name__ == "__main__":
    main()
