#!/usr/bin/env bash
# test_gh_comment_with_retry.sh — Tests for scripts/gh-comment-with-retry.sh.
#
# Covers the code paths from #4478 and #4503, plus the PR-comment
# generalization from #4484:
#   A — Success path: gh exits 0, helper prints URL and exits 0.
#   B — 504 + matching last-comment: helper exits 0 with "504 swallowed" log.
#   C — 504 + non-matching last-comment: helper exits non-zero (real failure).
#   C2 — 504 + no comment by current user: helper exits non-zero.
#   D — Other non-zero exit (auth fail / rate limit / etc.): pass-through.
#   E — Usage / argument validation paths.
#   F — GraphQL rate-limit exhaustion → REST fallback (#4503).
#   P — PR-comment shape via --pr flag (#4484): success, 504-recovery,
#       GraphQL-rate-limit REST fallback, and auth passthrough — exact
#       parity with the issue-comment paths above.
#
# All tests run against a PATH-mocked ``gh`` binary — no network. Uses the
# same temp-cleanup helper pattern as test_block_issue.sh and friends
# (see scripts/tests/_temp_cleanup_helpers.sh, #4343).
#
# Usage:
#   scripts/tests/test_gh_comment_with_retry.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/gh-comment-with-retry.sh"
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

# ── Precondition: wrapper exists and is executable ─────────────────────────

if [[ ! -x "$WRAPPER" ]]; then
    echo "FAIL: $WRAPPER is not executable (or does not exist)" >&2
    exit 1
fi

# ── Set up a mock gh CLI on PATH ──────────────────────────────────────────
#
# The mock supports four command shapes the wrapper exercises:
#
#   1. ``gh issue comment <N> --repo <repo> --body-file <path>``
#      Behavior controlled by ``MOCK_COMMENT_MODE`` env var:
#        - "success"  : exit 0, print URL on stdout (default).
#        - "504"      : exit 1, print 504 + Unicorn HTML on stderr.
#        - "auth"     : exit 4, print "authentication required" on stderr
#                       (other-failure passthrough path).
#
#   2. ``gh pr comment <N> --repo <repo> --body-file <path>`` (#4484)
#      Same MOCK_COMMENT_MODE switch as `gh issue comment`. Branding the
#      stdout URL with ``/pull/<N>`` lets the tests assert the wrapper
#      drove the PR shape rather than the issue shape.
#
#   3. ``gh api /user --jq .login``
#      Always prints ``MOCK_GH_USER`` (default: "test-user") and exits 0.
#
#   4. ``gh api /repos/<owner>/<repo>/issues/<N>/comments?...``
#      Prints the JSON staged in ``MOCK_COMMENTS_FILE`` and exits 0. If
#      ``MOCK_COMMENTS_FILE`` is unset or the file is missing, prints
#      ``[]`` (empty list) and exits 0. PR top-level comments resolve to
#      this same endpoint at the API layer (#4484), so the GET-comments
#      mock and the REST POST mock both serve the PR-comment paths
#      unchanged.

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

# ── gh issue comment <N> ... | gh pr comment <N> ... ──────────────────────
# Both call shapes share the same flag set and the same logical recovery
# paths; the PR shape stamps "/pull/" into the success URL so tests can
# assert the wrapper drove the right subcommand.
if [[ "${1:-}" == "issue" && "${2:-}" == "comment" ]] \
        || [[ "${1:-}" == "pr" && "${2:-}" == "comment" ]]; then
    target_kind="$1"   # "issue" or "pr"
    if [[ "$target_kind" == "pr" ]]; then
        url_path="pull"
    else
        url_path="issues"
    fi
    mode="${MOCK_COMMENT_MODE:-success}"
    case "$mode" in
        success)
            echo "https://github.com/judgemind/judgemind/${url_path}/${3:-0}#issuecomment-99999"
            exit 0
            ;;
        504)
            cat >&2 << 'STDERR_EOF'
HTTP 504: Gateway Timeout (https://api.github.com/repos/judgemind/judgemind/issues/100/comments)
<!DOCTYPE html>
<html>
<head>
<title>Unicorn! &middot; GitHub</title>
</head>
<body>
<h1>This page is taking way too long to load.</h1>
<p>Sorry about that. Please try refreshing and contact us if the problem persists.</p>
</body>
</html>
STDERR_EOF
            exit 1
            ;;
        auth)
            echo "error: authentication required" >&2
            exit 4
            ;;
        graphql_rate_limit)
            echo "GraphQL: API rate limit already exceeded for user ID 3708633." >&2
            exit 1
            ;;
        *)
            echo "mock gh: unknown MOCK_COMMENT_MODE=$mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh api /user --jq .login ───────────────────────────────────────────────
if [[ "${1:-}" == "api" && "${2:-}" == "/user" ]]; then
    echo "${MOCK_GH_USER:-test-user}"
    exit 0
fi

# ── gh api -X POST /repos/.../issues/<N>/comments (REST fallback path) ────
# Distinct from the GET-comments path below: the wrapper invokes this
# only after a GraphQL-rate-limit-exceeded stderr match. Behavior
# controlled by ``MOCK_REST_POST_MODE`` env var:
#   - "success" : exit 0, print html_url on stdout (default).
#   - "fail"    : exit 1, print "error: secondary failure" on stderr.
if [[ "${1:-}" == "api" && "${2:-}" == "-X" && "${3:-}" == "POST" \
        && "${4:-}" =~ ^/repos/.*/issues/[0-9]+/comments$ ]]; then
    rest_mode="${MOCK_REST_POST_MODE:-success}"
    case "$rest_mode" in
        success)
            echo "https://github.com/judgemind/judgemind/issues/100#issuecomment-rest-77"
            exit 0
            ;;
        fail)
            echo "error: secondary REST failure (rate limit or auth)" >&2
            exit 1
            ;;
        *)
            echo "mock gh: unknown MOCK_REST_POST_MODE=$rest_mode" >&2
            exit 99
            ;;
    esac
fi

# ── gh api /repos/.../issues/<N>/comments?... (GET — 504 recovery path) ────
if [[ "${1:-}" == "api" && "${2:-}" =~ ^/repos/.*/issues/[0-9]+/comments ]]; then
    if [[ -n "${MOCK_COMMENTS_FILE:-}" && -f "${MOCK_COMMENTS_FILE}" ]]; then
        cat "${MOCK_COMMENTS_FILE}"
    else
        echo "[]"
    fi
    exit 0
fi

# Unknown command — fail loudly so the test surfaces it.
echo "mock gh: unhandled command: $*" >&2
exit 127
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

# Helper to compute a body file's sha256 (for staging matching mock comments).
sha256_of_file() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

# ── Test E — Usage / argument validation paths ─────────────────────────────

# E.1 — No args prints help to stderr and exits 2.
exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.1: 0 args exits 2"
else
    fail "E.1: 0 args exits 2" "expected 2, got $exit_code"
fi

# E.2 — --help exits 0 and prints usage to stdout.
exit_code=0
help_output=$("$WRAPPER" --help 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 ]] && [[ "$help_output" == *"Usage:"* ]]; then
    pass "E.2: --help exits 0 with usage on stdout"
else
    fail "E.2: --help exits 0 with usage on stdout" "exit=$exit_code, output: $help_output"
fi

# E.3 — Non-numeric issue exits 2.
exit_code=0
err_output=$("$WRAPPER" abc --body-file /tmp/whatever 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 2 ]] && [[ "$err_output" == *"must be a positive integer"* ]]; then
    pass "E.3: non-numeric issue exits 2 with descriptive error"
else
    fail "E.3: non-numeric issue exits 2 with descriptive error" "exit=$exit_code, err: $err_output"
fi

# E.4 — Missing --body-file exits 2.
exit_code=0
"$WRAPPER" 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "E.4: missing --body-file exits 2"
else
    fail "E.4: missing --body-file exits 2" "expected 2, got $exit_code"
fi

# E.5 — Body-file does not exist exits 2.
exit_code=0
err_output=$("$WRAPPER" 100 --body-file /nonexistent/path/here 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 2 ]] && [[ "$err_output" == *"does not exist"* ]]; then
    pass "E.5: missing body-file exits 2 with descriptive error"
else
    fail "E.5: missing body-file exits 2 with descriptive error" "exit=$exit_code, err: $err_output"
fi

# E.6 — Leading '#' on issue arg is stripped.
BODY_FILE_E=$(mktemp)
register_temp_file "$BODY_FILE_E"
echo "test body for E.6" > "$BODY_FILE_E"
INVOCATIONS_E="$MOCK_BIN_DIR/invocations_e.txt"
: > "$INVOCATIONS_E"

exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_E" \
    MOCK_COMMENT_MODE=success \
    "$WRAPPER" "#100" --body-file "$BODY_FILE_E" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]] && grep -qE "^issue comment 100\b" "$INVOCATIONS_E"; then
    pass "E.6: leading '#' on issue arg is stripped (gh called with '100')"
else
    fail "E.6: leading '#' on issue arg is stripped (gh called with '100')" \
        "exit=$exit_code, invocations: $(cat "$INVOCATIONS_E")"
fi

# ── Test A — Success path ─────────────────────────────────────────────────

BODY_FILE_A=$(mktemp)
register_temp_file "$BODY_FILE_A"
echo "Hello from test A — process summary content here." > "$BODY_FILE_A"

INVOCATIONS_A="$MOCK_BIN_DIR/invocations_a.txt"
: > "$INVOCATIONS_A"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_A" \
    MOCK_COMMENT_MODE=success \
    "$WRAPPER" 100 --body-file "$BODY_FILE_A" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "A: success path exits 0"
else
    fail "A: success path exits 0" "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"https://github.com/judgemind/judgemind/issues/100"* ]]; then
    pass "A: success path prints comment URL to stdout"
else
    fail "A: success path prints comment URL to stdout" "got: $stdout_output"
fi

# Verify NO comment-list re-fetch happened (only the single ``gh issue comment``).
# Use awk so a no-match (zero count) doesn't trip pipefail.
api_calls=$(awk '/^api / {n++} END {print n+0}' "$INVOCATIONS_A")
if [[ "$api_calls" == "0" ]]; then
    pass "A: success path makes no extra API calls"
else
    fail "A: success path makes no extra API calls" "found $api_calls api calls: $(cat "$INVOCATIONS_A")"
fi

# ── Test B — 504 + matching last-comment ──────────────────────────────────

BODY_FILE_B=$(mktemp)
register_temp_file "$BODY_FILE_B"
cat > "$BODY_FILE_B" << 'BODY_B_EOF'
## Process Summary

This body's SHA-256 must match the last comment by test-user on
the mock issue. The wrapper should detect 504-after-success and
exit 0 with a "504 swallowed" log line.
BODY_B_EOF

# Stage a comments JSON whose first entry is by test-user with the same body.
COMMENTS_B=$(mktemp)
register_temp_file "$COMMENTS_B"
# Read the body file as a JSON-escaped string via python (consistent with how
# the GitHub API would round-trip the body).
python3 - "$BODY_FILE_B" "$COMMENTS_B" << 'PY_STAGE_B'
import json
import sys
body = open(sys.argv[1]).read()
comments = [
    {
        "html_url": "https://github.com/judgemind/judgemind/issues/100#issuecomment-99",
        "user": {"login": "test-user"},
        "body": body,
        "created_at": "2026-05-08T01:00:00Z",
    },
    {
        "html_url": "https://github.com/judgemind/judgemind/issues/100#issuecomment-98",
        "user": {"login": "someone-else"},
        "body": "an earlier unrelated comment",
        "created_at": "2026-05-08T00:30:00Z",
    },
]
with open(sys.argv[2], "w") as fh:
    json.dump(comments, fh)
PY_STAGE_B

INVOCATIONS_B="$MOCK_BIN_DIR/invocations_b.txt"
: > "$INVOCATIONS_B"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_B" \
    MOCK_COMMENT_MODE=504 \
    MOCK_GH_USER="test-user" \
    MOCK_COMMENTS_FILE="$COMMENTS_B" \
    "$WRAPPER" 100 --body-file "$BODY_FILE_B" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "B: 504 + matching last-comment exits 0"
else
    fail "B: 504 + matching last-comment exits 0" "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"504 swallowed"* ]]; then
    pass "B: stdout names '504 swallowed' on the recovery path"
else
    fail "B: stdout names '504 swallowed' on the recovery path" "got: $stdout_output"
fi

if [[ "$stdout_output" == *"https://github.com/judgemind/judgemind/issues/100#issuecomment-99"* ]]; then
    pass "B: stdout includes the matched comment URL"
else
    fail "B: stdout includes the matched comment URL" "got: $stdout_output"
fi

# ── Test C — 504 + non-matching last-comment (real failure) ───────────────

BODY_FILE_C=$(mktemp)
register_temp_file "$BODY_FILE_C"
echo "intended body for test C — should NOT match the staged comment" > "$BODY_FILE_C"

# Stage a comments JSON whose first test-user comment has DIFFERENT body.
COMMENTS_C=$(mktemp)
register_temp_file "$COMMENTS_C"
cat > "$COMMENTS_C" << 'COMMENTS_C_EOF'
[
  {
    "html_url": "https://github.com/judgemind/judgemind/issues/100#issuecomment-50",
    "user": {"login": "test-user"},
    "body": "an OLDER unrelated comment we posted earlier",
    "created_at": "2026-05-08T00:00:00Z"
  }
]
COMMENTS_C_EOF

exit_code=0
err_output=$(
    MOCK_COMMENT_MODE=504 \
    MOCK_GH_USER="test-user" \
    MOCK_COMMENTS_FILE="$COMMENTS_C" \
    "$WRAPPER" 100 --body-file "$BODY_FILE_C" 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "C: 504 + non-matching last-comment exits non-zero"
else
    fail "C: 504 + non-matching last-comment exits non-zero" "expected non-zero, got $exit_code"
fi

if [[ "$err_output" == *"does not match body-file content"* ]]; then
    pass "C: stderr names the SHA mismatch"
else
    fail "C: stderr names the SHA mismatch" "got: $err_output"
fi

# ── Test C2 — 504 + no comment by current user ────────────────────────────

# Edge case: stderr matched 504, but the comment list has zero comments by us.
BODY_FILE_C2=$(mktemp)
register_temp_file "$BODY_FILE_C2"
echo "test body C2" > "$BODY_FILE_C2"

COMMENTS_C2=$(mktemp)
register_temp_file "$COMMENTS_C2"
cat > "$COMMENTS_C2" << 'COMMENTS_C2_EOF'
[
  {
    "html_url": "https://github.com/judgemind/judgemind/issues/100#issuecomment-1",
    "user": {"login": "someone-else"},
    "body": "a comment by another user",
    "created_at": "2026-05-08T00:00:00Z"
  }
]
COMMENTS_C2_EOF

exit_code=0
err_output=$(
    MOCK_COMMENT_MODE=504 \
    MOCK_GH_USER="test-user" \
    MOCK_COMMENTS_FILE="$COMMENTS_C2" \
    "$WRAPPER" 100 --body-file "$BODY_FILE_C2" 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "C2: 504 + no comment by current user exits non-zero"
else
    fail "C2: 504 + no comment by current user exits non-zero" "expected non-zero, got $exit_code"
fi

if [[ "$err_output" == *"no recent comment by"* ]]; then
    pass "C2: stderr names the missing-by-user case"
else
    fail "C2: stderr names the missing-by-user case" "got: $err_output"
fi

# ── Test D — Other non-zero exit (auth fail) — pass-through ───────────────

BODY_FILE_D=$(mktemp)
register_temp_file "$BODY_FILE_D"
echo "test body D" > "$BODY_FILE_D"

INVOCATIONS_D="$MOCK_BIN_DIR/invocations_d.txt"
: > "$INVOCATIONS_D"

exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_D" \
    MOCK_COMMENT_MODE=auth \
    "$WRAPPER" 100 --body-file "$BODY_FILE_D" 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 4 ]]; then
    pass "D: auth-fail (other non-zero) passes through original exit code"
else
    fail "D: auth-fail (other non-zero) passes through original exit code" "expected 4, got $exit_code"
fi

if [[ "$err_output" == *"authentication required"* ]]; then
    pass "D: auth-fail stderr is passed through"
else
    fail "D: auth-fail stderr is passed through" "got: $err_output"
fi

# Verify NO recovery API calls happened — auth failure doesn't trigger
# the 504-swallow path. Use awk to avoid grep's exit-1-on-no-match tripping pipefail.
recovery_calls=$(awk '/^api / {n++} END {print n+0}' "$INVOCATIONS_D")
if [[ "$recovery_calls" == "0" ]]; then
    pass "D: auth-fail makes no recovery API calls"
else
    fail "D: auth-fail makes no recovery API calls" "found $recovery_calls api calls"
fi

# ── Test F — GraphQL rate-limit exhaustion → REST fallback (#4503) ────────

# F.1 — gh issue comment hits GraphQL quota; REST fallback succeeds.
BODY_FILE_F1=$(mktemp)
register_temp_file "$BODY_FILE_F1"
echo "test body F.1 — GraphQL exhausted, REST should rescue" > "$BODY_FILE_F1"

INVOCATIONS_F1="$MOCK_BIN_DIR/invocations_f1.txt"
: > "$INVOCATIONS_F1"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_F1" \
    MOCK_COMMENT_MODE=graphql_rate_limit \
    MOCK_REST_POST_MODE=success \
    "$WRAPPER" 100 --body-file "$BODY_FILE_F1" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "F.1: GraphQL rate-limit + REST success exits 0"
else
    fail "F.1: GraphQL rate-limit + REST success exits 0" "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"REST fallback"* ]]; then
    pass "F.1: stdout names 'REST fallback' on the recovery path"
else
    fail "F.1: stdout names 'REST fallback' on the recovery path" "got: $stdout_output"
fi

if [[ "$stdout_output" == *"https://github.com/judgemind/judgemind/issues/100#issuecomment-rest-77"* ]]; then
    pass "F.1: stdout includes the REST-posted comment URL"
else
    fail "F.1: stdout includes the REST-posted comment URL" "got: $stdout_output"
fi

# Verify the wrapper actually invoked `gh api -X POST /repos/.../issues/100/comments`
# with a body=@<file> form arg. Both signals must be present in the recorded invocations.
if grep -qE "^api -X POST /repos/.*/issues/100/comments( |$)" "$INVOCATIONS_F1"; then
    pass "F.1: wrapper invoked 'gh api -X POST /repos/.../issues/100/comments'"
else
    fail "F.1: wrapper invoked 'gh api -X POST /repos/.../issues/100/comments'" \
        "invocations: $(cat "$INVOCATIONS_F1")"
fi

if grep -qE "body=@.*$(basename "$BODY_FILE_F1")" "$INVOCATIONS_F1"; then
    pass "F.1: wrapper passed -F body=@<body-file> to gh api"
else
    fail "F.1: wrapper passed -F body=@<body-file> to gh api" \
        "invocations: $(cat "$INVOCATIONS_F1")"
fi

# Verify the 504-recovery path was NOT taken — there should be no GET on
# /repos/.../comments and no /user lookup (those are 504-path artifacts).
graphql_path_only_calls=$(awk '/^api \/repos\/.*\/comments\?/ {n++} /^api \/user/ {n++} END {print n+0}' "$INVOCATIONS_F1")
if [[ "$graphql_path_only_calls" == "0" ]]; then
    pass "F.1: 504-recovery path was NOT exercised (no GET comments / no /user)"
else
    fail "F.1: 504-recovery path was NOT exercised (no GET comments / no /user)" \
        "found $graphql_path_only_calls 504-recovery calls"
fi

# F.2 — gh issue comment hits GraphQL quota; REST fallback ALSO fails.
BODY_FILE_F2=$(mktemp)
register_temp_file "$BODY_FILE_F2"
echo "test body F.2 — both paths fail" > "$BODY_FILE_F2"

INVOCATIONS_F2="$MOCK_BIN_DIR/invocations_f2.txt"
: > "$INVOCATIONS_F2"

exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_F2" \
    MOCK_COMMENT_MODE=graphql_rate_limit \
    MOCK_REST_POST_MODE=fail \
    "$WRAPPER" 100 --body-file "$BODY_FILE_F2" 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "F.2: GraphQL rate-limit + REST fail exits non-zero"
else
    fail "F.2: GraphQL rate-limit + REST fail exits non-zero" "expected non-zero, got $exit_code"
fi

if [[ "$err_output" == *"REST also failed"* ]]; then
    pass "F.2: stderr names 'REST also failed' to disambiguate from gh failure"
else
    fail "F.2: stderr names 'REST also failed' to disambiguate from gh failure" "got: $err_output"
fi

if [[ "$err_output" == *"GraphQL: API rate limit"* ]]; then
    pass "F.2: stderr surfaces the original GraphQL rate-limit error for context"
else
    fail "F.2: stderr surfaces the original GraphQL rate-limit error for context" "got: $err_output"
fi

if [[ "$err_output" == *"secondary REST failure"* ]]; then
    pass "F.2: stderr surfaces the REST-side failure too"
else
    fail "F.2: stderr surfaces the REST-side failure too" "got: $err_output"
fi

# F.3 — Generic GraphQL error (NOT rate-limit) should NOT trigger REST fallback.
# We piggyback on the existing 'auth' mode which produces a non-rate-limit error.
# Already covered indirectly by Test D, but verify the explicit
# trigger-only-on-rate-limit-marker decision rule with a positive assertion.
BODY_FILE_F3=$(mktemp)
register_temp_file "$BODY_FILE_F3"
echo "test body F.3" > "$BODY_FILE_F3"

INVOCATIONS_F3="$MOCK_BIN_DIR/invocations_f3.txt"
: > "$INVOCATIONS_F3"

exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_F3" \
    MOCK_COMMENT_MODE=auth \
    "$WRAPPER" 100 --body-file "$BODY_FILE_F3" >/dev/null 2>&1 || exit_code=$?

# Auth failure should still pass through (exit 4) — REST fallback NOT invoked.
if [[ "$exit_code" -eq 4 ]]; then
    pass "F.3: auth-fail does NOT trigger REST fallback (passthrough preserved)"
else
    fail "F.3: auth-fail does NOT trigger REST fallback (passthrough preserved)" \
        "expected 4, got $exit_code"
fi

rest_post_calls=$(awk '/^api -X POST / {n++} END {print n+0}' "$INVOCATIONS_F3")
if [[ "$rest_post_calls" == "0" ]]; then
    pass "F.3: auth-fail makes no REST POST calls"
else
    fail "F.3: auth-fail makes no REST POST calls" \
        "found $rest_post_calls REST POST calls"
fi

# ── Test P — --pr flag drives `gh pr comment` (#4484) ─────────────────────
#
# Acceptance criteria (issue #4484): test coverage parity with the
# issue-comment path — at minimum success + 504+matching + 504+non-
# matching + GraphQL-rate-limit + other-non-zero. The PR-comment shape
# uses the SAME REST endpoint internally, so the recovery paths are the
# same; we just need to confirm the wrapper drove the right gh
# subcommand (asserted via `^pr comment ` invocation prefix and the
# `/pull/<N>` URL path the mock stamps for the PR shape).

# P.1 — Success path under --pr.
BODY_FILE_P1=$(mktemp)
register_temp_file "$BODY_FILE_P1"
echo "test body P.1 — PR success path" > "$BODY_FILE_P1"

INVOCATIONS_P1="$MOCK_BIN_DIR/invocations_p1.txt"
: > "$INVOCATIONS_P1"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_P1" \
    MOCK_COMMENT_MODE=success \
    "$WRAPPER" --pr 200 --body-file "$BODY_FILE_P1" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "P.1: --pr success path exits 0"
else
    fail "P.1: --pr success path exits 0" "expected 0, got $exit_code"
fi

if grep -qE "^pr comment 200\b" "$INVOCATIONS_P1"; then
    pass "P.1: --pr drove 'gh pr comment 200' (not 'gh issue comment')"
else
    fail "P.1: --pr drove 'gh pr comment 200' (not 'gh issue comment')" \
        "invocations: $(cat "$INVOCATIONS_P1")"
fi

if [[ "$stdout_output" == *"https://github.com/judgemind/judgemind/pull/200"* ]]; then
    pass "P.1: stdout includes the PR URL (mock stamp /pull/<N>)"
else
    fail "P.1: stdout includes the PR URL (mock stamp /pull/<N>)" "got: $stdout_output"
fi

# P.1b — Default (no --pr) still drives `gh issue comment` — backwards-compat guard.
BODY_FILE_P1B=$(mktemp)
register_temp_file "$BODY_FILE_P1B"
echo "test body P.1b — backwards-compat guard" > "$BODY_FILE_P1B"

INVOCATIONS_P1B="$MOCK_BIN_DIR/invocations_p1b.txt"
: > "$INVOCATIONS_P1B"

exit_code=0
MOCK_INVOCATIONS="$INVOCATIONS_P1B" \
    MOCK_COMMENT_MODE=success \
    "$WRAPPER" 200 --body-file "$BODY_FILE_P1B" >/dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]] && grep -qE "^issue comment 200\b" "$INVOCATIONS_P1B"; then
    pass "P.1b: default (no --pr) still drives 'gh issue comment' (backwards-compat)"
else
    fail "P.1b: default (no --pr) still drives 'gh issue comment' (backwards-compat)" \
        "exit=$exit_code, invocations: $(cat "$INVOCATIONS_P1B")"
fi

# P.2 — 504 + matching last-comment under --pr exits 0.
BODY_FILE_P2=$(mktemp)
register_temp_file "$BODY_FILE_P2"
cat > "$BODY_FILE_P2" << 'BODY_P2_EOF'
## PR comment — 504-after-success recovery test
This body's SHA-256 must match the last comment by test-user on the
mock PR. The wrapper should detect 504-after-success and exit 0.
BODY_P2_EOF

COMMENTS_P2=$(mktemp)
register_temp_file "$COMMENTS_P2"
python3 - "$BODY_FILE_P2" "$COMMENTS_P2" << 'PY_STAGE_P2'
import json
import sys
body = open(sys.argv[1]).read()
comments = [
    {
        "html_url": "https://github.com/judgemind/judgemind/pull/200#issuecomment-99",
        "user": {"login": "test-user"},
        "body": body,
        "created_at": "2026-05-08T01:00:00Z",
    },
]
with open(sys.argv[2], "w") as fh:
    json.dump(comments, fh)
PY_STAGE_P2

INVOCATIONS_P2="$MOCK_BIN_DIR/invocations_p2.txt"
: > "$INVOCATIONS_P2"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_P2" \
    MOCK_COMMENT_MODE=504 \
    MOCK_GH_USER="test-user" \
    MOCK_COMMENTS_FILE="$COMMENTS_P2" \
    "$WRAPPER" --pr 200 --body-file "$BODY_FILE_P2" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "P.2: --pr 504 + matching last-comment exits 0"
else
    fail "P.2: --pr 504 + matching last-comment exits 0" "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"504 swallowed"* ]]; then
    pass "P.2: --pr stdout names '504 swallowed' on the recovery path"
else
    fail "P.2: --pr stdout names '504 swallowed' on the recovery path" "got: $stdout_output"
fi

if [[ "$stdout_output" == *"https://github.com/judgemind/judgemind/pull/200#issuecomment-99"* ]]; then
    pass "P.2: --pr stdout includes the matched PR comment URL"
else
    fail "P.2: --pr stdout includes the matched PR comment URL" "got: $stdout_output"
fi

# Confirm the wrapper invoked `gh pr comment` first (not `gh issue comment`).
if grep -qE "^pr comment 200\b" "$INVOCATIONS_P2"; then
    pass "P.2: --pr drove 'gh pr comment' before recovery"
else
    fail "P.2: --pr drove 'gh pr comment' before recovery" \
        "invocations: $(cat "$INVOCATIONS_P2")"
fi

# P.3 — 504 + non-matching last-comment under --pr exits non-zero.
BODY_FILE_P3=$(mktemp)
register_temp_file "$BODY_FILE_P3"
echo "intended body for P.3 — should NOT match staged comment" > "$BODY_FILE_P3"

COMMENTS_P3=$(mktemp)
register_temp_file "$COMMENTS_P3"
cat > "$COMMENTS_P3" << 'COMMENTS_P3_EOF'
[
  {
    "html_url": "https://github.com/judgemind/judgemind/pull/200#issuecomment-50",
    "user": {"login": "test-user"},
    "body": "an OLDER unrelated comment we posted earlier",
    "created_at": "2026-05-08T00:00:00Z"
  }
]
COMMENTS_P3_EOF

exit_code=0
err_output=$(
    MOCK_COMMENT_MODE=504 \
    MOCK_GH_USER="test-user" \
    MOCK_COMMENTS_FILE="$COMMENTS_P3" \
    "$WRAPPER" --pr 200 --body-file "$BODY_FILE_P3" 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "P.3: --pr 504 + non-matching last-comment exits non-zero"
else
    fail "P.3: --pr 504 + non-matching last-comment exits non-zero" \
        "expected non-zero, got $exit_code"
fi

if [[ "$err_output" == *"does not match body-file content"* ]]; then
    pass "P.3: --pr stderr names the SHA mismatch"
else
    fail "P.3: --pr stderr names the SHA mismatch" "got: $err_output"
fi

# P.4 — Auth failure under --pr passes through original exit code.
BODY_FILE_P4=$(mktemp)
register_temp_file "$BODY_FILE_P4"
echo "test body P.4" > "$BODY_FILE_P4"

INVOCATIONS_P4="$MOCK_BIN_DIR/invocations_p4.txt"
: > "$INVOCATIONS_P4"

exit_code=0
err_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_P4" \
    MOCK_COMMENT_MODE=auth \
    "$WRAPPER" --pr 200 --body-file "$BODY_FILE_P4" 2>&1 >/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 4 ]]; then
    pass "P.4: --pr auth-fail passes through original exit code (4)"
else
    fail "P.4: --pr auth-fail passes through original exit code (4)" \
        "expected 4, got $exit_code"
fi

if [[ "$err_output" == *"authentication required"* ]]; then
    pass "P.4: --pr auth-fail stderr is passed through"
else
    fail "P.4: --pr auth-fail stderr is passed through" "got: $err_output"
fi

# P.5 — GraphQL rate-limit exhaustion under --pr → REST fallback success.
BODY_FILE_P5=$(mktemp)
register_temp_file "$BODY_FILE_P5"
echo "test body P.5 — GraphQL exhausted on PR comment, REST should rescue" > "$BODY_FILE_P5"

INVOCATIONS_P5="$MOCK_BIN_DIR/invocations_p5.txt"
: > "$INVOCATIONS_P5"

exit_code=0
stdout_output=$(
    MOCK_INVOCATIONS="$INVOCATIONS_P5" \
    MOCK_COMMENT_MODE=graphql_rate_limit \
    MOCK_REST_POST_MODE=success \
    "$WRAPPER" --pr 200 --body-file "$BODY_FILE_P5" 2>/dev/null
) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "P.5: --pr GraphQL rate-limit + REST success exits 0"
else
    fail "P.5: --pr GraphQL rate-limit + REST success exits 0" \
        "expected 0, got $exit_code"
fi

if [[ "$stdout_output" == *"REST fallback"* ]]; then
    pass "P.5: --pr stdout names 'REST fallback' on the recovery path"
else
    fail "P.5: --pr stdout names 'REST fallback' on the recovery path" "got: $stdout_output"
fi

# Confirm the REST fallback hit /repos/.../issues/200/comments — PR
# top-level comments resolve to the same endpoint as issue comments
# (#4484), which is the structural reason the same wrapper covers both.
if grep -qE "^api -X POST /repos/.*/issues/200/comments( |$)" "$INVOCATIONS_P5"; then
    pass "P.5: --pr REST fallback hits /repos/.../issues/200/comments (shared endpoint)"
else
    fail "P.5: --pr REST fallback hits /repos/.../issues/200/comments (shared endpoint)" \
        "invocations: $(cat "$INVOCATIONS_P5")"
fi

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
