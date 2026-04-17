#!/usr/bin/env python3
"""Enforce the ``# one-off: true`` / ``# permanent: true`` header convention.

Top-level scripts under ``scripts/`` whose filename matches a known one-off
name pattern (``backfill``, ``cleanup``, ``fix``, ``dedup``, ``merge``,
``migrate``, ``remediat``) must carry exactly one of:

* ``# one-off: true`` — finite-lifetime script (backfill, migration,
  cleanup). Candidate for archival once its work is done.
* ``# permanent: true`` — re-runnable utility (parameterizable, idempotent,
  intended to be invoked repeatedly). Exempt from one-off staleness checks.

The marker must appear anywhere in the first 50 lines of the file (the
``# venv:`` / ``# one-off:`` / ``# permanent:`` headers sit immediately
before or after the module docstring, so a docstring up to ~40 lines still
leaves room for the marker). The 50-line window replaces the historical
"first 10 lines" rule-of-thumb, which never matched reality — existing
scripts like ``backfill_llm_enrichment.py`` carry the marker at line 32
after a long docstring. See issue #2533.

Usage:
    scripts/check-script-headers.py            # scan scripts/ (default)
    scripts/check-script-headers.py PATH ...   # scan specific paths

Exit codes:
    0 — All matched scripts carry a marker.
    1 — One or more matched scripts are missing a marker.

Ref: https://github.com/judgemind/judgemind/issues/2533
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Name fragments that signal a one-off script. A file whose basename (minus
# ``.py``) contains any of these substrings is expected to carry either
# ``# one-off: true`` or ``# permanent: true``.
ONE_OFF_NAME_PATTERNS: tuple[str, ...] = (
    "backfill",
    "cleanup",
    "fix",
    "dedup",
    "merge",
    "migrate",
    "remediat",
)

# Number of leading lines scanned for the marker. 50 accommodates long
# module docstrings (see ``backfill_llm_enrichment.py`` whose docstring
# ends on line 29 and carries ``# permanent: true`` on line 32) while
# still being a hard upper bound so reviewers know where to look.
HEADER_WINDOW_LINES: int = 50

# Marker regex — matches a standalone top-level comment line. Whitespace
# is tolerated after the ``#`` and at end-of-line. The regex deliberately
# does NOT match the marker embedded in prose (e.g. inside a docstring)
# — it must be a real top-level comment.
_MARKER_RE = re.compile(r"^#\s*(?:one-off|permanent):\s*true\s*$")

# File basenames that should never be scanned even if they match the
# one-off name pattern by coincidence. The check script itself is listed
# here for documentation — its name does not actually match a pattern,
# but an explicit exemption keeps the intent clear.
_ALWAYS_EXEMPT_BASENAMES: frozenset[str] = frozenset(
    {
        "check-script-headers.py",
    }
)

# Subdirectory names within ``scripts/`` that are excluded from the
# scan. Anything under these paths is considered out-of-scope for the
# one-off/permanent convention.
_EXCLUDED_SUBDIRS: frozenset[str] = frozenset(
    {
        "archive",  # Archived one-off scripts — already retired.
        "eval",  # Evaluation harnesses, separate convention.
        "tests",  # Unit tests for scripts/ helpers.
        "spotcheck",  # Spot-check tooling, separate convention.
    }
)


def matches_one_off_pattern(filename: str) -> bool:
    """Return True if ``filename`` matches one of the one-off name fragments.

    Matches substring-style: ``backfill_llm_enrichment.py`` matches because
    ``backfill`` is in its name. ``check-test-except-pass.py`` does NOT match
    any fragment, so it is exempt from the marker requirement.
    """

    name = filename.lower()
    return any(frag in name for frag in ONE_OFF_NAME_PATTERNS)


def has_marker(path: Path, window: int = HEADER_WINDOW_LINES) -> bool:
    """Return True if the file at ``path`` carries a marker in its first ``window`` lines.

    The marker must appear as a standalone top-level comment line matching
    ``# one-off: true`` or ``# permanent: true`` (whitespace tolerated).
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    lines = text.splitlines()[:window]
    for line in lines:
        if _MARKER_RE.match(line):
            return True
    return False


def iter_candidate_files(roots: list[Path]) -> list[Path]:
    """Enumerate ``.py`` files under ``roots`` subject to the scan policy.

    For each root:
        * If it is a file, include it iff it is a ``.py`` file (and not
          in ``_ALWAYS_EXEMPT_BASENAMES``).
        * If it is a directory, iterate its immediate children
          (non-recursive); subdirectories listed in ``_EXCLUDED_SUBDIRS``
          are skipped. This matches the audit's scope: only top-level
          ``scripts/*.py`` files are subject to the convention; one-off
          scripts under ``scripts/archive/`` are, by definition, already
          retired.
    """

    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix == ".py" and root.name not in _ALWAYS_EXEMPT_BASENAMES:
                candidates.append(root)
            continue
        # Non-recursive: only direct children of the directory.
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                # Skip known subdirectories with separate conventions.
                # Other unexpected subdirectories are also skipped;
                # callers that want recursive behaviour should pass
                # subdir roots explicitly.
                continue
            if entry.suffix != ".py":
                continue
            if entry.name in _ALWAYS_EXEMPT_BASENAMES:
                continue
            candidates.append(entry)
    return candidates


def find_unmarked_scripts(roots: list[Path]) -> list[Path]:
    """Return candidate scripts that match the one-off name pattern but lack a marker."""

    unmarked: list[Path] = []
    for path in iter_candidate_files(roots):
        if not matches_one_off_pattern(path.name):
            continue
        if has_marker(path):
            continue
        unmarked.append(path)
    return unmarked


def main(argv: list[str]) -> int:
    # Default to scanning the top-level scripts/ directory if no args.
    if argv:
        roots = [Path(p) for p in argv]
    else:
        # Resolve relative to this script's location so invocation from
        # any cwd works (e.g. pre-push hook, CI, manual run).
        scripts_dir = Path(__file__).resolve().parent
        roots = [scripts_dir]

    unmarked = find_unmarked_scripts(roots)

    if not unmarked:
        return 0

    print(
        "ERROR: scripts matching the one-off name pattern lack a "
        "`# one-off: true` or `# permanent: true` marker:",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for path in unmarked:
        print(f"  {path}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Add ONE of the following as a top-level comment in the first "
        f"{HEADER_WINDOW_LINES} lines of each file:",
        file=sys.stderr,
    )
    print(
        "  # one-off: true    (finite-lifetime — backfill, migration, cleanup)",
        file=sys.stderr,
    )
    print(
        "  # permanent: true  (re-runnable utility, idempotent)",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "See docs/agent/code-standards.md §Python scripts for the convention.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
