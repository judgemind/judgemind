#!/usr/bin/env python3
# venv: none
# permanent: true
"""Implementation core for ``scripts/check-button-bg-secondary.sh``.

Scans ``*.tsx`` / ``*.ts`` files under a target directory for hand-rolled
``<button>`` or ``<a>`` JSX elements whose ``className`` contains the
shadcn ``bg-secondary`` token without a modifier prefix. The bash
entrypoint (``scripts/check-button-bg-secondary.sh``) is a thin wrapper
that invokes this module — the actual JSX-element extraction, modifier
detection, and allowlisting happens here.

Why this guard exists
---------------------
``bg-secondary`` and ``bg-primary`` resolve to opposite ends of the
lightness scale (``33 5% 15%`` vs ``30 11% 96%`` in light mode) — the
two class names are autocomplete-adjacent. On a CTA-shape JSX element
(``<button>`` / ``<a className="...rounded-md...">``) a hand-rolled
``bg-secondary`` paints near-invisible white-on-white in light mode.
The codebase routes nearly all CTAs through ``<Button variant="default">``
(canonical ``bg-primary`` lives inside ``button.tsx``); this guard makes
sure a hand-rolled bare-element CTA cannot regress to the silent
white-on-white failure mode that PR #2811 / issue #2816 caught for
``text-accent``.

Scope (chosen heuristic from the issue body)
--------------------------------------------
The simpler heuristic from #4226: any ``bg-secondary`` (with optional
opacity modifier like ``/80``) appearing inside a ``<button>`` or ``<a>``
JSX element opener, regardless of paired ``text-*`` token. There are
zero such usages today, so the guard ships with zero violations. The
component path through ``packages/web/src/components/ui/button.tsx``
is unaffected — the variant strings inside ``cva`` are not JSX element
openers.

Detected JSX shapes
-------------------
- ``<button ...>`` / ``<button>`` — lowercase HTML tag.
- ``<a ...>`` / ``<a>`` — lowercase HTML tag.
- ``<Button ...>`` (capital-B JSX component) is intentionally NOT scanned
  — that's the sanctioned wrapper.

The opener can span multiple lines (the canonical shadcn pattern indents
attributes one-per-line). The scanner reads the entire opener up to the
first unquoted ``>``; ``className`` values can be a string literal
(``"..."``) or a JSX expression (``{cn('...', ...)}``); the scan looks
for ``bg-secondary`` (word-bounded, without modifier-prefix and without
opacity-suffix being itself part of a modifier) inside the captured
opener text.

Modifier-prefixed forms (``hover:bg-secondary``, ``focus:bg-secondary``,
``data-[...]:bg-secondary``, ``dark:bg-secondary``) are legitimate
state-conditionals and are ignored. Opacity-suffixed forms
(``bg-secondary/80``) without a paired un-suffixed ``bg-secondary`` are
still flagged because the opacity does not change the surface lightness
materially — a 80%-opacity-of-near-white is still near-white.

Allowlist mechanism
-------------------
Inline ``secondary-cta: intentional`` comment on the same line OR within
the three preceding lines waives the violation. The marker name follows
the pattern from #2832's ``shadcn-accent: intentional`` — same 3-line
lookback, different namespace so a reviewer scanning waivers can tell
the two guards apart.

Usage
-----
    python3 scripts/_check_button_bg_secondary.py [target-dir]

Exit codes
----------
- 0 — No violations.
- 1 — One or more files have a hand-rolled bare ``<button>`` / ``<a>``
  with ``bg-secondary`` and no allowlist marker.

History
-------
- #4226 — initial guard, narrow scope (button/a JSX openers only).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

# Allowlist marker — namespaced separately from the accent guard's
# `shadcn-accent: intentional` so reviewers see at a glance which guard
# a waiver applies to.
ALLOWLIST_MARKER = "secondary-cta: intentional"

# Token-name fragments stored as data so this source file does not
# self-match when scanned (#2541/#2542/#4225 self-match defense). The
# fragments rejoin to plain English at runtime.
_BG = "bg-"
_TARGET_TOKEN = "secondary"

# Lowercase JSX HTML tag openers we care about. Capital-letter JSX
# components (``<Button>``, ``<Link>``) are intentionally out of scope
# — those are the sanctioned wrappers. The trailing alternation matches
# whitespace, ``>`` (immediate close), ``/`` (self-close), OR end-of-
# string — the last form catches openers like ``<button\n`` where the
# tag name runs straight into a line break.
ELEMENT_OPENER_RE = re.compile(r"<(button|a)(?=\s|>|/|$)")

# `bg-secondary` token detector run against an opener text after stripping
# modifier-prefixed forms. The trailing `(?![a-zA-Z0-9_])` ensures we
# don't match longer identifiers (``bg-secondaryish`` — none exist
# today, defensive). The leading word boundary ensures we don't match
# a token whose name happens to end in ``bg-secondary`` characters.
#
# Built from fragments to avoid self-match.
BAD_TOKEN_RE = re.compile(rf"\b{_BG}{_TARGET_TOKEN}(?:/[0-9]+)?(?![a-zA-Z0-9_-])")

# Modifier-prefixed token detector — masks legitimate
# ``hover:bg-secondary``, ``focus:bg-secondary``,
# ``data-[state=open]:bg-secondary``, ``dark:bg-secondary``, etc.
# The prefix is any character run not containing whitespace, quote,
# colon, or backtick, followed by ``:``.
MODIFIER_PREFIXED_RE = re.compile(rf"[^\s\"'`:]+:{_BG}{_TARGET_TOKEN}(?:/[0-9]+)?\b")


# ─────────────────────────────────────────────────────────────────────
# Core algorithm
# ─────────────────────────────────────────────────────────────────────


def find_files(target: Path) -> list[Path]:
    """Find all .tsx/.ts files under target, excluding tests."""
    if not target.is_dir():
        return []
    files: list[Path] = []
    for root, dirnames, filenames in os.walk(target):
        # Exclude any __tests__ subdirectories — regression tests can
        # spell `bg-secondary` literally in expectations.
        dirnames[:] = [d for d in dirnames if d != "__tests__"]
        for name in filenames:
            if name.endswith((".test.tsx", ".test.ts")):
                continue
            if name.endswith((".tsx", ".ts")):
                files.append(Path(root) / name)
    files.sort()
    return files


def find_element_openers(
    text: str,
) -> list[tuple[int, int, int, int]]:
    """Return a list of (start_line, start_col, end_line, end_col)
    ranges (1-indexed for line, 0-indexed for col, inclusive on the
    end_col side) for every ``<button`` / ``<a`` opener in ``text``.

    The opener runs from the column containing ``<`` of ``<tag`` up to
    and including the column of the matching ``>`` that closes the
    opening tag. JSX expressions inside attribute values (e.g.
    ``className={cn('...', x)}``) are tracked via brace-depth so an
    inner ``>`` doesn't end the opener prematurely. String literals
    inside attribute values are tracked via quote-pairing so an inner
    ``>`` in a string also doesn't end the opener.
    """
    openers: list[tuple[int, int, int, int]] = []
    lines = text.splitlines()

    line_idx = 0
    # When resuming after a previous opener on the same line, restart
    # the regex search from this column instead of column 0.
    resume_col = 0
    while line_idx < len(lines):
        line = lines[line_idx]
        match = ELEMENT_OPENER_RE.search(line, resume_col)
        if not match:
            line_idx += 1
            resume_col = 0
            continue

        # Found `<button` or `<a` at column match.start(). Scan forward
        # for the matching `>` of the opener, tracking quote and brace
        # state.
        start_line = line_idx + 1  # 1-indexed
        start_col = match.start()
        scan_idx = line_idx
        # Position immediately after the tag name (e.g. after ``<button``).
        col = match.start() + 1 + len(match.group(1))

        brace_depth = 0
        in_squote = False
        in_dquote = False
        in_btick = False
        end_line = None
        end_col = None

        while scan_idx < len(lines):
            current = lines[scan_idx]
            i = col if scan_idx == line_idx else 0
            while i < len(current):
                ch = current[i]
                if in_squote:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == "'":
                        in_squote = False
                elif in_dquote:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == '"':
                        in_dquote = False
                elif in_btick:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == "`":
                        in_btick = False
                else:
                    if ch == "'":
                        in_squote = True
                    elif ch == '"':
                        in_dquote = True
                    elif ch == "`":
                        in_btick = True
                    elif ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        if brace_depth > 0:
                            brace_depth -= 1
                    elif ch == ">" and brace_depth == 0:
                        end_line = scan_idx + 1  # 1-indexed
                        end_col = i
                        break
                i += 1
            if end_line is not None:
                break
            scan_idx += 1

        if end_line is None or end_col is None:
            # Unterminated opener — skip to next line and continue.
            line_idx += 1
            resume_col = 0
            continue

        openers.append((start_line, start_col, end_line, end_col))
        # Continue scanning after the opener. If the closing `>` is on
        # the same line we started on, keep scanning that line for a
        # later opener (e.g. `<button>...<a>` on one line); otherwise
        # advance to the line after end_line.
        if end_line - 1 == line_idx:
            resume_col = end_col + 1
        else:
            line_idx = end_line  # 0-indexed = (end_line - 1) + 1 (next line)
            resume_col = 0
    return openers


def opener_text(
    text: str,
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> str:
    """Slice the opener text out of the full file text.

    The slice runs from ``(start_line, start_col)`` through
    ``(end_line, end_col)`` inclusive on the end side — i.e. the
    closing ``>`` IS included in the returned text.
    """
    lines = text.splitlines()
    if start_line == end_line:
        return lines[start_line - 1][start_col : end_col + 1]
    parts: list[str] = [lines[start_line - 1][start_col:]]
    for idx in range(start_line, end_line - 1):
        parts.append(lines[idx])
    parts.append(lines[end_line - 1][: end_col + 1])
    return "\n".join(parts)


def opener_has_bare_bg_secondary(opener: str) -> bool:
    """Return True if the opener contains an unmodified ``bg-secondary``.

    Modifier-prefixed forms (``hover:bg-secondary``,
    ``data-[...]:bg-secondary``) are masked first.
    """
    masked = MODIFIER_PREFIXED_RE.sub("__MASKED_MODIFIER__", opener)
    return bool(BAD_TOKEN_RE.search(masked))


def opener_is_allowlisted(text: str, start_line: int) -> bool:
    """Return True if an allowlist marker appears on the opener's first
    line OR on any of the three preceding lines."""
    lines = text.splitlines()
    # Same-line check uses the full opener span, but the marker is a
    # one-shot string so checking start_line alone catches the same-line
    # case. The preceding-line check looks back up to 3 lines.
    same_line = lines[start_line - 1] if 0 < start_line <= len(lines) else ""
    if ALLOWLIST_MARKER in same_line:
        return True
    for offset in (1, 2, 3):
        idx = start_line - 1 - offset
        if idx < 0:
            break
        if ALLOWLIST_MARKER in lines[idx]:
            return True
    return False


def scan_file(path: Path, repo_root: Path) -> list[tuple[str, int, str]]:
    """Scan a file, returning a list of (rel_path, line_no, snippet)
    violations."""
    violations: list[tuple[str, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations

    for start_line, start_col, end_line, end_col in find_element_openers(text):
        opener = opener_text(text, start_line, start_col, end_line, end_col)
        if not opener_has_bare_bg_secondary(opener):
            continue
        if opener_is_allowlisted(text, start_line):
            continue
        try:
            rel_path = str(path.relative_to(repo_root))
        except ValueError:
            rel_path = str(path)
        # First line of opener as the snippet — full original line
        # (not the sliced opener) so reviewers see the intended context.
        full_lines = text.splitlines()
        snippet = full_lines[start_line - 1] if start_line - 1 < len(full_lines) else ""
        violations.append((rel_path, start_line, snippet))

    return violations


# ─────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────

# Build the user-facing help text from token-name fragments so this
# source file does not self-match when scanned. The fragments rejoin
# to plain English at runtime.
ERROR_HEADER = f"""\
ERROR: Hand-rolled <button> / <a> with {_BG}{_TARGET_TOKEN} under packages/web/src/.

  {_BG}{_TARGET_TOKEN} resolves to a near-white surface in light mode
  (33 11% 96%) and a near-black surface in dark mode (24 6% 17%) —
  on a CTA-shaped <button> / <a> element this paints invisible
  near-white-on-near-white chrome. The shape matches the #2816 root
  cause but for a different token.

  If you mean a CTA, use <Button variant="default"> from
  packages/web/src/components/ui/button.tsx — it hardcodes bg-primary
  with a guaranteed-visible text-primary-foreground pair.

  If you genuinely need a secondary-action filled state, use
  <Button variant="secondary"> — the variant string lives inside
  button.tsx where the paired text-secondary-foreground is also
  applied automatically.

  Allowlist
  ---------
  Modifier-prefixed forms (hover:, focus:, data-[...]:, dark:) are
  legitimate state-conditionals and are ignored. To waive a deliberate
  hand-rolled CTA bare-element use, add an inline allowlist comment
  on the same line OR within the three preceding lines:

      {{/* {ALLOWLIST_MARKER} - <reason> */}}

  Fix:
    1. Replace the bare element with <Button variant="default"> for a
       CTA, or <Button variant="secondary"> for a true secondary action.
    2. If a hand-rolled element is unavoidable (e.g. embedded inside
       another shadcn primitive's slot), add the allowlist comment
       and document the reason inline.
    3. For a state-conditional surface (selected-row chrome, active
       nav), ensure the token is modifier-prefixed.

  See issues #4226 (this guard), #4208 (Pair 4 audit), #2816 (root
  cause for the sibling text-accent regression), and #2832 (companion
  bare-shadcn-accent guard).

  Violations:
"""

ERROR_FOOTER = """\

  Reference: https://github.com/judgemind/judgemind/issues/4226"""


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
        print(
            f"All clean - no hand-rolled <button>/<a> with bare "
            f"{_BG}{_TARGET_TOKEN} under packages/web/src/."
        )
        return 0

    print(ERROR_HEADER)
    for rel_path, line_no, snippet in all_violations:
        print(f"    {rel_path}:{line_no}:{snippet}")
    print()
    print(
        f"  Found {len(all_violations)} hand-rolled element(s) with "
        f"bare {_BG}{_TARGET_TOKEN}."
    )
    print(ERROR_FOOTER)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
