#!/usr/bin/env bash
# check-admin-dispatcher-brand-accent.sh — Enforce brand-accent on admin dispatcher.
#
# The shadcn `accent` Tailwind token (mapped via `hsl(var(--accent))` in
# packages/web/tailwind.config.ts) is a near-gray *hover surface* — not the
# brand amber.  The brand amber lives on the separate `brand-accent` token
# (see `docs/BRAND.md §Tailwind Token Mapping` and the canonical usage in
# `packages/web/src/components/Wordmark.tsx`).  The two classes are one
# character apart, which is a persistent footgun: on PR #2811 issue + PR
# links on `/admin/dispatcher` ended up styled with the hover-surface gray
# and were nearly invisible on both light and dark backgrounds (#2816).
#
# This guard blocks bare `text-accent`, `bg-accent`, `border-accent`, and
# `ring-accent` inside `packages/web/src/app/(main)/admin/dispatcher/`.
# It does NOT block hover-surface idioms like
# `hover:bg-accent hover:text-accent-foreground` — those are intentional
# shadcn patterns (see the button / dropdown primitives).  Only bare
# always-visible `*-accent` usages trip the check.
#
# Algorithm (per line):
#   1. Pre-process the line with sed to delete any modifier-prefixed
#      usage (`hover:`, `focus:`, `data-[...]:`, etc.) and any
#      `*-accent-foreground` suffix usage.  Modifier-prefixed and
#      foreground-paired accent usages are legitimate.
#   2. On the remaining line, look for `text-accent`, `bg-accent`,
#      `border-accent`, or `ring-accent` followed by a word boundary.
#
# Usage:
#   scripts/check-admin-dispatcher-brand-accent.sh          # scan default dir
#   scripts/check-admin-dispatcher-brand-accent.sh [dir]    # scan custom dir
#
# Exit codes:
#   0 — No violations.
#   1 — One or more files in the dispatcher admin surface use a bare shadcn
#       accent token that should be `brand-accent` instead.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-}"

# ─── Target directory ──────────────────────────────────────────────────
DEFAULT_TARGET="$REPO_ROOT/packages/web/src/app/(main)/admin/dispatcher"
if [[ -n "$SCAN_DIR" ]]; then
    target="$SCAN_DIR"
else
    target="$DEFAULT_TARGET"
fi

if [[ ! -d "$target" ]]; then
    echo "All clean — target directory not found: $target"
    exit 0
fi

# ─── sed pre-processor ──────────────────────────────────────────────────
# Strip every occurrence of:
#   - `<prefix>:(text|bg|border|ring)-accent`  (any non-space modifier, e.g.
#     `hover:`, `focus:`, `group-hover:`, `data-[selected=true]:`,
#     `dark:hover:`, etc.).  `[^[:space:]:]+:` handles single-modifier; we
#     apply the stripping repeatedly via `g` plus the `-E` grammar so
#     chained modifiers like `dark:hover:bg-accent` collapse in one pass.
#     Accept brackets in the modifier (e.g. `data-[...]:`) by allowing
#     any non-space, non-colon chars plus balanced `[...]` groups.
#   - `(text|bg|border|ring)-accent-foreground`  (shadcn's
#     foreground-on-surface pairing).
#
# After stripping, any surviving `(text|bg|border|ring)-accent` followed
# by a word boundary is a bare shadcn accent token and a violation.
#
# The prefix pattern uses [^[:space:]:] to match one modifier segment and
# the outer loop (performed by sed's `s///g` on a single line) is re-run
# repeatedly until the input is stable — done via `-E` with three passes,
# which is sufficient for the realistic class-string depths we see (at
# most 2-3 chained modifiers).

strip_allowed() {
    # Three passes over the line, each stripping one layer of modifier
    # prefixes plus the -foreground suffix pair.  Three passes covers up
    # to `a:b:c:text-accent` which is deeper than anything in this repo.
    sed -E \
        -e 's/([^[:space:]"'\'':`]+:)(text|bg|border|ring)-accent/__MASKED__/g' \
        -e 's/(text|bg|border|ring)-accent-foreground/__MASKED__/g' \
        -e 's/([^[:space:]"'\'':`]+:)(text|bg|border|ring)-accent/__MASKED__/g' \
        -e 's/(text|bg|border|ring)-accent-foreground/__MASKED__/g' \
        -e 's/([^[:space:]"'\'':`]+:)(text|bg|border|ring)-accent/__MASKED__/g' \
        -e 's/(text|bg|border|ring)-accent-foreground/__MASKED__/g'
}

# Regex for bare, surviving `(text|bg|border|ring)-accent` followed by a
# non-identifier character.  Using grep -E for portability (macOS/BSD sed
# vs GNU sed word boundaries differ — safer to check with grep).
BAD_TOKEN_RE='(text|bg|border|ring)-accent($|[^a-zA-Z0-9_-])'

# ─── Scan ──────────────────────────────────────────────────────────────
violations=0

# Find all .tsx/.ts files under the target, excluding tests.
# Avoid `mapfile -d ''` (bash 4+) so the script runs on macOS bash 3.2 too.
files=()
while IFS= read -r -d '' file; do
    files+=("$file")
done < <(
    find "$target" \
        -type f \
        \( -name '*.tsx' -o -name '*.ts' \) \
        ! -path '*/__tests__/*' \
        ! -name '*.test.tsx' \
        ! -name '*.test.ts' \
        -print0
)

if [[ ${#files[@]} -eq 0 ]]; then
    echo "All clean — no .tsx/.ts files under $target"
    exit 0
fi

for file in "${files[@]}"; do
    line_no=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line_no=$((line_no + 1))

        # Skip comment lines — best-effort: `//`, ` * ` (JSDoc), `/*`.
        trimmed="${line#"${line%%[![:space:]]*}"}"
        case "$trimmed" in
            '//'*) continue ;;
            '*'*)  continue ;;
            '/*'*) continue ;;
        esac

        masked=$(printf '%s' "$line" | strip_allowed)

        if printf '%s\n' "$masked" | grep -qE "$BAD_TOKEN_RE"; then
            if [[ $violations -eq 0 ]]; then
                echo "ERROR: Bare shadcn accent token on the admin dispatcher surface."
                echo ""
                echo "  The shadcn accent token is a near-gray hover surface, NOT the"
                echo "  brand amber.  Use brand-accent for always-visible amber chrome:"
                echo ""
                echo "    text-accent                -> text-brand-accent dark:text-brand-accent-light"
                echo "    bg-accent   (not hover:)   -> bg-brand-accent (see docs/BRAND.md)"
                echo ""
                echo "  If you actually meant the shadcn hover surface, pair the token"
                echo "  with a modifier such as 'hover:bg-accent hover:text-accent-foreground'."
                echo ""
                echo "  See issue #2816 and packages/web/src/components/Wordmark.tsx."
                echo ""
                echo "  Violations:"
                echo ""
            fi
            rel_path="${file#"$REPO_ROOT/"}"
            echo "    $rel_path:$line_no:$line"
            violations=$((violations + 1))
        fi
    done < "$file"
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of bare shadcn accent tokens."
    echo "  Reference: https://github.com/judgemind/judgemind/issues/2816"
    exit 1
fi

echo "All clean — no bare shadcn accent tokens on /admin/dispatcher."
exit 0
