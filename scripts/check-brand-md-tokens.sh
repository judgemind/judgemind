#!/usr/bin/env bash
# check-brand-md-tokens.sh — Enforce BRAND.md ↔ tailwind.config.ts token sync.
#
# venv: none
# permanent: true
#
# Parses docs/BRAND.md §Tailwind Token Mapping code block and §Accent table,
# then cross-checks the extracted token/subkey/hex tuples against
# packages/web/tailwind.config.ts.  Exits non-zero with a clear per-violation
# report if the two disagree.
#
# Scope: Accent family + Tailwind Token Mapping code block only.
# Stone/semantic tables are out of scope — file a follow-up if needed.
#
# Algorithm:
#   1. From BRAND.md, extract the fenced code block under
#      "## Tailwind Token Mapping" (starting at "// packages/web/tailwind.config.ts").
#      Walk lines: when a line matches `'?<name>'?: {`, open a token scope;
#      collect subkey:hex pairs from lines matching the supported subkeys;
#      close the scope on `},`.
#   2. From BRAND.md §Accent table, parse rows matching
#      `| <Name> | \`#hex\` | \`brand-accent...\` |`
#      and emit (brand-accent, <subkey>, #hex) tuples.
#   3. Extract equivalent tuples from tailwind.config.ts inside `colors: {`.
#   4. Diff: report tokens missing from config, hex mismatches, Accent-table
#      entries absent from config.
#
# Usage:
#   scripts/check-brand-md-tokens.sh                       # use repo defaults
#   scripts/check-brand-md-tokens.sh <brand_md> <tailwind> # explicit paths (for tests)
#
# Exit codes:
#   0 — In sync.
#   1 — One or more mismatches.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BRAND_MD="${1:-$REPO_ROOT/docs/BRAND.md}"
TAILWIND_CONFIG="${2:-$REPO_ROOT/packages/web/tailwind.config.ts}"

# ─── Collect tuples from BRAND.md code block ──────────────────────────────
# Tuples written to temp arrays as "TOKEN:SUBKEY:VALUE" strings.

brand_tuples=()

# State machine over the fenced code block in BRAND.md.
# We look for the block starting with "// packages/web/tailwind.config.ts"
# inside triple-backtick fences.
in_fence=false
in_block=false
current_token=""

while IFS= read -r line; do
    # Detect opening fence
    if [[ "$line" =~ ^\`\`\` ]]; then
        if [[ "$in_fence" == false ]]; then
            in_fence=true
            in_block=false
            current_token=""
            continue
        else
            # closing fence
            in_fence=false
            in_block=false
            current_token=""
            continue
        fi
    fi

    if [[ "$in_fence" == false ]]; then
        continue
    fi

    # Look for the marker line that starts the relevant block
    if [[ "$line" =~ //\ packages/web/tailwind\.config\.ts ]]; then
        in_block=true
        continue
    fi

    if [[ "$in_block" == false ]]; then
        continue
    fi

    # Open a token scope: lines like  'brand-accent': {  or  accent: {
    if [[ "$line" =~ ^[[:space:]]*(\'[a-zA-Z0-9_-]+\'|[a-zA-Z0-9_-]+):[[:space:]]*\{ ]]; then
        # Extract token name, stripping quotes
        raw_token="${BASH_REMATCH[1]}"
        current_token="${raw_token//\'/}"
        continue
    fi

    # Close token scope
    if [[ "$line" =~ ^[[:space:]]*\}, ]]; then
        current_token=""
        continue
    fi

    # Collect subkey:value pairs inside a token scope
    if [[ -n "$current_token" ]]; then
        # Match:  DEFAULT:    '#b45309',
        #         hover:      '#92400e',
        #         foreground: 'hsl(var(--accent-foreground))',
        if [[ "$line" =~ ^[[:space:]]*(DEFAULT|hover|light|lighter|surface|foreground):[[:space:]]*(\'(#[0-9a-fA-F]{6}|hsl\(var\(--[a-z-]+\)\))\') ]]; then
            subkey="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[3]}"
            brand_tuples+=("${current_token}:${subkey}:${value}")
        fi
    fi
done < "$BRAND_MD"

# ─── Collect tuples from BRAND.md §Accent table ───────────────────────────
# Table rows look like:
#   | Amber 700 | `#b45309` | `brand-accent` (DEFAULT) |
#   | Amber 800 | `#92400e` | `brand-accent-hover` |

accent_tuples=()

while IFS= read -r line; do
    # Match table rows with a hex and a brand-accent token reference
    # Group 1: hex   Group 2: token (possibly with (DEFAULT))
    if [[ "$line" =~ \`(#[0-9a-fA-F]{6})\`[[:space:]]*\|[[:space:]]*\`(brand-accent[a-z-]*)\` ]]; then
        hex="${BASH_REMATCH[1]}"
        token_ref="${BASH_REMATCH[2]}"

        # Determine subkey from token_ref
        # brand-accent           -> DEFAULT
        # brand-accent-hover     -> hover
        # brand-accent-light     -> light
        # brand-accent-lighter   -> lighter
        # brand-accent-surface   -> surface
        # brand-accent-foreground-> foreground
        if [[ "$token_ref" == "brand-accent" ]]; then
            subkey="DEFAULT"
        else
            subkey="${token_ref#brand-accent-}"
        fi

        accent_tuples+=("brand-accent:${subkey}:${hex}")
    fi
done < "$BRAND_MD"

# ─── Collect tuples from tailwind.config.ts ───────────────────────────────

config_tuples=()
in_colors=false
depth=0
current_token=""

while IFS= read -r line; do
    # Detect colors: { block
    if [[ "$line" =~ colors:[[:space:]]*\{ ]]; then
        in_colors=true
        depth=1
        current_token=""
        continue
    fi

    if [[ "$in_colors" == false ]]; then
        continue
    fi

    # Track brace depth
    opens=$(printf '%s' "$line" | tr -cd '{' | wc -c)
    closes=$(printf '%s' "$line" | tr -cd '}' | wc -c)
    depth=$((depth + opens - closes))

    if [[ $depth -le 0 ]]; then
        in_colors=false
        current_token=""
        continue
    fi

    # At depth==1 inside colors: open a token scope
    if [[ $depth -ge 2 && -z "$current_token" ]]; then
        if [[ "$line" =~ (\'[a-zA-Z0-9_-]+\'|[a-zA-Z0-9_-]+):[[:space:]]*\{ ]]; then
            raw_token="${BASH_REMATCH[1]}"
            current_token="${raw_token//\'/}"
        fi
        continue
    fi

    # Close token scope (back to depth==1)
    if [[ $depth -eq 1 ]]; then
        current_token=""
        continue
    fi

    # Collect subkey:value pairs at depth==2
    if [[ $depth -eq 2 && -n "$current_token" ]]; then
        if [[ "$line" =~ (DEFAULT|hover|light|lighter|surface|foreground):[[:space:]]*(\'(#[0-9a-fA-F]{6}|hsl\(var\(--[a-z-]+\)\))\') ]]; then
            subkey="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[3]}"
            config_tuples+=("${current_token}:${subkey}:${value}")
        fi
    fi
done < "$TAILWIND_CONFIG"

# ─── Diff ──────────────────────────────────────────────────────────────────

violations=0

check_tuple_in_config() {
    local tuple="$1"
    local source_label="$2"
    local token subkey expected_val

    IFS=':' read -r token subkey expected_val <<< "$tuple"

    # Find matching tuple in config_tuples
    local found_val=""
    local t
    for t in "${config_tuples[@]}"; do
        local ct cs cv
        IFS=':' read -r ct cs cv <<< "$t"
        if [[ "$ct" == "$token" && "$cs" == "$subkey" ]]; then
            found_val="$cv"
            break
        fi
    done

    if [[ -z "$found_val" ]]; then
        if [[ $violations -eq 0 ]]; then
            echo "ERROR: BRAND.md ↔ tailwind.config.ts drift detected."
            echo ""
        fi
        echo "  MISSING: $token.$subkey = $expected_val (from $source_label)"
        echo "           not found in tailwind.config.ts colors block"
        violations=$((violations + 1))
    elif [[ "$found_val" != "$expected_val" ]]; then
        if [[ $violations -eq 0 ]]; then
            echo "ERROR: BRAND.md ↔ tailwind.config.ts drift detected."
            echo ""
        fi
        echo "  MISMATCH: $token.$subkey"
        echo "            BRAND.md says: $expected_val (from $source_label)"
        echo "            config  says: $found_val"
        violations=$((violations + 1))
    fi
}

# Check code-block tuples
for tuple in "${brand_tuples[@]}"; do
    check_tuple_in_config "$tuple" "Tailwind Token Mapping block"
done

# Check Accent table tuples (may overlap with code block — that's fine,
# the cross-check is the point)
for tuple in "${accent_tuples[@]}"; do
    check_tuple_in_config "$tuple" "Accent table"
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations drift violation(s)."
    echo ""
    echo "  Fix: update tailwind.config.ts to match docs/BRAND.md."
    echo "  Reference: docs/BRAND.md §Tailwind Token Mapping and §Accent"
    exit 1
fi

echo "All clean — BRAND.md tokens match tailwind.config.ts."
exit 0
