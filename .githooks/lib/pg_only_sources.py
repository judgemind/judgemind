#!/usr/bin/env python3
"""List the source files a package's real-Postgres tests import.

Used by .githooks/pre-push when it cannot get a local Postgres for a
package whose tests read TEST_DATABASE_URL (#4950). Those tests then
skip, so lines that only they reach show as uncovered. Rather than fail
the push on that, or pass it without a word, the hook leaves these files
out of its diff-coverage gate and prints which ones it left out. CI runs
the Postgres tests and checks diff coverage on every file.

Usage:
    pg_only_sources.py <package-dir>

A "Postgres test" is a .py file under <package-dir>/tests that mentions
TEST_DATABASE_URL. For each one, every import (module level or inside a
function) that resolves to a file under <package-dir>/src is listed.
Imports of other test modules (e.g. tests/split_harness.py) are
followed, so the sources those helpers import are listed too. Imports of
src modules are not followed: only files a Postgres test or helper
imports directly are listed.

Prints absolute paths, one per line, sorted. diff-cover --exclude matches
an absolute pattern against the file's absolute path.

Exit codes:
    0 - success (possibly nothing printed)
    2 - usage error
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MARKER = "TEST_DATABASE_URL"


def _module_file(root: Path, dotted: str) -> Path | None:
    """<root>/a/b.py or <root>/a/b/__init__.py for dotted name a.b."""
    base = root.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_names(tree: ast.AST) -> set[str]:
    """Dotted module names an AST imports, including from-import submodules."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            for alias in node.names:
                if alias.name != "*":
                    names.add(f"{node.module}.{alias.name}")
    return names


def pg_only_sources(pkg_dir: Path) -> list[Path]:
    src = pkg_dir / "src"
    tests = pkg_dir / "tests"
    if not tests.is_dir():
        return []

    queue = [
        p
        for p in sorted(tests.rglob("*.py"))
        if MARKER in p.read_text(errors="replace")
    ]
    seen_tests: set[Path] = set()
    sources: set[Path] = set()
    while queue:
        test_file = queue.pop()
        if test_file in seen_tests:
            continue
        seen_tests.add(test_file)
        try:
            tree = ast.parse(test_file.read_text(errors="replace"), str(test_file))
        except SyntaxError:
            continue
        for dotted in _imported_names(tree):
            found = _module_file(src, dotted) if src.is_dir() else None
            if found is not None:
                sources.add(found.resolve())
                continue
            # A test helper: next to the importing file, under tests/, or
            # named from the package root (tests.helpers.x).
            for root in (test_file.parent, tests, pkg_dir):
                helper = _module_file(root, dotted)
                if helper is not None and tests in helper.resolve().parents:
                    queue.append(helper.resolve())
                    break
    return sorted(sources)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    pkg_dir = Path(argv[1]).resolve()
    for path in pg_only_sources(pkg_dir):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
