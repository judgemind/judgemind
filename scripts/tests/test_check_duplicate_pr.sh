#!/usr/bin/env bash
# test_check_duplicate_pr.sh — Tests for scripts/check-duplicate-pr.sh
#
# Covers the three documented exit codes of the wrapper (and the underlying
# preflight_no_duplicate_pr function it delegates to):
#   0 — duplicate PR found; a "duplicate:" line is printed to stdout with the
#       existing PR number
#   1 — no duplicate PR; an "ok:" line is printed to stdout
#   2 — error (missing argument, gh CLI unavailable, or API failure); an
#       "error:" line is printed to stderr
#
# Usage:
#   scripts/tests/test_check_duplicate_pr.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/check-duplicate-pr.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

# Cleanup of temp directories + PATH restore via the shared helper
# (see #4343).
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

# ── Test 1: exit 2 when called with no argument ────────────────────────────

exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 with no argument"
else
    fail "exits 2 with no argument" "expected exit 2, got $exit_code"
fi

# ── Set up a mock gh CLI on PATH for the remaining tests ──────────────────

MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
ORIG_PATH_SAVE="$PATH"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

# ── Test 2: exit 1 when no duplicate PR exists ─────────────────────────────

# Mock gh pr list returning an empty array for both the title search and the
# branch-name search.
cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
# Any gh pr list call returns "[]".
if [[ "${1:-}" == "pr" && "${2:-}" == "list" ]]; then
    echo "[]"
    exit 0
fi
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
err="$MOCK_BIN_DIR/test2.err"
output=$("$WRAPPER" 99999 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when no duplicate PR exists"
else
    fail "exits 1 when no duplicate PR exists" "expected exit 1, got $exit_code stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

if [[ "$output" == *"ok"* ]]; then
    pass "prints ok line to stdout when no duplicate"
else
    fail "prints ok line to stdout when no duplicate" "expected stdout containing 'ok', got '$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# ── Test 3: exit 0 and prints duplicate line when a duplicate exists ────────

# Mock gh pr list returning a PR whose title contains "(#42)" on the first
# (title-search) call, then an empty array for the branch-name call if reached.
cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
if [[ "${1:-}" == "pr" && "${2:-}" == "list" ]]; then
    # Check whether this is the title search (has --search) or branch search (has --head).
    for arg in "$@"; do
        if [[ "$arg" == "--search" ]]; then
            echo '[{"number": 1234, "title": "feat(dx): add duplicate PR wrapper (#42)"}]'
            exit 0
        fi
    done
    echo "[]"
    exit 0
fi
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
err="$MOCK_BIN_DIR/test3.err"
output=$("$WRAPPER" 42 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "exits 0 when a duplicate PR exists"
else
    fail "exits 0 when a duplicate PR exists" "expected exit 0, got $exit_code stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

if [[ "$output" == *"duplicate:"* && "$output" == *"1234"* ]]; then
    pass "prints duplicate line with PR number to stdout"
else
    fail "prints duplicate line with PR number to stdout" "expected stdout containing 'duplicate:' and '1234', got '$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# ── Test 4: strips a leading '#' from the issue argument ───────────────────

exit_code=0
err="$MOCK_BIN_DIR/test4.err"
output=$("$WRAPPER" "#42" 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"duplicate:"* && "$output" == *"1234"* ]]; then
    pass "strips leading '#' from the issue argument"
else
    fail "strips leading '#' from the issue argument" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# ── Test 5: exit 2 when gh CLI call fails (API error) ──────────────────────

cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
# Simulate gh pr list failing (e.g. unauthenticated, API down).
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
stderr_output=$("$WRAPPER" 42 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 when gh pr list fails"
else
    fail "exits 2 when gh pr list fails" "expected exit 2, got $exit_code"
fi

if [[ "$stderr_output" == *"error:"* ]]; then
    pass "prints error line to stderr when gh pr list fails"
else
    fail "prints error line to stderr when gh pr list fails" "expected stderr containing 'error:', got '$stderr_output'"
fi

# ── Test 6: exit 2 when gh is not installed ────────────────────────────────

# Remove the mock gh and restrict PATH to only system dirs that hold core
# utilities (bash, python3, grep, awk, mktemp). `gh` typically lives in
# /opt/homebrew/bin or /usr/local/bin and is absent from /bin:/usr/bin on a
# stock install — so this PATH cleanly simulates "gh not installed" while
# leaving the utilities the preflight function relies on intact.
rm -f "$MOCK_BIN_DIR/gh"

if command -v gh > /dev/null 2>&1 && PATH="/bin:/usr/bin" command -v gh > /dev/null 2>&1; then
    # Environment has `gh` on /bin or /usr/bin — we cannot reliably construct
    # a PATH that hides it without also hiding the utilities the function
    # depends on. Skip this test rather than produce a false failure.
    echo "SKIP: gh is on /bin or /usr/bin; cannot simulate 'gh not installed'"
else
    exit_code=0
    PATH="/bin:/usr/bin" "$WRAPPER" 42 > /dev/null 2>&1 || exit_code=$?
    if [[ "$exit_code" -eq 2 ]]; then
        pass "exits 2 when gh CLI is not installed"
    else
        fail "exits 2 when gh CLI is not installed" "expected exit 2, got $exit_code"
    fi
fi

# Restore PATH
export PATH="$ORIG_PATH_SAVE"

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
