#!/usr/bin/env python3
# _check_shipped_pr_overlap.py — Compute issue↔PR file overlap for
# scripts/check-shipped-pr.sh.
#
# Reads `gh pr view --json mergedAt,baseRefName,files` JSON on stdin. Reads
# the candidate-files list from CHECK_SHIPPED_CANDIDATE_FILES (comma-
# separated). Prints "<count>\t<comma-separated-overlap-paths>" on stdout,
# or empty stdout if the PR is not eligible (not merged, not on main).
#
# venv: none
# permanent: true
#
# Eligibility:
#   - mergedAt must be non-null (PR is actually merged, not just closed).
#   - baseRefName must be "main" (we only count main-branch landings).
#
# Overlap is path-prefix-friendly:
#   - Exact match counts (e.g. PR's "scripts/check-foo.sh" == candidate
#     "scripts/check-foo.sh").
#   - Candidate-as-prefix-of-PR-file counts (e.g. candidate "docs/" — but
#     extract_files filters out paths under 8 chars, so a bare "docs/"
#     wouldn't reach this stage; the prefix path is here for legit longer
#     prefixes like "docs/agent/" intentionally cited as the directory
#     scope).
#
# Anything else (only PR-as-prefix-of-candidate) does NOT count, because
# that direction is the wrong way around (the PR landed something narrower
# than the issue mentioned, which suggests the issue's broader work is
# still incomplete).

import json
import os
import sys


def compute_overlap(
    pr_files: list[dict], candidates: list[str]
) -> tuple[list[str], list[str]]:
    """Return (overlap_paths, added_overlap_paths) given pr_files vs candidates.

    `pr_files` items have shape {"path": "...", "additions": N, "deletions": M}
    when fetched via `gh pr view --json files`. `added_overlap_paths` is the
    subset of overlap_paths that the PR newly *created* — derived
    heuristically from `deletions == 0 && additions > 0`. (`gh pr view --json
    files` does not surface a discrete `status` field; the deletions==0
    heuristic is the most direct equivalent that also works against the raw
    GitHub API where `status: "added"` is the canonical signal.)

    These newly-created overlaps are dispositive — when the AC names a script
    that doesn't exist yet and a closed PR created exactly that script, the
    issue is unambiguously shipped.
    """
    pr_paths = {f.get("path") for f in pr_files if f.get("path")}
    added_paths: set[str] = set()
    for f in pr_files:
        path = f.get("path")
        if not path:
            continue
        # Either the explicit GitHub-API `status: "added"` field (when the
        # caller passes raw API output) or the gh-CLI heuristic (deletions
        # == 0 && additions > 0).
        if f.get("status") == "added":
            added_paths.add(path)
            continue
        deletions = f.get("deletions")
        additions = f.get("additions")
        if (
            isinstance(deletions, int)
            and deletions == 0
            and isinstance(additions, int)
            and additions > 0
        ):
            added_paths.add(path)
    overlap: list[str] = []
    added_overlap: list[str] = []
    for cand in candidates:
        match_path: str | None = None
        if cand in pr_paths:
            match_path = cand
        elif cand.endswith("/"):
            # Prefix match: candidate is a directory prefix and PR has a
            # file under it.
            for pp in pr_paths:
                if pp.startswith(cand):
                    match_path = cand
                    break
        if match_path is None:
            continue
        overlap.append(match_path)
        # Treat as `added` overlap if the candidate (or any path under a
        # directory candidate) was added by the PR.
        if cand in added_paths:
            added_overlap.append(cand)
        elif cand.endswith("/"):
            for ap in added_paths:
                if ap.startswith(cand):
                    added_overlap.append(cand)
                    break
    return overlap, added_overlap


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 1

    merged_at = data.get("mergedAt")
    base_ref = data.get("baseRefName")
    if not merged_at:
        # PR closed without merging — not a shipped match.
        return 0
    if base_ref != "main":
        # Merged onto a feature branch, not main.
        return 0

    # Date-ordering guard (#4353). A PR that merged BEFORE the issue was
    # filed cannot have shipped the issue's work — by definition. The
    # check is a string comparison: GitHub timestamps are ISO-8601 UTC
    # with a trailing `Z`, which is lexicographically sortable. When
    # CHECK_SHIPPED_ISSUE_CREATED_AT is unset / empty (e.g. the caller
    # could not extract it from the issue JSON), the guard is a no-op
    # — preserving the pre-#4353 fail-open behavior so downstream tests
    # whose mocks omit `createdAt` continue to work.
    issue_created_at = os.environ.get("CHECK_SHIPPED_ISSUE_CREATED_AT", "")
    if issue_created_at and merged_at < issue_created_at:
        # PR merged before the issue existed — incidental file overlap,
        # not a shipped match.
        return 0

    candidate_csv = os.environ.get("CHECK_SHIPPED_CANDIDATE_FILES", "")
    candidates = [c for c in candidate_csv.split(",") if c]
    if not candidates:
        return 0

    pr_files = data.get("files") or []
    overlap, added_overlap = compute_overlap(pr_files, candidates)
    if not overlap:
        return 0

    # High-confidence threshold: at least one ADDED overlap (PR created a
    # file the issue prescribed), OR ≥2 total overlaps. A single overlap
    # on a *modified* file is too weak — it routinely fires on adjacent
    # scripts that the issue cites as references rather than load-bearing
    # targets (e.g. issue #4204 references scripts/check-duplicate-pr.sh
    # only as the file it extends, and the PR that originally created
    # that script then trips a 1-file-modified false positive).
    if len(added_overlap) < 1 and len(overlap) < 2:
        return 0

    # Output format: <count>\t<comma-separated-overlap>\t<comma-separated-added>
    # The fix-CI / pivot path consumes this as best_overlap_count and
    # best_overlap_list; added_overlap is informational for the JSON
    # summary downstream.
    overlap_csv = ",".join(overlap)
    added_csv = ",".join(added_overlap)
    print(f"{len(overlap)}\t{overlap_csv}\t{added_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
