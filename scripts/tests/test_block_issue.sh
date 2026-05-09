#!/usr/bin/env bash
# test_block_issue.sh — Tests for scripts/block-issue.sh.
#
# Companion to test_block_on_new_issue.sh, closing the test-coverage gap
# called out by issue #4051. Argument-validation paths run before the first
# `gh` call, so they need no network. The body-edit / label-flip behavior is
# exercised against a PATH-mocked `gh` binary (same pattern as
# test_unblock_issue.sh, #4343).
#
# Test cases:
#   A — Wrong arg count (0, 1, 3) exits 1.
#   B — Non-numeric <issue> or <blocker> exits 1.
#   C — Leading '#' on either arg is stripped (numeric validation passes).
#   D — Body without ## Dependencies: writer creates section + Blocked by line.
#   E — Body with ## Dependencies but no matching Blocked by: writer appends
#       the line under the existing heading.
#   F — Body that already has 'Blocked by #N': no body update; labels still
#       added (exit 0, idempotent).
#   G — Label flip: --add-label status/blocked + --remove-label agent/ready
#       always invoked.
#
# Usage:
#   scripts/tests/test_block_issue.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/block-issue.sh"
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

# ── Test A — Wrong arg count exits 1 ───────────────────────────────────────

# 0 args
exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "A: 0 args exits 1"
else
    fail "A: 0 args exits 1" "expected exit 1, got $exit_code"
fi

# 1 arg
exit_code=0
"$WRAPPER" 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "A: 1 arg exits 1"
else
    fail "A: 1 arg exits 1" "expected exit 1, got $exit_code"
fi

# 3 args
exit_code=0
"$WRAPPER" 100 200 300 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "A: 3 args exits 1"
else
    fail "A: 3 args exits 1" "expected exit 1, got $exit_code"
fi

# ── Test B — Non-numeric args exit 1 ───────────────────────────────────────

exit_code=0
err_output=$("$WRAPPER" abc 100 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "B: non-numeric <issue> exits 1"
else
    fail "B: non-numeric <issue> exits 1" "expected exit 1, got $exit_code"
fi

if [[ "$err_output" == *"must be issue numbers"* ]]; then
    pass "B: non-numeric error names the validation"
else
    fail "B: non-numeric error names the validation" "expected 'must be issue numbers', got: $err_output"
fi

exit_code=0
"$WRAPPER" 100 abc > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "B: non-numeric <blocker> exits 1"
else
    fail "B: non-numeric <blocker> exits 1" "expected exit 1, got $exit_code"
fi

# ── Set up a mock gh CLI on PATH for the body/label tests ─────────────────

MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
ORIG_PATH_SAVE="$PATH"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

# Helper — render a fresh mock gh that:
#   - On `gh issue view <N> --json body -q .body`, prints the staged body
#     for issue N (one body per N, picked from $BODY_FOR_<N> env vars).
#   - On `gh issue edit <N> ... --body-file <path>`, copies the body file to
#     $WRITTEN_BODY_<N>.
#   - On `gh issue edit <N> ... --add-label ...`, records the full call to
#     $INVOCATIONS.
#   - Records every call to $INVOCATIONS regardless.
write_gh_mock() {
    local invocations="$1"
    cat > "$MOCK_BIN_DIR/gh" << MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$invocations"

if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    ISSUE_NUM="\${3:-}"
    body_var="BODY_FOR_\$ISSUE_NUM"
    body_value="\${!body_var:-}"
    # Print body; if -q is present we pass through raw, else JSON.
    has_q=0
    for arg in "\$@"; do
        if [[ "\$arg" == "-q" || "\$arg" == "--jq" ]]; then
            has_q=1
        fi
    done
    if [[ "\$has_q" == "1" ]]; then
        printf '%s' "\$body_value"
    else
        printf '%s' "\$body_value"
    fi
    exit 0
fi

if [[ "\${1:-}" == "issue" && "\${2:-}" == "edit" ]]; then
    ISSUE_NUM="\${3:-}"
    # If --body-file is present, capture the written body to a per-issue file.
    prev=""
    for arg in "\$@"; do
        if [[ "\$prev" == "--body-file" ]]; then
            cp "\$arg" "$MOCK_BIN_DIR/written_body_\$ISSUE_NUM.txt"
        fi
        prev="\$arg"
    done
    exit 0
fi

exit 0
MOCKEOF
    chmod +x "$MOCK_BIN_DIR/gh"
}

# ── Test C — Leading '#' is stripped ───────────────────────────────────────

INVOCATIONS_C="$MOCK_BIN_DIR/invocations_c.txt"
write_gh_mock "$INVOCATIONS_C"
export BODY_FOR_500="## Summary

Stub body."

exit_code=0
"$WRAPPER" "#500" "#600" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "C: leading '#' on both args passes validation"
else
    fail "C: leading '#' on both args passes validation" "expected exit 0, got $exit_code"
fi

# Confirm the body update went to issue 500 (not "#500").
if grep -qE "^issue (view|edit) 500" "$INVOCATIONS_C" 2>/dev/null; then
    pass "C: '#500' resolves to issue number 500"
else
    fail "C: '#500' resolves to issue number 500" "no 'issue view 500' or 'issue edit 500' in: $(cat "$INVOCATIONS_C")"
fi

unset BODY_FOR_500

# ── Test D — Body without ## Dependencies → section is created ─────────────

INVOCATIONS_D="$MOCK_BIN_DIR/invocations_d.txt"
write_gh_mock "$INVOCATIONS_D"
export BODY_FOR_700="## Summary

No dependencies yet."

exit_code=0
"$WRAPPER" 700 800 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "D: body without ## Dependencies — exits 0"
else
    fail "D: body without ## Dependencies — exits 0" "expected exit 0, got $exit_code"
fi

WRITTEN_D="$MOCK_BIN_DIR/written_body_700.txt"
if [[ -f "$WRITTEN_D" ]] && grep -q "^## Dependencies" "$WRITTEN_D" && grep -q "^Blocked by #800" "$WRITTEN_D"; then
    pass "D: writer creates ## Dependencies section + 'Blocked by #800'"
else
    fail "D: writer creates ## Dependencies section + 'Blocked by #800'" "written body: $(cat "$WRITTEN_D" 2>/dev/null || echo '<not written>')"
fi

unset BODY_FOR_700

# ── Test E — Body with ## Dependencies → line appended under heading ───────

INVOCATIONS_E="$MOCK_BIN_DIR/invocations_e.txt"
write_gh_mock "$INVOCATIONS_E"
export BODY_FOR_710="## Summary

Stub.

## Dependencies

Blocked by #999"

exit_code=0
"$WRAPPER" 710 800 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "E: body with existing ## Dependencies — exits 0"
else
    fail "E: body with existing ## Dependencies — exits 0" "expected exit 0, got $exit_code"
fi

WRITTEN_E="$MOCK_BIN_DIR/written_body_710.txt"
if [[ -f "$WRITTEN_E" ]] && grep -q "^Blocked by #800" "$WRITTEN_E" && grep -q "^Blocked by #999" "$WRITTEN_E"; then
    pass "E: writer keeps existing 'Blocked by #999' and adds 'Blocked by #800'"
else
    fail "E: writer keeps existing 'Blocked by #999' and adds 'Blocked by #800'" "written body: $(cat "$WRITTEN_E" 2>/dev/null || echo '<not written>')"
fi

# Should have exactly one ## Dependencies heading (no duplication).
heading_count=$(grep -c "^## Dependencies" "$WRITTEN_E" 2>/dev/null || echo 0)
if [[ "$heading_count" == "1" ]]; then
    pass "E: writer does not duplicate the ## Dependencies heading"
else
    fail "E: writer does not duplicate the ## Dependencies heading" "found $heading_count headings; written body: $(cat "$WRITTEN_E" 2>/dev/null || echo '<not written>')"
fi

unset BODY_FOR_710

# ── Test F — Body already has 'Blocked by #N' → no body write, labels still set ─

INVOCATIONS_F="$MOCK_BIN_DIR/invocations_f.txt"
write_gh_mock "$INVOCATIONS_F"
export BODY_FOR_720="## Summary

Stub.

## Dependencies

Blocked by #800"

exit_code=0
stdout_output=$("$WRAPPER" 720 800 2>&1) || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "F: idempotent — already 'Blocked by #800', exits 0"
else
    fail "F: idempotent — already 'Blocked by #800', exits 0" "expected exit 0, got $exit_code"
fi

if [[ "$stdout_output" == *"already has 'Blocked by #800'"* ]] || [[ "$stdout_output" == *"skipping body update"* ]]; then
    pass "F: idempotent — log line confirms body update skipped"
else
    fail "F: idempotent — log line confirms body update skipped" "stdout: $stdout_output"
fi

# Body file write must NOT have happened (no written_body_720.txt).
WRITTEN_F="$MOCK_BIN_DIR/written_body_720.txt"
if [[ ! -f "$WRITTEN_F" ]]; then
    pass "F: idempotent — no body file was written"
else
    fail "F: idempotent — no body file was written" "found written body: $(cat "$WRITTEN_F" 2>/dev/null || echo '<empty>')"
fi

# Labels must STILL have been set even though the body was skipped.
if grep -q -- "--add-label" "$INVOCATIONS_F" 2>/dev/null && grep -q "status/blocked" "$INVOCATIONS_F" 2>/dev/null; then
    pass "F: idempotent — labels (status/blocked) still applied"
else
    fail "F: idempotent — labels (status/blocked) still applied" "invocations: $(cat "$INVOCATIONS_F" 2>/dev/null)"
fi

unset BODY_FOR_720

# ── Test G — Label flip always fires ───────────────────────────────────────
#
# Re-uses Test D's scenario (body without ## Dependencies, fresh mock state)
# but checks the label-flip line specifically.

INVOCATIONS_G="$MOCK_BIN_DIR/invocations_g.txt"
write_gh_mock "$INVOCATIONS_G"
export BODY_FOR_730="## Summary

Stub."

"$WRAPPER" 730 800 > /dev/null 2>&1 || true

# Find the label-edit invocation: should contain --add-label status/blocked
# and --remove-label agent/ready in a single call.
label_line=$(grep "issue edit 730" "$INVOCATIONS_G" 2>/dev/null | grep -- "--add-label" || true)
if [[ -n "$label_line" ]] \
    && [[ "$label_line" == *"status/blocked"* ]] \
    && [[ "$label_line" == *"--remove-label"* ]] \
    && [[ "$label_line" == *"agent/ready"* ]]; then
    pass "G: label-flip call sets +status/blocked, -agent/ready"
else
    fail "G: label-flip call sets +status/blocked, -agent/ready" "label-edit line: $label_line; full invocations: $(cat "$INVOCATIONS_G" 2>/dev/null)"
fi

unset BODY_FOR_730

# Restore PATH
export PATH="$ORIG_PATH_SAVE"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
