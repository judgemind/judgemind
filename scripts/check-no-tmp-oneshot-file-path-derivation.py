#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-no-tmp-oneshot-file-path-derivation.py — Fail if any
``scripts/*.py`` file derives a filesystem path from
``Path(__file__).resolve().parent.parent`` (or longer ``.parent`` chains)
and later passes that path to a data-load API, without an at-import
assertion that the path actually resolves on disk.

Why this check exists
---------------------
Issue #4374 surfaced a path-resolution bug in
``scripts/drain_splitter_carry_forward_clusters.py``: the file derived
``_SCRAPER_SRC = Path(__file__).resolve().parent.parent / "packages" /
"scraper-framework" / "src"`` and then passed
``_SCRAPER_SRC / "ingestion" / "split_ids.py"`` directly to
``importlib.util.spec_from_file_location``. The anchor only points at
the repo root on the developer-laptop layout. When the script is
executed via ``scripts/ecs-run-task.sh``, it is uploaded to
``/tmp/_oneshot_script`` and ``__file__`` resolves there — so
``parent.parent`` collapses to ``/`` and the hand-constructed path
silently becomes ``/packages/scraper-framework/src/ingestion/split_ids.py``,
which produced the bare error
``[Errno 2] No such file or directory: '/packages/scraper-framework/src/ingestion/split_ids.py'``.

PR #4378 fixed that one site with a candidate-path fallback list. This
check enforces the structural invariant: any future script that derives
a data-file path from ``Path(__file__).resolve().parent.parent`` must
either (a) assert at import time that the derived path actually exists
on disk (so a future ECS-oneshot upload crashes loudly at import
instead of silently failing inside a per-row ``except Exception``
branch) or (b) carry the ``# oneshot-path-required: <reason>`` opt-out
marker.

Rules enforced
--------------
For every ``scripts/*.py`` file (excluding ``scripts/tests/``,
``scripts/dispatcher/`` and ``scripts/.venv``):

  1. Identify "tmp-collapse-prone" assignments — ``Assign`` /
     ``AnnAssign`` nodes whose RHS is an expression involving
     ``Path(__file__).resolve().parent.parent`` (or longer ``.parent``
     chains). The shape is anchored by Path(__file__).resolve() at the
     bottom and at least TWO ``.parent`` accesses — the empty-string
     ``/`` collapse from oneshot upload. A single ``.parent`` resolves
     to ``/tmp`` (still a valid filesystem path), so it isn't part of
     the bug class.
  2. Track transitive derivation: ``X = _REPO_ROOT / "packages"`` makes
     ``X`` tmp-collapse-prone if ``_REPO_ROOT`` already is. Iterate to
     a fixed point.
  3. For each tmp-collapse-prone variable, scan the file for *unsafe*
     data-load API calls that pass it (or any value derived from it
     via further ``/ "literal"`` chains):
       - ``open(<path>, ...)`` — builtin or imported.
       - ``<X>.read_text(...)`` / ``<X>.read_bytes(...)``.
       - ``importlib.util.spec_from_file_location(name, location=X)``.
     Note: ``<X>.is_file()`` / ``<X>.is_dir()`` / ``<X>.exists()`` are
     deliberately NOT flagged — they are safe filesystem probes that
     return ``False`` rather than raising on a collapsed path. They
     are also the canonical SAFE pattern used to guard subsequent
     unsafe loads (Rule 4 below).
  4. An unsafe call is **safe** (not flagged) when at least one of:
       - The same function (or any enclosing scope up to module-level)
         contains an existence-probe call on the same name as the
         unsafe call: ``X.is_file()`` / ``X.is_dir()`` /
         ``X.exists()`` (typically inside ``if X.is_file():`` or ``if
         not X.is_file(): raise ...``). The probe is the script's
         own self-diagnosing guard against the collapse.
       - The file contains a module-scope existence assertion on any
         tmp-collapse-prone variable: ``assert X.is_file()`` or ``if
         not X.is_file(): raise ...`` at module top level. This makes
         every reference to ``X`` in any function body safe — the
         module fails to import otherwise.
       - The unsafe call is inside a ``try: ...`` block with an
         ``except OSError`` / ``except FileNotFoundError`` / bare
         ``except:`` handler. The OSError-class-catch is functionally
         equivalent to an existence probe — the wrong-path failure
         surfaces as caught-and-handled, not a silently-wrong result.
       - The unsafe call carries an ``# oneshot-path-required:
         <reason>`` marker on the same line or anywhere in its call
         span.
  5. ``sys.path.insert(...)`` and ``sys.path.append(...)`` are NOT
     flagged at all — passing a bogus path to ``sys.path`` just fails
     to resolve any imports, which is forgiving rather than
     silently-wrong (the ``ModuleNotFoundError`` is itself
     self-diagnosing).

Output
------
On failure the script prints one line per offending data-load call:
``path:line: <api> -- derived-from: <variable>``

Allowlist marker
----------------
``# oneshot-path-required: <reason>`` on the same line as the data-load
call (or its multi-line span). The rule name AND a non-empty reason are
required — a bare ``# noqa`` does NOT exempt.

Usage
-----
  scripts/check-no-tmp-oneshot-file-path-derivation.py             # scan default tree (scripts/)
  scripts/check-no-tmp-oneshot-file-path-derivation.py [path...]   # scan specific files

Exit codes
----------
  0 -- No violations.
  1 -- At least one tmp-collapse-prone path derivation reaches a data-load
       API without an at-import existence assertion.

Tracking: issue #4381.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_ROOT = REPO_ROOT / "scripts"

# Allowlist marker — see module docstring for the exact spelling. We
# require the rule name AND a non-empty reason. Bare markers are
# rejected — see _line_is_allowlisted.
_ALLOWLIST_RE = re.compile(r"#\s*oneshot-path-required\s*:\s*\S+")

# Unsafe data-load API names tracked by Rule 3.
#
# These methods raise ``FileNotFoundError`` (or worse — ``importlib.util.
# spec_from_file_location`` returns a spec whose loader.exec_module()
# explodes deep inside the import machinery) when the path doesn't
# resolve. They are the data-load APIs the bug class actually corrupts.
_UNSAFE_PATH_METHODS = frozenset({"read_text", "read_bytes", "open"})
_BUILTIN_DATA_LOAD_FUNCS = frozenset({"open"})

# Safe filesystem probes — Rule 3 explicitly excludes these from being
# flagged. Their presence in the same scope as an unsafe call makes the
# unsafe call SAFE (Rule 4: existence-probe guard).
_SAFE_PATH_PROBES = frozenset({"is_file", "is_dir", "exists"})


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _is_path_file_resolve_parent_chain(node: ast.expr) -> bool:
    """Return True if ``node`` matches
    ``Path(__file__).resolve().parent.parent`` or any extension of that
    chain (``parent.parent.parent`` …).

    The chain is anchored at the bottom by ``Path(__file__).resolve()``
    (or ``Path(__file__).resolve().parent`` already on a sub-expression)
    and must contain at least TWO ``.parent`` accesses to constitute
    the "/ collapses to /" failure mode. A single ``.parent`` after
    ``Path(__file__).resolve()`` (which is ``scripts/``) does not
    collapse — when uploaded to ``/tmp/_oneshot_script`` it resolves to
    ``/tmp``, and any literal segment chained off ``/tmp`` is still a
    valid filesystem path (just the wrong one). The bug class is
    specifically the empty-string ``/`` collapse from chaining two or
    more ``.parent`` accesses.
    """
    parent_count = 0
    cursor: ast.expr = node
    while isinstance(cursor, ast.Attribute) and cursor.attr == "parent":
        parent_count += 1
        cursor = cursor.value
    if parent_count < 2:
        return False
    # The bottom of the chain must be ``Path(__file__).resolve()``.
    return _is_path_file_resolve(cursor)


def _is_path_file_resolve(node: ast.expr) -> bool:
    """Return True if ``node`` matches ``Path(__file__).resolve()``."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "resolve":
        return False
    inner = node.func.value
    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)):
        return False
    if inner.func.id != "Path":
        return False
    if len(inner.args) != 1:
        return False
    arg = inner.args[0]
    return isinstance(arg, ast.Name) and arg.id == "__file__"


def _expression_contains_collapse_anchor(node: ast.expr) -> bool:
    """Return True if any sub-expression of ``node`` matches
    ``Path(__file__).resolve().parent.parent`` (with >=2 .parent)."""
    for descendant in ast.walk(node):
        if isinstance(descendant, ast.Attribute):
            if _is_path_file_resolve_parent_chain(descendant):
                return True
    return False


def _expression_uses_name(node: ast.expr, names: set[str]) -> bool:
    """Return True if any ``ast.Name(id=...)`` in ``node`` is in ``names``."""
    for descendant in ast.walk(node):
        if isinstance(descendant, ast.Name) and descendant.id in names:
            return True
    return False


# ---------------------------------------------------------------------------
# Tmp-collapse-prone variable discovery
# ---------------------------------------------------------------------------


def _find_collapse_prone_names(tree: ast.AST) -> set[str]:
    """Return the set of variable names whose value, at any point in
    ``tree``, was derived from ``Path(__file__).resolve().parent.parent``.

    Iterates assignments to a fixed point: ``A = Path(__file__).resolve()
    .parent.parent`` makes ``A`` collapse-prone; then ``B = A / "packages"``
    makes ``B`` collapse-prone; etc.
    """
    collapse_prone: set[str] = set()

    # Pass 1: seed with assignments whose RHS contains the literal
    # ``Path(__file__).resolve().parent.parent`` shape.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        rhs = node.value if isinstance(node, ast.Assign) else node.value
        if rhs is None:
            continue
        if not _expression_contains_collapse_anchor(rhs):
            continue
        for target in _assign_targets(node):
            if isinstance(target, ast.Name):
                collapse_prone.add(target.id)

    # Pass 2..N: propagate to assignments whose RHS references an
    # already-collapse-prone name. Iterate to a fixed point — the
    # sequence ``A = anchor; B = A / "x"; C = B / "y"`` requires 3
    # passes.
    while True:
        added = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            rhs = node.value if isinstance(node, ast.Assign) else node.value
            if rhs is None:
                continue
            if not _expression_uses_name(rhs, collapse_prone):
                continue
            for target in _assign_targets(node):
                if isinstance(target, ast.Name) and target.id not in collapse_prone:
                    collapse_prone.add(target.id)
                    added = True
        if not added:
            break

    return collapse_prone


def _assign_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    if isinstance(node, ast.AnnAssign):
        return [node.target] if node.target is not None else []
    targets: list[ast.expr] = []
    for target in node.targets:
        if isinstance(target, ast.Tuple):
            targets.extend(target.elts)
        else:
            targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# Module-scope existence-assertion detection
# ---------------------------------------------------------------------------


def _has_module_scope_existence_assertion(tree: ast.AST, names: set[str]) -> set[str]:
    """Return the subset of ``names`` for which the module body contains
    a module-scope existence assertion — either a top-level ``assert (X
    / ...).is_file()`` / ``.is_dir()``, OR a top-level ``if not (X /
    ...).is_file(): raise ...`` / similar guard.

    ``tree`` must be an ``ast.Module``. We only walk the direct body so
    function-scoped assertions don't count — the bug shape is "import
    fails loudly at module load", which only requires module-scope
    guards.
    """
    if not isinstance(tree, ast.Module):
        return set()

    asserted: set[str] = set()

    for stmt in tree.body:
        if isinstance(stmt, ast.Assert):
            if _assertion_calls_existence_check(stmt.test, names, asserted):
                continue
        if isinstance(stmt, ast.If):
            # ``if not <expr>.is_file(): raise ...`` and friends.
            if _if_guard_raises_on_missing_path(stmt, names, asserted):
                continue
        if isinstance(stmt, ast.Try):
            # try: <X>.is_file(); except: raise ...
            for sub in stmt.body:
                if isinstance(sub, ast.Assert):
                    _assertion_calls_existence_check(sub.test, names, asserted)
                if isinstance(sub, ast.If):
                    _if_guard_raises_on_missing_path(sub, names, asserted)

    return asserted


def _assertion_calls_existence_check(
    test: ast.expr, names: set[str], asserted: set[str]
) -> bool:
    """If ``test`` is a call to ``.is_file()`` / ``.is_dir()`` /
    ``.exists()`` on a Path expression involving any name in ``names``,
    add those names to ``asserted`` and return True."""
    if not isinstance(test, ast.Call):
        return False
    if not isinstance(test.func, ast.Attribute):
        return False
    if test.func.attr not in {"is_file", "is_dir", "exists"}:
        return False
    used = {n for n in names if _expression_uses_name(test.func.value, {n})}
    if not used:
        return False
    asserted.update(used)
    return True


def _if_guard_raises_on_missing_path(
    node: ast.If, names: set[str], asserted: set[str]
) -> bool:
    """Match ``if not <X>.is_file(): raise ...`` / ``if not <X>.is_dir():
    raise ...`` / ``if not <X>.exists(): raise ...`` where ``<X>`` is a
    Path expression involving any name in ``names``.

    Also matches the equivalent positive form:
    ``if <X>.is_file(): pass; else: raise ...``.

    On match, adds the matched names to ``asserted`` and returns True.
    """
    test = node.test
    target_call: ast.Call | None = None
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        # ``if not X.is_file(): raise ...``
        if isinstance(test.operand, ast.Call):
            target_call = test.operand
        body_raises = any(isinstance(s, ast.Raise) for s in node.body)
    else:
        # ``if X.is_file(): ...; else: raise ...``
        if isinstance(test, ast.Call):
            target_call = test
        body_raises = any(isinstance(s, ast.Raise) for s in node.orelse)

    if target_call is None or not body_raises:
        return False
    if not isinstance(target_call.func, ast.Attribute):
        return False
    if target_call.func.attr not in {"is_file", "is_dir", "exists"}:
        return False
    used = {n for n in names if _expression_uses_name(target_call.func.value, {n})}
    if not used:
        return False
    asserted.update(used)
    return True


# ---------------------------------------------------------------------------
# Data-load API call detection
# ---------------------------------------------------------------------------


def _is_unsafe_data_load_call(call: ast.Call) -> tuple[str, ast.expr] | None:
    """If ``call`` is an UNSAFE data-load API call (one that raises on a
    missing path), return ``(label, path_argument)``. Otherwise return
    None.

    Safe filesystem probes (``is_file()``, ``is_dir()``, ``exists()``)
    return False rather than raising on a collapsed path, so they are
    deliberately excluded from this detection.

    Recognised unsafe shapes:
      - ``open(<path>, ...)`` -- builtin / Path.
      - ``importlib.util.spec_from_file_location(<name>, <path>, ...)``
        or ``importlib.util.spec_from_file_location(name=..., location=<path>, ...)``.
      - ``<X>.read_text(...)`` / ``<X>.read_bytes(...)`` / ``<X>.open(...)``
        -- the path is ``<X>``.
    """
    func = call.func

    # ``open(<path>, ...)`` — builtin or imported.
    if isinstance(func, ast.Name) and func.id in _BUILTIN_DATA_LOAD_FUNCS:
        if call.args:
            return ("open", call.args[0])
        file_kwarg = _kwarg_value(call, "file")
        if file_kwarg is not None:
            return ("open", file_kwarg)

    # ``importlib.util.spec_from_file_location(name, location)``.
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "spec_from_file_location"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "util"
    ):
        # Second positional is ``location``.
        if len(call.args) >= 2:
            return ("importlib.util.spec_from_file_location", call.args[1])
        loc_kwarg = _kwarg_value(call, "location")
        if loc_kwarg is not None:
            return ("importlib.util.spec_from_file_location", loc_kwarg)

    # ``<X>.read_text() / .read_bytes() / .open()`` — the path is ``X``.
    if isinstance(func, ast.Attribute) and func.attr in _UNSAFE_PATH_METHODS:
        return (f"Path.{func.attr}", func.value)

    return None


def _scope_contains_safe_probe_for_var(scope: ast.AST, names: set[str]) -> bool:
    """Return True if any ``ast.Call`` under ``scope`` is a safe
    filesystem probe (``is_file()`` / ``is_dir()`` / ``exists()``) on
    a Path expression that uses any name in ``names``.

    The presence of such a probe in the same lexical scope as an unsafe
    data-load call makes the unsafe call safe — the script has its own
    self-diagnosing guard against the ``/`` collapse.

    The scope is typically a ``FunctionDef`` (the enclosing function of
    the unsafe call) or the ``Module`` itself when the unsafe call is at
    module top level.
    """
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in _SAFE_PATH_PROBES:
            continue
        if _expression_uses_name(func.value, names):
            return True
    return False


def _call_is_inside_try_except(tree: ast.AST, call: ast.Call) -> bool:
    """Return True if ``call`` is inside a ``try:`` block whose ``except``
    clauses catch ``OSError``, ``FileNotFoundError``, or a bare ``except``.

    This is the equivalent of an existence-probe guard: a
    ``read_text()`` wrapped in ``try: ...; except OSError: ...`` cannot
    silently produce a wrong path — the OSError is caught and handled.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        # Is ``call`` inside the ``try`` body (not the handlers)?
        for stmt in node.body:
            if _node_contains(stmt, call):
                # Check that at least one handler catches OSError /
                # FileNotFoundError / bare.
                for handler in node.handlers:
                    if _handler_catches_oserror(handler):
                        return True
    return False


def _handler_catches_oserror(handler: ast.ExceptHandler) -> bool:
    """Return True if ``handler`` catches ``OSError``,
    ``FileNotFoundError``, or is a bare ``except:``."""
    exc = handler.type
    if exc is None:
        # Bare ``except:`` catches everything.
        return True
    targets = {"OSError", "FileNotFoundError", "Exception", "BaseException"}
    if isinstance(exc, ast.Name) and exc.id in targets:
        return True
    if isinstance(exc, ast.Attribute) and exc.attr in targets:
        return True
    if isinstance(exc, ast.Tuple):
        for elt in exc.elts:
            if isinstance(elt, ast.Name) and elt.id in targets:
                return True
            if isinstance(elt, ast.Attribute) and elt.attr in targets:
                return True
    return False


def _enclosing_function_or_module(tree: ast.AST, target: ast.AST) -> ast.AST:
    """Return the innermost ``FunctionDef`` / ``AsyncFunctionDef`` ancestor
    of ``target``, or the module itself if ``target`` is at top level.

    We walk the tree explicitly because ``ast`` doesn't track parents.
    """
    candidate: ast.AST = tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _node_contains(node, target):
                candidate = node
    return candidate


def _node_contains(parent: ast.AST, child: ast.AST) -> bool:
    """Return True if ``child`` is a descendant of ``parent`` (or the same
    node)."""
    if parent is child:
        return True
    for node in ast.walk(parent):
        if node is child:
            return True
    return False


def _kwarg_value(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


# ---------------------------------------------------------------------------
# sys.path.insert / sys.path.append filter
# ---------------------------------------------------------------------------


def _is_sys_path_call(call: ast.Call) -> bool:
    """Return True if ``call`` is ``sys.path.insert(...)`` or
    ``sys.path.append(...)``. These are the safe usages of a
    tmp-collapse-prone variable: a bogus sys.path entry just doesn't
    resolve any imports."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"insert", "append"}:
        return False
    if not isinstance(func.value, ast.Attribute) or func.value.attr != "path":
        return False
    if not isinstance(func.value.value, ast.Name) or func.value.value.id != "sys":
        return False
    return True


# ---------------------------------------------------------------------------
# Allowlist parsing
# ---------------------------------------------------------------------------


def _line_is_allowlisted(call: ast.Call, source_lines: list[str]) -> bool:
    """Return True if any source line covered by ``call`` carries an
    ``# oneshot-path-required: <reason>`` comment."""
    start = call.lineno
    end = getattr(call, "end_lineno", None) or start
    for lineno in range(start, end + 1):
        idx = lineno - 1
        if 0 <= idx < len(source_lines):
            if _ALLOWLIST_RE.search(source_lines[idx]):
                return True
    return False


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def _iter_python_files(roots: list[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if not root.is_dir():
            continue
        # Top-level only — scripts/ is a flat directory of one-shots.
        # We don't recurse into scripts/dispatcher/ (covered by
        # check-no-unbounded-timeouts.py) or scripts/tests/.
        for path in sorted(root.glob("*.py")):
            yield path


def scan_file(path: Path) -> list[str]:
    """Return a list of violation lines for ``path``. Empty if clean."""
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    source_lines = source.splitlines()

    collapse_prone = _find_collapse_prone_names(tree)
    if not collapse_prone:
        return []

    # Names with a MODULE-scope existence assertion are safe globally —
    # the script crashes loudly at import time before any data-load
    # call has a chance to fire.
    module_asserted = _has_module_scope_existence_assertion(tree, collapse_prone)
    unguarded_module_level = collapse_prone - module_asserted
    if not unguarded_module_level:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_sys_path_call(node):
            continue
        result = _is_unsafe_data_load_call(node)
        if result is None:
            continue
        label, path_expr = result
        # Determine if path_expr derives from any unguarded name.
        derived_from = {
            n for n in unguarded_module_level if _expression_uses_name(path_expr, {n})
        }
        if not derived_from:
            continue
        if _line_is_allowlisted(node, source_lines):
            continue
        # Per-call scope check: an existence probe (is_file / is_dir /
        # exists) on the same name in the enclosing function (or
        # module body) makes the unsafe call safe.
        scope = _enclosing_function_or_module(tree, node)
        if _scope_contains_safe_probe_for_var(scope, derived_from):
            continue
        # try/except OSError around the unsafe call is also a safe
        # guard — the FileNotFoundError is caught and handled instead
        # of silently producing a wrong result.
        if _call_is_inside_try_except(tree, node):
            continue
        for var in sorted(derived_from):
            violations.append(f"{path}:{node.lineno}: {label} -- derived-from: {var}")
    return violations


def main(argv: list[str]) -> int:
    if argv:
        roots = [Path(a).resolve() for a in argv]
    else:
        roots = [DEFAULT_SCAN_ROOT]

    all_violations: list[str] = []
    for path in _iter_python_files(roots):
        all_violations.extend(scan_file(path))

    if all_violations:
        sys.stderr.write(
            "ERROR: scripts/*.py file(s) derive a data-file path from\n"
            "Path(__file__).resolve().parent.parent without an at-import\n"
            "existence assertion. When uploaded via scripts/ecs-run-task.sh\n"
            "the script lands at /tmp/_oneshot_script and parent.parent\n"
            "collapses to /, silently producing a wrong path.\n\n"
        )
        for line in all_violations:
            sys.stderr.write(f"    {line}\n")
        sys.stderr.write(
            "\nFix options:\n"
            "  1. For oneshot-uploadable scripts, build a candidate-path\n"
            "     fallback list (developer-laptop -> /app/<canonical> ->\n"
            "     defense-in-depth) and raise a self-diagnosing\n"
            "     RuntimeError when none of them resolve. See\n"
            "     scripts/drain_splitter_carry_forward_clusters.py\n"
            "     _split_ids_candidate_paths / _resolve_split_ids_path for\n"
            "     the canonical pattern (#4374, PR #4378).\n"
            "  2. For non-oneshot scripts, assert at import time that the\n"
            "     derived path resolves on disk:\n"
            "         _REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "         assert (_REPO_ROOT / 'packages').is_dir(), (\n"
            "             f'_REPO_ROOT={_REPO_ROOT} does not contain packages/'\n"
            "         )\n"
            "     Any future ECS-oneshot upload then crashes loudly at\n"
            "     import time instead of silently failing inside a\n"
            "     per-row except branch.\n"
            "  3. If the call site is intentionally exempt, annotate it\n"
            "     with '# oneshot-path-required: <reason>' on the same\n"
            "     line as the data-load call.\n"
            "Tracking: issue #4381.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
