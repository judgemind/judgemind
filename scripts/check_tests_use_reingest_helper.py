#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_tests_use_reingest_helper.py — AST scanner enforcing the use of
the shared ``make_reingest_cap_doc`` helper for reingest-shape regression
tests under ``packages/scraper-framework/tests/courts/``.

Driven by ``scripts/check-tests-use-reingest-helper.sh``.  See that
wrapper for the full motivation, CI integration, and exit codes.

Background
----------
Audit #4046 (``docs/investigations/parse_document-reingest-safety-2026-05.md``)
established that production scrapers' ``parse_document`` methods MUST
populate every relevant field from ``raw_content`` alone, because
``scripts/reingest_from_s3.py::_reparse_document`` constructs a fresh
``CapturedDocument`` carrying only ``raw_content`` + identifier fields
and clears all parsed fields it does not re-derive.

Issue #4153 introduced ``packages/scraper-framework/tests/helpers/reingest.py``
exporting ``make_reingest_cap_doc(...)`` — a shared helper that
constructs exactly that reingest-shape ``CapturedDocument``.  The
production-side guard (``scripts/check-parse-document-reingest-safety.sh``
from #4141) enforces that Live-only ``parse_document`` implementations
carry a docstring marker about the reingest hazard.  This check is the
test-side analog: it enforces that any new reingest-path regression
test under ``tests/courts/`` uses the shared helper instead of
constructing the cap_doc inline.

Three migrations took place because of structural drift:

* #4153 (the helper) + ``test_courtlistener.py`` migrated in #4153
* ``test_cc_tentatives_portal.py`` (#4133) migrated in #4153
* ``test_sf_civil_tentatives.py`` (#4134) migrated in #4165

#4134 had to be re-migrated in #4165 because it landed *before* #4153
and built its own inline ``CapturedDocument(...)`` constructions.  The
audit in #4046 anticipated this loop, but there was no CI guard
preventing the next scraper from doing the same thing.  This check
closes that loop.

What "reingest-shape" means
---------------------------
A ``CapturedDocument(...)`` call is **reingest-shape** when its
keyword-argument set is a *superset* of the identifier-fields set:

    {document_id, scraper_id, state, county, court, source_url,
     capture_timestamp, content_format, raw_content, content_hash}

AND it does NOT pass any of the parsed-field set (mirroring
``_PARSE_POPULATED_SCALAR_FIELDS`` from ``helpers/reingest.py``):

    {case_number, case_title, judge_name, hearing_date, ruling_text,
     ruling_text_html, outcome, motion_type, parties, extra,
     courthouse, department}

The "superset" rule lets test cases override identifier-default values
(e.g. ``content_format=ContentFormat.PDF``) without tripping the check.
The "no parsed fields" rule is what distinguishes a reingest-shape
construction from a fully-populated cap_doc — once a test passes
``case_number=`` or ``hearing_date=``, it is exercising a different
path than the reingest one and the helper does not apply.

If the call passes ``**kwargs`` (a starred-keyword-arg) we cannot
classify it — we conservatively skip such calls (no violation).

What this script does
---------------------
1. AST-parses every ``.py`` file under
   ``packages/scraper-framework/tests/courts/`` (recursively).
2. For each ``ast.Call`` whose ``func`` is a ``Name`` or ``Attribute``
   ending in ``CapturedDocument``, classifies the call shape.
3. For each reingest-shape inline call, prints one line in the form
   ``<path>:<lineno>:CapturedDocument(...)``.

Usage
-----

    python3 scripts/check_tests_use_reingest_helper.py

    # Override scan root (used by the wrapper's --root flag and tests):
    python3 scripts/check_tests_use_reingest_helper.py --root <DIR>

Exit codes
----------

  0 — Always.  The wrapper turns the printed-violations stream into a
       non-zero exit, mirroring the convention used by
       ``check_parse_document_reingest_safety.py`` (#4141) and
       ``check_no_redos_pattern.py`` (#4117).

Issue
-----
* This guard:           #4190
* Helper:               #4153
* Migrated tests:       #4153, #4133, #4165
* Production analog:    #4141 (audit #4046)
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# ─── Field-set contract ───────────────────────────────────────────────────
# Identifier fields the reingest helper (helpers/reingest.py) sets.  A
# reingest-shape call must pass at least these — i.e. its keywords are
# a superset of this set.  Mirrors the constructor argument list of
# ``make_reingest_cap_doc`` (the helper) and ``_reparse_document`` (the
# production reingest path in ``scripts/reingest_from_s3.py``).
_IDENTIFIER_FIELDS: frozenset[str] = frozenset(
    {
        "document_id",
        "scraper_id",
        "state",
        "county",
        "court",
        "source_url",
        "capture_timestamp",
        "content_format",
        "raw_content",
        "content_hash",
    }
)

# Parsed fields the reingest helper leaves at default.  A reingest-shape
# call MUST NOT pass any of these — once any parsed field is set, the
# call is exercising a non-reingest code path.  Mirrors
# ``_PARSE_POPULATED_SCALAR_FIELDS`` plus the list/dict fields
# (``parties``, ``extra``) and the courthouse/department geographic
# fields, per the issue #4190 spec.
_PARSED_FIELDS: frozenset[str] = frozenset(
    {
        "case_number",
        "case_title",
        "judge_name",
        "hearing_date",
        "ruling_text",
        "ruling_text_html",
        "outcome",
        "motion_type",
        "parties",
        "extra",
        "courthouse",
        "department",
    }
)


# ─── AST helpers ──────────────────────────────────────────────────────────
def _is_captured_document_call(call: ast.Call) -> bool:
    """Return True if the call's ``func`` resolves to ``CapturedDocument``.

    Matches both bare-name calls (``CapturedDocument(...)`` after a
    ``from framework import CapturedDocument``) and attribute calls
    (``framework.CapturedDocument(...)``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "CapturedDocument"
    if isinstance(func, ast.Attribute):
        return func.attr == "CapturedDocument"
    return False


def _has_starred_kwargs(call: ast.Call) -> bool:
    """Return True if the call passes a ``**kwargs``.

    AST encodes ``f(**d)`` as a ``keyword`` whose ``arg`` is ``None``.
    When this is present we cannot statically determine the keyword
    set, so we skip the call (no violation)."""
    return any(kw.arg is None for kw in call.keywords)


def _is_reingest_shape(call: ast.Call) -> bool:
    """Return True if the call's keyword set matches the reingest shape.

    Rules (see module docstring):

    * keyword set is a superset of ``_IDENTIFIER_FIELDS``
    * no keyword in ``_PARSED_FIELDS`` is present
    * no ``**kwargs`` (handled separately by the caller)
    """
    keyword_names = {kw.arg for kw in call.keywords if kw.arg is not None}
    if not _IDENTIFIER_FIELDS.issubset(keyword_names):
        return False
    if keyword_names & _PARSED_FIELDS:
        return False
    return True


# ─── File scanning ────────────────────────────────────────────────────────
def scan_file(path: Path) -> list[tuple[Path, int]]:
    """Scan one Python file for inline reingest-shape ``CapturedDocument(...)``
    calls.  Returns a list of ``(path, lineno)`` violations.
    Syntactically broken files are skipped silently (mirrors the
    ``check_no_redos_pattern.py`` / ``check_parse_document_reingest_safety.py``
    convention)."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[Path, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_captured_document_call(node):
            continue
        if _has_starred_kwargs(node):
            continue
        if _is_reingest_shape(node):
            violations.append((path, node.lineno))
    return violations


def _iter_python_files(root: Path) -> list[Path]:
    """Walk ``root`` and return every ``.py`` file, excluding common
    vendored / generated directories."""
    excluded_dirs = {".venv", "__pycache__", "node_modules", ".git"}
    files: list[Path] = []
    for child in root.rglob("*.py"):
        if any(part in excluded_dirs for part in child.parts):
            continue
        files.append(child)
    return sorted(files)


# ─── CLI ──────────────────────────────────────────────────────────────────
def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan tests under packages/scraper-framework/tests/courts/ "
            "for inline reingest-shape CapturedDocument(...) constructions "
            "(issue #4190)."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "Override the scan root.  Defaults to "
            "<repo>/packages/scraper-framework/tests/courts/."
        ),
    )
    return parser.parse_args(argv)


def _resolve_default_root() -> Path:
    """The default scan root is the tests/courts/ tree.  Resolve relative
    to this script's location so the script works regardless of cwd."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    return repo_root / "packages" / "scraper-framework" / "tests" / "courts"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    root: Path = args.root or _resolve_default_root()

    if not root.exists():
        # Silent skip: an empty / missing root produces no violations.
        # Mirrors check_parse_document_reingest_safety.py.
        return 0

    violations: list[tuple[Path, int]] = []
    for py in _iter_python_files(root):
        violations.extend(scan_file(py))

    for path, lineno in violations:
        print(f"{path}:{lineno}:CapturedDocument(...)")

    # Always exit 0 — the wrapper interprets non-empty stdout as
    # a failure.  See module docstring "Exit codes".
    return 0


if __name__ == "__main__":
    sys.exit(main())
