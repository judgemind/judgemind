#!/usr/bin/env bash
# check-hardcoded-colors.sh — Detect hardcoded Tailwind color classes in TSX.
#
# Enforces the use of semantic design tokens (text-foreground, bg-card, etc.)
# instead of hardcoded Tailwind color utilities (text-slate-500, bg-gray-200,
# etc.) in TSX component files.  Catches regressions after the cleanup done
# in #1444.
#
# Scans packages/web/src/ for patterns like text-slate-500, bg-zinc-100,
# border-stone-300 in .tsx files.
#
# Exempted by design:
#   - prose-slate (Tailwind Typography plugin theme class)
#   - Status/semantic colors: red, green, yellow, blue, amber, emerald,
#     orange, teal, cyan, violet, purple, pink, rose, lime, indigo, fuchsia,
#     sky (these are semantic in context — used for status indicators,
#     success/error/warning states, etc.)
#   - Non-TSX files (only .tsx files are checked)
#   - Test files
#
# Usage:
#   scripts/check-hardcoded-colors.sh          # exits 0 if clean, 1 if violations
#   scripts/check-hardcoded-colors.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No hardcoded color classes found.
#   1 — One or more files contain hardcoded color classes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-}"

# ─── Neutral color families (these should use semantic tokens) ────────
# slate, gray, zinc, stone, neutral — the "gray scale" families in Tailwind.
# These are the ones that should be replaced with semantic tokens like
# text-foreground, text-muted-foreground, bg-card, bg-muted, border, etc.
NEUTRAL_COLORS="slate|gray|zinc|stone|neutral"

# ─── Tailwind utility prefixes to check ──────────────────────────────
# We check text-, bg-, and border- utilities with neutral color shades.
PATTERN="(text|bg|border)-($NEUTRAL_COLORS)-[0-9]"

# ─── Determine scan targets ─────────────────────────────────────────
if [ -n "$SCAN_DIR" ]; then
    scan_targets=("$SCAN_DIR")
else
    # Default: scan packages/web/src/ for TSX files
    if [ -d "$REPO_ROOT/packages/web/src" ]; then
        scan_targets=("$REPO_ROOT/packages/web/src")
    else
        echo "All clean — packages/web/src/ not found, nothing to scan."
        exit 0
    fi
fi

# ─── Scan for violations ────────────────────────────────────────────
violations=0

for target in "${scan_targets[@]}"; do
    [ -e "$target" ] || continue

    # Find all .tsx files and grep for the pattern
    matches=$(grep -rnE "$PATTERN" "$target" --include='*.tsx' 2>/dev/null || true)

    [ -z "$matches" ] && continue

    while IFS= read -r line; do
        # Skip test files
        case "$line" in
            */__tests__/*|*/tests/*|*.test.tsx*|*.spec.tsx*) continue ;;
        esac

        # Skip prose-slate (Tailwind Typography plugin)
        content="${line#*:}"   # strip filename
        content="${content#*:}"  # strip line number
        if [[ "$content" == *"prose-slate"* ]] || [[ "$content" == *"prose-gray"* ]] || [[ "$content" == *"prose-zinc"* ]]; then
            # Only skip if the match IS the prose- variant
            # Check: remove all prose-{color} occurrences, then recheck for the pattern
            cleaned=$(echo "$content" | sed -E 's/prose-(slate|gray|zinc|stone|neutral)//g')
            if ! echo "$cleaned" | grep -qE "$PATTERN"; then
                continue
            fi
        fi

        # Skip comment lines (TSX // comments, JSX {/* */} comments)
        trimmed="${content#"${content%%[![:space:]]*}"}"
        if [[ "$trimmed" == "//"* ]]; then
            continue
        fi

        if [ $violations -eq 0 ]; then
            echo "ERROR: Found hardcoded Tailwind color classes in TSX files."
            echo ""
            echo "  Use semantic design tokens instead of hardcoded color utilities."
            echo "  For example:"
            echo "    text-slate-500  →  text-muted-foreground"
            echo "    bg-slate-100    →  bg-muted"
            echo "    border-slate-200 → border"
            echo ""
            echo "  Status colors (red, green, yellow, amber, etc.) are allowed."
            echo "  See: packages/web/src/app/globals.css for available tokens."
            echo ""
            echo "  Violations:"
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
done

if [ $violations -gt 0 ]; then
    echo ""
    echo "  Found $violations occurrence(s) of hardcoded color classes."
    echo ""
    echo "  Fix: Replace with semantic design tokens from globals.css."
    echo "  Reference: https://github.com/judgemind/judgemind/issues/1444"
    exit 1
fi

echo "All clean — no hardcoded Tailwind color classes found."
exit 0
