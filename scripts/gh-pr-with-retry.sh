#!/usr/bin/env bash
# permanent: true
# gh-pr-with-retry.sh — Run `gh pr create` / `gh pr edit` / `gh pr merge`
# and fall back to the GitHub REST API on GraphQL rate-limit exhaustion.
#
# Why this helper exists
# ----------------------
# `gh pr create`, `gh pr edit`, and `gh pr merge` all post via GitHub's
# GraphQL API, which has a separate 5,000-req/hr quota from the REST core
# API. Long agent sessions (dispatcher, multi-PR /task runs) routinely
# exhaust GraphQL via `mcp__github__*` reads + `gh pr view` polls while
# the REST core quota stays healthy, and then `gh pr create / edit /
# merge` hard-fails with:
#
#   GraphQL: API rate limit already exceeded for user ID <N>.
#
# This helper detects that exact stderr marker and falls back to the
# direct REST endpoints, which draw from the REST core quota:
#
#   create  → POST   /repos/<owner>/<repo>/pulls
#   edit    → PATCH  /repos/<owner>/<repo>/pulls/<N>
#   merge   → PUT    /repos/<owner>/<repo>/pulls/<N>/merge
#             (+ DELETE /repos/<owner>/<repo>/git/refs/heads/<branch>
#              when --delete-branch is set, to match `gh pr merge`'s
#              one-shot semantic)
#
# The shape mirrors `scripts/gh-comment-with-retry.sh` (#4478, #4503,
# #4484): try the gh subcommand first, on the explicit GraphQL-quota
# marker fall back to REST, otherwise pass the original failure through.
# This file does NOT implement the 504-after-success recovery path; PR
# create/edit/merge each have different idempotency stories than the
# comment endpoint, and the only failure mode #4527 calls out is
# GraphQL-quota exhaustion. The 504-on-merge recovery is documented in
# `.claude/skills/task/SKILL.md` §A.7 ("Fallback — `gh pr merge` returns
# 5xx / 504 Gateway Timeout (#4231)") and stays operator-driven.
#
# Tracking: issue #4527.
#
# Usage
# -----
#   scripts/gh-pr-with-retry.sh create \
#       --title "..." --body-file <path> --base main --head <branch> \
#       [--repo <owner/name>]
#
#   scripts/gh-pr-with-retry.sh edit <PR-N> \
#       [--title "..."] [--body-file <path>] [--repo <owner/name>]
#
#   scripts/gh-pr-with-retry.sh merge <PR-N> \
#       --squash [--delete-branch] [--repo <owner/name>]
#
#   scripts/gh-pr-with-retry.sh --help
#
# Behavior (all subcommands)
# --------------------------
#   1. Calls the corresponding `gh pr <subcommand>` first.
#   2. On exit 0: prints the gh stdout and exits 0.
#   3. On non-zero exit AND captured stderr matches
#      ``GraphQL: API rate limit already exceeded`` (#4503):
#      - Falls back to `gh api -X <METHOD> /repos/.../pulls/...`.
#      - On REST success: prints "<subcommand> succeeded (REST fallback): <url-or-info>"
#        and exits 0.
#      - On REST failure: prints both stderrs (truncated to 5 KB each)
#        and exits with the REST exit code.
#   4. On any other non-zero exit (auth failure, branch-protection
#      reject, validation error, generic 5xx): prints the captured
#      stderr and exits with the original code.
#
# Decision rules for the REST fallback
# ------------------------------------
# - Trigger ONLY on the explicit GraphQL-rate-limit-exceeded marker.
#   Generic 5xx, auth failures, validation errors, and branch-protection
#   rejects pass through untouched — those need either operator
#   intervention or a different recovery path (e.g. the #4231 504-on-
#   merge recipe in SKILL.md).
# - The REST endpoints draw from the REST core 5,000-req/hr quota,
#   which is rarely exhausted even in long sessions.
#
# Rate-budget hygiene
# -------------------
# Stays well within the 5,000 reqs/hr GitHub API budget — at most one
# extra REST call per GraphQL-quota-exhaustion (two for `merge
# --delete-branch`, since the API requires a separate DELETE on the
# branch ref). No exponential backoff retries: a single confirm-or-fail
# round trip is enough — if the REST quota is also exhausted, the
# operator needs to wait for the hourly reset.
#
# Integration
# -----------
# Replaces direct ``gh pr create / edit / merge`` calls in
# ``.claude/skills/task/SKILL.md`` Steps A.3 (create), A.6 (edit),
# and A.7 (merge).

set -euo pipefail

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

print_help() {
    cat << 'EOF'
gh-pr-with-retry.sh — `gh pr create` / `gh pr edit` / `gh pr merge`
with GraphQL-quota REST fallback.

Usage:
  scripts/gh-pr-with-retry.sh create \
      --title "..." --body-file <path> --base <branch> --head <branch> \
      [--repo <owner/name>]

  scripts/gh-pr-with-retry.sh edit <PR-N> \
      [--title "..."] [--body-file <path>] [--repo <owner/name>]

  scripts/gh-pr-with-retry.sh merge <PR-N> \
      --squash [--delete-branch] [--repo <owner/name>]

  scripts/gh-pr-with-retry.sh --help | -h

Subcommands:
  create   Open a new PR. Required: --title, --body-file, --base, --head.
  edit     Update an existing PR's title and/or body. At least one of
           --title or --body-file is required.
  merge    Squash-merge a PR. --squash is required (only mode supported);
           pass --delete-branch to delete the head ref after merging.

Common flags:
  --repo <owner/name>   Repository (default: judgemind/judgemind).
  -h, --help            Show this help and exit.

Exit codes:
  0  — Operation succeeded (including GraphQL-rate-limit REST fallback).
  1  — Real failure (auth, validation, branch protection, real 5xx, etc.).
  2  — Usage error (missing args, body-file not found, bad subcommand).

See header comment for behavior details. Tracking: #4527.
EOF
}

if [[ $# -eq 0 ]]; then
    print_help >&2
    exit 2
fi

case "${1:-}" in
    -h|--help)
        print_help
        exit 0
        ;;
esac

SUBCOMMAND="$1"
shift

case "$SUBCOMMAND" in
    create|edit|merge)
        ;;
    *)
        echo "ERROR: unknown subcommand: $SUBCOMMAND" >&2
        print_help >&2
        exit 2
        ;;
esac

# Shared state across subcommands.
PR_NUMBER=""
TITLE=""
BODY_FILE=""
BASE=""
HEAD=""
REPO="judgemind/judgemind"
SQUASH=0
DELETE_BRANCH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            print_help
            exit 0
            ;;
        --title)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --title requires a value." >&2
                exit 2
            fi
            TITLE="$2"
            shift 2
            ;;
        --body-file)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --body-file requires a value." >&2
                exit 2
            fi
            BODY_FILE="$2"
            shift 2
            ;;
        --base)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --base requires a value." >&2
                exit 2
            fi
            BASE="$2"
            shift 2
            ;;
        --head)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --head requires a value." >&2
                exit 2
            fi
            HEAD="$2"
            shift 2
            ;;
        --repo)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --repo requires a value." >&2
                exit 2
            fi
            REPO="$2"
            shift 2
            ;;
        --squash)
            SQUASH=1
            shift
            ;;
        --delete-branch)
            DELETE_BRANCH=1
            shift
            ;;
        -*)
            echo "ERROR: unknown flag: $1" >&2
            print_help >&2
            exit 2
            ;;
        *)
            if [[ -z "$PR_NUMBER" ]]; then
                PR_NUMBER="${1#\#}"
            else
                echo "ERROR: unexpected positional argument: $1" >&2
                print_help >&2
                exit 2
            fi
            shift
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Per-subcommand validation
# ---------------------------------------------------------------------------

validate_body_file() {
    if [[ ! -f "$BODY_FILE" ]]; then
        echo "ERROR: --body-file does not exist: $BODY_FILE" >&2
        exit 2
    fi
}

validate_pr_number() {
    if [[ -z "$PR_NUMBER" ]]; then
        echo "ERROR: missing <PR-N> argument." >&2
        print_help >&2
        exit 2
    fi
    if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
        echo "ERROR: <PR-N> must be a positive integer, got '$PR_NUMBER'." >&2
        exit 2
    fi
}

case "$SUBCOMMAND" in
    create)
        if [[ -z "$TITLE" ]]; then
            echo "ERROR: 'create' requires --title." >&2
            exit 2
        fi
        if [[ -z "$BODY_FILE" ]]; then
            echo "ERROR: 'create' requires --body-file." >&2
            exit 2
        fi
        if [[ -z "$BASE" ]]; then
            echo "ERROR: 'create' requires --base." >&2
            exit 2
        fi
        if [[ -z "$HEAD" ]]; then
            echo "ERROR: 'create' requires --head." >&2
            exit 2
        fi
        validate_body_file
        ;;
    edit)
        validate_pr_number
        if [[ -z "$TITLE" && -z "$BODY_FILE" ]]; then
            echo "ERROR: 'edit' requires at least one of --title or --body-file." >&2
            exit 2
        fi
        if [[ -n "$BODY_FILE" ]]; then
            validate_body_file
        fi
        ;;
    merge)
        validate_pr_number
        if [[ "$SQUASH" -ne 1 ]]; then
            echo "ERROR: 'merge' requires --squash (only mode supported)." >&2
            exit 2
        fi
        ;;
esac

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Capture-and-tee stderr so we can both inspect (for the GraphQL marker)
# and replay it on real failures without buffering megabytes in shell vars.
STDERR_FILE=$(mktemp)
STDOUT_FILE=$(mktemp)
REST_STDERR_FILE=$(mktemp)
REST_STDOUT_FILE=$(mktemp)
DELETE_STDERR_FILE=$(mktemp)
DELETE_STDOUT_FILE=$(mktemp)
# shellcheck disable=SC2064  # cleanup paths are fixed at trap-install time.
trap "rm -f '$STDERR_FILE' '$STDOUT_FILE' '$REST_STDERR_FILE' '$REST_STDOUT_FILE' '$DELETE_STDERR_FILE' '$DELETE_STDOUT_FILE'" EXIT

is_graphql_rate_limit() {
    # Trigger ONLY on the explicit marker. Generic 5xx, auth failures,
    # validation errors, etc. all flow through the passthrough path so
    # the operator (or the SKILL.md recipes) handle them as today.
    grep -qE "GraphQL: API rate limit already exceeded" "$STDERR_FILE"
}

passthrough_failure() {
    local exit_code="$1"
    cat "$STDERR_FILE" >&2
    exit "$exit_code"
}

# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

run_create() {
    set +e
    gh pr create --repo "$REPO" \
        --title "$TITLE" \
        --body-file "$BODY_FILE" \
        --base "$BASE" \
        --head "$HEAD" \
        > "$STDOUT_FILE" 2> "$STDERR_FILE"
    local gh_exit=$?
    set -e

    if [[ "$gh_exit" -eq 0 ]]; then
        cat "$STDOUT_FILE"
        exit 0
    fi

    if ! is_graphql_rate_limit; then
        passthrough_failure "$gh_exit"
    fi

    echo "gh-pr-with-retry: GraphQL rate-limit exhausted on 'pr create', falling back to REST API..." >&2

    # Build the JSON payload via a sibling python helper. Same pattern
    # as scripts/_gh_comment_with_retry_match.py and friends — keeps the
    # helper unit-testable and avoids shell-quoting hazards on the
    # multi-line body content.
    local payload_file
    payload_file=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$STDERR_FILE' '$STDOUT_FILE' '$REST_STDERR_FILE' '$REST_STDOUT_FILE' '$DELETE_STDERR_FILE' '$DELETE_STDOUT_FILE' '$payload_file'" EXIT

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if ! GH_PR_TITLE="$TITLE" \
            GH_PR_BODY_FILE="$BODY_FILE" \
            GH_PR_HEAD="$HEAD" \
            GH_PR_BASE="$BASE" \
            GH_PR_PAYLOAD_FILE="$payload_file" \
            python3 "$script_dir/_gh_pr_with_retry_payload.py" create; then
        echo "ERROR: failed to build REST payload for 'pr create'." >&2
        passthrough_failure "$gh_exit"
    fi

    set +e
    gh api -X POST "/repos/$REPO/pulls" \
        --input "$payload_file" \
        --jq '.html_url' \
        > "$REST_STDOUT_FILE" 2> "$REST_STDERR_FILE"
    local rest_exit=$?
    set -e

    if [[ "$rest_exit" -eq 0 ]]; then
        local rest_url
        rest_url=$(cat "$REST_STDOUT_FILE")
        echo "create succeeded (REST fallback): $rest_url"
        exit 0
    fi

    echo "ERROR: GraphQL rate-limit fallback to REST also failed (gh exit $rest_exit)." >&2
    echo "  --- gh pr create stderr (first 5 KB) ---" >&2
    head -c 5120 "$STDERR_FILE" >&2
    echo "" >&2
    echo "  --- gh api POST /pulls stderr (first 5 KB) ---" >&2
    head -c 5120 "$REST_STDERR_FILE" >&2
    echo "" >&2
    exit "$rest_exit"
}

# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

run_edit() {
    # Build the gh pr edit args.
    local -a gh_args=("pr" "edit" "$PR_NUMBER" "--repo" "$REPO")
    if [[ -n "$TITLE" ]]; then
        gh_args+=("--title" "$TITLE")
    fi
    if [[ -n "$BODY_FILE" ]]; then
        gh_args+=("--body-file" "$BODY_FILE")
    fi

    set +e
    gh "${gh_args[@]}" \
        > "$STDOUT_FILE" 2> "$STDERR_FILE"
    local gh_exit=$?
    set -e

    if [[ "$gh_exit" -eq 0 ]]; then
        cat "$STDOUT_FILE"
        exit 0
    fi

    if ! is_graphql_rate_limit; then
        passthrough_failure "$gh_exit"
    fi

    echo "gh-pr-with-retry: GraphQL rate-limit exhausted on 'pr edit', falling back to REST API..." >&2

    local payload_file
    payload_file=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$STDERR_FILE' '$STDOUT_FILE' '$REST_STDERR_FILE' '$REST_STDOUT_FILE' '$DELETE_STDERR_FILE' '$DELETE_STDOUT_FILE' '$payload_file'" EXIT

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if ! GH_PR_TITLE="$TITLE" \
            GH_PR_BODY_FILE="$BODY_FILE" \
            GH_PR_PAYLOAD_FILE="$payload_file" \
            python3 "$script_dir/_gh_pr_with_retry_payload.py" edit; then
        echo "ERROR: failed to build REST payload for 'pr edit'." >&2
        passthrough_failure "$gh_exit"
    fi

    set +e
    gh api -X PATCH "/repos/$REPO/pulls/$PR_NUMBER" \
        --input "$payload_file" \
        --jq '.html_url' \
        > "$REST_STDOUT_FILE" 2> "$REST_STDERR_FILE"
    local rest_exit=$?
    set -e

    if [[ "$rest_exit" -eq 0 ]]; then
        local rest_url
        rest_url=$(cat "$REST_STDOUT_FILE")
        echo "edit succeeded (REST fallback): $rest_url"
        exit 0
    fi

    echo "ERROR: GraphQL rate-limit fallback to REST also failed (gh exit $rest_exit)." >&2
    echo "  --- gh pr edit stderr (first 5 KB) ---" >&2
    head -c 5120 "$STDERR_FILE" >&2
    echo "" >&2
    echo "  --- gh api PATCH /pulls/$PR_NUMBER stderr (first 5 KB) ---" >&2
    head -c 5120 "$REST_STDERR_FILE" >&2
    echo "" >&2
    exit "$rest_exit"
}

# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

run_merge() {
    # Build the gh pr merge args. SQUASH is the only supported mode.
    local -a gh_args=("pr" "merge" "$PR_NUMBER" "--repo" "$REPO" "--squash")
    if [[ "$DELETE_BRANCH" -eq 1 ]]; then
        gh_args+=("--delete-branch")
    fi

    set +e
    gh "${gh_args[@]}" \
        > "$STDOUT_FILE" 2> "$STDERR_FILE"
    local gh_exit=$?
    set -e

    if [[ "$gh_exit" -eq 0 ]]; then
        cat "$STDOUT_FILE"
        exit 0
    fi

    if ! is_graphql_rate_limit; then
        passthrough_failure "$gh_exit"
    fi

    echo "gh-pr-with-retry: GraphQL rate-limit exhausted on 'pr merge', falling back to REST API..." >&2

    set +e
    gh api -X PUT "/repos/$REPO/pulls/$PR_NUMBER/merge" \
        -f "merge_method=squash" \
        --jq '{merged: .merged, sha: .sha, message: .message}' \
        > "$REST_STDOUT_FILE" 2> "$REST_STDERR_FILE"
    local rest_exit=$?
    set -e

    if [[ "$rest_exit" -ne 0 ]]; then
        echo "ERROR: GraphQL rate-limit fallback to REST also failed (gh exit $rest_exit)." >&2
        echo "  --- gh pr merge stderr (first 5 KB) ---" >&2
        head -c 5120 "$STDERR_FILE" >&2
        echo "" >&2
        echo "  --- gh api PUT /pulls/$PR_NUMBER/merge stderr (first 5 KB) ---" >&2
        head -c 5120 "$REST_STDERR_FILE" >&2
        echo "" >&2
        exit "$rest_exit"
    fi

    # Merge succeeded. If --delete-branch was set, delete the head ref
    # explicitly — the REST PUT /merge endpoint does not delete the
    # branch (unlike `gh pr merge --delete-branch` which is a one-shot).
    local rest_summary
    rest_summary=$(cat "$REST_STDOUT_FILE")

    if [[ "$DELETE_BRANCH" -ne 1 ]]; then
        echo "merge succeeded (REST fallback): $rest_summary"
        exit 0
    fi

    # Resolve the head ref via REST (one extra core-quota call). We
    # cannot rely on `gh pr view --jq .headRefName` here because that
    # path also goes through GraphQL — which is exactly why we're in
    # the fallback. Use the REST GET on the PR.
    local head_ref
    head_ref=$(gh api "/repos/$REPO/pulls/$PR_NUMBER" --jq '.head.ref' 2>/dev/null) || head_ref=""
    if [[ -z "$head_ref" ]]; then
        echo "WARNING: merge succeeded but could not resolve head ref to delete (--delete-branch)." >&2
        echo "merge succeeded (REST fallback): $rest_summary" >&2
        echo "  manual cleanup: gh api /repos/$REPO/git/refs/heads/<branch> -X DELETE" >&2
        # The merge succeeded; the branch deletion is best-effort. Exit
        # 0 — the operator can clean up the branch manually.
        echo "merge succeeded (REST fallback): $rest_summary"
        exit 0
    fi

    set +e
    gh api -X DELETE "/repos/$REPO/git/refs/heads/$head_ref" \
        > "$DELETE_STDOUT_FILE" 2> "$DELETE_STDERR_FILE"
    local delete_exit=$?
    set -e

    if [[ "$delete_exit" -eq 0 ]]; then
        echo "merge succeeded (REST fallback): $rest_summary; branch '$head_ref' deleted"
        exit 0
    fi

    # If the branch is already gone (DELETE returns 422 / "Reference does not exist"),
    # treat as success — same semantics as the SKILL.md A.7 recipe.
    if grep -qE "Reference does not exist|HTTP 422" "$DELETE_STDERR_FILE"; then
        echo "merge succeeded (REST fallback): $rest_summary; branch '$head_ref' was already deleted"
        exit 0
    fi

    echo "WARNING: merge succeeded but branch DELETE failed (gh exit $delete_exit)." >&2
    head -c 5120 "$DELETE_STDERR_FILE" >&2
    echo "" >&2
    echo "merge succeeded (REST fallback): $rest_summary; branch '$head_ref' NOT deleted"
    # Merge is the user's primary intent and it succeeded — exit 0 even
    # though the cleanup didn't. The warning above gives the operator
    # the manual recovery path.
    exit 0
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "$SUBCOMMAND" in
    create)
        run_create
        ;;
    edit)
        run_edit
        ;;
    merge)
        run_merge
        ;;
esac
