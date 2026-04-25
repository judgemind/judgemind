#!/usr/bin/env bash
# test_agent_runner_start_phase.sh — Tests for the issue #3366 START_PHASE
# env override on scripts/dispatcher/agent-runner-entrypoint.sh.
#
# What this exercises
# -------------------
# The entrypoint reads ``START_PHASE`` early (before the git clone) and
#   - exits 1 with a stderr message AND a structured log event
#     (``start_phase_override_invalid``) when the value is NOT in
#     AGENT_RUNNER_VALID_START_PHASES.
#   - emits ``start_phase_override`` log event when the value IS valid,
#     then proceeds with the normal startup path (which we don't
#     exercise here — the test focuses on validation, not the full
#     pipeline).
#
# The validation runs BEFORE the clone, so we don't need the heavy
# git/gh/psql stub harness from test_agent_runner_entrypoint.sh — we
# can drive the entrypoint with a bare DATABASE_URL placeholder and
# observe the early exit.
#
# Usage:
#   scripts/tests/test_agent_runner_start_phase.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

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

# ── 1. Invalid START_PHASE rejects with exit 1 ────────────────────────
echo "Invalid START_PHASE values reject with exit 1"

# Run the entrypoint with an unknown phase. The early validation block
# fires before the clone — we expect a fast non-zero exit and a stderr
# message containing the phase name.
TEST_TMP=$(mktemp -d)
out=$(AGENT_ID="00000000-0000-0000-0000-000000000000" \
    DATABASE_URL="postgres://test" \
    AGENT_WORKSPACE="$TEST_TMP" \
    START_PHASE="bogus_phase" \
    bash "$ENTRYPOINT" 2>&1)
rc=$?
rm -rf "$TEST_TMP"
if [[ "$rc" -eq 1 ]]; then
    pass "exit 1 on START_PHASE=bogus_phase"
else
    fail "exit 1 on START_PHASE=bogus_phase" \
         "got exit=$rc, output=$out"
fi
if printf '%s' "$out" | grep -q 'is not in the valid set'; then
    pass "stderr mentions invalid set on START_PHASE=bogus_phase"
else
    fail "stderr mentions invalid set on START_PHASE=bogus_phase" \
         "output=$out"
fi
if printf '%s' "$out" | grep -q 'start_phase_override_invalid'; then
    pass "structured log event start_phase_override_invalid on bogus phase"
else
    fail "structured log event start_phase_override_invalid on bogus phase" \
         "output=$out"
fi

# Also reject the legacy 'claiming' phase — the entrypoint always
# starts at claiming by default, so passing it via START_PHASE is
# pointless; the validator rejects to keep the resume contract narrow
# (resume points only).
TEST_TMP=$(mktemp -d)
out=$(AGENT_ID="00000000-0000-0000-0000-000000000000" \
    DATABASE_URL="postgres://test" \
    AGENT_WORKSPACE="$TEST_TMP" \
    START_PHASE="claiming" \
    bash "$ENTRYPOINT" 2>&1)
rc=$?
rm -rf "$TEST_TMP"
if [[ "$rc" -eq 1 ]]; then
    pass "exit 1 on START_PHASE=claiming (not a resume point)"
else
    fail "exit 1 on START_PHASE=claiming (not a resume point)" \
         "got exit=$rc"
fi

# ── 2. Valid START_PHASE proceeds past validation ────────────────────
# We can't easily run the full pipeline (would need git/gh stubs), but
# we can confirm the early validation passed by setting a known-valid
# value AND short-circuiting later. The validation block runs BEFORE
# the clone, so we observe the ``start_phase_override`` log line in
# stdout regardless of whether the subsequent clone fails.
echo "Valid START_PHASE values pass early validation"

valid_phases="planning setup ralph summary push_and_pr awaiting_ci fix_ci merge awaiting_deploy verify"
for phase in $valid_phases; do
    TEST_TMP=$(mktemp -d)
    set +e
    out=$(AGENT_ID="00000000-0000-0000-0000-000000000000" \
        DATABASE_URL="postgres://test" \
        AGENT_WORKSPACE="$TEST_TMP" \
        START_PHASE="$phase" \
        bash "$ENTRYPOINT" 2>&1)
    rc=$?
    set -e
    rm -rf "$TEST_TMP"
    # The entrypoint will fail later (no git/gh stubs), but the
    # validation block fired first. We just check that the
    # ``start_phase_override`` event with the right phase appeared in
    # the output AND that ``start_phase_override_invalid`` did NOT.
    if printf '%s' "$out" | grep -q '"event": "agent_runner.start_phase_override",.*"start_phase": "'"$phase"'"' \
       && ! printf '%s' "$out" | grep -q 'start_phase_override_invalid'; then
        pass "valid START_PHASE=$phase emits start_phase_override log event"
    else
        fail "valid START_PHASE=$phase emits start_phase_override log event" \
             "output=$out"
    fi
done

# ── 3. Unset START_PHASE skips the override block silently ────────────
echo "Unset START_PHASE skips the override block"
TEST_TMP=$(mktemp -d)
set +e
out=$(AGENT_ID="00000000-0000-0000-0000-000000000000" \
    DATABASE_URL="postgres://test" \
    AGENT_WORKSPACE="$TEST_TMP" \
    bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e
rm -rf "$TEST_TMP"
if ! printf '%s' "$out" | grep -q 'start_phase_override'; then
    pass "no start_phase_override event when START_PHASE unset"
else
    fail "no start_phase_override event when START_PHASE unset" \
         "output=$out"
fi

# ── 4. Parity with daemon's AGENT_RUNNER_VALID_START_PHASES ───────────
echo "Parity with daemon.AGENT_RUNNER_VALID_START_PHASES"
DAEMON_PY="$REPO_ROOT/scripts/dispatcher/daemon.py"
# Extract the set from the daemon source: every quoted string between
# the AGENT_RUNNER_VALID_START_PHASES = frozenset({...}) braces.
daemon_set=$(python3 - "$DAEMON_PY" <<'PYEOF'
import ast
import re
import sys

src = open(sys.argv[1]).read()
# Find the assignment block — non-greedy, dotall.
m = re.search(
    r"AGENT_RUNNER_VALID_START_PHASES:\s*frozenset\[str\]\s*=\s*frozenset\(\s*\{([^}]+)\}\s*\)",
    src,
    re.DOTALL,
)
if not m:
    print("PARITY_FAIL: could not find AGENT_RUNNER_VALID_START_PHASES in daemon.py")
    sys.exit(2)
body = "{" + m.group(1) + "}"
phases = sorted(ast.literal_eval(body))
print(" ".join(phases))
PYEOF
)
entrypoint_set=$(grep -oE 'AGENT_RUNNER_VALID_START_PHASES="[^"]+"' "$ENTRYPOINT" \
    | head -1 \
    | sed -E 's/^AGENT_RUNNER_VALID_START_PHASES="([^"]+)"$/\1/' \
    | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')
# Sort the daemon set the same way for byte-equality.
daemon_sorted=$(printf '%s' "$daemon_set" | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')
if [[ "$entrypoint_set" == "$daemon_sorted" ]]; then
    pass "daemon and entrypoint AGENT_RUNNER_VALID_START_PHASES match"
else
    fail "daemon and entrypoint AGENT_RUNNER_VALID_START_PHASES match" \
         "daemon=[$daemon_sorted] entrypoint=[$entrypoint_set]"
fi

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES)) / $TESTS passed"
if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
echo "All START_PHASE tests passed."
exit 0
