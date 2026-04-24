#!/usr/bin/env bash
# test_block_issue.sh — Tests for scripts/block-issue.sh terse-default behavior
#
# Verifies:
#   a. Default-mode success output fits ≤3 lines
#   b. --verbose / JM_VERBOSE=1 restores info lines
#   c. Failure path stderr contains Error:
#
# Usage:
#   scripts/tests/test_block_issue.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/block-issue.sh"
FAILURES=0
TESTS=0

TEMP_DIRS=()
ORIG_PATH_SAVE=""

cleanup() {
    set +eu
    for d in "${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
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

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    echo "$dir"
}

# Set up a mock gh that simulates success for issue view and edit.
setup_mock_gh_success() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/gh" << 'MOCK'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        case "${2:-}" in
            view)
                # Return a body without Blocked by line
                echo "## Summary"
                echo "A test issue."
                exit 0
                ;;
            edit)
                # Simulate success silently
                exit 0
                ;;
        esac
        ;;
esac
exit 0
MOCK
    chmod +x "$tmpdir/bin/gh"
    echo "$tmpdir"
}

# Set up a mock gh that fails on issue view.
setup_mock_gh_fail() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/gh" << 'MOCK'
#!/usr/bin/env bash
exit 1
MOCK
    chmod +x "$tmpdir/bin/gh"
    echo "$tmpdir"
}

# ── Precondition: script exists and is executable ─────────────────────────

if [[ ! -x "$SCRIPT" ]]; then
    echo "FAIL: $SCRIPT is not executable (or does not exist)" >&2
    exit 1
fi

ORIG_PATH_SAVE="$PATH"

# ── Test 1: Usage error with wrong argument count ─────────────────────────

exit_code=0
stderr_output=$("$SCRIPT" 42 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero with wrong arg count"
else
    fail "exits non-zero with wrong arg count" "expected non-zero, got 0"
fi

if echo "$stderr_output" | grep -q "Usage:"; then
    pass "prints usage on wrong arg count"
else
    fail "prints usage on wrong arg count" "got: $stderr_output"
fi

# ── Test 2: Default-mode success output ≤3 lines ─────────────────────────

tmpdir=$(setup_mock_gh_success)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

all_output=$("$SCRIPT" 100 200 2>&1) || true
line_count=$(echo "$all_output" | grep -c . || true)

if [[ "$line_count" -le 3 ]]; then
    pass "default-mode success output fits ≤3 lines (got $line_count)"
else
    fail "default-mode success output fits ≤3 lines" "got $line_count lines: $all_output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 3: --verbose flag restores info lines ────────────────────────────

tmpdir=$(setup_mock_gh_success)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

verbose_output=$("$SCRIPT" --verbose 100 200 2>&1) || true
default_output=$("$SCRIPT" 100 200 2>&1) || true

verbose_lines=$(echo "$verbose_output" | grep -c . || true)
default_lines=$(echo "$default_output" | grep -c . || true)

if [[ "$verbose_lines" -ge "$default_lines" ]]; then
    pass "--verbose produces at least as many lines as default"
else
    fail "--verbose produces at least as many lines as default" \
        "verbose=$verbose_lines default=$default_lines"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 4: JM_VERBOSE=1 is honored ──────────────────────────────────────

tmpdir=$(setup_mock_gh_success)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

env_verbose_output=$(JM_VERBOSE=1 "$SCRIPT" 100 200 2>&1) || true
env_verbose_lines=$(echo "$env_verbose_output" | grep -c . || true)
default_lines_2=$(echo "$default_output" | grep -c . || true)

if [[ "$env_verbose_lines" -ge "$default_lines_2" ]]; then
    pass "JM_VERBOSE=1 produces at least as many lines as default"
else
    fail "JM_VERBOSE=1 produces at least as many lines as default" \
        "env_verbose=$env_verbose_lines default=$default_lines_2"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 5: Failure path stderr contains Error: ───────────────────────────

tmpdir=$(setup_mock_gh_fail)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
stderr_fail=$("$SCRIPT" 100 200 2>&1 >/dev/null) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero on gh failure"
else
    fail "exits non-zero on gh failure" "expected non-zero, got 0"
fi

if echo "$stderr_fail" | grep -qi "error"; then
    pass "failure path stderr contains Error:"
else
    fail "failure path stderr contains Error:" "got: $stderr_fail"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 6: Success line mentions both issue numbers ─────────────────────

tmpdir=$(setup_mock_gh_success)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

success_output=$("$SCRIPT" 123 456 2>&1) || true
if echo "$success_output" | grep -q "123" && echo "$success_output" | grep -q "456"; then
    pass "success line mentions both issue numbers"
else
    fail "success line mentions both issue numbers" "got: $success_output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
