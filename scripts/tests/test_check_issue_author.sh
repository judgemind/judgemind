#!/usr/bin/env bash
# test_check_issue_author.sh — Tests for scripts/check-issue-author.sh
# terse-default behavior.
#
# Verifies:
#   a. TRUSTED: / UNTRUSTED: / ERROR: prefix contract is preserved
#   b. --verbose / JM_VERBOSE=1 flag accepted without error
#   c. Failure path (API error) exits 2 with ERROR: output
#
# Usage:
#   scripts/tests/test_check_issue_author.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/check-issue-author.sh"
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

# Mock gh that returns a trusted OWNER association.
setup_mock_gh_trusted() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/gh" << 'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "api" ]]; then
    echo "OWNER testuser"
    exit 0
fi
exit 0
MOCK
    chmod +x "$tmpdir/bin/gh"
    echo "$tmpdir"
}

# Mock gh that returns an untrusted NONE association.
setup_mock_gh_untrusted() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/gh" << 'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "api" ]]; then
    echo "NONE external-user"
    exit 0
fi
exit 0
MOCK
    chmod +x "$tmpdir/bin/gh"
    echo "$tmpdir"
}

# Mock gh that fails (API error).
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

# ── Test 1: TRUSTED: output for trusted author ───────────────────────────

tmpdir=$(setup_mock_gh_trusted)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
output=$("$SCRIPT" 42 2>&1) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "exits 0 for trusted author"
else
    fail "exits 0 for trusted author" "got exit $exit_code"
fi

if echo "$output" | grep -q "^TRUSTED:"; then
    pass "TRUSTED: prefix on stdout for trusted author"
else
    fail "TRUSTED: prefix on stdout for trusted author" "got: $output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 2: UNTRUSTED: output for untrusted author ───────────────────────

tmpdir=$(setup_mock_gh_untrusted)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
output=$("$SCRIPT" 42 2>&1) || exit_code=$?

if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 for untrusted author"
else
    fail "exits 1 for untrusted author" "got exit $exit_code"
fi

if echo "$output" | grep -q "^UNTRUSTED:"; then
    pass "UNTRUSTED: prefix on stdout for untrusted author"
else
    fail "UNTRUSTED: prefix on stdout for untrusted author" "got: $output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 3: ERROR: output on API failure ─────────────────────────────────

tmpdir=$(setup_mock_gh_fail)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
output=$("$SCRIPT" 42 2>&1) || exit_code=$?

if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 on API failure"
else
    fail "exits 2 on API failure" "got exit $exit_code"
fi

if echo "$output" | grep -q "^ERROR:"; then
    pass "ERROR: prefix on stderr for API failure"
else
    fail "ERROR: prefix on stderr for API failure" "got: $output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 4: --verbose flag accepted (output contract unchanged) ───────────

tmpdir=$(setup_mock_gh_trusted)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
output=$("$SCRIPT" --verbose 42 2>&1) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "--verbose exits 0 for trusted author"
else
    fail "--verbose exits 0 for trusted author" "got exit $exit_code"
fi

if echo "$output" | grep -q "^TRUSTED:"; then
    pass "--verbose preserves TRUSTED: prefix"
else
    fail "--verbose preserves TRUSTED: prefix" "got: $output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 5: JM_VERBOSE=1 accepted (output contract unchanged) ────────────

tmpdir=$(setup_mock_gh_trusted)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
output=$(JM_VERBOSE=1 "$SCRIPT" 42 2>&1) || exit_code=$?

if [[ "$exit_code" -eq 0 && "$(echo "$output" | grep -c "^TRUSTED:")" -eq 1 ]]; then
    pass "JM_VERBOSE=1 preserves TRUSTED: contract"
else
    fail "JM_VERBOSE=1 preserves TRUSTED: contract" "exit=$exit_code output=$output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
