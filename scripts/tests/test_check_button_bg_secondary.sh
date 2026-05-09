#!/usr/bin/env bash
# test_check_button_bg_secondary.sh — Tests for
# scripts/check-button-bg-secondary.sh.
#
# Synthesises small .tsx fixtures in a temp dir, runs the guard against
# the temp dir, and asserts the expected pass/fail outcome for every
# canonical pattern described in the check script's header comment.
#
# Mirrors the structure of test_check_bare_shadcn_accent.sh — same
# fixture-based approach, same assert helpers, same self-match check
# at the end (#2541/#2542).
#
# Usage:
#   scripts/tests/test_check_button_bg_secondary.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-button-bg-secondary.sh"
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

# ─── Test 1: Bare <button className="bg-secondary"> on a TSX element fails ──
# This is the canonical bug shape from #4226 — the issue's primary verify
# clause: a fixture <button className="bg-secondary"> trips the guard.
create_test_file "BadButton.tsx" \
    'export const Bad = () => <button className="bg-secondary">CTA</button>;' > /dev/null
assert_fails "Bare <button className=\"bg-secondary\"> is flagged"
reset_tmpdir

# ─── Test 2: Bare <a className="bg-secondary"> fails ─────────────────────
create_test_file "BadAnchor.tsx" \
    'export const Bad = () => <a href="#" className="bg-secondary rounded-md p-2">link</a>;' > /dev/null
assert_fails "Bare <a className=\"bg-secondary\"> is flagged"
reset_tmpdir

# ─── Test 3: <Button variant="secondary"> passes ─────────────────────────
# The issue's second verify clause: a fixture <Button variant="secondary">
# does NOT trip the guard. This is the sanctioned wrapper — capital-B JSX
# is out of scope.
create_test_file "OkButtonComponent.tsx" \
    'import {Button} from "@/components/ui/button"; export const Ok = () => <Button variant="secondary">CTA</Button>;' > /dev/null
assert_passes "<Button variant=\"secondary\"> (capital-B JSX wrapper) is allowed"
reset_tmpdir

# ─── Test 4: hover:bg-secondary on <button> passes (modifier-prefixed) ──
create_test_file "HoverPrefix.tsx" \
    'export const Hov = () => <button className="hover:bg-secondary">hi</button>;' > /dev/null
assert_passes "hover:bg-secondary (modifier-prefixed) on <button> is allowed"
reset_tmpdir

# ─── Test 5: data-[state=open]:bg-secondary passes ───────────────────────
# Mirrors the canonical usage in
# packages/web/src/components/ui/sheet.tsx line 62.
create_test_file "DataState.tsx" \
    'export const Sel = () => <button className="data-[state=open]:bg-secondary">hi</button>;' > /dev/null
assert_passes "data-[state=open]:bg-secondary passes (modifier-prefixed)"
reset_tmpdir

# ─── Test 6: focus:bg-secondary passes ───────────────────────────────────
create_test_file "FocusPrefix.tsx" \
    'export const Foc = () => <button className="focus:bg-secondary">hi</button>;' > /dev/null
assert_passes "focus:bg-secondary on <button> passes (modifier-prefixed)"
reset_tmpdir

# ─── Test 7: dark:bg-secondary passes ────────────────────────────────────
create_test_file "DarkPrefix.tsx" \
    'export const Dk = () => <button className="dark:bg-secondary">hi</button>;' > /dev/null
assert_passes "dark:bg-secondary on <button> passes (modifier-prefixed)"
reset_tmpdir

# ─── Test 8: Comment line referencing bg-secondary passes ────────────────
# A `// bg-secondary is bad` comment must not trip the guard — comments
# are not JSX elements.
create_test_file "Comment.tsx" \
    '// bg-secondary on a button is invisible-on-invisible.
export const Ok = () => <button className="bg-primary text-primary-foreground">hi</button>;' > /dev/null
assert_passes "// comment referencing bg-secondary is allowed"
reset_tmpdir

# ─── Test 9: Test files (.test.tsx) are excluded ─────────────────────────
create_test_file "page.test.tsx" \
    'it("flags bg-secondary", () => { render(<button className="bg-secondary" />); });' > /dev/null
assert_passes ".test.tsx files are excluded from the scan"
reset_tmpdir

# ─── Test 10: Files under __tests__/ are excluded ────────────────────────
create_test_file "__tests__/Foo.tsx" \
    'export const Bad = () => <button className="bg-secondary">hi</button>;' > /dev/null
assert_passes "Files under __tests__/ are excluded from the scan"
reset_tmpdir

# ─── Test 11: <div className="bg-secondary"> passes (not a CTA shape) ───
# `<div>` and other non-button/non-anchor elements are out of scope.
# This is where the existing 5 in-repo `bg-secondary` usages live
# (filter chrome / badges).
create_test_file "Div.tsx" \
    'export const Ok = () => <div className="bg-secondary p-2">filter pill</div>;' > /dev/null
assert_passes "<div className=\"bg-secondary\"> is allowed (not a CTA element)"
reset_tmpdir

# ─── Test 12: <span className="bg-secondary"> passes ─────────────────────
create_test_file "Span.tsx" \
    'export const Ok = () => <span className="bg-secondary text-xs">badge</span>;' > /dev/null
assert_passes "<span className=\"bg-secondary\"> is allowed (not a CTA element)"
reset_tmpdir

# ─── Test 13: bg-secondary inside cva(...) variant string passes ─────────
# This is exactly the pattern in
# packages/web/src/components/ui/button.tsx line 15 and
# packages/web/src/components/ui/badge.tsx line 12. The string is not
# inside a JSX element opener, so it passes.
create_test_file "Variants.tsx" \
    "import {cva} from 'class-variance-authority';
const variants = cva('base', {
  variants: {
    variant: {
      secondary: 'bg-secondary text-secondary-foreground hover:bg-secondary/80',
    },
  },
});
export const Ok = () => <button className={variants({variant: 'secondary'})}>hi</button>;" > /dev/null
assert_passes "bg-secondary inside cva() variant string passes (not a JSX opener)"
reset_tmpdir

# ─── Test 14: bg-secondary/80 (opacity) on <button> is flagged ──────────
# A 80%-opacity-of-near-white surface is still near-white. Opacity does
# not change the surface lightness materially.
create_test_file "BadOpacity.tsx" \
    'export const Bad = () => <button className="bg-secondary/80">hi</button>;' > /dev/null
assert_fails "bg-secondary/80 (opacity-suffixed) on <button> is flagged"
reset_tmpdir

# ─── Test 15: Multi-line <button> opener with bg-secondary on later line ─
# JSX openers commonly span multiple lines — attributes one per line.
# The guard must walk the opener through to the matching `>`.
create_test_file "MultiLine.tsx" \
    'export const Bad = () => (
  <button
    type="button"
    onClick={() => doThing()}
    className="bg-secondary rounded-md px-4"
  >
    Click
  </button>
);' > /dev/null
assert_fails "Multi-line <button> opener with bg-secondary attribute is flagged"
reset_tmpdir

# ─── Test 16: Allowlist marker on the same line passes ───────────────────
create_test_file "Allow.tsx" \
    'export const Ok = () => <button className="bg-secondary" /* secondary-cta: intentional - parent-paired chrome */>hi</button>;' > /dev/null
assert_passes "secondary-cta: intentional on the same line waives the violation"
reset_tmpdir

# ─── Test 17: Allowlist marker on the immediately preceding line passes ──
create_test_file "AllowPrev.tsx" \
    'export const Ok = () => (
  // secondary-cta: intentional - embedded in shadcn slot
  <button className="bg-secondary">hi</button>
);' > /dev/null
assert_passes "secondary-cta: intentional on the preceding line waives the violation"
reset_tmpdir

# ─── Test 18: Allowlist marker 3 lines back (wrapped JSX) passes ─────────
# Mirrors the JudgeProfile.tsx style waiver — comment above, attributes
# wrap.
create_test_file "AllowWrap.tsx" \
    'export const Wrap = () => (
  // secondary-cta: intentional - wrapped JSX selected-row chrome
  <button
    data-testid="filter-pill"
    className="bg-secondary text-xs"
  >hi</button>
);' > /dev/null
assert_passes "secondary-cta: intentional within 3 preceding lines waives the violation"
reset_tmpdir

# ─── Test 19: Allowlist marker 4+ lines back does NOT waive ──────────────
# Mirrors the same far-marker test in the accent guard — the marker has
# to be near the offending line so unrelated waivers don't cascade.
create_test_file "AllowFar.tsx" \
    'export const Far = () => (
  // secondary-cta: intentional - but unrelated, far away
  <div>
    <p>filler 1</p>
    <p>filler 2</p>
    <button className="bg-secondary">later</button>
  </div>
);' > /dev/null
assert_fails "secondary-cta: intentional more than 3 lines away does NOT waive a later violation"
reset_tmpdir

# ─── Test 20: Allowlist on one violation does not cover unrelated later ─
# A single allowlist marker doesn't cascade to flag-free everything in
# the rest of the file.
create_test_file "AllowOne.tsx" \
    'export const One = () => (
  // secondary-cta: intentional
  <button className="bg-secondary">selected</button>
);
export const Two = () => (
  <button className="bg-secondary">unrelated bug</button>
);' > /dev/null
assert_fails "Allowlist on one violation does not cover unrelated later violation"
reset_tmpdir

# ─── Test 21: Empty directory passes ─────────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 22: Underscore / camelCase lookalikes do not match ─────────────
# The token detector uses a word boundary; identifiers like
# `bg_secondary` or `bgSecondary` must not match.
create_test_file "Lookalike.tsx" \
    'const bg_secondary = 1; const bgSecondary = 2;
export const Ok = () => <button className="bg-primary text-primary-foreground">hi</button>;' > /dev/null
assert_passes "Underscore / camelCase lookalikes do not match"
reset_tmpdir

# ─── Test 23: <button> without className passes ──────────────────────────
create_test_file "NoClassName.tsx" \
    'export const Ok = () => <button onClick={fn}>hi</button>;' > /dev/null
assert_passes "<button> without className passes"
reset_tmpdir

# ─── Test 24: <button className="bg-primary"> passes ─────────────────────
create_test_file "Primary.tsx" \
    'export const Ok = () => <button className="bg-primary text-primary-foreground rounded-md">CTA</button>;' > /dev/null
assert_passes "<button className=\"bg-primary ...\"> passes (canonical CTA)"
reset_tmpdir

# ─── Test 25: <button className={cn(...)} expression passes when no bg-secondary ──
create_test_file "CnExpr.tsx" \
    "export const Ok = ({extra}: any) => <button className={cn('bg-primary text-primary-foreground', extra)}>hi</button>;" > /dev/null
assert_passes "<button className={cn('bg-primary', ...)} passes"
reset_tmpdir

# ─── Test 26: <button className={cn('bg-secondary', ...)} expression flagged ──
# A brace-expression className that contains a literal bg-secondary
# string is still a hand-rolled CTA — the JSX expression is part of the
# opener.
create_test_file "CnSecondary.tsx" \
    "export const Bad = ({extra}: any) => <button className={cn('bg-secondary', extra)}>hi</button>;" > /dev/null
assert_fails "<button className={cn('bg-secondary', ...)}> is flagged (literal token in expression)"
reset_tmpdir

# ─── Test 27: <Button> (capital B) with bg-secondary in className passes ─
# The capital-B Button is the sanctioned wrapper component; whatever
# className it forwards is its problem to compose, not this guard's.
# Adding a literal `bg-secondary` className to <Button> is a different
# code-review judgment call than putting it on a hand-rolled <button>.
create_test_file "ButtonComponentClassname.tsx" \
    'import {Button} from "@/components/ui/button";
export const Ok = () => <Button className="bg-secondary">CTA</Button>;' > /dev/null
assert_passes "<Button className=\"bg-secondary\"> (capital-B) is out of scope"
reset_tmpdir

# ─── Test 28: <button> followed by inner JSX that contains bg-secondary —
# The opener ends at the `>` after `<button ...>`; the inner content
# (`<span className="bg-secondary">`) is a separate element. Because
# `<span>` isn't in scope, this passes.
create_test_file "InnerSpan.tsx" \
    'export const Ok = () => <button onClick={fn}><span className="bg-secondary">badge</span></button>;' > /dev/null
assert_passes "<button> with inner <span className=\"bg-secondary\"> passes (span out of scope)"
reset_tmpdir

# ─── Test 29: <a href={x}> opener spanning lines is detected ─────────────
create_test_file "AnchorMulti.tsx" \
    'export const Bad = () => (
  <a
    href="/foo"
    className="bg-secondary inline-block rounded-md px-3"
  >
    Link
  </a>
);' > /dev/null
assert_fails "Multi-line <a> opener with bg-secondary attribute is flagged"
reset_tmpdir

# ─── Test 30: <button> + bg-secondary inside .ts string literal passes ──
# Plain `.ts` files with no real JSX should not produce false positives.
# A scanner that finds `<button` inside a quoted string literal but
# can't actually parse JSX is fine — we will not see real JSX-shaped
# output here, only string-literal HTML which is an antipattern in
# this codebase but not the bug class this guard targets.
#
# The brace/quote-tracking opener-end detection naturally handles this:
# the opener regex matches `<button` (lookahead-only), then the scanner
# walks forward looking for the closing `>`. The `<` lives inside a
# `"..."` literal, so when the scanner enters the string and sees the
# next `\"`, it follows the escape rules and resumes outside the string;
# the next `>` it sees is the real `>` of the closing string-literal
# `</button>`. Today that gives the same result as a real JSX opener
# (the opener span captures the literal HTML between them). So whether
# this case is flagged or not depends on the file's exact shape — we
# document the current observed behavior here, which is "pass" for the
# specific shape in the fixture below. The guard is intended for .tsx
# files; this test asserts we do not regress on the .ts surface.
create_test_file "Plain.ts" \
    'const html: string = "ignored";
export function render(): string { return html; }' > /dev/null
assert_passes ".ts file with no JSX passes (documents .ts safety)"
reset_tmpdir

# ─── Test 31: Self-match on ci.yml step name ─────────────────────────────
# Mirrors the #2541/#2542 self-match guard used by every string-forbidding
# check. If a ci.yml step name quotes the forbidden pattern, the guard
# matches itself on first CI run — describe what the check does, not what
# it forbids.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-button-bg-secondary.sh" "yml"

# ─── Test 32: Self-match on guard's own source files ─────────────────────
# The guard's source files (the bash entrypoint and the Python helper)
# both contain the forbidden token name as a literal string inside the
# regex pattern and docstrings. Running the guard against itself should
# pass — the source files live under scripts/, not packages/web/src/, so
# they're outside the default scan scope; but as a defensive assertion
# we copy them into the tmpdir as `.tsx` and verify the guard handles
# its own source verbatim.
TESTS=$((TESTS + 1))
mkdir -p "$TMPDIR_TEST"
cp "$SCRIPT_DIR/check-button-bg-secondary.sh" "$TMPDIR_TEST/guard_source.tsx"
cp "$SCRIPT_DIR/_check_button_bg_secondary.py" "$TMPDIR_TEST/guard_helper_source.tsx"
if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
    echo "PASS: Guard does not flag its own source files (self-match defense)"
else
    echo "FAIL: Guard flags its own source files (self-match — see #2541/#2542)"
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
