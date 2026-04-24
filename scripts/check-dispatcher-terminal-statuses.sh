#!/usr/bin/env bash
# check-dispatcher-terminal-statuses.sh — Assert that the terminal-status
# lists in Python and TypeScript stay in sync.
#
# Three canonical copies of the terminal-status list must always agree:
#   1. scripts/dispatcher/daemon.py::TERMINAL_AGENT_STATUSES (frozenset)
#   2. packages/api/src/graphql/dispatcher/constants.ts (TERMINAL_AGENT_STATUSES array)
#   3. packages/web/src/app/(main)/admin/dispatcher/ui-primitives.tsx
#      (TERMINAL_AGENT_STATUSES array)
#
# The script extracts the status token strings from each location, sorts
# them, and fails if any copy diverges from the others.  This guards against
# the accidental pattern of adding a new terminal status in Python without
# updating the TypeScript side (or vice versa).
#
# Usage:
#   scripts/check-dispatcher-terminal-statuses.sh
#
# Exit codes:
#   0 — All three copies are identical.
#   1 — One or more copies differ; details printed to stderr.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DAEMON_PY="$REPO_ROOT/scripts/dispatcher/daemon.py"
CONSTANTS_TS="$REPO_ROOT/packages/api/src/graphql/dispatcher/constants.ts"
UI_PRIMITIVES_TSX="$REPO_ROOT/packages/web/src/app/(main)/admin/dispatcher/ui-primitives.tsx"

# ─── Extraction helpers ───────────────────────────────────────────────────
# Extract status tokens from the Python frozenset literal:
#   TERMINAL_AGENT_STATUSES: frozenset[str] = frozenset(
#       {"succeeded", "failed", ...}
#   )
# We grab the block between the first { after the frozenset( call and the
# matching }, then strip down to bare word tokens.
extract_python() {
    local file="$1"
    # Grab the frozenset literal block (single or multi-line).
    # awk: print lines from 'TERMINAL_AGENT_STATUSES.*frozenset' to the
    # closing ')' of the outer frozenset() call.
    awk '/^TERMINAL_AGENT_STATUSES.*frozenset/{p=1} p{print; if(/\)/) {p=0; exit}}' "$file" \
        | grep -oE '"[a-z_]+"' \
        | tr -d '"' \
        | sort
}

# Extract status tokens from a TS/TSX const array:
#   const TERMINAL_AGENT_STATUSES = [
#     'succeeded', 'failed', ...
#   ] as const;
# We grab the block between the [ and ] for the TERMINAL_AGENT_STATUSES array.
extract_ts() {
    local file="$1"
    awk '/const TERMINAL_AGENT_STATUSES[[:space:]]*=[[:space:]]*\[/{p=1} p{print; if(/\] as const/) {p=0; exit}}' "$file" \
        | grep -oE "'[a-z_]+'" \
        | tr -d "'" \
        | sort
}

# ─── Extract all three lists ──────────────────────────────────────────────
py_list=$(extract_python "$DAEMON_PY")
api_ts_list=$(extract_ts "$CONSTANTS_TS")
web_tsx_list=$(extract_ts "$UI_PRIMITIVES_TSX")

if [[ -z "$py_list" ]]; then
    echo "ERROR: Could not extract TERMINAL_AGENT_STATUSES from $DAEMON_PY" >&2
    exit 1
fi
if [[ -z "$api_ts_list" ]]; then
    echo "ERROR: Could not extract TERMINAL_AGENT_STATUSES from $CONSTANTS_TS" >&2
    exit 1
fi
if [[ -z "$web_tsx_list" ]]; then
    echo "ERROR: Could not extract TERMINAL_AGENT_STATUSES from $UI_PRIMITIVES_TSX" >&2
    exit 1
fi

# ─── Compare ─────────────────────────────────────────────────────────────
failures=0

if [[ "$py_list" != "$api_ts_list" ]]; then
    echo "ERROR: Terminal-status list mismatch between daemon.py and constants.ts" >&2
    echo "" >&2
    echo "  daemon.py:      $(echo "$py_list" | tr '\n' ' ')" >&2
    echo "  constants.ts:   $(echo "$api_ts_list" | tr '\n' ' ')" >&2
    echo "" >&2
    failures=$((failures + 1))
fi

if [[ "$py_list" != "$web_tsx_list" ]]; then
    echo "ERROR: Terminal-status list mismatch between daemon.py and ui-primitives.tsx" >&2
    echo "" >&2
    echo "  daemon.py:          $(echo "$py_list" | tr '\n' ' ')" >&2
    echo "  ui-primitives.tsx:  $(echo "$web_tsx_list" | tr '\n' ' ')" >&2
    echo "" >&2
    failures=$((failures + 1))
fi

if [[ $failures -gt 0 ]]; then
    echo "  Fix: update all three copies together." >&2
    echo "  Files:" >&2
    echo "    $DAEMON_PY" >&2
    echo "    $CONSTANTS_TS" >&2
    echo "    $UI_PRIMITIVES_TSX" >&2
    exit 1
fi

echo "All three terminal-status lists match: $(echo "$py_list" | tr '\n' ' ')"
exit 0
