#!/usr/bin/env bash
# adopt-pr.sh — Adopt an existing PR branch into the current worktree.
#
# When an agent resumes work that a previous agent left behind (e.g. a PR that
# was blocked by a CI issue), it needs to check out the PR's branch in the new
# worktree instead of starting fresh. This script automates the manual steps:
# fetch the PR branch, check it out locally, rebase on latest main.
#
# Usage:
#   scripts/adopt-pr.sh <PR-number>
#
# Prerequisites:
#   - Must be run from inside a git worktree (not the main checkout)
#   - The worktree should have been created by start-worker.sh or the
#     dispatcher's isolation: "worktree" mode
#   - gh CLI must be authenticated
#
# What it does:
#   1. Fetches the PR's head branch name and ref from GitHub
#   2. Fetches the branch from origin
#   3. Switches the worktree to that branch (deleting the placeholder branch)
#   4. Rebases on latest origin/main
#   5. Reports any conflicts (does not silently fail)
#
# On success, prints the branch name to stdout.
# On failure, prints a diagnostic to stderr and exits non-zero.

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
if [[ $# -ne 1 ]]; then
    echo "Usage: scripts/adopt-pr.sh <PR-number>" >&2
    echo "  <PR-number>  — the GitHub PR number to adopt (e.g. 1194)" >&2
    exit 1
fi

PR_NUMBER="${1#\#}"
REPO="judgemind/judgemind"

# Validate argument is a number
if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
    echo "ERROR: PR number must be a positive integer, got '$PR_NUMBER'" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 1 — Verify we're in a worktree
# ---------------------------------------------------------------------------
GIT_DIR_PATH=$(git rev-parse --git-dir 2>/dev/null) || {
    echo "ERROR: Not inside a git repository." >&2
    exit 1
}

if [[ "$GIT_DIR_PATH" != *"/worktrees/"* ]]; then
    echo "ERROR: Not inside a git worktree. Run this from a worktree created by" >&2
    echo "  start-worker.sh or the dispatcher's isolation mode." >&2
    echo "  Current git dir: $GIT_DIR_PATH" >&2
    exit 1
fi

WORKTREE_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "ERROR: Could not determine worktree root." >&2
    exit 1
}

CURRENT_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null) || {
    echo "ERROR: HEAD is detached — cannot determine current branch." >&2
    exit 1
}

echo "Worktree: $WORKTREE_ROOT" >&2
echo "Current branch: $CURRENT_BRANCH" >&2

# ---------------------------------------------------------------------------
# Step 2 — Fetch PR metadata from GitHub
# ---------------------------------------------------------------------------
echo "Fetching PR #$PR_NUMBER metadata from GitHub..." >&2

# Fetch all fields in a single API call, output as tab-separated values
PR_DATA=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
    --json headRefName,state,baseRefName,title \
    --jq '[.headRefName, .state, .baseRefName, .title] | @tsv' 2>/dev/null) || {
    echo "ERROR: Could not fetch PR #$PR_NUMBER from $REPO." >&2
    echo "  Check that the PR exists and gh is authenticated." >&2
    exit 1
}

# Parse tab-separated fields
PR_HEAD=$(echo "$PR_DATA" | cut -f1)
PR_STATE=$(echo "$PR_DATA" | cut -f2)
PR_BASE=$(echo "$PR_DATA" | cut -f3)
PR_TITLE=$(echo "$PR_DATA" | cut -f4-)

echo "PR #$PR_NUMBER: $PR_TITLE" >&2
echo "  Head branch: $PR_HEAD" >&2
echo "  Base branch: $PR_BASE" >&2
echo "  State: $PR_STATE" >&2

if [[ "$PR_STATE" == "MERGED" ]]; then
    echo "ERROR: PR #$PR_NUMBER is already merged. Nothing to adopt." >&2
    exit 1
fi

if [[ "$PR_STATE" == "CLOSED" ]]; then
    echo "WARNING: PR #$PR_NUMBER is closed (not merged). Proceeding anyway..." >&2
fi

# ---------------------------------------------------------------------------
# Step 3 — Fetch the PR branch from origin
# ---------------------------------------------------------------------------
echo "Fetching branch '$PR_HEAD' from origin..." >&2
git fetch origin "$PR_HEAD" --quiet 2>/dev/null || {
    echo "ERROR: Could not fetch branch '$PR_HEAD' from origin." >&2
    echo "  The branch may have been deleted. Check the PR on GitHub." >&2
    exit 1
}

# Also fetch latest main for the rebase
echo "Fetching latest main..." >&2
git fetch origin main --quiet

# ---------------------------------------------------------------------------
# Step 4 — Switch the worktree to the PR branch
#
# Strategy: We need to get the worktree onto the PR's branch. Git doesn't
# allow two worktrees to share the same branch, so if the PR branch already
# exists as a local branch (from a previous agent's worktree that's since
# been removed), we need to delete it first, then create a fresh one.
#
# The old placeholder branch (created by start-worker.sh or the dispatcher)
# is deleted afterward.
# ---------------------------------------------------------------------------

# If a local branch with the PR's name already exists, check if it's safe to delete
if git show-ref --verify --quiet "refs/heads/$PR_HEAD" 2>/dev/null; then
    # Check if it's checked out in another worktree
    CHECKED_OUT_IN=$(git worktree list --porcelain \
        | awk -v branch="refs/heads/$PR_HEAD" \
            '/^worktree / { wt=$2 } /^branch / { if ($2 == branch) print wt }')

    if [[ -n "$CHECKED_OUT_IN" && "$CHECKED_OUT_IN" != "$WORKTREE_ROOT" ]]; then
        echo "ERROR: Branch '$PR_HEAD' is checked out in another worktree:" >&2
        echo "  $CHECKED_OUT_IN" >&2
        echo "  Remove that worktree first, or run from within it." >&2
        exit 1
    fi

    # Branch exists locally but not checked out elsewhere — safe to reset
    if [[ "$CURRENT_BRANCH" == "$PR_HEAD" ]]; then
        echo "Already on branch '$PR_HEAD' — resetting to origin..." >&2
        git reset --hard "origin/$PR_HEAD" >&2
    else
        echo "Deleting stale local branch '$PR_HEAD' and recreating from origin..." >&2
        git branch -D "$PR_HEAD" >&2 2>/dev/null || true
        git checkout -b "$PR_HEAD" "origin/$PR_HEAD" >&2
    fi
else
    echo "Creating local branch '$PR_HEAD' tracking origin/$PR_HEAD..." >&2
    git checkout -b "$PR_HEAD" "origin/$PR_HEAD" >&2
fi

# Set up tracking so `git push` goes to the right remote branch
git branch --set-upstream-to="origin/$PR_HEAD" "$PR_HEAD" >&2 2>/dev/null || true

# Clean up the old placeholder branch (if different from the PR branch)
if [[ "$CURRENT_BRANCH" != "$PR_HEAD" ]]; then
    echo "Deleting placeholder branch '$CURRENT_BRANCH'..." >&2
    git branch -D "$CURRENT_BRANCH" >&2 2>/dev/null || {
        echo "WARNING: Could not delete old branch '$CURRENT_BRANCH' (may still be in use)." >&2
    }
fi

# ---------------------------------------------------------------------------
# Step 5 — Rebase on latest main
# ---------------------------------------------------------------------------
echo "Rebasing '$PR_HEAD' onto origin/main..." >&2

# Temporarily disable errexit so we can catch rebase conflicts
set +e
git rebase origin/main >&2
REBASE_STATUS=$?
set -e

if [[ "$REBASE_STATUS" -eq 0 ]]; then
    echo "" >&2
    echo "Successfully adopted PR #$PR_NUMBER." >&2
    echo "  Branch: $PR_HEAD" >&2
    echo "  Rebased on latest main." >&2
    echo "  Ready to continue working." >&2
    echo "" >&2
    # Print branch name to stdout (for scripting)
    echo "$PR_HEAD"
    exit 0
else
    echo "" >&2
    echo "CONFLICT: Rebase of '$PR_HEAD' onto origin/main has conflicts." >&2
    echo "" >&2
    echo "  The branch has been checked out but the rebase is paused." >&2
    echo "  To resolve:" >&2
    echo "    1. Fix the conflicting files (look for <<<<<<< markers)" >&2
    echo "    2. Stage resolved files: git add <files>" >&2
    echo "    3. Continue the rebase: git rebase --continue" >&2
    echo "" >&2
    echo "  Or to abort and keep the branch as-is (without rebasing):" >&2
    echo "    git rebase --abort" >&2
    echo "" >&2
    exit 1
fi
