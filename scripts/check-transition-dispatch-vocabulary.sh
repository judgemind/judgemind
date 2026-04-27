#!/usr/bin/env bash
# check-transition-dispatch-vocabulary.sh — Verify that the
# dispatch_transition_action() helper in agent-runner-entrypoint.sh
# handles every TransitionAction enum value defined in phase_transitions.py,
# and every FAILURE_HINT_* constant is handled in the route_to_diagnoser
# sub-case inside the same helper.
#
# Why this check exists
# ---------------------
# dispatch_transition_action() (#3581) is the single source of truth for
# routing transition_for() results to advance_phase / agent_runner_reaped_failure.
# When a new TransitionAction value is added to phase_transitions.py, or a new
# FAILURE_HINT_* constant is added, the helper must also be updated or agents
# silently fall through to the unrecognized-action terminal.
#
# Rule
# ----
# 1. Parse ``TransitionAction`` enum values from phase_transitions.py via
#    awk between ``class TransitionAction`` and the next blank top-level line.
#    Extract the string values (e.g. "advance", "route_to_diagnoser").
#
# 2. Parse case-arms inside ``dispatch_transition_action()`` from
#    agent-runner-entrypoint.sh.  Extract case-arm tokens from the first
#    ``case "$_action" in`` block inside the function.
#
# 3. Cross-reference: every TransitionAction value (except ``unrecognized``,
#    which the wildcard ``*`` arm catches) must have an explicit case-arm OR
#    a ``*`` wildcard catch.  Fail with a specific message identifying the
#    missing action.
#
# 4. Parse FAILURE_HINT_* constant values from phase_transitions.py.
#    Parse the hint sub-case arms inside ``route_to_diagnoser)`` in
#    dispatch_transition_action(). Every hint must have an explicit arm or a
#    wildcard catch.
#
# 5. Fast-exit with 0 when source files are missing (same pattern as
#    check-dispatcher-terminals-consistent.sh) — guards CI on forks that
#    may not carry all files.
#
# Usage
# -----
#   scripts/check-transition-dispatch-vocabulary.sh
#   scripts/check-transition-dispatch-vocabulary.sh \
#       [phase_transitions.py] [agent-runner-entrypoint.sh]
#
# Exit codes
#   0 — All TransitionAction values and FAILURE_HINT_* constants are handled.
#   1 — At least one value is missing from dispatch_transition_action.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TRANSITIONS_FILE="${1:-$REPO_ROOT/scripts/dispatcher/phase_transitions.py}"
ENTRYPOINT_FILE="${2:-$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh}"

# ─── Fast exit if source files are missing ────────────────────────────────
if [[ ! -f "$TRANSITIONS_FILE" ]]; then
    echo "check-transition-dispatch-vocabulary: no phase_transitions.py at $TRANSITIONS_FILE — nothing to check."
    exit 0
fi

if [[ ! -f "$ENTRYPOINT_FILE" ]]; then
    echo "check-transition-dispatch-vocabulary: no agent-runner-entrypoint.sh at $ENTRYPOINT_FILE — nothing to check."
    exit 0
fi

failures=0

# ─── Step 1: Extract TransitionAction enum values from phase_transitions.py ─
# Parse lines of the form:
#   NAME = "value"
# inside ``class TransitionAction``. Stop at the next unindented non-blank line.
transition_actions="$(awk '
    /^class TransitionAction/ { in_class=1; next }
    in_class && /^[^ \t]/ { exit }
    in_class {
        # Match lines like:    ADVANCE = "advance"
        # Grab the string literal after =
        line = $0
        sub(/^[[:space:]]+/, "", line)
        if (line ~ /^[A-Z_]+ = "/) {
            # Extract token between double quotes
            sub(/^[^"]*"/, "", line)
            sub(/".*/, "", line)
            if (length(line) > 0) { print line }
        }
    }
' "$TRANSITIONS_FILE" | sort -u | tr '\n' ' ' | sed 's/ $//')"

if [[ -z "$transition_actions" ]]; then
    echo "ERROR: check-transition-dispatch-vocabulary: could not extract TransitionAction values from $TRANSITIONS_FILE"
    echo "  Expected: class TransitionAction(str, Enum): with NAME = \"value\" members"
    exit 1
fi

# ─── Step 2: Extract case-arm tokens from dispatch_transition_action() ────
# Find the function, then extract the first ``case "$_action" in`` block's
# arm labels (pipe-separated patterns before ``)``).
# Uses POSIX awk (no gawk match() with capture group).
entrypoint_action_arms="$(awk '
    /^dispatch_transition_action\(\)/ { in_func=1; next }
    in_func && /^\}[[:space:]]*$/ { exit }
    in_func && /case "\$_action" in/ { in_case=1; next }
    in_func && in_case && /^[[:space:]]*esac/ { in_case=0; next }
    in_func && in_case {
        line = $0
        sub(/^[[:space:]]+/, "", line)
        # Skip blank lines and comments
        if (length(line) == 0) { next }
        if (substr(line, 1, 1) == "#") { next }
        # A case arm ends with )  e.g.  advance)  or  advance_with_status)  or  *)
        if (substr(line, length(line), 1) == ")") {
            arm = line
            sub(/\)$/, "", arm)
            # Split on | and print each token
            n = split(arm, parts, "|")
            for (i = 1; i <= n; i++) {
                tok = parts[i]
                gsub(/^[[:space:]]+/, "", tok)
                gsub(/[[:space:]]+$/, "", tok)
                if (length(tok) > 0) { print tok }
            }
        }
    }
' "$ENTRYPOINT_FILE" | sort -u | tr '\n' ' ' | sed 's/ $//')"

set -f  # disable glob expansion so bare '*' in arm lists is treated literally
has_action_wildcard=0
for arm in $entrypoint_action_arms; do
    if [[ "$arm" == "*" ]]; then
        has_action_wildcard=1
        break
    fi
done

# ─── Step 3: Cross-reference TransitionAction values vs case arms ─────────
echo "check-transition-dispatch-vocabulary: checking TransitionAction values..."
echo "  phase_transitions.py values: $transition_actions"
echo "  entrypoint dispatch arms:    $entrypoint_action_arms"

missing_actions=""
for action in $transition_actions; do
    # 'unrecognized' is always caught by the wildcard arm — skip it
    if [[ "$action" == "unrecognized" ]]; then
        continue
    fi
    found=0
    for arm in $entrypoint_action_arms; do
        if [[ "$arm" == "$action" ]]; then
            found=1
            break
        fi
    done
    if [[ "$found" -eq 0 ]]; then
        if [[ "$has_action_wildcard" -eq 1 ]]; then
            : # wildcard covers it — not an error
        else
            missing_actions="$missing_actions $action"
        fi
    fi
done
set +f  # re-enable glob expansion

if [[ -n "$missing_actions" ]]; then
    echo ""
    echo "  DRIFT: transition vocabulary drift — the following TransitionAction values"
    echo "  are not handled by dispatch_transition_action() and no wildcard arm exists:"
    for action in $missing_actions; do
        echo "    transition vocabulary drift: action=$action is not handled by dispatch_transition_action"
    done
    echo "  Source: $TRANSITIONS_FILE"
    echo "  Handler: $ENTRYPOINT_FILE (dispatch_transition_action)"
    failures=$((failures + 1))
fi

# ─── Step 4: Extract FAILURE_HINT_* values from phase_transitions.py ──────
failure_hints="$(grep -E '^FAILURE_HINT_[A-Z_]+ = ' "$TRANSITIONS_FILE" \
    | grep -oE '"[a-z_]+"' \
    | tr -d '"' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"

if [[ -z "$failure_hints" ]]; then
    echo "check-transition-dispatch-vocabulary: no FAILURE_HINT_* constants found in $TRANSITIONS_FILE — skipping hint check."
else
    # Extract hint sub-case arms from the route_to_diagnoser) arm inside
    # dispatch_transition_action(). We look for the inner case "$_hint" in...esac.
    entrypoint_hint_arms="$(awk '
        /^dispatch_transition_action\(\)/ { in_func=1; next }
        in_func && /^\}[[:space:]]*$/ { exit }
        in_func && /case "\$_hint" in/ { in_hint_case=1; next }
        in_func && in_hint_case && /^[[:space:]]*esac/ { in_hint_case=0; next }
        in_func && in_hint_case {
            line = $0
            sub(/^[[:space:]]+/, "", line)
            if (length(line) == 0) { next }
            if (substr(line, 1, 1) == "#") { next }
            if (substr(line, length(line), 1) == ")") {
                arm = line
                sub(/\)$/, "", arm)
                n = split(arm, parts, "|")
                for (i = 1; i <= n; i++) {
                    tok = parts[i]
                    gsub(/^[[:space:]]+/, "", tok)
                    gsub(/[[:space:]]+$/, "", tok)
                    if (length(tok) > 0) { print tok }
                }
            }
        }
    ' "$ENTRYPOINT_FILE" | sort -u | tr '\n' ' ' | sed 's/ $//')"

    set -f  # disable glob expansion so bare '*' in arm lists is treated literally
    has_hint_wildcard=0
    for arm in $entrypoint_hint_arms; do
        if [[ "$arm" == "*" ]]; then
            has_hint_wildcard=1
            break
        fi
    done
    set +f  # re-enable glob expansion

    echo ""
    echo "check-transition-dispatch-vocabulary: checking FAILURE_HINT_* values..."
    echo "  phase_transitions.py hints: $failure_hints"
    echo "  entrypoint hint arms:       $entrypoint_hint_arms"

    missing_hints=""
    set -f
    for hint in $failure_hints; do
        found=0
        for arm in $entrypoint_hint_arms; do
            if [[ "$arm" == "$hint" ]]; then
                found=1
                break
            fi
        done
        if [[ "$found" -eq 0 ]]; then
            if [[ "$has_hint_wildcard" -eq 1 ]]; then
                : # wildcard covers it
            else
                missing_hints="$missing_hints $hint"
            fi
        fi
    done
    set +f

    if [[ -n "$missing_hints" ]]; then
        echo ""
        echo "  DRIFT: hint vocabulary drift — the following FAILURE_HINT_* values"
        echo "  are not handled by route_to_diagnoser in dispatch_transition_action() and no wildcard exists:"
        for hint in $missing_hints; do
            echo "    hint vocabulary drift: hint=$hint is not handled in route_to_diagnoser"
        done
        echo "  Source: $TRANSITIONS_FILE"
        echo "  Handler: $ENTRYPOINT_FILE (dispatch_transition_action > route_to_diagnoser)"
        failures=$((failures + 1))
    fi
fi

# ─── Final verdict ────────────────────────────────────────────────────────
if [[ "$failures" -gt 0 ]]; then
    echo ""
    echo "ERROR: check-transition-dispatch-vocabulary: $failures vocabulary drift(s) detected."
    echo ""
    echo "  Fix: update dispatch_transition_action() in $ENTRYPOINT_FILE"
    echo "  to handle every TransitionAction value and FAILURE_HINT_* constant"
    echo "  defined in $TRANSITIONS_FILE."
    exit 1
fi

echo ""
echo "check-transition-dispatch-vocabulary: all TransitionAction values and FAILURE_HINT_* constants are handled."
exit 0
