#!/usr/bin/env bash
# check-no-git-repo-root-anchor.sh — Every ``subprocess.run([... "git" ...])``
# call site in ``scripts/dispatcher/daemon.py`` whose first positional arg
# resolves to a list literal beginning with ``"git"`` AND containing
# ``self._repo_root()`` (directly, wrapped in ``str()``, or ``Path()``)
# is a bug: the correct anchor for git operations is
# ``self._git_parent_root()``.
#
# Why this check exists
# ---------------------
# In Fargate mode, ``_repo_root()`` returns ``/app`` (the container image
# mount), which has no ``.git`` directory. Any ``git -C <repo_root> ...``
# subprocess silently fails or operates on the wrong repo. The right anchor
# is ``_git_parent_root()``, which falls back to ``_repo_root()`` only in
# local-dev / unit-test mode.
#
# Non-git subprocesses (cleanup-script lookups, tmp-dir construction)
# legitimately use ``_repo_root()`` — this check only flags list literals
# whose first element is ``"git"``.
#
# Issue: #2829.
#
# Rule
# ----
# Every ``subprocess.run(...)`` call site in ``scripts/dispatcher/daemon.py``
# whose first positional arg resolves to a list literal that BOTH:
#   (a) begins with ``"git"``, AND
#   (b) contains ``self._repo_root()`` anywhere (directly or via str()/Path())
# is a violation.
#
# Name-bound forms (``cmd = ["git", "-C", str(self._repo_root()), ...]``)
# are also resolved and checked.
#
# Usage
# -----
#   scripts/check-no-git-repo-root-anchor.sh            # scan the real daemon.py
#   scripts/check-no-git-repo-root-anchor.sh [file]     # scan a specific file (tests)
#
# Exit codes
# ----------
#   0 — No violations found.
#   1 — At least one git subprocess anchors at self._repo_root().

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_FILE="${1:-$REPO_ROOT/scripts/dispatcher/daemon.py}"

# ─── Fast exit if the target file does not exist ─────────────────────────
if [[ ! -f "$TARGET_FILE" ]]; then
    echo "check-no-git-repo-root-anchor: no target file at $TARGET_FILE — nothing to check."
    exit 0
fi

# ─── Locate violating call sites via embedded Python AST walker ──────────
# The walker:
#   1. Finds all subprocess.run(...) call nodes.
#   2. Resolves the first positional arg: either a direct list literal, or
#      a name-bound list literal from an Assign in the same function scope.
#   3. Checks if the list's first element is "git".
#   4. Checks if any element of the list calls self._repo_root() —
#      directly (as an Attribute call on Self), or wrapped in str()/Path().
#   5. Emits "<lineno>:<snippet>" for each violating call site.
#
# Docstrings and comments are excluded because AST parsing only sees
# executable code — string literals not assigned or passed anywhere
# are not visited as Call nodes.

python_output="$(python3 - "$TARGET_FILE" <<'PYEOF'
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    source = path.read_text()
    tree = ast.parse(source)
except SyntaxError as exc:
    # Not valid Python — skip silently (the file may be a fixture
    # or a stub the check doesn't need to flag).
    sys.exit(0)

lines = source.splitlines()


def _literal_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _list_first_literal(node):
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    if not node.elts:
        return None
    return _literal_str(node.elts[0])


def _enclosing_func(line_no):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= line_no <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


def _resolve_name(name, before_line):
    """Resolve ``name`` to its most recent list-literal Assign within
    the enclosing function scope."""
    scope = _enclosing_func(before_line)
    if scope is None:
        return None
    best = None
    for sub in ast.walk(scope):
        if isinstance(sub, ast.Assign) and sub.lineno < before_line:
            for target in sub.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(sub.value, (ast.List, ast.Tuple)):
                        best = sub.value
    return best


def _is_repo_root_call(node):
    """Return True if ``node`` is self._repo_root(), str(self._repo_root()),
    or Path(self._repo_root())."""
    # Direct: self._repo_root()
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_repo_root"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ):
        return True
    # Wrapped: str(self._repo_root()) or Path(self._repo_root())
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("str", "Path")
        and len(node.args) == 1
    ):
        inner = node.args[0]
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "_repo_root"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "self"
        ):
            return True
    return False


def _list_contains_repo_root(list_node):
    """Return True if any element of the list literal is or wraps
    self._repo_root()."""
    for elt in list_node.elts:
        if _is_repo_root_call(elt):
            return True
    return False


for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    ):
        continue
    if not node.args:
        continue
    first = node.args[0]
    cmd_list = None
    if isinstance(first, (ast.List, ast.Tuple)):
        cmd_list = first
    elif isinstance(first, ast.Name):
        resolved = _resolve_name(first.id, node.lineno)
        if resolved is not None:
            cmd_list = resolved

    if cmd_list is None:
        continue

    # Only flag lists whose first element is the string "git"
    if _list_first_literal(cmd_list) != "git":
        continue

    # Flag if any element is or wraps self._repo_root()
    if _list_contains_repo_root(cmd_list):
        snippet = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
        print(f"{node.lineno}:{snippet}")
PYEOF
)"

# ─── Report violations ───────────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    exit 0
fi

violations=0
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    violations=$((violations + 1))
done <<< "$python_output"

if (( violations > 0 )); then
    echo "ERROR: git subprocess.run call site(s) anchor at self._repo_root() in $TARGET_FILE."
    echo ""
    echo "  In Fargate mode, _repo_root() returns /app (no .git directory)."
    echo "  Git operations must anchor at _git_parent_root() instead, which"
    echo "  returns the correct baseline-clone path in production and falls"
    echo "  back to _repo_root() in local-dev / unit-test mode."
    echo ""
    echo "  Fix: replace self._repo_root() with self._git_parent_root()"
    echo "  (or a pre-bound local variable from self._git_parent_root())."
    echo ""
    echo "  See issue #2829 for context."
    echo ""
    echo "  Violating sites:"
    echo ""
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        lineno="${entry%%:*}"
        snippet="${entry#*:}"
        echo "    $TARGET_FILE:${lineno}: ${snippet}"
    done <<< "$python_output"
    echo ""
    exit 1
fi

exit 0
