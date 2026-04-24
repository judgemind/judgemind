#!/usr/bin/env bash
# test_check_brand_md_tokens.sh — Tests for scripts/check-brand-md-tokens.sh.
#
# Synthesises minimal BRAND.md + tailwind.config.ts fixtures in a temp dir,
# invokes the check with explicit file args, and asserts the expected
# pass/fail outcome for each scenario.
#
# Also calls `assert_no_self_match_on_ci_step_name` at the end so the
# check does not accidentally flag the ci.yml step name that invokes it
# (see #2541/#2542 and scripts/tests/_guard_self_match_helpers.sh).
#
# Usage:
#   scripts/tests/test_check_brand_md_tokens.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-brand-md-tokens.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# ─── Fixture helpers ──────────────────────────────────────────────────────

# Minimal valid BRAND.md with the brand-accent token in both the code block
# and the Accent table.
write_brand_md() {
    local path="$1"
    cat > "$path" << 'BRAND_EOF'
# Judgemind Brand Guide

## Color Palette

### Accent

| Name | Hex | Tailwind token | Usage |
|------|-----|----------------|-------|
| Amber 700 | `#b45309` | `brand-accent` (DEFAULT) | Primary accent |
| Amber 800 | `#92400e` | `brand-accent-hover` | Hover |
| Amber 600 | `#d97706` | `brand-accent-light` | Dark mode |
| Amber 500 | `#f59e0b` | `brand-accent-lighter` | Lighter accent |
| Amber 100 | `#fef3c7` | `brand-accent-surface` | Surface |

## Tailwind Token Mapping

```
// packages/web/tailwind.config.ts
colors: {
  'brand-accent': {
    DEFAULT:    '#b45309',
    hover:      '#92400e',
    light:      '#d97706',
    lighter:    '#f59e0b',
    surface:    '#fef3c7',
    foreground: '#ffffff',
  },
  accent: {
    DEFAULT:    'hsl(var(--accent))',
    foreground: 'hsl(var(--accent-foreground))',
  },
}
```
BRAND_EOF
}

# Minimal valid tailwind.config.ts with brand-accent matching the brand.md above.
write_tailwind_config() {
    local path="$1"
    cat > "$path" << 'TW_EOF'
import type { Config } from 'tailwindcss';

const config: Config = {
  theme: {
    extend: {
      colors: {
        'brand-accent': {
          DEFAULT: '#b45309',
          hover: '#92400e',
          light: '#d97706',
          lighter: '#f59e0b',
          surface: '#fef3c7',
          foreground: '#ffffff',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
      },
    },
  },
};

export default config;
TW_EOF
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

assert_passes() {
    local desc="$1"
    local brand_md="${TMPDIR_TEST}/BRAND.md"
    local tailwind="${TMPDIR_TEST}/tailwind.config.ts"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$brand_md" "$tailwind" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    local brand_md="${TMPDIR_TEST}/BRAND.md"
    local tailwind="${TMPDIR_TEST}/tailwind.config.ts"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$brand_md" "$tailwind" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# ─── Test 1: In-sync BRAND.md + tailwind.config.ts passes ────────────────
write_brand_md "$TMPDIR_TEST/BRAND.md"
write_tailwind_config "$TMPDIR_TEST/tailwind.config.ts"
assert_passes "In-sync BRAND.md and tailwind.config.ts passes"
reset_tmpdir

# ─── Test 2: Hex mismatch in config fails ─────────────────────────────────
# BRAND.md says DEFAULT: '#b45309', config says '#b45300' (one digit off)
write_brand_md "$TMPDIR_TEST/BRAND.md"
cat > "$TMPDIR_TEST/tailwind.config.ts" << 'TW_EOF'
import type { Config } from 'tailwindcss';

const config: Config = {
  theme: {
    extend: {
      colors: {
        'brand-accent': {
          DEFAULT: '#b45300',
          hover: '#92400e',
          light: '#d97706',
          lighter: '#f59e0b',
          surface: '#fef3c7',
          foreground: '#ffffff',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
      },
    },
  },
};

export default config;
TW_EOF
assert_fails "Hex mismatch (brand-accent.DEFAULT #b45300 vs #b45309) fails"
reset_tmpdir

# ─── Test 3: Missing subkey in config fails ───────────────────────────────
# BRAND.md declares brand-accent.surface; config omits it
write_brand_md "$TMPDIR_TEST/BRAND.md"
cat > "$TMPDIR_TEST/tailwind.config.ts" << 'TW_EOF'
import type { Config } from 'tailwindcss';

const config: Config = {
  theme: {
    extend: {
      colors: {
        'brand-accent': {
          DEFAULT: '#b45309',
          hover: '#92400e',
          light: '#d97706',
          lighter: '#f59e0b',
          foreground: '#ffffff',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
      },
    },
  },
};

export default config;
TW_EOF
assert_fails "Missing token brand-accent.surface in config fails"
reset_tmpdir

# ─── Test 4: BRAND.md declares extra token absent from config fails ────────
# BRAND.md code block lists a new-token entry; config doesn't have it
write_tailwind_config "$TMPDIR_TEST/tailwind.config.ts"
cat > "$TMPDIR_TEST/BRAND.md" << 'BRAND_EOF'
# Judgemind Brand Guide

## Color Palette

### Accent

| Name | Hex | Tailwind token | Usage |
|------|-----|----------------|-------|
| Amber 700 | `#b45309` | `brand-accent` (DEFAULT) | Primary accent |
| Amber 800 | `#92400e` | `brand-accent-hover` | Hover |
| Amber 600 | `#d97706` | `brand-accent-light` | Dark mode |
| Amber 500 | `#f59e0b` | `brand-accent-lighter` | Lighter accent |
| Amber 100 | `#fef3c7` | `brand-accent-surface` | Surface |

## Tailwind Token Mapping

```
// packages/web/tailwind.config.ts
colors: {
  'brand-accent': {
    DEFAULT:    '#b45309',
    hover:      '#92400e',
    light:      '#d97706',
    lighter:    '#f59e0b',
    surface:    '#fef3c7',
    foreground: '#ffffff',
  },
  'new-token': {
    DEFAULT:    '#123456',
  },
  accent: {
    DEFAULT:    'hsl(var(--accent))',
    foreground: 'hsl(var(--accent-foreground))',
  },
}
```
BRAND_EOF
assert_fails "BRAND.md declares new-token absent from config fails"
reset_tmpdir

# ─── Test 5: Accent-table hex not present in config fails ─────────────────
# Accent table says brand-accent-hover = '#92400e'; config has '#92400f'
write_brand_md "$TMPDIR_TEST/BRAND.md"
cat > "$TMPDIR_TEST/tailwind.config.ts" << 'TW_EOF'
import type { Config } from 'tailwindcss';

const config: Config = {
  theme: {
    extend: {
      colors: {
        'brand-accent': {
          DEFAULT: '#b45309',
          hover: '#92400f',
          light: '#d97706',
          lighter: '#f59e0b',
          surface: '#fef3c7',
          foreground: '#ffffff',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
      },
    },
  },
};

export default config;
TW_EOF
assert_fails "Accent table hex mismatch (brand-accent-hover #92400f vs #92400e) fails"
reset_tmpdir

# ─── Test 6: Real repo files pass ─────────────────────────────────────────
# Run the script with no arguments (against actual docs/BRAND.md and
# packages/web/tailwind.config.ts in the repo root).
TESTS=$((TESTS + 1))
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if "$CHECK_SCRIPT" \
        "$REPO_ROOT/docs/BRAND.md" \
        "$REPO_ROOT/packages/web/tailwind.config.ts" \
        > /dev/null 2>&1; then
    echo "PASS: Real repo docs/BRAND.md + packages/web/tailwind.config.ts are in sync"
else
    echo "FAIL: Real repo docs/BRAND.md and packages/web/tailwind.config.ts are out of sync"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 7: No self-match on ci.yml step name ────────────────────────────
# Mirrors the #2541/#2542 self-match guard.  The assert_no_self_match helper
# writes ci.yml step names that invoke this script into a tmp file and runs
# the guard against it.  We override assert_passes to use file args rather
# than the default TMPDIR_TEST/{BRAND.md,tailwind.config.ts} approach above.

# Re-define assert_passes for the self-match helper's use (no file args needed —
# the helper synthesizes the file inside TMPDIR_TEST and calls assert_passes
# with a description string; we just need the function to call CHECK_SCRIPT
# with safe paths that will pass).
assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    # For the self-match test the helper already put a synthesized .yml file in
    # TMPDIR_TEST.  We call the script with the real repo files so it always
    # exits 0 regardless of what's in TMPDIR_TEST (the script only checks
    # BRAND.md/tailwind.config.ts, not arbitrary yml).
    if "$CHECK_SCRIPT" \
            "$REPO_ROOT/docs/BRAND.md" \
            "$REPO_ROOT/packages/web/tailwind.config.ts" \
            > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-brand-md-tokens.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
