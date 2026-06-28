#!/usr/bin/env bash
# test_check_issue_was_blocked_by.sh — Tests for
# scripts/check-issue-was-blocked-by.sh.
#
# Covers the three documented exit codes:
#   0 — former blockers all closed-completed; "was-blocked-by:" line
#       printed to stdout
#   1 — no actionable signal (no-marker / not-all-closed-completed);
#       "clear:" line printed to stdout
#   2 — error (missing argument, gh CLI unavailable, API failure);
#       "error:" line printed to stderr
#
# Mirrors test_check_issue_companion_closed.sh's harness shape.
#
# Usage:
#   scripts/tests/test_check_issue_was_blocked_by.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/check-issue-was-blocked-by.sh"
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
#     JSON for former-blocker #M
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
            # Unresolved former blocker — return empty.
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

# AC: marker with all-closed-completed former blockers → exit 0
TEST_DIR=$(mktemp -d)
register_temp_dir "$TEST_DIR"
mkdir -p "$TEST_DIR/states_ok"
cat > "$TEST_DIR/body_ok.txt" <<'BODYOK'
## Problem

Do the thing.

## Acceptance Criteria

- [ ] Do thing
  Verify: true

Was-blocked-by: #100, #101 (all closed-completed 2026-05-08; auto-unblocked)
BODYOK
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_ok/100.json"
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_ok/101.json"

write_mock_gh "$TEST_DIR/body_ok.txt" "$TEST_DIR/states_ok"
exit_code=0
err="$TEST_DIR/ok.err"
output=$("$WRAPPER" 3732 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"was-blocked-by:"* && "$output" == *"#100"* && "$output" == *"#101"* ]]; then
    pass "AC: marker + all former blockers closed-completed → exit 0 (pivot)"
else
    fail "AC: marker + all former blockers closed-completed → exit 0 (pivot)" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# No marker → exit 1 with no-marker
mkdir -p "$TEST_DIR/states_nm"
cat > "$TEST_DIR/body_nm.txt" <<'BODYNM'
## Problem

Just a normal issue with no provenance marker. References #100.
BODYNM
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_nm/100.json"

write_mock_gh "$TEST_DIR/body_nm.txt" "$TEST_DIR/states_nm"
exit_code=0
err="$TEST_DIR/nm.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-marker"* ]]; then
    pass "no Was-blocked-by marker → exit 1 with clear:no-marker"
else
    fail "no Was-blocked-by marker → exit 1 with clear:no-marker" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# One former blocker still open → exit 1 not-all-closed-completed
mkdir -p "$TEST_DIR/states_open"
cat > "$TEST_DIR/body_open.txt" <<'BODYOPEN'
Was-blocked-by: #100, #101 (all closed-completed 2026-05-08; auto-unblocked)
BODYOPEN
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_open/100.json"
echo '{"state":"open","stateReason":null}' > "$TEST_DIR/states_open/101.json"

write_mock_gh "$TEST_DIR/body_open.txt" "$TEST_DIR/states_open"
exit_code=0
err="$TEST_DIR/open.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"not-all-closed-completed"* ]]; then
    pass "one former blocker open → exit 1 with clear:not-all-closed-completed"
else
    fail "one former blocker open → exit 1 with clear:not-all-closed-completed" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# One former blocker closed-as-not_planned → exit 1 not-all-closed-completed
mkdir -p "$TEST_DIR/states_np"
cat > "$TEST_DIR/body_np.txt" <<'BODYNP'
Was-blocked-by: #100, #101 (all closed-completed 2026-05-08; auto-unblocked)
BODYNP
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_np/100.json"
echo '{"state":"closed","stateReason":"NOT_PLANNED"}' > "$TEST_DIR/states_np/101.json"

write_mock_gh "$TEST_DIR/body_np.txt" "$TEST_DIR/states_np"
exit_code=0
err="$TEST_DIR/np.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"not-all-closed-completed"* ]]; then
    pass "former blocker closed-as-not_planned → exit 1 with clear:not-all-closed-completed"
else
    fail "former blocker closed-as-not_planned → exit 1 with clear:not-all-closed-completed" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# Empty body → exit 1 with no-marker
mkdir -p "$TEST_DIR/states_empty"
echo "" > "$TEST_DIR/body_empty.txt"

write_mock_gh "$TEST_DIR/body_empty.txt" "$TEST_DIR/states_empty"
exit_code=0
err="$TEST_DIR/empty.err"
output=$("$WRAPPER" 1 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"clear:"* && "$output" == *"no-marker"* ]]; then
    pass "empty body → exit 1 with clear:no-marker"
else
    fail "empty body → exit 1 with clear:no-marker" "exit=$exit_code output='$output' stderr=$(tail -n 50 "$err" 2>/dev/null)"
fi

# Strips a leading '#' from the issue argument
mkdir -p "$TEST_DIR/states_hash"
cat > "$TEST_DIR/body_hash.txt" <<'BODYH'
Was-blocked-by: #100 (all closed-completed 2026-05-08; auto-unblocked)
BODYH
echo '{"state":"closed","stateReason":"COMPLETED"}' > "$TEST_DIR/states_hash/100.json"

write_mock_gh "$TEST_DIR/body_hash.txt" "$TEST_DIR/states_hash"
exit_code=0
err="$TEST_DIR/hash.err"
output=$("$WRAPPER" "#42" 2>"$err") || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"was-blocked-by:"* ]]; then
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
