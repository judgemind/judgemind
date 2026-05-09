#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_oneshot_repo_paths.py — AST-walking guard that flags ECS oneshot
scripts which reference ``REPO_ROOT`` / ``_REPO_ROOT`` for path
construction without a validated fallback.

Why this exists
---------------
Scripts run via ``scripts/ecs-run-task.sh`` are uploaded to the ECS
container as a single file at ``/tmp/_oneshot_script``. The repo
filesystem is NOT available inside the container, so anything that
resolves a path through ``_REPO_ROOT = Path(__file__).resolve().parent``
silently degrades — the path collapses to ``/tmp`` or ``/`` and
subsequent file loads either fail loudly with ``[Errno 2]`` or, worse,
silently return empty data.

This guard scans every top-level ``scripts/*.py`` file for module-level
references to ``_REPO_ROOT`` / ``REPO_ROOT`` and flags any that are
not in the ``LOCAL_ONLY`` (CI-only / dev-only) or ``VALIDATED`` (has a
documented fallback) lists.

AST-walk vs. text-grep (#4483)
-------------------------------
This script replaces the original ``grep -E '_?REPO_ROOT'`` scan with
an AST-walk so that mentions inside *prose* — module / function / class
docstrings, ``print(...)`` / ``lines.append(...)`` string literals,
inline comments — never trigger the guard. Only ``ast.Name(id=...)``
loads and ``ast.Assign`` / ``ast.AnnAssign`` targets with the matching
name count as real uses.

Surfacing case (#4464): the new
``scripts/check_test_script_imports_resolvable.py`` documents the
``_REPO_ROOT / "scripts" / "<name>.py"`` AST pattern in its module
docstring AND emits a ``Fix:`` block with the literal string
``"sys.path.insert(0, str(REPO_ROOT / 'scripts' / 'archive'))"`` —
neither line is a real REPO_ROOT use, but the pre-#4483 text-grep
flagged both. The AST-walk version silently passes such files because
no ``ast.Name(id="REPO_ROOT")`` Load context appears outside string
constants.

Usage
-----
    scripts/check_oneshot_repo_paths.py             # scan scripts/*.py
    scripts/check_oneshot_repo_paths.py --dir DIR   # scan DIR/*.py

Exit codes
----------
    0 — No violations.
    1 — One or more scripts reference REPO_ROOT without a validated fallback.
    2 — Usage error.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Skip lists — kept in lockstep with the previous shell version.
# ---------------------------------------------------------------------------

# Local-only scripts (never run as ECS oneshots). Imported from
# check-oneshot-imports.sh. These scripts only run in CI / dev, so
# repo-root references are safe.
LOCAL_ONLY: tuple[str, ...] = (
    "gemini_review.py",
    "log_ralph_review.py",
    "log_ralph_summary.py",
    "update-coverage-baselines.py",
    "validate-dq-baselines.py",
    # CI-only checker scripts — run in GitHub Actions, not ECS
    "check-oneshot-imports.sh",
    "check-oneshot-repo-paths.sh",
    "check_oneshot_repo_paths.py",  # this file (AST-walk replacement)
    "check-duplicate-functions.py",
    "check-sql-conflicts.py",
    "check-deprecated-models.sh",
    "check-hardcoded-models.sh",
    "check-aws-bool-flags.sh",
    "check-hardcoded-colors.sh",
    "check-migration-files.sh",
    "check-no-unbounded-timeouts.py",
    "check-no-tmp-oneshot-file-path-derivation.py",
    "check-issue-verify-sql.py",
    # NOTE: check_test_script_imports_resolvable.py used to live here as
    # a workaround for the text-grep false positive (#4483). With the
    # AST-walk in place it no longer needs the opt-out — its REPO_ROOT
    # mentions are docstring + string-literal only.
    # Developer/dispatcher tooling — only run locally
    "dispatcher-request.py",
    "phase_timer.py",
    "screenshot.py",
)

# VALIDATED scripts reference REPO_ROOT but have a documented fallback
# for ECS oneshot execution.
VALIDATED: tuple[str, ...] = (
    # data-quality-check.py — --baselines-base64 / --baselines-json CLI args bypass file path (#1225)
    "data-quality-check.py",
    # check-scraper-zero-record-runner.py — runs in dedicated ECS task; scripts/ baked into image (#2677)
    "check-scraper-zero-record-runner.py",
    # drain_splitter_carry_forward_clusters.py — _SCRAPER_SRC is no-op in ECS;
    # scraper-framework installed in /app venv; /app/scripts baked into image (#4321)
    "drain_splitter_carry_forward_clusters.py",
    # cc-dual-run-diff.py — _SF_SRC has .is_dir() guard; scraper-framework
    # installed in /app venv inside ECS, so the sys.path append is a no-op there;
    # the helper imports (courts.ca.cc_tentatives_portal._cc_dept_from_filename,
    # framework.dual_run_diff.*) resolve from the venv-installed package (#2610)
    "cc-dual-run-diff.py",
)

# Module-level variable name pattern: REPO_ROOT or _REPO_ROOT (no
# trailing characters — DEFAULT_REPO_ROOT_PATH should NOT match).
_NAME_RE = re.compile(r"^_?REPO_ROOT$")


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------


def _matches_name(node: ast.AST) -> bool:
    """Return True if ``node`` is an ``ast.Name`` whose id matches
    ``^_?REPO_ROOT$``. False otherwise — including for ``Constant`` /
    ``JoinedStr`` / ``Attribute`` nodes (which is the entire point of
    the AST upgrade in #4483)."""
    return isinstance(node, ast.Name) and bool(_NAME_RE.match(node.id))


def find_repo_root_uses(source: str) -> list[tuple[int, str]]:
    """Walk ``source`` as a Python AST and return every (lineno, snippet)
    pair where ``_REPO_ROOT`` / ``REPO_ROOT`` is used as a real Name —
    either as a Load reference or as an ``Assign`` / ``AnnAssign``
    target.

    String constants, docstrings, comments, and attribute accesses such
    as ``self.REPO_ROOT`` are NOT flagged. This matches the AC in #4483:
    only Name-context references count.

    Returns an empty list when the source contains no real uses, OR
    when the source fails to parse (we conservatively fall back to "no
    flag" rather than mask a syntax error in source code as a guard
    failure — pytest / ruff already catch syntax errors).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    uses: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        # Pattern 1: bare Name reference (Load, Store, or Del context).
        # ast.Name covers ``_REPO_ROOT`` and ``REPO_ROOT`` whether they
        # appear in ``X = _REPO_ROOT / "y"`` (Load), ``_REPO_ROOT = ...``
        # (Store), or ``del _REPO_ROOT`` (Del). The Store context is
        # also implicitly covered by Pattern 2 below; counting it twice
        # is fine because we de-duplicate by (lineno, repr).
        if _matches_name(node):
            lineno = getattr(node, "lineno", 0)
            uses.append((lineno, f"Name({node.id!r}) — module-level reference"))
            continue

        # Pattern 2: Assign / AnnAssign with a matching target. This is
        # already covered by Pattern 1 (the Name target is itself an
        # ast.Name in Store context), but we also handle the case where
        # an annotated assignment has no value yet (``REPO_ROOT: Path``)
        # for completeness — ast.walk still yields the Name target via
        # Pattern 1 either way, so this branch is currently a no-op
        # documentation point. Left as an explicit branch for the
        # benefit of future maintainers reading the AC in #4483.

    # De-duplicate by (lineno, snippet) — Pattern 1 + Pattern 2 can
    # yield the same Name node twice for an Assign target.
    return sorted(set(uses))


# ---------------------------------------------------------------------------
# Per-file scan
# ---------------------------------------------------------------------------


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return the list of REPO_ROOT-use rows in ``path``, or [] if the
    file is in LOCAL_ONLY / VALIDATED, is not a regular file, or has
    no real uses."""
    name = path.name
    if name in LOCAL_ONLY or name in VALIDATED:
        return []
    if not path.is_file():
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return find_repo_root_uses(source)


def emit_violation(path: Path, uses: list[tuple[int, str]]) -> None:
    """Print the same Fix-block format the original shell guard used,
    so existing tooling / dashboards / CI parsers don't notice the
    AST-walk swap."""
    print(f"ERROR: {path.name} references REPO_ROOT without a validated fallback")
    print("  ECS oneshot scripts cannot access repo-level files.")
    print("  The repo filesystem is NOT available inside the ECS container.")
    print()
    print("  References found:")
    for lineno, snippet in uses:
        print(f"    {lineno}: {snippet}")
    print()
    print("  Fix options:")
    print(
        "    1. Add a CLI argument to pass the data inline (e.g. --baselines-base64)."
    )
    print(
        "    2. Add an .exists() guard with graceful fallback when the file is missing."
    )
    print("    3. If this script is NEVER run as an ECS oneshot, add it to the")
    print("       LOCAL_ONLY array in scripts/check_oneshot_repo_paths.py.")
    print("    4. If the fallback already exists, add the script to the VALIDATED")
    print("       array in scripts/check_oneshot_repo_paths.py with a comment")
    print("       documenting the fallback mechanism.")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AST-walking guard for ECS oneshot REPO_ROOT references.",
    )
    parser.add_argument(
        "--dir",
        dest="target_dir",
        default=None,
        help="Directory to scan (default: scripts/ — the parent of this file).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.target_dir:
        target = Path(args.target_dir).resolve()
    else:
        target = Path(__file__).resolve().parent

    if not target.is_dir():
        print(f"ERROR: --dir target {target} is not a directory", file=sys.stderr)
        return 2

    violations = 0
    for entry in sorted(target.glob("*.py")):
        uses = scan_file(entry)
        if uses:
            emit_violation(entry, uses)
            violations += 1

    if violations > 0:
        print(f"Found {violations} script(s) with unvalidated REPO_ROOT references.")
        print("See https://github.com/judgemind/judgemind/issues/1277 for context.")
        return 1

    print("All scripts clean — no unvalidated REPO_ROOT references detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
