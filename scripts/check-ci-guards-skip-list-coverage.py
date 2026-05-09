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

**Python (env-var):**

* Top-level ``os.environ["VAR"]`` reads — these raise ``KeyError`` when
  the variable is unset, so blind invocation crashes. Only file-scope
  reads (outside function and class bodies) are flagged; inside
  ``main()`` and helpers a ``KeyError`` is the caller's contract.

**Shell:**

* ``${1:?...}`` — strict-required positional (canonical pattern, used by
  ``check-issue-author.sh`` and ``check-task-recovery.sh``).
* ``${VAR:?...}`` for any uppercase ``VAR`` at file scope (outside
  function bodies) — strict-required env vars. Without this an unset
  ``$VAR`` propagates as the empty string and the guard often exits 0
  with empty input — silently green when it should be failing
  (#4384).

Patterns intentionally NOT flagged (would produce false positives):

* ``${1:-}`` / ``${VAR:-default}`` — defaulted positional / env var.
  The script supplies a fallback; many legitimate guards behave this
  way (e.g. ``check-pr-title.sh`` reads ``${PR_TITLE:-}`` and exits
  with a useful message when empty — it is in ``SKIP_LIST`` for the
  same reason though, since blind invocation is meaningless).
* ``${VAR:?}`` inside function bodies — the caller of the function is
  responsible for supplying the value, not the surrounding script's
  environment.
* ``argparse`` arguments without ``required=True``. argparse's default
  is ``required=False``, so a missing optional flag is fine.
* ``os.environ.get("VAR")`` without an immediate ``if not VAR: sys.exit()``
  guard — defaulted reads are safe at top level. The full
  ``get + falsy-check + sys.exit`` shape is theoretically detectable
  but produces noisy false positives on legitimate optional reads;
  prefer the unambiguous ``os.environ["VAR"]`` shape or label the
  guard with ``# ci-guards: skip``.

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
import ast
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

# Shell: ``${VAR:?...}`` — strict-required env var (uppercase identifier
# starting with a letter or underscore). The leading character cannot be
# a digit (that's a positional, handled by ``_SH_REQUIRED_POSITIONAL_RE``).
# This regex is applied per line; the brace-depth tracker in
# ``has_shell_required_env_var`` skips matches inside function bodies so
# helper functions that validate their own arguments don't trigger.
_SH_REQUIRED_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z_0-9]*):\?")

# Shell: bash function-open patterns. Both forms are common in the repo:
#   * ``name() {`` (POSIX-ish, most common)
#   * ``function name() {`` / ``function name {`` (ksh-ish)
# We track these to compute brace depth so ``${VAR:?}`` inside a function
# body doesn't get flagged.
_SH_FUNCTION_OPEN_RE = re.compile(
    r"^\s*(?:function\s+)?[a-zA-Z_][a-zA-Z_0-9]*\s*\(\)\s*\{",
)
_SH_FUNCTION_OPEN_NOPARENS_RE = re.compile(
    r"^\s*function\s+[a-zA-Z_][a-zA-Z_0-9]*\s*\{",
)

# Python: ``os.environ["VAR"]`` strict subscript access — raises
# ``KeyError`` if unset. We flag only top-level reads (outside function
# and class bodies) because a ``KeyError`` raised inside a helper is
# the caller's contract, but a ``KeyError`` raised at module import time
# crashes the guard before it can report anything useful. The detection
# uses ``ast`` (see :func:`has_python_required_env_var`) — there is no
# regex constant because matching the
# ``Subscript(value=Attribute(value=Name(id='os'), attr='environ'), …)``
# AST node shape is the unambiguous test.

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


def has_python_required_env_var(path: Path) -> bool:
    """Return True if the script reads ``os.environ["VAR"]`` at top level.

    Only module-scope subscript reads count — reads inside functions or
    classes are the caller's contract (the function may be invoked with
    a guarantee that the env var is set). A module-scope read crashes
    the guard at import time when the env var is unset, which is the
    failure mode the umbrella's blind invocation triggers.

    Detection uses ``ast`` so multi-line expressions, docstrings, and
    comments don't trip up the scanner. If the file is not parseable
    Python (syntax error, encoding issue), we fail closed and return
    False — a non-Python ``check-*.py`` won't import at all, so
    blind-invocation crashes are out of scope for this check.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    # Module-scope statements only — descend through ``If``/``Try``/``With``
    # bodies that live at module scope, but stop at ``FunctionDef``,
    # ``AsyncFunctionDef``, and ``ClassDef`` boundaries.
    def _scan(stmts: list[ast.stmt]) -> bool:
        for stmt in stmts:
            # Don't descend into function/class bodies — those are not
            # module scope.
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            # Walk every node in this statement looking for the subscript
            # pattern. We use ``ast.walk`` here — but it's bounded by the
            # statement's own subtree, so we don't accidentally descend
            # into a nested function/class.
            for node in ast.walk(stmt):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    # Skip nested defs (e.g. inside ``if __name__``).
                    # Replace with a no-descent marker by clearing the
                    # walk by checking the node type — but ast.walk
                    # already gave us the children. We can't prune
                    # mid-walk; instead, rely on the outer loop's
                    # explicit skip. Continue here just suppresses this
                    # node from matching.
                    continue
                if _is_os_environ_subscript(node):
                    return True
            # If this statement contains a nested function/class, recurse
            # into the module-scope-equivalent siblings (e.g. an ``if``
            # block at module scope is still module scope).
            if isinstance(stmt, ast.If):
                if _scan(stmt.body) or _scan(stmt.orelse):
                    return True
            elif isinstance(stmt, ast.Try):
                if (
                    _scan(stmt.body)
                    or any(_scan(h.body) for h in stmt.handlers)
                    or _scan(stmt.orelse)
                    or _scan(stmt.finalbody)
                ):
                    return True
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                if _scan(stmt.body):
                    return True
        return False

    return _scan(tree.body)


def _is_os_environ_subscript(node: ast.AST) -> bool:
    """Return True if ``node`` is an ``os.environ["VAR"]`` subscript read.

    Matches the canonical attribute-then-subscript shape:

        Subscript(value=Attribute(value=Name(id='os'), attr='environ'),
                  slice=...)

    We don't constrain the slice — any string literal, name, or
    expression counts. The point is that the access uses ``[]``, which
    raises ``KeyError`` on miss, rather than ``.get(...)`` which
    returns ``None`` on miss.
    """

    if not isinstance(node, ast.Subscript):
        return False
    target = node.value
    if not isinstance(target, ast.Attribute):
        return False
    if target.attr != "environ":
        return False
    if not isinstance(target.value, ast.Name):
        return False
    return target.value.id == "os"


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


def has_shell_required_env_var(path: Path) -> bool:
    """Return True if the shell script uses ``${VAR:?...}`` at file scope.

    File scope = outside function bodies. We track brace depth across the
    file, opening on a line that defines a function and closing on a
    matching ``}`` at the function's nesting level. A ``${VAR:?}`` match
    at depth 0 is an env-var-required pattern; matches inside function
    bodies are intentionally ignored (the function caller supplies the
    value).

    The implementation is intentionally a simple line-by-line scanner
    rather than full bash parsing — robust enough for the canonical
    patterns in this repo (top-of-file ``VAR="${ENV:?...}"`` reads),
    not robust enough for pathological cases (here-docs, nested string
    expansion, etc.). Pathological cases that bypass detection still
    have the per-file ``# ci-guards: skip`` marker as an escape hatch.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    in_function = False
    brace_depth = 0
    for line in text.splitlines():
        # Strip trailing comments — bash comments start with ``#``
        # outside string literals. We use a conservative heuristic: only
        # treat ``#`` as a comment marker when it follows whitespace or
        # is at the start of the line. This avoids stripping ``#`` in
        # strings like ``echo "color: #fff"`` while still ignoring
        # comment-trailers like ``ARG="${1:?}"  # required``.
        code_part = _strip_shell_trailing_comment(line)

        if not in_function:
            # Detect a function-open on this line (any of the canonical
            # bash function-definition shapes). When detected, switch
            # state and consume the open brace from this line's count.
            if _SH_FUNCTION_OPEN_RE.match(
                code_part
            ) or _SH_FUNCTION_OPEN_NOPARENS_RE.match(code_part):
                in_function = True
                # Count braces in the rest of the line (the open ``{``
                # is part of the function definition; subsequent ``{``
                # / ``}`` adjust depth).
                brace_depth = 0
                for ch in code_part:
                    if ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        brace_depth -= 1
                if brace_depth <= 0:
                    # Single-line function (e.g. ``f() { echo hi; }``).
                    in_function = False
                    brace_depth = 0
                continue
            # File-scope line: check for an env-var-required pattern.
            if _SH_REQUIRED_ENV_VAR_RE.search(code_part):
                return True
        else:
            # Inside a function body — track brace depth, ignore matches.
            for ch in code_part:
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
                    if brace_depth <= 0:
                        in_function = False
                        brace_depth = 0
                        break
    return False


def _strip_shell_trailing_comment(line: str) -> str:
    """Strip a trailing ``# ...`` comment, leaving leading whitespace intact.

    Conservative: only strips when ``#`` is at the start of the line or
    follows whitespace. Doesn't try to handle quoted ``#`` characters
    (those are rare in this repo's check-* scripts and the brace-depth
    tracker tolerates the noise).
    """

    # Anchor: ``#`` at start, or whitespace-then-``#``.
    m = re.search(r"(?:^|\s)#", line)
    if m is None:
        return line
    return line[: m.start()]


def required_kind(path: Path) -> str | None:
    """Return the ``required-kind`` of a guard, or None if not required.

    Possible kinds:
      * ``"py-arg"``    — argparse ``required=True``.
      * ``"py-env"``    — top-level ``os.environ["VAR"]`` read.
      * ``"sh-arg"``    — shell ``${1:?}`` strict-required positional.
      * ``"sh-env"``    — shell ``${VAR:?}`` strict-required env var.
      * ``None``        — no required-arg / required-env-var detected.

    Dispatches on file extension. The ordering matters when a guard has
    BOTH an argparse ``required=True`` AND an ``os.environ`` read — we
    report the argparse signal first because the canonical fix
    (SKIP_LIST or marker) doesn't change, only the Fix-block wording
    needs to mention the dominant shape. Most real guards will hit at
    most one kind anyway.
    """

    suffix = path.suffix.lower()
    if suffix == ".py":
        if has_python_required_arg(path):
            return "py-arg"
        if has_python_required_env_var(path):
            return "py-env"
        return None
    if suffix == ".sh":
        if has_shell_required_positional(path):
            return "sh-arg"
        if has_shell_required_env_var(path):
            return "sh-env"
        return None
    return None


def is_argument_required(path: Path) -> bool:
    """Return True if the guard requires an argument or env var to run blind.

    Backwards-compatible wrapper around :func:`required_kind`. Callers
    that need the failure-mode wording for the Fix block should use
    :func:`required_kind` directly.
    """

    return required_kind(path) is not None


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
      * it requires a CLI argument or env var to run blind, AND
      * its basename is NOT in ``skip_list``, AND
      * it does NOT carry the ``# ci-guards: skip`` opt-out marker.

    The .sh-wrapper-companion logic from ``run-ci-guards.sh`` is NOT
    applied here — if a ``.py`` companion has ``required=True`` it
    would crash if the umbrella ever ran it (e.g. the .sh wrapper got
    deleted). Defensive coverage keeps the meta-check robust against
    future refactors.
    """

    return [path for path, _kind in find_violations_with_kind(scripts_dir, skip_list)]


def find_violations_with_kind(
    scripts_dir: Path,
    skip_list: set[str],
) -> list[tuple[Path, str]]:
    """Like :func:`find_violations` but also returns the ``required-kind``.

    Each tuple is ``(path, kind)`` where ``kind`` is one of the values
    documented on :func:`required_kind`. Callers that build the Fix
    block use the kind to tailor the failure-mode wording.
    """

    violations: list[tuple[Path, str]] = []
    for path in discover_check_scripts(scripts_dir):
        kind = required_kind(path)
        if kind is None:
            continue
        if path.name in skip_list:
            continue
        if has_opt_out_marker(path):
            continue
        violations.append((path, kind))
    return violations


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


# Human-readable label for each ``required-kind`` value. Used in the
# Fix block so operators can tell at a glance which kind of "required"
# triggered the failure (CLI arg vs. env var) — they remediate via the
# same SKIP_LIST / marker path either way, but the wording shifts so
# the person reading the failure isn't left wondering whether their
# CLI argparse change was the trigger when actually it was an
# ``os.environ[...]`` read.
_KIND_LABELS: dict[str, str] = {
    "py-arg": "Python argparse required argument",
    "py-env": 'Python top-level os.environ["VAR"] read (env-var required)',
    "sh-arg": "shell ${1:?} strict-required positional",
    "sh-env": "shell ${VAR:?} strict-required env-var (environment variable)",
}


def _format_fix_block(violations: list[tuple[Path, str]]) -> str:
    """Build the copy-pasteable Fix block for an error path.

    The block names every violating guard plus its detected
    ``required-kind`` (so operators know whether the trigger was a CLI
    argument or an environment variable), followed by the two
    remediation options (add to SKIP_LIST, or add ``# ci-guards: skip``
    marker).  The first option includes a literal SKIP_LIST entry the
    operator can paste into ``scripts/run-ci-guards.sh``.
    """

    lines: list[str] = []
    lines.append("Fix: each violating guard must be either added to the umbrella's")
    lines.append("SKIP_LIST or carry a `# ci-guards: skip` marker. Pick the option")
    lines.append("that matches what the guard does:")
    lines.append("")
    lines.append("Detected required-kind per guard:")
    for path, kind in violations:
        label = _KIND_LABELS.get(kind, kind)
        lines.append(f"  - {path.name}: {label}")
    lines.append("")
    lines.append("  Option A — Add to SKIP_LIST (preferred for guards that need")
    lines.append("  external context like an issue number, body file, or PR number,")
    lines.append("  OR an environment variable like PR_TITLE / PR_BODY).")
    lines.append("  Edit scripts/run-ci-guards.sh and append a line to the SKIP_LIST")
    lines.append("  array (~ line 119):")
    lines.append("")
    for path, _kind in violations:
        lines.append(f'      "{path.name}"')
    lines.append("")
    lines.append("  Option B — Add the per-file opt-out marker. Add this line as a")
    lines.append("  top-level comment in the first 20 lines of each violating file:")
    lines.append("")
    lines.append("      # ci-guards: skip")
    lines.append("")
    lines.append("  Use Option A when the guard is generic infrastructure (lives in")
    lines.append("  scripts/ permanently and the requirement is intrinsic to its CLI")
    lines.append("  or its environment-variable contract).")
    lines.append("  Use Option B for ad-hoc guards that depend on uncommon context")
    lines.append("  (Docker, npm-install, packages/web/ deps, etc.).")
    lines.append("")
    lines.append("  Option C — rename without the `check-` prefix. For ECS-oneshot")
    lines.append("  data-check scripts (those with `# venv:` + `# permanent: true`")
    lines.append("  headers that are invoked by scripts/ecs-run-task.sh with a")
    lines.append("  required argument like --date YYYY-MM-DD, NOT code-quality CI")
    lines.append("  guards). The canonical rename drops the `check-` prefix")
    lines.append("  entirely:")
    lines.append("")
    for path, _kind in violations:
        stripped = path.name
        if stripped.startswith("check-"):
            stripped = stripped[len("check-") :]
        lines.append(f"      {path.name}  →  {stripped}")
    lines.append("")
    lines.append("  See docs/agent/code-standards.md §\"Naming convention: don't")
    lines.append('  name ECS-oneshot data-check scripts scripts/check-*.{sh,py}"')
    lines.append("  for the full rationale (#4558).")
    lines.append("")
    lines.append("Reference: scripts/run-ci-guards.sh §SKIP_LIST entries (lines 37-66)")
    lines.append("for the decision rubric. Tracking: #4558 (Option C / naming")
    lines.append("convention), #4384 (env-var extension), #4379 (parent meta-check),")
    lines.append("#4332 (motivating retro).")
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

    violations = find_violations_with_kind(scripts_dir, skip_list)
    if not violations:
        return 0

    # Tailor the FAIL header so the failure mode is visible from the
    # first stderr line — operators who see "env-var-required" know to
    # check ``os.environ[...]`` reads / ``${VAR:?}`` patterns and not
    # to spend cycles looking for new argparse arguments.
    kinds = {kind for _path, kind in violations}
    has_arg = any(k in {"py-arg", "sh-arg"} for k in kinds)
    has_env = any(k in {"py-env", "sh-env"} for k in kinds)
    if has_arg and has_env:
        header_kind = "argument- or env-var-required"
    elif has_env:
        header_kind = "env-var-required (environment variable)"
    else:
        header_kind = "argument-required"

    print(
        f"FAIL: {header_kind} guard(s) missing from "
        "scripts/run-ci-guards.sh SKIP_LIST:",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for path, kind in violations:
        label = _KIND_LABELS.get(kind, kind)
        print(f"  - {path.name} [{label}]", file=sys.stderr)
    print("", file=sys.stderr)
    print(_format_fix_block(violations), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
