#!/usr/bin/env python3
"""Detect duplicate top-level function/class definitions within Python modules.

ruff's F811 rule catches redefinitions of unused imports but does NOT detect
duplicate function definitions at module scope.  This script fills that gap
by parsing Python files with the ast module and flagging any module-level
function or class that is defined more than once in the same file.

Usage:
    scripts/check-duplicate-functions.py [DIRECTORY ...]

    If no directories are given, defaults to scanning all packages/*/src/ and
    packages/*/tests/ directories.

Exit codes:
    0 — No duplicates found.
    1 — One or more duplicate definitions found.

Ref: https://github.com/judgemind/judgemind/issues/1300
"""
# permanent: true
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Default directories to scan (relative to repo root).
_DEFAULT_DIRS = [
    "packages/scraper-framework/src",
    "packages/scraper-framework/tests",
    "packages/nlp-pipeline/src",
    "packages/nlp-pipeline/tests",
    "packages/judgemind-config/src",
    "packages/judgemind-config/tests",
]


def find_duplicate_functions(filepath: Path) -> list[tuple[str, list[int]]]:
    """Parse a Python file and return duplicate top-level function/class names.

    Returns a list of (name, [line_numbers]) for each name defined more than
    once at module scope.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    # Collect top-level function and class definitions.
    definitions: dict[str, list[int]] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.setdefault(node.name, []).append(node.lineno)

    return [(name, lines) for name, lines in definitions.items() if len(lines) > 1]


def scan_directory(directory: Path) -> list[tuple[Path, str, list[int]]]:
    """Scan all .py files in a directory tree for duplicate definitions."""
    results: list[tuple[Path, str, list[int]]] = []
    if not directory.is_dir():
        return results
    for pyfile in sorted(directory.rglob("*.py")):
        for name, lines in find_duplicate_functions(pyfile):
            results.append((pyfile, name, lines))
    return results


def main() -> int:
    """Entry point."""
    # Determine repo root: this script lives in scripts/ at the repo root.
    repo_root = Path(__file__).resolve().parent.parent

    if len(sys.argv) > 1:
        dirs = [Path(d) for d in sys.argv[1:]]
    else:
        dirs = [repo_root / d for d in _DEFAULT_DIRS]

    all_duplicates: list[tuple[Path, str, list[int]]] = []
    for directory in dirs:
        all_duplicates.extend(scan_directory(directory))

    if not all_duplicates:
        print("No duplicate function/class definitions found.")
        return 0

    print("Duplicate function/class definitions found:\n")
    for filepath, name, lines in all_duplicates:
        # Show path relative to repo root for readability.
        try:
            rel = filepath.relative_to(repo_root)
        except ValueError:
            rel = filepath
        line_list = ", ".join(str(ln) for ln in lines)
        print(f"  {rel}: '{name}' defined at lines {line_list}")

    print(f"\n{len(all_duplicates)} duplicate(s) found. "
          "Remove or rename the duplicate definitions.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
