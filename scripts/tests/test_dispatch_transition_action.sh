#!/usr/bin/env bash
# test_dispatch_transition_action.sh — Regression tests for
# dispatch_transition_action() in scripts/dispatcher/agent-runner-entrypoint.sh
#
# Drives the helper by sourcing a thin stub shim of the entrypoint's runtime
# dependencies, then calls dispatch_transition_action once per action × hint
# matrix and asserts the correct downstream function was invoked.
#
# Asserts:
#   * advance       → advance_phase $_next
#   * advance_with_status → advance_phase $_next $_status
#   * route_to_diagnoser × each known hint → correct terminal
#   * route_to_diagnoser × novel hint → diagnoser_route_unrecognized_hint
#   * novel action → ${_phase}_transition_unrecognized terminal
#
# Uses _record_invocation.sh style invocation logging (write argv to file)
# so assertions can grep the log rather than inspecting stdout/stderr.
#
# bash 3.2 compatible.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh"
FAILURES=0
TESTS=0

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

TEST_TMP=""
cleanup() {
    set +eu
    if [[ "${DISPATCH_TEST_KEEP_TMP:-0}" == "1" ]]; then
        echo "DISPATCH_TEST_KEEP_TMP=1 — keeping $TEST_TMP for inspection"
        return
    fi
    if [[ -n "$TEST_TMP" && -d "$TEST_TMP" ]]; then
        rm -rf "$TEST_TMP"
    fi
}
trap cleanup EXIT

TEST_TMP=$(mktemp -d)
LOG_DIR="$TEST_TMP/logs"
mkdir -p "$LOG_DIR"

# ── Source shim: define stub functions and source dispatch_transition_action ──
# We extract dispatch_transition_action() from the entrypoint by source-ing
# a small wrapper that pre-defines all the functions it calls so that sourcing
# the relevant section of the entrypoint doesn't blow up.

if [[ ! -f "$ENTRYPOINT" ]]; then
    echo "SKIP: entrypoint not found at $ENTRYPOINT"
    exit 0
fi

# Verify the function exists in the entrypoint before trying to source it.
if ! grep -q '^dispatch_transition_action()' "$ENTRYPOINT"; then
    echo "SKIP: dispatch_transition_action() not found in $ENTRYPOINT"
    exit 0
fi

# Extract the dispatch_transition_action function body to a temp file.
# awk: start collecting at the function declaration line, stop at the first
# closing ``}`` at column 0 after the function body starts.
FUNC_FILE="$TEST_TMP/dispatch_func.sh"
awk '
    /^dispatch_transition_action\(\)/ { in_func=1; print; next }
    in_func {
        print
        if (/^\}[[:space:]]*$/) { exit }
    }
' "$ENTRYPOINT" > "$FUNC_FILE"

if [[ ! -s "$FUNC_FILE" ]]; then
    echo "SKIP: could not extract dispatch_transition_action() from $ENTRYPOINT"
    exit 0
fi

# ── Stub helpers that dispatch_transition_action() calls ──────────────────
# Each stub records its invocation to a per-function log file in LOG_DIR.

advance_phase() {
    printf 'advance_phase %s\n' "$*" >> "$LOG_DIR/advance_phase.log"
}

agent_runner_reaped_failure() {
    # $1=phase $2=reason $3=message
    printf 'agent_runner_reaped_failure %s %s\n' "$1" "$2" >> "$LOG_DIR/reaped_failure.log"
}

handle_ralph_not_ship_local() {
    printf 'handle_ralph_not_ship_local %s\n' "${1:-}" >> "$LOG_DIR/ralph_not_ship_local.log"
}

log() {
    printf 'log %s\n' "$*" >> "$LOG_DIR/log.log"
}

db_exec() {
    printf 'db_exec %s\n' "$*" >> "$LOG_DIR/db_exec.log"
}

# Source the extracted function.
# shellcheck source=/dev/null
source "$FUNC_FILE"

# ── Test helper: reset logs ───────────────────────────────────────────────
reset_logs() {
    : > "$LOG_DIR/advance_phase.log"
    : > "$LOG_DIR/reaped_failure.log"
    : > "$LOG_DIR/ralph_not_ship_local.log"
    : > "$LOG_DIR/log.log"
    : > "$LOG_DIR/db_exec.log"
}

# ── Test 1: advance → advance_phase $_next ─────────────────────────────────
reset_logs
dispatch_transition_action "planning" "advance" "ralph" "" ""

if grep -q "^advance_phase ralph$" "$LOG_DIR/advance_phase.log"; then
    pass "advance action → advance_phase ralph"
else
    fail "advance action → advance_phase ralph" \
         "advance_phase.log: $(cat "$LOG_DIR/advance_phase.log")"
fi

if ! grep -q "." "$LOG_DIR/reaped_failure.log"; then
    pass "advance action does not call agent_runner_reaped_failure"
else
    fail "advance action does not call agent_runner_reaped_failure" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 2: advance_with_status → advance_phase $_next $_status ───────────
reset_logs
dispatch_transition_action "verify" "advance_with_status" "awaiting_deploy" "succeeded" ""

if grep -q "^advance_phase awaiting_deploy succeeded$" "$LOG_DIR/advance_phase.log"; then
    pass "advance_with_status → advance_phase awaiting_deploy succeeded"
else
    fail "advance_with_status → advance_phase awaiting_deploy succeeded" \
         "advance_phase.log: $(cat "$LOG_DIR/advance_phase.log")"
fi

# ── Test 3: route_to_diagnoser × conflict_unresolvable ────────────────────
reset_logs
dispatch_transition_action "fix_conflict" "route_to_diagnoser" "" "" "conflict_unresolvable"

if grep -q "^agent_runner_reaped_failure conflict_unresolvable conflict_unresolvable$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=conflict_unresolvable → conflict_unresolvable terminal"
else
    fail "route_to_diagnoser hint=conflict_unresolvable → conflict_unresolvable terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 4: route_to_diagnoser × ralph_not_ship ───────────────────────────
reset_logs
_output='{"verdict": "BLOCKED", "block_reason": "test"}'
dispatch_transition_action "ralph" "route_to_diagnoser" "" "" "ralph_not_ship"

if grep -q "handle_ralph_not_ship_local" "$LOG_DIR/ralph_not_ship_local.log"; then
    pass "route_to_diagnoser hint=ralph_not_ship → handle_ralph_not_ship_local"
else
    fail "route_to_diagnoser hint=ralph_not_ship → handle_ralph_not_ship_local" \
         "ralph_not_ship_local.log: $(cat "$LOG_DIR/ralph_not_ship_local.log")"
fi

if ! grep -q "agent_runner_reaped_failure" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=ralph_not_ship does not call agent_runner_reaped_failure"
else
    fail "route_to_diagnoser hint=ralph_not_ship does not call agent_runner_reaped_failure" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 5: route_to_diagnoser × ralph_ac_infeasible ─────────────────────
reset_logs
dispatch_transition_action "ralph" "route_to_diagnoser" "" "" "ralph_ac_infeasible"

if grep -q "^agent_runner_reaped_failure ralph_ac_infeasible ralph_ac_infeasible$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=ralph_ac_infeasible → ralph_ac_infeasible terminal"
else
    fail "route_to_diagnoser hint=ralph_ac_infeasible → ralph_ac_infeasible terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 6: route_to_diagnoser × summary_ac_infeasible ───────────────────
reset_logs
dispatch_transition_action "summary" "route_to_diagnoser" "" "" "summary_ac_infeasible"

if grep -q "^agent_runner_reaped_failure summary_ac_infeasible summary_ac_infeasible$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=summary_ac_infeasible → summary_ac_infeasible terminal"
else
    fail "route_to_diagnoser hint=summary_ac_infeasible → summary_ac_infeasible terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 7: route_to_diagnoser × fix_ci_blocked ───────────────────────────
reset_logs
dispatch_transition_action "planning" "route_to_diagnoser" "" "" "fix_ci_blocked"

if grep -q "^agent_runner_reaped_failure fix_ci_blocked fix_ci_blocked$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=fix_ci_blocked → fix_ci_blocked terminal"
else
    fail "route_to_diagnoser hint=fix_ci_blocked → fix_ci_blocked terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 8: route_to_diagnoser × verify_failed_post_merge ────────────────
reset_logs
dispatch_transition_action "verify" "route_to_diagnoser" "" "" "verify_failed_post_merge"

if grep -q "^agent_runner_reaped_failure verify_failed_post_merge verify_failed_post_merge$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=verify_failed_post_merge → verify_failed_post_merge terminal"
else
    fail "route_to_diagnoser hint=verify_failed_post_merge → verify_failed_post_merge terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 9: route_to_diagnoser × push_and_pr_no_unmerged_files (#3543) ────
reset_logs
dispatch_transition_action "push_and_pr" "route_to_diagnoser" "" "" "push_and_pr_no_unmerged_files"

if grep -q "^agent_runner_reaped_failure push_and_pr_no_unmerged_files push_and_pr_no_unmerged_files$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=push_and_pr_no_unmerged_files → push_and_pr_no_unmerged_files terminal (#3543)"
else
    fail "route_to_diagnoser hint=push_and_pr_no_unmerged_files → push_and_pr_no_unmerged_files terminal (#3543)" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 10: route_to_diagnoser × operational_failed ─────────────────────
reset_logs
dispatch_transition_action "operational" "route_to_diagnoser" "" "" "operational_failed"

if grep -q "^agent_runner_reaped_failure operational_failed operational_failed$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=operational_failed → operational_failed terminal"
else
    fail "route_to_diagnoser hint=operational_failed → operational_failed terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 11: route_to_diagnoser × novel unknown hint ─────────────────────
reset_logs
dispatch_transition_action "verify" "route_to_diagnoser" "" "" "whatever_new_hint"

if grep -q "^agent_runner_reaped_failure diagnoser_route_unrecognized_hint diagnoser_route_unrecognized_hint$" "$LOG_DIR/reaped_failure.log"; then
    pass "route_to_diagnoser hint=whatever_new_hint → diagnoser_route_unrecognized_hint terminal"
else
    fail "route_to_diagnoser hint=whatever_new_hint → diagnoser_route_unrecognized_hint terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Test 12: novel action route_to_pluto → ${phase}_transition_unrecognized
reset_logs
dispatch_transition_action "planning" "route_to_pluto" "" "" ""

if grep -q "^agent_runner_reaped_failure planning_transition_unrecognized planning_transition_unrecognized$" "$LOG_DIR/reaped_failure.log"; then
    pass "novel action route_to_pluto → planning_transition_unrecognized terminal"
else
    fail "novel action route_to_pluto → planning_transition_unrecognized terminal" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

if ! grep -q "." "$LOG_DIR/advance_phase.log"; then
    pass "novel action does not call advance_phase"
else
    fail "novel action does not call advance_phase" \
         "advance_phase.log: $(cat "$LOG_DIR/advance_phase.log")"
fi

# ── Test 13: verify #3573 — ralph_baseline phase routes through helper ────
# The ralph_baseline arm now uses dispatch_transition_action, so a
# route_to_diagnoser from it (e.g. push_and_pr_no_unmerged_files after
# baseline rebase with no conflicts) reaches the correct terminal rather than
# emitting ralph_baseline_transition_unrecognized.
reset_logs
dispatch_transition_action "ralph_baseline" "route_to_diagnoser" "" "" "push_and_pr_no_unmerged_files"

if grep -q "^agent_runner_reaped_failure push_and_pr_no_unmerged_files push_and_pr_no_unmerged_files$" "$LOG_DIR/reaped_failure.log"; then
    pass "ralph_baseline route_to_diagnoser push_and_pr_no_unmerged_files → correct terminal (#3573)"
else
    fail "ralph_baseline route_to_diagnoser push_and_pr_no_unmerged_files → correct terminal (#3573)" \
         "reaped_failure.log: $(cat "$LOG_DIR/reaped_failure.log")"
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo "----"
echo "Tests: $TESTS, Failures: $FAILURES"
if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
exit 0
