#!/usr/bin/env python3
"""Remove test functions and classes from Python files using AST-based boundary detection.

Instead of error-prone line-range deletion, this script uses Python's AST to
properly identify function and class boundaries — including decorators — and
removes only the specified items while preserving all surrounding code.

Usage:
    scripts/remove_test_functions.py <file> --names func1 func2 ClassName ClassName.method

    # Dry run (show what would be removed without modifying the file):
    scripts/remove_test_functions.py --dry-run <file> --names func1

    # Dotted names remove methods/nested classes within a parent class:
    scripts/remove_test_functions.py <file> --names TestSuite.test_old_method

Exit codes:
    0 — Removal successful (or dry-run completed).
    1 — Error (e.g., target name not found, syntax error in input).

Ref: https://github.com/judgemind/judgemind/issues/2320
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def _find_decorator_start(
    lines: list[str],
    node: _DefOrClass,
) -> int:
    """Find the first line of a node including its decorators.

    Uses the AST ``decorator_list`` to get the earliest decorator line number,
    then walks backwards from that line to capture any multi-line decorator
    arguments that span above the ``@`` symbol.

    Args:
        lines: The full source split into lines (0-indexed).
        node: The AST node whose start line (including decorators) to find.

    Returns:
        1-based line number of the first decorator (or the node's own ``lineno``
        if it has no decorators).
    """
    if not node.decorator_list:
        return node.lineno

    # The AST gives us the line of each decorator's expression.  The first
    # decorator in the list is the topmost one.  Its ``lineno`` points to the
    # ``@`` line.
    earliest_lineno = min(d.lineno for d in node.decorator_list)

    # Now walk backwards from the ``@`` line to capture any preceding lines
    # that are part of the same multi-line decorator (shouldn't normally happen
    # for the topmost ``@`` line, but be safe).
    idx = earliest_lineno - 2  # 0-based index of the line above the ``@``
    indent = len(lines[earliest_lineno - 1]) - len(lines[earliest_lineno - 1].lstrip())

    while idx >= 0:
        line = lines[idx]
        stripped = line.lstrip()

        # Skip blank lines
        if not stripped:
            idx -= 1
            continue

        # Another decorator at the same indentation — include it
        line_indent = len(line) - len(stripped)
        if stripped.startswith("@") and line_indent == indent:
            earliest_lineno = idx + 1
            idx -= 1
            continue

        # Anything else — we've gone past all decorators
        break

    return earliest_lineno


def _node_end_lineno(node: ast.AST) -> int:
    """Return the end line number for an AST node (1-based).

    Python 3.8+ provides ``end_lineno`` on most statement nodes.
    """
    end = getattr(node, "end_lineno", None)
    if end is not None:
        return end
    # Fallback: shouldn't happen on Python 3.12+, but be safe.
    return getattr(node, "lineno", 0)


_DefOrClass = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _resolve_dotted_name(
    tree: ast.Module,
    dotted: str,
) -> tuple[_DefOrClass, str]:
    """Resolve a dotted name like ``ClassName.method`` to an AST node.

    Returns:
        A tuple of (node, simple_name) where *node* is the AST node to remove
        and *simple_name* is the leaf name (for error messages).

    Raises:
        ValueError: If the name cannot be found.
    """
    parts = dotted.split(".")
    if len(parts) == 1:
        # Top-level function or class
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == parts[0]:
                    return node, parts[0]
        msg = f"Name not found at module level: {dotted}"
        raise ValueError(msg)

    if len(parts) == 2:  # noqa: PLR2004 — exactly two parts
        parent_name, child_name = parts
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and node.name == parent_name:
                for child in ast.iter_child_nodes(node):
                    if isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        if child.name == child_name:
                            return child, dotted
                msg = f"Name not found in class {parent_name}: {child_name}"
                raise ValueError(msg)
        msg = f"Class not found at module level: {parent_name}"
        raise ValueError(msg)

    msg = f"Names deeper than ClassName.method are not supported: {dotted}"
    raise ValueError(msg)


def find_node_ranges(
    source: str,
    names: list[str],
) -> list[tuple[int, int]]:
    """Find the line ranges (1-based, inclusive) for the given function/class names.

    Each range includes any decorators above the ``def``/``class`` statement.

    Args:
        source: Python source code as a string.
        names: List of names to find.  Dotted names (e.g. ``ClassName.method``)
            are supported for removing methods within a class.

    Returns:
        A list of ``(start_line, end_line)`` tuples (1-based, inclusive),
        sorted by start_line.

    Raises:
        ValueError: If any name is not found in the source.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    ranges: list[tuple[int, int]] = []

    for name in names:
        node, _display = _resolve_dotted_name(tree, name)
        start = _find_decorator_start(lines, node)
        end = _node_end_lineno(node)
        ranges.append((start, end))

    ranges.sort(key=lambda r: r[0])
    return ranges


def _collapse_blank_lines(text: str, max_consecutive: int = 2) -> str:
    """Collapse runs of blank lines to at most *max_consecutive*."""
    result_lines: list[str] = []
    blank_count = 0
    for line in text.splitlines(keepends=True):
        if line.strip() == "":
            blank_count += 1
            if blank_count <= max_consecutive:
                result_lines.append(line)
        else:
            blank_count = 0
            result_lines.append(line)
    return "".join(result_lines)


def _ensure_class_body(source: str) -> str:
    """If removing all methods from a class left it empty, insert ``pass``.

    An empty class body is a syntax error.  We detect this by re-parsing and
    checking for ClassDef nodes with an empty body — which ``ast.parse`` cannot
    produce, so we look for the pattern in the raw text instead.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # If the source is already broken, try to fix empty class bodies
        # by scanning for class headers followed by no indented content.
        lines = source.splitlines(keepends=True)
        fixed_lines: list[str] = []
        i = 0
        while i < len(lines):
            fixed_lines.append(lines[i])
            stripped = lines[i].lstrip()
            if stripped.startswith("class ") and stripped.rstrip().endswith(":"):
                indent = len(lines[i]) - len(stripped)
                # Check if the next non-blank line is at the same or lesser indent
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                needs_pass = False
                if j >= len(lines):
                    needs_pass = True
                else:
                    next_indent = len(lines[j]) - len(lines[j].lstrip())
                    if next_indent <= indent:
                        needs_pass = True
                if needs_pass:
                    fixed_lines.append(" " * (indent + 4) + "pass\n")
            i += 1
        return "".join(fixed_lines)

    # Source parses fine — check for empty class bodies that somehow slipped through.
    # (This shouldn't happen because ast.parse would fail, but be defensive.)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not node.body:
            # This shouldn't be reachable, but just in case:
            pass
    return source


def remove_functions(
    source: str,
    names: list[str],
) -> str:
    """Remove the specified functions/classes from *source* and return the result.

    Args:
        source: Python source code as a string.
        names: Names to remove (may include dotted names for nested items).

    Returns:
        The modified source code with the specified items removed.  The output
        is guaranteed to pass ``ast.parse()``.

    Raises:
        ValueError: If any name is not found or the result fails validation.
    """
    ranges = find_node_ranges(source, names)
    lines = source.splitlines(keepends=True)

    # Build a set of line numbers to remove (1-based).
    remove_lines: set[int] = set()
    for start, end in ranges:
        for lineno in range(start, end + 1):
            remove_lines.add(lineno)

    # Reconstruct the source, skipping removed lines.
    kept_lines = [
        line for i, line in enumerate(lines, start=1) if i not in remove_lines
    ]
    result = "".join(kept_lines)

    # Clean up excessive blank lines left by removal.
    result = _collapse_blank_lines(result)

    # Fix empty class bodies.
    result = _ensure_class_body(result)

    # Ensure file ends with a newline.
    if result and not result.endswith("\n"):
        result += "\n"

    # Validate the output.
    try:
        ast.parse(result)
    except SyntaxError as exc:
        msg = f"Output failed syntax validation: {exc}"
        raise ValueError(msg) from exc

    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    parser = argparse.ArgumentParser(
        description="Remove test functions/classes from a Python file using AST.",
    )
    parser.add_argument(
        "file",
        type=Path,
        help="Path to the Python file to modify.",
    )
    parser.add_argument(
        "--names",
        nargs="+",
        required=True,
        help="Names of functions/classes to remove (supports ClassName.method).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without modifying the file.",
    )
    args = parser.parse_args(argv)

    filepath: Path = args.file
    if not filepath.is_file():
        print(f"Error: {filepath} is not a file.", file=sys.stderr)
        return 1

    source = filepath.read_text(encoding="utf-8")

    try:
        result = remove_functions(source, args.names)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Show what would be removed.
        ranges = find_node_ranges(source, args.names)
        lines = source.splitlines()
        for name, (start, end) in zip(args.names, ranges):
            print(f"Would remove '{name}': lines {start}-{end}")
            for lineno in range(start, end + 1):
                print(f"  {lineno:4d} | {lines[lineno - 1]}")
        return 0

    filepath.write_text(result, encoding="utf-8")
    removed_count = len(args.names)
    print(f"Removed {removed_count} item(s) from {filepath}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
