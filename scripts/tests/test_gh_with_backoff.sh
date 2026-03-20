#!/usr/bin/env bash
# test_gh_with_backoff.sh — Tests for scripts/gh-with-backoff.sh
#
# Tests the rate-limit-aware gh wrapper using mock gh commands.
#
# Usage:
#   scripts/tests/test_gh_with_backoff.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GH_BACKOFF_SCRIPT="$SCRIPT_DIR/gh-with-backoff.sh"

FAILURES=0
TESTS=0

TEMP_DIRS=()
cleanup() {
    set +e
    for d in "${TEMP_DIRS[@]}"; do
        [[ -n "$d" && -d "$d" ]] && rm -rf "$d"
    done
    # Restore PATH
    if [[ -n "${ORIG_PATH:-}" ]]; then
        export PATH="$ORIG_PATH"
    fi
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

# Save original PATH
ORIG_PATH="$PATH"

# Create a temp bin directory for mock gh
MOCK_BIN=$(mktemp -d)
TEMP_DIRS+=("$MOCK_BIN")

# ══════════════════════════════════════════════════════════════════════════
# Test 1: Shows usage when no arguments provided
# ══════════════════════════════════════════════════════════════════════════

exit_code=0
"$GH_BACKOFF_SCRIPT" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "shows usage and exits non-zero with no arguments"
else
    fail "shows usage and exits non-zero with no arguments" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 2: Passes through successful gh commands
# ══════════════════════════════════════════════════════════════════════════

# Create a mock gh that succeeds
cat > "$MOCK_BIN/gh" << 'MOCKEOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "api" && "${2:-}" == "rate_limit" ]]; then
    echo "4999 9999999999 5000"
    exit 0
fi
echo "mock-success-output"
exit 0
MOCKEOF
chmod +x "$MOCK_BIN/gh"

export PATH="$MOCK_BIN:$ORIG_PATH"

exit_code=0
output=$("$GH_BACKOFF_SCRIPT" issue view 42 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == "mock-success-output" ]]; then
    pass "passes through successful gh commands"
else
    fail "passes through successful gh commands" "exit: $exit_code, output: $output"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 3: Passes through non-rate-limit failures
# ══════════════════════════════════════════════════════════════════════════

cat > "$MOCK_BIN/gh" << 'MOCKEOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "api" && "${2:-}" == "rate_limit" ]]; then
    echo "4999 9999999999 5000"
    exit 0
fi
echo "not found" >&2
exit 1
MOCKEOF
chmod +x "$MOCK_BIN/gh"

exit_code=0
"$GH_BACKOFF_SCRIPT" issue view 999 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "passes through non-rate-limit failures"
else
    fail "passes through non-rate-limit failures" "expected exit 1, got $exit_code"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 4: Retries on 403 rate limit errors
# ══════════════════════════════════════════════════════════════════════════

# Create a mock gh that fails with 403 once then succeeds
CALL_COUNT_FILE=$(mktemp)
TEMP_DIRS+=("$CALL_COUNT_FILE")
echo "0" > "$CALL_COUNT_FILE"

cat > "$MOCK_BIN/gh" << MOCKEOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "api" && "\${2:-}" == "rate_limit" ]]; then
    echo "4999 9999999999 5000"
    exit 0
fi

count=\$(cat "$CALL_COUNT_FILE")
count=\$((count + 1))
echo "\$count" > "$CALL_COUNT_FILE"

if [[ "\$count" -le 1 ]]; then
    echo "API rate limit exceeded (403)" >&2
    exit 1
fi

echo "retry-success"
exit 0
MOCKEOF
chmod +x "$MOCK_BIN/gh"

# Use very short wait time for testing
export GH_BACKOFF_MIN_WAIT=1
export GH_BACKOFF_MAX_RETRIES=3

exit_code=0
output=$("$GH_BACKOFF_SCRIPT" pr list 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == "retry-success" ]]; then
    pass "retries on 403 rate limit errors and succeeds"
else
    fail "retries on 403 rate limit errors and succeeds" "exit: $exit_code, output: $output"
fi

# Reset env
unset GH_BACKOFF_MIN_WAIT
unset GH_BACKOFF_MAX_RETRIES

# ══════════════════════════════════════════════════════════════════════════
# Test 5: Exhausts retries and exits with error
# ══════════════════════════════════════════════════════════════════════════

cat > "$MOCK_BIN/gh" << 'MOCKEOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "api" && "${2:-}" == "rate_limit" ]]; then
    echo "0 9999999999 5000"
    exit 0
fi
echo "API rate limit exceeded (403)" >&2
exit 1
MOCKEOF
chmod +x "$MOCK_BIN/gh"

export GH_BACKOFF_MIN_WAIT=1
export GH_BACKOFF_MAX_RETRIES=2

exit_code=0
"$GH_BACKOFF_SCRIPT" run watch 123 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "exhausts retries and exits with error"
else
    fail "exhausts retries and exits with error" "expected non-zero exit"
fi

unset GH_BACKOFF_MIN_WAIT
unset GH_BACKOFF_MAX_RETRIES

# ══════════════════════════════════════════════════════════════════════════
# Test 6: Warns on low rate budget (pre-check)
# ══════════════════════════════════════════════════════════════════════════

cat > "$MOCK_BIN/gh" << 'MOCKEOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "api" && "${2:-}" == "rate_limit" ]]; then
    echo "50 9999999999 5000"
    exit 0
fi
echo "success"
exit 0
MOCKEOF
chmod +x "$MOCK_BIN/gh"

exit_code=0
stderr_output=$("$GH_BACKOFF_SCRIPT" issue list 2>&1 >/dev/null) || exit_code=$?
if echo "$stderr_output" | grep -qi "rate budget low"; then
    pass "warns on low rate budget"
else
    fail "warns on low rate budget" "expected rate budget warning in stderr: $stderr_output"
fi

# Restore PATH
export PATH="$ORIG_PATH"

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
