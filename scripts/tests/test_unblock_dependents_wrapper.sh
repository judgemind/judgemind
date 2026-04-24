#!/usr/bin/env bash
# test_unblock_dependents_wrapper.sh — Tests for scripts/unblock-dependents.sh
# terse-default behavior (wrapper/shell layer only — not the Python helper).
#
# Verifies:
#   a. Default-mode success output fits ≤3 lines (no candidates found path)
#   b. --verbose / JM_VERBOSE=1 flag accepted without error
#   c. Failure path stderr contains Error:
#
# Usage:
#   scripts/tests/test_unblock_dependents_wrapper.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/unblock-dependents.sh"
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

# Mock gh that returns an empty list (no blocked issues found).
setup_mock_gh_empty() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/gh" << 'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "issue" && "${2:-}" == "list" ]]; then
    echo "[]"
    exit 0
fi
exit 0
MOCK
    chmod +x "$tmpdir/bin/gh"
    echo "$tmpdir"
}

# Mock gh that fails.
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

# ── Test 1: Usage error with no argument ─────────────────────────────────

exit_code=0
stderr_output=$("$SCRIPT" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero with no argument"
else
    fail "exits non-zero with no argument" "expected non-zero, got 0"
fi

# ── Test 2: Default-mode "no issues found" output fits ≤3 lines ──────────

tmpdir=$(setup_mock_gh_empty)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

all_output=$("$SCRIPT" 9999 2>&1) || true
line_count=$(echo "$all_output" | grep -c . || true)

if [[ "$line_count" -le 3 ]]; then
    pass "default-mode no-issues output fits ≤3 lines (got $line_count)"
else
    fail "default-mode no-issues output fits ≤3 lines" "got $line_count lines: $all_output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 3: --verbose flag accepted ──────────────────────────────────────

tmpdir=$(setup_mock_gh_empty)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
"$SCRIPT" --verbose 9999 > /dev/null 2>&1 || exit_code=$?
# exit 0 is expected when no issues are blocked
if [[ "$exit_code" -eq 0 ]]; then
    pass "--verbose flag accepted without error"
else
    fail "--verbose flag accepted without error" "exit=$exit_code"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 4: JM_VERBOSE=1 accepted ────────────────────────────────────────

tmpdir=$(setup_mock_gh_empty)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
JM_VERBOSE=1 "$SCRIPT" 9999 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "JM_VERBOSE=1 accepted without error"
else
    fail "JM_VERBOSE=1 accepted without error" "exit=$exit_code"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 5: Failure path stderr contains Error: ───────────────────────────

tmpdir=$(setup_mock_gh_fail)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
stderr_fail=$("$SCRIPT" 9999 2>&1 >/dev/null) || exit_code=$?

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

# ── Test 6: --dry-run still works ────────────────────────────────────────

tmpdir=$(setup_mock_gh_empty)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
"$SCRIPT" --dry-run 9999 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "--dry-run still works"
else
    fail "--dry-run still works" "exit=$exit_code"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
