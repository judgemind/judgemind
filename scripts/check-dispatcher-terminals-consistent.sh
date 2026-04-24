#!/usr/bin/env bash
# check-dispatcher-terminals-consistent.sh — Verify that the canonical
# set of dispatcher terminal-status strings defined in
# ``_mark_agent_terminal`` (``scripts/dispatcher/daemon.py``) matches
# every consumer location in the API and web packages, and that every
# canonical terminal is classified inside ``_write_diagnosis_outcome_for_agent``.
#
# Why this check exists
# ---------------------
# The five terminal-status strings
# (succeeded / failed / crashed / plan_blocked / needs_review) are
# hard-coded as literals in seven places spread across the Python daemon,
# the GraphQL schema, the SQL query resolver, two TypeScript UI constructs,
# and a JSDoc comment. A contributor adding a sixth terminal status must
# update all seven locations — no compiler or type system enforces this
# across the language boundary.
#
# This check was motivated by issue #2870 (dispatcher terminal-status
# drift across daemon / API / web).
#
# Rule
# ----
# 1. Parse the canonical set from ``terminal = status in (...)`` inside
#    ``_mark_agent_terminal`` in ``scripts/dispatcher/daemon.py``.
#
# 2. Check SET EQUALITY against:
#      a. ``packages/api/src/graphql/dispatcher/schema.ts``
#         — three docstring occurrences
#           (DispatcherAgent.status, RecentCompletion.status,
#            DispatcherState.recentCompletions + DispatcherQueueKind.COMPLETED).
#      b. ``packages/api/src/graphql/dispatcher/resolvers.ts``
#         — two SQL ``status IN (...)`` lists.
#      c. ``packages/web/src/app/(main)/admin/dispatcher/ui-primitives.tsx``
#         — ``type OutcomeStatus = ...`` union lines AND keys of
#           ``OUTCOME_STYLES``.
#      d. ``packages/web/src/app/(main)/admin/dispatcher/RecentCompletionsPanel.tsx``
#         — the JSDoc phrase listing terminal statuses.
#
# 3. Check SUBSET: the explicit correct-outcome tuple in
#    ``_write_diagnosis_outcome_for_agent`` must only reference terminals
#    that exist in the canonical set (no phantom terminals).
#
# 4. Check ACTIVE_AGENT_STATUSES validity: every element must be either
#    ``running``, ``retrying``, or a member of the canonical set.
#
# On failure: print the canonical set, the drifting location, the
# extracted set, and the symmetric set-diff.  Exit 1.
# On success: print a summary line and exit 0.
#
# Usage
# -----
#   scripts/check-dispatcher-terminals-consistent.sh
#   scripts/check-dispatcher-terminals-consistent.sh \
#       [daemon.py] [schema.ts] [resolvers.ts] [ui-primitives.tsx] \
#       [RecentCompletionsPanel.tsx]
#
# Exit codes
# ----------
#   0 — All locations carry the canonical set.
#   1 — At least one location is inconsistent with the canonical set.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DAEMON_FILE="${1:-$REPO_ROOT/scripts/dispatcher/daemon.py}"
SCHEMA_FILE="${2:-$REPO_ROOT/packages/api/src/graphql/dispatcher/schema.ts}"
RESOLVERS_FILE="${3:-$REPO_ROOT/packages/api/src/graphql/dispatcher/resolvers.ts}"
UI_PRIM_FILE="${4:-$REPO_ROOT/packages/web/src/app/(main)/admin/dispatcher/ui-primitives.tsx}"
COMPLETIONS_FILE="${5:-$REPO_ROOT/packages/web/src/app/(main)/admin/dispatcher/RecentCompletionsPanel.tsx}"

# ─── Fast exit if the daemon file is missing ──────────────────────────────
if [[ ! -f "$DAEMON_FILE" ]]; then
    echo "check-dispatcher-terminals-consistent: no daemon file at $DAEMON_FILE — nothing to check."
    exit 0
fi

# ─── Helper: print symmetric diff with labels ─────────────────────────────
print_diff() {
    local label_a="$1"
    local label_b="$2"
    local tokens_a="$3"
    local tokens_b="$4"

    local only_a=""
    for tok in $tokens_a; do
        found=0
        for chk in $tokens_b; do
            if [[ "$tok" = "$chk" ]]; then found=1; break; fi
        done
        if (( found == 0 )); then
            only_a="$only_a $tok"
        fi
    done
    local only_b=""
    for tok in $tokens_b; do
        found=0
        for chk in $tokens_a; do
            if [[ "$tok" = "$chk" ]]; then found=1; break; fi
        done
        if (( found == 0 )); then
            only_b="$only_b $tok"
        fi
    done

    if [[ -n "$only_a" ]]; then
        echo "  In $label_a but NOT $label_b:$only_a"
    fi
    if [[ -n "$only_b" ]]; then
        echo "  In $label_b but NOT $label_a:$only_b"
    fi
}

# ─── Helper: check set equality, print error on mismatch ──────────────────
check_equality() {
    local label="$1"
    local loc_tokens="$2"
    local canonical="$3"

    local sorted_loc
    sorted_loc="$(echo "$loc_tokens" | tr ' ' '\n' | grep -v '^$' | sort | tr '\n' ' ' | sed 's/ $//')"
    local sorted_can
    sorted_can="$(echo "$canonical" | tr ' ' '\n' | grep -v '^$' | sort | tr '\n' ' ' | sed 's/ $//')"

    if [[ "$sorted_loc" != "$sorted_can" ]]; then
        echo ""
        echo "  DRIFT at $label"
        echo "  Canonical set: $canonical"
        echo "  Extracted set: $loc_tokens"
        print_diff "canonical" "$label" "$canonical" "$loc_tokens"
        return 1
    fi
    return 0
}

# ─── extract_quoted_tokens <file> — print all quoted strings (one/line) ──
# Extracts every double-quoted or single-quoted string literal from stdin.
# POSIX-compatible sed approach — no gawk match() with array required.
extract_quoted_tokens() {
    sed -n 's/.*"\([^"]*\)".*/\1/p; s/.*'"'"'\([^'"'"']*\)'"'"'.*/\1/p'
}

# ─── Step 1: Extract canonical set from daemon.py ─────────────────────────
# Find the ``terminal = status in (...)`` block inside _mark_agent_terminal.
# Uses awk to scan the file in one pass.
# POSIX awk: extract the quoted token on each line inside the block using
# sub() to consume the leading portion, then print what's left between quotes.
canonical_raw="$(awk '
    /terminal = status in \(/ { in_block=1; next }
    in_block && /^[[:space:]]*\)/ { exit }
    in_block {
        line = $0
        # Try double-quoted token
        if (sub(/^[^"]*"/, "", line)) {
            sub(/".*/, "", line)
            if (length(line) > 0) { print line; next }
        }
        # Reset line and try single-quoted token
        line = $0
        if (sub(/^[^'"'"']*'"'"'/, "", line)) {
            sub(/'"'"'.*/, "", line)
            if (length(line) > 0) { print line }
        }
    }
' "$DAEMON_FILE" | tr '\n' ' ' | sed 's/ $//')"

if [[ -z "$canonical_raw" ]]; then
    echo "ERROR: check-dispatcher-terminals-consistent: could not extract canonical terminal set from $DAEMON_FILE"
    echo "  Expected to find:  terminal = status in (...)"
    exit 1
fi

# Sort canonical set
CANONICAL="$(echo "$canonical_raw" | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//')"
CANONICAL_COUNT="$(echo "$CANONICAL" | tr ' ' '\n' | grep -c -v '^$')"

failures=0
location_count=0

# ─── Step 2a: schema.ts — check docstring occurrences ────────────────────
# Extracts the union of all terminal strings appearing in pipe-separated
# status-listing docstrings.
if [[ ! -f "$SCHEMA_FILE" ]]; then
    echo "check-dispatcher-terminals-consistent: no schema file at $SCHEMA_FILE — nothing to check."
    exit 0
fi
location_count=$((location_count + 1))

schema_raw="$(grep -E \
    "(succeeded|failed|crashed|plan_blocked|needs_review).*\|.*(succeeded|failed|crashed|plan_blocked|needs_review)" \
    "$SCHEMA_FILE" \
    | tr '|' ' ' | tr "'" ' ' | tr '`' ' ' \
    | grep -oE '(succeeded|failed|crashed|plan_blocked|needs_review)' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"

if ! check_equality "schema.ts (docstrings)" "$schema_raw" "$CANONICAL"; then
    failures=$((failures + 1))
    echo "  File: $SCHEMA_FILE"
fi

# ─── Step 2b: resolvers.ts — SQL IN lists ─────────────────────────────────
if [[ ! -f "$RESOLVERS_FILE" ]]; then
    echo "check-dispatcher-terminals-consistent: no resolvers file at $RESOLVERS_FILE — nothing to check."
    exit 0
fi
location_count=$((location_count + 1))

resolvers_raw="$(grep -iE "status IN \(" "$RESOLVERS_FILE" \
    | tr "'" ' ' | tr ',' ' ' | tr '(' ' ' | tr ')' ' ' \
    | grep -oE '(succeeded|failed|crashed|plan_blocked|needs_review)' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"

if ! check_equality "resolvers.ts (SQL IN lists)" "$resolvers_raw" "$CANONICAL"; then
    failures=$((failures + 1))
    echo "  File: $RESOLVERS_FILE"
fi

# ─── Step 2c: ui-primitives.tsx — OutcomeStatus union AND OUTCOME_STYLES ──
if [[ ! -f "$UI_PRIM_FILE" ]]; then
    echo "check-dispatcher-terminals-consistent: no ui-primitives file at $UI_PRIM_FILE — nothing to check."
    exit 0
fi
location_count=$((location_count + 1))

# Extract OutcomeStatus union members using POSIX awk.
# Terminated by the first line that ends with ';' after the opening line.
union_raw="$(awk '
    /^type OutcomeStatus[[:space:]]*=/ { in_union=1; next }
    in_union {
        line = $0
        # Extract single-quoted token
        if (sub(/^[^'"'"']*'"'"'/, "", line)) {
            sub(/'"'"'.*/, "", line)
            if (length(line) > 0) { print line }
        }
        # End: line ends with ;
        if ($0 ~ /;[[:space:]]*$/) { exit }
    }
' "$UI_PRIM_FILE" \
    | grep -E '^(succeeded|failed|crashed|plan_blocked|needs_review)$' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"

if ! check_equality "ui-primitives.tsx (OutcomeStatus union)" "$union_raw" "$CANONICAL"; then
    failures=$((failures + 1))
    echo "  File: $UI_PRIM_FILE"
fi

# Extract OUTCOME_STYLES keys using POSIX awk.
# Keys are identifiers followed by ':' at first non-space position in the block.
styles_raw="$(awk '
    /^const OUTCOME_STYLES/ { in_styles=1; next }
    in_styles && /^\}/ { exit }
    in_styles {
        line = $0
        # Strip leading whitespace
        sub(/^[[:space:]]+/, "", line)
        # Extract identifier before ':'
        id = line
        sub(/:.*/, "", id)
        # Only print identifiers that match lowercase + underscore
        if (id ~ /^[a-z_]+$/ && length(id) > 0) { print id }
    }
' "$UI_PRIM_FILE" \
    | grep -E '^(succeeded|failed|crashed|plan_blocked|needs_review)$' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"

if ! check_equality "ui-primitives.tsx (OUTCOME_STYLES keys)" "$styles_raw" "$CANONICAL"; then
    failures=$((failures + 1))
    echo "  File: $UI_PRIM_FILE (OUTCOME_STYLES)"
fi

# ─── Step 2d: RecentCompletionsPanel.tsx — JSDoc list ─────────────────────
if [[ ! -f "$COMPLETIONS_FILE" ]]; then
    echo "check-dispatcher-terminals-consistent: no completions panel file at $COMPLETIONS_FILE — nothing to check."
    exit 0
fi
location_count=$((location_count + 1))

completions_raw="$(grep -E \
    "(succeeded|failed|crashed|plan_blocked|needs_review).*/.*(succeeded|failed|crashed|plan_blocked|needs_review)" \
    "$COMPLETIONS_FILE" \
    | tr '/' ' ' \
    | grep -oE '(succeeded|failed|crashed|plan_blocked|needs_review)' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"

if ! check_equality "RecentCompletionsPanel.tsx (JSDoc)" "$completions_raw" "$CANONICAL"; then
    failures=$((failures + 1))
    echo "  File: $COMPLETIONS_FILE"
fi

# ─── Step 3: Subset check — _write_diagnosis_outcome_for_agent ────────────
# The explicit correct-outcome tuple inside _write_diagnosis_outcome_for_agent
# (the ``final_status in (...)`` expression) must only reference terminals
# that exist in the canonical set.  Catches stale phantom terminals left in
# the classifier after being removed from the canonical set.
location_count=$((location_count + 1))

# Use POSIX awk: enter the function body, find the final_status in (...) tuple,
# extract ALL double-quoted strings from it.  sub() iteratively strips the
# first "..." token on each pass to handle multiple tokens per line.
diag_outcome_raw="$(awk '
    /^    def _write_diagnosis_outcome_for_agent\(/ { in_func=1; next }
    in_func && /^    def [a-z_]+\(/ { exit }
    in_func && /final_status in \(/ { in_tuple=1 }
    in_func && in_tuple {
        line = $0
        while (1) {
            # Try to extract a double-quoted token
            if (!sub(/^[^"]*"/, "", line)) break
            tok = line
            sub(/".*/, "", tok)
            if (length(tok) > 0) { print tok }
            sub(/^[^"]*"/, "", line)
        }
        # End: closing paren on its own line OR inline single-line form
        if ($0 ~ /^[[:space:]]*\)/ || ($0 ~ /final_status in \(/ && $0 ~ /\)/)) {
            in_tuple=0
        }
    }
' "$DAEMON_FILE" | sort -u | tr '\n' ' ' | sed 's/ $//')"

if [[ -n "$diag_outcome_raw" ]]; then
    phantom_in_diag=""
    for terminal in $diag_outcome_raw; do
        found=0
        for canonical in $CANONICAL; do
            if [[ "$terminal" = "$canonical" ]]; then found=1; break; fi
        done
        if (( found == 0 )); then
            if [[ -z "$phantom_in_diag" ]]; then
                phantom_in_diag="$terminal"
            else
                phantom_in_diag="$phantom_in_diag $terminal"
            fi
        fi
    done
    if [[ -n "$phantom_in_diag" ]]; then
        echo ""
        echo "  DRIFT: _write_diagnosis_outcome_for_agent correct-outcome tuple contains"
        echo "  terminal(s) not in the canonical set: $phantom_in_diag"
        echo "  File: $DAEMON_FILE"
        echo "  The correct-outcome tuple must only reference terminals from"
        echo "  the canonical set defined in _mark_agent_terminal."
        failures=$((failures + 1))
    fi
fi

# ─── Step 4: ACTIVE_AGENT_STATUSES validity ───────────────────────────────
# Every element of ACTIVE_AGENT_STATUSES must be "running", "retrying",
# or a member of the canonical set.
location_count=$((location_count + 1))

active_raw="$(grep -E '^ACTIVE_AGENT_STATUSES[[:space:]]*=' "$DAEMON_FILE" \
    | tr "'" '"' \
    | grep -oE '"[a-z_]+"' \
    | tr -d '"' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"

if [[ -n "$active_raw" ]]; then
    bad_active=""
    for tok in $active_raw; do
        valid=0
        if [[ "$tok" = "running" || "$tok" = "retrying" ]]; then
            valid=1
        else
            for canonical in $CANONICAL; do
                if [[ "$tok" = "$canonical" ]]; then
                    valid=1
                    break
                fi
            done
        fi
        if (( valid == 0 )); then
            if [[ -z "$bad_active" ]]; then
                bad_active="$tok"
            else
                bad_active="$bad_active $tok"
            fi
        fi
    done
    if [[ -n "$bad_active" ]]; then
        echo ""
        echo "  DRIFT: ACTIVE_AGENT_STATUSES contains unknown status(es): $bad_active"
        echo "  Each element must be 'running', 'retrying', or a member of the"
        echo "  canonical terminal set ($CANONICAL)."
        echo "  File: $DAEMON_FILE"
        failures=$((failures + 1))
    fi
fi

# ─── Final verdict ────────────────────────────────────────────────────────
if (( failures > 0 )); then
    echo ""
    echo "ERROR: check-dispatcher-terminals-consistent: $failures location(s) are out of sync."
    echo ""
    echo "  Canonical set ($CANONICAL_COUNT terminals): $CANONICAL"
    echo "  Source: $DAEMON_FILE"
    echo "         _mark_agent_terminal — terminal = status in (...)"
    echo ""
    echo "  Fix: update every flagged location to match the canonical set."
    echo "  The canonical source is the tuple in _mark_agent_terminal."
    exit 1
fi

echo "check-dispatcher-terminals-consistent: $CANONICAL_COUNT terminal(s) aligned across $location_count location(s)."
exit 0
