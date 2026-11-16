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
#   merge   → GET    /repos/<owner>/<repo>/pulls/<N>   (protection gate)
#             PUT    /repos/<owner>/<repo>/pulls/<N>/merge  (sha= pinned)
#             (+ DELETE /repos/<owner>/<repo>/git/refs/heads/<branch>
#              when --delete-branch is set, to match `gh pr merge`'s
#              one-shot semantic)
#
#   The merge fallback is guarded (#4965): for a repo-admin token with
#   enforce_admins=false, `PUT /pulls/N/merge` skips required status
#   checks, so it is an admin bypass, not a re-evaluation of branch
#   protection. The wrapper therefore reads the PR first and refuses
#   (exit 1) unless `mergeable_state` is clean, unstable or has_hooks, and
#   it pins the PUT to the head SHA it checked (`-f sha=<head.sha>`).
#
# The shape mirrors `scripts/gh-comment-with-retry.sh` (#4478, #4503,
# #4484): try the gh subcommand first, on the explicit GraphQL-quota
# marker fall back to REST, otherwise pass the original failure through.
# Merge after a 5xx (#4231, #5122): GitHub's `gh pr merge` sometimes
# returns 502/504 after it has already accepted the squash, so
# `--delete-branch` never runs. On a 5xx the wrapper never retries the
# merge. It reads the PR over REST (`GET /pulls/<N>`); when `merged` is
# true it deletes the head ref (if --delete-branch was given; an
# already-gone ref counts as deleted) and exits 0. When the PR is not
# merged, or the read fails, the original failure passes through. This
# path only reads and deletes a ref; it never merges.
#
# PR-text gate at merge time (#5133): before any merge call, `merge` runs
# scripts/check-pr-closing-refs.py --pr N and scripts/check-pr-title.sh
# --pr N against the PR's live title, body and commits, and refuses (exit 1,
# with a Fix: block, no merge call) unless both pass. Those two checks also
# run in CI (pr-title-check.yml) but are not required status checks, so this
# gate is what stops a stray closing keyword or a placeholder title from
# merging. It runs on every path: plain, REST fallback and 5xx recovery.
#
# create/edit have no 5xx recovery: they are not idempotent the same way
# (a blind retry of create can open a duplicate PR). A
# `base branch policy prohibits the merge` refusal is passed through
# untouched: it means branch protection is not satisfied, so the caller
# waits (scripts/wait-for-ci.sh) instead of bypassing it (#4965).
#
# create passes --assignee / --label / --reviewer (each repeatable or
# comma-separated) and --draft through to `gh pr create`, so a PR opens
# assigned in one call (#5122). POST /pulls cannot carry assignees,
# labels or reviewers, so on the REST fallback the wrapper applies them
# with follow-up calls once the PR exists (`@me` is resolved via
# GET /user). A failure there warns but still exits 0, because the PR was
# created and a retry would open a duplicate.
#
# Tracking: issues #4527, #5122.
#
# Usage
# -----
#   scripts/gh-pr-with-retry.sh create \
#       --title "..." --body-file <path> --base main --head <branch> \
#       [--assignee <login>]... [--label <name>]... [--reviewer <login>]... \
#       [--draft] [--repo <owner/name>]
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
      [--assignee <login>]... [--label <name>]... [--reviewer <login>]... \
      [--draft] [--repo <owner/name>]

  scripts/gh-pr-with-retry.sh edit <PR-N> \
      [--title "..."] [--body-file <path>] [--repo <owner/name>]

  scripts/gh-pr-with-retry.sh merge <PR-N> \
      --squash [--delete-branch] [--repo <owner/name>]

  scripts/gh-pr-with-retry.sh --help | -h

Subcommands:
  create   Open a new PR. Required: --title, --body-file, --base, --head.
           Optional: --assignee, --label, --reviewer (repeatable or
           comma-separated) and --draft, passed through to gh pr create.
  edit     Update an existing PR's title and/or body. At least one of
           --title or --body-file is required.
  merge    Squash-merge a PR. --squash is required (only mode supported);
           pass --delete-branch to delete the head ref after merging.
           First runs the PR-text checks (check-pr-closing-refs.py and
           check-pr-title.sh) on the live PR and refuses (exit 1) unless
           both pass (#5133).
           On a 5xx from gh pr merge, reads the PR: if it merged anyway,
           deletes the head ref (with --delete-branch) and exits 0.

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
# create-only passthrough flags (#5122). Repeatable; each value is passed
# to `gh pr create` as its own --assignee / --label / --reviewer flag.
ASSIGNEES=()
LABELS=()
REVIEWERS=()
DRAFT=0

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
        --assignee|--label|--reviewer)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: $1 requires a value." >&2
                exit 2
            fi
            if [[ "$SUBCOMMAND" != "create" ]]; then
                echo "ERROR: $1 is only supported by 'create'." >&2
                exit 2
            fi
            # gh accepts comma-separated lists; split them so the REST
            # fallback can send each login / label on its own.
            IFS=',' read -r -a _vals <<< "$2"
            for _v in "${_vals[@]}"; do
                [[ -z "$_v" ]] && continue
                case "$1" in
                    --assignee) ASSIGNEES+=("$_v") ;;
                    --label) LABELS+=("$_v") ;;
                    --reviewer) REVIEWERS+=("$_v") ;;
                esac
            done
            shift 2
            ;;
        --draft)
            if [[ "$SUBCOMMAND" != "create" ]]; then
                echo "ERROR: $1 is only supported by 'create'." >&2
                exit 2
            fi
            DRAFT=1
            shift
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

# After a REST-fallback create, apply the --assignee / --label / --reviewer
# flags that POST /pulls cannot carry (#5122). The PR already exists, so a
# failure here only warns: exiting non-zero would invite a retry that opens
# a duplicate PR. Prints one status fragment per applied group on stdout.
apply_create_extras() {
    local pr_url="$1"
    local pr_number="${pr_url##*/}"
    local extras=""
    if ! [[ "$pr_number" =~ ^[0-9]+$ ]]; then
        if [[ ${#ASSIGNEES[@]} -gt 0 || ${#LABELS[@]} -gt 0 || ${#REVIEWERS[@]} -gt 0 ]]; then
            echo "WARNING: could not read the PR number from '$pr_url'; assignees/labels/reviewers NOT applied." >&2
        fi
        return 0
    fi

    if [[ ${#ASSIGNEES[@]} -gt 0 ]]; then
        local -a a_args=()
        local login
        for login in "${ASSIGNEES[@]}"; do
            if [[ "$login" == "@me" ]]; then
                # REST does not resolve @me; look up the token's login.
                login=$(gh api /user --jq '.login' 2>/dev/null) || login=""
                if [[ -z "$login" ]]; then
                    echo "WARNING: could not resolve @me to a login; skipping that assignee." >&2
                    continue
                fi
            fi
            a_args+=("-f" "assignees[]=$login")
        done
        if [[ ${#a_args[@]} -gt 0 ]]; then
            if gh api -X POST "/repos/$REPO/issues/$pr_number/assignees" "${a_args[@]}" > /dev/null 2>> "$REST_STDERR_FILE"; then
                extras+="; assignees applied"
            else
                echo "WARNING: PR #$pr_number created but assigning failed; run: gh pr edit $pr_number --repo $REPO --add-assignee ${ASSIGNEES[*]}" >&2
            fi
        fi
    fi

    if [[ ${#LABELS[@]} -gt 0 ]]; then
        local -a l_args=()
        local label
        for label in "${LABELS[@]}"; do
            l_args+=("-f" "labels[]=$label")
        done
        if gh api -X POST "/repos/$REPO/issues/$pr_number/labels" "${l_args[@]}" > /dev/null 2>> "$REST_STDERR_FILE"; then
            extras+="; labels applied"
        else
            echo "WARNING: PR #$pr_number created but labelling failed; run: gh pr edit $pr_number --repo $REPO --add-label <label>" >&2
        fi
    fi

    if [[ ${#REVIEWERS[@]} -gt 0 ]]; then
        local -a r_args=()
        local reviewer
        for reviewer in "${REVIEWERS[@]}"; do
            r_args+=("-f" "reviewers[]=$reviewer")
        done
        if gh api -X POST "/repos/$REPO/pulls/$pr_number/requested_reviewers" "${r_args[@]}" > /dev/null 2>> "$REST_STDERR_FILE"; then
            extras+="; reviewers requested"
        else
            echo "WARNING: PR #$pr_number created but requesting reviewers failed; run: gh pr edit $pr_number --repo $REPO --add-reviewer <login>" >&2
        fi
    fi

    printf '%s' "$extras"
}

run_create() {
    local -a gh_args=("pr" "create" "--repo" "$REPO"
        "--title" "$TITLE"
        "--body-file" "$BODY_FILE"
        "--base" "$BASE"
        "--head" "$HEAD")
    local v
    # ${arr[@]+...} keeps `set -u` happy on bash 3.2 when the array is empty.
    for v in ${ASSIGNEES[@]+"${ASSIGNEES[@]}"}; do
        gh_args+=("--assignee" "$v")
    done
    for v in ${LABELS[@]+"${LABELS[@]}"}; do
        gh_args+=("--label" "$v")
    done
    for v in ${REVIEWERS[@]+"${REVIEWERS[@]}"}; do
        gh_args+=("--reviewer" "$v")
    done
    if [[ "$DRAFT" -eq 1 ]]; then
        gh_args+=("--draft")
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
            GH_PR_DRAFT="$DRAFT" \
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
        local extras
        extras=$(apply_create_extras "$rest_url")
        echo "create succeeded (REST fallback): $rest_url$extras"
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

is_server_error() {
    # A 5xx from `gh pr merge` (#4231, #5122). GitHub often returns 502/504
    # (sometimes as a multi-KB "Unicorn!" HTML page) after it has already
    # accepted the squash, so `--delete-branch` never runs.
    grep -qE "HTTP 5[0-9]{2}|(^|[^0-9])50[0-4]([^0-9]|$)|Bad Gateway|Gateway Time-?out|Service Unavailable|Internal Server Error|Unicorn!" "$STDERR_FILE"
}

# Delete the head ref after a merge whose `--delete-branch` did not run.
# A ref that is already gone counts as deleted. Prints a status fragment.
delete_head_ref() {
    local head_ref="$1"
    if [[ -z "$head_ref" ]]; then
        echo "WARNING: could not resolve the head ref to delete." >&2
        printf '%s' "; head branch NOT deleted (unknown ref)"
        return 0
    fi
    set +e
    gh api -X DELETE "/repos/$REPO/git/refs/heads/$head_ref" \
        > "$DELETE_STDOUT_FILE" 2> "$DELETE_STDERR_FILE"
    local delete_exit=$?
    set -e
    if [[ "$delete_exit" -eq 0 ]]; then
        printf '%s' "; branch '$head_ref' deleted"
    elif grep -qE "Reference does not exist|HTTP 422" "$DELETE_STDERR_FILE"; then
        printf '%s' "; branch '$head_ref' was already deleted"
    else
        echo "WARNING: branch DELETE failed (gh exit $delete_exit):" >&2
        head -c 5120 "$DELETE_STDERR_FILE" >&2
        echo "" >&2
        echo "  manual cleanup: gh api /repos/$REPO/git/refs/heads/$head_ref -X DELETE" >&2
        printf '%s' "; branch '$head_ref' NOT deleted"
    fi
}

# `gh pr merge` returned a 5xx. Never retry the merge call: read the PR over
# REST (the GraphQL side is what just failed) and decide (#4231, #5122).
#   merged       → the squash landed. Delete the head ref if --delete-branch
#                  was asked for, report success, exit 0.
#   not merged   → pass the original failure through (the caller diagnoses
#                  and retries the wrapper once, per SKILL.md §A.7).
#   read failed  → pass the original failure through.
recover_merge_after_server_error() {
    local gh_exit="$1"
    echo "gh-pr-with-retry: 'pr merge' returned a server error; checking whether the merge landed anyway..." >&2

    local state_line merged head_ref pr_state
    state_line=$(gh api "/repos/$REPO/pulls/$PR_NUMBER" \
        --jq '[((.merged // false) | tostring), (.head.ref // ""), (.state // "")] | join("\u001f")' \
        2>/dev/null) || state_line=""
    IFS=$'\x1f' read -r merged head_ref pr_state <<< "$state_line" || true

    if [[ "${merged:-}" != "true" ]]; then
        echo "gh-pr-with-retry: PR #$PR_NUMBER is not merged (state='${pr_state:-unknown}'); passing the original failure through." >&2
        passthrough_failure "$gh_exit"
    fi

    local summary="merge succeeded (confirmed after server error): PR #$PR_NUMBER is merged"
    if [[ "$DELETE_BRANCH" -eq 1 ]]; then
        summary+=$(delete_head_ref "${head_ref:-}")
    fi
    echo "$summary"
    exit 0
}

# PR-text gate (#5133). `closing-refs` and `validate-pr` are not required
# status checks, and `ci-passed` does not re-run on a body edit, so GitHub
# will merge a PR whose text those checks would reject. wait-for-ci.sh only
# narrows the timing windows (#5078, #5090, #5128); an edit made after it
# exits, or a check run GitHub creates late, is never seen. So run both
# checkers against the PR's LIVE title, body and commits right before the
# merge call, and refuse on anything but a clean pass. Each checker reads the
# PR itself (closing-refs over GraphQL with a REST fallback, the title check
# over REST). A checker that cannot read the PR (exit 2) also refuses: an
# unchecked body is exactly what this gate exists to stop.
#
# GH_PR_CLOSING_REFS_CHECK / GH_PR_TITLE_CHECK override the checker paths
# (tests only).
run_pr_text_gate() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local closing_check="${GH_PR_CLOSING_REFS_CHECK:-$script_dir/check-pr-closing-refs.py}"
    local title_check="${GH_PR_TITLE_CHECK:-$script_dir/check-pr-title.sh}"
    local failed=0 rc

    set +e
    "$closing_check" --pr "$PR_NUMBER" --repo "$REPO" >&2
    rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
        failed=1
        echo "gh-pr-with-retry: closing-refs check on PR #$PR_NUMBER's live text failed (exit $rc)." >&2
    fi

    set +e
    "$title_check" --pr "$PR_NUMBER" --repo "$REPO" >&2
    rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
        failed=1
        echo "gh-pr-with-retry: title/body check on PR #$PR_NUMBER's live text failed (exit $rc)." >&2
    fi

    if [[ "$failed" -ne 0 ]]; then
        cat >&2 << EOF
ERROR: refusing to merge PR #$PR_NUMBER: its live title/body/commits fail the PR-text checks (#5133).
Fix:
  Reword the PR as the check output above says, then re-run the merge:
    scripts/gh-pr-with-retry.sh edit $PR_NUMBER --body-file <reworded-body-file>   # or --title "..."
    scripts/gh-pr-with-retry.sh merge $PR_NUMBER --squash --delete-branch
  A closing word in a commit message needs an amended commit and a push instead.
  If a checker could not read the PR (exit 2), wait for the API to recover and retry;
  never merge around this gate with plain 'gh pr merge' or the REST merge endpoint.
EOF
        exit 1
    fi
}

run_merge() {
    run_pr_text_gate

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
        if is_server_error; then
            recover_merge_after_server_error "$gh_exit"
        fi
        passthrough_failure "$gh_exit"
    fi

    echo "gh-pr-with-retry: GraphQL rate-limit exhausted on 'pr merge', falling back to REST API..." >&2

    # Branch-protection gate (#4965). With a repo-admin token and
    # enforce_admins=false, `PUT /pulls/N/merge` does NOT enforce required
    # checks — it is an admin bypass. So before calling it, re-read the PR
    # over REST and refuse unless GitHub reports the PR mergeable through the
    # protected path (mergeable_state clean / unstable / has_hooks; never
    # blocked, behind, dirty, draft or unknown). Then pin the merge to the
    # head SHA we just checked, so a commit pushed in between cannot merge
    # untested.
    local pr_state_line pr_mergeable_state pr_head_sha head_ref
    pr_state_line=$(gh api "/repos/$REPO/pulls/$PR_NUMBER" \
        --jq '[(.mergeable_state // "unknown"), (.head.sha // ""), (.head.ref // "")] | join("\u001f")' \
        2>/dev/null) || pr_state_line=""
    # Unit-separator (non-whitespace) IFS so an empty field is not collapsed.
    IFS=$'\x1f' read -r pr_mergeable_state pr_head_sha head_ref <<< "$pr_state_line" || true
    case "${pr_mergeable_state:-unknown}" in
        clean|unstable|has_hooks)
            ;;
        *)
            echo "ERROR: refusing the REST merge fallback for PR #$PR_NUMBER: mergeable_state='${pr_mergeable_state:-unknown}'." >&2
            echo "  The REST merge endpoint bypasses required checks for an admin token, so it only runs" >&2
            echo "  when GitHub reports the PR mergeable (clean/unstable/has_hooks). Wait for CI with" >&2
            echo "  scripts/wait-for-ci.sh $PR_NUMBER, then retry once the GraphQL quota resets." >&2
            exit 1
            ;;
    esac
    if [[ -z "${pr_head_sha:-}" ]]; then
        echo "ERROR: refusing the REST merge fallback for PR #$PR_NUMBER: could not read the head SHA to pin." >&2
        exit 1
    fi

    set +e
    gh api -X PUT "/repos/$REPO/pulls/$PR_NUMBER/merge" \
        -f "merge_method=squash" \
        -f "sha=$pr_head_sha" \
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
    # The head ref came from the pre-merge REST read above (GraphQL
    # `gh pr view` is unavailable — that is why we're in the fallback).
    # Branch deletion is best-effort: the merge, the caller's primary
    # intent, succeeded, so this exits 0 either way.
    local rest_summary
    rest_summary="merge succeeded (REST fallback): $(cat "$REST_STDOUT_FILE")"
    if [[ "$DELETE_BRANCH" -eq 1 ]]; then
        rest_summary+=$(delete_head_ref "${head_ref:-}")
    fi
    echo "$rest_summary"
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
