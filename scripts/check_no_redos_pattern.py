#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_no_redos_pattern.py — AST + regex-prefix scanner for ReDoS-shaped
``re.compile`` patterns.

Driven by ``scripts/check-no-redos-pattern.sh``.  See that wrapper for the
full motivation and CI integration story (issue #4117).

The scanner walks every ``re.compile(...)`` call in each input file and
emits a one-line report of the form ``<path>:<lineno>:<pattern-snippet>``
for each call that matches both:

  1. The pattern string begins with an unanchored lazy quantifier
     (``.+?``, ``.*?``, ``[...]+?``, ``[...]*?``, ``\\S+?``, ``\\W+?``, etc.),
     optionally inside a single wrapping group ``(``, ``(?:``, ``(?P<name>``,
     ``(?=``, or ``(?!``, BEFORE any literal anchor character.
  2. The flags argument contains ``re.IGNORECASE`` (or ``re.I``).

A leading literal anchor — ``^``, ``$``, ``\\A``, ``\\Z``, ``\\b``, ``\\B``,
or any literal alphanumeric / escaped-literal character at the top level
of the pattern — counts as an effective anchor and disqualifies the
match.

Suppression: if the line containing the ``re.compile(`` call (or its
opening line in a multi-line call) has the trailing comment
``# noqa: redos-pattern``, the call is skipped.

Heuristic-level by design — false negatives are acceptable.  See issue
#4117 for the rationale.

Usage
-----

    python3 scripts/check_no_redos_pattern.py [PATH ...]

Each PATH is a ``.py`` file.  The wrapper script discovers the paths.

Exit codes
----------

  0 — Always.  The wrapper turns the printed-violations stream into a
       non-zero exit.  Splitting the responsibility keeps this script's
       output stream the single source of truth for tests and the
       wrapper alike.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


# ─── Regex-string prefix classifier ───────────────────────────────────────
# We classify the *prefix* of the pattern up to the first lazy quantifier
# or the first literal anchor.  If the lazy quantifier wins, the pattern
# is flagged.

# Structures we strip from the very front before classifying:
#   ( (?:  (?P<name>  (?=  (?!  (?P=name)
# The intent: ``re.compile(r"([^\\n]+?)\\s+...")`` should be classified by
# what comes *inside* the wrapping group, since the wrapping group itself
# is just a no-op anchor-wise.
_WRAP_PREFIX_RE = re.compile(
    r"""
    ^
    \(                  # opening paren
    (?:
        \?:             # non-capturing
      | \?P<[^>]+>      # named capture
      | \?=             # lookahead
      | \?!             # neg lookahead
      | \?<=            # lookbehind
      | \?<!            # neg lookbehind
    )?
    """,
    re.VERBOSE,
)

# A "wildcard-with-lazy-quantifier" head — the bad shape we flag.
# Matches:
#   .+?       .*?       [...]+?      [...]*?
#   \\S+?     \\W+?     \\D+?        \\S*?  etc.
# Notes:
#   - The character class ``[...]`` may contain escapes; we use a
#     non-greedy class that disallows nested ``]``.
#   - The wildcard MUST be followed immediately by ``+?`` or ``*?``
#     (or ``{m,n}?`` — also lazy, also bad).
_LAZY_HEAD_RE = re.compile(
    r"""
    ^
    (?:
        \.                              # any-char
      | \[ (?: [^\]\\] | \\. )+ \]      # character class
      | \\ [SsWwDd]                     # escaped wildcard
    )
    (?:
        [+*] \?                         # lazy +? or *?
      | \{ \d+ , \d* \} \?              # lazy {m,n}?
      | \{ , \d+ \} \?                  # lazy {,n}?
    )
    """,
    re.VERBOSE,
)

# A leading literal-anchor head — the safe shape we exempt.
# Anchors:
#   ^   $   \A   \Z   \b   \B
# Plus any literal character (alphanumeric, space, escaped punctuation).
# Specifically NOT exempted: ``.``, ``[``, ``\\S``, ``\\W``, ``\\D`` —
# these are wildcards, not anchors.
_ANCHOR_HEAD_RE = re.compile(
    r"""
    ^
    (?:
        \^                              # start-of-line / start-of-string
      | \$                              # end-of-line / end-of-string
      | \\A                             # start-of-string
      | \\Z                             # end-of-string
      | \\b                             # word boundary
      | \\B                             # non-word boundary
      | \\ [^SsWwDdAZbB]                # escaped literal (e.g. \., \(, \/)
      | [A-Za-z0-9_\- ]                 # literal alphanumeric / space / hyphen
    )
    """,
    re.VERBOSE,
)


def _pattern_is_redos_shape(pattern: str) -> bool:
    """Return True if ``pattern`` starts with a lazy wildcard that is
    not preceded by a literal anchor (after at most one wrapping group)."""

    if not pattern:
        return False

    # Strip at most one outer group prefix so ``([^\n]+?)..`` is
    # classified by ``[^\n]+?`` rather than the bare ``(``.
    m = _WRAP_PREFIX_RE.match(pattern)
    head = pattern[m.end():] if m else pattern

    # If the very first construct is a literal anchor, we are safe —
    # bail out early.
    if _ANCHOR_HEAD_RE.match(head):
        return False

    # Otherwise: is the very first construct a wildcard with a lazy
    # quantifier?  If yes, flag.
    if _LAZY_HEAD_RE.match(head):
        return True

    return False


# ─── re.IGNORECASE flag-arg detection ─────────────────────────────────────
def _flags_have_ignorecase(node: ast.AST | None) -> bool:
    """Return True if ``node`` (the ``flags=`` arg of re.compile) contains
    ``re.IGNORECASE`` or ``re.I``.

    Handles the common shapes:
        re.IGNORECASE
        re.I
        re.IGNORECASE | re.MULTILINE
        re.MULTILINE | re.IGNORECASE
        re.IGNORECASE | re.DOTALL | re.MULTILINE  (any chain of |)
    """
    if node is None:
        return False
    if isinstance(node, ast.Attribute):
        return node.attr in ("IGNORECASE", "I")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flags_have_ignorecase(node.left) or _flags_have_ignorecase(node.right)
    return False


# ─── Pattern-arg literal extraction ───────────────────────────────────────
def _extract_pattern_string(node: ast.AST) -> str | None:
    """Return the string value of a ``re.compile`` first arg if it is a
    literal, else None.  Handles plain ``str``, raw-string-via-Constant,
    and trivial concatenation of string literals (``"a" + "b"``).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string: skip — we cannot statically know the value.
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _extract_pattern_string(node.left)
        right = _extract_pattern_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


# ─── Suppression marker ───────────────────────────────────────────────────
_SUPPRESS_MARKER = "noqa: redos-pattern"


def _is_suppressed(source_lines: list[str], call_node: ast.Call) -> bool:
    """Return True if the call site has a ``# noqa: redos-pattern``
    suppression on the opening line OR on any line spanned by the call."""
    start = call_node.lineno - 1
    end = (call_node.end_lineno or call_node.lineno) - 1
    for idx in range(start, min(end + 1, len(source_lines))):
        if _SUPPRESS_MARKER in source_lines[idx]:
            return True
    return False


# ─── re.compile call detection ────────────────────────────────────────────
def _is_re_compile_call(node: ast.Call) -> bool:
    """Return True if ``node.func`` resolves to ``re.compile``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr == "compile" and isinstance(func.value, ast.Name) and func.value.id == "re":
            return True
    return False


def _flags_arg(node: ast.Call) -> ast.AST | None:
    """Return the flags arg of an re.compile call, whether positional
    (2nd arg) or keyword (``flags=...``).  Returns None if no flags
    were passed."""
    # re.compile(pattern, flags=0) — flags is the 2nd positional arg.
    if len(node.args) >= 2:
        return node.args[1]
    for kw in node.keywords:
        if kw.arg == "flags":
            return kw.value
    return None


# ─── Per-file scan ────────────────────────────────────────────────────────
def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return a list of (lineno, snippet) for every violating re.compile
    call in ``path``."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Not valid Python — skip.  May be a fixture or template.
        return []

    source_lines = source.splitlines()
    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_re_compile_call(node):
            continue
        if not node.args:
            continue

        flags_node = _flags_arg(node)
        if not _flags_have_ignorecase(flags_node):
            continue

        pattern = _extract_pattern_string(node.args[0])
        if pattern is None:
            # Pattern is dynamic (variable, f-string, etc.) — skip.
            continue

        if not _pattern_is_redos_shape(pattern):
            continue

        if _is_suppressed(source_lines, node):
            continue

        # Truncate the pattern in the report to keep output readable.
        snippet = pattern if len(pattern) <= 80 else pattern[:77] + "..."
        violations.append((node.lineno, snippet))

    return violations


def main(argv: list[str]) -> int:
    """Print one ``<path>:<lineno>:<pattern>`` line per violation."""
    paths = [Path(p) for p in argv[1:]]
    for path in paths:
        for lineno, snippet in scan_file(path):
            print(f"{path}:{lineno}:{snippet}")
    # Exit 0 unconditionally — the wrapper script aggregates and exits 1
    # on non-empty output.  See module docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
