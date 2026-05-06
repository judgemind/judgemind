#!/usr/bin/env python3
# venv: none
# permanent: true
"""
Implementation core for `scripts/check-bare-shadcn-accent.sh`.

Scans `*.tsx` / `*.ts` files under a target directory for bare shadcn
surface or foreground Tailwind tokens that produce invisible chrome on
default surfaces. The bash entrypoint
(`scripts/check-bare-shadcn-accent.sh`) is a thin wrapper that
invokes this module — the actual matching, allowlisting, and pair
detection happens here so the per-line conditional logic is expressible
without a fragile multi-pass sed.

This script is invoked by `scripts/check-bare-shadcn-accent.sh`. Direct
invocation is supported for the peer test only:

    python3 scripts/_check_bare_shadcn_strip_pairs.py <dir>

Behaviour
---------
The detailed algorithm (allowlist marker, modifier-prefix masking,
surface-pair detection, body-color allowlisting) is documented in the
header comment of `scripts/check-bare-shadcn-accent.sh`. This module
mirrors that behaviour and is the source of truth — keep the two in
sync when extending either.

Exit codes:
    0 — No violations found.
    1 — One or more violations found (printed to stdout).

History
-------
    - #4225 — moved the entire per-line scan into Python so the
      "if line contains surface-X, mask foreground-X" conditional
      can be expressed cleanly. The previous bash + sed pipeline was
      too slow when invoked once per line and could not express
      pair-detection without an external helper.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ───────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────

# X-class tokens with paired surface + foreground variants.
PAIRED_TOKENS: tuple[str, ...] = (
    "primary",
    "secondary",
    "card",
    "popover",
    "destructive",
    "accent",
)

# Allowlist marker — name preserved from #2832 so existing waivers in
# Sidebar.tsx and JudgeProfile.tsx continue to work after the #4225
# expansion.
ALLOWLIST_MARKER = "shadcn-accent: intentional"

# Skip pure-comment lines so allowlist-adjacent comment paragraphs that
# happen to mention forbidden tokens (e.g. the warning paragraph in a
# JSDoc block) don't trip the guard.
#
# Recognised comment styles:
#   - `//`   - TSX/TS line comment.
#   - `*`    - JSDoc continuation line.
#   - `/*`   - TSX/TS block comment opener.
#   - `#`    - Python/bash line comment. Included so the guard, when
#              run defensively against its own (non-TSX) source files
#              renamed as .tsx, does not flag forbidden tokens that
#              appear in source-file docstrings (#4225 self-match).
#              TSX legitimately starts a line with `#` only for private
#              class fields, which never carry a token name.
COMMENT_LINE_RE = re.compile(r"^\s*(//|\*|/\*|#)")

# Modifier-prefixed token usages are legitimate shadcn idioms
# (`hover:bg-accent`, `data-[selected=true]:text-accent-foreground`,
# `dark:hover:bg-primary text-primary-foreground`, etc.). The prefix
# is any character run that does not contain whitespace, quote, colon,
# or backtick, followed by `:`.
_MODIFIER_PREFIX = r"[^\s\"'`:]+:"

MODIFIER_PREFIXED_RE = re.compile(
    rf"{_MODIFIER_PREFIX}(text|bg|border|ring)-(?:{'|'.join(PAIRED_TOKENS)})(?:-foreground)?\b"
)

# `text-muted-foreground` is the legitimate body-color idiom (Stone 500
# in `docs/BRAND.md` §Color Palette, 170+ usages). It is allowlisted
# unconditionally.
MUTED_FOREGROUND_RE = re.compile(r"\btext-muted-foreground\b")

# Surface tokens (NOT followed by `-foreground`). The trailing
# negative lookahead allows opacity modifiers like `bg-primary/80` to
# count as a surface pair (the `80` opacity suffix doesn't change
# whether the surface is painted).
def _surface_re(x: str) -> re.Pattern[str]:
    return re.compile(rf"\b(bg|text|border|ring)-{re.escape(x)}(?![a-zA-Z0-9_-])")


# Foreground tokens, the form we want to mask when paired.
def _foreground_re(x: str) -> re.Pattern[str]:
    return re.compile(rf"\b(text|bg|border|ring)-{re.escape(x)}-foreground\b")


SURFACE_RES = {x: _surface_re(x) for x in PAIRED_TOKENS}
FOREGROUND_RES = {x: _foreground_re(x) for x in PAIRED_TOKENS}

# Bare-bad-token detector run on the masked line. Three groups:
#
#   1. The `(text|bg|border|ring)` x `accent` form (the original #2832
#      case).
#   2. `(text|bg|border|ring)-X-foreground` for X in PAIRED_TOKENS
#      (the #4225 generalisation).
#   3. The `text` x `background` and `(bg|border|ring)` x `foreground`
#      symmetric typo case from #4208.
#
# Trailing `($|[^a-zA-Z0-9_-])` ensures we don't false-match on
# longer identifiers like `text-accentish` (none exist today, but
# defensive).
#
# The regex is assembled from token-name fragments rather than literal
# joined strings so the script's own source does not self-match when
# scanned (#2541/#2542/#4225 self-match defense). Treating the token
# names as data keeps the source file regex-clean while preserving the
# user-facing regex semantics.
_BG = "background"
_FG = "foreground"
BAD_TOKEN_RE = re.compile(
    r"(?:"
    r"(?:text|bg|border|ring)-accent"
    r"|"
    rf"(?:text|bg|border|ring)-(?:{'|'.join(PAIRED_TOKENS)})-{_FG}"
    r"|"
    rf"text-{_BG}"
    r"|"
    rf"(?:bg|border|ring)-{_FG}"
    r")(?:$|[^a-zA-Z0-9_-])"
)


# ───────────────────────────────────────────────────────────────────────
# Core algorithm
# ───────────────────────────────────────────────────────────────────────


def mask_allowed(line: str) -> str:
    """Mask all legitimate token uses so the surviving line can be scanned."""
    masked = line

    # 1. Modifier-prefixed forms are always legitimate.
    masked = MODIFIER_PREFIXED_RE.sub("__MASKED_MODIFIER__", masked)

    # Re-run a couple of times to catch modifier-prefixed forms whose
    # prefix happens to contain a previously-stripped token. In
    # practice one pass is enough — chained `dark:hover:` is a single
    # `[^...]+:` match — but two passes cost nothing.
    masked = MODIFIER_PREFIXED_RE.sub("__MASKED_MODIFIER__", masked)

    # 2. `text-muted-foreground` is allowlisted unconditionally.
    masked = MUTED_FOREGROUND_RE.sub("__MASKED_MUTED__", masked)

    # 3. `(text|bg|border|ring)-accent-foreground` alone is fine —
    #    it's the foreground partner of the shadcn hover surface
    #    and shows up in `text-accent-foreground` test assertions etc.
    masked = re.sub(
        r"\b(text|bg|border|ring)-accent-foreground\b",
        "__MASKED_ACCENT_FG__",
        masked,
    )

    # 4. Surface-pair strip: for each X, if the (post-modifier-strip)
    #    line contains `(bg|text|border|ring)-X` (NOT followed by
    #    `-foreground`), mask the corresponding `*-X-foreground` on
    #    the same line — that is the legitimate paired idiom.
    for x in PAIRED_TOKENS:
        if SURFACE_RES[x].search(masked):
            masked = FOREGROUND_RES[x].sub("__MASKED_PAIRED_FG__", masked)

    return masked


def scan_line(line: str) -> bool:
    """Return True if the line contains a bare bad token after masking."""
    masked = mask_allowed(line)
    return bool(BAD_TOKEN_RE.search(masked))


def is_comment_line(line: str) -> bool:
    return bool(COMMENT_LINE_RE.match(line))


def find_files(target: Path) -> list[Path]:
    """Find all .tsx/.ts files under target, excluding tests."""
    if not target.is_dir():
        return []
    files: list[Path] = []
    for root, dirnames, filenames in os.walk(target):
        # Exclude any __tests__ subdirectories.
        dirnames[:] = [d for d in dirnames if d != "__tests__"]
        for name in filenames:
            if name.endswith((".test.tsx", ".test.ts")):
                continue
            if name.endswith((".tsx", ".ts")):
                files.append(Path(root) / name)
    files.sort()
    return files


def scan_file(path: Path, repo_root: Path) -> list[tuple[str, int, str]]:
    """Scan a file, returning a list of (rel_path, line_no, line) violations."""
    violations: list[tuple[str, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations

    # Sliding window of the previous 3 lines so the `shadcn-accent:
    # intentional` allowlist marker can be picked up on the same line
    # OR up to 3 lines above.
    prev3 = ""
    prev2 = ""
    prev1 = ""

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line

        # Skip comment lines, but rotate them into the window so the
        # allowlist marker on a `// ...` comment line carries forward
        # to the next non-comment line.
        if is_comment_line(line):
            prev3, prev2, prev1 = prev2, prev1, line
            continue

        if scan_line(line):
            # Allowlist check — same-line, prev1, prev2, or prev3.
            if (
                ALLOWLIST_MARKER in line
                or ALLOWLIST_MARKER in prev1
                or ALLOWLIST_MARKER in prev2
                or ALLOWLIST_MARKER in prev3
            ):
                prev3, prev2, prev1 = prev2, prev1, line
                continue

            try:
                rel_path = str(path.relative_to(repo_root))
            except ValueError:
                rel_path = str(path)
            violations.append((rel_path, line_no, line))

        prev3, prev2, prev1 = prev2, prev1, line

    return violations


# ───────────────────────────────────────────────────────────────────────
# Output
# ───────────────────────────────────────────────────────────────────────

# The user-facing help text mentions the forbidden tokens. Build it
# from fragment concatenation so this source file does not self-match
# when scanned (#2541/#2542/#4225 self-match defense). The fragments
# rejoin to plain English at runtime.
_T = "text-"
_BGP = "bg-"
ERROR_HEADER = f"""\
ERROR: Bare shadcn token under packages/web/src/.

  Bare shadcn surface or foreground tokens produce invisible chrome on
  default surfaces:

    {_T}accent  -> shadcn near-gray hover surface, not brand amber.
                    Use text-brand-accent dark:text-brand-accent-light.

    {_T}X-{_FG} (X = primary, secondary, card, popover,
                          destructive, accent)
                 -> designed to sit on bg-X. Bare on a default surface
                    it inverts to invisible. Pair with bg-X / text-X /
                    border-X / ring-X on the same element, or use a
                    modifier such as hover: / focus: / data-[...]:.

    {_T}{_BG} / {_BGP}{_FG} -> swapped tokens. Both paint
                    same-color-on-same-color (invisible).

  Allowlists:
    text-muted-{_FG} is allowed (legitimate body color).
    Modifier-prefixed forms are allowed (hover:, focus:, etc.).

  If this is a legitimate selected-row surface (sidebar active nav,
  filter pill, etc.) or a deliberate parent-paired className, add an
  inline allowlist comment on the preceding or same line:
    {{/* shadcn-accent: intentional - selected-row chrome */}}

  See issues #2816, #2832, #4208, #4225 and
  packages/web/src/components/Wordmark.tsx.

  Violations:
"""

ERROR_FOOTER = """\

  Reference: https://github.com/judgemind/judgemind/issues/4225"""


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(f"Usage: {argv[0]} [target-dir]", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    default_target = repo_root / "packages" / "web" / "src"
    target = Path(argv[1]).resolve() if len(argv) == 2 else default_target

    if not target.is_dir():
        print(f"All clean - target directory not found: {target}")
        return 0

    files = find_files(target)
    if not files:
        print(f"All clean - no .tsx/.ts files under {target}")
        return 0

    all_violations: list[tuple[str, int, str]] = []
    for file in files:
        all_violations.extend(scan_file(file, repo_root))

    if not all_violations:
        print("All clean - no bare shadcn tokens under packages/web/src/.")
        return 0

    print(ERROR_HEADER)
    for rel_path, line_no, line in all_violations:
        print(f"    {rel_path}:{line_no}:{line}")
    print()
    print(f"  Found {len(all_violations)} occurrence(s) of bare shadcn tokens.")
    print(ERROR_FOOTER)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
