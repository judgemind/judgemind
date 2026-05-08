#!/usr/bin/env bash
# check-placeholder-gates.sh — Detect placeholder security gates.
#
# permanent: true
#
# Flags patterns that indicate a security gate (MFA, auth, rate-limit, etc.)
# has been left as a placeholder stub rather than a real implementation.
# Catches regressions of the class documented in #2884.
#
# Flagged patterns:
#   1. "accept(ed)? any non-empty" within ±3 lines of MFA|gate|auth|security|
#      rate.?limit|token (same-line match or proximity match)
#   2. "TODO.*Phase [0-9]+.*placeholder"
#   3. "placeholder.*(MFA|gate|auth|security).*(accept|pass)"
#
# Excluded by design:
#   - Lines that reference #2884 (intentional historical regression markers)
#   - docs/ directory
#   - Test paths (**/tests/**, *.test.*, *_test.*)
#   - This script and its tests
#
# Usage:
#   scripts/check-placeholder-gates.sh          # exits 0 if clean, 1 if violations
#   scripts/check-placeholder-gates.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No placeholder security gates found.
#   1 — One or more files contain placeholder gate patterns.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Per-check extras on top of REPO_WALK_EXCLUSIONS ──────────────────
# `docs/` is excluded because design docs and incident write-ups
# routinely paraphrase the placeholder patterns this check looks for.
EXTRA_EXCLUDE_DIRS=(docs)

# ─── Files to exclude (relative to repo root) ─────────────────────────
EXCLUDE_FILES=(
    "scripts/check-placeholder-gates.sh"
    "scripts/tests/test_check_placeholder_gates.sh"
)

# ─── Path patterns to exclude ──────────────────────────────────────────
# Test directories and test files are allowed to reference these patterns.
EXCLUDE_PATH_PATTERNS=(
    "*/tests/*"
    "*_test.*"
    "*.test.*"
)

# ─── Build grep exclude arguments ────────────────────────────────────
# Canonical list (REPO_WALK_EXCLUSIONS in preflight.sh, #4308) plus the
# per-check extras above.
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}" ${EXTRA_EXCLUDE_DIRS[@]+"${EXTRA_EXCLUDE_DIRS[@]}"}; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Determine scan targets ───────────────────────────────────────────
scan_targets=()
if [[ "$SCAN_DIR" == "$REPO_ROOT" ]]; then
    for pkg_src in "$REPO_ROOT"/packages/*/src/; do
        [[ -d "$pkg_src" ]] && scan_targets+=("$pkg_src")
    done
    scan_targets+=("$REPO_ROOT/scripts/")
else
    scan_targets+=("$SCAN_DIR")
fi

# ─── Helper: check if a grep output line should be skipped ───────────
should_skip() {
    local line="$1"

    # Exempt lines referencing #2884
    if [[ "$line" == *"#2884"* ]]; then
        return 0
    fi

    # Check excluded files
    for excl in "${EXCLUDE_FILES[@]}"; do
        if [[ "$line" == *"$excl"* ]]; then
            return 0
        fi
    done

    # Check excluded path patterns
    for pat in "${EXCLUDE_PATH_PATTERNS[@]}"; do
        # shellcheck disable=SC2254
        case "$line" in
            *$pat*) return 0 ;;
        esac
    done

    return 1
}

emit_header() {
    echo "ERROR: Found placeholder security gate pattern(s) in the codebase."
    echo ""
    echo "  These patterns indicate a security gate was left as a stub."
    echo "  See #2884 for the canonical incident and remediation guide."
    echo ""
    echo "  To suppress a known-intentional stub, add a reference to #2884"
    echo "  on the same line (e.g. as a comment)."
    echo ""
    echo "  Violations:"
    echo ""
}

violations=0

# ─── Pattern 1: "accept(ed)? any non-empty" near security keywords ────
# Grep for the base phrase, then verify a security keyword appears
# within ±3 lines of each hit.

PAT1='accept(ed)? any non-empty'
SECURITY_KW='(MFA|gate|auth|security|rate.?limit|token)'

pat1_matches=$(grep -rnE "$PAT1" "${scan_targets[@]}" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$pat1_matches" ]]; then
    while IFS= read -r match_line; do
        should_skip "$match_line" && continue

        # Extract filepath and line number (grep format: path:lineno:content)
        filepath="${match_line%%:*}"
        rest="${match_line#*:}"
        lineno="${rest%%:*}"

        # Read ±3 lines around the match from the file to check for security kw
        start=$(( lineno - 3 ))
        (( start < 1 )) && start=1
        end=$(( lineno + 3 ))

        context_block=""
        if [[ -f "$filepath" ]]; then
            context_block=$(sed -n "${start},${end}p" "$filepath" 2>/dev/null || true)
        fi

        # Skip if #2884 appears anywhere in the surrounding context block
        # (the marker may be on a preceding comment line, not the match line itself)
        if echo "$context_block" | grep -q '#2884' 2>/dev/null; then
            continue
        fi

        if echo "$context_block" | grep -qE "$SECURITY_KW" 2>/dev/null; then
            [[ $violations -eq 0 ]] && emit_header
            echo "    $match_line"
            violations=$((violations + 1))
        fi
    done <<< "$pat1_matches"
fi

# ─── Pattern 2: TODO.*Phase [0-9]+.*placeholder ───────────────────────
PAT2='TODO.*Phase [0-9]+.*placeholder'

pat2_matches=$(grep -rnE "$PAT2" "${scan_targets[@]}" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$pat2_matches" ]]; then
    while IFS= read -r line; do
        should_skip "$line" && continue
        [[ $violations -eq 0 ]] && emit_header
        echo "    $line"
        violations=$((violations + 1))
    done <<< "$pat2_matches"
fi

# ─── Pattern 3: placeholder.*(MFA|gate|auth|security).*(accept|pass) ──
PAT3='placeholder.*(MFA|gate|auth|security).*(accept|pass)'

pat3_matches=$(grep -rnE "$PAT3" "${scan_targets[@]}" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$pat3_matches" ]]; then
    while IFS= read -r line; do
        should_skip "$line" && continue
        [[ $violations -eq 0 ]] && emit_header
        echo "    $line"
        violations=$((violations + 1))
    done <<< "$pat3_matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of placeholder security gate patterns."
    echo ""
    echo "  Fix: Implement the real gate, or add '#2884' to the line if the stub"
    echo "  is an intentional regression marker reviewed in that issue."
    echo ""
    exit 1
fi

echo "All clean — no placeholder security gates found."
exit 0
