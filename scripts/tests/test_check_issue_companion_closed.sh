#!/usr/bin/env bash
# test_check_issue_companion_closed.sh — Tests for
# scripts/check-issue-companion-closed.sh.
#
# Covers the three documented exit codes:
#   0 — companion-closed obsoletion detected; "companion-closed:" line
#       printed to stdout
#   1 — no actionable signal (no-references / no-companion /
#       no-closed-completed); "clear:" line printed to stdout
#   2 — error (missing argument, gh CLI unavailable, API failure);
#       "error:" line printed to stderr
#
# Mirrors test_check_issue_plan_blocked.sh's harness shape.
#
# Usage:
#   scripts/tests/test_check_issue_companion_closed.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/check-issue-companion-closed.sh"
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

# Helper: write a mock gh that supports the two distinct calls the
# wrapper makes:
#   - ``gh issue view <N> --json body``  →  emit body JSON for the
#     primary issue
#   - ``gh issue view <M> --json state,stateReason``  →  emit state
#     JSON for sibling #M
#
# The mock dispatches based on the requested --json argument (any
# match for `body` returns the body; any other call returns the
# state map keyed by issue number from $STATES_DIR/<num>.json if it
# exists, or empty JSON otherwise).
write_mock_gh() {
    local body_file="$1"
    local states_dir="$2"
    cat > "$MOCK_BIN_DIR/gh" <<MOCKGH
#!/usr/bin/env bash
# Args: gh issue view <N> --repo X --json body
#       gh issue view <M> --repo X --json state,stateReason
issue_num=""
json_arg=""
in_json=0
for a in "\$@"; do
    if [[ "\$in_json" == "1" ]]; then
        json_arg="\$a"
        in_json=0
        continue
    fi
    if [[ "\$a" == "--json" ]]; then
        in_json=1
        continue
    fi
    if [[ "\$a" =~ ^[0-9]+\$ ]]; then
        issue_num="\$a"
    fi
done
case "\$json_arg" in
    body)
        printf '{"body":'
        python3 -c 'import json,sys; sys.stdout.write(json.dumps(open("$body_file").read()))'
        printf '}'
        ;;
    *state*)
        f="$states_dir/\${issue_num}.json"
        if [[ -f "\$f" ]]; then
            cat "\$f"
        else
            # Unresolved sibling — return empty (the wrapper
            # tolerates this and skips).
            echo '{"state":"","stateReason":""}'
        fi
        ;;
    *)
        echo '{}'
        ;;
esac
MOCKGH
    chmod +x "$MOCK_BIN_DIR/gh"
}

# AC #1: closed sibling + matching framing → exit 0
TEST_DIR=$(mktemp -d)
register_temp_dir "$TEST_DIR"
mkdir -p "$TEST_DIR/states_ac1"
cat > "$TEST_DIR/body_ac1.txt" <<'BODY1'
## Summary

Update the docstring.

This is a temporary caveat that should be removed when the structural fix in #4408 lands.

## References

- See also #9999 for context.

Parent: #4397
BODY1
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_ac1/4408.json"
echo '{"state":"open","stateReason":null}' > "$TEST_DIR/states_ac1/4397.json"
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_ac1/9999.json"

write_mock_gh "$TEST_DIR/body_ac1.txt" "$TEST_DIR/states_ac1"
exit_code=0
err="$TEST_DIR/ac1.err"
output=$("$WRAPPER" 4409 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"companion-closed:"* && "$output" == *"4408"* ]]; then
    pass "AC #1: closed sibling + matching framing → exit 0 with companion-closed:4408"
else
    fail "AC #1: closed sibling + matching framing → exit 0 with companion-closed:4408" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# AC #2: open sibling → exit 1
mkdir -p "$TEST_DIR/states_ac2"
cat > "$TEST_DIR/body_ac2.txt" <<'BODY2'
This is a temporary caveat that should be removed when the structural fix in #4408 lands.
BODY2
echo '{"state":"open","stateReason":null}' > "$TEST_DIR/states_ac2/4408.json"

write_mock_gh "$TEST_DIR/body_ac2.txt" "$TEST_DIR/states_ac2"
exit_code=0
err="$TEST_DIR/ac2.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-closed-completed"* ]]; then
    pass "AC #2: open sibling → exit 1 with clear:no-closed-completed"
else
    fail "AC #2: open sibling → exit 1 with clear:no-closed-completed" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# AC #3: closed-as-not-planned sibling → exit 1
mkdir -p "$TEST_DIR/states_ac3"
cat > "$TEST_DIR/body_ac3.txt" <<'BODY3'
This is a temporary caveat that should be removed when the structural fix in #4408 lands.
BODY3
echo '{"state":"closed","stateReason":"NOT_PLANNED"}' > "$TEST_DIR/states_ac3/4408.json"

write_mock_gh "$TEST_DIR/body_ac3.txt" "$TEST_DIR/states_ac3"
exit_code=0
err="$TEST_DIR/ac3.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-closed-completed"* ]]; then
    pass "AC #3: closed-as-not-planned sibling → exit 1 with clear:no-closed-completed"
else
    fail "AC #3: closed-as-not-planned sibling → exit 1 with clear:no-closed-completed" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# AC #4: hashtag without companion framing → exit 1
mkdir -p "$TEST_DIR/states_ac4"
cat > "$TEST_DIR/body_ac4.txt" <<'BODY4'
## Summary

Add a new feature.

See #4408 for context.
BODY4
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_ac4/4408.json"

write_mock_gh "$TEST_DIR/body_ac4.txt" "$TEST_DIR/states_ac4"
exit_code=0
err="$TEST_DIR/ac4.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-companion"* ]]; then
    pass "AC #4: bare hashtag without framing → exit 1 with clear:no-companion"
else
    fail "AC #4: bare hashtag without framing → exit 1 with clear:no-companion" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# Empty body → exit 1 with no-references
mkdir -p "$TEST_DIR/states_empty"
echo "" > "$TEST_DIR/body_empty.txt"

write_mock_gh "$TEST_DIR/body_empty.txt" "$TEST_DIR/states_empty"
exit_code=0
err="$TEST_DIR/empty.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-references"* ]]; then
    pass "empty body → exit 1 with clear:no-references"
else
    fail "empty body → exit 1 with clear:no-references" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# Strips a leading '#' from the issue argument
mkdir -p "$TEST_DIR/states_hash"
cat > "$TEST_DIR/body_hash.txt" <<'BODYH'
This is a temporary caveat that should be removed when the structural fix in #4408 lands.
BODYH
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_hash/4408.json"

write_mock_gh "$TEST_DIR/body_hash.txt" "$TEST_DIR/states_hash"
exit_code=0
err="$TEST_DIR/hash.err"
output=$("$WRAPPER" "#42" 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"companion-closed:"* ]]; then
    pass "strips leading '#' from the issue argument"
else
    fail "strips leading '#' from the issue argument" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# exit 2 when gh CLI fails (API error)
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

# exit 2 when gh CLI is not on PATH (stock-install simulation)
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
