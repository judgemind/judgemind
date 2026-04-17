#!/usr/bin/env python3
"""Forbid ``try/except Exception: pass`` (and siblings) inside test files.

This check prevents a class of silent test regression: wrapping the call
under test in a bare ``try/except Exception: pass`` makes the test pass
even when the code path never executes successfully.  See #2443 for the
failure-mode analysis and #2459 for the issue proposing this check.

Forbidden patterns (inside any file under ``tests/``):

* ``try: ... except: pass``
* ``try: ... except Exception: pass``
* ``try: ... except BaseException: pass``

Nested ``pass`` statements count (e.g. ``except Exception:\\n    pass``),
but ``except`` handlers that do anything else — log, re-raise, assert —
are fine.

Escape hatch: add ``# noqa: BLE001`` on the ``except`` line to
acknowledge that the swallow is intentional (teardown cleanup, optional
best-effort code, etc.).

Usage:
    scripts/check-test-except-pass.py [PATH ...]

    With no arguments, scans every ``tests/`` directory under ``packages/``
    (this matches the CI invocation).

Exit codes:
    0 — No violations found.
    1 — One or more violations found.

Ref: https://github.com/judgemind/judgemind/issues/2459
"""
# permanent: true

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Default directories to scan when no paths are supplied.
_DEFAULT_DIRS = [
    "packages/scraper-framework/tests",
    "packages/nlp-pipeline/tests",
    "packages/judgemind-config/tests",
]

# ``except`` clause types that count as "blind" for this check.
_BLIND_EXCEPTION_NAMES = {"Exception", "BaseException"}

# Comment that opts a specific ``except`` line out of the check.
_NOQA_MARKER = "# noqa: BLE001"


def _is_pass_only_body(handler: ast.ExceptHandler) -> bool:
    """Return True iff the handler's body is exactly ``pass``.

    ``try/except Foo: pass`` compiles to a handler whose body is a
    single ``ast.Pass`` node.  We treat any other body — a ``log.*``
    call, a ``raise``, an ``assert`` — as non-swallowing.
    """

    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)


def _is_blind_handler(handler: ast.ExceptHandler) -> bool:
    """Return True iff the handler catches everything (or nearly so).

    Matches:
        except:
        except Exception:
        except BaseException:
        except Exception as e:  (still blind — the name binding is cosmetic)
    Ignores narrowed handlers like ``except ValueError:``, which are
    legitimate.
    """

    exc_type = handler.type
    if exc_type is None:
        # Bare ``except:`` — unconditionally blind.
        return True
    if isinstance(exc_type, ast.Name) and exc_type.id in _BLIND_EXCEPTION_NAMES:
        return True
    # ``except (Exception, OSError):`` — treat as blind if ANY element is
    # one of the blind names.  Anything narrower than Exception is still
    # blind when bundled with Exception itself.
    if isinstance(exc_type, ast.Tuple):
        for elt in exc_type.elts:
            if isinstance(elt, ast.Name) and elt.id in _BLIND_EXCEPTION_NAMES:
                return True
    return False


def _line_has_noqa(source_lines: list[str], lineno: int) -> bool:
    """Return True iff the source line at ``lineno`` carries the noqa marker.

    ``lineno`` is 1-indexed (matches ``ast``).  We accept either the marker
    exactly or a longer ``# noqa: BLE001, ...`` list.
    """

    if lineno < 1 or lineno > len(source_lines):
        return False
    line = source_lines[lineno - 1]
    # Accept ``# noqa: BLE001`` and ``# noqa: BLE001, XYZ`` etc.
    return "# noqa: BLE001" in line or "# noqa:BLE001" in line


def find_violations(filepath: Path) -> list[tuple[int, str]]:
    """Parse a Python file and return ``(lineno, snippet)`` for each violation.

    ``snippet`` is the offending ``except`` source line (stripped), useful
    for the human-readable error message.
    """

    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        # Broken files are CI's job to report via ruff/pytest, not ours.
        return []

    source_lines = source.splitlines()
    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if not _is_blind_handler(handler):
                continue
            if not _is_pass_only_body(handler):
                continue
            if _line_has_noqa(source_lines, handler.lineno):
                continue
            snippet_line = source_lines[handler.lineno - 1].strip() if handler.lineno - 1 < len(source_lines) else ""
            violations.append((handler.lineno, snippet_line))

    return violations


def iter_test_files(paths: list[Path]) -> list[Path]:
    """Yield ``.py`` files inside any ``tests/`` directory under ``paths``.

    Files outside a ``tests/`` directory are ignored even if passed
    directly, so the check can be safely pointed at a package root.
    """

    files: list[Path] = []
    for root in paths:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix == ".py" and "tests" in root.parts:
                files.append(root)
            continue
        for candidate in root.rglob("*.py"):
            if "tests" in candidate.parts:
                files.append(candidate)
    return files


def main(argv: list[str]) -> int:
    if argv:
        paths = [Path(p) for p in argv]
    else:
        paths = [Path(p) for p in _DEFAULT_DIRS]

    files = iter_test_files(paths)
    violations: list[tuple[Path, int, str]] = []
    for fp in files:
        for lineno, snippet in find_violations(fp):
            violations.append((fp, lineno, snippet))

    if not violations:
        return 0

    print(
        "ERROR: Forbidden `try/except ...: pass` pattern in test file(s):",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for fp, lineno, snippet in violations:
        print(f"  {fp}:{lineno}: {snippet}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Blindly swallowing exceptions in tests makes them silently pass on a "
        "never-executed code path (see #2443).",
        file=sys.stderr,
    )
    print(
        "If the swallow is truly necessary (teardown, best-effort cleanup), "
        f"append `{_NOQA_MARKER}` to the `except` line.",
        file=sys.stderr,
    )
    print(
        "See docs/agent/code-standards.md §Pre-PR Checks for details.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
