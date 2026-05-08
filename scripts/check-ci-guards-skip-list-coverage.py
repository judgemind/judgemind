#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-ci-guards-skip-list-coverage.py — Meta-check guarding the
``scripts/run-ci-guards.sh`` umbrella against guards that take required
arguments but are missing from the umbrella's ``SKIP_LIST`` (or the
per-file ``# ci-guards: skip`` opt-out marker).

Why this check exists
---------------------
``scripts/run-ci-guards.sh`` runs every executable ``scripts/check-*.sh``
and ``scripts/check-*.py`` blind from the local working tree, with no
arguments. Guards that *require* an argument (``--issue N``, positional
``${1:?}``, etc.) crash on blind invocation, and the umbrella reports the
crash as a failure even though the guard is fine when called from CI with
the right args. Two issues so far have shipped a new argument-required
guard and forgotten to add it to the umbrella's ``SKIP_LIST``: #4332
(motivating retro) → #4372 (most recent recurrence). Each follow-up cost
a full investigate-fix-CI-merge cycle.

This check is the structural fix. It scans every ``scripts/check-*.{sh,py}``
for hard-required argument signatures and fails when a hit is not covered
by either:

* the umbrella's ``SKIP_LIST`` (parsed live from
  ``scripts/run-ci-guards.sh``), or
* a top-level ``# ci-guards: skip`` opt-out marker (first 20 lines of the
  file — same window the umbrella scans).

What counts as "argument-required"
----------------------------------
**Python (argparse):**

* ``required=True`` on an ``add_argument`` call (any line in the file).
* ``add_mutually_exclusive_group(required=True)`` — at least one option
  in the group must be supplied.

**Shell:**

* ``${1:?...}`` — strict-required positional (canonical pattern, used by
  ``check-issue-author.sh`` and ``check-task-recovery.sh``).

Patterns intentionally NOT flagged (would produce false positives):

* ``${1:-}`` — defaulted positional. The script may or may not need an
  argument; many legitimate guards take an optional flag here.
* Environment-variable-required scripts (e.g. ``check-pr-title.sh`` reads
  ``PR_TITLE`` / ``PR_BODY`` from the environment). These run without a
  CLI arg but still need external context — they belong in ``SKIP_LIST``,
  but this check does not detect them syntactically. The category is
  small and stable; every member is already in the list.
* ``argparse`` arguments without ``required=True``. argparse's default
  is ``required=False``, so a missing optional flag is fine.

CLI
---
::

    scripts/check-ci-guards-skip-list-coverage.py            # scan repo
    scripts/check-ci-guards-skip-list-coverage.py --scripts-dir DIR
                                                             # scan a custom dir

Exit codes
----------
* ``0`` — every argument-required guard is in ``SKIP_LIST`` or carries the
  marker.
* ``1`` — one or more argument-required guards are missing. A ``Fix:``
  block names the offending file and the canonical remediation (add to
  ``SKIP_LIST`` or add ``# ci-guards: skip`` marker).
* ``2`` — script error (umbrella not found, scripts/ dir missing, etc.).

Tracking: issue #4379 (parent: #4332).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# Window size for the per-file ``# ci-guards: skip`` marker. Mirrors the
# 20-line window in ``scripts/run-ci-guards.sh::has_opt_out_marker``.
_MARKER_WINDOW_LINES: int = 20

# Marker regex — matches a standalone top-level comment line. Whitespace
# is tolerated. Mirrors the regex in ``run-ci-guards.sh``.
_OPT_OUT_MARKER_RE = re.compile(r"^\s*#\s*ci-guards:\s*skip\s*$")

# Python argparse: ``required=True`` anywhere in an ``add_argument`` or
# ``add_mutually_exclusive_group`` call. We tolerate whitespace around
# the ``=`` and accept a trailing ``,`` (signalling additional kwargs).
# Matching is line-oriented because argparse calls in this repo span
# at most a handful of lines and the kwarg lives on its own line in the
# canonical style — see ``check-issue-verify-sql.py:608`` for the
# reference shape.
_PY_REQUIRED_TRUE_RE = re.compile(r"\brequired\s*=\s*True\b")

# Python argparse: positional argument (no leading ``-``). Detection is
# best-effort — argparse positionals are required by default unless
# ``nargs="?"`` / ``"*"`` is given. We do NOT flag positionals here
# because they would produce false positives on the many ``argparse``
# scripts that accept ``"paths"`` with ``nargs="*"`` (zero or more —
# blind invocation works fine). Stick to the explicit ``required=True``
# signal.

# Shell: ``${1:?...}`` — strict-required positional with custom error.
# This is the canonical "first positional is required" pattern used by
# ``check-issue-author.sh`` and ``check-task-recovery.sh``. Variants
# like ``${1:?}`` (no message) are also matched.
_SH_REQUIRED_POSITIONAL_RE = re.compile(r"\$\{1:\?")

# SKIP_LIST extraction regex. Matches a ``"basename"`` literal inside the
# ``SKIP_LIST=( … )`` array block in ``scripts/run-ci-guards.sh``. We
# parse the live file rather than hard-coding the list so the meta-check
# stays in sync as the SKIP_LIST evolves.
_SKIP_LIST_BLOCK_RE = re.compile(
    r"^\s*SKIP_LIST=\(\s*$(.*?)^\s*\)\s*$",
    flags=re.MULTILINE | re.DOTALL,
)
_SKIP_LIST_ENTRY_RE = re.compile(r'"([^"]+)"')

# Self-exempt: this check's own filename. Even though it has no required
# args today (``--scripts-dir`` is optional), exclude it defensively so a
# future change that adds a required arg can't loop the check on itself.
_SELF_BASENAMES: frozenset[str] = frozenset(
    {
        "check-ci-guards-skip-list-coverage.py",
        "check-ci-guards-skip-list-coverage.sh",
    }
)


# ─────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────


def parse_skip_list(umbrella_path: Path) -> set[str]:
    """Parse the ``SKIP_LIST=( ... )`` array out of run-ci-guards.sh.

    Returns the set of basenames listed. Raises ``ValueError`` if the
    block can't be located — this is a fail-loud signal that the umbrella
    has been refactored in a way the meta-check doesn't recognise.
    """

    text = umbrella_path.read_text(encoding="utf-8")
    block_match = _SKIP_LIST_BLOCK_RE.search(text)
    if block_match is None:
        raise ValueError(
            f"Could not locate SKIP_LIST=( ... ) block in {umbrella_path}. "
            "The umbrella may have been refactored — update "
            "scripts/check-ci-guards-skip-list-coverage.py to match."
        )
    block_body = block_match.group(1)
    return {m.group(1) for m in _SKIP_LIST_ENTRY_RE.finditer(block_body)}


def has_opt_out_marker(path: Path, window: int = _MARKER_WINDOW_LINES) -> bool:
    """Return True if ``# ci-guards: skip`` appears in the first ``window`` lines."""

    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= window:
                    break
                if _OPT_OUT_MARKER_RE.match(line):
                    return True
    except (OSError, UnicodeDecodeError):
        return False
    return False


def has_python_required_arg(path: Path) -> bool:
    """Return True if the file contains ``required=True`` on any line.

    This catches both the ``add_argument(..., required=True, ...)`` and
    ``add_mutually_exclusive_group(required=True)`` forms — both are
    semantically "the user must supply this on the command line".

    A line containing ``required=True`` inside a comment or docstring
    technically also matches, but the regex is conservative enough that
    the false-positive risk is negligible: the ``required=True`` token is
    not a thing one writes in prose. The 100-line cap keeps the scan
    cheap on the largest scripts in the tree.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_PY_REQUIRED_TRUE_RE.search(text))


def has_shell_required_positional(path: Path) -> bool:
    """Return True if the shell script uses ``${1:?...}`` for the first arg.

    We restrict to ``${1:?...}`` rather than scanning all ``${N:?...}``
    because this is the canonical "first positional is required" idiom
    in this repo, and matches the AC literally (positional ``$1`` /
    ``${1:?}`` references with no default).
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_SH_REQUIRED_POSITIONAL_RE.search(text))


def is_argument_required(path: Path) -> bool:
    """Return True if the guard requires an argument to run blind.

    Dispatches on file extension:
      * ``.py`` → argparse ``required=True``.
      * ``.sh`` → strict positional ``${1:?...}``.

    Anything else returns False.
    """

    suffix = path.suffix.lower()
    if suffix == ".py":
        return has_python_required_arg(path)
    if suffix == ".sh":
        return has_shell_required_positional(path)
    return False


def discover_check_scripts(scripts_dir: Path) -> list[Path]:
    """Return every ``scripts/check-*.{sh,py}`` file at the top level.

    Excludes the meta-check itself (``_SELF_BASENAMES``).  Sorts the
    output alphabetically for deterministic violation ordering.
    """

    candidates: list[Path] = []
    for path in scripts_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix not in {".sh", ".py"}:
            continue
        name = path.name
        if not name.startswith("check-"):
            continue
        if name in _SELF_BASENAMES:
            continue
        candidates.append(path)
    candidates.sort(key=lambda p: p.name)
    return candidates


def find_violations(
    scripts_dir: Path,
    skip_list: set[str],
) -> list[Path]:
    """Return guards that require an arg but are missing from the SKIP_LIST.

    A guard is a violation iff ALL of:
      * it requires a CLI argument to run blind, AND
      * its basename is NOT in ``skip_list``, AND
      * it does NOT carry the ``# ci-guards: skip`` opt-out marker.

    The .sh-wrapper-companion logic from ``run-ci-guards.sh`` is NOT
    applied here — if a ``.py`` companion has ``required=True`` it
    would crash if the umbrella ever ran it (e.g. the .sh wrapper got
    deleted). Defensive coverage keeps the meta-check robust against
    future refactors.
    """

    violations: list[Path] = []
    for path in discover_check_scripts(scripts_dir):
        if not is_argument_required(path):
            continue
        if path.name in skip_list:
            continue
        if has_opt_out_marker(path):
            continue
        violations.append(path)
    return violations


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _format_fix_block(violations: list[Path]) -> str:
    """Build the copy-pasteable Fix block for an error path.

    The block names every violating guard plus the two remediation
    options (add to SKIP_LIST, or add ``# ci-guards: skip`` marker).
    The first option includes a literal SKIP_LIST entry the operator can
    paste into ``scripts/run-ci-guards.sh``.
    """

    lines: list[str] = []
    lines.append("Fix: each violating guard must be either added to the umbrella's")
    lines.append("SKIP_LIST or carry a `# ci-guards: skip` marker. Pick the option")
    lines.append("that matches what the guard does:")
    lines.append("")
    lines.append("  Option A — Add to SKIP_LIST (preferred for guards that need")
    lines.append("  external context like an issue number, body file, or PR number).")
    lines.append("  Edit scripts/run-ci-guards.sh and append a line to the SKIP_LIST")
    lines.append("  array (~ line 119):")
    lines.append("")
    for path in violations:
        lines.append(f'      "{path.name}"')
    lines.append("")
    lines.append("  Option B — Add the per-file opt-out marker. Add this line as a")
    lines.append("  top-level comment in the first 20 lines of each violating file:")
    lines.append("")
    lines.append("      # ci-guards: skip")
    lines.append("")
    lines.append("  Use Option A when the guard is generic infrastructure (lives in")
    lines.append("  scripts/ permanently and the requirement is intrinsic to its CLI).")
    lines.append("  Use Option B for ad-hoc guards that depend on uncommon context")
    lines.append("  (Docker, npm-install, packages/web/ deps, etc.).")
    lines.append("")
    lines.append("Reference: scripts/run-ci-guards.sh §SKIP_LIST entries (lines 37-66)")
    lines.append("for the decision rubric. Tracking: #4379 (parent #4332).")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Meta-check: every argument-required scripts/check-*.{sh,py} "
            "guard must be in run-ci-guards.sh's SKIP_LIST or carry the "
            "`# ci-guards: skip` marker."
        ),
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=None,
        help=(
            "Directory containing the check-*.{sh,py} guards "
            "(default: scripts/ relative to this script's location)."
        ),
    )
    parser.add_argument(
        "--umbrella",
        type=Path,
        default=None,
        help=(
            "Path to the umbrella runner whose SKIP_LIST array is parsed "
            "(default: <scripts-dir>/run-ci-guards.sh)."
        ),
    )
    args = parser.parse_args(argv)

    if args.scripts_dir is None:
        scripts_dir = Path(__file__).resolve().parent
    else:
        scripts_dir = args.scripts_dir.resolve()

    if not scripts_dir.is_dir():
        print(f"ERROR: scripts/ dir not found: {scripts_dir}", file=sys.stderr)
        return 2

    if args.umbrella is None:
        umbrella_path = scripts_dir / "run-ci-guards.sh"
    else:
        umbrella_path = args.umbrella.resolve()

    if not umbrella_path.is_file():
        print(f"ERROR: umbrella not found: {umbrella_path}", file=sys.stderr)
        return 2

    try:
        skip_list = parse_skip_list(umbrella_path)
    except (OSError, ValueError) as exc:
        print(
            f"ERROR: failed to parse SKIP_LIST from {umbrella_path}: {exc}",
            file=sys.stderr,
        )
        return 2

    violations = find_violations(scripts_dir, skip_list)
    if not violations:
        return 0

    print(
        "FAIL: argument-required guard(s) missing from "
        "scripts/run-ci-guards.sh SKIP_LIST:",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for path in violations:
        print(f"  - {path.name}", file=sys.stderr)
    print("", file=sys.stderr)
    print(_format_fix_block(violations), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
