#!/usr/bin/env bash
# test_check_bare_shadcn_accent.sh — Tests for
# scripts/check-bare-shadcn-accent.sh.
#
# Synthesises small .tsx fixtures in a temp dir, runs the guard against
# the temp dir, and asserts the expected pass/fail outcome for every
# canonical pattern described in the check script's header comment.
#
# Mirrors the scope of test_check_admin_dispatcher_brand_accent.sh so
# the repo-wide replacement preserves all existing coverage, plus adds
# cases for the `shadcn-accent: intentional` allowlist mechanism (#2832).
#
# Also calls `assert_no_self_match_on_ci_step_name` at the end so the
# check does not accidentally flag the ci.yml step name that invokes
# it (see #2541/#2542 and scripts/tests/_guard_self_match_helpers.sh).
#
# Usage:
#   scripts/tests/test_check_bare_shadcn_accent.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-bare-shadcn-accent.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: Bare text-accent in TSX should fail ─────────────────────────
create_test_file "Bad.tsx" \
    'export const Bad = () => <span className="text-accent">hi</span>;' > /dev/null
assert_fails "Bare text-accent on a TSX element is flagged"
reset_tmpdir

# ─── Test 2: Bare bg-accent in TSX should fail ───────────────────────────
create_test_file "BadBg.tsx" \
    'export const Bad = () => <div className="bg-accent p-2">hi</div>;' > /dev/null
assert_fails "Bare bg-accent on a TSX element is flagged"
reset_tmpdir

# ─── Test 3: Bare border-accent in TSX should fail ───────────────────────
create_test_file "BadBorder.tsx" \
    'export const Bad = () => <div className="border border-accent">hi</div>;' > /dev/null
assert_fails "Bare border-accent on a TSX element is flagged"
reset_tmpdir

# ─── Test 4: Bare ring-accent in TSX should fail ─────────────────────────
create_test_file "BadRing.tsx" \
    'export const Bad = () => <input className="ring-2 ring-accent" />;' > /dev/null
assert_fails "Bare ring-accent on a TSX element is flagged"
reset_tmpdir

# ─── Test 5: brand-accent idiom passes ───────────────────────────────────
create_test_file "Good.tsx" \
    'export const Ok = () => <span className="text-brand-accent dark:text-brand-accent-light">hi</span>;' > /dev/null
assert_passes "Brand-accent idiom (text-brand-accent) is allowed"
reset_tmpdir

# ─── Test 6: hover:bg-accent hover:text-accent-foreground pattern passes ─
# This is shadcn's canonical hover behaviour — ui/button.tsx, dropdown,
# select, command, sidebar all use it. It must not be flagged.
create_test_file "Hover.tsx" \
    'export const Hov = () => <button className="hover:bg-accent hover:text-accent-foreground">hi</button>;' > /dev/null
assert_passes "Hover-state accent pair (hover:bg-accent hover:text-accent-foreground) is allowed"
reset_tmpdir

# ─── Test 7: focus:bg-accent passes ──────────────────────────────────────
create_test_file "Focus.tsx" \
    'export const Foc = () => <input className="focus:bg-accent focus:text-accent-foreground" />;' > /dev/null
assert_passes "focus:bg-accent pair is allowed"
reset_tmpdir

# ─── Test 8: data-[selected=true]:bg-accent pattern passes ───────────────
# Shadcn's command.tsx / dropdown-menu.tsx / select.tsx use this form.
create_test_file "Selected.tsx" \
    'export const Sel = () => <li className="data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground">hi</li>;' > /dev/null
assert_passes "data-[selected=true]:bg-accent pattern is allowed"
reset_tmpdir

# ─── Test 9: text-accent-foreground alone passes ─────────────────────────
# Used in sidebar.tsx (expect().toContain('text-accent-foreground')) — the
# foreground token pairs with an already-styled surface.
create_test_file "Fg.tsx" \
    'export const Fg = () => <span className="text-accent-foreground">hi</span>;' > /dev/null
assert_passes "text-accent-foreground alone is allowed"
reset_tmpdir

# ─── Test 10: Comment line referencing text-accent passes ────────────────
create_test_file "Comment.tsx" \
    '// Note: text-accent is the shadcn hover surface — do not use it here.
export const Ok = () => <span className="text-brand-accent">hi</span>;' > /dev/null
assert_passes "// comment referencing text-accent as explanatory context is allowed"
reset_tmpdir

# ─── Test 11: Test files (.test.tsx) are excluded ────────────────────────
create_test_file "page.test.tsx" \
    'it("has text-accent", () => { expect(el).toHaveClass("text-accent"); });' > /dev/null
assert_passes ".test.tsx files are excluded from the scan"
reset_tmpdir

# ─── Test 12: Underscore-joined lookalike is not matched ─────────────────
# `text_accent` as an identifier, `textAccent` as a JS var, must not match.
create_test_file "Lookalike.tsx" \
    'const text_accent = 1; const textAccent = 2; export {};' > /dev/null
assert_passes "Underscore / camelCase lookalikes do not match"
reset_tmpdir

# ─── Test 13: Empty directory passes ─────────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 14: Mixed file — bad line in TSX is caught ─────────────────────
create_test_file "Mixed.tsx" \
    'import X from "x";
// hover:text-accent would be fine on a hover pair.
export const Bad = () => <span className="text-accent underline">hi</span>;
export const Good = () => <span className="text-brand-accent">hi</span>;' > /dev/null
assert_fails "Real usage line in a mixed file is caught"
reset_tmpdir

# ─── Test 15: Files under __tests__/ are excluded ────────────────────────
# The narrow predecessor excluded only test filename suffixes; the
# repo-wide guard must also exclude __tests__/ subdirectories so that
# regression tests can spell `text-accent` literally in expectations.
create_test_file "__tests__/Foo.tsx" \
    'export const Bad = () => <span className="text-accent">hi</span>;' > /dev/null
assert_passes "Files under __tests__/ are excluded from the scan"
reset_tmpdir

# ─── Test 16: Allowlist marker on the same line passes ───────────────────
create_test_file "Allow.tsx" \
    'export const Sel = () => <span className="bg-accent" /* shadcn-accent: intentional */>hi</span>;' > /dev/null
assert_passes "shadcn-accent: intentional on the same line waives the violation"
reset_tmpdir

# ─── Test 17: Allowlist marker on the immediately preceding line passes ──
create_test_file "AllowPrev.tsx" \
    'export const Sel = () => (
  // shadcn-accent: intentional — selected-row chrome
  <span className="bg-accent text-accent-foreground">hi</span>
);' > /dev/null
assert_passes "shadcn-accent: intentional on the preceding line waives the violation"
reset_tmpdir

# ─── Test 18: Allowlist marker 3 lines back (wrapped JSX) passes ─────────
# The motion-type filter pill in JudgeProfile.tsx puts the allowlist
# comment above the opening <span and the className wraps to the next
# line. The lookback window must reach 3 lines back to catch this.
create_test_file "AllowWrap.tsx" \
    'export const Wrap = () => (
  // shadcn-accent: intentional — wrapped JSX selected-row chrome
  <span
    data-testid="filter-pill"
    className="bg-accent text-accent-foreground"
  >hi</span>
);' > /dev/null
assert_passes "shadcn-accent: intentional within 3 preceding lines waives the violation (wrapped JSX)"
reset_tmpdir

# ─── Test 19: Allowlist marker 4+ lines back does NOT waive ──────────────
# A bare reference too far up the file must not silently allowlist a
# later violation — the marker has to be near the offending line.
create_test_file "AllowFar.tsx" \
    'export const Far = () => (
  // shadcn-accent: intentional — but unrelated, far away
  <div>
    <p>filler 1</p>
    <p>filler 2</p>
    <span className="bg-accent">later</span>
  </div>
);' > /dev/null
assert_fails "shadcn-accent: intentional more than 3 lines away does NOT waive a later violation"
reset_tmpdir

# ─── Test 20: Allowlist marker only waives its line, not later ones ──────
# Test that a single allowlist marker doesn't cascade to flag-free
# everything in the rest of the file.
create_test_file "AllowOne.tsx" \
    'export const One = () => (
  // shadcn-accent: intentional
  <span className="bg-accent">selected</span>
);
export const Two = () => (
  <span className="bg-accent">unrelated bug</span>
);' > /dev/null
assert_fails "Allowlist on one violation does not cover unrelated later violation"
reset_tmpdir

# ─── #4225 expansion: *-foreground family + invisible-chrome typos ──────
#
# The guard expansion in #4225 adds:
#   - bare `(text|bg|border|ring)-X-foreground` for X in
#     {primary, secondary, card, popover, destructive, accent}
#   - `text-background` / `(bg|border|ring)-foreground` symmetric typos
#   - `text-muted-foreground` allowlist (legitimate body color)
# Tests 22-37 below cover the new cases. The marker name is preserved
# from #2832 so existing waivers continue to work (already covered by
# tests 16-20 above).
# ─────────────────────────────────────────────────────────────────────────

# ─── Test 22: Bare text-primary-foreground is flagged ────────────────────
create_test_file "BadFgPrimary.tsx" \
    'export const Bad = () => <span className="text-primary-foreground">hi</span>;' > /dev/null
assert_fails "Bare text-primary-foreground (no paired bg-primary) is flagged"
reset_tmpdir

# ─── Test 23: bg-primary text-primary-foreground pair passes ─────────────
# Same-element pairing is the canonical shadcn idiom (button.tsx,
# badge.tsx); the foreground sits on its paired surface.
create_test_file "GoodPairPrimary.tsx" \
    'export const Ok = () => <button className="bg-primary text-primary-foreground hover:bg-primary/90">hi</button>;' > /dev/null
assert_passes "bg-primary text-primary-foreground on same element passes (paired idiom)"
reset_tmpdir

# ─── Test 24: Bare text-secondary-foreground is flagged ──────────────────
create_test_file "BadFgSecondary.tsx" \
    'export const Bad = () => <span className="text-secondary-foreground">hi</span>;' > /dev/null
assert_fails "Bare text-secondary-foreground is flagged"
reset_tmpdir

# ─── Test 25: Bare text-card-foreground is flagged ───────────────────────
create_test_file "BadFgCard.tsx" \
    'export const Bad = () => <div className="rounded-lg p-4 text-card-foreground">hi</div>;' > /dev/null
assert_fails "Bare text-card-foreground without bg-card is flagged"
reset_tmpdir

# ─── Test 26: bg-card text-card-foreground pair passes ───────────────────
# Mirrors `packages/web/src/components/ui/card.tsx`.
create_test_file "GoodPairCard.tsx" \
    "export const Ok = () => <div className={cn('rounded-lg border bg-card text-card-foreground shadow-sm', className)} />;" > /dev/null
assert_passes "bg-card text-card-foreground inside cn() passes (paired idiom)"
reset_tmpdir

# ─── Test 27: Bare text-popover-foreground is flagged ────────────────────
create_test_file "BadFgPopover.tsx" \
    'export const Bad = () => <span className="text-popover-foreground">hi</span>;' > /dev/null
assert_fails "Bare text-popover-foreground is flagged"
reset_tmpdir

# ─── Test 28: Bare text-destructive-foreground is flagged ────────────────
create_test_file "BadFgDestructive.tsx" \
    'export const Bad = () => <span className="text-destructive-foreground">hi</span>;' > /dev/null
assert_fails "Bare text-destructive-foreground is flagged"
reset_tmpdir

# ─── Test 29: Modifier-prefixed *-foreground passes ──────────────────────
# Mirrors `packages/web/src/components/ui/checkbox.tsx` - the
# `data-[state=checked]:` modifier marks a state-conditional that is
# legitimate even without an unmodified surface partner.
create_test_file "ModifierFg.tsx" \
    'export const Ok = () => <button className="data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground">hi</button>;' > /dev/null
assert_passes "data-[state=checked]:text-primary-foreground passes (modifier-prefixed)"
reset_tmpdir

# ─── Test 30: dark: chained modifier-prefixed *-foreground passes ────────
create_test_file "DarkModifierFg.tsx" \
    'export const Ok = () => <span className="dark:hover:text-secondary-foreground">hi</span>;' > /dev/null
assert_passes "dark:hover:text-secondary-foreground passes (chained modifier)"
reset_tmpdir

# ─── Test 31: text-muted-foreground on <p> passes ────────────────────────
create_test_file "MutedP.tsx" \
    'export const Ok = () => <p className="text-muted-foreground">hi</p>;' > /dev/null
assert_passes "text-muted-foreground on <p> passes (legitimate body color)"
reset_tmpdir

# ─── Test 32: text-muted-foreground on <span> passes ─────────────────────
create_test_file "MutedSpan.tsx" \
    'export const Ok = () => <span className="text-muted-foreground">hi</span>;' > /dev/null
assert_passes "text-muted-foreground on <span> passes (legitimate body color)"
reset_tmpdir

# ─── Test 33: text-muted-foreground inside cn(...) passes ────────────────
create_test_file "MutedCn.tsx" \
    "export const Ok = ({className}: any) => <span className={cn('text-sm text-muted-foreground', className)}>hi</span>;" > /dev/null
assert_passes "text-muted-foreground inside cn(...) passes"
reset_tmpdir

# ─── Test 34: Bare bg-foreground is flagged ──────────────────────────────
# Symmetric typo - `bg-foreground` paints the foreground color as a
# surface. On a default-cascade page this is near-black-on-near-black
# (light mode) or white-on-white (dark mode). 0 current usages.
create_test_file "BadBgFg.tsx" \
    'export const Bad = () => <div className="bg-foreground p-4">hi</div>;' > /dev/null
assert_fails "Bare bg-foreground is flagged (symmetric typo)"
reset_tmpdir

# ─── Test 35: Bare text-background is flagged ────────────────────────────
# Symmetric typo - `text-background` paints text in the surface color.
create_test_file "BadTextBg.tsx" \
    'export const Bad = () => <span className="text-background">hi</span>;' > /dev/null
assert_fails "Bare text-background is flagged (symmetric typo)"
reset_tmpdir

# ─── Test 36: Bare border-foreground is flagged ──────────────────────────
create_test_file "BadBorderFg.tsx" \
    'export const Bad = () => <div className="border border-foreground">hi</div>;' > /dev/null
assert_fails "Bare border-foreground is flagged (symmetric typo)"
reset_tmpdir

# ─── Test 37: Bare ring-foreground is flagged ────────────────────────────
create_test_file "BadRingFg.tsx" \
    'export const Bad = () => <input className="ring-2 ring-foreground" />;' > /dev/null
assert_fails "Bare ring-foreground is flagged (symmetric typo)"
reset_tmpdir

# ─── Test 38: text-foreground (legitimate body text) passes ──────────────
# `text-foreground` is the default body-text token and the symmetric
# partner of `bg-background` (default page surface). It is NOT in the
# forbidden list - only `text-background` (swap) and
# `bg-foreground` (swap) are flagged.
create_test_file "TextFg.tsx" \
    'export const Ok = () => <h1 className="text-xl font-bold text-foreground">hi</h1>;' > /dev/null
assert_passes "Bare text-foreground passes (legitimate body text token)"
reset_tmpdir

# ─── Test 39: bg-background passes ───────────────────────────────────────
# Symmetric to test 38 - `bg-background` is the default surface and is
# not in the forbidden list.
create_test_file "BgBackground.tsx" \
    'export const Ok = () => <body className="bg-background text-foreground">hi</body>;' > /dev/null
assert_passes "bg-background passes (default surface token)"
reset_tmpdir

# ─── Test 40: Allowlist marker waives a *-foreground violation ────────────
# Mirrors the parent-paired pattern from
# `packages/web/src/components/Autocomplete.tsx` where the surface lives
# on a parent <ul> and the foreground lives on the <li> child. The
# `shadcn-accent: intentional` marker (preserved from #2832) waives the
# guard.
create_test_file "AllowFg.tsx" \
    'export const Ok = () => (
  <ul className="bg-popover">
    {/* shadcn-accent: intentional - parent-paired chrome */}
    <li className="text-popover-foreground hover:bg-accent">hi</li>
  </ul>
);' > /dev/null
assert_passes "shadcn-accent: intentional waives a *-foreground parent-pair violation"
reset_tmpdir

# ─── Test 41: bg-primary/80 opacity counts as a paired surface ───────────
# Mirrors `packages/web/src/components/ui/badge.tsx` line 11:
# 'border-transparent bg-primary text-primary-foreground hover:bg-primary/80'.
# The `/80` opacity modifier doesn't change whether the surface is
# painted, so the foreground pair should pass.
create_test_file "BadgeOpacity.tsx" \
    "export const Ok = () => <span className=\"border-transparent bg-primary text-primary-foreground hover:bg-primary/80\">hi</span>;" > /dev/null
assert_passes "bg-primary text-primary-foreground hover:bg-primary/80 (opacity) passes"
reset_tmpdir

# ─── Test 42: Self-match on ci.yml step name ─────────────────────────────
# Mirrors the #2541/#2542 self-match guard used by every string-forbidding
# check. If a ci.yml step name quotes the forbidden pattern, the guard
# matches itself on first CI run — describe what the check does, not what
# it forbids.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-bare-shadcn-accent.sh" "yml"

# ─── Test 43: Self-match on guard's own source files (#4225) ─────────────
# The guard now exists across two files (the bash entrypoint and the
# Python helper) that both contain the forbidden token names as literal
# strings inside regex patterns and docstrings. Running the guard
# against itself should pass - the source files live under scripts/, not
# packages/web/src/, so they're outside the default scan scope; but as
# a defensive assertion we copy them into the tmpdir as `.tsx` and
# verify the guard handles its own source verbatim.
TESTS=$((TESTS + 1))
mkdir -p "$TMPDIR_TEST"
# The guard scans .ts/.tsx; rename to .tsx so it's picked up.
cp "$SCRIPT_DIR/check-bare-shadcn-accent.sh" "$TMPDIR_TEST/guard_source.tsx"
cp "$SCRIPT_DIR/_check_bare_shadcn_strip_pairs.py" "$TMPDIR_TEST/guard_helper_source.tsx"
if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
    echo "PASS: Guard does not flag its own source files (#4225 self-match)"
else
    echo "FAIL: Guard flags its own source files (#4225 self-match — see #2541/#2542)"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
