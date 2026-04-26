#!/usr/bin/env bash
# check-aws-flag-portability.sh — Detect AWS CLI flags that only exist in v2.
#
# Some AWS CLI flags are v2-only and cause hard failures on v1.  The
# canonical example is --no-cli-pager: it was introduced in v2 and raises
# "Unknown options: --no-cli-pager" on any v1 runtime.  The portable
# replacement is to set AWS_PAGER="" in the environment, which both v1
# and v2 honour.
#
# This script scans all shell scripts in scripts/ for v2-only flag usage.
# For each violation it prints the offending line and the recommended fix.
#
# Usage:
#   scripts/check-aws-flag-portability.sh          # exits 0 if clean, 1 if violations
#   scripts/check-aws-flag-portability.sh [dir]    # scan a specific directory instead
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more scripts use v2-only AWS CLI flags.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCAN_DIR="${1:-$SCRIPT_DIR}"

# ─── Known v2-only AWS CLI flags ──────────────────────────────────────────
# These flags do not exist in AWS CLI v1 and will cause a hard failure when
# present in a script running on a v1 runtime (e.g. the agent-runner image).
#
# To add a new flag, append its base name (without the -- prefix) below.
V2_ONLY_FLAGS=(
    cli-pager
)

# ─── Build grep pattern ──────────────────────────────────────────────────
# Match lines containing: --<flag> or --no-<flag>
# We build an alternation of all flag forms (with and without --no- prefix).

flag_forms=()
for base in "${V2_ONLY_FLAGS[@]}"; do
    flag_forms+=("--${base}" "--no-${base}")
done

# Build alternation: (--cli-pager|--no-cli-pager|...)
alternation=$(IFS='|'; echo "${flag_forms[*]}")

# The pattern matches a v2-only flag as a standalone token (surrounded by
# whitespace or end of line). We use word-boundary-style matching to avoid
# false positives on partial matches (e.g. --cli-pagerate).
PATTERN="(${alternation})([^-a-zA-Z0-9]|$)"

# ─── Scan shell scripts ──────────────────────────────────────────────────

violations=0

for f in "$SCAN_DIR"/*.sh; do
    [ -f "$f" ] || continue
    basename_f="$(basename "$f")"

    # Use grep with extended regex to find violations.
    # Exclude comment lines (^\s*#) and echo/printf lines (string literals
    # that mention the pattern in help text or examples).
    matches=$(grep -nE "$PATTERN" "$f" 2>/dev/null \
        | grep -vE "^[0-9]+:[[:space:]]*#" \
        | grep -vE "^[0-9]+:.*\b(echo|printf)\b" \
        || true)

    if [[ -n "$matches" ]]; then
        echo "ERROR: $basename_f uses an AWS CLI v2-only flag"
        echo ""
        echo "  Violations:"
        while IFS= read -r line; do
            echo "    $line"
        done <<< "$matches"
        echo ""
        echo "  These flags do not exist in AWS CLI v1 and cause a hard failure"
        echo "  on v1 runtimes (e.g. the agent-runner image)."
        echo ""
        echo "  Fix: remove the flag and add the following line immediately after"
        echo "  'set -euo pipefail' at the top of the script:"
        echo ""
        echo "    # AWS CLI v1/v2 portability: suppress pager without --no-cli-pager (v2-only flag). See #3461."
        echo "    export AWS_PAGER=\"\""
        echo ""
        echo "  AWS_PAGER=\"\" is honoured by both v1 and v2 and suppresses the"
        echo "  interactive pager without using any v2-only flag."
        echo ""
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo "Found $violations script(s) with AWS CLI v2-only flag usage."
    exit 1
fi

echo "All scripts clean — no AWS CLI v2-only flag usage detected."
exit 0
