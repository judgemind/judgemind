#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_no_hardcoded_llm_provider.py — AST scanner for hardcoded
``provider="..."`` arguments to the LLM adapter layer.

Driven by ``scripts/check-no-hardcoded-llm-provider.sh``.  See that
wrapper for the full motivation and CI integration story (issue #4050).

The scanner walks every call expression in each input file and emits a
one-line report of the form ``<path>:<lineno>:<func>(provider="<value>")``
for each call where:

  1. The callee's name (``func.id`` for plain calls, ``func.attr`` for
     attribute-style calls like ``llm_providers.call_llm(``) is one of:
       - ``call_llm``
       - ``call_llm_with_images``
       - ``create_client``
       - ``create_llm_client``
  2. The call has a ``provider=`` keyword argument whose value is an
     ``ast.Constant`` with a string literal (e.g. ``provider="anthropic"``).

A ``provider=`` arg whose value is a name (``provider=self._llm_provider``,
``provider=DEFAULT_PROVIDER``), an attribute access, or any other non-literal
expression is NOT flagged — the env var resolution path stays in control.

The ``LlmExtractor(provider="google", ...)`` constructor pattern is
intentionally OUT of scope (per #4050 "Out of Scope") — that's a per-court
override at the scraper-class boundary, distinct from the call-site flip.
The four function names above are the closed set.

Suppression: if the line of the call site (or any line of a multi-line
call, between the opening paren line and the closing paren line) has a
``# hardcoded-provider-ok: <reason>`` comment, the call is skipped.

Usage
-----

    python3 scripts/check_no_hardcoded_llm_provider.py [PATH ...]

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

# The closed set of LLM-adapter entry points whose ``provider=`` arg
# bypasses the LLM_PROVIDER env var when pinned to a string literal.
_TARGET_FUNCS: frozenset[str] = frozenset(
    {
        "call_llm",
        "call_llm_with_images",
        "create_client",
        "create_llm_client",
    }
)

# The opt-out marker — a trailing comment on any line of the call site.
_OPT_OUT_RE = re.compile(r"#\s*hardcoded-provider-ok\s*:\s*\S")


def _callee_name(func: ast.expr) -> str | None:
    """Return the bare function name regardless of call shape.

    Handles ``call_llm(...)``, ``llm_providers.call_llm(...)``, and
    ``mod.sub.call_llm(...)``.  Returns ``None`` for any callable that
    isn't a Name or Attribute (e.g. lambdas, subscripts, calls).
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _has_opt_out(source_lines: list[str], start_lineno: int, end_lineno: int) -> bool:
    """Check for ``# hardcoded-provider-ok: <reason>`` on any line of the call.

    ``source_lines`` is the file split on newlines (1-indexed via -1).
    """
    for line_idx in range(start_lineno - 1, min(end_lineno, len(source_lines))):
        if _OPT_OUT_RE.search(source_lines[line_idx]):
            return True
    return False


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of ``(lineno, callee, literal)`` violations for ``path``."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    violations: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        callee = _callee_name(node.func)
        if callee not in _TARGET_FUNCS:
            continue

        for kw in node.keywords:
            if kw.arg != "provider":
                continue
            if not isinstance(kw.value, ast.Constant):
                continue
            if not isinstance(kw.value.value, str):
                continue

            # Determine the line range of the call expression for opt-out
            # marker scanning.  end_lineno can be None on older ASTs;
            # default to lineno in that case.
            start = node.lineno
            end = getattr(node, "end_lineno", None) or start

            if _has_opt_out(source_lines, start, end):
                continue

            violations.append((start, callee, kw.value.value))

    return violations


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]]

    for path in paths:
        for lineno, callee, literal in _scan_file(path):
            print(f'{path}:{lineno}:{callee}(provider="{literal}")')

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
