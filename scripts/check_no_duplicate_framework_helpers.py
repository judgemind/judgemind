#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_no_duplicate_framework_helpers.py — AST scanner for the
"script duplicates a name exported by ``framework.s3_keys``" pattern.

Driven by ``scripts/check-no-duplicate-framework-helpers.sh``. See that
wrapper for the full motivation and CI integration story (issue #4456).

Background — why duplicates accrue
----------------------------------

PR #4447 extracted the flat-hash S3 key helpers from the duplicated
copies in ``cleanup_mislabeled_s3_2661.py`` and
``repoint_mislabeled_documents_4439.py`` into the shared module
``packages/scraper-framework/src/framework/s3_keys.py``. ECS oneshot
scripts can import them via the standard ``from framework.s3_keys
import ...`` path because the helpers are bundled into the
ingestion-worker / scraper-framework Docker image. Both scripts were
archived to ``scripts/archive/`` in #4565 after their runtime applies
landed; the post-#4447 import shape is preserved verbatim in the
archived copies.

Days later PR #4453 shipped ``scripts/create_missing_twins_4446.py``
that re-duplicated the helpers — the agent read the *pre-#4447*
docstring of ``repoint_mislabeled_documents_4439.py`` (which still
carried the legacy NOTE about the duplication being deliberate) and
faithfully copied the duplication. The post-#4447 version of the
repoint script imports cleanly from ``framework.s3_keys``. Issue #4455
tracked the migration of ``create_missing_twins_4446.py`` (archived
at ``scripts/archive/`` in #4565); this guard prevents the next agent
who clones one of those scripts from inheriting the same duplication.

What is flagged
---------------

A Python source file under ``scripts/`` is flagged when:

  1. It defines a top-level ``def <name>(...)`` (or ``async def``) OR a
     top-level ``<NAME> = ...`` (or annotated ``<NAME>: T = ...``)
     constant, AND
  2. ``<name>`` appears in the public API of
     ``packages/scraper-framework/src/framework/s3_keys.py``.

The public API is determined by:

  - If the framework module declares ``__all__``, use the names listed
    there.
  - Otherwise, fall back to every top-level ``def`` / ``class`` / and
    module-scope assignment whose target identifier does NOT start
    with an underscore. *Imported* names (``import``, ``from ...
    import ...``) are NEVER counted as part of the public API — only
    names *defined* in the module are.

What is NOT flagged
-------------------

  - Files under ``scripts/archive/`` (deprecated one-offs kept for
    posterity — the migration that originally produced the helpers
    lives there as ``scripts/archive/migrate_s3_keys.py`` and it is
    intentionally kept independent).
  - Files under ``scripts/tests/`` (tests legitimately import from
    the framework but may also redefine names locally as fixtures or
    inside ``mock_sys_modules`` blocks — out of scope for this guard).
  - Files under ``scripts/dispatcher/``, ``scripts/dispatcher_v3/``,
    ``scripts/spotcheck/`` — they live under separate observability
    conventions and don't ingest from S3 the way the top-level
    oneshot scripts do.
  - Files carrying a ``# allow-duplicate-framework-helpers: <reason>``
    header pragma anywhere in the file (intended for archive/legacy
    scripts that genuinely cannot import the framework helpers; the
    reason must cite an issue or PR).
  - The framework module itself (``packages/scraper-framework/...``)
    and the framework's own tests — out of scan scope by virtue of
    not being under ``scripts/``.

Usage
-----

    python3 scripts/check_no_duplicate_framework_helpers.py [PATH ...]

Each PATH is a ``.py`` file under ``scripts/``. The wrapper script
discovers the paths.

Exit codes
----------

  0 — Always. The wrapper turns the printed-violations stream into a
       non-zero exit. Splitting the responsibility keeps this script's
       output stream the single source of truth for tests and the
       wrapper alike.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ─── Allowlist marker ────────────────────────────────────────────────
# Files containing this marker anywhere in their content opt out of the
# check. The marker must cite the issue/PR justifying the exception so
# future readers can audit whether the exemption is still warranted.
ALLOW_MARKER = "# allow-duplicate-framework-helpers:"


def collect_public_api(framework_path: Path) -> frozenset[str]:
    """Return the public API names exported by *framework_path*.

    If the module declares ``__all__`` as a literal list/tuple/set of
    string constants, those names are the public API. Otherwise the
    fallback is every module-scope ``def`` / ``async def`` / ``class``
    name plus every module-scope assignment target identifier whose
    name does NOT start with an underscore.

    Imported names (``import re``, ``from botocore.exceptions import
    ClientError``) are never counted — the goal is to surface
    ``framework.s3_keys``'s own definitions, not its imports.
    """
    try:
        source = framework_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()

    # First pass: look for __all__.
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    names = _extract_string_literals(node.value)
                    if names is not None:
                        return frozenset(names)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "__all__"
                and node.value is not None
            ):
                names = _extract_string_literals(node.value)
                if names is not None:
                    return frozenset(names)

    # Fallback: collect non-underscore names from defs and assignments.
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                names.add(node.target.id)
        # Imports are intentionally skipped — they are not part of the
        # module's own definitions.
    return frozenset(names)


def _extract_string_literals(node: ast.expr) -> list[str] | None:
    """Return the list of string literals inside *node* if it's a
    list/tuple/set of string constants, else ``None``."""
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    literals: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            literals.append(elt.value)
        else:
            return None
    return literals


def collect_top_level_defs(script_path: Path) -> list[tuple[int, str]]:
    """Return module-scope ``(lineno, name)`` pairs for the script.

    Includes top-level ``def`` / ``async def`` / ``class`` names plus
    top-level assignment target identifiers. Imports are excluded so
    that ``from framework.s3_keys import parse_flat_hash_key`` does
    NOT count as a duplicate definition.

    Targets buried inside ``if`` / ``try`` / ``with`` blocks at module
    scope are intentionally NOT walked — only names that appear at the
    truly top level of the module body are surfaced. Real shadowing
    happens at top level; a name defined inside a conditional import
    branch (``if sys.version_info >= ...``) is a feature, not a bug.
    """
    try:
        source = script_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    out: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((node.lineno, node.name))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.append((node.lineno, tgt.id))
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                out.append((node.lineno, node.target.id))
    return out


def has_allow_marker(path: Path) -> bool:
    """Return True if *path* contains the allowlist marker anywhere."""
    try:
        return ALLOW_MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def scan_file(
    script_path: Path,
    public_api: frozenset[str],
) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` violations — one per duplicated symbol.

    A file with the allowlist marker returns an empty list regardless
    of its definitions.
    """
    if has_allow_marker(script_path):
        return []
    violations: list[tuple[int, str]] = []
    for lineno, name in collect_top_level_defs(script_path):
        if name in public_api:
            violations.append((lineno, name))
    return violations


def main(argv: list[str]) -> int:
    """Print one ``<path>:<lineno>:<name>`` line per violation.

    The first argument is the path to the framework module
    (``packages/scraper-framework/src/framework/s3_keys.py``); the
    remaining arguments are the script paths to scan.
    """
    if len(argv) < 2:
        # Wrapper invocation guarantees at least one path; defensive.
        return 0
    framework_path = Path(argv[1])
    public_api = collect_public_api(framework_path)
    if not public_api:
        # Framework module unreadable or empty — nothing to compare against.
        return 0

    for raw in argv[2:]:
        script_path = Path(raw)
        for lineno, name in scan_file(script_path, public_api):
            print(f"{script_path}:{lineno}:{name}")
    # Exit 0 unconditionally — the wrapper aggregates output and exits 1
    # on non-empty output. See module docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
