#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-no-check-prefix-on-ecs-oneshots.py — Hygiene guard enforcing
the naming convention from #4558: ECS-oneshot data-check scripts MUST
NOT be named ``scripts/check-*.{sh,py}``.

Why this check exists
---------------------
The ``scripts/check-*.{sh,py}`` glob is the umbrella's auto-discovery
namespace for **code-quality CI guards** — guards that run blind on the
local tree, take no arguments, and fail loudly when the tree contains a
forbidden pattern. ECS-oneshot data-check scripts (those with
``# venv: <pkg>`` + ``# permanent: true`` headers, invoked by
``scripts/ecs-run-task.sh`` from a scheduled GitHub Actions workflow with
required arguments like ``--date YYYY-MM-DD``) collide with this glob
whenever they are named ``check-*``. Three cascading pre-push failures
result:

1. ``scripts/check-ci-guards-skip-list-coverage.sh`` (#11a) flags the
   script for missing-from-SKIP_LIST coverage, because every executable
   ``scripts/check-*.{sh,py}`` with required-arg signatures must be in
   ``run-ci-guards.sh``'s ``SKIP_LIST`` or carry the
   ``# ci-guards: skip`` opt-out marker.
2. ``scripts/check-fix-block-coverage-complete.sh`` (#23a) flags the
   script for missing-from-inventory coverage, because every executable
   ``scripts/check-*.{sh,py}`` in the tree must carry a row in
   ``docs/dx/check-script-fix-block-coverage.md``.
3. ``scripts/run-ci-guards.sh`` blindly invokes the script with no args
   and exits non-zero on the strict-required argument's parameter-not-set
   exit.

#4558 documented the rule (passive docs + remediation hints in the
sibling guards' Fix blocks). This guard is the active enforcement —
fail at hygiene-check time when any ``scripts/check-*.{sh,py}`` is shaped
like an ECS-oneshot data-check script, BEFORE the cascading failures
trigger.

Sibling pattern: ``scripts/check-ci-guards-skip-list-coverage.py``
(#4379), ``scripts/check-fix-block-coverage-complete.sh`` (#4367).

What counts as an ECS-oneshot data-check script
------------------------------------------------
ALL THREE signals must be present (any one alone is too weak — many
legitimate CI guards carry ``# permanent: true`` and required argparse
arguments without being ECS oneshots):

1. ``# venv: <pkg>`` header where ``<pkg>`` is anything other than
   ``none``. ``# venv: none`` means the script runs under the
   stdlib-only ``python3`` interpreter; an ECS oneshot's whole point
   is to run inside a packaged ``.venv`` with non-stdlib dependencies
   (boto3, psycopg, scraper-framework, etc.).
2. ``# permanent: true`` header. ECS oneshots are re-runnable utilities
   (parameterizable, idempotent) — never one-shot fixups.
3. **At least one** of:
   - Python ``argparse``: ``required=True`` on any ``add_argument``
     call (or ``add_mutually_exclusive_group(required=True)``).
   - Shell: ``${1:?...}`` strict-required positional (the canonical
     "first positional is required" pattern).

A guard with header signals 1 + 2 but no required-arg signature is NOT
an ECS oneshot — it is an unusual "permanent script that happens to
need a packaged venv" (rare but legitimate). We do not flag those
because the third signal is what unambiguously distinguishes "lives
in a scheduled cron, takes ``--date YYYY-MM-DD``" from "lives in
``scripts/`` permanently and runs without arguments."

Allowlist (built-in, grandfathered)
-----------------------------------
Three pre-existing scripts carry ``check-`` prefixes for historical
reasons and live in ``run-ci-guards.sh``'s ``SKIP_LIST``:

* ``check-scraper-zero-record-streak.py``
* ``check-scraper-zero-record-runner.py``
* ``check-short-unsubstantive-rulings.py``

These are documented in ``docs/agent/code-standards.md`` §"Naming
convention: don't name ECS-oneshot data-check scripts
``scripts/check-*.{sh,py}``" as the canonical-rename targets the next
time they are touched. Until then the allowlist exempts them so this
guard does not fail on the back catalog. New ECS-oneshot data-check
scripts MUST NOT be added to this allowlist — the canonical fix is to
not use the ``check-`` prefix at all (e.g. rename to
``scripts/audit_foo_data.py`` or ``scripts/foo_data.py``).

CLI
---
::

    scripts/check-no-check-prefix-on-ecs-oneshots.py            # scan repo, fail on violations
    scripts/check-no-check-prefix-on-ecs-oneshots.py --list     # print discovered set, exit 0
    scripts/check-no-check-prefix-on-ecs-oneshots.py --scripts-dir DIR
                                                                # scan a custom dir

Exit codes
----------
* ``0`` — every ``check-*.{sh,py}`` is either NOT shaped like an ECS
  oneshot OR is in the grandfathered allowlist; OR the ``--list`` flag
  was passed and the discovered set was printed.
* ``1`` — one or more guards match all three ECS-oneshot signals AND
  are not in the allowlist. A ``Fix:`` block names the canonical
  rename + the SKIP_LIST/inventory/marker fallback.
* ``2`` — script error (scripts/ dir missing, etc.).

Tracking: issue #4563 (parent: #4558).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# Window size for header scanning. Mirrors the 50-line window in
# ``check-script-headers.py`` — long module docstrings push the
# ``# permanent: true`` marker as far as line 32 in some scripts
# (e.g. backfill_llm_enrichment.py at filing time).
_HEADER_WINDOW_LINES: int = 50

# ``# venv: <pkg>`` header. We capture the package name so we can
# distinguish ``none`` (stdlib-only — not an ECS oneshot) from
# ``scraper-framework`` / ``api`` / etc. (packaged venv — could be).
_VENV_HEADER_RE = re.compile(r"^\s*#\s*venv:\s*(\S+)\s*$")

# ``# permanent: true`` header. Same regex as
# ``check-script-headers.py``.
_PERMANENT_HEADER_RE = re.compile(r"^\s*#\s*permanent:\s*true\s*$")

# Python argparse ``required=True`` — same regex as
# ``check-ci-guards-skip-list-coverage.py``. Matches both
# ``add_argument(..., required=True, ...)`` and
# ``add_mutually_exclusive_group(required=True)``.
_PY_REQUIRED_TRUE_RE = re.compile(r"\brequired\s*=\s*True\b")

# Shell ``${1:?...}`` — strict-required positional. Same regex as
# ``check-ci-guards-skip-list-coverage.py``.
_SH_REQUIRED_POSITIONAL_RE = re.compile(r"\$\{1:\?")

# Self-exempt: this check's own filename. The check itself has no
# ``# venv:`` non-none header (it is ``# venv: none``), so it would
# not match anyway — but exclude defensively in case a future
# refactor changes that.
_SELF_BASENAMES: frozenset[str] = frozenset(
    {
        "check-no-check-prefix-on-ecs-oneshots.py",
        "check-no-check-prefix-on-ecs-oneshots.sh",
    }
)

# Grandfathered allowlist. These three scripts pre-date #4558's naming
# rule and carry ``check-`` prefixes for historical reasons. They live
# in ``run-ci-guards.sh``'s ``SKIP_LIST`` and are exempt from this
# guard. New ECS-oneshot data-check scripts MUST NOT be added to this
# allowlist — the canonical fix is to not use the ``check-`` prefix
# at all. See ``docs/agent/code-standards.md`` §"Naming convention".
_GRANDFATHERED_ALLOWLIST: frozenset[str] = frozenset(
    {
        "check-scraper-zero-record-streak.py",
        "check-scraper-zero-record-runner.py",
        "check-short-unsubstantive-rulings.py",
    }
)


# ─────────────────────────────────────────────────────────────────────
# Header detection
# ─────────────────────────────────────────────────────────────────────


def _read_header_lines(path: Path, window: int = _HEADER_WINDOW_LINES) -> list[str]:
    """Return up to ``window`` leading lines of ``path``.

    Returns an empty list if the file is unreadable / non-UTF-8 — the
    caller treats "no header signals detected" as "not an ECS oneshot,"
    so failing closed is safe.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return text.splitlines()[:window]


def venv_header(path: Path) -> str | None:
    """Return the ``<pkg>`` from a ``# venv: <pkg>`` header, or None.

    ``None`` is returned when no header is present. ``"none"`` is a
    distinct return value (matching ``# venv: none``) — callers MUST
    treat ``"none"`` as "not an ECS oneshot." The distinction lets
    debugging surface "header parsed but pkg was none" vs. "no header
    at all" separately.
    """

    for line in _read_header_lines(path):
        m = _VENV_HEADER_RE.match(line)
        if m is not None:
            return m.group(1)
    return None


def has_packaged_venv_header(path: Path) -> bool:
    """Return True iff the file has ``# venv: <pkg>`` with ``<pkg>`` ≠ ``none``."""

    pkg = venv_header(path)
    return pkg is not None and pkg != "none"


def has_permanent_header(path: Path) -> bool:
    """Return True iff the file has ``# permanent: true`` in its header."""

    for line in _read_header_lines(path):
        if _PERMANENT_HEADER_RE.match(line):
            return True
    return False


def has_python_required_arg(path: Path) -> bool:
    """Return True if the Python script has ``required=True`` anywhere.

    Mirrors :func:`scripts.check_ci_guards_skip_list_coverage.has_python_required_arg`
    — same conservative regex, same false-positive characterisation.
    Plain regex is used rather than AST because the ``required=True``
    token is not a thing one writes in prose, so the FP risk is
    negligible.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_PY_REQUIRED_TRUE_RE.search(text))


def has_shell_required_positional(path: Path) -> bool:
    """Return True if the shell script uses ``${1:?...}``.

    Mirrors :func:`scripts.check_ci_guards_skip_list_coverage.has_shell_required_positional`
    — same conservative regex.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_SH_REQUIRED_POSITIONAL_RE.search(text))


# ─────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────


def is_ecs_oneshot(path: Path) -> bool:
    """Return True iff ``path`` matches all three ECS-oneshot signals.

    ALL THREE must be present — see the module docstring for why a
    weaker rule (any one or two signals) would over-flag legitimate CI
    guards.
    """

    if not has_packaged_venv_header(path):
        return False
    if not has_permanent_header(path):
        return False

    suffix = path.suffix.lower()
    if suffix == ".py":
        return has_python_required_arg(path)
    if suffix == ".sh":
        return has_shell_required_positional(path)
    # Other extensions (none in practice — discovery globs ``*.sh`` /
    # ``*.py`` only) cannot be ECS oneshots.
    return False


def discover_check_scripts(scripts_dir: Path) -> list[Path]:
    """Return every ``scripts/check-*.{sh,py}`` file at the top level.

    Excludes the meta-check itself (``_SELF_BASENAMES``). Sorts the
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


def find_violations(scripts_dir: Path) -> list[Path]:
    """Return guards that match all three ECS-oneshot signals AND are not allowlisted."""

    violations: list[Path] = []
    for path in discover_check_scripts(scripts_dir):
        if path.name in _GRANDFATHERED_ALLOWLIST:
            continue
        if is_ecs_oneshot(path):
            violations.append(path)
    return violations


# ─────────────────────────────────────────────────────────────────────
# Fix block
# ─────────────────────────────────────────────────────────────────────


def _suggested_rename(name: str) -> str:
    """Return the canonical-rename suggestion for an offending guard.

    Drops the ``check-`` prefix entirely AND swaps any remaining hyphens
    for underscores. This matches the conventional ECS-oneshot naming
    in the tree (``audit_correctly_labeled_s3_orphans.py``,
    ``drain_splitter_carry_forward_clusters.py``,
    ``backfill_llm_enrichment.py``) — Python module names use
    underscores, and these scripts are imported by their tests.
    """

    suffix = Path(name).suffix
    stem = Path(name).stem
    # Drop the ``check-`` prefix.
    if stem.startswith("check-"):
        stem = stem[len("check-") :]
    # Hyphens → underscores for Python; keep hyphens for shell (shell
    # scripts in this repo use kebab-case).
    if suffix == ".py":
        stem = stem.replace("-", "_")
    return f"{stem}{suffix}"


def _format_fix_block(violations: list[Path]) -> str:
    """Build the copy-pasteable Fix block for an error path.

    The block prefers the canonical-rename remediation (Option A) and
    documents the SKIP_LIST + inventory + marker fallback as Option B
    for the rare case where a rename would break dashboards / runtime
    / incident-response muscle memory.
    """

    lines: list[str] = []
    lines.append("Fix: rename each offending guard to drop the `check-` prefix.")
    lines.append("ECS-oneshot data-check scripts (`# venv: <pkg>` + `# permanent:")
    lines.append("true` + required argparse / `${1:?}` positional) collide with")
    lines.append("the `scripts/check-*.{sh,py}` auto-discovery namespace, which")
    lines.append("is reserved for code-quality CI guards (run blind, take no")
    lines.append("arguments).")
    lines.append("")
    lines.append("  Option A — Canonical rename (preferred, drops the ECS oneshot")
    lines.append("  out of the `check-*` namespace entirely):")
    lines.append("")
    for path in violations:
        rename = _suggested_rename(path.name)
        lines.append(f"      git mv scripts/{path.name} scripts/{rename}")
    lines.append("")
    lines.append("  After the rename, update any scheduled-cron workflow that")
    lines.append("  invokes the script (`.github/workflows/<name>.yml`), any")
    lines.append("  inventory rows in `docs/dx/check-script-fix-block-coverage.md`")
    lines.append("  that mention the old name, and any test files under")
    lines.append("  `scripts/tests/` that import it.")
    lines.append("")
    lines.append("  Option B — Add to the grandfathered allowlist (LAST RESORT,")
    lines.append("  only when the rename would break dashboards / runtime /")
    lines.append("  incident-response muscle memory). Add the basename(s) to")
    lines.append("  `_GRANDFATHERED_ALLOWLIST` in")
    lines.append("  `scripts/check-no-check-prefix-on-ecs-oneshots.py` AND ensure")
    lines.append("  the script is in `scripts/run-ci-guards.sh`'s SKIP_LIST AND")
    lines.append("  has an inventory row in")
    lines.append("  `docs/dx/check-script-fix-block-coverage.md`. Cite the")
    lines.append("  rationale in the PR description — every avoided rename is")
    lines.append("  one more landmine for the next agent who touches the")
    lines.append("  umbrella runner.")
    lines.append("")
    lines.append("  See docs/agent/code-standards.md §\"Naming convention: don't")
    lines.append('  name ECS-oneshot data-check scripts scripts/check-*.{sh,py}"')
    lines.append("  for the full rationale (#4558).")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Hygiene guard: forbid ECS-oneshot data-check scripts in the "
            "scripts/check-*.{sh,py} namespace (enforces #4558's naming rule)."
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
        "--list",
        action="store_true",
        dest="list_mode",
        help=(
            "Print the discovered scripts/check-*.{sh,py} set and exit 0 "
            "without scanning for violations. Used by the AC's first "
            "Verify line."
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

    discovered = discover_check_scripts(scripts_dir)

    if args.list_mode:
        # Print one basename per line; the set is already sorted by
        # ``discover_check_scripts``.
        print(f"Discovered: {len(discovered)} check-*.{{sh,py}} guard(s)")
        for path in discovered:
            allowlisted = (
                " (grandfathered)" if path.name in _GRANDFATHERED_ALLOWLIST else ""
            )
            ecs = is_ecs_oneshot(path)
            tag = " [ECS-oneshot signals: ALL]" if ecs else ""
            print(f"  {path.name}{tag}{allowlisted}")
        return 0

    violations = find_violations(scripts_dir)
    if not violations:
        return 0

    print(
        "FAIL: scripts/check-*.{sh,py} guard(s) match ECS-oneshot data-check "
        "signals (`# venv: <pkg>` + `# permanent: true` + required-arg):",
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
