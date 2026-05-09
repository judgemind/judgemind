#!/usr/bin/env bash
# test_gh_comment_with_retry.sh — Tests for scripts/gh-comment-with-retry.sh.
#
# Covers the four code paths called out in issue #4478:
#   A — Success path: gh exits 0, helper prints URL and exits 0.
#   B — 504 + matching last-comment: helper exits 0 with "504 swallowed" log.
#   C — 504 + non-matching last-comment: helper exits non-zero (real failure).
#   D — Other non-zero exit (auth fail / rate limit / etc.): pass-through.
#   E — Usage / argument validation paths.
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
# The mock supports three command shapes the wrapper exercises:
#
#   1. ``gh issue comment <N> --repo <repo> --body-file <path>``
#      Behavior controlled by ``MOCK_COMMENT_MODE`` env var:
#        - "success"  : exit 0, print URL on stdout (default).
#        - "504"      : exit 1, print 504 + Unicorn HTML on stderr.
#        - "auth"     : exit 4, print "authentication required" on stderr
#                       (other-failure passthrough path).
#
#   2. ``gh api /user --jq .login``
#      Always prints ``MOCK_GH_USER`` (default: "test-user") and exits 0.
#
#   3. ``gh api /repos/<owner>/<repo>/issues/<N>/comments?...``
#      Prints the JSON staged in ``MOCK_COMMENTS_FILE`` and exits 0. If
#      ``MOCK_COMMENTS_FILE`` is unset or the file is missing, prints
#      ``[]`` (empty list) and exits 0.

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

# ── gh issue comment <N> --repo <repo> --body-file <path> ──────────────────
if [[ "${1:-}" == "issue" && "${2:-}" == "comment" ]]; then
    mode="${MOCK_COMMENT_MODE:-success}"
    case "$mode" in
        success)
            echo "https://github.com/judgemind/judgemind/issues/${3:-0}#issuecomment-99999"
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

# ── gh api /repos/.../issues/<N>/comments?... ──────────────────────────────
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

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
