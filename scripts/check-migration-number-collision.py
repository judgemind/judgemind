#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-migration-number-collision.py — Detect migration-number conflicts
across open pull requests.

Motivation (#2916): Two open PRs can independently claim the same
migration number because `migration-files-check` only inspects the PR's
own diff. Example (2026-04-20): #2899 and #2900 both picked migration
33 in parallel.

This check:
  1. Reads migration numbers added or modified in the current PR's diff
     vs a base ref (default: `origin/main`).
  2. For each number, queries `gh pr list --state open --json
     number,title,files` and grep-detects any OTHER open PR that
     touches `packages/api/migrations/N_*.sql` with the same N.
  3. Exits non-zero with a clear message naming the conflicting PRs if
     a collision is found.
  4. Bonus: emits a WARNING (no failure) if a migration number in the
     current PR is `<= the latest migration number already on `base`,
     which signals a stale branch that will conflict on rebase.

The check is pure-ish: the I/O layer is small (git diff, gh pr list,
ls packages/api/migrations on base). Core detection is a function
`find_collisions(my_numbers, other_prs, current_pr_number)` that unit
tests can exercise against canned inputs.

Usage:
    scripts/check-migration-number-collision.py
    scripts/check-migration-number-collision.py --base main
    scripts/check-migration-number-collision.py --current-pr 1234
    scripts/check-migration-number-collision.py \\
        --gh-pr-list-file pr_list.json --diff-file diff.txt
    scripts/check-migration-number-collision.py --help

Exit codes:
    0 — No collisions detected (warnings may still be printed).
    1 — Collision detected with another open PR.
    2 — Script error (cannot parse inputs, cannot run git/gh, etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = "packages/api/migrations"
_MIGRATION_FILE_RE = re.compile(r"^packages/api/migrations/(\d+)_.+\.sql$")


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------


@dataclass
class OtherPR:
    """A single open PR to compare against.

    Fields mirror the output of `gh pr list --json number,title,files`.
    """

    number: int
    title: str
    migration_numbers: list[int]


@dataclass
class Collision:
    """A single collision: `my_number` appears in `other_pr.migration_numbers`."""

    my_number: int
    other_pr: OtherPR


# -----------------------------------------------------------------------------
# Parsing helpers (pure — easy to unit-test)
# -----------------------------------------------------------------------------


def extract_migration_number(path: str) -> int | None:
    """Return the migration number for a given repo-relative path, or None.

    A path qualifies only if it sits directly under packages/api/migrations/
    and begins with a digit-prefix followed by an underscore and `.sql`:

        packages/api/migrations/33_dispatcher-thing.sql -> 33
        packages/api/migrations/README.md              -> None
        packages/api/migrations/sub/1_inner.sql        -> None
    """
    m = _MIGRATION_FILE_RE.match(path.strip())
    if not m:
        return None
    return int(m.group(1))


def parse_added_migration_numbers_from_diff(diff_text: str) -> list[int]:
    """From a unified `git diff` over the PR, return migration numbers added
    or modified.

    We care about `diff --git a/<path> b/<path>` headers where the b-side
    path matches our migration pattern. This covers:
      - Added migration files (new side present, old side absent)
      - Modified migration files (should still flag; better safe)
      - Renamed migration files landing on a new number
    and ignores:
      - Deleted migration files (we don't collide with a deletion)
      - Unrelated files

    Returns a de-duplicated, sorted-ascending list.
    """
    numbers: set[int] = set()
    for line in diff_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        # `diff --git a/path b/path`
        parts = line.split(" ")
        if len(parts) < 4:
            continue
        new_token = parts[-1]
        if not new_token.startswith("b/"):
            continue
        new_path = new_token[2:]
        n = extract_migration_number(new_path)
        if n is not None:
            numbers.add(n)
    return sorted(numbers)


def parse_gh_pr_list(
    pr_list_json_text: str,
) -> list[OtherPR]:
    """Parse the output of `gh pr list --state open --json number,title,files`.

    Each element has shape:
        {"number": 2899, "title": "...", "files": [{"path": "...", ...}, ...]}

    We extract migration numbers from each PR's file list so the collision
    test has a compact structure to work with. Returns a list sorted by PR
    number ascending.
    """
    try:
        data = json.loads(pr_list_json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON from gh pr list: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("expected a JSON array from gh pr list")

    prs: list[OtherPR] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        title = entry.get("title") or ""
        files = entry.get("files") or []
        if not isinstance(number, int):
            continue
        mig_numbers: set[int] = set()
        for f in files:
            if not isinstance(f, dict):
                continue
            path = f.get("path")
            if not isinstance(path, str):
                continue
            n = extract_migration_number(path)
            if n is not None:
                mig_numbers.add(n)
        prs.append(
            OtherPR(
                number=number,
                title=title,
                migration_numbers=sorted(mig_numbers),
            )
        )
    prs.sort(key=lambda p: p.number)
    return prs


# -----------------------------------------------------------------------------
# Core detection (pure — unit-tested)
# -----------------------------------------------------------------------------


def find_collisions(
    my_numbers: list[int],
    other_prs: list[OtherPR],
    current_pr_number: int | None,
) -> list[Collision]:
    """Return every (my_number, other_pr) where the other PR also claims
    that migration number.

    `current_pr_number` identifies the PR whose diff supplied `my_numbers`
    — it is excluded from `other_prs` so a PR never reports a collision
    with itself.

    Ordering: collisions are returned sorted by `(my_number, other_pr.number)`
    so CLI output is deterministic.
    """
    if not my_numbers:
        return []
    my_set = set(my_numbers)
    collisions: list[Collision] = []
    for pr in other_prs:
        if current_pr_number is not None and pr.number == current_pr_number:
            continue
        for n in pr.migration_numbers:
            if n in my_set:
                collisions.append(Collision(my_number=n, other_pr=pr))
    collisions.sort(key=lambda c: (c.my_number, c.other_pr.number))
    return collisions


def find_stale_numbers(
    my_numbers: list[int],
    latest_on_base: int | None,
) -> list[int]:
    """Bonus check — return migration numbers in `my_numbers` that are
    already <= the latest applied migration on `base`.

    This catches a stale long-lived branch whose migration number has
    been "used up" on main since the branch forked. Purely advisory —
    the caller warns but does not fail.
    """
    if latest_on_base is None:
        return []
    return sorted(n for n in my_numbers if n <= latest_on_base)


# -----------------------------------------------------------------------------
# I/O: git / gh / filesystem
# -----------------------------------------------------------------------------


def get_diff_from_git(base_ref: str, repo_root: Path) -> str:
    """Run `git diff <merge-base>...HEAD` and return the unified diff text."""
    mb = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "HEAD", base_ref],
        capture_output=True,
        text=True,
    )
    base = mb.stdout.strip() if (mb.returncode == 0 and mb.stdout.strip()) else base_ref
    result = subprocess.run(
        ["git", "-C", str(repo_root), "diff", f"{base}...HEAD", "--unified=0"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def get_latest_migration_number_on_base(base_ref: str, repo_root: Path) -> int | None:
    """List `packages/api/migrations/` on the base ref and return the
    highest migration number. Returns None if the listing is empty or on
    error."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-tree",
            "--name-only",
            f"{base_ref}:{MIGRATIONS_DIR}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    nums: list[int] = []
    for name in result.stdout.splitlines():
        m = re.match(r"^(\d+)_.+\.sql$", name.strip())
        if m:
            nums.append(int(m.group(1)))
    if not nums:
        return None
    return max(nums)


def get_open_prs_via_gh(repo: str | None) -> str:
    """Invoke `gh pr list --state open --json number,title,files`.

    Returns the raw JSON text. Raises RuntimeError on failure.
    """
    cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        "open",
        "--json",
        "number,title,files",
        "--limit",
        "200",
    ]
    if repo:
        cmd += ["--repo", repo]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr list failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def detect_current_pr_number(repo_root: Path) -> int | None:
    """Best-effort: if this commit is part of an open PR, return its number.

    In GitHub Actions' `pull_request` context the PR number is available
    via `GITHUB_REF` (`refs/pull/<N>/merge`). Otherwise we fall back to
    asking `gh` to resolve the current branch's PR. Returns None if no
    PR is associated with HEAD.
    """
    # GitHub Actions sets GITHUB_REF like refs/pull/1234/merge on PR runs.
    ref = os.environ.get("GITHUB_REF", "")
    m = re.match(r"^refs/pull/(\d+)/(merge|head)$", ref)
    if m:
        return int(m.group(1))

    # Fall back to `gh pr view --json number` — matches HEAD's branch to a PR.
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "number"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    n = data.get("number") if isinstance(data, dict) else None
    return n if isinstance(n, int) else None


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def format_collision(c: Collision) -> str:
    pr_url_hint = f"https://github.com/<owner>/<repo>/pull/{c.other_pr.number}"
    return (
        f"ERROR: migration number {c.my_number} is also claimed by PR #{c.other_pr.number}\n"
        f"  Title: {c.other_pr.title}\n"
        f"  Link:  {pr_url_hint}\n"
        f"\n"
        f"Remediation:\n"
        f"  1. git fetch origin main\n"
        f"  2. git rebase origin/main\n"
        f"  3. Inspect the latest migration number actually on main:\n"
        f"       ls packages/api/migrations/ | sort | tail -5\n"
        f"  4. Rename your migration file to the next available number.\n"
        f"  5. git add packages/api/migrations/ && git commit --amend\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Detect migration-number collisions between the current PR and "
            "other open PRs on the same repository."
        )
    )
    ap.add_argument(
        "--base",
        default="origin/main",
        help="Base ref to diff against (default: origin/main).",
    )
    ap.add_argument(
        "--repo",
        default=None,
        help="owner/repo passed to `gh pr list`; default: gh's inferred repo.",
    )
    ap.add_argument(
        "--current-pr",
        type=int,
        default=None,
        help=(
            "Current PR number (excluded from the open-PR comparison). Default: "
            "auto-detected from GITHUB_REF or `gh pr view`."
        ),
    )
    ap.add_argument(
        "--repo-root",
        default=None,
        help="Repo root (default: auto-detect from git rev-parse --show-toplevel).",
    )
    ap.add_argument(
        "--diff-file",
        default=None,
        help="Read unified diff from a file instead of invoking git (for tests).",
    )
    ap.add_argument(
        "--gh-pr-list-file",
        default=None,
        help="Read gh pr list JSON from a file instead of invoking gh (for tests).",
    )
    ap.add_argument(
        "--latest-base-migration",
        type=int,
        default=None,
        help=(
            "Override the latest-migration-on-base number used for the stale "
            "branch warning (for tests)."
        ),
    )
    args = ap.parse_args()

    # -------- Resolve repo root --------
    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            repo_root = Path(r.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"ERROR: cannot determine repo root: {exc}", file=sys.stderr)
            return 2

    # -------- Read diff --------
    if args.diff_file:
        try:
            diff_text = Path(args.diff_file).read_text()
        except OSError as exc:
            print(f"ERROR: cannot read diff file: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            diff_text = get_diff_from_git(args.base, repo_root)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    my_numbers = parse_added_migration_numbers_from_diff(diff_text)

    if not my_numbers:
        print(
            "check-migration-number-collision: no migration files touched in diff — OK."
        )
        return 0

    print(
        f"check-migration-number-collision: PR touches migration numbers: "
        f"{', '.join(str(n) for n in my_numbers)}"
    )

    # -------- Read open PRs --------
    if args.gh_pr_list_file:
        try:
            pr_list_text = Path(args.gh_pr_list_file).read_text()
        except OSError as exc:
            print(f"ERROR: cannot read gh pr list file: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            pr_list_text = get_open_prs_via_gh(args.repo)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except FileNotFoundError:
            print(
                "ERROR: `gh` CLI not found in PATH — cannot query open PRs.",
                file=sys.stderr,
            )
            return 2

    try:
        other_prs = parse_gh_pr_list(pr_list_text)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    current_pr = args.current_pr
    if current_pr is None:
        current_pr = detect_current_pr_number(repo_root)

    collisions = find_collisions(my_numbers, other_prs, current_pr)

    # -------- Stale-branch warning (advisory only) --------
    if args.latest_base_migration is not None:
        latest_on_base = args.latest_base_migration
    else:
        latest_on_base = get_latest_migration_number_on_base(args.base, repo_root)

    stale = find_stale_numbers(my_numbers, latest_on_base)
    if stale:
        print(
            f"WARNING: migration number(s) {', '.join(str(n) for n in stale)} are "
            f"<= the latest migration on `{args.base}` ({latest_on_base}). This "
            f"branch may be stale — rebase and renumber.",
            file=sys.stderr,
        )

    if not collisions:
        print("check-migration-number-collision: no collisions with open PRs.")
        return 0

    print("", file=sys.stderr)
    for c in collisions:
        print(format_collision(c), file=sys.stderr)
    print(
        "See https://github.com/judgemind/judgemind/issues/2916 for background.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
