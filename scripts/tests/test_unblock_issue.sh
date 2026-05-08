#!/usr/bin/env bash
# test_unblock_issue.sh — Tests for scripts/unblock-issue.sh and scripts/_unblock_issue.py
#
# Uses PATH-mocked gh CLI (pattern from test_check_duplicate_pr.sh) so no real
# GitHub API calls are made. Each mock writes every invocation to a tempfile
# so assertions can verify which ``gh issue edit`` args were used.
#
# Test cases:
#   A — Mixed closed/open blockers: only closed-blocker line is pruned; status/blocked stays.
#   B — All blockers closed: all Blocked by lines pruned; status/blocked → agent/ready.
#   C — CLI flags: --help exits 0; --dry-run makes no writes; missing arg exits 1; bad arg exits 1.
#   D — Idempotence: body with no Blocked by lines → no-op exit 0, no writes.
#
# Usage:
#   scripts/tests/test_unblock_issue.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/unblock-issue.sh"
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

# ── Test C — CLI flag checks (no gh mock needed for these) ─────────────────

# --help exits 0
exit_code=0
"$WRAPPER" --help > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "--help exits 0"
else
    fail "--help exits 0" "expected exit 0, got $exit_code"
fi

# --help prints usage containing 'unblock-issue.sh'
help_output=$("$WRAPPER" --help 2>/dev/null)
if [[ "$help_output" == *"unblock-issue.sh"* ]]; then
    pass "--help prints usage line"
else
    fail "--help prints usage line" "expected 'unblock-issue.sh' in output"
fi

# missing argument exits 1
exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "missing argument exits 1"
else
    fail "missing argument exits 1" "expected exit 1, got $exit_code"
fi

# non-numeric argument exits 1
exit_code=0
"$WRAPPER" "abc" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "non-numeric argument exits 1"
else
    fail "non-numeric argument exits 1" "expected exit 1, got $exit_code"
fi

# ── Set up a mock gh CLI on PATH for the remaining tests ──────────────────

MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
ORIG_PATH_SAVE="$PATH"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

# ── Test A — Mixed closed/open blockers ────────────────────────────────────
#
# Issue 999 body: "Blocked by #100\nBlocked by #200"
# #100 is closed, #200 is open
# labels: ["status/blocked"]
# Expected: body updated with only "Blocked by #200" remaining; NO label flip

INVOCATIONS_A="$MOCK_BIN_DIR/invocations_a.txt"
WRITTEN_BODY_A="$MOCK_BIN_DIR/written_body_a.txt"

# Write the mock issue data file that gh would return.
# The shell script calls: gh issue view 999 --repo ... --json body,labels
# We mock this by writing a gh that, for "issue view 999", emits the JSON.
cat > "$MOCK_BIN_DIR/gh" << MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$INVOCATIONS_A"
# Dispatch on subcommand + args
if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    ISSUE_NUM="\${3:-}"
    # Main issue fetch
    if [[ "\$ISSUE_NUM" == "999" ]]; then
        printf '%s' '{"body":"Blocked by #100\nBlocked by #200","labels":[{"name":"status/blocked"}]}'
        exit 0
    fi
    # Blocker state checks
    if [[ "\$ISSUE_NUM" == "100" ]]; then
        printf '%s' '{"state":"CLOSED"}'
        exit 0
    fi
    if [[ "\$ISSUE_NUM" == "200" ]]; then
        printf '%s' '{"state":"OPEN"}'
        exit 0
    fi
fi
if [[ "\${1:-}" == "issue" && "\${2:-}" == "edit" ]]; then
    # Capture what body file was written (--body-file arg)
    prev=""
    for arg in "\$@"; do
        if [[ "\$prev" == "--body-file" ]]; then
            cat "\$arg" > "$WRITTEN_BODY_A"
        fi
        prev="\$arg"
    done
    exit 0
fi
exit 0
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
"$WRAPPER" 999 > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "Test A: exits 0 with mixed closed/open blockers"
else
    fail "Test A: exits 0 with mixed closed/open blockers" "expected exit 0, got $exit_code"
fi

# Body written must contain "Blocked by #200" (open blocker kept)
if [[ -f "$WRITTEN_BODY_A" ]] && grep -q "Blocked by #200" "$WRITTEN_BODY_A"; then
    pass "Test A: open blocker line (#200) retained in written body"
else
    fail "Test A: open blocker line (#200) retained in written body" "written body: $(cat "$WRITTEN_BODY_A" 2>/dev/null || echo '<not written>')"
fi

# Body written must NOT contain "Blocked by #100" (closed blocker pruned)
if [[ -f "$WRITTEN_BODY_A" ]] && ! grep -q "Blocked by #100" "$WRITTEN_BODY_A"; then
    pass "Test A: closed blocker line (#100) pruned from written body"
else
    fail "Test A: closed blocker line (#100) pruned from written body" "written body: $(cat "$WRITTEN_BODY_A" 2>/dev/null || echo '<not written>')"
fi

# No --remove-label status/blocked invocation (open blocker still present)
if grep -q -- "--remove-label" "$INVOCATIONS_A" 2>/dev/null; then
    fail "Test A: no label flip when open blockers remain" "found --remove-label in invocations"
else
    pass "Test A: no label flip when open blockers remain"
fi

# ── Test B — All blockers closed ───────────────────────────────────────────
#
# Issue 888 body: "Blocked by #100\nBlocked by #200"
# Both closed; labels: ["status/blocked"]
# Expected: body has zero Blocked by lines; status/blocked removed, agent/ready added.

INVOCATIONS_B="$MOCK_BIN_DIR/invocations_b.txt"
WRITTEN_BODY_B="$MOCK_BIN_DIR/written_body_b.txt"

cat > "$MOCK_BIN_DIR/gh" << MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$INVOCATIONS_B"
if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    ISSUE_NUM="\${3:-}"
    if [[ "\$ISSUE_NUM" == "888" ]]; then
        printf '%s' '{"body":"Blocked by #100\nBlocked by #200","labels":[{"name":"status/blocked"}]}'
        exit 0
    fi
    if [[ "\$ISSUE_NUM" == "100" ]]; then
        printf '%s' '{"state":"CLOSED"}'
        exit 0
    fi
    if [[ "\$ISSUE_NUM" == "200" ]]; then
        printf '%s' '{"state":"CLOSED"}'
        exit 0
    fi
fi
if [[ "\${1:-}" == "issue" && "\${2:-}" == "edit" ]]; then
    prev=""
    for arg in "\$@"; do
        if [[ "\$prev" == "--body-file" ]]; then
            cat "\$arg" > "$WRITTEN_BODY_B"
        fi
        prev="\$arg"
    done
    exit 0
fi
exit 0
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
"$WRAPPER" 888 > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "Test B: exits 0 when all blockers closed"
else
    fail "Test B: exits 0 when all blockers closed" "expected exit 0, got $exit_code"
fi

# Written body must have zero "Blocked by" lines
if [[ -f "$WRITTEN_BODY_B" ]] && ! grep -q "Blocked by" "$WRITTEN_BODY_B"; then
    pass "Test B: all Blocked by lines pruned from written body"
else
    fail "Test B: all Blocked by lines pruned from written body" "written body: $(cat "$WRITTEN_BODY_B" 2>/dev/null || echo '<not written>')"
fi

# Exactly one invocation with --remove-label status/blocked and --add-label agent/ready
if grep -q -- "--remove-label" "$INVOCATIONS_B" 2>/dev/null; then
    if grep -q -- "agent/ready" "$INVOCATIONS_B" 2>/dev/null; then
        pass "Test B: label flip fired (status/blocked removed, agent/ready added)"
    else
        fail "Test B: label flip fired" "found --remove-label but not agent/ready in invocations"
    fi
else
    fail "Test B: label flip fired (status/blocked removed, agent/ready added)" "no --remove-label found in invocations"
fi

# ── Test C — --dry-run makes no gh issue edit writes ─────────────────────
#
# Same scenario as Test B (all blockers closed) but with --dry-run.
# Expected: no gh issue edit calls at all.

INVOCATIONS_C="$MOCK_BIN_DIR/invocations_c.txt"

cat > "$MOCK_BIN_DIR/gh" << MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$INVOCATIONS_C"
if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    ISSUE_NUM="\${3:-}"
    if [[ "\$ISSUE_NUM" == "777" ]]; then
        printf '%s' '{"body":"Blocked by #100\nBlocked by #200","labels":[{"name":"status/blocked"}]}'
        exit 0
    fi
    if [[ "\$ISSUE_NUM" == "100" ]]; then
        printf '%s' '{"state":"CLOSED"}'
        exit 0
    fi
    if [[ "\$ISSUE_NUM" == "200" ]]; then
        printf '%s' '{"state":"CLOSED"}'
        exit 0
    fi
fi
exit 0
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
"$WRAPPER" --dry-run 777 > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "Test C (dry-run): exits 0"
else
    fail "Test C (dry-run): exits 0" "expected exit 0, got $exit_code"
fi

if grep -q "issue edit" "$INVOCATIONS_C" 2>/dev/null; then
    fail "Test C (dry-run): no gh issue edit calls made" "found 'issue edit' in invocations"
else
    pass "Test C (dry-run): no gh issue edit calls made"
fi

# ── Test D — Idempotence: body with no Blocked by lines → no-op ────────────
#
# Issue 666 body has no Blocked by lines.
# Expected: exits 0, no gh issue edit calls.

INVOCATIONS_D="$MOCK_BIN_DIR/invocations_d.txt"

cat > "$MOCK_BIN_DIR/gh" << MOCKEOF
#!/usr/bin/env bash
echo "\$@" >> "$INVOCATIONS_D"
if [[ "\${1:-}" == "issue" && "\${2:-}" == "view" ]]; then
    ISSUE_NUM="\${3:-}"
    if [[ "\$ISSUE_NUM" == "666" ]]; then
        printf '%s' '{"body":"## Summary\n\nNo blockers here.","labels":[]}'
        exit 0
    fi
fi
exit 0
MOCKEOF
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
"$WRAPPER" 666 > /dev/null 2>&1 || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "Test D (idempotence): exits 0 with no Blocked by lines"
else
    fail "Test D (idempotence): exits 0 with no Blocked by lines" "expected exit 0, got $exit_code"
fi

if grep -q "issue edit" "$INVOCATIONS_D" 2>/dev/null; then
    fail "Test D (idempotence): no gh issue edit calls made" "found 'issue edit' in invocations"
else
    pass "Test D (idempotence): no gh issue edit calls made"
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
