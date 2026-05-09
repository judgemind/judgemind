#!/usr/bin/env bash
# test_block_on_new_issue.sh — Tests for scripts/block-on-new-issue.sh.
#
# Closes the gap surfaced by #4051: scripts/block-on-new-issue.sh and its
# companion scripts/block-issue.sh shipped in #4035 / PR #4045 with zero test
# coverage. The argument-validation paths were sanity-checked manually before
# the PR went out, but a future refactor of the arg parser could silently
# regress and the next agent that hits an upstream blocker would discover it
# the hard way (failed `gh issue create` invocation mid-flight, dependent
# issue never blocked, repeat-investigation cycle that #4035 just closed
# re-opens).
#
# Coverage model — all argument-validation paths exercise without network
# because block-on-new-issue.sh's parser runs to completion before the first
# `gh` call. The tests that DO need to reach the gh-call layer use a
# PATH-mocked gh binary (same pattern as test_unblock_issue.sh, #4343) so the
# real GitHub API is never touched.
#
# Test cases:
#   A — `--help` exits 0 with usage text on stdout (here: stderr; usage()
#       prints to stderr per the script).
#   B — Missing <dependent-issue> exits 1.
#   C — Non-numeric <dependent-issue> exits 1 with explicit error message.
#   D — Missing --title exits 1.
#   E — Missing --body-file exits 1.
#   F — --body-file points to a path that does not exist exits 1.
#   G — --priority p9 (invalid) exits 1; --priority p0|p1|p2|p3 are accepted.
#   H — Unknown flag exits 1.
#   I — Happy path with a mocked `gh` records a `gh issue create` call AND a
#       `gh issue edit` call (via block-issue.sh) on the dependent issue.
#   J — --priority p1 sugar adds priority/p1 to the gh issue create labels.
#
# Usage:
#   scripts/tests/test_block_on_new_issue.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/block-on-new-issue.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

# Cleanup of temp dirs/files + PATH restore via the shared helper (#4343).
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

# Stage a body file used by every test that gets past the body-file check.
BODY_DIR=$(mktemp -d)
register_temp_dir "$BODY_DIR"
BODY_FILE="$BODY_DIR/body.md"
echo "stub body for tests" > "$BODY_FILE"

# ── Test A — --help exits 0 ────────────────────────────────────────────────

exit_code=0
"$WRAPPER" --help > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "A: --help exits 0"
else
    fail "A: --help exits 0" "expected exit 0, got $exit_code"
fi

# --help prints usage containing 'block-on-new-issue.sh' (usage prints to
# stderr per the script).
help_output=$("$WRAPPER" --help 2>&1 >/dev/null) || true
if [[ "$help_output" == *"block-on-new-issue.sh"* ]]; then
    pass "A: --help prints usage line"
else
    fail "A: --help prints usage line" "expected 'block-on-new-issue.sh' in output, got: $help_output"
fi

# ── Test B — Missing <dependent-issue> exits 1 ─────────────────────────────

exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "B: missing <dependent-issue> exits 1"
else
    fail "B: missing <dependent-issue> exits 1" "expected exit 1, got $exit_code"
fi

# ── Test C — Non-numeric <dependent-issue> exits 1 ─────────────────────────

exit_code=0
err_output=$("$WRAPPER" "abc" --title "x" --body-file "$BODY_FILE" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "C: non-numeric <dependent-issue> exits 1"
else
    fail "C: non-numeric <dependent-issue> exits 1" "expected exit 1, got $exit_code"
fi

if [[ "$err_output" == *"must be an issue number"* ]]; then
    pass "C: non-numeric error names the validation"
else
    fail "C: non-numeric error names the validation" "expected 'must be an issue number', got: $err_output"
fi

# Leading-# form is accepted (gets stripped) — check by piping into a path
# that fails *after* validation (no --title) so we know validation passed.
exit_code=0
err_output=$("$WRAPPER" "#42" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]] && [[ "$err_output" == *"--title is required"* ]]; then
    pass "C: leading-# form (#42) passes numeric validation"
else
    fail "C: leading-# form (#42) passes numeric validation" "expected '--title is required' (i.e. validation passed), got exit=$exit_code output: $err_output"
fi

# ── Test D — Missing --title exits 1 ───────────────────────────────────────

exit_code=0
err_output=$("$WRAPPER" 42 --body-file "$BODY_FILE" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "D: missing --title exits 1"
else
    fail "D: missing --title exits 1" "expected exit 1, got $exit_code"
fi

if [[ "$err_output" == *"--title is required"* ]]; then
    pass "D: missing --title error mentions --title"
else
    fail "D: missing --title error mentions --title" "expected '--title is required', got: $err_output"
fi

# ── Test E — Missing --body-file exits 1 ───────────────────────────────────

exit_code=0
err_output=$("$WRAPPER" 42 --title "x" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "E: missing --body-file exits 1"
else
    fail "E: missing --body-file exits 1" "expected exit 1, got $exit_code"
fi

if [[ "$err_output" == *"--body-file is required"* ]]; then
    pass "E: missing --body-file error mentions --body-file"
else
    fail "E: missing --body-file error mentions --body-file" "expected '--body-file is required', got: $err_output"
fi

# ── Test F — --body-file points to a non-existent path exits 1 ─────────────

exit_code=0
err_output=$("$WRAPPER" 42 --title "x" --body-file "$BODY_DIR/does-not-exist.md" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "F: --body-file with non-existent path exits 1"
else
    fail "F: --body-file with non-existent path exits 1" "expected exit 1, got $exit_code"
fi

if [[ "$err_output" == *"does not exist"* ]]; then
    pass "F: --body-file non-existent error names the missing path"
else
    fail "F: --body-file non-existent error names the missing path" "expected 'does not exist', got: $err_output"
fi

# ── Test G — --priority validation ─────────────────────────────────────────

# Invalid priority exits 1 BEFORE attempting any gh call.
exit_code=0
err_output=$("$WRAPPER" 42 --title "x" --body-file "$BODY_FILE" --priority p9 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "G: --priority p9 (invalid) exits 1"
else
    fail "G: --priority p9 (invalid) exits 1" "expected exit 1, got $exit_code"
fi

if [[ "$err_output" == *"--priority must be one of"* ]]; then
    pass "G: --priority p9 error names valid choices"
else
    fail "G: --priority p9 error names valid choices" "expected '--priority must be one of', got: $err_output"
fi

# Valid priorities (p0..p3) pass validation. We can't run the script all the
# way through without a gh mock, so we verify each priority is accepted by
# making the call fail at a *later* point — by setting PATH so gh is missing,
# which causes the script to fail in CREATE_ARGS execution (gh: command not
# found), AFTER having passed --priority validation. We detect that by the
# absence of the priority error text in the output.
EMPTY_BIN=$(mktemp -d)
register_temp_dir "$EMPTY_BIN"
ORIG_PATH_SAVE="$PATH"
export PATH="$EMPTY_BIN"   # gh not available

for prio in p0 p1 p2 p3; do
    exit_code=0
    err_output=$("$WRAPPER" 42 --title "x" --body-file "$BODY_FILE" --priority "$prio" 2>&1 >/dev/null) || exit_code=$?
    # Priority validation must NOT have triggered.
    if [[ "$err_output" == *"--priority must be one of"* ]]; then
        fail "G: --priority $prio is accepted" "got --priority error: $err_output"
    else
        pass "G: --priority $prio is accepted (passed validation)"
    fi
done

# Restore PATH for subsequent gh-mock tests.
export PATH="$ORIG_PATH_SAVE"

# ── Test H — Unknown flag exits 1 ──────────────────────────────────────────

exit_code=0
err_output=$("$WRAPPER" 42 --title "x" --body-file "$BODY_FILE" --bogus-flag value 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "H: unknown flag exits 1"
else
    fail "H: unknown flag exits 1" "expected exit 1, got $exit_code"
fi

if [[ "$err_output" == *"unknown argument"* ]]; then
    pass "H: unknown flag error names 'unknown argument'"
else
    fail "H: unknown flag error names 'unknown argument'" "expected 'unknown argument', got: $err_output"
fi

# ── Set up a mock gh CLI on PATH for tests I and J ─────────────────────────

MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
ORIG_PATH_SAVE="$PATH"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

# ── Test I — Happy path: mocked gh records create + edit ───────────────────
#
# Scenario: file a new tracker for issue #500.
# Mock gh records every invocation. block-on-new-issue.sh should:
#   1. Call `gh issue create ... --title "tracker for X"` and capture the URL.
#   2. Hand off to block-issue.sh which calls `gh issue view 500` (body
#      fetch), `gh issue edit 500 --body-file ...` (body update), and
#      `gh issue edit 500 --add-label status/blocked --remove-label
#      agent/ready` (label flip).

INVOCATIONS_I="$MOCK_BIN_DIR/invocations_i.txt"

cat > "$MOCK_BIN_DIR/gh" << MOCKEOF
#!/usr/bin/env bash
# Record every invocation so the test can assert against them.
echo "\$@" >> "$INVOCATIONS_I"

# gh issue create — emit a fake URL so the wrapper can parse the new number.
if [[ "\${1:-}" == "issue" && "\${2:-}" == "create" ]]; then
    echo "https://github.com/judgemind/judgemind/issues/9999"
    exit 0
fi

# gh issue view <N> --json body -q .body  (block-issue.sh body fetch)
# Match the -q form regardless of how the tool quotes it.
if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    # Emit a body with no ## Dependencies section so block-issue.sh creates
    # one. The -q '.body' flag means we return raw body text, not JSON.
    has_q=0
    for arg in "\$@"; do
        if [[ "\$arg" == "-q" || "\$arg" == "--jq" ]]; then
            has_q=1
        fi
    done
    if [[ "\$has_q" == "1" ]]; then
        echo "## Summary"
        echo ""
        echo "Stub body, no dependencies yet."
    else
        # JSON form (block-issue.sh uses -q so this branch is unused, but
        # included for safety / future-proofing).
        printf '%s' '{"body":"## Summary\n\nStub body, no dependencies yet."}'
    fi
    exit 0
fi

# gh issue edit — body update or label flip. Always succeed.
if [[ "\${1:-}" == "issue" && "\${2:-}" == "edit" ]]; then
    exit 0
fi

# Default: succeed silently.
exit 0
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
"$WRAPPER" 500 --title "tracker for X" --body-file "$BODY_FILE" > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "I: happy path exits 0"
else
    fail "I: happy path exits 0" "expected exit 0, got $exit_code; invocations: $(cat "$INVOCATIONS_I" 2>/dev/null || echo '<none>')"
fi

if grep -q "issue create" "$INVOCATIONS_I" 2>/dev/null; then
    pass "I: happy path calls 'gh issue create'"
else
    fail "I: happy path calls 'gh issue create'" "no 'issue create' in invocations: $(cat "$INVOCATIONS_I" 2>/dev/null || echo '<none>')"
fi

# block-issue.sh adds the status/blocked label — confirm the dependent (500)
# was edited.
if grep -q "issue edit 500" "$INVOCATIONS_I" 2>/dev/null; then
    pass "I: happy path calls 'gh issue edit 500' (block-issue.sh handoff)"
else
    fail "I: happy path calls 'gh issue edit 500' (block-issue.sh handoff)" "no 'issue edit 500' in invocations: $(cat "$INVOCATIONS_I" 2>/dev/null || echo '<none>')"
fi

if grep -q "status/blocked" "$INVOCATIONS_I" 2>/dev/null; then
    pass "I: happy path adds status/blocked label"
else
    fail "I: happy path adds status/blocked label" "no 'status/blocked' in invocations"
fi

# ── Test J — --priority p1 sugar adds priority/p1 to gh issue create ───────

INVOCATIONS_J="$MOCK_BIN_DIR/invocations_j.txt"

cat > "$MOCK_BIN_DIR/gh" << MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$INVOCATIONS_J"
if [[ "\${1:-}" == "issue" && "\${2:-}" == "create" ]]; then
    echo "https://github.com/judgemind/judgemind/issues/8888"
    exit 0
fi
if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    echo "## Summary"
    echo ""
    echo "Stub."
    exit 0
fi
exit 0
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
"$WRAPPER" 600 --title "p1 tracker" --body-file "$BODY_FILE" --priority p1 > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "J: --priority p1 happy path exits 0"
else
    fail "J: --priority p1 happy path exits 0" "expected exit 0, got $exit_code"
fi

# Find the line for gh issue create and confirm it includes 'priority/p1'.
create_line=$(grep "^issue create" "$INVOCATIONS_J" 2>/dev/null || true)
if [[ -n "$create_line" && "$create_line" == *"priority/p1"* ]]; then
    pass "J: --priority p1 adds 'priority/p1' label to gh issue create"
else
    fail "J: --priority p1 adds 'priority/p1' label to gh issue create" "create line: $create_line"
fi

# Restore PATH
export PATH="$ORIG_PATH_SAVE"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
