#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check-per-phase-timeouts.py — Verify that *_TIMEOUT_SECONDS constants
referenced inside phase-loop functions use a per-phase access form or carry
a ``# global-by-design (#NNNN)`` annotation.

Motivation (#3776): The dispatcher uses per-phase timeout tables (e.g.
``STUCK_TIMEOUT_SECONDS_BY_PHASE``, ``CLAUDE_PHASE_TIMEOUT_RALPH_SECONDS``)
so each phase gets the right cap. When a developer adds a new plain
``MY_NEW_TIMEOUT_SECONDS = 300`` constant and references it inside a
phase-loop function (one that dispatches on a ``phase`` argument), every
phase silently gets the same cap — the intent of per-phase tuning is
bypassed without any author-time signal. This check catches that regression
at PR time.

What the check does
───────────────────
1. Walk ``scripts/dispatcher/**/*.py`` and ``scripts/dispatcher/**/*.sh``
   (or a custom directory from argv[1]).
2. For Python files: use ``ast`` to find every ``FunctionDef`` /
   ``AsyncFunctionDef`` whose argument list contains a parameter literally
   named ``phase``. Skip functions whose name ends in ``_for_phase`` or
   ``_by_phase`` — those ARE the per-phase implementation.
3. For Bash files: use a regex + brace-depth state machine to extract
   ``name() { … }`` function bodies. Identify phase-loop functions by:
   (a) first non-comment body line is ``_phase="$1"`` or ``phase="$1"``,
   (b) body contains ``for _phase in`` / ``for phase in``, or
   (c) body contains ``case "$_phase"`` / ``case "$phase"``.
   Skip functions ending in ``_for_phase`` or ``_by_phase``.
4. Inside each phase-loop function body, scan every line for
   ``\\b[A-Z][A-Z0-9_]*_TIMEOUT_SECONDS\\b`` token references.
5. Classify each reference as PASS if any of:
   (1) token name ends in ``_BY_PHASE``;
   (2) token embeds a known phase name (``_PLANNING_``, ``_RALPH_``,
       ``_SUMMARY_``, ``_FIX_CI_``, ``_FIX_CONFLICT_``, ``_VERIFY_``,
       ``_RETRO_``, ``_PUSH_AND_PR_``, ``_OPERATIONAL_``,
       ``_AWAITING_CI_``, ``_AWAITING_DEPLOY_``, ``_CLAIMING_``);
   (3) reference line matches per-phase access form (dict/array subscript
       on ``phase`` / ``_phase``, ``.get(phase``, ``_for_phase(``);
   (4) the reference line, any of the 5 lines immediately above it, or
       the constant's own declaration line in the file contains
       ``# global-by-design (#<number>)``.
   Otherwise FLAG.
6. Print a human-readable report (file:line:snippet) and exit 1 if any
   flags; exit 0 otherwise. Prints a one-line summary on success.

Usage
─────
    scripts/check-per-phase-timeouts.sh                    # default dir
    scripts/check-per-phase-timeouts.sh scripts/dispatcher # explicit dir
    scripts/check-per-phase-timeouts.py --help

Exit codes
──────────
    0   Clean — no unannoted global-timeout references inside phase-loop
        functions.
    1   Violations found — at least one reference flagged.
    2   CLI / IO error (bad arguments, unreadable file, parse error).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Phase names whose presence in a token name constitutes a per-phase form.
# Each entry is the canonical underscore-delimited form as it appears inside
# the constant name (e.g. ``CLAUDE_PHASE_TIMEOUT_RALPH_SECONDS`` embeds
# ``_RALPH_``). The leading/trailing underscores are part of the match so
# ``_CLAIMING_`` does not match ``RECLAIMING``.
# ---------------------------------------------------------------------------
_PHASE_NAME_FRAGMENTS: tuple[str, ...] = (
    "_PLANNING_",
    "_RALPH_",
    "_SUMMARY_",
    "_FIX_CI_",
    "_FIX_CONFLICT_",
    "_VERIFY_",
    "_RETRO_",
    "_PUSH_AND_PR_",
    "_OPERATIONAL_",
    "_AWAITING_CI_",
    "_AWAITING_DEPLOY_",
    "_CLAIMING_",
)

# Match any ALL_CAPS token that ends with _TIMEOUT_SECONDS.
_TIMEOUT_TOKEN_RE = re.compile(r"\b([A-Z][A-Z0-9_]*_TIMEOUT_SECONDS)\b")

# Match per-phase access forms in a source line.
_PER_PHASE_ACCESS_RE = re.compile(
    r"""
    \[_?phase\]          # dict/array subscript: [phase] or [_phase]
    | \[\$_?phase\]      # bash subscript:       [$phase] or [$_phase]
    | \.get\(_?phase\b   # Python dict.get(phase or .get(_phase
    | _for_phase\(       # call to *_for_phase(...) helper
    """,
    re.VERBOSE,
)

# Match the global-by-design annotation.
_GLOBAL_BY_DESIGN_RE = re.compile(r"#\s*global-by-design\s*\(#\d+\)")

# Default directory to scan.
DEFAULT_SOURCE_DIR = Path("scripts/dispatcher")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check that *_TIMEOUT_SECONDS constants in phase-loop functions "
            "use per-phase access forms or carry a global-by-design annotation."
        ),
    )
    parser.add_argument(
        "source_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=(
            "Directory to scan (recursively). "
            "Default: scripts/dispatcher."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Shared PASS-condition logic
# ---------------------------------------------------------------------------

def _token_passes_by_name(token: str) -> bool:
    """PASS conditions that depend only on the token name (rules 1 & 2)."""
    if token.endswith("_BY_PHASE"):
        return True
    for fragment in _PHASE_NAME_FRAGMENTS:
        if fragment in token:
            return True
    return False


def _line_has_per_phase_access(line: str) -> bool:
    """Rule 3: line matches a per-phase access form."""
    return bool(_PER_PHASE_ACCESS_RE.search(line))


def _line_has_annotation(line: str) -> bool:
    """True if the line contains the global-by-design annotation."""
    return bool(_GLOBAL_BY_DESIGN_RE.search(line))


def _context_has_annotation(all_lines: list[str], ref_lineno: int) -> bool:
    """Rule 4: the reference line or any of the 5 lines above it has the annotation.

    ``ref_lineno`` is 1-based (matching ast / human line numbers).
    """
    start = max(0, ref_lineno - 6)  # 5 lines above + the line itself (0-based)
    end = ref_lineno  # exclusive, so includes ref_lineno-1 (0-based)
    return any(_line_has_annotation(all_lines[i]) for i in range(start, end))


def _decl_has_annotation(all_lines: list[str], token: str) -> bool:
    """Rule 4 (declaration arm): the constant's declaration line has the annotation.

    Scans ``all_lines`` for a line that looks like ``TOKEN = …`` or
    ``TOKEN="${…`` and checks for the annotation on the same line.
    """
    # Python assignment: TOKEN = ...
    py_decl = re.compile(rf"^\s*{re.escape(token)}\s*=")
    # Bash assignment: TOKEN="..." or TOKEN=${...} or TOKEN=...
    sh_decl = re.compile(rf"^\s*{re.escape(token)}=")
    for line in all_lines:
        if (py_decl.match(line) or sh_decl.match(line)) and _line_has_annotation(line):
            return True
    return False


def _check_reference(
    token: str,
    ref_lineno: int,
    all_lines: list[str],
) -> bool:
    """Return True (PASS) / False (FLAG) for a single token reference.

    ``ref_lineno`` is 1-based.
    """
    if _token_passes_by_name(token):
        return True
    ref_line = all_lines[ref_lineno - 1]
    if _line_has_per_phase_access(ref_line):
        return True
    if _context_has_annotation(all_lines, ref_lineno):
        return True
    if _decl_has_annotation(all_lines, token):
        return True
    return False


# ---------------------------------------------------------------------------
# Python file analysis
# ---------------------------------------------------------------------------

def _is_exempt_python(func_name: str) -> bool:
    return func_name.endswith("_for_phase") or func_name.endswith("_by_phase")


def _has_phase_param(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    all_args = (
        func.args.args
        + func.args.posonlyargs
        + func.args.kwonlyargs
    )
    return any(a.arg == "phase" for a in all_args)


def _check_python_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, token, snippet) violations in a Python file."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}") from exc

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise SystemExit(f"error: cannot parse {path}: {exc}") from exc

    all_lines = source.splitlines()
    violations: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_exempt_python(node.name):
            continue
        if not _has_phase_param(node):
            continue
        # Collect body line numbers (1-based).
        body_start = node.body[0].lineno
        body_end = node.end_lineno or node.body[-1].end_lineno or body_start
        for lineno in range(body_start, (body_end or body_start) + 1):
            if lineno < 1 or lineno > len(all_lines):
                continue
            line = all_lines[lineno - 1]
            for m in _TIMEOUT_TOKEN_RE.finditer(line):
                token = m.group(1)
                if not _check_reference(token, lineno, all_lines):
                    violations.append((lineno, token, line.strip()))

    return violations


# ---------------------------------------------------------------------------
# Bash file analysis
# ---------------------------------------------------------------------------

_FUNC_DEF_RE = re.compile(
    r"""
    ^                     # start of line
    (?:function\s+)?      # optional `function` keyword
    ([A-Za-z_][A-Za-z0-9_]*)  # function name
    \s*\(\s*\)            # ()
    \s*\{?                # optional opening brace on same line
    """,
    re.VERBOSE,
)


def _extract_bash_functions(all_lines: list[str]) -> list[tuple[str, int, int]]:
    """Return list of (name, start_lineno, end_lineno) for each bash function.

    Uses a brace-depth state machine. Line numbers are 1-based.
    The start_lineno points to the function's opening line.
    Body lines are start_lineno+1 .. end_lineno-1 (exclusive of braces).
    """
    functions: list[tuple[str, int, int]] = []
    i = 0
    n = len(all_lines)
    while i < n:
        line = all_lines[i]
        m = _FUNC_DEF_RE.match(line)
        if m:
            func_name = m.group(1)
            # Find the opening brace (may be on this line or next).
            depth = line.count("{") - line.count("}")
            j = i
            if depth == 0:
                j += 1
                while j < n:
                    depth += all_lines[j].count("{") - all_lines[j].count("}")
                    if depth > 0:
                        break
                    j += 1
            # Now scan forward until depth returns to 0.
            body_start = j
            k = j + 1
            while k < n and depth > 0:
                depth += all_lines[k].count("{") - all_lines[k].count("}")
                k += 1
            # k-1 is the closing brace line (0-based → 1-based: k)
            functions.append((func_name, i + 1, k))
            i = k
        else:
            i += 1
    return functions


def _is_exempt_bash(func_name: str) -> bool:
    return func_name.endswith("_for_phase") or func_name.endswith("_by_phase")


# Pattern for first non-comment body line that sets _phase or phase from $1.
_PHASE_ASSIGN_RE = re.compile(r'^\s*_?phase="?\$1"?')

# Pattern for a for-loop over phase.
_PHASE_FOR_RE = re.compile(r"\bfor\s+_?phase\s+in\b")

# Pattern for case dispatch on phase variable.
_PHASE_CASE_RE = re.compile(r'\bcase\s+"?\$_?phase"?\s+in\b')


def _is_phase_loop_bash(body_lines: list[str]) -> bool:
    """Return True if this bash function body looks like a phase-loop function."""
    # Check first non-comment, non-empty body line for _phase="$1".
    for line in body_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _PHASE_ASSIGN_RE.match(stripped):
            return True
        break
    # Also check for for-loop or case over phase anywhere in body.
    for line in body_lines:
        if _PHASE_FOR_RE.search(line):
            return True
        if _PHASE_CASE_RE.search(line):
            return True
    return False


def _check_bash_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, token, snippet) violations in a Bash file."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}") from exc

    all_lines = source.splitlines()
    functions = _extract_bash_functions(all_lines)
    violations: list[tuple[int, str, str]] = []

    for func_name, start_lineno, end_lineno in functions:
        if _is_exempt_bash(func_name):
            continue
        # Body lines are between opening and closing brace (exclusive).
        body_linenos = range(start_lineno + 1, end_lineno)
        body_lines = [
            all_lines[ln - 1] for ln in body_linenos if ln <= len(all_lines)
        ]
        if not _is_phase_loop_bash(body_lines):
            continue
        for ln in body_linenos:
            if ln < 1 or ln > len(all_lines):
                continue
            line = all_lines[ln - 1]
            # Skip comment lines — tokens appear in comments as documentation
            # (e.g. "The legacy AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS var")
            # and should not be treated as references.
            if line.lstrip().startswith("#"):
                continue
            for m in _TIMEOUT_TOKEN_RE.finditer(line):
                token = m.group(1)
                if not _check_reference(token, ln, all_lines):
                    violations.append((ln, token, line.strip()))

    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    source_dir = args.source_dir

    if not source_dir.is_dir():
        print(f"error: source_dir does not exist: {source_dir}", file=sys.stderr)
        return 2

    all_violations: list[tuple[Path, int, str, str]] = []
    refs_checked = 0
    phase_funcs_found = 0

    # Collect Python files.
    py_files = sorted(source_dir.rglob("*.py"))
    for py_file in py_files:
        # Skip test files.
        if "tests" in py_file.parts or py_file.name.startswith("test_"):
            continue
        viols = _check_python_file(py_file)
        all_violations.extend((py_file, ln, tok, snip) for ln, tok, snip in viols)
        # Count refs checked (approximate — we count per scan not per flag).

    # Collect Bash files.
    sh_files = sorted(source_dir.rglob("*.sh"))
    for sh_file in sh_files:
        if "tests" in sh_file.parts or sh_file.name.startswith("test_"):
            continue
        viols = _check_bash_file(sh_file)
        all_violations.extend((sh_file, ln, tok, snip) for ln, tok, snip in viols)

    if all_violations:
        print(
            "ERROR: *_TIMEOUT_SECONDS references inside phase-loop functions "
            "that are not per-phase:",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        for file_path, lineno, token, snippet in all_violations:
            print(f"  {file_path}:{lineno}: {token}", file=sys.stderr)
            print(f"      {snippet}", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "Fix: use a per-phase lookup (e.g. a BY_PHASE dict/table, a token name\n"
            "that embeds the phase name, or a *_for_phase() helper), OR add a\n"
            "  # global-by-design (#NNNN)\n"
            "comment on the reference line or its constant declaration if the global\n"
            "cap is intentional for all phases.",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print(f"  See issue #3776 for background.", file=sys.stderr)
        return 1

    total_files = len(py_files) + len(sh_files)
    print(
        f"check-per-phase-timeouts: {total_files} file(s) scanned in "
        f"{source_dir}, no violations found."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
