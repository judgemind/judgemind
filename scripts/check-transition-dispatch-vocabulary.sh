#!/usr/bin/env bash
# check-transition-dispatch-vocabulary.sh — Assert the action vocabulary
# in scripts/dispatcher/phase_transitions.py matches the dispatch coverage
# in scripts/dispatcher/agent-runner-entrypoint.sh's
# ``dispatch_transition_action`` helper. (#3581)
#
# Why this guard exists
# ---------------------
# Issue #3581 centralized transition-action dispatch into a single helper
# (``dispatch_transition_action`` in agent-runner-entrypoint.sh). All
# per-phase case-statements now delegate to that one helper, which
# eliminates the cluster-bug pattern from #3543/#3558/#3573/#3580 where
# adding a new action / failure-hint left some per-phase case-statement
# silently un-updated.
#
# But the centralization is only as good as the helper's coverage. If a
# future PR adds:
#
#   - a new ``TransitionAction`` enum value to phase_transitions.py
#   - a new ``FAILURE_HINT_*`` constant to phase_transitions.py
#
# AND forgets to extend ``dispatch_transition_action`` to handle it, the
# next agent that hits that action / hint will fall through to the
# unrecognized-action terminal, terminal-failing instead of advancing
# correctly. The cluster bug returns.
#
# This guard runs in CI and fails the PR if the vocabularies drift apart,
# naming the missing action / hint so the author knows exactly what to
# add to dispatch_transition_action.
#
# Usage:
#   scripts/check-transition-dispatch-vocabulary.sh
#
# Exit codes:
#   0 — Every TransitionAction value AND every FAILURE_HINT_* constant
#       has matching coverage in dispatch_transition_action.
#   1 — One or more drifts detected; details printed to stderr.
#   2 — Could not parse one of the source files (file moved / rewritten
#       in a shape this script doesn't know).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PY_FILE="$REPO_ROOT/scripts/dispatcher/phase_transitions.py"
SH_FILE="$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh"

if [[ ! -f "$PY_FILE" ]]; then
    echo "ERROR: phase_transitions.py not found at $PY_FILE" >&2
    exit 2
fi
if [[ ! -f "$SH_FILE" ]]; then
    echo "ERROR: agent-runner-entrypoint.sh not found at $SH_FILE" >&2
    exit 2
fi

# ── Extract the TransitionAction enum values from phase_transitions.py ──
#
# The enum body looks like:
#
#   class TransitionAction(str, Enum):
#       ...docstrings...
#       ADVANCE = "advance"
#       ADVANCE_WITH_STATUS = "advance_with_status"
#       ROUTE_TO_DIAGNOSER = "route_to_diagnoser"
#       UNRECOGNIZED = "unrecognized"
#
# We grab the block from ``class TransitionAction`` to the next
# top-level ``class `` or ``def `` definition, then scan for lines of the
# form ``NAME = "value"``. The lower-case value is what we care about
# (it's what transition_for emits at runtime).
extract_action_enum_values() {
    # Inside a Python class body, enum members are indented (typically
    # 4 spaces). We scope the match to lines that start with whitespace
    # AND are between ``class TransitionAction(`` and the next un-
    # indented top-level definition. The ``next`` for blank lines
    # avoids ending the block on docstring spacing.
    awk '
        /^class TransitionAction\(/ { in_enum = 1; next }
        in_enum && /^[A-Za-z]/ { exit }
        in_enum && /^[[:space:]]+[A-Z][A-Z_]* = "/ {
            # Match: NAME = "value" with leading whitespace.
            match($0, /"[^"]+"/)
            if (RSTART > 0) {
                v = substr($0, RSTART + 1, RLENGTH - 2)
                print v
            }
        }
    ' "$PY_FILE" | sort -u
}

# ── Extract FAILURE_HINT_* constant values from phase_transitions.py ──
#
# Pattern: ``FAILURE_HINT_<NAME> = "<value>"`` at module scope.
extract_failure_hints() {
    grep -E '^FAILURE_HINT_[A-Z_]+ = "[^"]+"' "$PY_FILE" \
        | sed -E 's/.*= "([^"]+)".*/\1/' \
        | sort -u
}

# ── Extract the action arms handled inside dispatch_transition_action ──
#
# The helper's outer ``case "$_action" in`` block has arms like:
#   advance)
#   advance_with_status)
#   route_to_diagnoser)
#   unrecognized|*)
#
# We find the helper definition and then enumerate the case-arm labels
# (the bare-word tokens before ``)``) inside its outer case block —
# stopping at the closing ``esac``.
extract_helper_action_arms() {
    awk '
        /^dispatch_transition_action\(\) \{/ { in_fn = 1; next }
        in_fn && /^}/ { exit }
        in_fn && /case "\$_action"/ { in_action_case = 1; depth = 1; next }
        in_action_case && /case "\$_hint"/ {
            # Inner case for hints — skip over its arms by tracking
            # depth. We re-enter the outer case after the inner esac.
            inner = 1
            next
        }
        in_action_case && inner && /esac/ {
            inner = 0
            next
        }
        in_action_case && inner { next }
        in_action_case && /esac/ { in_action_case = 0; in_fn = 0; exit }
        in_action_case && /^[[:space:]]+[a-z_|*]+\)/ {
            # Strip leading whitespace + trailing ``)``. Split on ``|``
            # for multi-arm labels (e.g. ``unrecognized|*)``).
            sub(/^[[:space:]]+/, "")
            sub(/\).*$/, "")
            n = split($0, parts, "|")
            for (i = 1; i <= n; i++) {
                if (parts[i] != "*") {
                    print parts[i]
                }
            }
        }
    ' "$SH_FILE" | sort -u
}

# ── Extract the hint arms handled inside dispatch_transition_action ──
#
# The inner ``case "$_hint" in`` block has arms like:
#   conflict_unresolvable)
#   ralph_not_ship)
#   ralph_ac_infeasible|summary_ac_infeasible|fix_ci_blocked|...)
#
# We list every label on the LHS of the ``)`` (split on ``|``).
extract_helper_hint_arms() {
    awk '
        /^dispatch_transition_action\(\) \{/ { in_fn = 1; next }
        in_fn && /^}/ { exit }
        in_fn && /case "\$_hint"/ { in_hint_case = 1; next }
        in_hint_case && /esac/ { in_hint_case = 0; next }
        in_hint_case && /^[[:space:]]+[a-z_|]+\)/ {
            sub(/^[[:space:]]+/, "")
            sub(/\).*$/, "")
            n = split($0, parts, "|")
            for (i = 1; i <= n; i++) {
                if (parts[i] != "*") {
                    print parts[i]
                }
            }
        }
    ' "$SH_FILE" | sort -u
}

py_actions=$(extract_action_enum_values)
py_hints=$(extract_failure_hints)
sh_actions=$(extract_helper_action_arms)
sh_hints=$(extract_helper_hint_arms)

if [[ -z "$py_actions" ]]; then
    echo "ERROR: Could not extract TransitionAction enum values from $PY_FILE" >&2
    exit 2
fi
if [[ -z "$py_hints" ]]; then
    echo "ERROR: Could not extract FAILURE_HINT_* constants from $PY_FILE" >&2
    exit 2
fi
if [[ -z "$sh_actions" ]]; then
    echo "ERROR: Could not locate dispatch_transition_action helper or its action case arms in $SH_FILE" >&2
    echo "       Has the helper been renamed or moved? Update this script's awk patterns." >&2
    exit 2
fi
if [[ -z "$sh_hints" ]]; then
    echo "ERROR: Could not locate inner case \"\$_hint\" arms in dispatch_transition_action ($SH_FILE)" >&2
    exit 2
fi

# ── Cross-reference ───────────────────────────────────────────────────

failures=0

# Every Python TransitionAction value (except UNRECOGNIZED, which is
# handled by the fall-through ``unrecognized|*)`` arm) must appear as a
# specific case arm in the helper.
echo "Python TransitionAction values:"
echo "$py_actions" | sed 's/^/  /'
echo
echo "Helper action arms (dispatch_transition_action outer case):"
echo "$sh_actions" | sed 's/^/  /'
echo

while IFS= read -r action; do
    [[ -z "$action" ]] && continue
    if ! echo "$sh_actions" | grep -qx "$action"; then
        echo "ERROR: TransitionAction.$(echo "$action" | tr 'a-z' 'A-Z') = \"$action\" is defined in $PY_FILE but NOT handled by dispatch_transition_action in $SH_FILE" >&2
        echo "       Add a case arm:" >&2
        echo "         $action)" >&2
        echo "             ...handler logic..." >&2
        echo "             ;;" >&2
        failures=$((failures + 1))
    fi
done <<< "$py_actions"

# Every Python FAILURE_HINT_* value must appear in the helper's inner
# ``case "$_hint" in`` block (either as a dedicated arm or as part of a
# multi-arm label like ``a|b|c)``).
echo "Python FAILURE_HINT_* values:"
echo "$py_hints" | sed 's/^/  /'
echo
echo "Helper hint arms (dispatch_transition_action inner case):"
echo "$sh_hints" | sed 's/^/  /'
echo

while IFS= read -r hint; do
    [[ -z "$hint" ]] && continue
    if ! echo "$sh_hints" | grep -qx "$hint"; then
        echo "ERROR: FAILURE_HINT_$(echo "$hint" | tr 'a-z' 'A-Z') = \"$hint\" is defined in $PY_FILE but NOT handled by dispatch_transition_action in $SH_FILE" >&2
        echo "       Add a case arm to the inner case \"\$_hint\" block:" >&2
        echo "         $hint)" >&2
        echo "             agent_runner_reaped_failure ..." >&2
        echo "             ;;" >&2
        failures=$((failures + 1))
    fi
done <<< "$py_hints"

if [[ $failures -gt 0 ]]; then
    echo "" >&2
    echo "Transition-dispatch vocabulary drift detected: $failures missing arm(s)" >&2
    echo "  Fix: extend dispatch_transition_action in $SH_FILE to cover the" >&2
    echo "  identifier(s) above, then re-run this script to verify." >&2
    exit 1
fi

echo "Transition-dispatch vocabulary in sync: $(echo "$py_actions" | wc -l | tr -d ' ') action(s), $(echo "$py_hints" | wc -l | tr -d ' ') hint(s) all handled."
exit 0
