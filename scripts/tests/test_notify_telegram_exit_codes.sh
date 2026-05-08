#!/usr/bin/env bash
# test_notify_telegram_exit_codes.sh — Tests for scripts/notify-telegram.sh exit codes
#
# Issue #3061. Asserts the script's exit-code contract:
#
#   0 — at least one recipient received HTTP 200
#   1 — usage error (no args)
#   2 — all sends failed (every recipient non-200)
#   3 — secret fetch failed (aws returns non-zero)
#   4 — bot_token empty in secret
#   5 — allowed_user_ids empty in secret
#
# Strategy: run the script with a temp PATH that shadows `aws` and `curl`
# with bash-function stubs. Each test controls the stubs' behavior via
# env vars so we never touch real AWS or real Telegram.
#
# Usage:
#   scripts/tests/test_notify_telegram_exit_codes.sh
#
# Exit codes:
#   0 — all tests passed
#   1 — one or more tests failed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTIFY_SCRIPT="$SCRIPT_DIR/notify-telegram.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()

cleanup() {
    set +eu
    # ``${TEMP_DIRS[@]+...}`` guards empty-array iteration under
    # bash 3.2 + set -u (see #4336).
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

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

# Create a temp "bin" directory with stub `aws` and `curl` commands.
# The stubs' behavior is controlled by env vars exported by each test:
#   STUB_AWS_EXIT      — exit code for the `aws` stub (default 0)
#   STUB_AWS_STDOUT    — stdout for the `aws` stub (default: valid secret JSON)
#   STUB_CURL_HTTP     — value `curl` writes for `-w '%{http_code}'` (default 200)
make_stub_bin() {
    local bindir
    bindir=$(mktemp -d)
    TEMP_DIRS+=("$bindir")

    cat > "$bindir/aws" <<'AWS_STUB'
#!/usr/bin/env bash
# Stub `aws` for notify-telegram.sh tests. Emits STUB_AWS_STDOUT
# and exits with STUB_AWS_EXIT.
exit_code="${STUB_AWS_EXIT:-0}"
default_secret='{"bot_token":"test-token","allowed_user_ids":[42,43]}'
if [ "$exit_code" = "0" ]; then
    printf '%s' "${STUB_AWS_STDOUT:-$default_secret}"
else
    echo "stub-aws: simulated failure (STUB_AWS_EXIT=$exit_code)" >&2
fi
exit "$exit_code"
AWS_STUB
    chmod +x "$bindir/aws"

    cat > "$bindir/curl" <<'CURL_STUB'
#!/usr/bin/env bash
# Stub `curl` for notify-telegram.sh tests. Writes STUB_CURL_HTTP to
# stdout (the script uses -w '%{http_code}' -o /dev/null so we only
# emit the HTTP code). Always exits 0.
printf '%s' "${STUB_CURL_HTTP:-200}"
CURL_STUB
    chmod +x "$bindir/curl"

    echo "$bindir"
}

# Run the notify script with a stubbed PATH. Echo exit code to stdout.
run_notify() {
    local bindir="$1"
    shift
    local exit_code=0
    PATH="$bindir:$PATH" "$NOTIFY_SCRIPT" "$@" > /dev/null 2>&1 || exit_code=$?
    echo "$exit_code"
}

# ── Tests ──────────────────────────────────────────────────────────────────

test_usage_error_exits_1() {
    local bindir
    bindir=$(make_stub_bin)
    local ec=0
    PATH="$bindir:$PATH" "$NOTIFY_SCRIPT" > /dev/null 2>&1 || ec=$?
    if [ "$ec" = "1" ]; then
        pass "usage error (no args) exits 1"
    else
        fail "usage error (no args) exits 1" "got $ec, expected 1"
    fi
}

test_secret_fetch_failure_exits_3() {
    local bindir
    bindir=$(make_stub_bin)
    local ec
    ec=$(STUB_AWS_EXIT=1 run_notify "$bindir" "test message")
    if [ "$ec" = "3" ]; then
        pass "secret fetch failure exits 3"
    else
        fail "secret fetch failure exits 3" "got $ec, expected 3"
    fi
}

test_empty_bot_token_exits_4() {
    local bindir
    bindir=$(make_stub_bin)
    local ec
    ec=$(STUB_AWS_STDOUT='{"bot_token":"","allowed_user_ids":[42]}' \
        run_notify "$bindir" "test message")
    if [ "$ec" = "4" ]; then
        pass "empty bot_token exits 4"
    else
        fail "empty bot_token exits 4" "got $ec, expected 4"
    fi
}

test_missing_bot_token_key_exits_4() {
    # When the key is absent, python3 .get('bot_token','') returns ''.
    local bindir
    bindir=$(make_stub_bin)
    local ec
    ec=$(STUB_AWS_STDOUT='{"allowed_user_ids":[42]}' \
        run_notify "$bindir" "test message")
    if [ "$ec" = "4" ]; then
        pass "missing bot_token key exits 4"
    else
        fail "missing bot_token key exits 4" "got $ec, expected 4"
    fi
}

test_empty_user_ids_exits_5() {
    local bindir
    bindir=$(make_stub_bin)
    local ec
    ec=$(STUB_AWS_STDOUT='{"bot_token":"test-token","allowed_user_ids":[]}' \
        run_notify "$bindir" "test message")
    if [ "$ec" = "5" ]; then
        pass "empty allowed_user_ids exits 5"
    else
        fail "empty allowed_user_ids exits 5" "got $ec, expected 5"
    fi
}

test_missing_user_ids_key_exits_5() {
    local bindir
    bindir=$(make_stub_bin)
    local ec
    ec=$(STUB_AWS_STDOUT='{"bot_token":"test-token"}' \
        run_notify "$bindir" "test message")
    if [ "$ec" = "5" ]; then
        pass "missing allowed_user_ids key exits 5"
    else
        fail "missing allowed_user_ids key exits 5" "got $ec, expected 5"
    fi
}

test_happy_path_exits_0() {
    local bindir
    bindir=$(make_stub_bin)
    # Default stub secrets + default STUB_CURL_HTTP=200.
    local ec
    ec=$(run_notify "$bindir" "test message")
    if [ "$ec" = "0" ]; then
        pass "happy path (200 to all recipients) exits 0"
    else
        fail "happy path exits 0" "got $ec, expected 0"
    fi
}

test_all_send_fail_exits_2() {
    local bindir
    bindir=$(make_stub_bin)
    # Stub curl returns 500 for every recipient.
    local ec
    ec=$(STUB_CURL_HTTP=500 run_notify "$bindir" "test message")
    if [ "$ec" = "2" ]; then
        pass "all sends failed exits 2"
    else
        fail "all sends failed exits 2" "got $ec, expected 2"
    fi
}

test_empty_message_exits_0() {
    # The empty-message short-circuit is intentionally kept as exit 0
    # (caller-side — "I handed you a blank message, nothing to do").
    # The issue's exit-0-is-a-lie concern is about the CONFIG paths,
    # not this argv-validation short-circuit.
    local bindir
    bindir=$(make_stub_bin)
    local ec
    ec=$(run_notify "$bindir" "")
    if [ "$ec" = "0" ]; then
        pass "empty message exits 0"
    else
        fail "empty message exits 0" "got $ec, expected 0"
    fi
}

test_stderr_mentions_not_sent_on_config_paths() {
    # Regression: the stderr message on exit codes 3/4/5 should
    # make it unambiguous that nothing was delivered, so an operator
    # grepping logs for "sent" doesn't get a false positive.
    local bindir
    bindir=$(make_stub_bin)
    local stderr_out
    stderr_out=$(STUB_AWS_EXIT=1 PATH="$bindir:$PATH" "$NOTIFY_SCRIPT" "msg" 2>&1 1>/dev/null || true)
    if echo "$stderr_out" | grep -qi "NOT sent"; then
        pass "exit 3 stderr includes 'NOT sent'"
    else
        fail "exit 3 stderr includes 'NOT sent'" "got: $stderr_out"
    fi
}

# ── Run ────────────────────────────────────────────────────────────────────

test_usage_error_exits_1
test_secret_fetch_failure_exits_3
test_empty_bot_token_exits_4
test_missing_bot_token_key_exits_4
test_empty_user_ids_exits_5
test_missing_user_ids_key_exits_5
test_happy_path_exits_0
test_all_send_fail_exits_2
test_empty_message_exits_0
test_stderr_mentions_not_sent_on_config_paths

echo ""
echo "Ran $TESTS tests, $FAILURES failures"
if [ "$FAILURES" -gt 0 ]; then
    exit 1
fi
exit 0
