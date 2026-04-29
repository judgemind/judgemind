#!/usr/bin/env bash
# test_check_transition_dispatch_vocabulary.sh — Tests for the CI guard
# scripts/check-transition-dispatch-vocabulary.sh. (#3581)
#
# What this exercises
# -------------------
# The guard fails (exit 1) when:
#   * a TransitionAction enum value in phase_transitions.py has no
#     matching arm in ``dispatch_transition_action`` (outer case).
#   * a FAILURE_HINT_* constant in phase_transitions.py has no matching
#     arm in the helper's inner ``case "$_hint"`` block.
#
# The guard passes (exit 0) when both vocabularies agree.
#
# Strategy: copy the real phase_transitions.py + agent-runner-entrypoint.sh
# into a sandbox, run the guard once unchanged (expect pass), then mutate
# each file to introduce a synthetic drift and assert the guard fails
# with the right error message.
#
# Usage:
#   scripts/tests/test_check_transition_dispatch_vocabulary.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD="$REPO_ROOT/scripts/check-transition-dispatch-vocabulary.sh"
PY_SRC="$REPO_ROOT/scripts/dispatcher/phase_transitions.py"
SH_SRC="$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh"

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

TEST_TMP=$(mktemp -d)
trap 'rm -rf "$TEST_TMP"' EXIT

# Build a sandbox with the same on-disk layout as the real repo, so the
# guard's REPO_ROOT detection (cd .. from scripts/) lands on the right
# directory.
SANDBOX_REPO="$TEST_TMP/sandbox"
mkdir -p "$SANDBOX_REPO/scripts/dispatcher"
cp "$GUARD" "$SANDBOX_REPO/scripts/check-transition-dispatch-vocabulary.sh"
chmod +x "$SANDBOX_REPO/scripts/check-transition-dispatch-vocabulary.sh"

# ── 1. Baseline — unchanged repo passes ───────────────────────────────
echo "Baseline: unchanged phase_transitions.py + agent-runner-entrypoint.sh"

cp "$PY_SRC" "$SANDBOX_REPO/scripts/dispatcher/phase_transitions.py"
cp "$SH_SRC" "$SANDBOX_REPO/scripts/dispatcher/agent-runner-entrypoint.sh"

if "$SANDBOX_REPO/scripts/check-transition-dispatch-vocabulary.sh" >/dev/null 2>"$TEST_TMP/baseline.err"; then
    pass "guard exits 0 on a clean repo"
else
    rc=$?
    fail "guard exits 0 on a clean repo" \
        "exit=$rc, stderr=$(cat "$TEST_TMP/baseline.err")"
fi

# ── 2. Synthetic drift: new TransitionAction not in helper ─────────────
echo "Drift: new TransitionAction value not in helper"

# Inject a new enum member into phase_transitions.py before the closing
# of the TransitionAction class. We add it on the line right after
# UNRECOGNIZED = "unrecognized" — that line ends the enum body.
python3 - "$SANDBOX_REPO/scripts/dispatcher/phase_transitions.py" <<'PYEOF'
import sys

p = sys.argv[1]
text = open(p).read()
# Insert ``RETRY_LATER = "retry_later"`` after the UNRECOGNIZED line.
# Use a marker line so the insertion is unambiguous.
marker = '    UNRECOGNIZED = "unrecognized"\n'
addition = '    RETRY_LATER = "retry_later"\n'
assert marker in text, "could not find UNRECOGNIZED enum line"
text = text.replace(marker, marker + addition, 1)
open(p, "w").write(text)
PYEOF

if "$SANDBOX_REPO/scripts/check-transition-dispatch-vocabulary.sh" >"$TEST_TMP/drift1.out" 2>"$TEST_TMP/drift1.err"; then
    fail "guard fails on new TransitionAction not in helper" \
        "guard exited 0, expected non-zero. stderr: $(cat "$TEST_TMP/drift1.err")"
else
    rc=$?
    if [[ $rc -eq 1 ]]; then
        pass "guard exits 1 on new TransitionAction not handled by helper"
    else
        fail "guard exits 1 on new TransitionAction not handled by helper" \
            "exit=$rc (expected 1)"
    fi
    # Specific error message names the missing action.
    if grep -q '"retry_later"' "$TEST_TMP/drift1.err"; then
        pass "drift error message names the missing action (retry_later)"
    else
        fail "drift error message names the missing action (retry_later)" \
            "stderr did not mention retry_later: $(cat "$TEST_TMP/drift1.err")"
    fi
fi

# Restore the Python file for the next test.
cp "$PY_SRC" "$SANDBOX_REPO/scripts/dispatcher/phase_transitions.py"

# ── 3. Synthetic drift: new FAILURE_HINT_* not in helper ───────────────
echo "Drift: new FAILURE_HINT_* constant not in helper"

# Append a new FAILURE_HINT constant near the existing ones. Use a
# marker so we know exactly where to inject.
python3 - "$SANDBOX_REPO/scripts/dispatcher/phase_transitions.py" <<'PYEOF'
import sys

p = sys.argv[1]
text = open(p).read()
# Find the last FAILURE_HINT_* line. We append right after the
# OPERATIONAL_FAILED constant — that's stable per current source.
marker = 'FAILURE_HINT_OPERATIONAL_FAILED = "operational_failed"\n'
addition = 'FAILURE_HINT_NEWLY_INVENTED = "newly_invented_hint_for_test"\n'
assert marker in text, "could not find FAILURE_HINT_OPERATIONAL_FAILED line"
text = text.replace(marker, marker + addition, 1)
open(p, "w").write(text)
PYEOF

if "$SANDBOX_REPO/scripts/check-transition-dispatch-vocabulary.sh" >"$TEST_TMP/drift2.out" 2>"$TEST_TMP/drift2.err"; then
    fail "guard fails on new FAILURE_HINT_* not in helper" \
        "guard exited 0, expected non-zero. stderr: $(cat "$TEST_TMP/drift2.err")"
else
    rc=$?
    if [[ $rc -eq 1 ]]; then
        pass "guard exits 1 on new FAILURE_HINT_* not handled by helper"
    else
        fail "guard exits 1 on new FAILURE_HINT_* not handled by helper" \
            "exit=$rc (expected 1)"
    fi
    if grep -q "newly_invented_hint_for_test" "$TEST_TMP/drift2.err"; then
        pass "drift error message names the missing hint (newly_invented_hint_for_test)"
    else
        fail "drift error message names the missing hint" \
            "stderr did not mention newly_invented_hint_for_test: $(cat "$TEST_TMP/drift2.err")"
    fi
fi

# Restore the Python file.
cp "$PY_SRC" "$SANDBOX_REPO/scripts/dispatcher/phase_transitions.py"

# ── 4. Removing a hint arm from the helper triggers the guard ─────────
echo "Drift: helper missing an existing FAILURE_HINT_* arm"

# Strip ``operational_failed)`` from the helper's inner case-statement —
# more precisely, remove the multi-arm label that contains it. The
# real helper has a single multi-label arm; we remove just operational_failed
# from it. Since we can't easily edit a multi-arm label in a way that's
# both valid bash AND removes one entry, we'll instead strip
# ``operational_failed`` from inside the |-pipe label.
python3 - "$SANDBOX_REPO/scripts/dispatcher/agent-runner-entrypoint.sh" <<'PYEOF'
import sys

p = sys.argv[1]
text = open(p).read()
# Look for the multi-label arm and remove operational_failed from it.
# The label looks like:
#   ralph_ac_infeasible|summary_ac_infeasible|...|operational_failed|plan_blocked)
old_label = "ralph_ac_infeasible|summary_ac_infeasible|fix_ci_blocked|verify_failed_post_merge|push_and_pr_no_unmerged_files|operational_failed|plan_blocked)"
new_label = "ralph_ac_infeasible|summary_ac_infeasible|fix_ci_blocked|verify_failed_post_merge|push_and_pr_no_unmerged_files|plan_blocked)"
assert old_label in text, f"could not find expected multi-label arm: {old_label}"
text = text.replace(old_label, new_label, 1)
open(p, "w").write(text)
PYEOF

if "$SANDBOX_REPO/scripts/check-transition-dispatch-vocabulary.sh" >"$TEST_TMP/drift3.out" 2>"$TEST_TMP/drift3.err"; then
    fail "guard catches removed hint arm" \
        "guard exited 0, expected non-zero"
else
    rc=$?
    if [[ $rc -eq 1 ]] && grep -q "operational_failed" "$TEST_TMP/drift3.err"; then
        pass "guard catches removed hint arm (operational_failed)"
    else
        fail "guard catches removed hint arm (operational_failed)" \
            "exit=$rc, stderr: $(cat "$TEST_TMP/drift3.err")"
    fi
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
