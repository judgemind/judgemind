"""Helper to derive touched packages from the worktree's change set for /ralph.

Ralph's worker runs the full test suite for every package this helper
prints. Ralph runs *before* /task commits anything, so the change set must
include uncommitted work, not just commits (#5112). The change set is the
de-duplicated union of:

* committed changes vs the merge base with ``origin/main``
  (``git diff --name-only origin/main...HEAD``),
* staged + unstaged changes vs HEAD (``git diff --name-only HEAD``),
* staged changes (``git diff --cached --name-only``),
* untracked files (``git ls-files --others --exclude-standard``).

An empty result must never pass for "nothing to verify" by accident, so the
CLI reports every outcome explicitly:

* exit ``0`` — one package name per line on stdout.
* exit ``3`` — no packages to test, with the reason on stderr as a
  ``no-packages:`` line. Either the change set is genuinely empty, or every
  changed file is outside the package code paths (docs, ``scripts/``,
  package config such as ``pyproject.toml``), or it maps only to the other
  language's packages.
* exit ``1`` — the change set has a code file (``.py`` / ``.ts`` / ``.tsx``)
  under ``packages/`` that no package mapping covers. Fails loudly with a
  ``Fix:`` block instead of returning empty.
* exit ``2`` — git failed (not a repo, ``origin/main`` missing, ...).
  ``Fix:`` block on stderr.

This module is intentionally dependency-free (stdlib only) to avoid requiring
a venv for the scripts/ directory.
"""
# venv: none
# permanent: true

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_UNMAPPED = 1
EXIT_GIT_ERROR = 2
EXIT_NO_PACKAGES = 3

_CODE_SUFFIXES = (".py", ".ts", ".tsx")


class GitError(RuntimeError):
    """A git command needed to build the change set failed."""


def derive_touched_packages(changed_files: list[str]) -> list[str]:
    """Return sorted list of Python packages with code changes.

    Counts ``.py`` files under ``packages/<name>/src/`` or
    ``packages/<name>/tests/``, plus a package-root ``conftest.py`` (it
    shapes the package's test run). Doc, config, and fixture changes are
    excluded.

    Args:
        changed_files: List of repo-relative file paths.

    Returns:
        Sorted list of package *names* (the ``<name>`` component, not the
        full path).  E.g. ``['nlp-pipeline', 'scraper-framework']``.
    """
    packages: set[str] = set()
    for f in changed_files:
        parts = f.split("/")
        if len(parts) < 3 or parts[0] != "packages" or not f.endswith(".py"):
            continue
        if (len(parts) >= 4 and parts[2] in ("src", "tests")) or (
            len(parts) == 3 and parts[2] == "conftest.py"
        ):
            packages.add(parts[1])
    return sorted(packages)


def derive_touched_ts_packages(changed_files: list[str]) -> list[str]:
    """Return sorted list of TypeScript packages with code changes.

    Counts ``.ts`` / ``.tsx`` files under ``packages/<name>/src/``,
    ``packages/<name>/app/`` or ``packages/<name>/tests/``, plus
    package-root config files such as ``vitest.config.ts``.

    Args:
        changed_files: List of repo-relative file paths.

    Returns:
        Sorted list of package *names* (the ``<name>`` component, not the
        full path).  E.g. ``['web']``.
    """
    packages: set[str] = set()
    for f in changed_files:
        parts = f.split("/")
        if len(parts) < 3 or parts[0] != "packages":
            continue
        if not f.endswith((".ts", ".tsx")):
            continue
        if (len(parts) >= 4 and parts[2] in ("src", "app", "tests")) or len(parts) == 3:
            packages.add(parts[1])
    return sorted(packages)


def unmapped_code_files(changed_files: list[str]) -> list[str]:
    """Return code files under ``packages/`` that neither mapper attributes.

    These are the paths that would otherwise make the helper print nothing
    while the change set really does touch package code.
    """
    unmapped = []
    for f in changed_files:
        if not f.startswith("packages/") or not f.endswith(_CODE_SUFFIXES):
            continue
        if not derive_touched_packages([f]) and not derive_touched_ts_packages([f]):
            unmapped.append(f)
    return sorted(unmapped)


def _git_lines(worktree: str | Path, args: list[str]) -> list[str]:
    """Run ``git -C <worktree> <args>`` and return its non-empty output lines.

    Raises:
        GitError: git is missing, timed out, or exited non-zero.
    """
    cmd = ["git", "-C", str(worktree), *args]
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=30
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise GitError(f"`{' '.join(cmd)}` failed: {exc}") from exc
    if result.returncode != 0:
        raise GitError(
            f"`{' '.join(cmd)}` exited {result.returncode}: {result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


def git_diff_names(worktree: str | Path, base: str = "origin/main") -> list[str]:
    """Return the de-duplicated, sorted change set of the worktree.

    Union of committed changes vs the merge base with *base*, staged and
    unstaged changes vs HEAD, staged changes, and untracked files. All
    paths are repo-root-relative.

    Raises:
        GitError: any of the git commands failed. The caller must not treat
            this as "no changes".
    """
    names: set[str] = set()
    names.update(_git_lines(worktree, ["diff", "--name-only", f"{base}...HEAD"]))
    names.update(_git_lines(worktree, ["diff", "--name-only", "HEAD"]))
    names.update(_git_lines(worktree, ["diff", "--cached", "--name-only"]))
    names.update(
        _git_lines(
            worktree, ["ls-files", "--others", "--exclude-standard", "--full-name"]
        )
    )
    return sorted(names)


def _no_packages_reason(changed: list[str], ts_mode: bool) -> str:
    """Explain why the change set maps to no package of the requested kind."""
    if not changed:
        return (
            "change set is empty (no committed, staged, unstaged or untracked "
            "changes vs origin/main)"
        )
    other = (
        derive_touched_packages(changed)
        if ts_mode
        else derive_touched_ts_packages(changed)
    )
    kind, other_kind, other_flag = (
        ("TypeScript", "Python", "without --ts")
        if ts_mode
        else ("Python", "TypeScript", "--ts")
    )
    if other:
        return (
            f"no {kind} packages touched; {len(changed)} changed file(s) map to "
            f"{other_kind} package(s) {', '.join(other)} (run {other_flag})"
        )
    sample = ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else "")
    return (
        f"{len(changed)} changed file(s), none under a package's code paths "
        f"(docs / scripts / config-only change): {sample}"
    )


def run(worktree: str | Path, ts_mode: bool = False) -> int:
    """Print touched packages for *worktree* and return the exit code."""
    try:
        changed = git_diff_names(worktree)
    except GitError as exc:
        print(f"FAIL: could not read the change set: {exc}", file=sys.stderr)
        print(
            "Fix:\n"
            "  git fetch origin main          # origin/main must resolve\n"
            f"  git -C {worktree} status       # must be a git work tree\n"
            "  # then re-run scripts/ralph_touched_packages.py",
            file=sys.stderr,
        )
        return EXIT_GIT_ERROR

    unmapped = unmapped_code_files(changed)
    if unmapped:
        print(
            "FAIL: code files under packages/ map to no package, so ralph's "
            "full-suite step would skip them:",
            file=sys.stderr,
        )
        for f in unmapped:
            print(f"  {f}", file=sys.stderr)
        print(
            "Fix:\n"
            "  # Run the owning package's full suite by hand, e.g.\n"
            "  packages/<pkg>/.venv/bin/pytest packages/<pkg>/tests/   # Python\n"
            "  npm test --prefix packages/<pkg>                       # TypeScript\n"
            "  # and teach derive_touched_packages / derive_touched_ts_packages in\n"
            "  # scripts/ralph_touched_packages.py about the new path.",
            file=sys.stderr,
        )
        return EXIT_UNMAPPED

    pkgs = (
        derive_touched_ts_packages(changed)
        if ts_mode
        else derive_touched_packages(changed)
    )
    if not pkgs:
        print(f"no-packages: {_no_packages_reason(changed, ts_mode)}", file=sys.stderr)
        return EXIT_NO_PACKAGES

    for pkg in pkgs:
        print(pkg)
    return EXIT_OK


def main() -> None:
    """CLI entry point.

    Usage::

        python3 scripts/ralph_touched_packages.py [--ts] [<worktree>]

    If *worktree* is omitted, the current directory is used. See the module
    docstring for the exit codes.
    """
    args = sys.argv[1:]
    ts_mode = "--ts" in args
    args = [a for a in args if a != "--ts"]
    worktree = Path(args[0]) if args else Path(".")
    sys.exit(run(worktree, ts_mode=ts_mode))


if __name__ == "__main__":
    main()
