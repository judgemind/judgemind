#!/usr/bin/env bash
# test_check_issue_plan_blocked.sh — Tests for scripts/check-issue-plan-blocked.sh
#
# Covers the three documented exit codes:
#   0 — plan-blocked marker on latest non-bot comment; "plan-blocked:" line
#       printed to stdout
#   1 — no actionable marker (no comments / no marker / superseded by a
#       later human comment); "clear:" line printed to stdout
#   2 — error (missing argument, gh CLI unavailable, API failure);
#       "error:" line printed to stderr
#
# Usage:
#   scripts/tests/test_check_issue_plan_blocked.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/check-issue-plan-blocked.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

# Cleanup of temp directories + PATH restore via the shared helper (#4343).
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

# ── Test 2: exit 2 when issue argument is non-numeric ──────────────────────

exit_code=0
"$WRAPPER" "abc" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 with non-numeric issue argument"
else
    fail "exits 2 with non-numeric issue argument" "expected exit 2, got $exit_code"
fi

# ── Set up a mock gh CLI on PATH for the remaining tests ──────────────────

MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
ORIG_PATH_SAVE="$PATH"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

# Helper: write a mock gh that emits the given comments JSON for any
# `gh issue view --json comments` call.
write_mock_gh() {
    local comments_json="$1"
    cat > "$MOCK_BIN_DIR/gh" <<MOCKGH
#!/usr/bin/env bash
if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    cat <<'COMMENTS_JSON'
$comments_json
COMMENTS_JSON
    exit 0
fi
exit 0
MOCKGH
    chmod +x "$MOCK_BIN_DIR/gh"
}

# ── Test 3: exit 1 ("clear:no-comments") when issue has no comments ────────

write_mock_gh '{"comments":[]}'
exit_code=0
output=$("$WRAPPER" 1 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-comments"* ]]; then
    pass "exits 1 with clear:no-comments when issue has no comments"
else
    fail "exits 1 with clear:no-comments when issue has no comments" "exit=$exit_code output='$output'"
fi

# ── Test 4: exit 1 ("clear:no-marker") when comments lack the sentinel ─────

write_mock_gh '{"comments":[{"author":{"login":"drewthaler"},"createdAt":"2026-05-01T00:00:00Z","body":"some other comment"}]}'
exit_code=0
output=$("$WRAPPER" 1 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-marker"* ]]; then
    pass "exits 1 with clear:no-marker when sentinel absent"
else
    fail "exits 1 with clear:no-marker when sentinel absent" "exit=$exit_code output='$output'"
fi

# ── Test 5: exit 0 when latest non-bot comment carries the sentinel ────────

write_mock_gh '{"comments":[{"author":{"login":"drewthaler"},"createdAt":"2026-04-23T00:00:00Z","body":"<!-- dispatcher-plan-blocked -->\nplan returned go=false\n<!-- dispatcher-plan-blocked-recommendation: operator-triage -->"}]}'
exit_code=0
output=$("$WRAPPER" 1 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"plan-blocked:"* ]]; then
    pass "exits 0 with plan-blocked: line when sentinel present on latest comment"
else
    fail "exits 0 with plan-blocked: line when sentinel present on latest comment" "exit=$exit_code output='$output'"
fi

if [[ "$output" == *"operator-triage"* ]]; then
    pass "extracts recommendation token from footer"
else
    fail "extracts recommendation token from footer" "output='$output'"
fi

# ── Test 6: exit 1 ("clear:superseded") when a later human comment follows ─

write_mock_gh '{"comments":[{"author":{"login":"drewthaler"},"createdAt":"2026-04-23T00:00:00Z","body":"<!-- dispatcher-plan-blocked -->\nplan reason"},{"author":{"login":"drewthaler"},"createdAt":"2026-04-24T00:00:00Z","body":"acknowledged, will re-scope"}]}'
exit_code=0
output=$("$WRAPPER" 1 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"superseded"* ]]; then
    pass "exits 1 with clear:superseded when later human comment follows the marker"
else
    fail "exits 1 with clear:superseded when later human comment follows the marker" "exit=$exit_code output='$output'"
fi

# ── Test 7: bot comments are filtered out of the latest-comment pick ──────

# Mark + later github-actions[bot] comment → still plan-blocked because the
# bot comment is filtered.
write_mock_gh '{"comments":[{"author":{"login":"drewthaler"},"createdAt":"2026-04-23T00:00:00Z","body":"<!-- dispatcher-plan-blocked -->\nplan reason"},{"author":{"login":"github-actions[bot]"},"createdAt":"2026-04-24T00:00:00Z","body":"automated noise"}]}'
exit_code=0
output=$("$WRAPPER" 1 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"plan-blocked:"* ]]; then
    pass "filters github-actions[bot] when picking the latest non-bot comment"
else
    fail "filters github-actions[bot] when picking the latest non-bot comment" "exit=$exit_code output='$output'"
fi

# ── Test 8: empty/missing recommendation footer (pre-#4438 comments) ───────

# Older plan-blocked comments lack the footer line. We still pivot, but the
# recommendation field is empty / "(unspecified)".
write_mock_gh '{"comments":[{"author":{"login":"drewthaler"},"createdAt":"2026-04-23T00:00:00Z","body":"<!-- dispatcher-plan-blocked -->\nlegacy comment without footer"}]}'
exit_code=0
output=$("$WRAPPER" 1 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"plan-blocked:"* ]]; then
    pass "exits 0 even when comment lacks the recommendation footer (pre-#4438)"
else
    fail "exits 0 even when comment lacks the recommendation footer (pre-#4438)" "exit=$exit_code output='$output'"
fi

# ── Test 9: strips a leading '#' from the issue argument ───────────────────

write_mock_gh '{"comments":[]}'
exit_code=0
output=$("$WRAPPER" "#42" 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* ]]; then
    pass "strips leading '#' from the issue argument"
else
    fail "strips leading '#' from the issue argument" "exit=$exit_code output='$output'"
fi

# ── Test 10: exit 2 when gh CLI fails (API error) ──────────────────────────

cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
stderr_output=$("$WRAPPER" 42 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 when gh issue view fails"
else
    fail "exits 2 when gh issue view fails" "expected exit 2, got $exit_code"
fi

if [[ "$stderr_output" == *"error:"* ]]; then
    pass "prints error line to stderr when gh issue view fails"
else
    fail "prints error line to stderr when gh issue view fails" "stderr='$stderr_output'"
fi

# ── Test 11: exit 2 when gh CLI is not on PATH ─────────────────────────────

# Restrict PATH to system dirs that lack gh — same simulation as the
# duplicate-PR test (gh typically lives in /opt/homebrew/bin or
# /usr/local/bin and is absent from /bin:/usr/bin on a stock install).
# Some CI runners DO have gh in /usr/bin; bypass this test in that case.
SAVED_PATH="$PATH"
export PATH="/bin:/usr/bin"

if command -v gh >/dev/null 2>&1; then
    pass "skip: gh present in /bin:/usr/bin (stock-install simulation unavailable)"
else
    exit_code=0
    stderr_output=$("$WRAPPER" 42 2>&1 >/dev/null) || exit_code=$?
    if [[ "$exit_code" -eq 2 && "$stderr_output" == *"error:"* ]]; then
        pass "exits 2 when gh CLI is not on PATH"
    else
        fail "exits 2 when gh CLI is not on PATH" "exit=$exit_code stderr='$stderr_output'"
    fi
fi

export PATH="$SAVED_PATH"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Tests run: $TESTS, failures: $FAILURES"
if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
exit 0
