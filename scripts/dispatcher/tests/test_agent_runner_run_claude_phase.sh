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

# ── Sourceability: sourcing defines all named functions ─────────────────────

# #4099: ``_ms_now`` is the helper's portable epoch-ms shim, used by
# ``run_claude_phase`` for ``_phase_start_ms`` / ``_phase_end_ms``. The
# previous in-line ``date -u +%s%3N`` pattern silently emitted a literal
# ``N`` on macOS BSD date (e.g. ``17780132253N``), poisoning the
# downstream ``$((end - start))`` arithmetic with
# ``value too great for base``. Listing ``_ms_now`` here so a future
# refactor that drops or renames the helper trips this static lint.
#
# #4125: ``_resolve_timeout_cmd`` is the helper's portable timeout(1)
# resolver — picks ``timeout`` (Linux) or ``gtimeout`` (macOS+coreutils)
# at run time, falls back to empty string when neither is on PATH so a
# fresh Mac without coreutils still exec's ``claude -p`` directly
# instead of aborting with rc=127 ("command not found"). Listing
# ``_resolve_timeout_cmd`` here so a future refactor that drops the
# resolver re-introduces the macOS portability gap and trips this lint.
for fn in _ms_now _resolve_timeout_cmd write_phase_input read_phase_output \
          phase_to_skill run_claude_phase \
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

# ── #4099 regression: _ms_now produces clean integer (no trailing N) ────────
#
# The bug being prevented: GNU date format ``+%s%3N`` is unsupported on
# macOS BSD date, which silently emits a literal ``N`` appended to the
# seconds value. The helper routes through python3 to avoid the BSD/GNU
# split. Verify the function returns either a non-negative integer or
# the literal 'unknown' fallback (no other strings are valid).
ms_now_output=$(
    set +u
    # shellcheck disable=SC1090
    source "$HELPER"
    _ms_now
)
if [[ "$ms_now_output" =~ ^[0-9]+$ ]] || [[ "$ms_now_output" == 'unknown' ]]; then
    pass "_ms_now returns integer ms or 'unknown' (#4099 — got '$ms_now_output')"
else
    fail "_ms_now returns integer ms or 'unknown' (#4099)" \
        "expected ^[0-9]+$ or 'unknown', got '$ms_now_output'"
fi

# ── #4099 regression: arithmetic on _ms_now output succeeds under set -e ────
#
# The original failure mode: ``$((end_ms - start_ms))`` aborted under
# ``set -e`` when start/end carried the literal ``N`` token. Verify the
# arithmetic shape from ``run_claude_phase`` succeeds end-to-end with
# the new ``_ms_now`` helper.
arithmetic_ok=$(
    set -e
    set +u
    # shellcheck disable=SC1090
    source "$HELPER"
    _start=$(_ms_now)
    _end=$(_ms_now)
    if [[ "$_start" == 'unknown' || "$_end" == 'unknown' ]]; then
        printf 'fallback'
    else
        _delta=$((_end - _start))
        if (( _delta >= 0 )); then
            printf 'ok'
        else
            printf 'negative'
        fi
    fi
)
if [[ "$arithmetic_ok" == 'ok' ]] || [[ "$arithmetic_ok" == 'fallback' ]]; then
    pass "_ms_now output supports \$((end - start)) arithmetic under set -e (#4099 — '$arithmetic_ok')"
else
    fail "_ms_now output supports \$((end - start)) arithmetic under set -e (#4099)" \
        "expected 'ok' or 'fallback', got '$arithmetic_ok'"
fi

# ── #4099 regression: stubbed python3 (rc=0, empty stdout) → 'unknown' ──────
#
# Tests in this very repo stub python3 with ``#!/usr/bin/env bash\nexit 0\n``
# to keep the PHASE_INPUT_SHIM lookup from blocking the dispatch path. A
# naive ``python3 -c ... 2>/dev/null || printf 'unknown'`` would NOT trip
# the fallback (rc=0 with empty stdout is treated as success), leaking the
# empty string into the downstream ``$((end - start))`` arithmetic. The
# helper validates the captured stdout is a non-empty digit string and
# falls back to 'unknown' otherwise. Verify the fallback fires.
broken_stub_tmp=$(mktemp -d)
TEMP_DIRS+=("$broken_stub_tmp")
printf '#!/usr/bin/env bash\nexit 0\n' > "$broken_stub_tmp/python3"
chmod +x "$broken_stub_tmp/python3"
broken_stub_output=$(
    set +u
    export PATH="$broken_stub_tmp:$PATH"
    # shellcheck disable=SC1090
    source "$HELPER"
    _ms_now
)
if [[ "$broken_stub_output" == 'unknown' ]]; then
    pass "_ms_now falls back to 'unknown' when python3 returns rc=0 with empty stdout (#4099)"
else
    fail "_ms_now falls back to 'unknown' when python3 returns rc=0 with empty stdout (#4099)" \
        "expected 'unknown', got '$broken_stub_output'"
fi

# ── #4125 regression: _resolve_timeout_cmd — macOS portability ─────────────
#
# The bug being prevented: bare ``timeout`` invocation in run_claude_phase
# returns rc=127 ("command not found") on macOS BSD without coreutils,
# which never trips the rc==124 short-circuit and silently breaks the
# runtime-timeout regression below. The resolver picks ``timeout`` /
# ``gtimeout`` at runtime via ``command -v`` lookups, falling back to an
# empty string so the call site exec's the inner command directly when
# neither is on PATH. Verify all three branches.

# Case 1: PATH has only ``timeout`` (Linux-shape).
timeout_only_tmp=$(mktemp -d)
TEMP_DIRS+=("$timeout_only_tmp")
printf '#!/usr/bin/env bash\nexit 0\n' > "$timeout_only_tmp/timeout"
chmod +x "$timeout_only_tmp/timeout"
timeout_only_output=$(
    set +u
    # Empty PATH except our stub so ``command -v gtimeout`` can't find
    # a coreutils install on the test host.
    export PATH="$timeout_only_tmp"
    # shellcheck disable=SC1090
    source "$HELPER"
    _resolve_timeout_cmd
)
if [[ "$timeout_only_output" == 'timeout' ]]; then
    pass "_resolve_timeout_cmd picks 'timeout' when only timeout is on PATH (#4125 — Linux shape)"
else
    fail "_resolve_timeout_cmd picks 'timeout' when only timeout is on PATH (#4125 — Linux shape)" \
        "expected 'timeout', got '$timeout_only_output'"
fi

# Case 2: PATH has only ``gtimeout`` (macOS + coreutils shape).
gtimeout_only_tmp=$(mktemp -d)
TEMP_DIRS+=("$gtimeout_only_tmp")
printf '#!/usr/bin/env bash\nexit 0\n' > "$gtimeout_only_tmp/gtimeout"
chmod +x "$gtimeout_only_tmp/gtimeout"
gtimeout_only_output=$(
    set +u
    export PATH="$gtimeout_only_tmp"
    # shellcheck disable=SC1090
    source "$HELPER"
    _resolve_timeout_cmd
)
if [[ "$gtimeout_only_output" == 'gtimeout' ]]; then
    pass "_resolve_timeout_cmd picks 'gtimeout' when only gtimeout is on PATH (#4125 — macOS+coreutils shape)"
else
    fail "_resolve_timeout_cmd picks 'gtimeout' when only gtimeout is on PATH (#4125 — macOS+coreutils shape)" \
        "expected 'gtimeout', got '$gtimeout_only_output'"
fi

# Case 3: PATH has neither (fresh Mac without coreutils).
neither_tmp=$(mktemp -d)
TEMP_DIRS+=("$neither_tmp")
neither_output=$(
    set +u
    export PATH="$neither_tmp"
    # shellcheck disable=SC1090
    source "$HELPER"
    _resolve_timeout_cmd
)
if [[ -z "$neither_output" ]]; then
    pass "_resolve_timeout_cmd returns empty string when neither is on PATH (#4125 — fresh Mac)"
else
    fail "_resolve_timeout_cmd returns empty string when neither is on PATH (#4125 — fresh Mac)" \
        "expected empty string, got '$neither_output'"
fi

# Case 4: PATH has both — ``timeout`` wins (Linux precedence over Mac+coreutils;
# avoids surprising operators who installed coreutils via brew on a Linux
# distro that already ships its own coreutils).
both_tmp=$(mktemp -d)
TEMP_DIRS+=("$both_tmp")
printf '#!/usr/bin/env bash\nexit 0\n' > "$both_tmp/timeout"
printf '#!/usr/bin/env bash\nexit 0\n' > "$both_tmp/gtimeout"
chmod +x "$both_tmp/timeout" "$both_tmp/gtimeout"
both_output=$(
    set +u
    export PATH="$both_tmp"
    # shellcheck disable=SC1090
    source "$HELPER"
    _resolve_timeout_cmd
)
if [[ "$both_output" == 'timeout' ]]; then
    pass "_resolve_timeout_cmd prefers 'timeout' over 'gtimeout' when both are on PATH (#4125)"
else
    fail "_resolve_timeout_cmd prefers 'timeout' over 'gtimeout' when both are on PATH (#4125)" \
        "expected 'timeout', got '$both_output'"
fi

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

    # #4125: stub ``timeout`` so the test runs on macOS without coreutils.
    # macOS BSD does not ship ``timeout(1)`` — without this stub the bare
    # ``$_timeout_cmd "$_phase_timeout" claude ...`` invocation in
    # ``run_claude_phase`` would either resolve to nothing (bypassing the
    # rc==124 short-circuit because no timer ever fires) or — pre-#4125,
    # before the resolver — exit rc=127 ("command not found"). The stub
    # mirrors GNU ``timeout(1)`` semantics for the ``timer fired`` path:
    # SIGTERM the inner command after ``$1`` seconds and return rc=124,
    # so the helper's rc==124 branch builds the BLOCKED envelope as
    # expected. Mirrors the precedent set by
    # ``scripts/tests/test_agent_runner_entrypoint.sh`` (lines 599-605),
    # which stubs a passthrough ``timeout`` for the same portability
    # reason.
    cat > "$stub_bin/timeout" <<'TIMEOUTEOF'
#!/usr/bin/env bash
# argv: <seconds> <inner-cmd> <inner-args...>
# Mimics GNU timeout(1) for the runtime-timeout regression test:
# fork the inner command, sleep for $seconds, and if it's still
# running, SIGTERM it and return 124. Bash 3.2-compatible (no
# ``wait -n``, no ``-fr 0`` peeking).
seconds="$1"
shift
"$@" &
inner_pid=$!
( sleep "$seconds" ; kill -TERM "$inner_pid" 2>/dev/null ) &
sleeper_pid=$!
wait "$inner_pid" 2>/dev/null
inner_rc=$?
# If the sleeper killed the inner process, $inner_rc is 143 (SIGTERM)
# on most shells; normalize to 124 to match GNU timeout(1).
kill -0 "$sleeper_pid" 2>/dev/null && kill "$sleeper_pid" 2>/dev/null
if (( inner_rc == 143 )); then
    exit 124
fi
exit "$inner_rc"
TIMEOUTEOF
    chmod +x "$stub_bin/timeout"

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
