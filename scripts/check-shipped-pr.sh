#!/usr/bin/env bash
# check-shipped-pr.sh — Detect already-shipped issues before claiming.
#
# venv: none
# permanent: true
#
# Companion to scripts/check-duplicate-pr.sh. The duplicate-PR check finds
# *open* PRs; this one finds *merged-but-unclosed* PRs whose code already
# shipped the issue's work. It exists because pre-#3994 PRs sometimes landed
# without a `Closes #N` keyword (placeholder titles like "WIP: ralph
# output", null bodies, or operator-edited titles), so the GitHub auto-close
# never fired and the originating issue stayed `agent/ready` indefinitely.
# Result: agents pay the full claim + worktree-setup + ralph cycle on a
# task whose code shipped weeks ago. See #4204 (this issue) and #2831 (the
# canonical zombie that triggered the work).
#
# Algorithm:
#   1. Fetch the issue body via `gh issue view`.
#   2. Extract candidate file paths from the body using a fixed regex
#      that covers the four conventional repo roots
#      (scripts/, packages/, docs/, infra/) plus `.github/`.
#   3. For each unique file path, query the commits API on `main` to find
#      commits that touched it (`gh api /repos/.../commits?path=<file>`),
#      and parse the squash-merge PR number from each commit headline
#      (the trailing `(#N)` token).
#   4. For each candidate PR, fetch its merge metadata and changed files
#      via `gh pr view --json mergedAt,baseRefName,files`. Drop the
#      candidate if `mergedAt` is null or `baseRefName != main`.
#   5. Compute overlap of the candidate PR's changed files vs the file
#      paths extracted from the issue body. Treat ≥1 file overlap as a
#      high-confidence shipped match.
#   6. On match: print a `shipped:` line and a JSON summary to stdout,
#      exit 0. On no match: print a `not-shipped:` line, exit 1. On
#      error: print an `error:` line to stderr, exit 2.
#
# Usage:
#   scripts/check-shipped-pr.sh <issue_number>
#   scripts/check-shipped-pr.sh 2831
#   scripts/check-shipped-pr.sh '#2831'         # leading # stripped
#
# Environment variables (testing hooks):
#   CHECK_SHIPPED_REPO     — override "judgemind/judgemind" (for tests)
#   CHECK_SHIPPED_GH_BIN   — override "gh" binary path (for tests)
#
# Exit codes:
#   0 — High-confidence shipped match. A `shipped:` line and a JSON
#       summary are printed to stdout. Caller pivots to the
#       "verify and close" path documented in .claude/skills/task/
#       SKILL.md Step 4a.
#   1 — No high-confidence match. A `not-shipped:` line is printed to
#       stdout. Caller proceeds with normal /task flow.
#   2 — Error (missing argument, gh CLI unavailable, API failure). An
#       `error:` line is printed to stderr.

set -uo pipefail

REPO="${CHECK_SHIPPED_REPO:-judgemind/judgemind}"
GH_BIN="${CHECK_SHIPPED_GH_BIN:-gh}"

# ─── Argument parsing ──────────────────────────────────────────────────────

issue_arg="${1:-}"
if [[ -z "$issue_arg" ]]; then
    echo "error: check-shipped-pr.sh requires an issue number argument (exit 2)" >&2
    echo "  usage: scripts/check-shipped-pr.sh <issue_number>" >&2
    exit 2
fi

issue_num="${issue_arg#\#}"
if ! [[ "$issue_num" =~ ^[0-9]+$ ]]; then
    echo "error: '$issue_arg' is not a valid issue number (exit 2)" >&2
    exit 2
fi

# ─── gh CLI availability ───────────────────────────────────────────────────

if ! command -v "$GH_BIN" >/dev/null 2>&1; then
    echo "error: '$GH_BIN' CLI not found on PATH — cannot check shipped PRs (exit 2)" >&2
    exit 2
fi

# ─── Fetch issue body ──────────────────────────────────────────────────────

issue_json=""
if ! issue_json=$("$GH_BIN" issue view "$issue_num" --repo "$REPO" --json body,title,createdAt 2>/dev/null); then
    echo "error: failed to fetch issue #${issue_num} from ${REPO} (exit 2)" >&2
    exit 2
fi

# Extract candidate file paths from the issue body via Python (so the regex
# dialect is portable across BSD vs GNU grep).
candidate_files=""
if ! candidate_files=$(printf '%s' "$issue_json" | python3 \
    "$(dirname "${BASH_SOURCE[0]}")/_check_shipped_pr_extract_files.py" \
    2>/dev/null); then
    echo "error: failed to extract candidate file paths from issue #${issue_num} (exit 2)" >&2
    exit 2
fi

# Extract the issue's createdAt timestamp for the date-ordering guard
# (#4353). The helper exits 0 unconditionally and emits an empty string
# when the field is absent / null / malformed, so the guard fails open
# (no date check applied) when the data is unavailable. The downstream
# overlap helper uses CHECK_SHIPPED_ISSUE_CREATED_AT to skip candidate
# PRs whose `mergedAt` precedes the issue's `createdAt` — a PR that
# merged before the issue existed cannot have shipped its work.
issue_created_at=""
issue_created_at=$(printf '%s' "$issue_json" | python3 \
    "$(dirname "${BASH_SOURCE[0]}")/_check_shipped_pr_extract_created_at.py" \
    2>/dev/null) || issue_created_at=""

if [[ -z "$candidate_files" ]]; then
    echo "not-shipped: no candidate file paths in issue #${issue_num} body (exit 1)"
    exit 1
fi

# ─── Find candidate PRs that touched any of those files ────────────────────

# Collect (PR number, comma-separated unique). For each file path, list
# recent commits on main; parse EVERY `(#N)` token from each headline
# via the Python helper (bash regex match captures only the first
# token, which silently drops the squash-merge PR when the conventional-
# commits subject already references a closed-by issue, e.g.
# `fix(ci): squash-merge fix (#2837) (#3170)` — see issue #4214).
candidate_prs=""
while IFS= read -r file_path; do
    [[ -z "$file_path" ]] && continue
    # gh api may fail for non-existent paths — that's expected, just skip.
    commits_json=""
    if ! commits_json=$("$GH_BIN" api \
        "/repos/${REPO}/commits?path=${file_path}&per_page=30" \
        --jq '.[] | .commit.message' \
        2>/dev/null); then
        continue
    fi
    # Extract EVERY `(#N)` token across all headlines via the Python
    # helper. The downstream vetting loop (lines below) filters out
    # issue numbers and unknown PRs via `gh pr view --json mergedAt`
    # (returns null for non-PR numbers).
    pr_nums=""
    if ! pr_nums=$(printf '%s\n' "$commits_json" | python3 \
        "$(dirname "${BASH_SOURCE[0]}")/_check_shipped_pr_extract_pr_nums.py" \
        2>/dev/null); then
        continue
    fi
    while IFS= read -r pr_num; do
        [[ -z "$pr_num" ]] && continue
        # Append if not already present
        if [[ ",${candidate_prs}," != *",${pr_num},"* ]]; then
            if [[ -z "$candidate_prs" ]]; then
                candidate_prs="$pr_num"
            else
                candidate_prs="${candidate_prs},${pr_num}"
            fi
        fi
    done <<< "$pr_nums"
done <<< "$candidate_files"

if [[ -z "$candidate_prs" ]]; then
    echo "not-shipped: no candidate PRs found for issue #${issue_num} files (exit 1)"
    exit 1
fi

# ─── Vet each candidate PR for high-confidence match ───────────────────────

best_pr=""
best_overlap_count=0
best_overlap_list=""
best_added_list=""

# Convert candidate_files (newline-separated) into a comma-list for the
# overlap helper.
candidate_files_csv=$(printf '%s' "$candidate_files" | tr '\n' ',' | sed 's/,$//')

# Iterate candidate PRs (comma-separated)
IFS=',' read -ra prs_array <<< "$candidate_prs"
for pr_num in "${prs_array[@]}"; do
    [[ -z "$pr_num" ]] && continue
    pr_json=""
    if ! pr_json=$("$GH_BIN" pr view "$pr_num" --repo "$REPO" \
        --json number,title,body,mergedAt,baseRefName,files \
        2>/dev/null); then
        continue
    fi

    # Closes-keyword filter (#4327). If the PR body contains a closing
    # keyword (`Closes #N` / `Fixes #N` / `Resolves #N`, case-insensitive,
    # all 9 GitHub verb forms) that names ONLY issues other than the one
    # we're checking, skip this candidate — the PR shipped a different
    # issue's work, and the file overlap is incidental (e.g. a previous
    # PR introduced the file the current issue is about to extend).
    # Canonical placeholder-titled WIP PRs have empty bodies and so do
    # NOT trip this filter — exit 1 from the helper means "keep". The
    # helper exits 0 only when EVERY closing-keyword reference points
    # at a different issue. See #4327 for the worked example
    # (issue #4317 vs PR #3426).
    closes_other_exit=0
    printf '%s' "$pr_json" | CHECK_SHIPPED_ISSUE_NUM="$issue_num" python3 \
        "$(dirname "${BASH_SOURCE[0]}")/_check_shipped_pr_closes_other.py" \
        2>/dev/null || closes_other_exit=$?
    if [[ "$closes_other_exit" -eq 0 ]]; then
        continue
    fi

    # Compute overlap and merged-on-main check via Python helper.
    # The helper applies the threshold (≥1 added overlap OR ≥2 total
    # overlap), the date-ordering guard (#4353 — a PR that merged
    # before the issue existed cannot have shipped its work), and
    # emits a tab-separated line on match, empty on miss.
    overlap_result=""
    if ! overlap_result=$(printf '%s' "$pr_json" | \
        CHECK_SHIPPED_CANDIDATE_FILES="$candidate_files_csv" \
        CHECK_SHIPPED_ISSUE_CREATED_AT="$issue_created_at" \
        python3 \
        "$(dirname "${BASH_SOURCE[0]}")/_check_shipped_pr_overlap.py" \
        2>/dev/null); then
        continue
    fi

    # overlap_result is "<count>\t<overlap-csv>\t<added-csv>" or empty.
    if [[ -z "$overlap_result" ]]; then
        continue
    fi
    # Tab-split into count, overlap_list, added_list.
    IFS=$'\t' read -r count overlap_list added_list <<< "$overlap_result"
    if [[ "$count" =~ ^[0-9]+$ && "$count" -gt "$best_overlap_count" ]]; then
        best_overlap_count="$count"
        best_pr="$pr_num"
        best_overlap_list="$overlap_list"
        best_added_list="$added_list"
    fi
done

if [[ -z "$best_pr" || "$best_overlap_count" -lt 1 ]]; then
    echo "not-shipped: no merged PR has ≥1 file overlap with issue #${issue_num} (exit 1)"
    exit 1
fi

# ─── Emit shipped: line + JSON summary ─────────────────────────────────────

# Pretty JSON via Python (avoids quoting issues in pure bash).
summary_json=""
summary_json=$(CHECK_SHIPPED_ISSUE="$issue_num" \
                CHECK_SHIPPED_PR="$best_pr" \
                CHECK_SHIPPED_OVERLAP_COUNT="$best_overlap_count" \
                CHECK_SHIPPED_OVERLAP_FILES="$best_overlap_list" \
                CHECK_SHIPPED_ADDED_FILES="$best_added_list" \
                CHECK_SHIPPED_CANDIDATE_FILES="$candidate_files_csv" \
                python3 "$(dirname "${BASH_SOURCE[0]}")/_check_shipped_pr_summary.py" \
                2>/dev/null) || {
    echo "error: failed to format shipped summary JSON (exit 2)" >&2
    exit 2
}

echo "shipped: PR #${best_pr} merged to main with ${best_overlap_count} file overlap(s) for issue #${issue_num} (exit 0)"
echo "$summary_json"
exit 0
