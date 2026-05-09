#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_no_manual_sys_modules_mock.py — AST scanner for the manual
``sys.modules["<name>"] = ...`` mocking anti-pattern at module level in
``scripts/tests/test_*.py``.

Driven by ``scripts/check-no-manual-sys-modules-mock.sh``.  See that
wrapper for the full motivation and CI integration story (issue #4434).

Background — the manual save/replay footgun
---------------------------------------------

Pre-#4430, every ``scripts/tests/test_*.py`` file that needed to mock
unavailable modules (``psycopg``, ``structlog``, ``framework.*``,
``ingestion.*``) maintained its own ~15-line save/restore boilerplate
around the import:

    _modules_to_mock = {"structlog": MagicMock(), ...}
    _saved_modules: dict[str, object] = {}
    for _mod_name, _mock_mod in _modules_to_mock.items():
        if _mod_name in sys.modules:
            _saved_modules[_mod_name] = sys.modules[_mod_name]
        sys.modules[_mod_name] = _mock_mod

    import my_script  # noqa: E402

    for _mod_name in list(_modules_to_mock.keys()):
        if _mod_name in _saved_modules:
            sys.modules[_mod_name] = _saved_modules[_mod_name]
        elif _mod_name in sys.modules:
            del sys.modules[_mod_name]

A single forgotten restore loop pollutes ``sys.modules`` for every test
collected later in the same pytest process — the bug class #4426 caught
and pinned with ``test_scripts_tests_isolation.py``.  PR #4430 introduced
``scripts/tests/_mock_helpers.py::mock_sys_modules`` as the canonical
context-manager replacement; this guard prevents the legacy pattern from
re-accruing as new test files are added.

What is flagged
---------------

A ``scripts/tests/test_*.py`` file is flagged when it contains a
**module-level** assignment of the shape ``sys.modules["<name>"] = <expr>``
(or ``sys.modules[name] = <expr>`` with a Name slice) that is NOT inside:

  - a ``with mock_sys_modules(...)`` block (the canonical replacement)
  - a ``def`` function body (those are call-time, not import-time, so
    they are typically already correct via ``patch.dict(sys.modules)``)
  - a ``class`` body (rare, but legal — module-level is the dangerous
    location)
  - a ``try``/``except``/``finally``/``if``/``for``/``while`` block
    that is itself nested inside one of the above (descended via the
    AST walk's ancestor chain).

Allowlist
---------

Two structural carve-outs are honored:

1. **Files calling ``importlib.util.spec_from_file_location(...)`` at
   module level.** These tests load a hyphen-named script as a Python
   module and register it in ``sys.modules`` so dataclass / pickle
   reconstruction (``cls.__module__`` lookup) works — a legitimate use
   that is structurally distinct from MagicMock injection.  Examples:
   ``test_audit_shipped_zombies.py``, ``test_check_shipped_pr_*.py``,
   ``test_check_ci_job_skipped.py``, ``test_check_script_headers.py``,
   ``test_check_ci_guards_skip_list_coverage.py``,
   ``test_check_nullable_column_reads.py``,
   ``test_check_migration_number_collision.py``,
   ``test_check_graphql_nullability_drift.py``, etc.

2. **Filename allowlist** for files whose module-level
   ``sys.modules`` munging is structurally legitimate but does not use
   ``spec_from_file_location``:

   - ``test_mock_helpers.py`` — the helper's own self-tests; all
     ``sys.modules`` assignments are inside ``def test_*`` methods
     (so the structural filter would already pass), but the file is
     listed defensively in case a future test adds a module-level
     probe.
   - ``test_inspect.py`` — exercises ``scripts/spotcheck/inspect.py``
     which collides with the stdlib ``inspect`` module; the test
     pops the stdlib entry, imports the spotcheck module, then
     restores the stdlib so downstream tests aren't broken.  This
     legitimately requires module-level ``sys.modules`` rebinds and
     is not the manual-mocking anti-pattern.

Files can also opt out per-line via the ``# noqa: manual-sys-modules-mock``
trailing comment, or per-file via the
``# manual-sys-modules-mock-allowed: <reason>`` header marker (in the
first 20 lines).  Use the per-file marker only with a clear reason
naming why the file's module-level ``sys.modules`` mutation is not the
``MagicMock``-injection footgun.

Usage
-----

    python3 scripts/check_no_manual_sys_modules_mock.py [PATH ...]

Each PATH is a ``.py`` file under ``scripts/tests/``.  The wrapper
script discovers the paths.

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

# ─── Filename allowlist ───────────────────────────────────────────────
# Files whose basename is in this set are exempt from the scan.  See
# module docstring §"Allowlist" for rationale.  Keep this list short
# and require a CLAUDE.md / docs/agent/code-standards.md justification
# for additions.
_FILENAME_ALLOWLIST = frozenset(
    {
        "test_mock_helpers.py",
        "test_inspect.py",
    }
)

# ─── Per-file opt-out marker ──────────────────────────────────────────
# A header comment of the form ``# manual-sys-modules-mock-allowed: <reason>``
# in the first 20 lines exempts the file.  The reason after the colon
# is required.
_OPT_OUT_MARKER_PREFIX = "# manual-sys-modules-mock-allowed:"
_OPT_OUT_HEADER_LINES = 20

# ─── Per-line opt-out marker ──────────────────────────────────────────
# A trailing comment matching the literal stored in ``_NOQA_MARKER`` on
# the assignment line exempts that single assignment.  See the module
# docstring §"Allowlist" for the exact comment text.  The literal lives
# in a constant rather than inline in a comment to avoid tripping
# ruff's directive parser, which scans every comment for the pattern
# matched by ``_NOQA_MARKER``.
_NOQA_MARKER = "noqa: manual-sys-modules-mock"


def _has_file_opt_out(source: str) -> bool:
    """Return True if the source has the per-file opt-out marker in its
    first ``_OPT_OUT_HEADER_LINES`` lines.

    The marker requires a non-empty reason after the colon — a bare
    ``# manual-sys-modules-mock-allowed:`` (no text) does NOT trip the
    opt-out.  This forces a one-time deliberate justification at the
    add site.
    """
    for line in source.splitlines()[:_OPT_OUT_HEADER_LINES]:
        stripped = line.strip()
        if stripped.startswith(_OPT_OUT_MARKER_PREFIX):
            reason = stripped[len(_OPT_OUT_MARKER_PREFIX) :].strip()
            if reason:
                return True
    return False


def _line_has_noqa(source_lines: list[str], lineno: int) -> bool:
    """Return True if the given source line carries the per-line noqa marker."""
    if 1 <= lineno <= len(source_lines):
        return _NOQA_MARKER in source_lines[lineno - 1]
    return False


def _file_uses_spec_from_file_location(tree: ast.Module) -> bool:
    """Return True if the file calls ``importlib.util.spec_from_file_location``
    anywhere — at module level or inside a function body.

    The signal is the *call*, not the *placement* — a file that loads a
    dash-named script via ``spec_from_file_location`` and registers it
    in ``sys.modules`` is structurally a hyphenated-script loader, not a
    MagicMock injector, regardless of whether the call sits at module
    level (typical) or inside a ``_load_module()`` helper (also typical).

    Recognised forms:

        importlib.util.spec_from_file_location(...)
        spec_from_file_location(...)              # after ``from importlib.util import spec_from_file_location``

    The check is permissive — any callable whose final attribute or
    bare name is ``spec_from_file_location`` counts, since there is no
    other function with that name in stdlib or this codebase.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "spec_from_file_location":
            return True
        if isinstance(func, ast.Name) and func.id == "spec_from_file_location":
            return True
    return False


def _is_sys_modules_subscript_assign(node: ast.AST) -> bool:
    """Return True if ``node`` is an assignment whose target is
    ``sys.modules[<key>]`` (the shape the guard forbids at module level).

    Recognises both ``sys.modules["x"] = y`` (Constant slice) and
    ``sys.modules[name_var] = y`` (Name slice).  The variable form is
    less common but still legal and still has the same restore footgun.
    """
    if not isinstance(node, ast.Assign):
        return False
    # Multiple targets (``a = b = sys.modules["x"]``) — unusual; reject
    # unless every target is a sys.modules subscript.  The simpler
    # invariant: at least one target IS the subscript shape.
    for target in node.targets:
        if _is_sys_modules_subscript(target):
            return True
    return False


def _is_sys_modules_subscript(node: ast.AST) -> bool:
    """Return True if ``node`` is the AST subscript ``sys.modules[<x>]``."""
    if not isinstance(node, ast.Subscript):
        return False
    value = node.value
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "modules"
        and isinstance(value.value, ast.Name)
        and value.value.id == "sys"
    )


def _collect_module_level_violations(
    tree: ast.Module,
    source_lines: list[str],
) -> list[tuple[int, str]]:
    """Walk the module body recursively, collecting ``sys.modules[...] = ...``
    assignments that sit at the module scope outside ``def`` / ``class``
    bodies and outside ``with mock_sys_modules(...)`` blocks.

    A ``with mock_sys_modules(...)`` block's body is module-level
    structurally, but the helper's contract guarantees restoration —
    so we descend into the block but skip flagging assignments inside
    it.  This means a helper-wrapped assignment is NOT flagged, while
    a manual one in the same module IS.
    """
    violations: list[tuple[int, str]] = []

    def is_mock_sys_modules_call(node: ast.AST) -> bool:
        """Return True if ``node`` is a call to ``mock_sys_modules(...)``.

        Recognises both the bare-name form (``mock_sys_modules(...)``)
        and any attribute form ending in ``.mock_sys_modules`` (rare).
        """
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name) and func.id == "mock_sys_modules":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "mock_sys_modules":
            return True
        return False

    def visit(stmts: list[ast.stmt], inside_helper: bool) -> None:
        """Recursively visit a list of statements at module-equivalent scope.

        ``inside_helper`` is True iff we are currently descending inside
        the body of a ``with mock_sys_modules(...)`` block — assignments
        encountered while True are NOT flagged.

        Function and class bodies are skipped entirely — those are
        call-time scopes, not import-time.
        """
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Function / class body — do not descend.  Module-load
                # never executes these unless the function is called,
                # which is fine because it's call-time, not import-time.
                continue

            if isinstance(stmt, ast.With):
                # A ``with`` block at module level.  If any of its items
                # is ``mock_sys_modules(...)``, treat the body as
                # helper-wrapped.
                wraps_helper = any(
                    is_mock_sys_modules_call(item.context_expr) for item in stmt.items
                )
                visit(stmt.body, inside_helper or wraps_helper)
                continue

            if isinstance(stmt, (ast.If, ast.For, ast.While)):
                # Simple control-flow blocks at module level — descend
                # into all branches.  An assignment guarded by ``if`` is
                # still effectively module-level (it runs at import time
                # if the condition holds).
                visit(stmt.body, inside_helper)
                visit(stmt.orelse, inside_helper)
                continue

            if isinstance(stmt, ast.Try):
                # Try/except/finally at module level — descend.
                visit(stmt.body, inside_helper)
                for handler in stmt.handlers:
                    visit(handler.body, inside_helper)
                visit(stmt.orelse, inside_helper)
                visit(stmt.finalbody, inside_helper)
                continue

            # Plain statement.  If it's a sys.modules subscript
            # assignment AND we're not inside a helper-wrapped block,
            # flag it (subject to the per-line noqa marker).
            if not inside_helper and _is_sys_modules_subscript_assign(stmt):
                lineno = stmt.lineno
                if not _line_has_noqa(source_lines, lineno):
                    snippet = _format_snippet(stmt, source_lines)
                    violations.append((lineno, snippet))

    visit(tree.body, inside_helper=False)
    return violations


def _format_snippet(node: ast.stmt, source_lines: list[str]) -> str:
    """Render a one-line snippet of the violating assignment for the
    error message.  Uses the original source line so the operator sees
    exactly what they wrote (``sys.modules["foo"] = MagicMock()``)."""
    lineno = node.lineno
    if 1 <= lineno <= len(source_lines):
        # Strip leading/trailing whitespace to match the rest of the
        # error report.  Truncate at 80 characters to keep CI logs
        # readable.
        line = source_lines[lineno - 1].strip()
        if len(line) > 80:
            line = line[:77] + "..."
        return line
    return "<assignment at line {0}>".format(lineno)


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return a list of (lineno, snippet) tuples — one per violation.

    A file with no violations returns an empty list.  Files in the
    filename allowlist or carrying the per-file opt-out marker return
    early with an empty list.
    """
    if path.name in _FILENAME_ALLOWLIST:
        return []

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    if _has_file_opt_out(source):
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Not valid Python — skip.  May be a fixture or template.
        return []

    if _file_uses_spec_from_file_location(tree):
        return []

    source_lines = source.splitlines()
    return _collect_module_level_violations(tree, source_lines)


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
