#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_no_basicconfig_with_extra.py — AST scanner for the
``logging.basicConfig`` + ``extra=`` anti-pattern.

Driven by ``scripts/check-no-basicconfig-with-extra.sh``.  See that
wrapper for the full motivation and CI integration story (issue #4376).

Background — the silent extra= field drop
-----------------------------------------

``logging.basicConfig(format="%(asctime)s %(levelname)-8s %(message)s")``
is the canonical Python stdlib idiom for one-line console logging.  The
format string looks reasonable, but it silently drops every ``extra=``
field passed to ``logger.<level>(...)`` calls because the format only
references ``%(asctime)s``, ``%(levelname)s``, and ``%(message)s`` —
nothing that surfaces the LogRecord's extra dict.  Issue #4368
documented the production incident: a backfill script's ``extra=``
fields disappeared from CloudWatch Logs Insights, and the post-deploy
verification that depended on those fields silently passed.

The fix is ``configure_structlog(json=True, stdlib_bridge=True)`` from
``packages/scraper-framework/src/framework/logging.py`` — it routes
stdlib ``logging.getLogger(__name__)`` calls through structlog's
ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
extras as one event per line.  PR #4368 fixed
``scripts/drain_splitter_carry_forward_clusters.py``; #4373 migrated
the other 13 affected scripts.  This guard prevents the bug from
re-accruing as ``scripts/*.py`` expands.

What is flagged
---------------

A Python source file is flagged when ALL of the following hold:

  1. The file calls ``logging.basicConfig(...)`` (also via
     ``from logging import basicConfig`` then ``basicConfig(...)``).
  2. The file passes ``extra=...`` as a keyword argument to at least
     one logger method call (``logger.info(...)``, ``log.warning(...)``,
     ``LOGGER.error(...)``, etc.) — any attribute call whose attribute
     name is one of ``debug``, ``info``, ``warning``, ``warn``,
     ``error``, ``critical``, ``exception``, or ``log``, with an
     ``extra=`` keyword.
  3. The file does NOT call ``configure_structlog(...)`` anywhere.
     Calling ``configure_structlog`` in the same file means the
     structlog config supersedes basicConfig (or the two are at least
     present, and the operator has been deliberate about routing
     extras through).

The AST-based check correctly distinguishes call sites from comment
references and docstrings — many post-#4373 scripts mention
``logging.basicConfig`` in their migration-history comment but only
*call* ``configure_structlog``, so they pass cleanly.

What is NOT flagged
-------------------

  - Files with ``basicConfig(...)`` but no ``extra=`` calls — the bug
    is latent.  #4373 migrated these defensively; the guard does not
    re-flag them.  (Catching the latent shape too is appealing but
    out of scope per the issue body — the primary class of regression
    we want to prevent is *new* ``extra=`` calls layered onto existing
    ``basicConfig`` configs.)
  - Files that call both ``basicConfig`` AND ``configure_structlog``.
    Order matters at runtime (the last config wins), but if a file
    has been deliberate enough to call both, we trust that the
    contributor has the structlog routing they want.
  - Files outside the scan scope (the wrapper restricts to
    ``scripts/*.py`` top-level by default; pass a custom path for
    tests).

Usage
-----

    python3 scripts/check_no_basicconfig_with_extra.py [PATH ...]

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
import sys
from pathlib import Path

# ─── Logger-method names ──────────────────────────────────────────────
# Every logger call attribute we recognise.  Matches the standard
# ``logging.Logger`` API plus the lowercase aliases.  We accept any
# variable name on the receiving end (``logger``, ``log``, ``LOGGER``,
# ``self.log``, etc.) — the AST walk does not need to resolve the
# variable, it just needs to spot ``<anything>.<method>(extra=...)``.
_LOGGER_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "critical",
        "exception",
        "log",
    }
)


def _is_basicconfig_call(node: ast.Call) -> bool:
    """Return True if ``node`` is a ``logging.basicConfig(...)`` call.

    Recognises both forms:

        logging.basicConfig(...)
        basicConfig(...)             # after ``from logging import basicConfig``
    """
    func = node.func
    # logging.basicConfig(...)
    if isinstance(func, ast.Attribute):
        if (
            func.attr == "basicConfig"
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
        ):
            return True
    # basicConfig(...)  — caller did `from logging import basicConfig`
    if isinstance(func, ast.Name) and func.id == "basicConfig":
        return True
    return False


def _is_configure_structlog_call(node: ast.Call) -> bool:
    """Return True if ``node`` is a ``configure_structlog(...)`` call.

    Recognises both forms:

        configure_structlog(...)
        framework.logging.configure_structlog(...)
        anything.configure_structlog(...)

    The check is intentionally permissive — any callable whose final
    attribute or bare name is ``configure_structlog`` counts as the
    structlog opt-in, since there is no other function with that name
    in the codebase.
    """
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "configure_structlog":
        return True
    if isinstance(func, ast.Name) and func.id == "configure_structlog":
        return True
    return False


def _is_logger_call_with_extra(node: ast.Call) -> bool:
    """Return True if ``node`` is a logger method call with ``extra=`` kwarg.

    Matches any attribute call whose attribute name is in
    ``_LOGGER_METHODS`` AND has at least one keyword named ``extra``.
    The receiver name is not constrained — ``logger.info(extra=...)``,
    ``log.warning(extra=...)``, ``self.LOGGER.error(extra=...)``, and
    ``getLogger(__name__).debug(extra=...)`` all match.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _LOGGER_METHODS:
        return False
    for kw in node.keywords:
        if kw.arg == "extra":
            return True
    return False


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return a list of (lineno, snippet) tuples — one per violation.

    A file with no violations returns an empty list.

    Each violation is reported once at the line of the
    ``logging.basicConfig(...)`` call, with a snippet naming the
    co-occurring ``extra=`` line numbers so the operator can find both
    halves of the bug at a glance.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Not valid Python — skip.  May be a fixture or template.
        return []

    basicconfig_lines: list[int] = []
    configure_structlog_lines: list[int] = []
    extra_call_lines: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_basicconfig_call(node):
            basicconfig_lines.append(node.lineno)
        elif _is_configure_structlog_call(node):
            configure_structlog_lines.append(node.lineno)
        elif _is_logger_call_with_extra(node):
            extra_call_lines.append(node.lineno)

    # No basicConfig OR no extra= → no violation.
    if not basicconfig_lines or not extra_call_lines:
        return []

    # Has configure_structlog → operator opted into structlog routing,
    # trust the deliberate config.
    if configure_structlog_lines:
        return []

    # All three conditions hold — flag the file.  Report at the first
    # basicConfig call line; show the first two extra= line numbers in
    # the snippet so the human reading the CI log can jump to both
    # halves of the anti-pattern.
    first_bc = basicconfig_lines[0]
    extra_preview = ",".join(str(n) for n in extra_call_lines[:2])
    if len(extra_call_lines) > 2:
        extra_preview += f",... ({len(extra_call_lines)} total)"
    snippet = f"logging.basicConfig + extra= at line(s) {extra_preview} (no configure_structlog)"
    return [(first_bc, snippet)]


def main(argv: list[str]) -> int:
    """Print one ``<path>:<lineno>:<snippet>`` line per violation."""
    paths = [Path(p) for p in argv[1:]]
    for path in paths:
        for lineno, snippet in scan_file(path):
            print(f"{path}:{lineno}:{snippet}")
    # Exit 0 unconditionally — the wrapper script aggregates and exits 1
    # on non-empty output.  See module docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
