#!/usr/bin/env bash
# check-bare-shadcn-accent.sh - Repo-wide guard for the shadcn token-pair
# footgun family.
#
# Bash entrypoint - delegates the actual scan to
# `scripts/_check_bare_shadcn_strip_pairs.py`. The script name is
# preserved from #2832 so the existing CI step, peer test, and four
# in-repo waivers in
# `packages/web/src/components/layout/Sidebar.tsx` and
# `packages/web/src/app/(main)/judges/[id]/JudgeProfile.tsx` continue
# to work after the #4225 expansion.
#
# WHY THIS BUG CLASS IS SEVERE
# ----------------------------
# The shadcn `accent` token (mapped via `hsl(var(--accent))` in
# packages/web/tailwind.config.ts) is a near-gray *hover surface* - not
# the brand amber. The brand amber lives on the separate `brand-accent`
# token (see `docs/BRAND.md` Tailwind Token Mapping and the canonical
# usage in `packages/web/src/components/Wordmark.tsx`). The two classes
# are one character apart, which is a persistent footgun: PR #2811
# shipped `text-accent` on issue/PR links on `/admin/dispatcher` and
# they rendered as nearly-invisible gray on both light and dark mode
# (#2816).
#
# The same shape applies repo-wide for two other one-word typos
# (audited in #4208 / #4225):
#
#   1. The `*-foreground` family without a paired surface.
#      `text-primary-foreground` is designed to sit on `bg-primary`.
#      Bare on a default-cascade surface it inverts: white-on-white in
#      light mode, near-black-on-near-black in dark mode.
#
#   2. `text-background` and `bg-foreground`. Single-word swap, same
#      length, autocomplete-adjacent. Today there are 0 usages, so the
#      bug hasn't shipped yet - but the next hand-rolled element is
#      one autocomplete away.
#
# Allowlist mechanism (preserved from #2832)
# ------------------------------------------
# To allowlist a specific line, place an inline opt-out comment on the
# same line OR within the three preceding lines. The opt-out token is
# the literal string `shadcn-accent: intentional`. It appears in the
# diff and code review, so reviewers see exactly which lines were
# waived and why.
#
# Usage
# -----
#   scripts/check-bare-shadcn-accent.sh          # scan packages/web/src
#   scripts/check-bare-shadcn-accent.sh [dir]    # scan custom dir
#
# Exit codes
# ----------
#   0 - No violations.
#   1 - One or more files use a bare shadcn token without an allowlist
#       comment.
#
# History
# -------
#   - #2816 introduced the narrow `/admin/dispatcher`-only guard.
#   - #2832 expanded to repo-wide and added the
#     `shadcn-accent: intentional` allowlist mechanism.
#   - #4225 generalised to the `*-foreground` family and the
#     `text-background` / `bg-foreground` symmetric typos. Moved the
#     scan into Python (`_check_bare_shadcn_strip_pairs.py`) so the
#     conditional pair-on-line strip could be expressed without a
#     fragile multi-pass sed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_PY="$SCRIPT_DIR/_check_bare_shadcn_strip_pairs.py"

if [[ ! -f "$HELPER_PY" ]]; then
    echo "ERROR: helper missing at $HELPER_PY" >&2
    exit 2
fi

exec python3 "$HELPER_PY" "$@"
