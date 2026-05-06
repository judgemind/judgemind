#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_parse_document_reingest_safety.py — AST scanner enforcing the
reingest-hazard docstring marker on Live-only ``parse_document``
implementations under ``packages/scraper-framework/src/courts/``.

Driven by ``scripts/check-parse-document-reingest-safety.sh``.  See that
wrapper for the full motivation, CI integration, and exit codes.

Background
----------
Audit #4046 (``docs/investigations/parse_document-reingest-safety-2026-05.md``)
classified all 20 ``parse_document`` implementations under
``packages/scraper-framework/src/courts/`` against the reingest path
that ``scripts/reingest_from_s3.py::_reparse_document`` exercises.  The
classification rule is mechanical:

* **Reingest-aware** — function body reads ``doc.raw_content`` directly
  OR delegates to ``super().parse_document(...)`` (which itself reads
  raw_content).  The reingest path's fresh ``CapturedDocument``
  (carrying only ``raw_content`` + DB-seeded identifiers) round-trips
  through the parse, so structured fields are re-derived.
* **Mixed** — branches on a discriminant such as
  ``doc.extra.get("pre_split")`` or ``doc.extra.get("_llm_extracted")``.
  The reingest ``cap_doc.extra`` is empty, so the *else* branch (which
  must itself be Reingest-aware) is what runs on reingest.  Mixed
  methods are safe iff their non-discriminant branch reads
  ``raw_content`` or delegates via super.
* **Live-only** — function body is ``return doc`` (or returns ``doc``
  unchanged) with no ``raw_content`` read and no super delegation.
  Live-only ``parse_document`` is a no-op on reingest, which clears
  ``judge_name``/``department``/``parties`` to ``None``/``[]`` via
  ``_reparse_document``'s unconditional-overwrite merge logic (see the
  audit's "How _reparse_document actually consumes parse_document"
  section).

Issue #3986 was the production-shipped example of the Live-only failure
mode (CourtListener returned ``doc`` unchanged on the reingest path,
producing rulings with the JSON envelope decoded as ``ruling_text``).
The runtime ``_TRUNCATION_SENTINEL_LENGTH`` validator caught it after
ship, by chance.  This check is the structural complement: every
Live-only ``parse_document`` MUST carry a docstring marker that
explicitly names the reingest hazard, ensuring no future scraper
silently lands as Live-only with a misleading docstring like the one
#3986 fixed.

Marker contract
---------------
Live-only ``parse_document`` methods MUST contain at least one of these
substrings (case-insensitive) in their docstring:

* ``Reingest hazard``
* ``no-op on the reingest path``
* ``not reingest-safe``

These are the three patterns the audit landed across the three current
Live-only sites (``cc_tentatives_portal.py``, ``sf_civil_tentatives.py``,
``oc_tentatives.py``).  Adding a new marker phrase is fine; adding a
new Live-only ``parse_document`` without one of these markers is a
build break.

What this script does
---------------------
1. AST-parses every ``.py`` file under
   ``packages/scraper-framework/src/courts/`` (recursively).
2. For each ``parse_document`` method (function defined inside a class
   body), classifies it as Live-only or Not-Live-only:
   * Not-Live-only: contains a read of ``X.raw_content`` (where ``X``
     is any ``Name`` or ``Attribute`` chain — typically ``doc``) OR
     contains a call to ``super().parse_document(...)``.
   * Live-only: everything else.
3. For each Live-only method, asserts the docstring contains at least
   one of the marker phrases above.
4. Prints one line per violation in the form
   ``<path>:<lineno>:<class.parse_document>``.

Usage
-----

    python3 scripts/check_parse_document_reingest_safety.py

    # Override scan root (used by the wrapper's --root flag and tests):
    python3 scripts/check_parse_document_reingest_safety.py --root <DIR>

    # Override the marker phrases (one per --marker, used by tests):
    python3 scripts/check_parse_document_reingest_safety.py --marker "Reingest hazard"

Exit codes
----------

  0 — Always.  The wrapper turns the printed-violations stream into a
       non-zero exit, mirroring the convention used by
       ``check_no_redos_pattern.py`` (#4117).  Splitting the
       responsibility keeps this script's stdout the single source of
       truth for tests and the wrapper alike.

Issue
-----
* Audit:    #4046 (``docs/investigations/parse_document-reingest-safety-2026-05.md``)
* Marker enforcement: #4141
* The structural failure shape: #3986 (CourtListener)
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# ─── Default marker phrases ───────────────────────────────────────────────
# Matched case-insensitively against the docstring of every Live-only
# ``parse_document`` method.  At least one must be present.
_DEFAULT_MARKERS: tuple[str, ...] = (
    "reingest hazard",
    "no-op on the reingest path",
    "not reingest-safe",
)


# ─── Live-only classification ─────────────────────────────────────────────
def _reads_raw_content(node: ast.AST) -> bool:
    """Return True if the AST subtree contains an attribute access of
    the form ``X.raw_content`` (where ``X`` is any expression — Name,
    Attribute chain, Call, etc.)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "raw_content":
            return True
    return False


def _delegates_to_other_parse_document(node: ast.AST) -> bool:
    """Return True if the AST subtree contains a call of the form
    ``X.parse_document(...)`` where ``X`` is anything other than the
    bare name ``self``.

    This covers both audit-recognized delegation patterns:

    * ``super().parse_document(doc)`` — e.g. sb_tentatives.py,
      riverside_tentatives.py.  The function attribute is on a
      ``Call(func=Name("super"))`` value, which is not ``Name("self")``.
    * ``parser.parse_document(doc)`` — e.g. sd_pipeline.py, where
      ``parser`` is an instance of another scraper class.  The function
      attribute is on ``Name("parser")``, which is not ``Name("self")``.

    Calls of the form ``self.parse_document(...)`` (recursion) are
    excluded — they don't delegate to a different parser, so they do
    not count toward Reingest-aware classification on their own.
    """
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "parse_document"
        ):
            value = child.func.value
            # Skip self.parse_document(...) — recursion is not delegation.
            if isinstance(value, ast.Name) and value.id == "self":
                continue
            return True
    return False


def _is_live_only(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A ``parse_document`` is Live-only if its body neither reads
    ``raw_content`` nor delegates to another ``parse_document``
    implementation (via ``super()`` or via a stored instance)."""
    if _reads_raw_content(func):
        return False
    if _delegates_to_other_parse_document(func):
        return False
    return True


# ─── Marker check ─────────────────────────────────────────────────────────
def _has_marker(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    markers: tuple[str, ...],
) -> bool:
    """Return True if ``func``'s docstring contains at least one marker
    (case-insensitive substring match)."""
    docstring = ast.get_docstring(func) or ""
    haystack = docstring.lower()
    return any(marker.lower() in haystack for marker in markers)


# ─── File scanning ────────────────────────────────────────────────────────
def _iter_parse_document_methods(
    tree: ast.AST,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield ``(class_name, func_node)`` for every ``parse_document``
    method defined inside a class body.  Module-level functions named
    ``parse_document`` are NOT included — the audit scope is class
    methods on scraper subclasses."""
    results: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "parse_document"
                ):
                    results.append((node.name, item))
    return results


def scan_file(path: Path, markers: tuple[str, ...]) -> list[tuple[Path, int, str]]:
    """Scan one Python file for Live-only ``parse_document`` methods
    missing the marker.  Returns a list of ``(path, lineno, label)``
    violations.  Syntactically broken files are skipped silently
    (mirrors the ``check_no_redos_pattern.py`` convention)."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[Path, int, str]] = []
    for class_name, func in _iter_parse_document_methods(tree):
        if not _is_live_only(func):
            continue
        if _has_marker(func, markers):
            continue
        label = f"{class_name}.parse_document"
        violations.append((path, func.lineno, label))
    return violations


def _iter_python_files(root: Path) -> list[Path]:
    """Walk ``root`` and return every ``.py`` file, excluding common
    vendored / generated directories."""
    excluded_dirs = {".venv", "__pycache__", "node_modules", ".git"}
    files: list[Path] = []
    for child in root.rglob("*.py"):
        # Skip if any path part is in the excluded set.
        if any(part in excluded_dirs for part in child.parts):
            continue
        files.append(child)
    return sorted(files)


# ─── CLI ──────────────────────────────────────────────────────────────────
def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan parse_document methods under "
            "packages/scraper-framework/src/courts/ for missing "
            "reingest-hazard docstring markers (issue #4141)."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "Override the scan root.  Defaults to "
            "<repo>/packages/scraper-framework/src/courts/."
        ),
    )
    parser.add_argument(
        "--marker",
        action="append",
        default=None,
        help=(
            "Override the marker phrases.  Can be repeated.  "
            "Defaults to the three audit-landed markers."
        ),
    )
    return parser.parse_args(argv)


def _resolve_default_root() -> Path:
    """The default scan root is the courts/ tree.  Resolve relative to
    this script's location so the script works regardless of cwd."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    return repo_root / "packages" / "scraper-framework" / "src" / "courts"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    root: Path = args.root or _resolve_default_root()
    markers: tuple[str, ...] = tuple(args.marker) if args.marker else _DEFAULT_MARKERS

    if not root.exists():
        # Silent skip: an empty / missing root produces no violations.
        # This mirrors the behavior of check_no_redos_pattern.py when
        # given a non-existent path.
        return 0

    violations: list[tuple[Path, int, str]] = []
    for py in _iter_python_files(root):
        violations.extend(scan_file(py, markers))

    for path, lineno, label in violations:
        print(f"{path}:{lineno}:{label}")

    # Always exit 0 — the wrapper interprets non-empty stdout as
    # a failure.  See module docstring "Exit codes".
    return 0


if __name__ == "__main__":
    sys.exit(main())
