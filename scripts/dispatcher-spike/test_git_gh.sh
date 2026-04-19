#!/usr/bin/env bash
# test_git_gh.sh — Dispatcher v2 spike 0.7 end-to-end git/GitHub auth check.
#
# Exercises the four operations the dispatcher daemon's `/task-v2-*`
# subprocesses will need to perform from inside a Fargate container:
#
#   1. `git push`   — push a throwaway branch to judgemind/judgemind.
#   2. `gh pr create` / `gh pr view` / `gh pr close` — open, inspect,
#      and close a throwaway PR without merging it to main.
#   3. `scripts/check-issue-author.sh <N>` — the public-repo trust-check
#      every autonomous agent runs before acting on an issue.
#   4. Cleanup: close the PR, delete the remote branch.
#
# Auth is taken from ${GITHUB_TOKEN}, which is populated by ECS from
# Secrets Manager (`judgemind/dev/dispatcher-spike/github-token`) when
# launched via the spike task definition.
#
# Usage:
#   GITHUB_TOKEN=ghp_xxx test_git_gh.sh [test-issue-number]
#
# Arguments:
#   test-issue-number   Issue to run `check-issue-author.sh` against.
#                       Defaults to 2689 (spike 0.7 issue itself, which
#                       must be MEMBER-filed and thus return TRUSTED).
#
# Env overrides:
#   SPIKE_REPO             Default "judgemind/judgemind".
#   SPIKE_BRANCH_PREFIX    Default "spike/0.7-auth-test". An `-<epoch>`
#                          suffix is appended so reruns don't collide.
#   SPIKE_WORKDIR          Working directory for the clone. Default is
#                          `$(mktemp -d)`. Override for debugging.
#
# Output:
#   Each operation logs its outcome with an [OP-N] prefix. On success
#   the final line is "ALL_CHECKS_PASSED". On failure the script exits
#   non-zero after printing "OP-<N>_FAILED" with the failing operation.
#
# Exit codes:
#   0   All four operations succeeded.
#   1   Any operation failed. Inspect the preceding [OP-N] lines.
#   2   Misconfiguration (missing GITHUB_TOKEN, missing gh/git binaries).
#
# This script is intentionally silent about which auth mechanism
# populated ${GITHUB_TOKEN} (scoped PAT vs. GitHub App installation
# token) — the spike wrapper that runs it is what makes that choice.
# As long as GITHUB_TOKEN resolves to a usable credential, the script
# passes.

set -u

TEST_ISSUE="${1:-2689}"
SPIKE_REPO="${SPIKE_REPO:-judgemind/judgemind}"

EPOCH="$(date -u +%s)"
SPIKE_BRANCH_PREFIX="${SPIKE_BRANCH_PREFIX:-spike/0.7-auth-test}"
SPIKE_BRANCH="${SPIKE_BRANCH_PREFIX}-${EPOCH}"

SPIKE_WORKDIR="${SPIKE_WORKDIR:-$(mktemp -d)}"

log()  { printf '[test-git-gh] %s\n' "$*"; }

# ─── Pre-flight ─────────────────────────────────────────────────────────────

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    printf '[test-git-gh] GITHUB_TOKEN is unset. Aborting.\n' >&2
    exit 2
fi

command -v git >/dev/null 2>&1 || {
    printf '[test-git-gh] git not installed. Aborting.\n' >&2
    exit 2
}

command -v gh >/dev/null 2>&1 || {
    printf '[test-git-gh] gh CLI not installed. Aborting.\n' >&2
    exit 2
}

log "repo=${SPIKE_REPO} branch=${SPIKE_BRANCH} workdir=${SPIKE_WORKDIR} test_issue=#${TEST_ISSUE}"

# Configure gh auth non-interactively from the token. `gh auth status`
# and `gh pr *` both read ${GITHUB_TOKEN} automatically, but being
# explicit here surfaces auth problems early with a clear error.
log "gh auth status"
if ! gh auth status 2>&1 | sed 's/^/  /'; then
    printf 'OP-0_FAILED (gh auth status)\n'
    exit 1
fi

# Tell gh how to drive git operations — this is what makes `git push`
# succeed under ${GITHUB_TOKEN} without a keyring or ssh key. We do
# this in a temp git config so we never touch the user's global one.
export GIT_CONFIG_GLOBAL="${SPIKE_WORKDIR}/.gitconfig-spike"
: > "${GIT_CONFIG_GLOBAL}"
git config --global user.name  "Judgemind Dispatcher Spike"
git config --global user.email "noreply@judgemind.org"
git config --global init.defaultBranch main

# Use gh as a git credential helper. This maps `git push
# https://github.com/judgemind/judgemind` to ${GITHUB_TOKEN}.
gh auth setup-git 2>&1 | sed 's/^/  /' || {
    printf 'OP-0_FAILED (gh auth setup-git)\n'
    exit 1
}

cd "${SPIKE_WORKDIR}"

# ─── OP-1: clone + push a throwaway branch ──────────────────────────────────

log "OP-1: clone ${SPIKE_REPO} (shallow) and push ${SPIKE_BRANCH}"

CLONE_DIR="${SPIKE_WORKDIR}/repo"
if ! git clone --depth=1 "https://github.com/${SPIKE_REPO}.git" "${CLONE_DIR}" 2>&1 | sed 's/^/  /'; then
    printf 'OP-1_FAILED (git clone)\n'
    exit 1
fi

cd "${CLONE_DIR}"
if ! git checkout -b "${SPIKE_BRANCH}" 2>&1 | sed 's/^/  /'; then
    printf 'OP-1_FAILED (git checkout)\n'
    exit 1
fi

# Make a trivial one-line edit so the branch has a commit the PR
# diff will show. Touch a throwaway file under tmp/ which is
# gitignored by convention but not in every repo — use a dedicated
# path that is guaranteed to be tracked.
SPIKE_MARKER="docs/investigations/.spike-0.7-marker-${EPOCH}.txt"
mkdir -p "$(dirname "${SPIKE_MARKER}")"
printf 'Spike 0.7 auth probe — epoch %s. Safe to ignore; PR will be closed not merged.\n' \
    "${EPOCH}" > "${SPIKE_MARKER}"
git add "${SPIKE_MARKER}"
if ! git commit -m "test: spike 0.7 auth probe (throwaway, do not merge) [epoch=${EPOCH}]" 2>&1 | sed 's/^/  /'; then
    printf 'OP-1_FAILED (git commit)\n'
    exit 1
fi

if ! git push -u origin "${SPIKE_BRANCH}" 2>&1 | sed 's/^/  /'; then
    printf 'OP-1_FAILED (git push)\n'
    exit 1
fi

log "OP-1: PASSED (push of ${SPIKE_BRANCH} to ${SPIKE_REPO} succeeded)"

# ─── OP-2: gh pr create / view ──────────────────────────────────────────────

log "OP-2: gh pr create / view"

PR_BODY_FILE="${SPIKE_WORKDIR}/pr-body.txt"
{
    printf 'Spike 0.7 auth probe. Throwaway — will be closed, not merged.\n'
    printf '\n'
    printf 'Generated by scripts/dispatcher-spike/test_git_gh.sh at epoch %s.\n' "${EPOCH}"
    printf 'Part of verification for #2689 (dispatcher v2 spike 0.7).\n'
} > "${PR_BODY_FILE}"

PR_URL_FILE="${SPIKE_WORKDIR}/pr-url.txt"
if ! gh pr create \
        --repo "${SPIKE_REPO}" \
        --title "test: spike 0.7 auth probe (throwaway) [epoch=${EPOCH}]" \
        --body-file "${PR_BODY_FILE}" \
        --head "${SPIKE_BRANCH}" \
        --base main \
        > "${PR_URL_FILE}" 2>&1; then
    sed 's/^/  /' "${PR_URL_FILE}"
    printf 'OP-2_FAILED (gh pr create)\n'
    exit 1
fi
sed 's/^/  /' "${PR_URL_FILE}"

PR_URL="$(tail -n 1 "${PR_URL_FILE}")"
log "OP-2: PR created → ${PR_URL}"

# gh pr view returns the PR body + state; we just need it to exit 0.
if ! gh pr view "${PR_URL}" --repo "${SPIKE_REPO}" \
        --json number,state,url,headRefName \
        2>&1 | sed 's/^/  /'; then
    printf 'OP-2_FAILED (gh pr view)\n'
    exit 1
fi

log "OP-2: PASSED (gh pr create + view succeeded)"

# ─── OP-3: scripts/check-issue-author.sh ────────────────────────────────────

log "OP-3: scripts/check-issue-author.sh ${TEST_ISSUE}"

# check-issue-author.sh lives under /usr/local/bin in the spike image,
# but when run locally it's at the repo root. Probe both.
CHECK_ISSUE_AUTHOR=""
for candidate in \
    /usr/local/bin/check-issue-author.sh \
    "${CLONE_DIR}/scripts/check-issue-author.sh" \
    "$(dirname "${BASH_SOURCE[0]}")/../check-issue-author.sh"
do
    if [[ -x "${candidate}" ]]; then
        CHECK_ISSUE_AUTHOR="${candidate}"
        break
    fi
done

if [[ -z "${CHECK_ISSUE_AUTHOR}" ]]; then
    printf 'OP-3_FAILED (could not locate check-issue-author.sh)\n'
    exit 1
fi

log "OP-3: invoking ${CHECK_ISSUE_AUTHOR}"
CHECK_OUTPUT_FILE="${SPIKE_WORKDIR}/check-output.txt"
set +e
"${CHECK_ISSUE_AUTHOR}" "${TEST_ISSUE}" > "${CHECK_OUTPUT_FILE}" 2>&1
CHECK_EC=$?
set -e
sed 's/^/  /' "${CHECK_OUTPUT_FILE}"

if [[ "${CHECK_EC}" -ne 0 ]]; then
    printf 'OP-3_FAILED (check-issue-author.sh exited %s)\n' "${CHECK_EC}"
    exit 1
fi

if ! grep -q '^TRUSTED:' "${CHECK_OUTPUT_FILE}"; then
    printf 'OP-3_FAILED (expected TRUSTED: prefix not found)\n'
    exit 1
fi

log "OP-3: PASSED (trust check returned TRUSTED for #${TEST_ISSUE})"

# ─── OP-4: gh pr close + branch delete ──────────────────────────────────────

log "OP-4: gh pr close ${PR_URL} (not merged)"
if ! gh pr close "${PR_URL}" --repo "${SPIKE_REPO}" --delete-branch \
        --comment "Spike 0.7 auth probe complete (epoch ${EPOCH}). Closing without merge." \
        2>&1 | sed 's/^/  /'; then
    printf 'OP-4_FAILED (gh pr close)\n'
    exit 1
fi
log "OP-4: PASSED (PR closed, branch deleted)"

# ─── Report ─────────────────────────────────────────────────────────────────

printf '\n'
log "=================================================="
log "ALL_CHECKS_PASSED"
log "  PR URL:   ${PR_URL}"
log "  Branch:   ${SPIKE_BRANCH} (deleted)"
log "  Issue:    #${TEST_ISSUE} (TRUSTED)"
log "  Epoch:    ${EPOCH}"
log "=================================================="
printf '\n'
printf 'ALL_CHECKS_PASSED\n'
exit 0
