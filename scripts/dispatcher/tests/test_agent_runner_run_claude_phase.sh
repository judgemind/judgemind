#!/usr/bin/env bash
# test_agent_runner_run_claude_phase.sh — Regression test for issue #3775.
#
# Verifies that agent_runner_run_claude_phase.sh is sourceable, defines all
# 5 extracted functions, and that the runtime timeout path (rc==124) emits
# the structured BLOCKED envelope introduced in #3766 rather than falling
# through to the empty-result branch.
#
# AC coverage:
#   AC1 — helper defines 5 named functions (sourceability + type checks).
#   AC2 — entrypoint sources the helper (static lint).
#   AC3 — this test is executable and passes against current code.
#   AC4 — the runtime test would FAIL if run_claude_phase had no rc==124
#          short-circuit (pre-#3766 code would produce "{}" instead of
#          the BLOCKED envelope).
#   AC5 — wired into CI via scripts/run-scripts-tests.sh with
#          TESTS_DIR=scripts/dispatcher/tests.
#
# Usage:
#   scripts/dispatcher/tests/test_agent_runner_run_claude_phase.sh
#
# Exit codes:
#   0 — all assertions passed.
#   1 — one or more assertions failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="$SCRIPT_DIR/dispatcher/agent_runner_run_claude_phase.sh"
ENTRYPOINT="$SCRIPT_DIR/dispatcher/agent-runner-entrypoint.sh"

FAILURES=0
TESTS=0

TEMP_DIRS=()
cleanup() {
    set +e
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

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

# ── Precondition: the helper exists ─────────────────────────────────────────

if [[ ! -f "$HELPER" ]]; then
    fail "helper script exists" "expected $HELPER to be present"
    echo
    echo "Tests: $TESTS, Failures: $FAILURES"
    exit 1
fi
pass "helper script $HELPER exists"

# ── Sourceability: sourcing defines all 5 functions ──────────────────────────

for fn in write_phase_input read_phase_output phase_to_skill run_claude_phase \
          claude_phase_timeout_seconds_by_phase; do
    if ! ( set +u; source "$HELPER"; type "$fn" >/dev/null 2>&1 ); then
        fail "helper defines $fn" \
            "sourcing $HELPER did not define the function $fn"
        echo
        echo "Tests: $TESTS, Failures: $FAILURES"
        exit 1
    fi
    pass "helper defines $fn function"
done

# ── phase_to_skill: phase name → skill suffix mapping ────────────────────────

check_phase_to_skill() {
    local phase="$1"
    local expected="$2"
    local got
    got=$(
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        phase_to_skill "$phase" 2>/dev/null
    )
    if [[ "$got" == "$expected" ]]; then
        pass "phase_to_skill $phase → $expected"
    else
        fail "phase_to_skill $phase → $expected" \
            "got: '$got'"
    fi
}

run_phase_to_skill_tests() {
    # Bash 3.2 compat: explicit pairs instead of declare -A.
    check_phase_to_skill "planning"    "plan"
    check_phase_to_skill "ralph"       "ralph"
    check_phase_to_skill "fix_ci"      "fix-ci"
    check_phase_to_skill "fix_conflict" "fix-conflict"
    check_phase_to_skill "verify"      "verify"
    check_phase_to_skill "retro"       "retro"
    check_phase_to_skill "summary"     "summary"
    check_phase_to_skill "operational" "operational"

    # Unknown phase → die (exits non-zero).
    if ( set +u; source "$HELPER"; phase_to_skill "unknown_bogus_phase" >/dev/null 2>&1 ); then
        fail "phase_to_skill unknown → non-zero exit" \
            "phase_to_skill did not die on unknown phase"
    else
        pass "phase_to_skill unknown phase → die (non-zero exit)"
    fi
}

run_phase_to_skill_tests

# ── claude_phase_timeout_seconds_by_phase: per-phase cap values ──────────────

run_timeout_lookup_tests() {
    # Verify a selection of phase → timeout mappings.
    check_timeout() {
        local phase="$1"
        local expected="$2"
        got=$(
            set +u
            # shellcheck disable=SC1090
            source "$HELPER"
            claude_phase_timeout_seconds_by_phase "$phase" 2>/dev/null
        )
        if [[ "$got" == "$expected" ]]; then
            pass "claude_phase_timeout_seconds_by_phase $phase → $expected"
        else
            fail "claude_phase_timeout_seconds_by_phase $phase → $expected" \
                "got: '$got'"
        fi
    }

    check_timeout "ralph"   "5400"
    check_timeout "planning" "1200"
    check_timeout "summary" "600"
    check_timeout "fix_ci"  "1800"
    check_timeout "retro"   "600"

    # Unknown phase → DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS (1800).
    got=$(
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        unset AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS || true
        unset AGENT_RUNNER_DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS || true
        claude_phase_timeout_seconds_by_phase "some_future_phase" 2>/dev/null
    )
    if [[ "$got" == "1800" ]]; then
        pass "claude_phase_timeout_seconds_by_phase unknown phase → 1800 (DEFAULT)"
    else
        fail "claude_phase_timeout_seconds_by_phase unknown phase → 1800 (DEFAULT)" \
            "got: '$got'"
    fi

    # Override env var takes precedence over per-phase constant.
    got=$(
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS=42
        claude_phase_timeout_seconds_by_phase "ralph" 2>/dev/null
    )
    if [[ "$got" == "42" ]]; then
        pass "AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS overrides ralph cap"
    else
        fail "AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS overrides ralph cap" \
            "got: '$got', expected: '42'"
    fi
}

run_timeout_lookup_tests

# ── read_phase_output: file resolution ───────────────────────────────────────

run_read_phase_output_tests() {
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local repo="$tmp/repo"
    mkdir -p "$repo/tmp/dispatcher-output"

    # Missing file → rc 1.
    if (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export REPO_ROOT="$repo"
        read_phase_output "nonexistent-skill" >/dev/null 2>/dev/null
    ); then
        fail "read_phase_output missing file → rc 1" \
            "expected non-zero exit for missing file"
    else
        pass "read_phase_output missing file → rc 1"
    fi

    # Valid JSON file → minified JSON on stdout, rc 0.
    printf '{\n  "verdict": "SHIP",\n  "x": 1\n}\n' \
        > "$repo/tmp/dispatcher-output/ralph.json"
    got=$(
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export REPO_ROOT="$repo"
        read_phase_output "ralph" 2>/dev/null
    )
    # jq -c outputs a single line of minified JSON.
    # Command substitution strips trailing newlines, so just check
    # the output contains the expected fields and starts with '{'.
    if echo "$got" | grep -q '"verdict"' && [[ "${got:0:1}" == "{" ]]; then
        pass "read_phase_output valid JSON → minified stdout, rc 0"
    else
        fail "read_phase_output valid JSON → minified stdout, rc 0" \
            "got: '$got'"
    fi

    # Malformed JSON → rc 1.
    printf 'THIS IS NOT JSON\n' \
        > "$repo/tmp/dispatcher-output/plan.json"
    if (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export REPO_ROOT="$repo"
        read_phase_output "plan" >/dev/null 2>/dev/null
    ); then
        fail "read_phase_output malformed JSON → rc 1" \
            "expected non-zero exit for malformed JSON"
    else
        pass "read_phase_output malformed JSON → rc 1"
    fi
}

run_read_phase_output_tests

# ── AC2: entrypoint sources the helper ───────────────────────────────────────

if [[ ! -f "$ENTRYPOINT" ]]; then
    fail "agent-runner-entrypoint.sh exists" "expected at $ENTRYPOINT"
else
    if grep -q "source.*agent_runner_run_claude_phase.sh" "$ENTRYPOINT"; then
        pass "entrypoint sources agent_runner_run_claude_phase.sh"
    else
        fail "entrypoint sources agent_runner_run_claude_phase.sh" \
            "grep 'source.*agent_runner_run_claude_phase.sh' found nothing in $ENTRYPOINT"
    fi
fi

# ── Runtime timeout regression (headline test) ───────────────────────────────
#
# Stubs ``claude`` to ``sleep 5``, sets AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS=1
# so the 1s timeout fires, and asserts that:
#   * run_claude_phase exits 0 (the function catches rc==124 internally).
#   * stdout contains the structured BLOCKED envelope with
#     "category":"claude_phase_timeout" and "verdict":"BLOCKED".
#   * stderr contains the ``claude_phase_timeout`` log event (from the
#     log() shim, which writes to stderr in standalone mode).
#
# This test FAILS against pre-#3766 code (no rc==124 short-circuit) because
# the function would fall through to the empty-result branch and produce
# ``{}`` → no "category" field → assertion fails.

run_timeout_regression_test() {
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local ws="$tmp/ws"
    local repo="$tmp/repo"
    local stub_bin="$tmp/bin"
    local err_file="$tmp/err.log"

    mkdir -p "$ws" "$repo/tmp/dispatcher-output" "$stub_bin"

    # Stub claude: sleep 5 so the 1s timeout fires.
    printf '#!/usr/bin/env bash\nsleep 5\n' > "$stub_bin/claude"
    chmod +x "$stub_bin/claude"

    # Stub python3: the PHASE_INPUT_SHIM just needs to exit 0.
    printf '#!/usr/bin/env bash\nexit 0\n' > "$stub_bin/python3"
    chmod +x "$stub_bin/python3"

    # Minimal shim.py (never actually called — python3 stub intercepts).
    local shim_py="$tmp/shim.py"
    printf '#!/usr/bin/env python3\nimport sys; sys.exit(0)\n' > "$shim_py"

    local output
    output=$(
        set +u
        export PATH="$stub_bin:$PATH"
        # shellcheck disable=SC1090
        source "$HELPER"
        export AGENT_RUNNER_DRY_RUN=0
        export AGENT_ID="test-agent"
        export AGENT_WORKSPACE="$ws"
        export REPO_ROOT="$repo"
        export PHASE_INPUT_SHIM="$shim_py"
        export AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS=1
        run_claude_phase ralph 2>"$err_file"
    )
    local exit_code=$?

    # Assert: exit 0 (run_claude_phase catches rc==124 internally).
    if [[ "$exit_code" -eq 0 ]]; then
        pass "runtime timeout: run_claude_phase ralph exits 0"
    else
        fail "runtime timeout: run_claude_phase ralph exits 0" \
            "exit code was $exit_code"
    fi

    # Assert: stdout contains "category":"claude_phase_timeout".
    if echo "$output" | grep -q '"category"' && \
       echo "$output" | grep -q 'claude_phase_timeout'; then
        pass "runtime timeout: stdout contains category=claude_phase_timeout"
    else
        fail "runtime timeout: stdout contains category=claude_phase_timeout" \
            "stdout was: $output"
    fi

    # Assert: stdout contains "verdict":"BLOCKED".
    if echo "$output" | grep -q '"verdict"' && \
       echo "$output" | grep -q 'BLOCKED'; then
        pass "runtime timeout: stdout contains verdict=BLOCKED"
    else
        fail "runtime timeout: stdout contains verdict=BLOCKED" \
            "stdout was: $output"
    fi

    # Assert: stderr contains the claude_phase_timeout log event.
    if grep -q "claude_phase_timeout" "$err_file"; then
        pass "runtime timeout: stderr contains claude_phase_timeout log event"
    else
        fail "runtime timeout: stderr contains claude_phase_timeout log event" \
            "stderr was: $(cat "$err_file")"
    fi
}

run_timeout_regression_test

# ── Summary ───────────────────────────────────────────────────────────────────

echo
echo "Tests: $TESTS, Failures: $FAILURES"

if (( FAILURES > 0 )); then
    exit 1
fi
exit 0
