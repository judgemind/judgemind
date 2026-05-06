#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-ci-passed-coverage.py — Assert `ci-passed.needs:` and the top-level
`jobs:` block in `.github/workflows/ci.yml` agree in both directions, so
gaps like #3919 (missing entry) and #2832 (stale/renamed entry) are caught
before merge.

Motivation: The `ci-passed` aggregator job is the sole required status
check for branch protection. Two ways the array can fall out of sync with
the real job set, both of which this script flags:

  1. Missing entry (#3919): a job is defined in ci.yml but absent from
     `ci-passed.needs:`. CI can be green on a PR even when that job fails,
     giving a false green signal. The original failure mode this guard
     was created for.

  2. Stale entry (#2832): `ci-passed.needs:` references a job name that
     no longer exists as a top-level `jobs:` entry — typically because a
     job was renamed or deleted but the array was not updated. actionlint
     also flags this at push time with `job "ci-passed" needs job "X"
     which does not exist in this workflow [job-needs]`, but actionlint
     runs after most other pre-push hooks, so a targeted check at the
     same hygiene layer that already enforces direction (1) gives a
     faster, more specific error.

This script:
1. Parses `.github/workflows/ci.yml` (simple line-oriented parser).
2. Enumerates every top-level job name.
3. Reads the `ci-passed.needs:` array.
4. Asserts every non-allow-listed job is present (direction 1 — missing).
5. Asserts every entry in `needs:` corresponds to a real top-level job
   (direction 2 — stale / renamed / removed / nonexistent).

Allow-list (jobs that legitimately do not need to be in ci-passed.needs:):
  - detect-changes       (orchestrator; always succeeds)
  - ci-passed            (the aggregator itself)
  - migration-notice     (informational PR comment; no gate)

Note: ci-passed-coverage-check (this guard) is NOT in the allow-list — it IS
wired into ci-passed.needs: by design, so it is covered by the check itself.

Usage:
    scripts/check-ci-passed-coverage.py                  # Use repo-root ci.yml
    scripts/check-ci-passed-coverage.py --ci-yml PATH    # Override ci.yml location
    scripts/check-ci-passed-coverage.py --help

Exit codes:
    0 — `ci-passed.needs:` and the jobs block agree in both directions.
    1 — One or more jobs are missing from ci-passed.needs:, OR
        one or more entries in ci-passed.needs: do not correspond to a
        real top-level job (stale / renamed / removed).
    2 — Script error (cannot parse ci.yml, etc.).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


# Jobs that are intentionally excluded from the ci-passed.needs: requirement.
ALLOW_LIST: frozenset[str] = frozenset(
    [
        "detect-changes",  # orchestrator; always succeeds, not a gate
        "ci-passed",  # the aggregator itself
        "migration-notice",  # informational PR comment; no gate
    ]
)


def find_repo_root() -> Path:
    """Auto-detect the repo root via git."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return Path(r.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"cannot determine repo root: {exc}") from exc


def parse_job_names(lines: list[str]) -> list[str]:
    """Return all top-level job names from the jobs: block."""
    jobs_start: int | None = None
    for i, line in enumerate(lines):
        if re.match(r"^jobs:\s*$", line):
            jobs_start = i + 1
            break
    if jobs_start is None:
        raise ValueError("Could not find 'jobs:' block in ci.yml")

    job_header_re = re.compile(r"^  ([A-Za-z0-9_\-]+):\s*$")
    job_names: list[str] = []
    for line in lines[jobs_start:]:
        if not line.strip():
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            break
        m = job_header_re.match(line)
        if m:
            job_names.append(m.group(1))
    return job_names


def parse_ci_passed_needs(lines: list[str]) -> list[str]:
    """Return the list of job names in ci-passed.needs:."""
    # Find the ci-passed: job header.
    ci_passed_line: int | None = None
    for i, line in enumerate(lines):
        if re.match(r"^  ci-passed:\s*$", line):
            ci_passed_line = i
            break
    if ci_passed_line is None:
        raise ValueError("Could not find 'ci-passed:' job in ci.yml")

    # Look for `needs: [...]` within the next ~5 lines of the job body.
    needs_re = re.compile(r"^\s+needs:\s*\[(.+)\]")
    for line in lines[ci_passed_line + 1 : ci_passed_line + 10]:
        m = needs_re.match(line)
        if m:
            raw = m.group(1)
            return [name.strip() for name in raw.split(",")]
    raise ValueError("Could not find 'needs: [...]' in ci-passed job body")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Assert every top-level CI job is listed in ci-passed.needs:."
    )
    ap.add_argument(
        "--ci-yml",
        default=None,
        help="Path to ci.yml (absolute or relative to cwd). Default: auto-detect.",
    )
    args = ap.parse_args()

    if args.ci_yml:
        ci_yml_path = Path(args.ci_yml).resolve()
    else:
        try:
            repo_root = find_repo_root()
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        ci_yml_path = repo_root / ".github" / "workflows" / "ci.yml"

    if not ci_yml_path.is_file():
        print(f"ERROR: ci.yml not found at {ci_yml_path}", file=sys.stderr)
        return 2

    lines = ci_yml_path.read_text(encoding="utf-8").splitlines()

    try:
        all_jobs = parse_job_names(lines)
    except ValueError as exc:
        print(f"ERROR: failed to parse job names: {exc}", file=sys.stderr)
        return 2

    try:
        needs = parse_ci_passed_needs(lines)
    except ValueError as exc:
        print(f"ERROR: failed to parse ci-passed.needs: {exc}", file=sys.stderr)
        return 2

    job_set = set(all_jobs)
    needs_set = set(needs)

    # Direction 1 (#3919): every real job must appear in ci-passed.needs:.
    missing: list[str] = []
    for job in all_jobs:
        if job in ALLOW_LIST:
            continue
        if job not in needs_set:
            missing.append(job)

    # Direction 2 (#2832): every entry in ci-passed.needs: must correspond
    # to a real top-level job. A stale entry indicates a job was renamed or
    # removed but the array was not updated.
    stale: list[str] = []
    for entry in needs:
        if entry not in job_set:
            stale.append(entry)

    if not missing and not stale:
        print(f"check-ci-passed-coverage: all {len(all_jobs)} jobs covered.")
        return 0

    if missing:
        print(
            "check-ci-passed-coverage: the following jobs are NOT listed in "
            "ci-passed.needs: and will not block merge if they fail:",
            file=sys.stderr,
        )
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print(
            "\nFix: add the missing job name(s) to the ci-passed.needs: array in "
            ".github/workflows/ci.yml.",
            file=sys.stderr,
        )

    if stale:
        if missing:
            print("", file=sys.stderr)
        print(
            "check-ci-passed-coverage: the following entries in "
            "ci-passed.needs: do not correspond to any top-level job in "
            "ci.yml (stale / renamed / removed):",
            file=sys.stderr,
        )
        for name in stale:
            print(f"  - {name}", file=sys.stderr)
        print(
            "\nFix: remove the stale entry from the ci-passed.needs: array, "
            "or update it to the current job name.",
            file=sys.stderr,
        )

    print(
        "\nSee https://github.com/judgemind/judgemind/issues/3919 (missing) "
        "and #2832 (stale) for background.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
