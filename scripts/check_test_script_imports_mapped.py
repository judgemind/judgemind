#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_test_script_imports_mapped.py — Verify that every top-level
``scripts/*.py`` module imported by a test under
``packages/scraper-framework/tests/`` is listed in the
``dorny/paths-filter`` filter that gates the CI job which runs that
test.

Motivation (#4452, root cause of #4449)
---------------------------------------
``packages/scraper-framework/tests/test_reingest_from_s3.py`` imports
``scripts/reingest_from_s3.py`` (via ``sys.path`` injection +
``importlib.import_module("reingest_from_s3")``).  The
``ingestion-tests`` job in ``.github/workflows/ci.yml`` runs that
test file, but the path filter that gates the job (``scraper:``)
only fires on changes to ``packages/scraper-framework/**`` or
``packages/judgemind-config/**`` — NOT on changes to
``scripts/reingest_from_s3.py``.

Concrete failure mode (#4449):

    PR #4421 modified scripts/reingest_from_s3.py only.  CI ran;
    the scraper filter did not match; ingestion-tests skipped; the
    KeyError regression in _reparse_document slipped past CI.  Two
    weeks later, the next PR that touched packages/scraper-framework/**
    (unrelated change) tripped the now-broken tests, blocking every
    subsequent scraper PR until #4449 was diagnosed and patched.

The same shape applies to every ``scripts/*.py`` exercised exclusively
by tests in ``packages/scraper-framework/tests/`` (today: ~7 such
scripts, including ``rebuild_db.py``, ``reingest_from_s3.py``,
``backfill_llm_enrichment.py``, ``audit_oc_ruling_integrity.py``,
``dq_trend_storage.py``, ``check-scraper-registry.py``,
``check_tests_use_reingest_helper.py``).  Every one of them carries
the same slip-through risk.  This guard structurally enforces the
"imported script must be in the path filter" invariant so the bug
class stops accruing.

What the check does
-------------------
1. Walk every ``.py`` file under
   ``packages/scraper-framework/tests/`` (recursively).
2. For each test file, AST-scan for imports of top-level
   ``scripts/*.py`` modules.  Three import shapes are detected:

       import <name>
       from <name> import ...
       importlib.import_module("<name>")

   The candidate <name> is a "real" import if it matches the stem
   (or full filename, for hyphen-named scripts) of an existing
   top-level ``scripts/*.py`` file.  Imports of archived /
   nonexistent scripts (``scripts/archive/<name>.py``,
   ``scripts/oneoff/<name>.py``) are intentionally ignored here
   because the path-filter mapping invariant only applies to live
   ``scripts/*.py``.  The orthogonal hygiene problem ("a test
   imports a script that has been archived without explicit
   intent") is covered by the sibling guard
   ``scripts/check-test-script-imports-resolvable.sh`` (#4464).

3. Determine the CI job that runs each test based on the explicit
   shard mapping below (mirrors the ``run:`` blocks in
   ``.github/workflows/ci.yml``):

       INGESTION_TESTS = {
           "tests/test_reingest_from_s3.py",
           "tests/test_reingest_registry.py",
           "tests/test_ingestion.py",
           "tests/test_extract.py",
       }
       Anything under ``tests/courts/`` -> scraper-courts filter
                                           (job: scraper-court-tests)
       Anything else under ``tests/``  -> scraper-framework filter
                                           (job: scraper-framework-tests)

4. Parse ``.github/workflows/ci.yml`` for the three ``dorny/paths-filter``
   filter blocks (``scraper``, ``scraper-framework``, ``scraper-courts``).

5. For each (test-file, imported-script) pair, assert that the
   imported script's path (``scripts/<name>.py``) appears in (or is
   matched by a glob of) the corresponding filter.  Report
   violations with file:line and a copy-pasteable Fix block.

Exit codes
----------
    0 — All clean: every test-imported script is covered.
    1 — One or more violations.
    2 — Internal / parse error.

Usage
-----
    scripts/check-test-script-imports-mapped.sh
    # or directly:
    python3 scripts/check_test_script_imports_mapped.py
    python3 scripts/check_test_script_imports_mapped.py --repo-root PATH
    python3 scripts/check_test_script_imports_mapped.py \\
        --tests-dir packages/scraper-framework/tests \\
        --workflow .github/workflows/ci.yml
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — explicit mapping from test-file path to filter name
# ---------------------------------------------------------------------------

# Tests run by the ``ingestion-tests`` job (gated on ``scraper`` filter).
# Mirrors the explicit pytest list in .github/workflows/ci.yml::ingestion-tests.
INGESTION_TESTS = frozenset(
    {
        "tests/test_reingest_from_s3.py",
        "tests/test_reingest_registry.py",
        "tests/test_ingestion.py",
        "tests/test_extract.py",
    }
)

# Filter names as they appear in .github/workflows/ci.yml.
INGESTION_FILTER = "scraper"
SCRAPER_FRAMEWORK_FILTER = "scraper-framework"
SCRAPER_COURTS_FILTER = "scraper-courts"


# ---------------------------------------------------------------------------
# Glob → regex (picomatch-ish, matches dorny/paths-filter)
# ---------------------------------------------------------------------------
# This is a near-copy of the helper in
# scripts/check_workflow_paths_filter_coverage.py — kept as a local copy
# rather than imported so this script remains self-contained (the umbrella
# ``run-ci-guards.sh`` invokes each guard as a standalone executable).


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Convert a picomatch-ish glob to a compiled regex anchored to full path."""
    glob = glob.strip().strip("'\"")

    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                if i + 2 < n and glob[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in r".+^$()[]{}|\\":
            out.append(re.escape(c))
            i += 1
        else:
            out.append(re.escape(c))
            i += 1

    pattern = "^" + "".join(out) + "$"
    return re.compile(pattern)


def path_matches_any(path: str, compiled: list[re.Pattern[str]]) -> bool:
    return any(p.match(path) for p in compiled)


# ---------------------------------------------------------------------------
# YAML-ish parser (line-oriented; same pattern as
# check_workflow_paths_filter_coverage.py)
# ---------------------------------------------------------------------------
#
# We only need to extract the named-filter blocks under the ``filters: |``
# block scalar of a ``dorny/paths-filter`` step.  In ci.yml this looks like:
#
#     - uses: dorny/paths-filter@v4
#       id: changes
#       with:
#         filters: |
#           scraper:
#             - 'packages/scraper-framework/**'
#             - 'packages/judgemind-config/**'
#           scraper-framework:
#             - 'packages/scraper-framework/src/framework/**'
#             ...
#
# The block-scalar content lives inside the ``filters: |`` mapping value.  Each
# top-level filter name is a key whose value is a YAML list of glob strings.

FILTER_BLOCK_HEADER_RE = re.compile(r"^(\s*)([A-Za-z0-9_\-]+):\s*$")
PATH_ENTRY_RE = re.compile(r"^(\s*)-\s+(.+?)\s*$")
FILTERS_KEY_RE = re.compile(r"^(\s*)filters:\s*\|[\s+\-]*\s*$")


@dataclass
class PathsFilter:
    """One named filter under a ``filters: |`` block."""

    name: str
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)

    def matches(self, path: str) -> bool:
        pos = [glob_to_regex(g) for g in self.positives]
        neg = [glob_to_regex(g) for g in self.negatives]
        if not path_matches_any(path, pos):
            return False
        if path_matches_any(path, neg):
            return False
        return True


def parse_filters_from_workflow(workflow_path: Path) -> dict[str, PathsFilter]:
    """Parse the ``filters: |`` block from a workflow file.

    Returns a mapping {filter_name: PathsFilter}.  Raises ValueError if no
    ``filters: |`` block is present.
    """
    if not workflow_path.is_file():
        raise FileNotFoundError(
            f"workflow file does not exist on disk: {workflow_path} "
            "(if invoked via ECS oneshot, --workflow=PATH must be passed; "
            "see check-no-tmp-oneshot-file-path-derivation.py / #4374 / #4381)"
        )
    text = workflow_path.read_text()
    lines = text.splitlines()

    n = len(lines)
    i = 0
    filters: dict[str, PathsFilter] = {}

    while i < n:
        m = FILTERS_KEY_RE.match(lines[i])
        if m:
            filters_indent = len(m.group(1))
            # Walk the block-scalar lines.  Block ends when we encounter a
            # line at indent <= filters_indent that is non-blank.
            j = i + 1
            current_filter: PathsFilter | None = None
            current_filter_indent: int | None = None
            while j < n:
                line = lines[j]
                if not line.strip():
                    j += 1
                    continue
                line_indent = len(line) - len(line.lstrip(" "))
                if line_indent <= filters_indent:
                    break

                # Check for filter header: `<indent>name:` at depth = filters_indent + 2 (typical)
                m_header = FILTER_BLOCK_HEADER_RE.match(line)
                if m_header and (
                    current_filter_indent is None
                    or len(m_header.group(1)) <= current_filter_indent
                ):
                    name = m_header.group(2)
                    current_filter = PathsFilter(name=name)
                    current_filter_indent = len(m_header.group(1))
                    filters[name] = current_filter
                    j += 1
                    continue

                # Path entry line: `- '...'` (deeper than current_filter_indent)
                m_entry = PATH_ENTRY_RE.match(line)
                if m_entry and current_filter is not None:
                    val = m_entry.group(2).strip()
                    if (val.startswith("'") and val.endswith("'")) or (
                        val.startswith('"') and val.endswith('"')
                    ):
                        val = val[1:-1]
                    if val.startswith("!"):
                        current_filter.negatives.append(val[1:])
                    else:
                        current_filter.positives.append(val)

                j += 1
            i = j
            continue
        i += 1

    if not filters:
        raise ValueError(
            f"No `filters: |` block found in workflow {workflow_path} "
            "(or the block was empty)."
        )
    return filters


# ---------------------------------------------------------------------------
# Test-file scanning — AST-based detection of scripts/*.py imports
# ---------------------------------------------------------------------------


def list_top_level_scripts(scripts_dir: Path) -> dict[str, str]:
    """Return ``{module-name: relative-script-path}`` for every top-level
    ``scripts/*.py``.

    The module-name is the file stem (for normal Python module names) OR the
    full filename without ``.py`` (for hyphen-named scripts that are imported
    via ``importlib.import_module("check-scraper-registry")``).  The
    relative-path is what a paths-filter glob will match against —
    e.g. ``scripts/rebuild_db.py``.
    """
    out: dict[str, str] = {}
    for f in sorted(scripts_dir.iterdir()):
        if f.is_file() and f.suffix == ".py":
            # Hyphen-named scripts: stem is the same as filename-minus-.py.
            # Both stem-with-underscores and stem-with-hyphens key into the
            # same path so import detection works either way.
            out[f.stem] = f"scripts/{f.name}"
    return out


@dataclass
class TestImport:
    """One import-of-a-scripts/*.py-module discovered in a test file."""

    test_path: Path  # absolute path of test file
    test_rel: str  # POSIX rel path under repo root, e.g. tests/test_x.py
    line_number: int  # 1-indexed line of the import statement
    module_name: str  # module name as used in import (e.g. "rebuild_db")
    script_path: str  # relative scripts/<file>.py path, e.g. scripts/rebuild_db.py


def find_script_imports_in_file(
    path: Path, test_rel: str, scripts: dict[str, str]
) -> list[TestImport]:
    """AST-walk ``path`` and return TestImport entries for each scripts/* import."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []

    found: list[TestImport] = []

    def _record(name: str, lineno: int) -> None:
        if name in scripts:
            found.append(
                TestImport(
                    test_path=path,
                    test_rel=test_rel,
                    line_number=lineno,
                    module_name=name,
                    script_path=scripts[name],
                )
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                _record(top, node.lineno)
                if top == "scripts" and len(alias.name.split(".")) >= 2:
                    sub = alias.name.split(".", 1)[1].split(".")[0]
                    _record(sub, node.lineno)
        if isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                _record(top, node.lineno)
                if top == "scripts" and len(node.module.split(".")) >= 2:
                    sub = node.module.split(".", 1)[1].split(".")[0]
                    _record(sub, node.lineno)
        if isinstance(node, ast.Call):
            func = node.func
            attr_name = func.attr if isinstance(func, ast.Attribute) else None
            if attr_name == "import_module" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    name = arg.value
                    if name.startswith("scripts."):
                        name = name.split(".", 1)[1]
                    _record(name, node.lineno)

    # Deduplicate (module_name, script_path) pairs per file — keep first
    # occurrence so the violation report points at the lexically-earliest
    # import statement.
    seen: set[tuple[str, str]] = set()
    deduped: list[TestImport] = []
    for ti in found:
        key = (ti.module_name, ti.script_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ti)
    return deduped


def find_all_imports(
    tests_dir: Path,
    repo_root: Path,
    scripts: dict[str, str],
) -> list[TestImport]:
    """Scan every .py file under tests_dir (recursively) and return TestImports."""
    if not tests_dir.is_dir():
        raise ValueError(f"tests dir not found: {tests_dir}")

    imports: list[TestImport] = []
    package_dir = tests_dir.parent  # packages/scraper-framework/
    for f in sorted(tests_dir.rglob("*.py")):
        # `tests/<rel>` form, POSIX-style.
        try:
            rel = f.relative_to(package_dir).as_posix()
        except ValueError:
            rel = f.as_posix()
        imports.extend(find_script_imports_in_file(f, rel, scripts))
    return imports


# ---------------------------------------------------------------------------
# Filter-name resolution — which filter gates the test that uses this import?
# ---------------------------------------------------------------------------


def required_filters_for_test(test_rel: str) -> list[str]:
    """Return the list of filter names that must contain the imported script.

    test_rel is a POSIX path under packages/scraper-framework/, e.g.
    ``tests/test_reingest_from_s3.py`` or ``tests/courts/test_x.py``.
    """
    if test_rel in INGESTION_TESTS:
        return [INGESTION_FILTER]
    if test_rel.startswith("tests/courts/"):
        return [SCRAPER_COURTS_FILTER]
    return [SCRAPER_FRAMEWORK_FILTER]


# ---------------------------------------------------------------------------
# Violation type + reporter
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    test_rel: str  # tests/<rel> as observed
    test_lineno: int
    module_name: str
    script_path: str  # e.g. scripts/rebuild_db.py
    missing_filter: str  # the filter name the script is NOT in


def check(
    tests_dir: Path,
    workflow_path: Path,
    scripts_dir: Path,
    repo_root: Path,
) -> list[Violation]:
    scripts = list_top_level_scripts(scripts_dir)
    imports = find_all_imports(tests_dir, repo_root, scripts)
    filters = parse_filters_from_workflow(workflow_path)

    violations: list[Violation] = []
    for ti in imports:
        for filter_name in required_filters_for_test(ti.test_rel):
            pf = filters.get(filter_name)
            if pf is None:
                # Filter named in mapping doesn't exist in workflow — that's
                # itself a violation worth flagging (likely an out-of-date
                # ci.yml or a renamed filter).
                violations.append(
                    Violation(
                        test_rel=ti.test_rel,
                        test_lineno=ti.line_number,
                        module_name=ti.module_name,
                        script_path=ti.script_path,
                        missing_filter=f"{filter_name} (filter does not exist in workflow)",
                    )
                )
                continue
            if not pf.matches(ti.script_path):
                violations.append(
                    Violation(
                        test_rel=ti.test_rel,
                        test_lineno=ti.line_number,
                        module_name=ti.module_name,
                        script_path=ti.script_path,
                        missing_filter=filter_name,
                    )
                )
    return violations


def format_fix_block(violations: list[Violation], workflow_path: Path) -> str:
    """Produce a copy-pasteable Fix block grouped by missing_filter."""
    by_filter: dict[str, set[str]] = {}
    for v in violations:
        by_filter.setdefault(v.missing_filter, set()).add(v.script_path)

    lines: list[str] = []
    lines.append("Fix:")
    lines.append(
        f"  Add the missing script path(s) to {workflow_path.as_posix()} under"
    )
    lines.append("  the appropriate `dorny/paths-filter` filter block, so that")
    lines.append("  PRs that modify the script trigger the CI job that imports it.")
    lines.append("")
    for filter_name in sorted(by_filter):
        scripts_for_filter = sorted(by_filter[filter_name])
        lines.append(f"  In the `{filter_name}:` filter, add:")
        for sp in scripts_for_filter:
            lines.append(f"      - '{sp}'")
        lines.append("")
    lines.append("  See #4452 (this guard) and #4449 (the slip-through it prevents).")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Verify that every top-level scripts/*.py imported by a test under "
            "packages/scraper-framework/tests/ is in the path filter that "
            "gates the CI job which runs that test."
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
        help="Override tests dir (default: packages/scraper-framework/tests under repo).",
    )
    ap.add_argument(
        "--scripts-dir",
        default=None,
        help="Override scripts dir (default: scripts/ under repo).",
    )
    ap.add_argument(
        "--workflow",
        default=None,
        help="Override workflow path (default: .github/workflows/ci.yml under repo).",
    )
    args = ap.parse_args()

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        # scripts/check_test_script_imports_mapped.py -> repo_root is one parent up.
        repo_root = Path(__file__).resolve().parent.parent

    tests_dir = (
        Path(args.tests_dir).resolve()
        if args.tests_dir
        else repo_root / "packages" / "scraper-framework" / "tests"
    )
    scripts_dir = (
        Path(args.scripts_dir).resolve() if args.scripts_dir else repo_root / "scripts"
    )
    workflow_path = (
        Path(args.workflow).resolve()
        if args.workflow
        else repo_root / ".github" / "workflows" / "ci.yml"
    )

    if not tests_dir.is_dir():
        print(f"ERROR: tests dir not found: {tests_dir}", file=sys.stderr)
        return 2
    if not scripts_dir.is_dir():
        print(f"ERROR: scripts dir not found: {scripts_dir}", file=sys.stderr)
        return 2
    if not workflow_path.is_file():
        print(f"ERROR: workflow file not found: {workflow_path}", file=sys.stderr)
        return 2

    try:
        violations = check(tests_dir, workflow_path, scripts_dir, repo_root)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: check failed: {exc}", file=sys.stderr)
        return 2

    if not violations:
        return 0

    print(
        "check-test-script-imports-mapped: one or more tests under "
        "packages/scraper-framework/tests/ import a scripts/*.py module that "
        "is NOT in the CI path filter gating the job that runs that test.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)

    # Group by (filter, script) for readability.
    seen: set[tuple[str, str]] = set()
    for v in violations:
        key = (v.missing_filter, v.script_path)
        if key in seen:
            continue
        seen.add(key)
        print(
            f"  {v.test_rel}:{v.test_lineno}\n"
            f"    imports  {v.module_name}  (-> {v.script_path})\n"
            f"    but      {v.script_path} is NOT in the `{v.missing_filter}` filter\n",
            file=sys.stderr,
        )

    print(format_fix_block(violations, workflow_path), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
