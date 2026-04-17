#!/usr/bin/env python3
# venv: none
"""
check-ci-job-skipped.py — Detect CI jobs whose body was modified in a diff
but would be SKIPPED on that diff's CI run because the paths-filter gate
doesn't match any changed file.

Motivation (#2527): When a PR modifies a job in `.github/workflows/ci.yml`
that has an `if: needs.detect-changes.outputs.X == 'true'` gate on a paths
filter, the job may be SKIPPED on that PR's CI run — so the changes to the
job definition are never actually exercised. The author sees green CI and
merges with confidence, but the modification is only tested on some later
PR that happens to touch a file the filter matches.

This has recurred at least twice:

- #2410: added a new test job gated on `hooks` filter; the filter did not
  include `.github/workflows/ci.yml` itself, so edits to the job body
  were not exercised on the PR that introduced them.
- #2505: replaced 21 explicit `run:` steps in `scripts-tests` with a
  single auto-discovery loop. The `scripts` filter did not include
  `.github/workflows/ci.yml`, so the first commit never exercised
  the new loop; the fix was a follow-up commit to add ci.yml to
  the filter.

This check detects that class of silent failure BEFORE the push:

1. Parse `.github/workflows/ci.yml` to extract:
   - The dorny/paths-filter filter block (filter-name → globs).
   - Each job's body line range and its `if:` gate (if any).

2. `git diff` against the merge base with origin/main to get:
   - The set of changed files.
   - The line ranges changed inside ci.yml itself.

3. For each gated job whose body has modified lines:
   - Look up the filter's glob list.
   - If NO changed file matches any glob in that list, FAIL with
     an actionable message.

Usage:
    scripts/check-ci-job-skipped.py                  # Diff against origin/main
    scripts/check-ci-job-skipped.py --base main      # Diff against a specific base
    scripts/check-ci-job-skipped.py --ci-yml PATH    # Override ci.yml location
    scripts/check-ci-job-skipped.py --diff-file FILE # Read unified diff from a file
                                                     # (for testing without git)
    scripts/check-ci-job-skipped.py --help

Exit codes:
    0 — No modified-but-would-be-skipped jobs detected.
    1 — One or more modified jobs would be skipped on their own PR.
    2 — Script error (cannot parse ci.yml, cannot run git, etc.).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# -----------------------------------------------------------------------------
# Glob matching
# -----------------------------------------------------------------------------
# dorny/paths-filter uses picomatch glob syntax. We only need to support the
# subset actually used in ci.yml:
#
#   - `packages/foo/**`          - recursive descendants
#   - `scripts/*.py`             - files directly in scripts/
#   - `.github/workflows/ci.yml` - exact path
#   - `infra/**`                 - recursive
#
# We compile each glob to a regex. Rules:
#   - `**` matches any sequence (including slashes, including empty).
#   - `*`  matches any characters except `/`.
#   - `?`  matches a single character except `/`.
#   - Other chars are literal (escaped where needed for regex).


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Convert a picomatch-ish glob to a compiled regex anchored to full path."""
    # Strip surrounding quotes if present
    glob = glob.strip().strip("'\"")

    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            # Look for `**` (optionally followed by `/`)
            if i + 1 < n and glob[i + 1] == "*":
                # `**/` matches any sequence of dirs including none
                if i + 2 < n and glob[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                # `/**` at end or elsewhere: match anything (including empty).
                out.append(".*")
                i += 2
                continue
            # Single `*`: match anything except slash.
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in r".+^$()[]{}|\\":
            out.append(re.escape(c))
            i += 1
        else:
            out.append(re.escape(c))
            i += 1

    pattern = "^" + "".join(out) + "$"
    return re.compile(pattern)


def path_matches_any(path: str, compiled_globs: list[re.Pattern[str]]) -> bool:
    """Return True if path matches at least one compiled glob."""
    for pat in compiled_globs:
        if pat.match(path):
            return True
    return False


# -----------------------------------------------------------------------------
# ci.yml parsing
# -----------------------------------------------------------------------------


@dataclass
class Job:
    """A top-level job in the `jobs:` block of ci.yml."""

    name: str
    start_line: int  # 1-indexed, line of the `<name>:` header
    end_line: int  # 1-indexed, last line of the job body
    if_gate_filter: str | None  # e.g. "scripts" if gated on detect-changes.outputs.scripts


@dataclass
class CIYaml:
    filters: dict[str, list[str]]  # filter-name -> list of glob strings
    jobs: list[Job]


# detect-changes `if` gate pattern:
#   if: needs.detect-changes.outputs.scripts == 'true'
#   if: needs.detect-changes.outputs.scraper == 'true' || needs.detect-changes.outputs.nlp == 'true'
IF_OUTPUT_RE = re.compile(r"needs\.detect-changes\.outputs\.([A-Za-z0-9_\-]+)")


def parse_ci_yaml(ci_yml_path: Path) -> CIYaml:
    """Parse ci.yml into a CIYaml structure.

    This is a minimal line-oriented parser tailored to the ci.yml structure
    we actually have — NOT a general YAML parser. It assumes:
      - 2-space indentation
      - top-level `jobs:` block with each job at indent level 2
      - `detect-changes` job contains a `filters: |` folded scalar
    """
    lines = ci_yml_path.read_text().splitlines()

    # ------- Step 1: find `jobs:` block -------
    jobs_start = None
    for i, line in enumerate(lines):
        if re.match(r"^jobs:\s*$", line):
            jobs_start = i + 1  # First line of jobs block
            break
    if jobs_start is None:
        raise ValueError("Could not find 'jobs:' block in ci.yml")

    # ------- Step 2: find each job at indent level 2 -------
    # A job header line matches: `  <name>:` with exactly 2 spaces.
    job_header_re = re.compile(r"^  ([A-Za-z0-9_\-]+):\s*$")
    job_headers: list[tuple[str, int]] = []  # (name, 0-indexed line)
    for i in range(jobs_start, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        # If the line is at indent level 0 (non-empty, non-indent, non-comment),
        # we've left the jobs block.
        if line and not line.startswith(" ") and not line.startswith("#"):
            break
        m = job_header_re.match(line)
        if m:
            job_headers.append((m.group(1), i))

    # ------- Step 3: for each job, find end line and `if:` gate -------
    jobs: list[Job] = []
    for idx, (name, start_i) in enumerate(job_headers):
        # End is the line before the next job header (or EOF/end-of-jobs).
        if idx + 1 < len(job_headers):
            end_i = job_headers[idx + 1][1] - 1
        else:
            end_i = len(lines) - 1
            for j in range(start_i + 1, len(lines)):
                ln = lines[j]
                if ln and not ln.startswith(" ") and not ln.startswith("#"):
                    end_i = j - 1
                    break

        # Find `if:` gate inside this job body at indent 4 (direct child).
        # Collect ALL outputs.X names in the expression so multi-filter
        # gates (`A == 'true' || B == 'true'`) are handled.
        if_filter: str | None = None
        for j in range(start_i + 1, end_i + 1):
            ln = lines[j]
            m = re.match(r"^    if:\s*(.+)$", ln)
            if m:
                expr = m.group(1)
                matches = IF_OUTPUT_RE.findall(expr)
                if matches:
                    # Join multi-filter names with `|` as a compact key.
                    # Downstream logic checks ALL referenced filters (OR).
                    if_filter = "|".join(matches)
                break  # first `if:` wins; jobs only have one

        jobs.append(
            Job(
                name=name,
                start_line=start_i + 1,  # 1-indexed
                end_line=end_i + 1,  # 1-indexed
                if_gate_filter=if_filter,
            )
        )

    # ------- Step 4: extract filters from detect-changes job -------
    filters = _extract_filters(lines, jobs)

    return CIYaml(filters=filters, jobs=jobs)


def _extract_filters(lines: list[str], jobs: list[Job]) -> dict[str, list[str]]:
    """Extract the paths-filter `filters: |` block from detect-changes."""
    dc_job = next((j for j in jobs if j.name == "detect-changes"), None)
    if dc_job is None:
        return {}

    # Find `filters: |` within the detect-changes body.
    filters_start: int | None = None
    filters_indent: int | None = None
    for i in range(dc_job.start_line - 1, dc_job.end_line):
        m = re.match(r"^(\s+)filters:\s*\|\s*$", lines[i])
        if m:
            filters_start = i + 1
            filters_indent = len(m.group(1))
            break
    if filters_start is None or filters_indent is None:
        return {}

    # Determine the filter-name indent (first non-blank, non-comment line).
    filter_name_indent: int | None = None
    for i in range(filters_start, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        if ln.lstrip().startswith("#"):
            continue
        leading = len(ln) - len(ln.lstrip(" "))
        if leading <= filters_indent:
            break  # left the filters: | block
        filter_name_indent = leading
        break

    if filter_name_indent is None:
        return {}

    filter_name_re = re.compile(
        rf"^ {{{filter_name_indent}}}([A-Za-z0-9_\-]+):\s*$"
    )
    # Glob entries are lines more deeply indented than the filter name,
    # starting with `- '...'`.
    glob_entry_re = re.compile(
        rf"^ {{{filter_name_indent + 2},}}-\s*(.+?)\s*$"
    )

    filters: dict[str, list[str]] = {}
    current_filter: str | None = None

    for i in range(filters_start, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        # Comment lines inside the block — skip.
        if ln.lstrip().startswith("#"):
            continue
        leading = len(ln) - len(ln.lstrip(" "))
        if leading <= filters_indent:
            break  # out of filters: | block

        m_name = filter_name_re.match(ln)
        if m_name:
            current_filter = m_name.group(1)
            filters[current_filter] = []
            continue

        m_glob = glob_entry_re.match(ln)
        if m_glob and current_filter is not None:
            glob = m_glob.group(1).strip()
            # Strip trailing `# comment` if any
            if "#" in glob:
                # Only strip if `#` is outside quotes (simple heuristic)
                quote_char = None
                for ci_idx, ch in enumerate(glob):
                    if ch in ("'", '"'):
                        if quote_char is None:
                            quote_char = ch
                        elif quote_char == ch:
                            quote_char = None
                    elif ch == "#" and quote_char is None:
                        glob = glob[:ci_idx].rstrip()
                        break
            # Strip surrounding quotes
            if (glob.startswith("'") and glob.endswith("'")) or (
                glob.startswith('"') and glob.endswith('"')
            ):
                glob = glob[1:-1]
            if glob:
                filters[current_filter].append(glob)
            continue

    return filters


# -----------------------------------------------------------------------------
# Git diff parsing
# -----------------------------------------------------------------------------


@dataclass
class DiffResult:
    changed_files: list[str]
    # For ci.yml specifically: the set of line numbers touched on the NEW side.
    ci_yml_changed_lines: set[int]


HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(diff_text: str, ci_yml_rel_path: str) -> DiffResult:
    """Parse a unified diff and extract changed files + ci.yml modified lines.

    We consider a line modified on the NEW side if it is a `+` line. We
    also mark nearby new-side lines when a hunk contains `-` (deletion)
    lines, so that pure-deletion edits inside a job body are still
    detected as a modification to that job.
    """
    changed_files: list[str] = []
    ci_lines: set[int] = set()

    lines = diff_text.splitlines()
    i = 0
    current_file: str | None = None
    while i < len(lines):
        line = lines[i]
        # File header: `diff --git a/path b/path`
        if line.startswith("diff --git "):
            parts = line.split(" ")
            new_path_token = parts[-1]
            if new_path_token.startswith("b/"):
                current_file = new_path_token[2:]
                if current_file not in changed_files:
                    changed_files.append(current_file)
            else:
                current_file = None
            i += 1
            continue

        # Handle `+++ b/<path>` as a fallback file header
        m_plus = re.match(r"^\+\+\+ b/(.+)$", line)
        if m_plus:
            current_file = m_plus.group(1)
            if current_file and current_file not in changed_files:
                changed_files.append(current_file)
            i += 1
            continue

        if line.startswith("--- ") or line.startswith("+++ "):
            i += 1
            continue

        m_hunk = HUNK_HEADER_RE.match(line)
        if m_hunk and current_file == ci_yml_rel_path:
            new_start = int(m_hunk.group(1))
            hunk_has_deletion = False
            j = i + 1
            hunk_new_line_numbers: list[int] = []
            running_new_line = new_start
            while j < len(lines):
                hl = lines[j]
                if hl.startswith("@@ ") or hl.startswith("diff --git "):
                    break
                if hl.startswith("+"):
                    ci_lines.add(running_new_line)
                    hunk_new_line_numbers.append(running_new_line)
                    running_new_line += 1
                elif hl.startswith("-"):
                    hunk_has_deletion = True
                elif hl.startswith(" "):
                    hunk_new_line_numbers.append(running_new_line)
                    running_new_line += 1
                elif hl.startswith("\\"):
                    # `\ No newline at end of file`
                    pass
                else:
                    break
                j += 1

            if hunk_has_deletion and hunk_new_line_numbers:
                # Mark the new-side range of the hunk as changed so that
                # pure deletions inside a job body are still detected.
                for ln in hunk_new_line_numbers:
                    ci_lines.add(ln)
                # Also mark new_start for pure-deletion hunks with no +/context.
                ci_lines.add(new_start)

            i = j
            continue

        i += 1

    return DiffResult(changed_files=changed_files, ci_yml_changed_lines=ci_lines)


def get_diff_from_git(base_ref: str, repo_root: Path) -> str:
    """Run `git diff` against the merge-base of `base_ref`."""
    mb = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "HEAD", base_ref],
        capture_output=True,
        text=True,
    )
    if mb.returncode == 0 and mb.stdout.strip():
        base = mb.stdout.strip()
    else:
        base = base_ref

    result = subprocess.run(
        ["git", "-C", str(repo_root), "diff", f"{base}...HEAD", "--unified=3"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


# -----------------------------------------------------------------------------
# Main check logic
# -----------------------------------------------------------------------------


@dataclass
class SkippedJobProblem:
    job_name: str
    filter_name: str  # may be "a|b" for multi-filter gates
    filter_globs: list[str]


def find_skipped_modified_jobs(
    ci: CIYaml,
    changed_files: list[str],
    ci_yml_changed_lines: set[int],
) -> list[SkippedJobProblem]:
    """Return the list of jobs whose body was modified but would be skipped.

    A job is "modified" if any line in its body (inclusive of start_line and
    end_line) is in ci_yml_changed_lines.

    A job would be "skipped on its own PR" if it has an if-gate on a
    detect-changes filter AND no changed file in the diff matches any glob
    in any of the referenced filters' glob lists.
    """
    problems: list[SkippedJobProblem] = []
    for job in ci.jobs:
        if job.if_gate_filter is None:
            continue
        if job.name == "detect-changes":
            continue

        body_modified = any(
            job.start_line <= ln <= job.end_line for ln in ci_yml_changed_lines
        )
        if not body_modified:
            continue

        filter_names = job.if_gate_filter.split("|")
        all_globs: list[str] = []
        for fname in filter_names:
            globs = ci.filters.get(fname, [])
            all_globs.extend(globs)

        # Unknown filter (not in detect-changes) — skip silently.
        if not all_globs:
            continue

        compiled = [glob_to_regex(g) for g in all_globs]

        any_match = any(path_matches_any(f, compiled) for f in changed_files)
        if any_match:
            continue

        problems.append(
            SkippedJobProblem(
                job_name=job.name,
                filter_name=job.if_gate_filter,
                filter_globs=all_globs,
            )
        )
    return problems


def format_problem(p: SkippedJobProblem) -> str:
    globs_joined = ", ".join(p.filter_globs)
    if "|" in p.filter_name:
        filter_label = f"filters `{p.filter_name.replace('|', '`, `')}`"
    else:
        filter_label = f"filter `{p.filter_name}`"
    return (
        f"Job `{p.job_name}` is gated on {filter_label}.\n"
        f"  Your diff modifies `{p.job_name}` in .github/workflows/ci.yml, but\n"
        f"  NO file in the diff matches any glob in that filter's path list.\n"
        f"  As a result, `{p.job_name}` will be SKIPPED on this PR's CI run,\n"
        f"  and your changes to that job body will not actually be exercised.\n"
        f"\n"
        f"  Filter globs: {globs_joined}\n"
        f"\n"
        f"  Fix: add `.github/workflows/ci.yml` to the filter's path list in\n"
        f"  `.github/workflows/ci.yml` (under `detect-changes` → `filters:`),\n"
        f"  or modify a file that already matches one of those globs."
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Detect CI jobs whose body was modified but would be skipped on the PR."
    )
    ap.add_argument(
        "--base",
        default="origin/main",
        help="Base ref to diff against (default: origin/main).",
    )
    ap.add_argument(
        "--ci-yml",
        default=".github/workflows/ci.yml",
        help="Path to ci.yml, relative to repo root.",
    )
    ap.add_argument(
        "--repo-root",
        default=None,
        help="Repo root (default: auto-detect from git).",
    )
    ap.add_argument(
        "--diff-file",
        default=None,
        help="Read unified diff from a file instead of invoking git (for tests).",
    )
    args = ap.parse_args()

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

    ci_yml_path = repo_root / args.ci_yml
    if not ci_yml_path.is_file():
        print(f"ERROR: ci.yml not found at {ci_yml_path}", file=sys.stderr)
        return 2

    try:
        ci = parse_ci_yaml(ci_yml_path)
    except Exception as exc:
        print(f"ERROR: failed to parse ci.yml: {exc}", file=sys.stderr)
        return 2

    if args.diff_file:
        diff_path = Path(args.diff_file)
        if not diff_path.is_file():
            print(f"ERROR: diff file not found: {diff_path}", file=sys.stderr)
            return 2
        diff_text = diff_path.read_text()
    else:
        try:
            diff_text = get_diff_from_git(args.base, repo_root)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    diff = parse_unified_diff(diff_text, args.ci_yml)

    problems = find_skipped_modified_jobs(
        ci, diff.changed_files, diff.ci_yml_changed_lines
    )

    if not problems:
        return 0

    print(
        "check-ci-job-skipped: one or more modified jobs would be SKIPPED",
        file=sys.stderr,
    )
    print("on this PR's CI run:", file=sys.stderr)
    print("", file=sys.stderr)
    for p in problems:
        print(format_problem(p), file=sys.stderr)
        print("", file=sys.stderr)
    print(
        "See https://github.com/judgemind/judgemind/issues/2527 for background.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
