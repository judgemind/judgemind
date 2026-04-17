#!/usr/bin/env python3
# venv: none
"""Enforce the ``# one-off: true`` / ``# permanent: true`` header convention.

Every top-level script under ``scripts/`` (excluding the check script itself
and the ``archive``/``eval``/``tests``/``spotcheck`` subdirectories) must
carry exactly one of:

* ``# one-off: true`` — finite-lifetime script (backfill, migration,
  cleanup). Candidate for archival once its work is done.
* ``# permanent: true`` — re-runnable utility (parameterizable, idempotent,
  intended to be invoked repeatedly). Exempt from one-off staleness checks.

Historical note: the original (#2533) convention only required a marker on
scripts whose filename matched a set of name fragments (``backfill``,
``cleanup``, ``fix``, ``dedup``, ``merge``, ``migrate``, ``remediat``).
#2547 extended the requirement to ALL top-level scripts so the
``/audit`` skill could use ``count(# permanent: true) + HEADROOM`` as a
self-adjusting threshold for script-directory accumulation — a new
permanent utility raises the threshold automatically; a new one-off
consumes a slot of headroom, correctly flagging when too many one-offs
accumulate.

The marker must appear anywhere in the first 50 lines of the file (the
``# venv:`` / ``# one-off:`` / ``# permanent:`` headers sit immediately
before or after the module docstring, so a docstring up to ~40 lines still
leaves room for the marker). See #2533 for the original 50-line window
rationale.

Usage:
    scripts/check-script-headers.py            # scan scripts/ (default)
    scripts/check-script-headers.py PATH ...   # scan specific paths
    scripts/check-script-headers.py --count    # emit JSON marker counts (for audit)
    scripts/check-script-headers.py --narrow   # historical #2533 check (name-pattern only)

Exit codes:
    0 — All scripts carry a marker (or, with --count, counts emitted OK).
    1 — One or more scripts are missing a marker.

Ref:
    - https://github.com/judgemind/judgemind/issues/2533 (original convention)
    - https://github.com/judgemind/judgemind/issues/2547 (extend to all scripts)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Name fragments that historically signalled a one-off script. These are
# retained for backwards compatibility (``matches_one_off_pattern`` /
# ``find_unmarked_scripts``) but are no longer used by the default check —
# every top-level script now requires a marker regardless of its name.
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
_PERMANENT_RE = re.compile(r"^#\s*permanent:\s*true\s*$")
_ONE_OFF_RE = re.compile(r"^#\s*one-off:\s*true\s*$")

# File basenames that should never be scanned even if they would otherwise
# be included. The check script itself is listed here — it IS the marker
# convention, so it has no need to declare itself.
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
    """Return True if ``filename`` matches one of the historical one-off name fragments.

    Retained for backwards compatibility with the #2533 convention. The
    default check no longer uses this function — every top-level script
    now requires a marker regardless of its name (#2547). The ``--narrow``
    CLI flag still invokes the historical behaviour for callers that
    want the original scan.
    """

    name = filename.lower()
    return any(frag in name for frag in ONE_OFF_NAME_PATTERNS)


def _read_header_lines(path: Path, window: int = HEADER_WINDOW_LINES) -> list[str]:
    """Return up to ``window`` leading lines of ``path`` (safe on unreadable files)."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return text.splitlines()[:window]


def has_marker(path: Path, window: int = HEADER_WINDOW_LINES) -> bool:
    """Return True if the file at ``path`` carries a marker in its first ``window`` lines.

    The marker must appear as a standalone top-level comment line matching
    ``# one-off: true`` or ``# permanent: true`` (whitespace tolerated).
    """

    for line in _read_header_lines(path, window):
        if _MARKER_RE.match(line):
            return True
    return False


def has_permanent_marker(path: Path, window: int = HEADER_WINDOW_LINES) -> bool:
    """Return True if the file carries a ``# permanent: true`` marker."""

    for line in _read_header_lines(path, window):
        if _PERMANENT_RE.match(line):
            return True
    return False


def has_one_off_marker(path: Path, window: int = HEADER_WINDOW_LINES) -> bool:
    """Return True if the file carries a ``# one-off: true`` marker."""

    for line in _read_header_lines(path, window):
        if _ONE_OFF_RE.match(line):
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
    """Return candidate scripts that match the one-off name pattern but lack a marker.

    Historical (#2533) narrow behaviour: only flags scripts whose filename
    matches a one-off name fragment. Retained for backwards compatibility
    with callers that want the narrow scan (CLI ``--narrow``).
    """

    unmarked: list[Path] = []
    for path in iter_candidate_files(roots):
        if not matches_one_off_pattern(path.name):
            continue
        if has_marker(path):
            continue
        unmarked.append(path)
    return unmarked


def find_unmarked_all_scripts(roots: list[Path]) -> list[Path]:
    """Return every candidate script that lacks a marker.

    New (#2547) default behaviour: every top-level script must carry
    either ``# one-off: true`` or ``# permanent: true``. A marker-less
    script would otherwise raise the audit's computed threshold (``total
    > permanent_count + HEADROOM``) without contributing to the legitimate
    baseline, eventually triggering false-positive ratchet findings.
    """

    unmarked: list[Path] = []
    for path in iter_candidate_files(roots):
        if has_marker(path):
            continue
        unmarked.append(path)
    return unmarked


def count_markers(roots: list[Path]) -> dict[str, int]:
    """Return marker counts across candidate scripts.

    Keys:
        total       — total candidate scripts (excluding exempt/archive).
        permanent   — scripts carrying ``# permanent: true``.
        one_off     — scripts carrying ``# one-off: true``.
        unmarked    — scripts with no marker at all.

    The sum of ``permanent + one_off + unmarked`` equals ``total``. Used
    by ``/audit`` §1.9 to compute the self-adjusting threshold
    ``permanent + HEADROOM``.
    """

    total = 0
    permanent = 0
    one_off = 0
    unmarked = 0
    for path in iter_candidate_files(roots):
        total += 1
        if has_permanent_marker(path):
            permanent += 1
        elif has_one_off_marker(path):
            one_off += 1
        else:
            unmarked += 1
    return {
        "total": total,
        "permanent": permanent,
        "one_off": one_off,
        "unmarked": unmarked,
    }


def _default_roots() -> list[Path]:
    """Resolve to the top-level ``scripts/`` directory the check script lives in."""

    scripts_dir = Path(__file__).resolve().parent
    return [scripts_dir]


def _print_unmarked_error(unmarked: list[Path]) -> None:
    print(
        "ERROR: top-level scripts lack a `# one-off: true` or "
        "`# permanent: true` marker:",
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=("Enforce one-off/permanent marker convention on scripts/*.py."),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "Paths to scan (files or directories). Defaults to the "
            "top-level scripts/ directory."
        ),
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help=(
            "Emit JSON marker counts (total/permanent/one_off/unmarked) "
            "instead of running the enforcement check. Used by /audit §1.9 "
            "to compute the self-adjusting threshold."
        ),
    )
    parser.add_argument(
        "--narrow",
        action="store_true",
        help=(
            "Use the historical (#2533) narrow check: only flag scripts "
            "whose filename matches the one-off name pattern. The default "
            "(#2547) is to flag every top-level script without a marker."
        ),
    )
    args = parser.parse_args(argv)

    if args.paths:
        roots = [Path(p) for p in args.paths]
    else:
        roots = _default_roots()

    if args.count:
        counts = count_markers(roots)
        print(json.dumps(counts, indent=2, sort_keys=True))
        return 0

    if args.narrow:
        unmarked = find_unmarked_scripts(roots)
    else:
        unmarked = find_unmarked_all_scripts(roots)

    if not unmarked:
        return 0

    _print_unmarked_error(unmarked)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
