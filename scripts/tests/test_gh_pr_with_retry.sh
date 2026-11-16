#!/usr/bin/env bash
# test_gh_pr_with_retry.sh — Tests for scripts/gh-pr-with-retry.sh.
#
# Covers the GraphQL-rate-limit REST fallback path (#4527) for each
# subcommand:
#
#   E   — Usage / argument validation paths.
#   C.* — `create`: success + GraphQL-rate-limit + REST-fail + auth-passthrough.
#   X.* — `edit`:   success + GraphQL-rate-limit + REST-fail + auth-passthrough.
#   M.* — `merge`:  success + GraphQL-rate-limit + REST-fail + auth-passthrough,
#                   plus --delete-branch coverage (success + already-deleted).
#   A.* — `create` passthrough of --assignee/--label/--reviewer/--draft,
#         direct and on the REST fallback (#5122).
#   S.* — `merge` after a 5xx that merged anyway: head-ref delete, no
#         merge retry (#4231, #5122).
#
# All tests run against a PATH-mocked ``gh`` binary — no network. Uses
# the same temp-cleanup helper pattern as test_gh_comment_with_retry.sh.
#
# Usage:
#   scripts/tests/test_gh_pr_with_retry.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/gh-pr-with-retry.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
ORIG_PATH_SAVE=""
restore_path() {
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
    fi
}
register_cleanup_hook restore_path

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

if [[ ! -x "$WRAPPER" ]]; then
    echo "FAIL: $WRAPPER is not executable (or does not exist)" >&2
    exit 1
fi

# ── Set up a mock gh CLI on PATH ──────────────────────────────────────────
#
# The mock supports the call shapes the wrapper exercises:
#
#   1. ``gh pr create --repo X --title T --body-file B --base BB --head HH``
#      Behavior controlled by ``MOCK_PR_CREATE_MODE`` env var:
#        - "success"            : exit 0, print URL on stdout (default).
#        - "graphql_rate_limit" : exit 1, print GraphQL marker on stderr.
#        - "auth"               : exit 4, print "authentication required" on stderr.
#
#   2. ``gh pr edit <N> --repo X [--title T] [--body-file B]``
#      Behavior controlled by ``MOCK_PR_EDIT_MODE`` (same modes as create).
#
#   3. ``gh pr merge <N> --repo X --squash [--delete-branch]``
#      Behavior controlled by ``MOCK_PR_MERGE_MODE`` (same modes as create).
#
#   4. ``gh api -X POST /repos/<owner>/<repo>/pulls --input <file> --jq .html_url``
#      REST fallback for `pr create`. Behavior: ``MOCK_REST_CREATE_MODE``
#      ("success" prints html_url, "fail" prints err on stderr).
#      Side effect: when MOCK_PAYLOAD_DUMP is set, copies --input file
#      content there for later assertion.
#
#   5. ``gh api -X PATCH /repos/<owner>/<repo>/pulls/<N> --input <file> --jq .html_url``
#      REST fallback for `pr edit`. Behavior: ``MOCK_REST_EDIT_MODE``.
#
#   6. ``gh api -X PUT /repos/<owner>/<repo>/pulls/<N>/merge -f merge_method=squash --jq ...``
#      REST fallback for `pr merge`. Behavior: ``MOCK_REST_MERGE_MODE``.
#
#   7. ``gh api /repos/<owner>/<repo>/pulls/<N> --jq <expr>``
#      Pre-merge protection gate read in the REST path (#4965). Runs the
#      wrapper's jq over a PR object built from ``MOCK_MERGEABLE_STATE``
#      (default "clean"), ``MOCK_HEAD_SHA`` and ``MOCK_HEAD_REF``
#      (default "feature-branch").
#
#   8. ``gh api -X DELETE /repos/<owner>/<repo>/git/refs/heads/<ref>``
#      Branch-delete in the REST path. Behavior:
#      ``MOCK_REST_DELETE_MODE`` ("success" exit 0; "already_gone"
#      exit 1 with "Reference does not exist" on stderr; "fail"
#      exit 1 with generic stderr).

MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
ORIG_PATH_SAVE="$PATH"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

cat > "$MOCK_BIN_DIR/gh" << 'MOCKEOF'
#!/usr/bin/env bash
# Record every invocation for assertions.
if [[ -n "${MOCK_INVOCATIONS:-}" ]]; then
    echo "$@" >> "$MOCK_INVOCATIONS"
fi

# ── gh pr create ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "pr" && "${2:-}" == "create" ]]; then
    mode="${MOCK_PR_CREATE_MODE:-success}"
    case "$mode" in
        success)
            echo "https://github.com/judgemind/judgemind/pull/777"
            exit 0
            ;;
        graphql_rate_limit)
            echo "GraphQL: API rate limit already exceeded for user ID 3708633." >&2
            exit 1
            ;;
        auth)
            echo "error: authentication required" >&2
            exit 4
            ;;
        *)
            echo "mock gh: unknown MOCK_PR_CREATE_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh pr edit ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "pr" && "${2:-}" == "edit" ]]; then
    mode="${MOCK_PR_EDIT_MODE:-success}"
    case "$mode" in
        success)
            echo "https://github.com/judgemind/judgemind/pull/${3:-0}"
            exit 0
            ;;
        graphql_rate_limit)
            echo "GraphQL: API rate limit already exceeded for user ID 3708633." >&2
            exit 1
            ;;
        auth)
            echo "error: authentication required" >&2
            exit 4
            ;;
        *)
            echo "mock gh: unknown MOCK_PR_EDIT_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh pr merge ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "pr" && "${2:-}" == "merge" ]]; then
    mode="${MOCK_PR_MERGE_MODE:-success}"
    case "$mode" in
        success)
            # gh pr merge --squash --delete-branch normally prints
            # nothing to stdout on success (or a line like "✓ Squashed
            # and merged"). For the test we just exit 0.
            exit 0
            ;;
        graphql_rate_limit)
            echo "GraphQL: API rate limit already exceeded for user ID 3708633." >&2
            exit 1
            ;;
        auth)
            echo "error: authentication required" >&2
            exit 4
            ;;
        server_502)
            echo "HTTP 502: Bad Gateway (https://api.github.com/graphql)" >&2
            exit 1
            ;;
        server_504_html)
            printf '%s\n' "<!DOCTYPE html>" "<title>Unicorn! &middot; GitHub</title>" \
                "<p>This page is taking too long to load.</p>" >&2
            exit 1
            ;;
        *)
            echo "mock gh: unknown MOCK_PR_MERGE_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh api -X POST /repos/.../pulls (REST fallback for create) ────────────
if [[ "${1:-}" == "api" && "${2:-}" == "-X" && "${3:-}" == "POST" \
        && "${4:-}" =~ ^/repos/.*/pulls$ ]]; then
    # Capture --input payload if MOCK_PAYLOAD_DUMP is set.
    if [[ -n "${MOCK_PAYLOAD_DUMP:-}" ]]; then
        # Walk args looking for "--input <path>".
        for ((i=1; i<=$#; i++)); do
            if [[ "${!i}" == "--input" ]]; then
                next=$((i+1))
                src_path="${!next}"
                if [[ -f "$src_path" ]]; then
                    cp "$src_path" "$MOCK_PAYLOAD_DUMP"
                fi
                break
            fi
        done
    fi
    mode="${MOCK_REST_CREATE_MODE:-success}"
    case "$mode" in
        success)
            echo "https://github.com/judgemind/judgemind/pull/9001"
            exit 0
            ;;
        fail)
            echo "error: REST create secondary failure" >&2
            exit 1
            ;;
        *)
            echo "mock gh: unknown MOCK_REST_CREATE_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh api -X POST assignees / labels / requested_reviewers (#5122) ───────
# Follow-up calls after a REST-fallback create. MOCK_REST_EXTRAS_MODE:
# "success" (default) or "fail".
if [[ "${1:-}" == "api" && "${2:-}" == "-X" && "${3:-}" == "POST" \
        && "${4:-}" =~ ^/repos/.*/(issues/[0-9]+/(assignees|labels)|pulls/[0-9]+/requested_reviewers)$ ]]; then
    if [[ "${MOCK_REST_EXTRAS_MODE:-success}" == "fail" ]]; then
        echo "HTTP 422: Validation Failed" >&2
        exit 1
    fi
    echo '{}'
    exit 0
fi

# ── gh api /user --jq .login (resolve @me on the REST path) ───────────────
if [[ "${1:-}" == "api" && "${2:-}" == "/user" ]]; then
    echo "${MOCK_USER_LOGIN:-mock-login}"
    exit 0
fi

# ── gh api -X PATCH /repos/.../pulls/<N> (REST fallback for edit) ─────────
if [[ "${1:-}" == "api" && "${2:-}" == "-X" && "${3:-}" == "PATCH" \
        && "${4:-}" =~ ^/repos/.*/pulls/[0-9]+$ ]]; then
    if [[ -n "${MOCK_PAYLOAD_DUMP:-}" ]]; then
        for ((i=1; i<=$#; i++)); do
            if [[ "${!i}" == "--input" ]]; then
                next=$((i+1))
                src_path="${!next}"
                if [[ -f "$src_path" ]]; then
                    cp "$src_path" "$MOCK_PAYLOAD_DUMP"
                fi
                break
            fi
        done
    fi
    mode="${MOCK_REST_EDIT_MODE:-success}"
    case "$mode" in
        success)
            # Extract PR number from URL for echoing back.
            pr_n=$(echo "${4}" | sed -E 's|.*/pulls/([0-9]+)$|\1|')
            echo "https://github.com/judgemind/judgemind/pull/${pr_n}"
            exit 0
            ;;
        fail)
            echo "error: REST edit secondary failure" >&2
            exit 1
            ;;
        *)
            echo "mock gh: unknown MOCK_REST_EDIT_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh api -X PUT /repos/.../pulls/<N>/merge (REST fallback for merge) ────
if [[ "${1:-}" == "api" && "${2:-}" == "-X" && "${3:-}" == "PUT" \
        && "${4:-}" =~ ^/repos/.*/pulls/[0-9]+/merge$ ]]; then
    mode="${MOCK_REST_MERGE_MODE:-success}"
    case "$mode" in
        success)
            echo '{"merged":true,"sha":"abc1234567890","message":"Pull Request successfully merged"}'
            exit 0
            ;;
        fail)
            echo "error: REST merge secondary failure" >&2
            exit 1
            ;;
        *)
            echo "mock gh: unknown MOCK_REST_MERGE_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh api -X DELETE /repos/.../git/refs/heads/<ref> (branch delete) ──────
if [[ "${1:-}" == "api" && "${2:-}" == "-X" && "${3:-}" == "DELETE" \
        && "${4:-}" =~ ^/repos/.*/git/refs/heads/.+$ ]]; then
    mode="${MOCK_REST_DELETE_MODE:-success}"
    case "$mode" in
        success)
            exit 0
            ;;
        already_gone)
            cat >&2 << 'GONE_EOF'
HTTP 422: Reference does not exist (https://api.github.com/...)
{"message":"Reference does not exist","documentation_url":"..."}
GONE_EOF
            exit 1
            ;;
        fail)
            echo "error: REST DELETE secondary failure" >&2
            exit 1
            ;;
        *)
            echo "mock gh: unknown MOCK_REST_DELETE_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh api /repos/.../pulls/<N> --jq <expr> (pre-merge gate read) ─────────
# Builds a minimal PR object from MOCK_MERGEABLE_STATE / MOCK_HEAD_SHA /
# MOCK_HEAD_REF and runs the wrapper's own --jq expression over it.
if [[ "${1:-}" == "api" && "${2:-}" =~ ^/repos/.*/pulls/[0-9]+$ ]]; then
    if [[ "${MOCK_PR_READ_MODE:-success}" == "fail" ]]; then
        echo "HTTP 502: Bad Gateway" >&2
        exit 1
    fi
    pr_json=$(jq -cn \
        --arg st "${MOCK_MERGEABLE_STATE:-clean}" \
        --arg sha "${MOCK_HEAD_SHA:-0123456789abcdef0123456789abcdef01234567}" \
        --arg ref "${MOCK_HEAD_REF:-feature-branch}" \
        --argjson merged "${MOCK_MERGED:-false}" \
        --arg state "${MOCK_PR_STATE:-open}" \
        '{mergeable_state: $st, merged: $merged, state: $state, head: {sha: $sha, ref: $ref}}')
    jq_expr=""
    for ((i=1; i<=$#; i++)); do
        if [[ "${!i}" == "--jq" ]]; then
            next=$((i+1))
            jq_expr="${!next}"
            break
        fi
    done
    if [[ -n "$jq_expr" ]]; then
        printf '%s' "$pr_json" | jq -r "$jq_expr"
    else
        printf '%s\n' "$pr_json"
    fi
    exit 0
fi

# Unknown command — fail loudly so the test surfaces it.
echo "mock gh: unhandled command: $*" >&2
exit 127
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

# ── Test E — Usage / argument validation paths ─────────────────────────────

# E.1 — No args prints help and exits 2.
exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.1: 0 args exits 2"
else
    fail "E.1: 0 args exits 2" "expected 2, got $exit_code"
fi

# E.2 — --help exits 0 with usage on stdout.
exit_code=0
help_output=$("$WRAPPER" --help 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 ]] && [[ "$help_output" == *"Usage:"* ]] \
        && [[ "$help_output" == *"create"* ]] \
        && [[ "$help_output" == *"edit"* ]] \
        && [[ "$help_output" == *"merge"* ]]; then
    pass "E.2: --help exits 0 and mentions all three subcommands"
else
    fail "E.2: --help exits 0 and mentions all three subcommands" \
        "exit=$exit_code, output: $help_output"
fi

# E.3 — Unknown subcommand exits 2.
exit_code=0
err_output=$("$WRAPPER" frobnicate 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 2 ]] && [[ "$err_output" == *"unknown subcommand"* ]]; then
    pass "E.3: unknown subcommand exits 2 with descriptive error"
else
    fail "E.3: unknown subcommand exits 2 with descriptive error" \
        "exit=$exit_code, err: $err_output"
fi

# E.4 — `create` missing --title exits 2.
BODY_FILE_E4=$(mktemp)
register_temp_file "$BODY_FILE_E4"
echo "test body" > "$BODY_FILE_E4"
exit_code=0
"$WRAPPER" create --body-file "$BODY_FILE_E4" --base main --head feature \
    > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.4: create missing --title exits 2"
else
    fail "E.4: create missing --title exits 2" "expected 2, got $exit_code"
fi

# E.5 — `create` missing --body-file exits 2.
exit_code=0
"$WRAPPER" create --title "T" --base main --head feature \
    > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.5: create missing --body-file exits 2"
else
    fail "E.5: create missing --body-file exits 2" "expected 2, got $exit_code"
fi

# E.6 — `create` body-file does not exist exits 2.
exit_code=0
err_output=$("$WRAPPER" create --title "T" --body-file /nonexistent/xyz \
    --base main --head feature 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 2 ]] && [[ "$err_output" == *"does not exist"* ]]; then
    pass "E.6: create with missing body-file exits 2 with descriptive error"
else
    fail "E.6: create with missing body-file exits 2 with descriptive error" \
        "exit=$exit_code, err: $err_output"
fi

# E.7 — `edit` with neither --title nor --body-file exits 2.
exit_code=0
"$WRAPPER" edit 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.7: edit with no fields exits 2"
else
    fail "E.7: edit with no fields exits 2" "expected 2, got $exit_code"
fi

# E.8 — `edit` non-numeric PR number exits 2.
exit_code=0
"$WRAPPER" edit abc --title "T" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.8: edit non-numeric PR exits 2"
else
    fail "E.8: edit non-numeric PR exits 2" "expected 2, got $exit_code"
fi

# E.9 — `merge` without --squash exits 2.
exit_code=0
"$WRAPPER" merge 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.9: merge without --squash exits 2"
else
    fail "E.9: merge without --squash exits 2" "expected 2, got $exit_code"
fi

# E.10 — Leading '#' on PR number stripped.
INVOCATIONS_E10="$MOCK_BIN_DIR/invocations_e10.txt"
: > "$INVOCATIONS_E10"
exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_E10" \
    MOCK_PR_MERGE_MODE=success \
    "$WRAPPER" merge "#100" --squash > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]] && grep -qE "^pr merge 100\b" "$INVOCATIONS_E10"; then
    pass "E.10: leading '#' on PR number is stripped"
else
    fail "E.10: leading '#' on PR number is stripped" \
        "exit=$exit_code, invocations: $(cat "$INVOCATIONS_E10")"
fi

# ── Test C — `create` ──────────────────────────────────────────────────────

# C.1 — Success path.
BODY_FILE_C1=$(mktemp)
register_temp_file "$BODY_FILE_C1"
cat > "$BODY_FILE_C1" << 'BODY_EOF'
## Summary
Test PR body for C.1.

Closes #4527
BODY_EOF

INVOCATIONS_C1="$MOCK_BIN_DIR/invocations_c1.txt"
: > "$INVOCATIONS_C1"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_C1" \
    MOCK_PR_CREATE_MODE=success \
    "$WRAPPER" create \
        --title "feat(dx): test create" \
        --body-file "$BODY_FILE_C1" \
        --base main \
        --head feature \
        2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "C.1: create success exits 0"
else
    fail "C.1: create success exits 0" "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"https://github.com/judgemind/judgemind/pull/777"* ]]; then
    pass "C.1: create success prints PR URL on stdout"
else
    fail "C.1: create success prints PR URL on stdout" "got: $stdout_output"
fi

# Verify NO REST fallback was invoked (the marker MOCK_REST_CREATE_MODE wasn't set).
rest_calls=$(awk '/^api / {n++} END {print n+0}' "$INVOCATIONS_C1")
if [[ "$rest_calls" == "0" ]]; then
    pass "C.1: create success makes no REST API calls"
else
    fail "C.1: create success makes no REST API calls" \
        "found $rest_calls api calls: $(cat "$INVOCATIONS_C1")"
fi

# C.2 — GraphQL rate-limit + REST success.
BODY_FILE_C2=$(mktemp)
register_temp_file "$BODY_FILE_C2"
echo "Body content for the C.2 test." > "$BODY_FILE_C2"

PAYLOAD_DUMP_C2=$(mktemp)
register_temp_file "$PAYLOAD_DUMP_C2"

INVOCATIONS_C2="$MOCK_BIN_DIR/invocations_c2.txt"
: > "$INVOCATIONS_C2"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_C2" \
    MOCK_PR_CREATE_MODE=graphql_rate_limit \
    MOCK_REST_CREATE_MODE=success \
    MOCK_PAYLOAD_DUMP="$PAYLOAD_DUMP_C2" \
    "$WRAPPER" create \
        --title "feat(dx): C.2 graphql fallback" \
        --body-file "$BODY_FILE_C2" \
        --base main \
        --head feature-c2 \
        2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "C.2: create GraphQL rate-limit + REST success exits 0"
else
    fail "C.2: create GraphQL rate-limit + REST success exits 0" \
        "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"REST fallback"* ]]; then
    pass "C.2: create stdout names 'REST fallback' on the recovery path"
else
    fail "C.2: create stdout names 'REST fallback' on the recovery path" \
        "got: $stdout_output"
fi

if [[ "$stdout_output" == *"https://github.com/judgemind/judgemind/pull/9001"* ]]; then
    pass "C.2: create stdout includes the REST-posted PR URL"
else
    fail "C.2: create stdout includes the REST-posted PR URL" \
        "got: $stdout_output"
fi

# Confirm wrapper invoked `gh api -X POST /repos/.../pulls`.
if grep -qE "^api -X POST /repos/.*/pulls( |$)" "$INVOCATIONS_C2"; then
    pass "C.2: wrapper invoked 'gh api -X POST /repos/.../pulls'"
else
    fail "C.2: wrapper invoked 'gh api -X POST /repos/.../pulls'" \
        "invocations: $(cat "$INVOCATIONS_C2")"
fi

# Confirm payload contains all four fields with correct values.
if [[ -s "$PAYLOAD_DUMP_C2" ]]; then
    payload_check=$(python3 - "$PAYLOAD_DUMP_C2" << 'PY_CHECK_C2'
import json, sys
data = json.load(open(sys.argv[1]))
ok = (
    data.get("title") == "feat(dx): C.2 graphql fallback"
    and data.get("body") == "Body content for the C.2 test.\n"
    and data.get("head") == "feature-c2"
    and data.get("base") == "main"
)
print("OK" if ok else f"BAD: {data}")
PY_CHECK_C2
)
    if [[ "$payload_check" == "OK" ]]; then
        pass "C.2: REST payload has correct title/body/head/base"
    else
        fail "C.2: REST payload has correct title/body/head/base" "$payload_check"
    fi
else
    fail "C.2: REST payload was captured" "MOCK_PAYLOAD_DUMP file empty"
fi

# C.3 — GraphQL rate-limit + REST also fails.
BODY_FILE_C3=$(mktemp)
register_temp_file "$BODY_FILE_C3"
echo "C.3 body" > "$BODY_FILE_C3"

INVOCATIONS_C3="$MOCK_BIN_DIR/invocations_c3.txt"
: > "$INVOCATIONS_C3"

exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_C3" \
    MOCK_PR_CREATE_MODE=graphql_rate_limit \
    MOCK_REST_CREATE_MODE=fail \
    "$WRAPPER" create \
        --title "C.3" \
        --body-file "$BODY_FILE_C3" \
        --base main \
        --head feature-c3 \
        2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "C.3: create GraphQL rate-limit + REST fail exits non-zero"
else
    fail "C.3: create GraphQL rate-limit + REST fail exits non-zero" \
        "expected non-zero, got $exit_code"
fi

if [[ "$err_output" == *"REST also failed"* ]]; then
    pass "C.3: create stderr names 'REST also failed'"
else
    fail "C.3: create stderr names 'REST also failed'" "got: $err_output"
fi

if [[ "$err_output" == *"GraphQL: API rate limit"* ]] \
        && [[ "$err_output" == *"REST create secondary failure"* ]]; then
    pass "C.3: create stderr includes both gh and REST stderrs"
else
    fail "C.3: create stderr includes both gh and REST stderrs" "got: $err_output"
fi

# C.4 — Auth failure passes through (REST fallback NOT triggered).
BODY_FILE_C4=$(mktemp)
register_temp_file "$BODY_FILE_C4"
echo "C.4 body" > "$BODY_FILE_C4"

INVOCATIONS_C4="$MOCK_BIN_DIR/invocations_c4.txt"
: > "$INVOCATIONS_C4"

exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_C4" \
    MOCK_PR_CREATE_MODE=auth \
    "$WRAPPER" create \
        --title "C.4" \
        --body-file "$BODY_FILE_C4" \
        --base main \
        --head feature-c4 \
        2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 4 ]]; then
    pass "C.4: create auth-fail passes through original exit code (4)"
else
    fail "C.4: create auth-fail passes through original exit code (4)" \
        "expected 4, got $exit_code"
fi

if [[ "$err_output" == *"authentication required"* ]]; then
    pass "C.4: create auth-fail stderr is passed through"
else
    fail "C.4: create auth-fail stderr is passed through" "got: $err_output"
fi

rest_post_calls=$(awk '/^api -X POST / {n++} END {print n+0}' "$INVOCATIONS_C4")
if [[ "$rest_post_calls" == "0" ]]; then
    pass "C.4: create auth-fail makes no REST POST calls"
else
    fail "C.4: create auth-fail makes no REST POST calls" \
        "found $rest_post_calls REST POST calls"
fi

# ── Test X — `edit` ────────────────────────────────────────────────────────

# X.1 — Success path with --body-file only.
BODY_FILE_X1=$(mktemp)
register_temp_file "$BODY_FILE_X1"
echo "X.1 updated body" > "$BODY_FILE_X1"

INVOCATIONS_X1="$MOCK_BIN_DIR/invocations_x1.txt"
: > "$INVOCATIONS_X1"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_X1" \
    MOCK_PR_EDIT_MODE=success \
    "$WRAPPER" edit 200 --body-file "$BODY_FILE_X1" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "X.1: edit success exits 0"
else
    fail "X.1: edit success exits 0" "expected 0, got $exit_code"
fi

if grep -qE "^pr edit 200\b" "$INVOCATIONS_X1" \
        && grep -qE -- "--body-file" "$INVOCATIONS_X1"; then
    pass "X.1: edit invokes 'gh pr edit 200 --body-file ...'"
else
    fail "X.1: edit invokes 'gh pr edit 200 --body-file ...'" \
        "invocations: $(cat "$INVOCATIONS_X1")"
fi

# X.2 — GraphQL rate-limit + REST success (body-only).
BODY_FILE_X2=$(mktemp)
register_temp_file "$BODY_FILE_X2"
echo "X.2 body for edit fallback" > "$BODY_FILE_X2"

PAYLOAD_DUMP_X2=$(mktemp)
register_temp_file "$PAYLOAD_DUMP_X2"

INVOCATIONS_X2="$MOCK_BIN_DIR/invocations_x2.txt"
: > "$INVOCATIONS_X2"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_X2" \
    MOCK_PR_EDIT_MODE=graphql_rate_limit \
    MOCK_REST_EDIT_MODE=success \
    MOCK_PAYLOAD_DUMP="$PAYLOAD_DUMP_X2" \
    "$WRAPPER" edit 250 --body-file "$BODY_FILE_X2" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "X.2: edit GraphQL rate-limit + REST success exits 0"
else
    fail "X.2: edit GraphQL rate-limit + REST success exits 0" \
        "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"REST fallback"* ]]; then
    pass "X.2: edit stdout names 'REST fallback' on the recovery path"
else
    fail "X.2: edit stdout names 'REST fallback' on the recovery path" \
        "got: $stdout_output"
fi

if grep -qE "^api -X PATCH /repos/.*/pulls/250( |$)" "$INVOCATIONS_X2"; then
    pass "X.2: edit invokes 'gh api -X PATCH /repos/.../pulls/250'"
else
    fail "X.2: edit invokes 'gh api -X PATCH /repos/.../pulls/250'" \
        "invocations: $(cat "$INVOCATIONS_X2")"
fi

# Confirm payload has body but NO title (we didn't pass --title).
if [[ -s "$PAYLOAD_DUMP_X2" ]]; then
    payload_check=$(python3 - "$PAYLOAD_DUMP_X2" << 'PY_CHECK_X2'
import json, sys
data = json.load(open(sys.argv[1]))
ok = (
    "body" in data
    and data["body"] == "X.2 body for edit fallback\n"
    and "title" not in data
)
print("OK" if ok else f"BAD: {data}")
PY_CHECK_X2
)
    if [[ "$payload_check" == "OK" ]]; then
        pass "X.2: REST PATCH payload has body but not title (partial PATCH)"
    else
        fail "X.2: REST PATCH payload has body but not title" "$payload_check"
    fi
else
    fail "X.2: REST PATCH payload was captured" "MOCK_PAYLOAD_DUMP file empty"
fi

# X.3 — GraphQL rate-limit + REST success (title + body).
BODY_FILE_X3=$(mktemp)
register_temp_file "$BODY_FILE_X3"
echo "X.3 body content" > "$BODY_FILE_X3"

PAYLOAD_DUMP_X3=$(mktemp)
register_temp_file "$PAYLOAD_DUMP_X3"

exit_code=0
MOCK_PR_EDIT_MODE=graphql_rate_limit \
    MOCK_REST_EDIT_MODE=success \
    MOCK_PAYLOAD_DUMP="$PAYLOAD_DUMP_X3" \
    "$WRAPPER" edit 251 \
        --title "feat: new title" \
        --body-file "$BODY_FILE_X3" \
        > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]] && [[ -s "$PAYLOAD_DUMP_X3" ]]; then
    payload_check=$(python3 - "$PAYLOAD_DUMP_X3" << 'PY_CHECK_X3'
import json, sys
data = json.load(open(sys.argv[1]))
ok = (
    data.get("title") == "feat: new title"
    and data.get("body") == "X.3 body content\n"
)
print("OK" if ok else f"BAD: {data}")
PY_CHECK_X3
)
    if [[ "$payload_check" == "OK" ]]; then
        pass "X.3: edit REST PATCH payload includes both title and body"
    else
        fail "X.3: edit REST PATCH payload includes both title and body" "$payload_check"
    fi
else
    fail "X.3: edit REST PATCH success" "exit=$exit_code"
fi

# X.4 — Auth failure passes through.
BODY_FILE_X4=$(mktemp)
register_temp_file "$BODY_FILE_X4"
echo "X.4 body" > "$BODY_FILE_X4"

exit_code=0
err_output=$(
    MOCK_PR_EDIT_MODE=auth \
    "$WRAPPER" edit 250 --body-file "$BODY_FILE_X4" 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 4 ]] && [[ "$err_output" == *"authentication required"* ]]; then
    pass "X.4: edit auth-fail passes through original exit code (4) and stderr"
else
    fail "X.4: edit auth-fail passes through original exit code (4) and stderr" \
        "exit=$exit_code, err: $err_output"
fi

# ── Test M — `merge` ───────────────────────────────────────────────────────

# M.1 — Success path with --squash --delete-branch.
INVOCATIONS_M1="$MOCK_BIN_DIR/invocations_m1.txt"
: > "$INVOCATIONS_M1"

exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_M1" \
    MOCK_PR_MERGE_MODE=success \
    "$WRAPPER" merge 300 --squash --delete-branch \
    > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "M.1: merge success exits 0"
else
    fail "M.1: merge success exits 0" "expected 0, got $exit_code"
fi

if grep -qE "^pr merge 300 --repo .* --squash --delete-branch" "$INVOCATIONS_M1"; then
    pass "M.1: merge invokes 'gh pr merge 300 --squash --delete-branch'"
else
    fail "M.1: merge invokes 'gh pr merge 300 --squash --delete-branch'" \
        "invocations: $(cat "$INVOCATIONS_M1")"
fi

# Verify NO REST fallback calls happened.
rest_put_calls=$(awk '/^api -X PUT / {n++} END {print n+0}' "$INVOCATIONS_M1")
if [[ "$rest_put_calls" == "0" ]]; then
    pass "M.1: merge success makes no REST PUT calls"
else
    fail "M.1: merge success makes no REST PUT calls" \
        "found $rest_put_calls"
fi

# M.2 — GraphQL rate-limit + REST success + branch-delete success.
INVOCATIONS_M2="$MOCK_BIN_DIR/invocations_m2.txt"
: > "$INVOCATIONS_M2"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_M2" \
    MOCK_PR_MERGE_MODE=graphql_rate_limit \
    MOCK_REST_MERGE_MODE=success \
    MOCK_REST_DELETE_MODE=success \
    MOCK_HEAD_REF="worktree-agent-foo" \
    "$WRAPPER" merge 350 --squash --delete-branch 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "M.2: merge GraphQL rate-limit + REST success + DELETE success exits 0"
else
    fail "M.2: merge GraphQL rate-limit + REST success + DELETE success exits 0" \
        "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"REST fallback"* ]] \
        && [[ "$stdout_output" == *"branch 'worktree-agent-foo' deleted"* ]]; then
    pass "M.2: merge stdout names 'REST fallback' AND branch deletion"
else
    fail "M.2: merge stdout names 'REST fallback' AND branch deletion" \
        "got: $stdout_output"
fi

if grep -qE "^api -X PUT /repos/.*/pulls/350/merge( |$)" "$INVOCATIONS_M2"; then
    pass "M.2: merge invokes 'gh api -X PUT /repos/.../pulls/350/merge'"
else
    fail "M.2: merge invokes 'gh api -X PUT /repos/.../pulls/350/merge'" \
        "invocations: $(cat "$INVOCATIONS_M2")"
fi

if grep -qE "^api -X DELETE /repos/.*/git/refs/heads/worktree-agent-foo( |$)" "$INVOCATIONS_M2"; then
    pass "M.2: merge invokes DELETE /git/refs/heads/<head-ref>"
else
    fail "M.2: merge invokes DELETE /git/refs/heads/<head-ref>" \
        "invocations: $(cat "$INVOCATIONS_M2")"
fi

# M.3 — GraphQL rate-limit + REST success + branch already gone (422).
INVOCATIONS_M3="$MOCK_BIN_DIR/invocations_m3.txt"
: > "$INVOCATIONS_M3"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_M3" \
    MOCK_PR_MERGE_MODE=graphql_rate_limit \
    MOCK_REST_MERGE_MODE=success \
    MOCK_REST_DELETE_MODE=already_gone \
    MOCK_HEAD_REF="dead-branch" \
    "$WRAPPER" merge 351 --squash --delete-branch 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "M.3: merge with already-deleted branch still exits 0"
else
    fail "M.3: merge with already-deleted branch still exits 0" \
        "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"already deleted"* ]]; then
    pass "M.3: merge stdout notes 'already deleted' for the branch"
else
    fail "M.3: merge stdout notes 'already deleted' for the branch" \
        "got: $stdout_output"
fi

# M.4 — GraphQL rate-limit + REST merge fails.
INVOCATIONS_M4="$MOCK_BIN_DIR/invocations_m4.txt"
: > "$INVOCATIONS_M4"

exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_M4" \
    MOCK_PR_MERGE_MODE=graphql_rate_limit \
    MOCK_REST_MERGE_MODE=fail \
    "$WRAPPER" merge 352 --squash --delete-branch 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "M.4: merge GraphQL rate-limit + REST fail exits non-zero"
else
    fail "M.4: merge GraphQL rate-limit + REST fail exits non-zero" \
        "expected non-zero, got $exit_code"
fi

if [[ "$err_output" == *"REST also failed"* ]]; then
    pass "M.4: merge stderr names 'REST also failed'"
else
    fail "M.4: merge stderr names 'REST also failed'" "got: $err_output"
fi

# Verify DELETE was NOT called (merge failed).
delete_calls=$(awk '/^api -X DELETE / {n++} END {print n+0}' "$INVOCATIONS_M4")
if [[ "$delete_calls" == "0" ]]; then
    pass "M.4: merge REST-fail does not invoke DELETE"
else
    fail "M.4: merge REST-fail does not invoke DELETE" \
        "found $delete_calls DELETE calls"
fi

# M.5 — Auth failure passes through (no REST fallback, no DELETE).
INVOCATIONS_M5="$MOCK_BIN_DIR/invocations_m5.txt"
: > "$INVOCATIONS_M5"

exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_M5" \
    MOCK_PR_MERGE_MODE=auth \
    "$WRAPPER" merge 353 --squash --delete-branch 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 4 ]] && [[ "$err_output" == *"authentication required"* ]]; then
    pass "M.5: merge auth-fail passes through original exit code (4) and stderr"
else
    fail "M.5: merge auth-fail passes through original exit code (4) and stderr" \
        "exit=$exit_code, err: $err_output"
fi

api_calls=$(awk '/^api / {n++} END {print n+0}' "$INVOCATIONS_M5")
if [[ "$api_calls" == "0" ]]; then
    pass "M.5: merge auth-fail makes no REST API calls"
else
    fail "M.5: merge auth-fail makes no REST API calls" \
        "found $api_calls api calls"
fi

# M.6 — GraphQL rate-limit + REST success WITHOUT --delete-branch.
INVOCATIONS_M6="$MOCK_BIN_DIR/invocations_m6.txt"
: > "$INVOCATIONS_M6"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_M6" \
    MOCK_PR_MERGE_MODE=graphql_rate_limit \
    MOCK_REST_MERGE_MODE=success \
    "$WRAPPER" merge 354 --squash 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]] && [[ "$stdout_output" == *"REST fallback"* ]]; then
    pass "M.6: merge without --delete-branch (REST fallback) exits 0"
else
    fail "M.6: merge without --delete-branch (REST fallback) exits 0" \
        "exit=$exit_code, stdout: $stdout_output"
fi

# DELETE should NOT be called when --delete-branch was not passed.
delete_calls=$(awk '/^api -X DELETE / {n++} END {print n+0}' "$INVOCATIONS_M6")
if [[ "$delete_calls" == "0" ]]; then
    pass "M.6: merge without --delete-branch makes no DELETE call"
else
    fail "M.6: merge without --delete-branch makes no DELETE call" \
        "found $delete_calls DELETE calls"
fi

# M.7 (#4965) — the REST merge pins the head SHA it checked.
INVOCATIONS_M7="$MOCK_BIN_DIR/invocations_m7.txt"
: > "$INVOCATIONS_M7"

exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_M7" \
MOCK_PR_MERGE_MODE=graphql_rate_limit \
MOCK_REST_MERGE_MODE=success \
MOCK_MERGEABLE_STATE=unstable \
MOCK_HEAD_SHA=feedfacefeedfacefeedfacefeedfacefeedface \
    "$WRAPPER" merge 355 --squash > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]] \
        && grep -qE "^api -X PUT /repos/.*/pulls/355/merge .*-f sha=feedfacefeedfacefeedfacefeedfacefeedface( |$)" "$INVOCATIONS_M7"; then
    pass "M.7: REST merge pins -f sha=<head.sha>"
else
    fail "M.7: REST merge pins -f sha=<head.sha>" \
        "exit=$exit_code invocations: $(cat "$INVOCATIONS_M7")"
fi

# M.8 (#4965) — the REST merge refuses when branch protection is not
# satisfied. It is an admin bypass, so mergeable_state=blocked (a required
# check still running) must never reach the PUT.
for blocked_state in blocked behind dirty unknown draft; do
    INVOCATIONS_M8="$MOCK_BIN_DIR/invocations_m8_${blocked_state}.txt"
    : > "$INVOCATIONS_M8"

    exit_code=0
    err_output=$(
        MOCK_INVOCATIONS="$INVOCATIONS_M8" \
        MOCK_PR_MERGE_MODE=graphql_rate_limit \
        MOCK_REST_MERGE_MODE=success \
        MOCK_MERGEABLE_STATE="$blocked_state" \
        "$WRAPPER" merge 356 --squash --delete-branch 2>&1 >/dev/null
    ) || exit_code=$?

    put_calls=$(awk '/^api -X PUT / {n++} END {print n+0}' "$INVOCATIONS_M8")
    if [[ "$exit_code" -eq 1 ]] && [[ "$put_calls" == "0" ]] \
            && [[ "$err_output" == *"mergeable_state='$blocked_state'"* ]]; then
        pass "M.8: REST merge refuses on mergeable_state=$blocked_state (exit 1, no PUT)"
    else
        fail "M.8: REST merge refuses on mergeable_state=$blocked_state (exit 1, no PUT)" \
            "exit=$exit_code put_calls=$put_calls err: $err_output"
    fi
done

# ── Test A — create passthrough flags (#5122) ─────────────────────────────

BODY_FILE_A=$(mktemp)
register_temp_file "$BODY_FILE_A"
echo "A body" > "$BODY_FILE_A"

# A.1 — --assignee / --label / --reviewer / --draft reach `gh pr create`
# in one call, with no follow-up API writes.
INVOCATIONS_A1="$MOCK_BIN_DIR/invocations_a1.txt"
: > "$INVOCATIONS_A1"
exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_A1" \
    MOCK_PR_CREATE_MODE=success \
    "$WRAPPER" create --title "A.1" --body-file "$BODY_FILE_A" \
        --base main --head feature-a1 \
        --assignee drewthaler --label type/dx --reviewer octocat --draft \
        2>/dev/null
) || exit_code=$?
create_calls=$(awk '/^pr create / {n++} END {print n+0}' "$INVOCATIONS_A1")
api_calls=$(awk '/^api / {n++} END {print n+0}' "$INVOCATIONS_A1")
if [[ "$exit_code" -eq 0 ]] && [[ "$create_calls" == "1" ]] && [[ "$api_calls" == "0" ]] \
        && grep -qE -- "^pr create .* --head feature-a1 --assignee drewthaler --label type/dx --reviewer octocat --draft$" "$INVOCATIONS_A1" \
        && [[ "$stdout_output" == *"/pull/777"* ]]; then
    pass "A.1: create passes --assignee/--label/--reviewer/--draft to one gh pr create call"
else
    fail "A.1: create passes --assignee/--label/--reviewer/--draft to one gh pr create call" \
        "exit=$exit_code invocations: $(cat "$INVOCATIONS_A1")"
fi

# A.2 — repeated and comma-separated values each become their own flag.
INVOCATIONS_A2="$MOCK_BIN_DIR/invocations_a2.txt"
: > "$INVOCATIONS_A2"
exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_A2" \
    "$WRAPPER" create --title "A.2" --body-file "$BODY_FILE_A" \
        --base main --head feature-a2 \
        --assignee alice,bob --assignee carol --label p1 \
        > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]] \
        && grep -qE -- "--assignee alice --assignee bob --assignee carol --label p1$" "$INVOCATIONS_A2"; then
    pass "A.2: repeated and comma-separated --assignee values each pass through"
else
    fail "A.2: repeated and comma-separated --assignee values each pass through" \
        "exit=$exit_code invocations: $(cat "$INVOCATIONS_A2")"
fi

# A.3 — no extras: the gh pr create argv is unchanged from before #5122.
INVOCATIONS_A3="$MOCK_BIN_DIR/invocations_a3.txt"
: > "$INVOCATIONS_A3"
exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_A3" \
    "$WRAPPER" create --title "A.3" --body-file "$BODY_FILE_A" \
        --base main --head feature-a3 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]] && grep -qE -- "--base main --head feature-a3$" "$INVOCATIONS_A3"; then
    pass "A.3: create without extras sends no extra flags"
else
    fail "A.3: create without extras sends no extra flags" \
        "exit=$exit_code invocations: $(cat "$INVOCATIONS_A3")"
fi

# A.4 — --assignee is create-only.
exit_code=0
err_output=$("$WRAPPER" merge 100 --squash --assignee x 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 2 ]] && [[ "$err_output" == *"only supported by 'create'"* ]]; then
    pass "A.4: --assignee on merge exits 2"
else
    fail "A.4: --assignee on merge exits 2" "exit=$exit_code err: $err_output"
fi

# A.5 — --assignee without a value exits 2.
exit_code=0
"$WRAPPER" create --title T --body-file "$BODY_FILE_A" --base main --head f --assignee \
    > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "A.5: --assignee without a value exits 2"
else
    fail "A.5: --assignee without a value exits 2" "expected 2, got $exit_code"
fi

# A.6 — REST fallback applies assignees (with @me resolved), labels and
# reviewers after the PR exists, and sends draft in the payload.
INVOCATIONS_A6="$MOCK_BIN_DIR/invocations_a6.txt"
: > "$INVOCATIONS_A6"
PAYLOAD_DUMP_A6=$(mktemp)
register_temp_file "$PAYLOAD_DUMP_A6"
exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_A6" \
    MOCK_PR_CREATE_MODE=graphql_rate_limit \
    MOCK_REST_CREATE_MODE=success \
    MOCK_PAYLOAD_DUMP="$PAYLOAD_DUMP_A6" \
    MOCK_USER_LOGIN=resolved-me \
    "$WRAPPER" create --title "A.6" --body-file "$BODY_FILE_A" \
        --base main --head feature-a6 \
        --assignee @me --assignee drewthaler --label type/dx --reviewer octocat --draft \
        2>/dev/null
) || exit_code=$?
if [[ "$exit_code" -eq 0 ]] \
        && grep -qE "^api -X POST /repos/judgemind/judgemind/issues/9001/assignees -f assignees\[\]=resolved-me -f assignees\[\]=drewthaler$" "$INVOCATIONS_A6" \
        && grep -qE "^api -X POST /repos/judgemind/judgemind/issues/9001/labels -f labels\[\]=type/dx$" "$INVOCATIONS_A6" \
        && grep -qE "^api -X POST /repos/judgemind/judgemind/pulls/9001/requested_reviewers -f reviewers\[\]=octocat$" "$INVOCATIONS_A6" \
        && [[ "$stdout_output" == *"assignees applied"* ]] \
        && [[ "$stdout_output" == *"labels applied"* ]] \
        && [[ "$stdout_output" == *"reviewers requested"* ]] \
        && jq -e '.draft == true' "$PAYLOAD_DUMP_A6" > /dev/null; then
    pass "A.6: REST fallback applies assignees/labels/reviewers and draft"
else
    fail "A.6: REST fallback applies assignees/labels/reviewers and draft" \
        "exit=$exit_code stdout=$stdout_output invocations: $(cat "$INVOCATIONS_A6") payload: $(cat "$PAYLOAD_DUMP_A6")"
fi

# A.7 — REST fallback: a failed assign warns but exits 0 (the PR exists;
# a retry would open a duplicate).
exit_code=0
err_output=$(
    MOCK_PR_CREATE_MODE=graphql_rate_limit \
    MOCK_REST_CREATE_MODE=success \
    MOCK_REST_EXTRAS_MODE=fail \
    "$WRAPPER" create --title "A.7" --body-file "$BODY_FILE_A" \
        --base main --head feature-a7 --assignee drewthaler 2>&1 >/dev/null
) || exit_code=$?
if [[ "$exit_code" -eq 0 ]] && [[ "$err_output" == *"WARNING: PR #9001 created but assigning failed"* ]]; then
    pass "A.7: REST fallback assign failure warns and exits 0"
else
    fail "A.7: REST fallback assign failure warns and exits 0" "exit=$exit_code err: $err_output"
fi

# A.8 — REST fallback without extras makes no follow-up calls and no draft.
INVOCATIONS_A8="$MOCK_BIN_DIR/invocations_a8.txt"
: > "$INVOCATIONS_A8"
PAYLOAD_DUMP_A8=$(mktemp)
register_temp_file "$PAYLOAD_DUMP_A8"
exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_A8" \
    MOCK_PR_CREATE_MODE=graphql_rate_limit \
    MOCK_REST_CREATE_MODE=success \
    MOCK_PAYLOAD_DUMP="$PAYLOAD_DUMP_A8" \
    "$WRAPPER" create --title "A.8" --body-file "$BODY_FILE_A" \
        --base main --head feature-a8 > /dev/null 2>&1 || exit_code=$?
api_calls=$(awk '/^api / {n++} END {print n+0}' "$INVOCATIONS_A8")
if [[ "$exit_code" -eq 0 ]] && [[ "$api_calls" == "1" ]] \
        && jq -e 'has("draft") | not' "$PAYLOAD_DUMP_A8" > /dev/null; then
    pass "A.8: REST fallback without extras makes only the POST /pulls call"
else
    fail "A.8: REST fallback without extras makes only the POST /pulls call" \
        "exit=$exit_code invocations: $(cat "$INVOCATIONS_A8")"
fi

# ── Test S — merge after a 5xx (#4231, #5122) ─────────────────────────────

# S.1 — 502, but the PR merged: delete the head ref, exit 0, never retry
# the merge and never call the REST merge endpoint.
INVOCATIONS_S1="$MOCK_BIN_DIR/invocations_s1.txt"
: > "$INVOCATIONS_S1"
exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_S1" \
    MOCK_PR_MERGE_MODE=server_502 \
    MOCK_MERGED=true \
    MOCK_PR_STATE=closed \
    MOCK_HEAD_REF=worktree-agent-s1 \
    "$WRAPPER" merge 400 --squash --delete-branch 2>/dev/null
) || exit_code=$?
merge_calls=$(awk '/^pr merge / {n++} END {print n+0}' "$INVOCATIONS_S1")
put_calls=$(awk '/^api -X PUT / {n++} END {print n+0}' "$INVOCATIONS_S1")
if [[ "$exit_code" -eq 0 ]] && [[ "$merge_calls" == "1" ]] && [[ "$put_calls" == "0" ]] \
        && grep -qE "^api /repos/judgemind/judgemind/pulls/400 --jq " "$INVOCATIONS_S1" \
        && grep -qE "^api -X DELETE /repos/judgemind/judgemind/git/refs/heads/worktree-agent-s1$" "$INVOCATIONS_S1" \
        && [[ "$stdout_output" == *"confirmed after server error"* ]] \
        && [[ "$stdout_output" == *"branch 'worktree-agent-s1' deleted"* ]]; then
    pass "S.1: 502 + merged deletes the head branch and exits 0 (no merge retry, no PUT)"
else
    fail "S.1: 502 + merged deletes the head branch and exits 0 (no merge retry, no PUT)" \
        "exit=$exit_code stdout=$stdout_output invocations: $(cat "$INVOCATIONS_S1")"
fi

# S.2 — 502 and the PR is NOT merged: pass the failure through, no DELETE.
INVOCATIONS_S2="$MOCK_BIN_DIR/invocations_s2.txt"
: > "$INVOCATIONS_S2"
exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_S2" \
    MOCK_PR_MERGE_MODE=server_502 \
    MOCK_MERGED=false \
    MOCK_PR_STATE=open \
    "$WRAPPER" merge 401 --squash --delete-branch 2>&1 >/dev/null
) || exit_code=$?
write_calls=$(awk '/^api -X / {n++} END {print n+0}' "$INVOCATIONS_S2")
merge_calls=$(awk '/^pr merge / {n++} END {print n+0}' "$INVOCATIONS_S2")
if [[ "$exit_code" -eq 1 ]] && [[ "$write_calls" == "0" ]] && [[ "$merge_calls" == "1" ]] \
        && [[ "$err_output" == *"HTTP 502"* ]] && [[ "$err_output" == *"is not merged"* ]]; then
    pass "S.2: 502 + not merged passes the failure through with no writes"
else
    fail "S.2: 502 + not merged passes the failure through with no writes" \
        "exit=$exit_code err=$err_output invocations: $(cat "$INVOCATIONS_S2")"
fi

# S.3 — 502 + merged, but the branch is already gone: still exits 0.
exit_code=0
stdout_output=$(
    MOCK_PR_MERGE_MODE=server_502 \
    MOCK_MERGED=true \
    MOCK_REST_DELETE_MODE=already_gone \
    MOCK_HEAD_REF=gone-branch \
    "$WRAPPER" merge 402 --squash --delete-branch 2>/dev/null
) || exit_code=$?
if [[ "$exit_code" -eq 0 ]] && [[ "$stdout_output" == *"branch 'gone-branch' was already deleted"* ]]; then
    pass "S.3: 502 + merged + branch already gone exits 0"
else
    fail "S.3: 502 + merged + branch already gone exits 0" "exit=$exit_code stdout=$stdout_output"
fi

# S.4 — 504 HTML page + merged, no --delete-branch: exit 0, no DELETE.
INVOCATIONS_S4="$MOCK_BIN_DIR/invocations_s4.txt"
: > "$INVOCATIONS_S4"
exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_S4" \
    MOCK_PR_MERGE_MODE=server_504_html \
    MOCK_MERGED=true \
    "$WRAPPER" merge 403 --squash 2>/dev/null
) || exit_code=$?
delete_calls=$(awk '/^api -X DELETE / {n++} END {print n+0}' "$INVOCATIONS_S4")
if [[ "$exit_code" -eq 0 ]] && [[ "$delete_calls" == "0" ]] \
        && [[ "$stdout_output" == *"PR #403 is merged"* ]]; then
    pass "S.4: Unicorn HTML 5xx + merged without --delete-branch exits 0, no DELETE"
else
    fail "S.4: Unicorn HTML 5xx + merged without --delete-branch exits 0, no DELETE" \
        "exit=$exit_code stdout=$stdout_output invocations: $(cat "$INVOCATIONS_S4")"
fi

# S.5 — 502 and the PR read also fails: pass the original failure through.
INVOCATIONS_S5="$MOCK_BIN_DIR/invocations_s5.txt"
: > "$INVOCATIONS_S5"
exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_S5" \
    MOCK_PR_MERGE_MODE=server_502 \
    MOCK_PR_READ_MODE=fail \
    "$WRAPPER" merge 404 --squash --delete-branch > /dev/null 2>&1 || exit_code=$?
write_calls=$(awk '/^api -X / {n++} END {print n+0}' "$INVOCATIONS_S5")
if [[ "$exit_code" -eq 1 ]] && [[ "$write_calls" == "0" ]]; then
    pass "S.5: 502 + unreadable PR passes the failure through with no writes"
else
    fail "S.5: 502 + unreadable PR passes the failure through with no writes" \
        "exit=$exit_code invocations: $(cat "$INVOCATIONS_S5")"
fi

# S.6 — a non-5xx failure (branch protection) is not treated as a 5xx:
# no PR read, no writes.
INVOCATIONS_S6="$MOCK_BIN_DIR/invocations_s6.txt"
: > "$INVOCATIONS_S6"
exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_S6" \
    MOCK_PR_MERGE_MODE=auth \
    MOCK_MERGED=true \
    "$WRAPPER" merge 405 --squash --delete-branch > /dev/null 2>&1 || exit_code=$?
api_calls=$(awk '/^api / {n++} END {print n+0}' "$INVOCATIONS_S6")
if [[ "$exit_code" -eq 4 ]] && [[ "$api_calls" == "0" ]]; then
    pass "S.6: non-5xx merge failure skips the merged-anyway check"
else
    fail "S.6: non-5xx merge failure skips the merged-anyway check" \
        "exit=$exit_code invocations: $(cat "$INVOCATIONS_S6")"
fi

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
