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

# ─── Test 21: Self-match on ci.yml step name ─────────────────────────────
# Mirrors the #2541/#2542 self-match guard used by every string-forbidding
# check. If a ci.yml step name quotes the forbidden pattern, the guard
# matches itself on first CI run — describe what the check does, not what
# it forbids.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-bare-shadcn-accent.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
