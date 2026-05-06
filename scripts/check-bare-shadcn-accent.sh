#!/usr/bin/env bash
# check-bare-shadcn-accent.sh — Repo-wide guard for the brand-accent vs
# shadcn-accent footgun.
#
# The shadcn `accent` Tailwind token (mapped via `hsl(var(--accent))` in
# packages/web/tailwind.config.ts) is a near-gray *hover surface* — not
# the brand amber. The brand amber lives on the separate `brand-accent`
# token (see `docs/BRAND.md §Tailwind Token Mapping` and the canonical
# usage in `packages/web/src/components/Wordmark.tsx`). The two classes
# are one character apart, which is a persistent footgun: PR #2811
# shipped `text-accent` on issue/PR links on `/admin/dispatcher` and
# they rendered as nearly-invisible gray on both light and dark mode
# (#2816). The bug is easy to make and hard to spot in code review.
#
# This guard blocks bare `text-accent`, `bg-accent`, `border-accent`,
# and `ring-accent` anywhere under `packages/web/src/`. It does NOT
# block hover-surface idioms like
# `hover:bg-accent hover:text-accent-foreground` — those are intentional
# shadcn patterns (button, dropdown, select, command, sidebar primitives
# all use them). Only bare always-visible `*-accent` usages trip the
# check.
#
# Allowlist mechanism
# -------------------
# Some legitimate "selected-row" surfaces use `bg-accent` or
# `bg-accent text-accent-foreground` deliberately as the selected-state
# chrome (see Sidebar's active nav link, JudgeProfile's motion-type
# filter pill). These are intentional shadcn idioms, not the bug class
# this check is for.
#
# To allowlist a specific line, place an inline opt-out comment on the
# same line OR within the three preceding lines:
#
#     // shadcn-accent: intentional — selected-row chrome
#     <span
#       className="bg-accent text-accent-foreground"
#     >
#
# The 3-line lookback handles JSX where the comment must sit above the
# opening tag and the className wraps to the next line. Same-line
# comments work too:
#
#     <span className="bg-accent" /* shadcn-accent: intentional */>
#
# The opt-out token is the literal string `shadcn-accent: intentional`.
# It appears in the diff and code review, so reviewers see exactly which
# lines were waived and why.
#
# Algorithm (per line)
# --------------------
#   1. Skip lines that contain `shadcn-accent: intentional` on the
#      same line OR on any of the three preceding lines.
#   2. Pre-process the line with sed to delete any modifier-prefixed
#      usage (`hover:`, `focus:`, `data-[...]:`, etc.) and any
#      `*-accent-foreground` suffix usage. Modifier-prefixed and
#      foreground-paired accent usages are legitimate shadcn idioms.
#   3. On the remaining line, look for `text-accent`, `bg-accent`,
#      `border-accent`, or `ring-accent` followed by a non-identifier
#      char.
#
# Usage
# -----
#   scripts/check-bare-shadcn-accent.sh          # scan packages/web/src
#   scripts/check-bare-shadcn-accent.sh [dir]    # scan custom dir
#
# Exit codes
# ----------
#   0 — No violations.
#   1 — One or more files use a bare shadcn accent token without an
#       allowlist comment.
#
# History
# -------
#   - #2816 introduced the narrow `/admin/dispatcher`-only guard.
#   - #2832 expanded to repo-wide and added the
#     `shadcn-accent: intentional` allowlist mechanism.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-}"

# ─── Target directory ──────────────────────────────────────────────────
DEFAULT_TARGET="$REPO_ROOT/packages/web/src"
if [[ -n "$SCAN_DIR" ]]; then
    target="$SCAN_DIR"
else
    target="$DEFAULT_TARGET"
fi

if [[ ! -d "$target" ]]; then
    echo "All clean — target directory not found: $target"
    exit 0
fi

# ─── Allowlist marker ──────────────────────────────────────────────────
# The literal string we look for on a preceding line or same line to
# waive the check on a specific occurrence. Spelled with a leading
# space-or-non-alphanum boundary so we don't match prose like
# "the shadcn-accent: intentional pattern is" inside a paragraph in
# unrelated files (we still scan only .tsx/.ts so the practical
# concern is comments adjacent to a flagged line).
ALLOWLIST_MARKER='shadcn-accent: intentional'

# ─── sed pre-processor ──────────────────────────────────────────────────
# Strip every occurrence of:
#   - `<prefix>:(text|bg|border|ring)-accent`  (any non-space modifier,
#     e.g. `hover:`, `focus:`, `group-hover:`, `data-[selected=true]:`,
#     `dark:hover:`, etc.). Three passes handle chained modifiers like
#     `dark:hover:bg-accent`.
#   - `(text|bg|border|ring)-accent-foreground`  (shadcn's
#     foreground-on-surface pairing).
#
# After stripping, any surviving `(text|bg|border|ring)-accent`
# followed by a non-identifier character is a bare shadcn accent token
# and a violation.
strip_allowed() {
    sed -E \
        -e 's/([^[:space:]"'\'':`]+:)(text|bg|border|ring)-accent/__MASKED__/g' \
        -e 's/(text|bg|border|ring)-accent-foreground/__MASKED__/g' \
        -e 's/([^[:space:]"'\'':`]+:)(text|bg|border|ring)-accent/__MASKED__/g' \
        -e 's/(text|bg|border|ring)-accent-foreground/__MASKED__/g' \
        -e 's/([^[:space:]"'\'':`]+:)(text|bg|border|ring)-accent/__MASKED__/g' \
        -e 's/(text|bg|border|ring)-accent-foreground/__MASKED__/g'
}

# Regex for bare, surviving `(text|bg|border|ring)-accent` followed by a
# non-identifier character. Using grep -E for portability (macOS/BSD sed
# vs GNU sed word boundaries differ).
BAD_TOKEN_RE='(text|bg|border|ring)-accent($|[^a-zA-Z0-9_-])'

# ─── Scan ──────────────────────────────────────────────────────────────
violations=0

# Find all .tsx/.ts files under the target, excluding tests.
# Avoid `mapfile -d ''` (bash 4+) so the script runs on macOS bash 3.2
# too — same constraint observed by the predecessor
# `check-admin-dispatcher-brand-accent.sh` (#3082).
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
    # Sliding window of the previous 3 lines (most recent last).
    # Empty strings prefill so the lookback at line 1 is well-defined.
    prev1=""  # line at line_no-1
    prev2=""  # line at line_no-2
    prev3=""  # line at line_no-3
    while IFS= read -r line || [[ -n "$line" ]]; do
        line_no=$((line_no + 1))

        # Skip comment lines — best-effort: `//`, ` * ` (JSDoc), `/*`.
        # Note: comment lines are also rotated into the lookback
        # window so the allowlist marker on a `// ...` comment line
        # carries forward to the next non-comment line.
        trimmed="${line#"${line%%[![:space:]]*}"}"
        case "$trimmed" in
            '//'*|'*'*|'/*'*)
                prev3="$prev2"; prev2="$prev1"; prev1="$line"
                continue
                ;;
        esac

        masked=$(printf '%s' "$line" | strip_allowed)

        if printf '%s\n' "$masked" | grep -qE "$BAD_TOKEN_RE"; then
            # Allowlist check: is the current line OR any of the three
            # preceding lines marked with `shadcn-accent: intentional`?
            if [[ "$line"  == *"$ALLOWLIST_MARKER"* ]] \
                || [[ "$prev1" == *"$ALLOWLIST_MARKER"* ]] \
                || [[ "$prev2" == *"$ALLOWLIST_MARKER"* ]] \
                || [[ "$prev3" == *"$ALLOWLIST_MARKER"* ]]; then
                prev3="$prev2"; prev2="$prev1"; prev1="$line"
                continue
            fi

            if [[ $violations -eq 0 ]]; then
                echo "ERROR: Bare shadcn accent token under packages/web/src/."
                echo ""
                echo "  The shadcn accent token is a near-gray hover surface,"
                echo "  NOT the brand amber. Use brand-accent for always-visible"
                echo "  amber chrome:"
                echo ""
                echo "    text-accent                -> text-brand-accent dark:text-brand-accent-light"
                echo "    bg-accent   (not hover:)   -> bg-brand-accent (see docs/BRAND.md)"
                echo ""
                echo "  If you actually meant the shadcn hover surface, pair the"
                echo "  token with a modifier such as:"
                echo "    hover:bg-accent hover:text-accent-foreground"
                echo ""
                echo "  If this is a legitimate selected-row surface (sidebar"
                echo "  active nav, filter pill, etc.), add an inline allowlist"
                echo "  comment on the preceding or same line:"
                echo "    {/* shadcn-accent: intentional — selected-row chrome */}"
                echo ""
                echo "  See issue #2816, #2832 and packages/web/src/components/Wordmark.tsx."
                echo ""
                echo "  Violations:"
                echo ""
            fi
            rel_path="${file#"$REPO_ROOT/"}"
            echo "    $rel_path:$line_no:$line"
            violations=$((violations + 1))
        fi
        prev3="$prev2"; prev2="$prev1"; prev1="$line"
    done < "$file"
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of bare shadcn accent tokens."
    echo "  Reference: https://github.com/judgemind/judgemind/issues/2816"
    exit 1
fi

echo "All clean — no bare shadcn accent tokens under packages/web/src/."
exit 0
