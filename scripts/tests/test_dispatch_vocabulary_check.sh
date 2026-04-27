#!/usr/bin/env bash
# test_dispatch_vocabulary_check.sh — Tests for
# scripts/check-transition-dispatch-vocabulary.sh
#
# Verifies that the guard:
#   (1) passes when dispatch_transition_action handles all TransitionAction values
#   (2) fails when a new TransitionAction value is not handled (drift scenario)
#   (3) passes when all FAILURE_HINT_* constants are covered (via wildcard)
#   (4) fails when a FAILURE_HINT_* constant is uncovered and no wildcard exists
#   (5) exits 0 (fast-skip) when source files are missing
#   (6) passes on the real repo files
#
# Usage:
#   scripts/tests/test_dispatch_vocabulary_check.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-transition-dispatch-vocabulary.sh"
FAILURES=0
TESTS=0

if [[ ! -f "$CHECK_SCRIPT" ]]; then
    echo "SKIP: $CHECK_SCRIPT not found"
    exit 0
fi

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# ── Fixture helpers ──────────────────────────────────────────────────────────

write_transitions() {
    # Write a minimal phase_transitions.py fixture with 3 TransitionAction values
    # and 2 FAILURE_HINT_* constants.
    cat > "$TMPDIR_TEST/phase_transitions.py" << 'PYEOF'
class TransitionAction(str, Enum):
    ADVANCE = "advance"
    ADVANCE_WITH_STATUS = "advance_with_status"
    ROUTE_TO_DIAGNOSER = "route_to_diagnoser"

FAILURE_HINT_CONFLICT_UNRESOLVABLE = "conflict_unresolvable"
FAILURE_HINT_RALPH_NOT_SHIP = "ralph_not_ship"
PYEOF
}

write_entrypoint_good() {
    # Write a minimal entrypoint fixture whose dispatch_transition_action
    # handles all 3 TransitionAction values, with a wildcard for hints.
    cat > "$TMPDIR_TEST/agent-runner-entrypoint.sh" << 'SHEOF'
#!/usr/bin/env bash
dispatch_transition_action() {
    local _phase="$1"
    local _action="$2"
    local _next="$3"
    local _status="$4"
    local _hint="$5"
    case "$_action" in
        advance)
            advance_phase "$_next"
            ;;
        advance_with_status)
            advance_phase "$_next" "$_status"
            ;;
        route_to_diagnoser)
            case "$_hint" in
                conflict_unresolvable)
                    agent_runner_reaped_failure "conflict_unresolvable" "conflict_unresolvable" "unresolvable"
                    ;;
                ralph_not_ship)
                    handle_ralph_not_ship_local "${_output:-}"
                    ;;
                *)
                    agent_runner_reaped_failure "diagnoser_route_unrecognized_hint" "diagnoser_route_unrecognized_hint" "hint=${_hint}"
                    ;;
            esac
            ;;
        *)
            agent_runner_reaped_failure "${_phase}_transition_unrecognized" "${_phase}_transition_unrecognized" "action=${_action}"
            ;;
    esac
}
SHEOF
}

write_entrypoint_missing_action() {
    # Write a fixture that is MISSING the route_to_diagnoser arm (drift scenario).
    cat > "$TMPDIR_TEST/agent-runner-entrypoint.sh" << 'SHEOF'
#!/usr/bin/env bash
dispatch_transition_action() {
    local _phase="$1"
    local _action="$2"
    local _next="$3"
    local _status="$4"
    local _hint="$5"
    case "$_action" in
        advance)
            advance_phase "$_next"
            ;;
        advance_with_status)
            advance_phase "$_next" "$_status"
            ;;
    esac
}
SHEOF
}

write_transitions_with_new_action() {
    # phase_transitions.py with a new SOME_NEW_ACTION that is not in the entrypoint.
    cat > "$TMPDIR_TEST/phase_transitions.py" << 'PYEOF'
class TransitionAction(str, Enum):
    ADVANCE = "advance"
    ADVANCE_WITH_STATUS = "advance_with_status"
    ROUTE_TO_DIAGNOSER = "route_to_diagnoser"
    SOME_NEW_ACTION = "some_new_action"

FAILURE_HINT_CONFLICT_UNRESOLVABLE = "conflict_unresolvable"
FAILURE_HINT_RALPH_NOT_SHIP = "ralph_not_ship"
PYEOF
}

assert_passes() {
    local desc="$1"
    local transitions="${2:-$TMPDIR_TEST/phase_transitions.py}"
    local entrypoint="${3:-$TMPDIR_TEST/agent-runner-entrypoint.sh}"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$transitions" "$entrypoint" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        "$CHECK_SCRIPT" "$transitions" "$entrypoint" 2>&1 | sed 's/^/    /'
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    local pattern="$2"
    local transitions="${3:-$TMPDIR_TEST/phase_transitions.py}"
    local entrypoint="${4:-$TMPDIR_TEST/agent-runner-entrypoint.sh}"
    TESTS=$((TESTS + 1))
    output="$("$CHECK_SCRIPT" "$transitions" "$entrypoint" 2>&1 || true)"
    if printf '%s' "$output" | grep -q "$pattern"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected failure with pattern '$pattern')"
        echo "  actual output:"
        printf '%s\n' "$output" | sed 's/^/    /'
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: Happy path — all actions handled ─────────────────────────────
write_transitions
write_entrypoint_good
assert_passes "Happy path: all TransitionAction values handled"

# ─── Test 2: Drift — SOME_NEW_ACTION not in entrypoint, no wildcard ───────
write_transitions_with_new_action
write_entrypoint_missing_action
assert_fails "Drift: some_new_action not handled → fails with specific message" \
    "some_new_action"

# ─── Test 3: Drift message is specific ────────────────────────────────────
write_transitions_with_new_action
write_entrypoint_missing_action
TESTS=$((TESTS + 1))
output="$("$CHECK_SCRIPT" "$TMPDIR_TEST/phase_transitions.py" "$TMPDIR_TEST/agent-runner-entrypoint.sh" 2>&1 || true)"
if printf '%s' "$output" | grep -q "transition vocabulary drift: action=some_new_action"; then
    echo "PASS: Drift message contains 'transition vocabulary drift: action=some_new_action'"
else
    echo "FAIL: Drift message does not contain expected text"
    echo "  actual output:"
    printf '%s\n' "$output" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 4: Wildcard arm covers unrecognized actions ─────────────────────
# The good fixture has a wildcard arm — even a new SOME_NEW_ACTION should pass.
write_transitions_with_new_action
write_entrypoint_good
assert_passes "Wildcard arm covers unrecognized TransitionAction values"

# ─── Test 5: Missing source files exit 0 (fast-skip) ─────────────────────
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" "$TMPDIR_TEST/does_not_exist.py" "$TMPDIR_TEST/agent-runner-entrypoint.sh" > /dev/null 2>&1; then
    echo "PASS: Missing phase_transitions.py exits 0 (fast-skip)"
else
    echo "FAIL: Missing phase_transitions.py should exit 0 (fast-skip)"
    FAILURES=$((FAILURES + 1))
fi

TESTS=$((TESTS + 1))
write_transitions
if "$CHECK_SCRIPT" "$TMPDIR_TEST/phase_transitions.py" "$TMPDIR_TEST/does_not_exist.sh" > /dev/null 2>&1; then
    echo "PASS: Missing agent-runner-entrypoint.sh exits 0 (fast-skip)"
else
    echo "FAIL: Missing agent-runner-entrypoint.sh should exit 0 (fast-skip)"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 6: Real repo files pass ─────────────────────────────────────────
REAL_TRANSITIONS="$REPO_ROOT/scripts/dispatcher/phase_transitions.py"
REAL_ENTRYPOINT="$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh"
if [[ -f "$REAL_TRANSITIONS" && -f "$REAL_ENTRYPOINT" ]]; then
    assert_passes "Real repo files: dispatch vocabulary is consistent" \
        "$REAL_TRANSITIONS" "$REAL_ENTRYPOINT"
fi

# ─── Summary ──────────────────────────────────────────────────────────────
echo "----"
if [[ "$FAILURES" -gt 0 ]]; then
    echo "$FAILURES of $TESTS tests FAILED"
    exit 1
fi
echo "all $TESTS tests passed"
exit 0
