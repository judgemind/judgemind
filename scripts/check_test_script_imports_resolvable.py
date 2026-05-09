#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_test_script_imports_resolvable.py — Flag tests that import a
``scripts/<name>.py`` module which has been archived (lives in
``scripts/archive/`` or ``scripts/oneoff/``) or is otherwise
unresolvable.

Motivation (#4464, sibling of #4452)
------------------------------------
The ``check-test-script-imports-mapped.sh`` guard (#4452) verifies that
every top-level ``scripts/*.py`` imported by a scraper-framework test is
covered by the appropriate CI path filter. By design it ignores imports
of *non-existent* / *archived* scripts — those don't fit the
path-filter-mapping invariant the guard codifies. That left a gap:
tests that import scripts in ``scripts/archive/`` (or that name
non-existent scripts via literal ``Path / 'scripts' / '<name>.py'``)
sit in the tree as silent dead weight, fail collection, and only
surface when something else trips them.

Issue #4459 drained the back-catalog of 22 such tests by hand. This
guard exists so the next one cannot re-accrue silently. It complements
``check-test-script-imports-mapped.sh`` — same domain (test files
under ``packages/scraper-framework/tests/``), different invariant.

What the check does
-------------------
1. Walk every ``.py`` file under
   ``packages/scraper-framework/tests/`` (recursively).

2. AST-scan each file for any of three import shapes that NAME a
   ``<scripts>`` module:

       import <name>
       from <name> import ...
       importlib.import_module("<name>")
       importlib.util.spec_from_file_location("<name>", <path-arg>)

   For the first three forms, the candidate name is the leaf module
   name (e.g. ``rebuild_db`` from ``import rebuild_db`` or
   ``from rebuild_db import foo``).

   For the fourth form, the candidate name is the first positional
   string argument. The second arg (the path) is also used: if it is
   a literal ``BinOp`` chain that ends with ``Constant("<name>.py")``
   under ``"scripts"``, the leaf basename is captured as the candidate
   *and* the path is checked for whether it points at an
   archived/oneoff/missing location.

3. For each candidate name resolved by step 2, classify:

       a. resolves to ``scripts/<name>.py``        → OK (live script)
       b. resolves to ``scripts/archive/<name>.py``→ VIOLATION (archived)
       c. resolves to ``scripts/oneoff/<name>.py`` → VIOLATION (one-off)
       d. nowhere on the resolution map           → IGNORED
          (genuine third-party import — `pytest`, `dataclasses`, etc.
          — the guard is intentionally conservative here so it does
          NOT flag e.g. `from datetime import date`. The candidate
          set is built from BOTH the scripts-tree AND the test
          file's `sys.path.insert` hint: if the test file injects
          `scripts/` (or the repo root) onto sys.path AND the candidate
          name matches a basename anywhere under `scripts/**/*.py`,
          we treat it as resolvable for purposes of the check.)

4. Report each violation with the test file's relative path, the
   import line number, the offending name, the resolved
   ``scripts/<sub>/<name>.py`` location, and a copy-pasteable
   ``Fix:`` block per
   ``docs/dx/check-script-fix-block-coverage.md``.

The Fix-block proposes two options:
    1. Re-point the test's ``sys.path.insert`` at
       ``scripts/archive/`` (or ``scripts/oneoff/``) AND move the
       test under ``packages/scraper-framework/tests/archive/`` to
       make the archived intent explicit.
    2. Delete the test file (the canonical resolution of #4459 for
       one-off backfill scripts).

Exit codes
----------
    0 — All clean: no test imports an archived / unresolvable script.
    1 — One or more violations.
    2 — Internal / parse error.

Usage
-----
    scripts/check-test-script-imports-resolvable.sh
    # or directly:
    python3 scripts/check_test_script_imports_resolvable.py
    python3 scripts/check_test_script_imports_resolvable.py --repo-root PATH
    python3 scripts/check_test_script_imports_resolvable.py \\
        --tests-dir packages/scraper-framework/tests \\
        --scripts-dir scripts
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolution map — where a candidate `<name>` can live in the tree
# ---------------------------------------------------------------------------
#
# Three subtrees of `scripts/` are considered:
#   * scripts/<name>.py          — live, runnable script (OK)
#   * scripts/archive/<name>.py  — archived (VIOLATION when a test
#     imports it without explicit archive intent)
#   * scripts/oneoff/<name>.py   — one-off backfill (VIOLATION,
#     same shape as archive)

LIVE_SUBDIR = ""
ARCHIVE_SUBDIR = "archive"
ONEOFF_SUBDIR = "oneoff"

# Resolution categories — the ordered tuple lets us iterate
# deterministically and produce stable output.
CATEGORIES = (
    ("live", LIVE_SUBDIR),
    ("archive", ARCHIVE_SUBDIR),
    ("oneoff", ONEOFF_SUBDIR),
)


# ---------------------------------------------------------------------------
# Build the resolution map: name -> (category, relative-script-path)
# ---------------------------------------------------------------------------


def build_resolution_map(scripts_dir: Path) -> dict[str, tuple[str, str]]:
    """Return ``{name: (category, "scripts/<sub>/<name>.py")}``.

    The map covers every ``*.py`` file directly under ``scripts/``,
    ``scripts/archive/``, and ``scripts/oneoff/``. Subtree precedence is
    live > archive > oneoff: if the same basename exists in multiple
    locations (rare but possible during a half-finished archive move),
    the live entry wins so the guard does NOT spuriously flag a test
    against a transient duplicate.
    """
    out: dict[str, tuple[str, str]] = {}

    # Walk in reverse precedence order so live overwrites archive
    # which overwrites oneoff.
    for category, subdir in reversed(CATEGORIES):
        sub = scripts_dir / subdir if subdir else scripts_dir
        if not sub.is_dir():
            continue
        for f in sorted(sub.iterdir()):
            if not (f.is_file() and f.suffix == ".py"):
                continue
            name = f.stem
            rel = f"scripts/{subdir}/{f.name}" if subdir else f"scripts/{f.name}"
            out[name] = (category, rel)
    return out


# ---------------------------------------------------------------------------
# Violation type
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    test_rel: str  # POSIX rel path under the test package, e.g. "tests/test_x.py"
    test_lineno: int  # 1-indexed line of the offending import / call
    module_name: str  # candidate name as imported (e.g. "dedup_rulings")
    resolved_path: str  # e.g. "scripts/archive/dedup_rulings.py"
    category: str  # "archive" or "oneoff"
    pattern: str  # which AST shape: "import", "from", "import_module", "spec_from_file_location"


# ---------------------------------------------------------------------------
# AST scanner — collect all candidate names in a file with line + pattern
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    name: str  # bare module name as imported
    lineno: int  # 1-indexed line
    pattern: str  # one of: "import", "from", "import_module", "spec_from_file_location"


def _path_constant_leaf(node: ast.AST) -> str | None:
    """Extract the trailing ``"<name>.py"`` literal from a path-construction
    expression like ``_REPO_ROOT / "scripts" / "<name>.py"``.

    Returns ``"<name>"`` (without the .py suffix) on a successful match,
    None otherwise. Does not require the chain to be rooted at any
    particular variable name — only that *some* ``Constant("scripts")``
    appears in the chain and the trailing constant ends with ``.py``.
    """
    # Walk the BinOp chain (operator: ast.Div / "/") and collect string
    # constants. Path / "scripts" / "name.py" parses as
    # BinOp(BinOp(Path, Div, "scripts"), Div, "name.py").
    constants: list[str] = []

    def _walk(n: ast.AST) -> None:
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            constants.append(n.value)
        # Otherwise it's a Name / Attribute / Call (e.g. Path(__file__),
        # parents[N]) — opaque to us; skip.

    _walk(node)

    if not constants:
        return None
    last = constants[-1]
    if not last.endswith(".py"):
        return None
    if "scripts" not in constants[:-1]:
        # Conservative: only treat this as a scripts-tree path if the
        # chain explicitly contains the string "scripts" before the leaf.
        # Path / "data" / "x.py" should NOT be interpreted as a
        # scripts/x.py candidate.
        return None
    return last[:-3]  # strip .py


def detect_explicit_archive_intent(path: Path) -> set[str]:
    """Return the set of archive subtrees the file deliberately injects
    onto ``sys.path``.

    Each element is one of ``"archive"`` / ``"oneoff"``. A test that
    explicitly does ``sys.path.insert(0, ".../scripts/archive")`` (or
    constructs the same path via ``Path / "scripts" / "archive"``) is
    declaring intent to test the archived script — the resolvable check
    suppresses violations against that subtree for that file.

    Detection is conservative: we only treat a ``sys.path.insert`` /
    ``sys.path.append`` call as intent-bearing when (a) its second
    positional arg is a literal ``Path / "scripts" / "<sub>"`` chain or
    a ``str(...)`` wrapper around one, OR (b) it is a string literal
    that ends with ``/scripts/archive`` or ``/scripts/oneoff`` (with
    optional trailing slash), OR (c) it is an ``os.path.join(...)``
    call whose final positional string-constant arg is ``"archive"`` or
    ``"oneoff"`` and whose chain contains ``"scripts"``.

    Anything more dynamic (a variable, a conditional, an inline
    ``os.fspath`` of an unknown call) returns an empty set — the guard
    falls back to flagging the import. The conservative default is
    deliberate: false-positive flagging is recoverable (move the test
    under ``tests/archive/`` or delete it); false-negative silencing
    re-opens the bug class #4459 documents.
    """
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return set()

    intents: set[str] = set()

    def _string_literal_intent(s: str) -> str | None:
        # Strip trailing slash so e.g. ".../scripts/archive/" matches.
        norm = s.rstrip("/").rstrip("\\")
        for sub in (ARCHIVE_SUBDIR, ONEOFF_SUBDIR):
            tail = f"scripts/{sub}"
            tail_win = f"scripts\\{sub}"
            if norm.endswith(tail) or norm.endswith(tail_win):
                return sub
        return None

    def _binop_path_intent(node: ast.AST) -> str | None:
        """``Path(...) / "scripts" / "<sub>"`` chain or wrapped in str()."""
        # Unwrap str(...) one level.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and node.args
        ):
            node = node.args[0]
        constants: list[str] = []

        def _walk(n: ast.AST) -> None:
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
                _walk(n.left)
                _walk(n.right)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                constants.append(n.value)

        _walk(node)
        if "scripts" not in constants:
            return None
        for sub in (ARCHIVE_SUBDIR, ONEOFF_SUBDIR):
            if sub in constants:
                return sub
        return None

    def _ospath_join_intent(node: ast.AST) -> str | None:
        """``os.path.join(..., "scripts", "<sub>")`` with all-string args."""
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "join"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
        ):
            return None
        constants: list[str] = []
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                constants.append(arg.value)
            # else: skip — opaque variable / call.
        if "scripts" not in constants:
            return None
        for sub in (ARCHIVE_SUBDIR, ONEOFF_SUBDIR):
            if sub in constants:
                return sub
        return None

    def _classify_path_arg(node: ast.AST) -> str | None:
        # Plain string literal
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _string_literal_intent(node.value)
        binop = _binop_path_intent(node)
        if binop is not None:
            return binop
        return _ospath_join_intent(node)

    # Build a one-shot alias map of module-level names bound to a path
    # expression we can classify.  This handles the common shape:
    #     _SCRIPTS_ONEOFF_DIR = os.path.join(..., "scripts", "oneoff")
    #     sys.path.insert(0, _SCRIPTS_ONEOFF_DIR)
    # We only walk top-level Assign / AnnAssign bodies to keep the scope
    # tight — variables reassigned inside functions / conditionals are
    # opaque to us and fall back to the conservative "not intent-bearing"
    # default.
    aliases: dict[str, str] = {}
    for stmt in tree.body if isinstance(tree, ast.Module) else []:
        target_names: list[str] = []
        value: ast.AST | None = None
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    target_names.append(tgt.id)
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            target_names = [stmt.target.id]
            value = stmt.value
        if not target_names or value is None:
            continue
        sub = _classify_path_arg(value)
        if sub is None:
            continue
        for name in target_names:
            aliases[name] = sub

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("insert", "append"):
            continue
        # `sys.path.<insert/append>` — chain: Attribute(Attribute(Name("sys"), "path"), <op>)
        outer = func.value
        if not (
            isinstance(outer, ast.Attribute)
            and outer.attr == "path"
            and isinstance(outer.value, ast.Name)
            and outer.value.id == "sys"
        ):
            continue
        # The path-string arg is at index 1 for sys.path.insert(0, X),
        # or index 0 for sys.path.append(X).
        if func.attr == "insert" and len(node.args) >= 2:
            target = node.args[1]
        elif func.attr == "append" and len(node.args) >= 1:
            target = node.args[0]
        else:
            continue
        sub = _classify_path_arg(target)
        if sub is None and isinstance(target, ast.Name):
            sub = aliases.get(target.id)
        if sub:
            intents.add(sub)

    return intents


def collect_candidates(path: Path) -> list[Candidate]:
    """AST-walk ``path`` and return one Candidate per script-name reference.

    Handles four AST shapes:
      * ``import X`` (and ``import scripts.X``)
      * ``from X import Y`` (and ``from scripts.X import Y``)
      * ``importlib.import_module("X")`` (or ``"scripts.X"``)
      * ``importlib.util.spec_from_file_location("X", <path-arg>)``

    For ``spec_from_file_location``, both the first-arg name AND the
    path-arg leaf basename are emitted (where parseable). The path-leaf
    candidate carries pattern ``"spec_from_file_location"`` so the
    reporter can blame the right line.
    """
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []

    out: list[Candidate] = []

    for node in ast.walk(tree):
        # `import X` and `import scripts.X`
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                top = parts[0]
                if top == "scripts" and len(parts) >= 2:
                    out.append(Candidate(parts[1], node.lineno, "import"))
                else:
                    out.append(Candidate(top, node.lineno, "import"))
            continue

        # `from X import Y` and `from scripts.X import Y`
        if isinstance(node, ast.ImportFrom):
            if node.module:
                parts = node.module.split(".")
                top = parts[0]
                if top == "scripts" and len(parts) >= 2:
                    out.append(Candidate(parts[1], node.lineno, "from"))
                else:
                    out.append(Candidate(top, node.lineno, "from"))
            continue

        # importlib.import_module("X") or importlib.util.spec_from_file_location("X", ...)
        if isinstance(node, ast.Call):
            func = node.func
            attr_name = func.attr if isinstance(func, ast.Attribute) else None

            if attr_name == "import_module" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    name = arg.value
                    if name.startswith("scripts."):
                        name = name.split(".", 1)[1]
                    out.append(Candidate(name, node.lineno, "import_module"))

            elif attr_name == "spec_from_file_location":
                # First arg = module name, second arg = path. Both
                # contribute candidates if parseable.
                if node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        out.append(
                            Candidate(
                                first.value, node.lineno, "spec_from_file_location"
                            )
                        )
                if len(node.args) >= 2:
                    leaf = _path_constant_leaf(node.args[1])
                    if leaf is not None:
                        out.append(
                            Candidate(leaf, node.lineno, "spec_from_file_location")
                        )

    return out


# ---------------------------------------------------------------------------
# End-to-end check
# ---------------------------------------------------------------------------


# A tests-tree subdirectory named "archive" is treated as deliberate
# archive-intent: tests living under it are EXEMPTED from the
# resolvable-imports check, because the whole point of putting a test
# under ``tests/archive/`` is to keep an archived script's tests
# alongside the archived script.
ARCHIVE_TESTS_SUBDIR_PARTS = ("archive",)


def _is_under_archive_tests(test_rel: str) -> bool:
    """True if ``test_rel`` lives under ``tests/archive/`` (any depth)."""
    parts = Path(test_rel).parts
    # parts[0] is "tests"; archive-intent lives at parts[1].
    return len(parts) >= 2 and parts[1] in ARCHIVE_TESTS_SUBDIR_PARTS


def check(
    tests_dir: Path,
    scripts_dir: Path,
) -> list[Violation]:
    """Return every ``Violation`` found under ``tests_dir``.

    A test that imports a name resolving to ``scripts/archive/<name>.py``
    or ``scripts/oneoff/<name>.py`` is a violation UNLESS the test
    itself lives under ``tests/archive/``.
    """
    if not tests_dir.is_dir():
        raise ValueError(f"tests dir not found: {tests_dir}")
    if not scripts_dir.is_dir():
        raise ValueError(f"scripts dir not found: {scripts_dir}")

    resolution = build_resolution_map(scripts_dir)
    violations: list[Violation] = []
    package_dir = tests_dir.parent  # packages/scraper-framework/

    for f in sorted(tests_dir.rglob("*.py")):
        try:
            test_rel = f.relative_to(package_dir).as_posix()
        except ValueError:
            test_rel = f.as_posix()

        if _is_under_archive_tests(test_rel):
            continue

        # Did the test deliberately inject scripts/archive/ or
        # scripts/oneoff/ onto sys.path?  If so, imports against that
        # subtree are intentional — suppress matching violations.
        intents = detect_explicit_archive_intent(f)

        for cand in collect_candidates(f):
            mapped = resolution.get(cand.name)
            if mapped is None:
                # Not in any scripts/ subtree — third-party / stdlib.
                continue
            category, rel_path = mapped
            if category == "live":
                continue  # OK
            if category in intents:
                # The test deliberately points sys.path at this subtree;
                # the import is archive-intent, not a stale dead reference.
                continue
            # category in {"archive", "oneoff"}: violation
            violations.append(
                Violation(
                    test_rel=test_rel,
                    test_lineno=cand.lineno,
                    module_name=cand.name,
                    resolved_path=rel_path,
                    category=category,
                    pattern=cand.pattern,
                )
            )

    # De-duplicate (test_rel, module_name, resolved_path) — first occurrence wins.
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Violation] = []
    for v in violations:
        key = (v.test_rel, v.module_name, v.resolved_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(v)
    return deduped


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


def format_violation(v: Violation) -> str:
    return (
        f"  {v.test_rel}:{v.test_lineno}\n"
        f"    imports  {v.module_name}  (via {v.pattern})\n"
        f"    resolves to  {v.resolved_path}  ({v.category})\n"
    )


def format_fix_block(violations: list[Violation]) -> str:
    """Produce a copy-pasteable Fix block per
    ``docs/dx/check-script-fix-block-coverage.md``.

    Two options surfaced — re-point at the archive subtree (and move
    the test under ``tests/archive/``) OR delete the test file
    (the canonical #4459 resolution).
    """
    by_test: dict[str, list[Violation]] = {}
    for v in violations:
        by_test.setdefault(v.test_rel, []).append(v)

    lines: list[str] = []
    lines.append("Fix:")
    lines.append("  Each test file below imports a script that is no longer in")
    lines.append("  top-level scripts/ — it has been moved to scripts/archive/")
    lines.append("  or scripts/oneoff/. Pick one of two options for each test:")
    lines.append("")
    lines.append("  Option 1 — Delete the test (canonical for one-off backfills):")
    lines.append("")
    for test_rel in sorted(by_test):
        lines.append(f"      git rm packages/scraper-framework/{test_rel}")
    lines.append("")
    lines.append("  Option 2 — Move the test under tests/archive/ AND re-point")
    lines.append("  its sys.path.insert at the archive subtree, e.g.:")
    lines.append("")
    lines.append("      git mv packages/scraper-framework/tests/test_<X>.py \\\\")
    lines.append("             packages/scraper-framework/tests/archive/test_<X>.py")
    lines.append("      # then in the moved file:")
    lines.append("      sys.path.insert(0, str(REPO_ROOT / 'scripts' / 'archive'))")
    lines.append("")
    lines.append("  See #4464 (this guard) and #4459 (the back-catalog cleanup).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Flag tests under packages/scraper-framework/tests/ that import a "
            "scripts/<name>.py module which has been archived (lives in "
            "scripts/archive/ or scripts/oneoff/) or is otherwise unresolvable. "
            "Sibling guard to scripts/check-test-script-imports-mapped.sh "
            "(#4452). Tracking issue: #4464."
        )
    )
    ap.add_argument(
        "--repo-root",
        default=None,
        help="Repo root (default: auto-detect from this script's location).",
    )
    ap.add_argument(
        "--tests-dir",
        default=None,
        help="Override tests dir (default: packages/scraper-framework/tests).",
    )
    ap.add_argument(
        "--scripts-dir",
        default=None,
        help="Override scripts dir (default: scripts/ under repo).",
    )
    args = ap.parse_args()

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        repo_root = Path(__file__).resolve().parent.parent

    tests_dir = (
        Path(args.tests_dir).resolve()
        if args.tests_dir
        else repo_root / "packages" / "scraper-framework" / "tests"
    )
    scripts_dir = (
        Path(args.scripts_dir).resolve() if args.scripts_dir else repo_root / "scripts"
    )

    if not tests_dir.is_dir():
        print(f"ERROR: tests dir not found: {tests_dir}", file=sys.stderr)
        return 2
    if not scripts_dir.is_dir():
        print(f"ERROR: scripts dir not found: {scripts_dir}", file=sys.stderr)
        return 2

    try:
        violations = check(tests_dir, scripts_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: check failed: {exc}", file=sys.stderr)
        return 2

    if not violations:
        return 0

    print(
        "check-test-script-imports-resolvable: one or more tests under "
        "packages/scraper-framework/tests/ import a scripts/<name>.py module "
        "that has been archived (scripts/archive/ or scripts/oneoff/) or is "
        "otherwise unresolvable. The test will fail collection in any "
        "environment that runs it.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for v in violations:
        print(format_violation(v), file=sys.stderr)

    print(format_fix_block(violations), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
