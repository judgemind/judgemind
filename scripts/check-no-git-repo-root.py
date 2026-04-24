#!/usr/bin/env python3
"""Guard against ``git -C self._repo_root()`` in ``scripts/dispatcher/daemon.py``.

Why this check exists
---------------------
Issues #2804 and #2821 traced two production failures to the same root cause:
a ``subprocess.run(["git", "-C", str(self._repo_root()), ...])`` call in
the daemon.  On the Fargate deployment, ``self._repo_root()`` returns ``/app``
— the image's CWD — which has no ``.git`` child.  Every such ``git`` command
fails with "not a git repository" and, in some cases, corrupted worktree
state.  The correct anchor for git operations is ``self._git_parent_root()``
(returns the baseline clone at ``/var/lib/dispatcher/judgemind`` when
``DaemonConfig.baseline_repo_root`` is set, and falls back to ``_repo_root()``
in local-dev mode where both happen to be the same path).

The ``_repo_root()`` docstring already warns about this.  This guard makes the
warning machine-enforceable: it fails CI / pre-push when any
``subprocess.run(["git", ...])`` call site has ``self._repo_root()`` (or
``str(self._repo_root())``) anywhere in its argv list.  Non-``git`` subprocess
calls are out of scope (the bug class is git-worktree-specific per #2804/#2821).

Rule
----
For every ``subprocess.run(...)`` call in ``scripts/dispatcher/daemon.py``
whose first argv element is the string literal ``"git"``, NONE of the
remaining argv elements may resolve to a ``self._repo_root()`` call (with or
without an outer ``str()`` wrapping).

Scope
-----
- Only scans daemon.py (the sole file with this pattern).  ``scripts/dispatcher/``
  sibling modules have no ``self._repo_root()`` usages.
- Only flags ``"git"`` subprocesses; ``"gh"`` subprocesses are out of scope.
- AST-based to avoid false-positives from docstrings and comments.

Usage
-----
    scripts/check-no-git-repo-root.py                         # scan daemon.py
    scripts/check-no-git-repo-root.py [file]                  # scan a specific file

Exit codes
----------
    0 — No violations found (or target file absent).
    1 — One or more violations found.

Ref: issues #2804, #2821, #2829.
"""
# permanent: true

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_TARGET = _REPO_ROOT / "scripts" / "dispatcher" / "daemon.py"


def _is_repo_root_call(node: ast.expr) -> bool:
    """Return True if ``node`` is ``self._repo_root()`` or ``str(self._repo_root())``.

    Accepted shapes:
        self._repo_root()
        str(self._repo_root())
    """
    # Direct call: self._repo_root()
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_repo_root"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ):
        return True

    # str() wrapper: str(self._repo_root())
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and not node.keywords
    ):
        return _is_repo_root_call(node.args[0])

    return False


def _first_argv_is_git(argv_node: ast.expr) -> bool:
    """Return True if the first element of the argv list is the string ``"git"``."""
    if not isinstance(argv_node, ast.List):
        return False
    if not argv_node.elts:
        return False
    first = argv_node.elts[0]
    return isinstance(first, ast.Constant) and first.value == "git"


def _argv_contains_repo_root(argv_node: ast.expr) -> int | None:
    """Return the line number of the first offending element, or None.

    Walks every element in the argv list (skipping the first ``"git"`` element)
    and returns the line number of the first element that is a
    ``self._repo_root()`` call (directly or wrapped in ``str()``).
    """
    if not isinstance(argv_node, ast.List):
        return None
    for elt in argv_node.elts[1:]:
        if _is_repo_root_call(elt):
            return elt.lineno
    return None


def find_violations(filepath: Path) -> list[tuple[int, str]]:
    """Parse ``filepath`` and return ``(lineno, snippet)`` for each violation.

    Walks every ``subprocess.run(...)`` call node.  For those whose first
    positional argument is a list starting with ``"git"``, checks whether
    any subsequent list element resolves to ``self._repo_root()``.

    Returns an empty list on parse / IO errors (ruff/pytest will catch those).
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Match ``subprocess.run(...)`` — both ``subprocess.run`` (Attribute)
        # and bare ``run(...)`` (Name) shapes.
        func = node.func
        is_subprocess_run = (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ) or (isinstance(func, ast.Name) and func.id == "run")

        if not is_subprocess_run:
            continue

        # The argv list must be the first positional argument.
        if not node.args:
            continue
        argv = node.args[0]

        if not _first_argv_is_git(argv):
            continue

        offending_lineno = _argv_contains_repo_root(argv)
        if offending_lineno is None:
            continue

        # Build a compact snippet from the offending source line.
        snippet = ""
        if 1 <= offending_lineno <= len(source_lines):
            snippet = source_lines[offending_lineno - 1].strip()

        violations.append((offending_lineno, snippet))

    return violations


def main(argv: list[str]) -> int:
    if argv:
        target = Path(argv[0])
    else:
        target = _DEFAULT_TARGET

    if not target.is_file():
        print(
            f"check-no-git-repo-root: no target file at {target} — nothing to check."
        )
        return 0

    violations = find_violations(target)

    if not violations:
        print(
            f"check-no-git-repo-root: {target} — no git subprocess "
            "anchored to self._repo_root() found."
        )
        return 0

    print("ERROR: git subprocess call(s) anchored to self._repo_root().", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  Target: {target}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  self._repo_root() returns os.getcwd() — on the Fargate deployment",
        file=sys.stderr,
    )
    print(
        "  that is /app, which has no .git child. Every `git -C /app ...`",
        file=sys.stderr,
    )
    print(
        '  fails with "not a git repository" (#2804, #2821).',
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("  Violations:", file=sys.stderr)
    print("", file=sys.stderr)
    for lineno, snippet in violations:
        print(f"    {target}:{lineno}: {snippet}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  Fix: replace self._repo_root() with self._git_parent_root() in",
        file=sys.stderr,
    )
    print(
        "  the git subprocess argv.  self._git_parent_root() returns the",
        file=sys.stderr,
    )
    print(
        "  baseline clone path when baseline_repo_root is configured, and",
        file=sys.stderr,
    )
    print(
        "  falls back to _repo_root() in local-dev / unit-test mode — so",
        file=sys.stderr,
    )
    print(
        "  the call is correct in both environments. See #2829.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
