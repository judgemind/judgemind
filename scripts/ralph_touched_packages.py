"""Helper to derive touched packages from git diff for the /ralph loop.

Given a list of changed file paths, derives which Python or TypeScript
packages have code changes (src/ or tests/ .py/.ts/.tsx files).

This module is intentionally dependency-free (stdlib only) to avoid requiring
a venv for the scripts/ directory.
"""
# venv: none
# permanent: true

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def derive_touched_packages(changed_files: list[str]) -> list[str]:
    """Return sorted list of Python packages with src/ or tests/ .py changes.

    A package is identified by its path under packages/<name>/ that contains
    a pyproject.toml.  Only files matching
    ``packages/<name>/src/**/*.py`` or ``packages/<name>/tests/**/*.py``
    count as code changes — doc, config, and fixture changes are excluded.

    Args:
        changed_files: List of repo-relative file paths (as returned by
            ``git diff --name-only``).

    Returns:
        Sorted list of package *names* (the ``<name>`` component, not the
        full path).  E.g. ``['nlp-pipeline', 'scraper-framework']``.
    """
    packages: set[str] = set()
    for f in changed_files:
        parts = f.split("/")
        # Must be packages/<name>/src/... or packages/<name>/tests/... .py
        if (
            len(parts) >= 4
            and parts[0] == "packages"
            and parts[2] in ("src", "tests")
            and f.endswith(".py")
        ):
            packages.add(parts[1])
    return sorted(packages)


def derive_touched_ts_packages(changed_files: list[str]) -> list[str]:
    """Return sorted list of TypeScript packages with src/ or app/ .ts/.tsx changes.

    A package is identified by its path under packages/<name>/ that contains
    a package.json.  Only files matching
    ``packages/<name>/src/**/*.{ts,tsx}`` or ``packages/<name>/app/**/*.{ts,tsx}``
    count as code changes.

    Args:
        changed_files: List of repo-relative file paths (as returned by
            ``git diff --name-only``).

    Returns:
        Sorted list of package *names* (the ``<name>`` component, not the
        full path).  E.g. ``['web']``.
    """
    packages: set[str] = set()
    for f in changed_files:
        parts = f.split("/")
        # Must be packages/<name>/src/... or packages/<name>/app/... .ts/.tsx
        if (
            len(parts) >= 4
            and parts[0] == "packages"
            and parts[2] in ("src", "app")
            and (f.endswith(".ts") or f.endswith(".tsx"))
        ):
            packages.add(parts[1])
    return sorted(packages)


def git_diff_names(worktree: str | Path, base: str = "origin/main") -> list[str]:
    """Return list of file paths changed relative to *base*.

    Wraps ``git -C <worktree> diff --name-only <base>...HEAD`` so callers
    do not need to shell out directly.

    Args:
        worktree: Absolute path to the worktree.
        base: The base ref to compare against (default: ``origin/main``).

    Returns:
        List of repo-relative file paths, one per changed file.  Returns
        an empty list if git is unavailable or the command fails.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "diff",
                "--name-only",
                f"{base}...HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> None:
    """CLI entry point.

    Prints one package name per line to stdout.  With ``--ts``, prints
    TypeScript packages; without it, prints Python packages.

    Usage::

        python3 scripts/ralph_touched_packages.py [--ts] [<worktree>]

    If *worktree* is omitted, the current directory is used.
    """
    args = sys.argv[1:]
    ts_mode = "--ts" in args
    args = [a for a in args if a != "--ts"]

    worktree = Path(args[0]) if args else Path(".")

    changed = git_diff_names(worktree)

    if ts_mode:
        pkgs = derive_touched_ts_packages(changed)
    else:
        pkgs = derive_touched_packages(changed)

    for pkg in pkgs:
        print(pkg)


if __name__ == "__main__":
    main()
