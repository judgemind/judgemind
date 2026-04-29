#!/usr/bin/env bash
# test_dispatch_transition_action.sh — Tests for the centralized
# transition-action dispatch helper (#3581).
#
# What this exercises
# -------------------
# The ``dispatch_transition_action`` helper in
# ``scripts/dispatcher/agent-runner-entrypoint.sh`` is the single source
# of truth for translating a ``transition_for`` tuple into the right
# downstream call (advance_phase / advance_phase with terminal status /
# descriptive terminal via agent_runner_reaped_failure). Before #3581,
# every phase had its own duplicate case-statement; new actions / hints
# would silently bypass any phase the PR author missed (#3543, #3558,
# #3573, #3580 family).
#
# These tests verify the helper handles every action arm correctly:
#   * advance → calls advance_phase $next
#   * advance_with_status → calls advance_phase $next $status
#   * route_to_diagnoser + each known FAILURE_HINT_* → calls
#     agent_runner_reaped_failure with the descriptive terminal
#   * route_to_diagnoser + unknown hint → diagnoser_route_unrecognized_hint
#   * unrecognized action → ${phase}_transition_unrecognized
#
# Test method: extract the helper definition (and its dependencies:
# ``log``, ``advance_phase``, ``agent_runner_reaped_failure``) into a
# sandbox script that stubs DB writes, then invoke each action arm and
# assert the recorded calls match expectations.
#
# Usage:
#   scripts/tests/test_dispatch_transition_action.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh"

TESTS=0
FAILURES=0

pass() { TESTS=$((TESTS + 1)); echo "  PASS: $1"; }
fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "  FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "         $2"
    fi
}

# Build a sandbox harness that defines the stubs first, then sources
# only the dispatch_transition_action helper definition out of the
# entrypoint. The helper depends on:
#   - ``log $event $kv...`` — we record events to a log file.
#   - ``advance_phase $next [$status]`` — we record calls to a calls file.
#   - ``agent_runner_reaped_failure $term_phase $category $reason`` — we
#     record calls and exit 0 (mirrors the real function's contract).

TEST_TMP=$(mktemp -d)
trap 'rm -rf "$TEST_TMP"' EXIT

extract_helper() {
    # Print the dispatch_transition_action function body from the
    # entrypoint. The function spans from its definition to the
    # matching closing ``}`` at column 0. We use a small awk to find
    # the start and copy until the next column-0 ``}``.
    awk '
        /^dispatch_transition_action\(\) \{/ { in_fn = 1 }
        in_fn { print }
        in_fn && /^}/ { exit }
    ' "$ENTRYPOINT"
}

build_harness() {
    cat > "$TEST_TMP/harness.sh" <<'EOF'
#!/usr/bin/env bash
# Test harness for dispatch_transition_action.

CALLS_FILE="${HARNESS_CALLS_FILE:-/tmp/calls}"
LOGS_FILE="${HARNESS_LOGS_FILE:-/tmp/logs}"

log() {
    # $1 = event, remaining args = key=value pairs.
    local event="$1"
    shift
    local line="$event"
    for kv in "$@"; do
        line="$line $kv"
    done
    printf '%s\n' "$line" >> "$LOGS_FILE"
}

advance_phase() {
    # $1 = next, $2 = status (optional).
    if [[ -n "${2:-}" ]]; then
        printf 'advance_phase next=%s status=%s\n' "$1" "$2" >> "$CALLS_FILE"
    else
        printf 'advance_phase next=%s\n' "$1" >> "$CALLS_FILE"
    fi
}

agent_runner_reaped_failure() {
    # $1 = term_phase, $2 = category, $3 = reason.
    printf 'reaped term=%s category=%s reason=%s\n' "$1" "$2" "$3" >> "$CALLS_FILE"
}

EOF
    extract_helper >> "$TEST_TMP/harness.sh"
    cat >> "$TEST_TMP/harness.sh" <<'EOF'

# Run the helper with the args from $@.
dispatch_transition_action "$@"
EOF
    chmod +x "$TEST_TMP/harness.sh"
}

run_helper() {
    local calls_file="$TEST_TMP/calls.$$"
    local logs_file="$TEST_TMP/logs.$$"
    : > "$calls_file"
    : > "$logs_file"
    HARNESS_CALLS_FILE="$calls_file" \
        HARNESS_LOGS_FILE="$logs_file" \
        bash "$TEST_TMP/harness.sh" "$@"
    local rc=$?
    LAST_CALLS=$(cat "$calls_file")
    LAST_LOGS=$(cat "$logs_file")
    rm -f "$calls_file" "$logs_file"
    return $rc
}

build_harness

echo
echo "=== dispatch_transition_action: action arm coverage ==="

# 1. advance — calls advance_phase $next.
run_helper "summary" "advance" "push_and_pr" "" "" "{}"
expected="advance_phase next=push_and_pr"
if [[ "$LAST_CALLS" == "$expected" ]]; then
    pass "advance arm calls advance_phase with next phase"
else
    fail "advance arm calls advance_phase with next phase" \
        "expected '$expected', got '$LAST_CALLS'"
fi

# 2. advance_with_status — calls advance_phase $next $status.
run_helper "verify" "advance_with_status" "retro" "succeeded" "" "{}"
expected="advance_phase next=retro status=succeeded"
if [[ "$LAST_CALLS" == "$expected" ]]; then
    pass "advance_with_status arm calls advance_phase with next + status"
else
    fail "advance_with_status arm calls advance_phase with next + status" \
        "expected '$expected', got '$LAST_CALLS'"
fi

# 3. route_to_diagnoser + conflict_unresolvable hint.
run_helper "fix_conflict" "route_to_diagnoser" "" "" "conflict_unresolvable" "{}"
if [[ "$LAST_CALLS" == *"reaped term=conflict_unresolvable category=conflict_unresolvable"* ]]; then
    pass "route_to_diagnoser + conflict_unresolvable -> conflict_unresolvable terminal"
else
    fail "route_to_diagnoser + conflict_unresolvable -> conflict_unresolvable terminal" \
        "got: $LAST_CALLS"
fi

# 4. route_to_diagnoser + ralph_not_ship hint, with block_reason in
#    the output JSON. The helper extracts block_reason via jq and
#    passes it as the reason arg.
output_json='{"verdict":"REVISE","block_reason":"max iterations reached"}'
run_helper "ralph" "route_to_diagnoser" "" "" "ralph_not_ship" "$output_json"
if [[ "$LAST_CALLS" == *"reaped term=ralph_not_ship category=ralph_not_ship reason=max iterations reached"* ]]; then
    pass "route_to_diagnoser + ralph_not_ship extracts block_reason from output"
else
    fail "route_to_diagnoser + ralph_not_ship extracts block_reason from output" \
        "got: $LAST_CALLS"
fi

# 5. route_to_diagnoser + each known descriptive hint — sample three of
#    the cluster from FAILURE_HINT_*.
for hint in ralph_ac_infeasible summary_ac_infeasible fix_ci_blocked verify_failed_post_merge push_and_pr_no_unmerged_files operational_failed plan_blocked; do
    run_helper "post_claude" "route_to_diagnoser" "" "" "$hint" "{}"
    if [[ "$LAST_CALLS" == *"reaped term=$hint category=$hint"* ]]; then
        pass "route_to_diagnoser + $hint -> $hint descriptive terminal"
    else
        fail "route_to_diagnoser + $hint -> $hint descriptive terminal" \
            "got: $LAST_CALLS"
    fi
done

# 6. route_to_diagnoser + truly novel hint — emits
#    diagnoser_route_unrecognized_hint.
run_helper "post_claude" "route_to_diagnoser" "" "" "totally_made_up_hint_999" "{}"
if [[ "$LAST_CALLS" == *"reaped term=diagnoser_route_unrecognized_hint"* ]]; then
    pass "route_to_diagnoser + novel hint -> diagnoser_route_unrecognized_hint"
else
    fail "route_to_diagnoser + novel hint -> diagnoser_route_unrecognized_hint" \
        "got: $LAST_CALLS"
fi
# And the log line carries the phase + hint for operator triage.
if [[ "$LAST_LOGS" == *"post_claude_route_unrecognized_hint hint=totally_made_up_hint_999"* ]]; then
    pass "novel-hint path emits phase_tagged_route_unrecognized_hint log"
else
    fail "novel-hint path emits phase_tagged_route_unrecognized_hint log" \
        "got: $LAST_LOGS"
fi

# 7. Unrecognized action — emits ${phase}_transition_unrecognized.
run_helper "summary" "totally_unknown_action_kind" "" "" "" "{}"
if [[ "$LAST_CALLS" == *"reaped term=summary_transition_unrecognized category=summary_transition_unrecognized"* ]]; then
    pass "novel action -> phase_tagged_transition_unrecognized terminal"
else
    fail "novel action -> phase_tagged_transition_unrecognized terminal" \
        "got: $LAST_CALLS"
fi

# 8. Per-phase tagging — the same novel action with different phase
#    names should produce phase-tagged terminals so operator can tell
#    which phase emitted the bad shape.
run_helper "fix_conflict" "totally_unknown_action_kind" "" "" "" "{}"
if [[ "$LAST_CALLS" == *"reaped term=fix_conflict_transition_unrecognized"* ]]; then
    pass "unrecognized-action terminal is phase-tagged (fix_conflict)"
else
    fail "unrecognized-action terminal is phase-tagged (fix_conflict)" \
        "got: $LAST_CALLS"
fi

# 9. The ``unrecognized`` action enum value (defensive sentinel from
#    phase_transitions.py) routes through the same branch as truly
#    novel actions.
run_helper "ralph" "unrecognized" "" "" "" "{}"
if [[ "$LAST_CALLS" == *"reaped term=ralph_transition_unrecognized"* ]]; then
    pass "explicit 'unrecognized' action -> phase_tagged_transition_unrecognized"
else
    fail "explicit 'unrecognized' action -> phase_tagged_transition_unrecognized" \
        "got: $LAST_CALLS"
fi

# ── Summary ───────────────────────────────────────────────────────────
echo
echo "=== Summary ==="
echo "  $TESTS test(s), $FAILURES failure(s)"

if [[ "$FAILURES" -ne 0 ]]; then
    exit 1
fi

echo "  ALL PASSED"
exit 0
