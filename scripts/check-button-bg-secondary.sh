#!/usr/bin/env bash
# check-button-bg-secondary.sh - Guard against hand-rolled <button> /
# <a> JSX elements that use the shadcn `bg-secondary` token.
#
# Bash entrypoint - delegates the actual scan to
# `scripts/_check_button_bg_secondary.py`. The Python helper handles
# JSX-element extraction (multi-line opener, brace/quote depth),
# modifier-prefix masking, and the 3-line allowlist lookback.
#
# WHY THIS BUG CLASS IS SEVERE
# ----------------------------
# The shadcn `secondary` and `primary` tokens resolve to opposite ends
# of the lightness scale (`33 5% 15%` vs `30 11% 96%` in light mode -
# see `packages/web/src/app/globals.css` and `docs/BRAND.md`
# §Tailwind Token Mapping). The two class names are autocomplete-
# adjacent. On a CTA-shaped `<button>` / `<a>` element a hand-rolled
# `bg-secondary` paints near-invisible near-white-on-near-white chrome
# in light mode - the same severity as the #2816 `text-accent` regression
# that prompted the companion `scripts/check-bare-shadcn-accent.sh` guard.
#
# The codebase routes nearly all CTAs through `<Button variant="default">`
# (which hardcodes `bg-primary` in `packages/web/src/components/ui/button.tsx`).
# This guard ensures a hand-rolled bare `<button>` / `<a>` cannot regress
# silently - reviewer judgment ("why not <Button>?") is reinforced by CI.
#
# Scope (chosen heuristic from #4226)
# -----------------------------------
# The simpler heuristic from the issue body: any `bg-secondary` (with
# optional opacity modifier `/80`) appearing inside a `<button>` or
# `<a>` JSX element opener, regardless of paired `text-*` token. There
# are zero such usages today - the 5 current `bg-secondary` references
# are filter chrome / badges (`packages/web/src/components/ui/sheet.tsx`
# line 62 - modifier-prefixed `data-[state=open]:bg-secondary`), the
# Button / Badge component variants (inside `cva(...)` strings, not
# JSX element openers), and the BRAND.md documentation example. The
# guard ships with zero violations.
#
# Detected JSX shapes
# -------------------
#   - `<button ...>` / `<button>` - lowercase HTML tag.
#   - `<a ...>` / `<a>` - lowercase HTML tag.
#   - `<Button ...>` (capital-B JSX component) is intentionally NOT
#     scanned - that's the sanctioned wrapper. The component itself
#     contains `bg-secondary` inside a `cva` variant string in
#     `button.tsx`, and that scan target is not a JSX element opener.
#
# Allowlist mechanism
# -------------------
# To allowlist a specific element, place an inline opt-out comment on
# the same line OR within the three preceding lines. The opt-out token
# is the literal string `secondary-cta: intentional`. It appears in
# the diff and code review, so reviewers see exactly which lines were
# waived and why.
#
# The marker is namespaced separately from the accent guard's
# `shadcn-accent: intentional` so a reviewer scanning waivers can tell
# the two guards apart at a glance.
#
# Usage
# -----
#   scripts/check-button-bg-secondary.sh          # scan packages/web/src
#   scripts/check-button-bg-secondary.sh [dir]    # scan custom dir
#
# Exit codes
# ----------
#   0 - No violations.
#   1 - One or more files have a hand-rolled bare-element CTA without
#       an allowlist comment.
#
# History
# -------
#   - #4226 - initial guard, narrow scope (button/a JSX openers only).
#     Companion to #2832's `check-bare-shadcn-accent.sh` (different
#     token family, different scope).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_PY="$SCRIPT_DIR/_check_button_bg_secondary.py"

if [[ ! -f "$HELPER_PY" ]]; then
    echo "ERROR: helper missing at $HELPER_PY" >&2
    exit 2
fi

exec python3 "$HELPER_PY" "$@"
