#!/usr/bin/env bash
# check-aws-bool-flags.sh — Detect incorrect AWS CLI boolean flag usage.
#
# AWS CLI v2 boolean flags do not accept value arguments.  Use --flag for
# true and --no-flag for false.  Passing "true" or "false" as a separate
# argument silently breaks the command or causes confusing errors.
#
# This script scans all shell scripts in scripts/ for patterns like:
#   --start-from-head false
#   --no-paginate true
#   --cli-pager false
#
# Usage:
#   scripts/check-aws-bool-flags.sh          # exits 0 if clean, 1 if violations
#   scripts/check-aws-bool-flags.sh [dir]    # scan a specific directory instead
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more scripts use boolean flags incorrectly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCAN_DIR="${1:-$SCRIPT_DIR}"

# ─── Known AWS CLI boolean flags ──────────────────────────────────────────
# These flags are toggles — they must not be followed by "true" or "false".
# Each entry can appear as --flag or --no-flag.  We check both forms.
#
# To add a new flag, append its base name (without --/--no- prefix) below.
BOOLEAN_FLAG_BASES=(
    start-from-head
    cli-pager
    paginate
    descending
    cli-auto-prompt
    dryrun
    dry-run
    no-verify-ssl
    verify-ssl
    color
)

# ─── Build grep pattern ──────────────────────────────────────────────────
# Match lines containing: --<flag> true  OR  --<flag> false
# Also match:              --no-<flag> true  OR  --no-<flag> false
#
# We build an alternation of all flag forms (with and without --no- prefix).

flag_forms=()
for base in "${BOOLEAN_FLAG_BASES[@]}"; do
    flag_forms+=("--${base}" "--no-${base}")
done

# Build alternation: (--start-from-head|--no-start-from-head|--cli-pager|...)
alternation=$(IFS='|'; echo "${flag_forms[*]}")

# The pattern matches a boolean flag followed by whitespace and true/false.
# We use word boundaries to avoid false positives on partial matches.
PATTERN="(${alternation})[[:space:]]+(true|false)"

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
        echo "ERROR: $basename_f uses AWS CLI boolean flag with value argument"
        echo ""
        echo "  Violations:"
        while IFS= read -r line; do
            echo "    $line"
        done <<< "$matches"
        echo ""
        echo "  AWS CLI v2 boolean flags do not accept 'true' or 'false' arguments."
        echo "  Use --flag (for true) or --no-flag (for false) instead."
        echo ""
        echo "  Example fix:"
        echo "    BAD:  --start-from-head false"
        echo "    GOOD: --no-start-from-head"
        echo ""
        echo "    BAD:  --no-paginate true"
        echo "    GOOD: --no-paginate"
        echo ""
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo "Found $violations script(s) with incorrect AWS CLI boolean flag usage."
    exit 1
fi

echo "All scripts clean — no incorrect AWS CLI boolean flag usage detected."
exit 0
