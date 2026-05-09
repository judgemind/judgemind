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
        # Three possible signals for "this file was newly added by the PR":
        #   1. ``status: "added"`` — raw GitHub REST API.
        #   2. ``changeType: "ADDED"`` — gh CLI's ``--json files``
        #      output (uppercased; equivalent to the REST API's ``status``).
        #   3. ``deletions == 0 && additions > 0`` — fallback heuristic
        #      for callers that pass neither of the above. This was the
        #      pre-#4340 default but mis-classifies large pure-additive
        #      *modifications* (e.g. PR #3552's
        #      ``scripts/rebuild_db.py +120 -0`` is a modification but
        #      reads as "added" to the heuristic). Only used when neither
        #      authoritative signal is present.
        if f.get("status") == "added":
            added_paths.add(path)
            continue
        change_type = f.get("changeType")
        if isinstance(change_type, str):
            # When the authoritative signal is present, trust it
            # exclusively — do NOT fall through to the deletions==0
            # heuristic (which would re-add modified files with
            # ``+N -0`` diffs, exactly the false-positive pattern that
            # tripped the original #4340 bug against PR #3552).
            if change_type.upper() == "ADDED":
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

    # Target-context candidate paths (#4340). The threshold below is
    # applied AGAINST THIS SUBSET ONLY — search-context paths
    # (Verify: / grep / pytest / aws / curl / rg invocation arguments)
    # cannot trip a shipped match by themselves. When the env var is
    # missing or empty the overlap helper falls back to treating ALL
    # candidates as target-context — preserving pre-#4340 behavior so
    # downstream callers that don't pass the new env var keep working.
    target_csv = os.environ.get("CHECK_SHIPPED_TARGET_FILES")
    if target_csv is None:
        targets = candidates
    else:
        targets = [c for c in target_csv.split(",") if c]

    pr_files = data.get("files") or []
    overlap, added_overlap = compute_overlap(pr_files, candidates)
    if not overlap:
        return 0

    # High-confidence threshold (target-context-aware as of #4340):
    #
    #   ≥1 ADDED overlap on a target-context path
    #   OR ≥2 total overlaps that are ALL on target-context paths
    #
    # Search-context overlaps (Verify-line-only file references) never
    # count toward the threshold by themselves. The pre-#4340 rule —
    # ≥1 added OR ≥2 total — is preserved over the target subset.
    target_set = set(targets)
    target_overlap = [p for p in overlap if p in target_set]
    target_added_overlap = [p for p in added_overlap if p in target_set]
    if len(target_added_overlap) < 1 and len(target_overlap) < 2:
        return 0

    # Audit-class tightening (#4223, refined #4501). When the issue is
    # classified as audit / investigation / refactor / migrate / extend /
    # tighten / harden / additional (see
    # `_check_shipped_pr_classify_issue.py`), apply a stricter threshold:
    # the candidate PR must show a creation-style signal on the
    # target-context overlap. This drops the canonical FP class for this
    # issue type — investigation issues that name a specific file as the
    # *subject of further work*, where some prior unrelated PR happened
    # to create or modify that same file.
    #
    # Rationale: an audit/investigation issue is by intent asking for
    # MORE work on an existing file. The work is legitimately distinct
    # from anything a prior PR shipped on the same file — even if that
    # prior PR added the file in the first place. The strict rule needs
    # at-least 2 target-context paths (#4353's date guard handles the
    # case where a creation commit predates the issue) AND a creation-
    # style signal.
    #
    # Refinement (#4501) — mixed ADDED+MODIFIED escape hatch.
    # The pre-#4501 rule required EVERY target-context overlap to be
    # ADDED, which over-penalized a sub-class the tightening should not
    # block: audit issues that prescribe BOTH (a) modifications to an
    # existing file AND (b) creation of a new file. PR #3319 ↔ #3310 is
    # exactly this shape — the issue prescribed a `main.tf` policy fix
    # AND a new smoke-test script, and the PR shipped both. With the
    # pre-#4501 strict rule, the modified `main.tf` overlap dropped the
    # legitimate match because not every overlap was ADDED.
    #
    # The refined rule: when ≥1 target-context overlap is ADDED, the
    # audit issue's intent is provably broader than "more work on an
    # existing file" — the candidate PR demonstrably created at least
    # one of the target files the issue prescribed, which is a strong
    # creation-style signal even when other overlaps are modifications.
    # Allow the match in that case. Drop only when EVERY target-context
    # overlap is a modification (the canonical FP — issue cites multiple
    # files in a directory, one prior PR refactored them).
    #
    # Tradeoffs (issue #4501 §Proposal option A, refining #4223 option 1):
    #   - false-negative cost is LOW — audit issues that ARE shipped
    #     get re-investigated cheaply (the worst case is one extra
    #     /task cycle, not a wrong-fix-merged regression).
    #   - false-positive cost is HIGH — without this guard the daemon
    #     auto-closes legitimate `agent/ready` audit issues whenever
    #     any prior PR touched the same file, which is structurally
    #     guaranteed to happen on a healthy codebase.
    #   - the refinement preserves both bounds. The all-modifications
    #     drop still catches the canonical FP. The ≥1-added relaxation
    #     re-opens legitimate matches like #3310 ↔ #3319 where the
    #     issue's intent encompasses both modification and creation.
    #
    # When CHECK_SHIPPED_AUDIT_CLASS is unset / empty / "0", this guard
    # is a no-op — preserving pre-#4223 behavior so downstream tests
    # whose mocks omit the new env var continue to work.
    audit_class = os.environ.get("CHECK_SHIPPED_AUDIT_CLASS", "")
    if audit_class and audit_class != "0":
        # Tightened rule (per #4223 option 1, refined per #4501 option A):
        #
        #   total target-context overlap ≥ 2  AND
        #   ≥1 target-context overlap is ADDED
        #
        # The "≥2" half drops single-file overlaps regardless of class
        # (the most common FP shape — issue cites one file, one prior
        # PR touched it). The "≥1 ADDED" half drops 2+ overlaps where
        # NONE are added (the second-most-common FP — issue cites
        # multiple files in a directory, one prior PR refactored them
        # without creating any new file).
        if len(target_overlap) < 2:
            return 0
        if not target_added_overlap:
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
