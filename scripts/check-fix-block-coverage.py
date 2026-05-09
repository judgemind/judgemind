#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-fix-block-coverage.py — Regenerable survey of `scripts/check-*` guards.

Programmatically classifies every executable hygiene guard under
``scripts/check-*.{sh,py}`` against the verdict vocabulary used by
``docs/dx/check-script-fix-block-coverage.md``, and either:

  * ``--check`` (CI mode): asserts that the inventory's verdict for every
    guard matches the classifier output. Fails when the doc has drifted
    from reality (e.g. a guard was upgraded but its row was not).
  * ``--regenerate``: prints a fully regenerated survey table to stdout.
    Manual annotations in the Notes column are intentionally lost — this
    output is the verdict-only canonical form. Operators diff the
    regenerated table against the checked-in doc and either accept the
    auto-generated Notes or hand-merge their richer annotations.

The classifier never tries to reproduce hand-curated Notes. Verdict
correctness is the load-bearing invariant; Notes are operator commentary.

Why this exists
---------------
``docs/dx/check-script-fix-block-coverage.md`` (#4346) is a hand-authored
inventory of all ``scripts/check-*.{sh,py}`` hygiene guards. Each new
guard or guard upgrade requires a manual edit. ``#4349`` proposed a
regenerator script so the verdict column is self-maintaining and CI can
catch drift before merge.

A sibling guard ``scripts/check-fix-block-coverage-complete.sh`` (#23a)
already enforces presence — every guard must have a row. This script
adds the orthogonal invariant: presence is not enough, the verdict must
match the classifier's output.

Classification heuristic
------------------------
Priority order (first match wins):

  1. **decision flow (no violation list)** — script emits a verdict-shaped
     token at the start of an ``echo`` / ``print`` argument
     (``TRUSTED:`` / ``UNTRUSTED:`` / ``DONE:`` / ``RESUME:`` / ``UNKNOWN:``
     / ``duplicate:`` / ``ok:`` / ``shipped:`` / ``not-shipped:``).
     Decision-flow guards return a verdict to a caller; the fix is
     enacted by the caller.

  2. **self-diagnosing (Fix block)** — error path emits a labelled block
     marker (``Fix:`` / ``Fix options:`` / ``Remediation:`` / ``Required
     shape:`` / ``Required:`` / ``Recovery:`` / ``Example fix:`` / ``Fix
     recipes`` / ``Fix for ``) inside an ``echo`` / ``print`` /
     stderr-output statement. Wrappers that emit their own Fix block
     are caught here before the wrapper detector runs.

  3. **operational health probe** — file invokes ``aws ecs`` / ``aws ssm``
     / ``aws secretsmanager`` / ``curl http(s)://`` / ``docker build``
     / ``docker run`` substantively (commands at line start, not inside
     quoted strings or pattern variables), OR (for ``.py``) imports
     ``psycopg`` / ``boto3`` / ``requests`` and queries live state.
     Fix is judgment-driven (rotate the key, redeploy, investigate
     the trigger).

  4. **wrapper (delegates to helper)** — ``.sh`` whose primary action is
     ``exec python3 <helper>.py "$@"`` and which has no own Fix-block
     emission. The doc convention is to flag wrappers explicitly so
     reviewers can immediately see where the actual Fix-block lives.

  5. **self-diagnosing (actionable text)** — error path mentions ``Fix
     by`` / ``Replace with`` / ``Use:`` / ``Add a `` / ``Add tests`` /
     ``rename`` / ``MUST use`` / etc. but not in a labelled block.

  6. **Fall-through .py-to-.sh sibling lookup** — when a discovered
     underscore-named ``.py`` file (e.g.
     ``check_no_basicconfig_with_extra.py``) reaches this point with no
     direct verdict signal AND a hyphen-named ``.sh`` sibling exists,
     classify the ``.sh`` and inherit. Standalone helper rows in the
     inventory carry the verdict the operator actually sees on a CI
     failure — the ``.sh`` wrapper is the canonical entry point.

  7. **NEEDS UPGRADE** — guard emits only ``<file>:<line>: <message>`` or
     similar with no actionable Fix text and no labelled block.

CLI
---
::

    # CI mode (default). Exits 0 on match, 1 on drift, 2 on error.
    python3 scripts/check-fix-block-coverage.py --check

    # Print the regenerated table (with auto-Notes) to stdout.
    python3 scripts/check-fix-block-coverage.py --regenerate

    # Print just the verdicts, one per line: ``<basename>\\t<verdict>``.
    python3 scripts/check-fix-block-coverage.py --print

Issue tracking: #4349 (this script), #4346 (the inventory), #4367
(``check-fix-block-coverage-complete.sh`` sibling), #4322/#4345 (the
Fix-block contract).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ─── Verdict vocabulary ─────────────────────────────────────────────────

VERDICT_DECISION_FLOW = "decision flow (no violation list)"
VERDICT_OPERATIONAL = "operational health probe"
VERDICT_FIX_BLOCK = "self-diagnosing (Fix block)"
VERDICT_WRAPPER = "wrapper (delegates to helper)"
VERDICT_ACTIONABLE = "self-diagnosing (actionable text)"
VERDICT_NEEDS_UPGRADE = "NEEDS UPGRADE"

ALL_VERDICTS = (
    VERDICT_DECISION_FLOW,
    VERDICT_OPERATIONAL,
    VERDICT_FIX_BLOCK,
    VERDICT_WRAPPER,
    VERDICT_ACTIONABLE,
    VERDICT_NEEDS_UPGRADE,
)


# ─── Fixed-shape detectors ──────────────────────────────────────────────

# Fix-block markers that indicate "self-diagnosing (Fix block)" verdict.
# The marker appears inside an emitted string — i.e. inside an
# ``echo "..."`` / ``print(...)`` / similar. Detection is text-based
# (the marker inside a header comment block doesn't count, because
# ``_strip_comments`` removes header comments before this runs).
#
# Per-marker semantics:
#   - The labelled-block markers (``Fix:``, ``Fix options:``,
#     ``Remediation:``, ``Required shape:``, ``Recovery:``,
#     ``Example fix:``) match anywhere within an emitted string. The
#     historical doc treats e.g. ``check-bash-set-u-empty-array.sh``
#     (which prints ``Fix: replace 'declare -a'...`` mid-line as part of
#     a multi-echo prose block) as Fix-block-shaped — we mirror that.
#   - ``Fix for `` is a prefix-only marker for ``check_fix_block_
#     coverage_complete.py``-style "Fix for `<guard>`:" emissions.
_FIX_BLOCK_MARKERS = (
    "Fix:",
    "Fix options:",
    "Remediation:",
    "Required shape:",
    "Required: ",  # check-parse-document-reingest-safety.sh
    "Recovery:",
    "Example fix:",
    "Fix recipes",  # check-no-heredoc-pipe-shadow.sh "Fix recipes:" plural
    "Fix for ",  # check_fix_block_coverage_complete.py emits "Fix for `..`:"
)

# Actionable-text markers (case-insensitive). They appear inline in
# string literals. The vocabulary spans the imperative-verb shapes the
# existing inventory already classifies as actionable text.
_ACTIONABLE_MARKERS = (
    "Fix by",
    "Replace with",
    "Use:",
    "Add a ",
    "Add tests",
    "Add `",
    "Add the ",
    "rename ",
    "remove the ",
    "remove one ",
    "must emit",
    "must include",
    "MUST use",
    "must use",
    "use the shared helper",
    "use the canonical",
)

# Operational-health-probe shell-command shapes. We require the
# command to be invoked at the start of an executable line (after
# leading whitespace), not embedded in a string literal or a regex
# pattern variable. The pre-filter strips quoted strings before
# applying these patterns.
_OPERATIONAL_SHELL_PATTERNS = (
    r"^\s*aws\s+ecs\s+(?:describe|list|run|register|update|wait)",
    r"^\s*aws\s+ssm\s+(?:start|send|get|describe)",
    r"^\s*aws\s+secretsmanager\s+(?:get|describe)",
    r"^\s*aws\s+cloudwatch\s+(?:get|list|describe)",
    r"^\s*curl\s+(?:-[A-Za-z]+\s+)?[\"']?https?://",
    r"^\s*docker\s+(?:build|run|exec)\b",
    # Subshell + assignment shapes — `var=$(aws ecs describe ...)` and
    # `var=$(docker ...)`. These are how scripts capture command output.
    r"=\s*[\"']?\$\(\s*aws\s+ecs",
    r"=\s*[\"']?\$\(\s*docker\s+(?:build|run)",
    r"=\s*[\"']?\$\(\s*curl\s+",
)

# Operational-health-probe Python signals.
_OPERATIONAL_PY_IMPORTS = (
    "import psycopg",
    "from psycopg",
    "import boto3",
    "from boto3",
)
_OPERATIONAL_PY_TABLE_PATTERNS = (
    # Live-state query against telemetry.* schema or boto3 client calls
    # against AWS APIs.
    r"\btelemetry\.[a-z_]+\b",
    r"\.client\(['\"]\s*ecs\s*['\"]\)",
    r"\.client\(['\"]\s*ssm\s*['\"]\)",
)

# Wrapper detection: "exec python3 <something>.py ..." appearing at top
# level (not commented, not inside a function body that's never called).
# Two shapes are recognised:
#   (a) literal .py path:       exec python3 "$SCRIPT_DIR/check-foo.py" "$@"
#   (b) variable + .py assign:  HELPER_PY="$SCRIPT_DIR/_check_foo.py"
#                               ...
#                               exec python3 "$HELPER_PY" "$@"
# The detector accepts (a) directly, and (b) by pairing an `exec python3`
# line with a prior shell-variable assignment whose value ends in ``.py``.
_WRAPPER_EXEC_PATTERN_LITERAL = re.compile(
    r"^\s*exec\s+python3\s+[\"'\$\{][^\n]+\.py[\"'\}\s]",
    flags=re.MULTILINE,
)
_WRAPPER_EXEC_PATTERN_VARIABLE = re.compile(
    r"^\s*exec\s+python3\s+\"\$\{?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}?\"",
    flags=re.MULTILINE,
)
_PY_VAR_ASSIGN_RE = re.compile(
    r"^\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)=\"\$[A-Za-z_][A-Za-z0-9_]*[^\"]*\.py\"",
    flags=re.MULTILINE,
)


@dataclass(frozen=True)
class Guard:
    """One discovered hygiene guard."""

    path: Path
    basename: str

    @property
    def is_shell(self) -> bool:
        return self.basename.endswith(".sh")

    @property
    def is_python(self) -> bool:
        return self.basename.endswith(".py")


@dataclass(frozen=True)
class InventoryRow:
    """One row parsed from the survey table.

    ``rownum`` may carry a letter suffix (``"23"``, ``"23a"``).
    """

    rownum: str
    basename: str
    verdict: str
    notes: str


# ─── Discovery ──────────────────────────────────────────────────────────


def discover_guards(scripts_dir: Path, *, self_basename: str) -> list[Guard]:
    """Return every executable hygiene guard under ``scripts_dir``.

    Mirrors the discovery rules in
    ``scripts/check-fix-block-coverage-complete.sh``:

      * ``check-*.sh`` files must be executable to count.
      * ``check-*.py`` and ``check_*.py`` files always count (CI invokes
        them via ``python3``).
      * The umbrella wrapper itself is excluded.
      * Companion ``.py`` files are dropped when a ``.sh`` sibling exists
        with the same hyphen-stem (the ``.sh`` row covers both).
    """

    discovered: list[Guard] = []
    for path in sorted(scripts_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name == self_basename:
            continue
        if name.startswith("check-") and name.endswith(".sh"):
            # .sh must be executable.
            if path.stat().st_mode & 0o111 == 0:
                continue
        elif name.startswith("check-") and name.endswith(".py"):
            pass
        elif name.startswith("check_") and name.endswith(".py"):
            pass
        else:
            continue
        discovered.append(Guard(path=path, basename=name))

    # Drop check-foo.py when check-foo.sh is present (companion-pair
    # dedup). Underscore-named .py files (check_foo.py) have no .sh
    # sibling — they stand on their own.
    basenames = {g.basename for g in discovered}
    keepers: list[Guard] = []
    for g in discovered:
        if g.is_python and g.basename.startswith("check-"):
            sh_companion = g.basename[: -len(".py")] + ".sh"
            if sh_companion in basenames:
                continue
        keepers.append(g)
    return keepers


# ─── Classification ─────────────────────────────────────────────────────


def _strip_comments(content: str, *, language: str) -> str:
    """Remove comment lines from ``content`` so detectors don't false-match.

    For shell, drop lines whose first non-whitespace character is ``#``
    (preserving shebangs is unnecessary — they never carry markers).
    For Python, drop both ``#`` line comments and triple-quoted module
    docstrings at file scope. We leave inline ``#`` comments inside
    strings alone (we detect markers in stripped lines, so a trailing
    ``# Fix:`` comment will not register as a Fix block).
    """

    lines = content.split("\n")
    out: list[str] = []
    if language == "shell":
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            out.append(line)
        return "\n".join(out)

    # Python: strip module-scope docstrings (triple-quoted at file
    # start, possibly preceded by ``from __future__`` imports or a
    # shebang).  We use a simple state machine that walks character by
    # character — heavyweight but bug-tolerant for our use case.
    stripped_lines: list[str] = []
    in_docstring = False
    docstring_quote = ""
    for line in lines:
        if in_docstring:
            if docstring_quote in line:
                # Closing line — drop everything up to and including
                # the closing quote.
                idx = line.index(docstring_quote)
                rest = line[idx + len(docstring_quote) :]
                stripped_lines.append(rest)
                in_docstring = False
            continue

        # Detect opening triple-quoted docstring on a line that's
        # otherwise blank/leading-only.
        match = re.match(r"^\s*([rRbBuU]?)(\"\"\"|''')", line)
        if match:
            quote = match.group(2)
            after = line[match.end() :]
            if quote in after:
                # Single-line docstring — strip the whole line.
                continue
            in_docstring = True
            docstring_quote = quote
            continue

        # Drop pure-comment lines.
        if re.match(r"^\s*#", line):
            continue
        stripped_lines.append(line)

    return "\n".join(stripped_lines)


def _has_decision_flow_exit_codes(content: str) -> bool:
    """True when the script emits decision-shaped verdict tokens in its output.

    The decision-flow vocabulary used by ``/task`` and the dispatcher is
    a small fixed set of verdict tokens emitted on stdout/stderr:

      * ``TRUSTED:`` / ``UNTRUSTED:`` (issue-author check)
      * ``DONE:`` / ``RESUME:`` / ``UNKNOWN:`` (task-recovery check)
      * ``duplicate:`` / ``ok:`` (duplicate-PR check)
      * ``shipped:`` / ``not-shipped:`` (shipped-PR check)
      * ``plan-blocked:`` / ``clear:`` (issue-plan-blocked check, #4438)

    Decision-flow guards are characterised by emitting one of these
    tokens as the leading content of an ``echo`` / ``print`` line —
    callers parse the prefix out of stdout to take action. Hygiene
    guards (the vast majority) emit ``OK:`` / ``FAIL:`` / file:line
    violation lists instead.

    Detection: walk the content, find quoted strings emitted via echo/
    print, and look for a string that *starts with* one of the decision
    tokens. ``OK:`` and ``FAIL:`` are explicitly NOT in the token set —
    they're the hygiene-pass/fail vocabulary.
    """

    decision_tokens = (
        "TRUSTED:",
        "UNTRUSTED:",
        "DONE:",
        "RESUME:",
        "UNKNOWN:",
        "duplicate:",
        "ok:",
        "shipped:",
        "not-shipped:",
        "plan-blocked:",
        "clear:",
    )

    # Find emit lines (echo / print) and inspect their first quoted
    # argument. We also accept python f-strings.
    emit_re = re.compile(
        r"\b(?:echo|print|stderr\.write|sys\.stdout\.write|sys\.stderr\.write|cat\s*<<)"
    )
    for line in content.split("\n"):
        if not emit_re.search(line):
            continue
        strings = re.findall(r"\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'", line)
        for double, single in strings:
            inside = double or single
            for token in decision_tokens:
                if inside.startswith(token):
                    return True
    return False


def _strip_quoted_strings(line: str) -> str:
    """Remove ``"..."`` and ``'...'`` substrings from ``line``.

    Used by the operational-probe detector to ensure ``aws ecs ...``
    patterns embedded in pattern-variable string literals or echo
    arguments don't trigger a false positive. We don't try to handle
    every shell-quoting edge case (escape sequences, here-strings) —
    the goal is to remove the common "string content that mentions an
    AWS verb" shape.
    """

    return re.sub(
        r"\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'",
        "",
        line,
    )


def _is_operational_probe(*, content: str, language: str) -> bool:
    """True when the script targets live infrastructure rather than source code."""

    if language == "shell":
        for line in content.split("\n"):
            stripped = _strip_quoted_strings(line)
            for pat in _OPERATIONAL_SHELL_PATTERNS:
                if re.search(pat, stripped, flags=re.MULTILINE):
                    return True
        return False

    # Python.
    has_live_import = any(marker in content for marker in _OPERATIONAL_PY_IMPORTS)
    if has_live_import:
        # Confirm the import is used substantively — guard files
        # occasionally import boto3 only for type hints. We require at
        # least one psycopg/boto3 verb call site.
        if re.search(
            r"\.connect\(|\.cursor\(|\.client\(|\.fetchall\(|\.execute\(", content
        ):
            return True
    for pat in _OPERATIONAL_PY_TABLE_PATTERNS:
        if re.search(pat, content):
            return True
    return False


def _has_fix_block(content: str) -> bool:
    """True when an emitted string contains a Fix-block marker.

    Walks every non-comment line, finds quoted strings, and matches
    each marker per its declared semantics:

      * ``Fix for `` is a prefix-only marker (the "Fix for `<guard>`:"
        shape from ``check_fix_block_coverage_complete.py``).
      * Every other marker matches anywhere within the emitted string —
        the labelled block can appear mid-line in a multi-echo prose
        sequence (canonical example:
        ``check-bash-set-u-empty-array.sh``).
    """

    for line in content.split("\n"):
        strings = re.findall(r"\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'", line)
        for double, single in strings:
            inside = double or single
            stripped = inside.lstrip()
            for marker in _FIX_BLOCK_MARKERS:
                if marker.endswith(" "):
                    # Prefix-only marker.
                    if stripped.startswith(marker):
                        return True
                else:
                    if marker in inside:
                        return True
    return False


def _has_actionable_text(content: str) -> bool:
    """True when an emitted string contains an actionable-text marker."""

    for line in content.split("\n"):
        strings = re.findall(r"\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'", line)
        for double, single in strings:
            inside = double or single
            for marker in _ACTIONABLE_MARKERS:
                if marker.lower() in inside.lower():
                    return True
    return False


def _is_wrapper(content: str) -> bool:
    """True when the script's primary action is ``exec python3 <helper>.py``.

    The wrapper test is conservative — we require that an ``exec
    python3`` line appears at top level (not inside a function), and
    that no Fix-block markers are emitted by the script itself. The
    caller checks Fix-block emission *first*; if we reach this branch
    the script doesn't emit a Fix block of its own.

    Recognises two shapes:

      * Literal path: ``exec python3 "$SCRIPT_DIR/check-foo.py" "$@"``.
      * Variable-indirect: ``HELPER_PY="$SCRIPT_DIR/_check_foo.py"`` /
        later ``exec python3 "$HELPER_PY" "$@"``. We resolve the
        variable name from the exec line and confirm it was assigned a
        ``.py``-suffixed value earlier in the file.
    """

    if _WRAPPER_EXEC_PATTERN_LITERAL.search(content):
        return True

    var_match = _WRAPPER_EXEC_PATTERN_VARIABLE.search(content)
    if var_match is None:
        return False
    var_name = var_match.group("var")
    # Confirm a prior assignment to this variable contains ``.py``.
    for assign in _PY_VAR_ASSIGN_RE.finditer(content):
        if assign.group("var") == var_name:
            return True
    return False


def _extract_header(content: str, *, language: str) -> str:
    """Return the leading comment block (shell) or module docstring (Python)."""

    if language == "shell":
        out: list[str] = []
        for line in content.split("\n"):
            if line.startswith("#!"):
                continue
            stripped = line.lstrip()
            if stripped.startswith("#"):
                out.append(stripped[1:].lstrip())
                continue
            if stripped == "":
                # Blank line — end-of-header heuristic: stop on first
                # non-comment, non-blank line.
                if out:
                    out.append("")
                continue
            break
        return "\n".join(out)

    # Python: extract the first triple-quoted docstring at file scope.
    match = re.search(
        r'^(?:#![^\n]*\n)?(?:from __future__[^\n]*\n)?\s*("""|\'\'\')(.*?)\1',
        content,
        flags=re.DOTALL,
    )
    if match:
        return match.group(2)
    # Fall back to the leading ``#``-comment block.
    out: list[str] = []
    for line in content.split("\n"):
        if line.startswith("#!"):
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out.append(stripped[1:].lstrip())
            continue
        if stripped == "":
            if out:
                out.append("")
            continue
        break
    return "\n".join(out)


def classify(guard: Guard, *, _seen: set[str] | None = None) -> str:
    """Return the verdict string for ``guard``.

    ``_seen`` is an internal cycle-prevention set used during wrapper-
    helper recursion. Callers should not pass it.
    """

    if _seen is None:
        _seen = set()
    if guard.basename in _seen:
        # Cycle — fall back to plain wrapper verdict.
        return VERDICT_WRAPPER
    _seen.add(guard.basename)

    content = guard.path.read_text(encoding="utf-8", errors="replace")
    language = "shell" if guard.is_shell else "python"
    body = _strip_comments(content, language=language)

    # 1. Decision flow — script emits a decision-shaped verdict token
    #    (TRUSTED:, DONE:, RESUME:, duplicate:, shipped:, etc.).
    if _has_decision_flow_exit_codes(body):
        return VERDICT_DECISION_FLOW

    # 2. Self-diagnosing (Fix block) — labelled marker in emitted text.
    #    Fix block beats operational probe because a probe that also
    #    emits a Recovery / Fix recipe gives the operator the literal
    #    next step; the doc treats those as Fix-block-shaped (e.g.
    #    check-ingestion-worker-task-def-fingerprint.sh's Recovery:
    #    block).
    if _has_fix_block(body):
        return VERDICT_FIX_BLOCK

    # 3. Operational health probe — live-infra calls.
    if _is_operational_probe(content=body, language=language):
        return VERDICT_OPERATIONAL

    # 4. Wrapper — exec python3 <helper>.py without own Fix block.
    #    The wrapper's verdict is always "wrapper (delegates to helper)"
    #    — the Notes column explains what the helper does. Inheriting
    #    the helper's verdict here would be tempting (the operator sees
    #    the helper's output on CI failure), but the doc convention is
    #    to flag wrappers explicitly so reviewers can immediately see
    #    where the actual Fix-block lives. Wrappers that emit their own
    #    Fix block are caught by step 2 above and never reach this branch.
    if guard.is_shell and _is_wrapper(body):
        return VERDICT_WRAPPER

    # 5. Self-diagnosing (actionable text) — inline guidance.
    if _has_actionable_text(body):
        return VERDICT_ACTIONABLE

    # 6. Fall back: when this is an underscore-named .py with a
    #    hyphen-named .sh sibling, defer to the sibling's verdict.
    #    Standalone helper rows in the inventory (#78a
    #    `check_no_basicconfig_with_extra.py`, etc.) emit only file:line
    #    text — the wrapper provides the Fix block. The doc credits the
    #    helper with the wrapper's verdict because that's what the
    #    operator sees on a CI failure.
    if guard.is_python and "_" in guard.basename:
        hyphen_name = guard.basename.replace("_", "-")
        if hyphen_name.endswith(".py"):
            sh_sibling = guard.path.parent / (hyphen_name[: -len(".py")] + ".sh")
            if sh_sibling.is_file() and sh_sibling.name not in _seen:
                sh_guard = Guard(path=sh_sibling, basename=sh_sibling.name)
                return classify(sh_guard, _seen=_seen)

    # 7. NEEDS UPGRADE — no signals.
    return VERDICT_NEEDS_UPGRADE


# ─── Inventory parsing ──────────────────────────────────────────────────

# Row format inside the survey table:
#   | 23a | `scripts/check-foo.sh` | self-diagnosing (Fix block) | <notes> |
_ROW_RE = re.compile(
    r"^\|\s*(?P<rn>[0-9]+[a-z]?)\s*\|\s*`scripts/(?P<name>check[-_a-zA-Z0-9_]+\.(?:sh|py))`"
    r"\s*\|\s*(?P<verdict>[^|]+?)\s*\|\s*(?P<notes>.*?)\s*\|"
)


def parse_inventory(doc_path: Path) -> list[InventoryRow]:
    """Parse the survey table out of the inventory doc."""

    rows: list[InventoryRow] = []
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        m = _ROW_RE.match(line)
        if m is None:
            continue
        rows.append(
            InventoryRow(
                rownum=m.group("rn"),
                basename=m.group("name"),
                verdict=m.group("verdict").strip(),
                notes=m.group("notes").strip(),
            )
        )
    return rows


# ─── Regenerate / check / print modes ───────────────────────────────────


def _short_note(verdict: str, basename: str) -> str:
    """A minimal one-line note for the regenerated table."""

    if verdict == VERDICT_WRAPPER:
        # Suggest the helper name from the convention.
        if basename.endswith(".sh"):
            stem = basename[: -len(".sh")]
            # Both hyphen-named (`check-foo.py`) and underscore-named
            # (`check_foo.py`) helpers exist. Default to hyphen-named —
            # operators can hand-edit if needed.
            return f"Wrapper for `{stem}.py` (auto-classified)."
        return "Wrapper (auto-classified)."
    if verdict == VERDICT_DECISION_FLOW:
        return "Returns a verdict for callers (auto-classified)."
    if verdict == VERDICT_OPERATIONAL:
        return "Probes live infrastructure (auto-classified)."
    if verdict == VERDICT_FIX_BLOCK:
        return "Emits a labelled `Fix:` block (auto-classified)."
    if verdict == VERDICT_ACTIONABLE:
        return "Emits actionable text (auto-classified)."
    return "No actionable Fix text detected — consider upgrade (auto-classified)."


def regenerate_table(guards: list[Guard]) -> str:
    """Return the regenerated survey table (header + rows + summary)."""

    classified = [(g, classify(g)) for g in sorted(guards, key=lambda g: g.basename)]

    lines: list[str] = []
    lines.append("| # | Guard | Verdict | Notes |")
    lines.append("|---|-------|---------|-------|")
    for idx, (g, verdict) in enumerate(classified, start=1):
        note = _short_note(verdict, g.basename)
        lines.append(f"| {idx} | `scripts/{g.basename}` | {verdict} | {note} |")

    # Summary section.
    counts: dict[str, int] = {v: 0 for v in ALL_VERDICTS}
    for _g, v in classified:
        counts[v] = counts.get(v, 0) + 1
    lines.append("")
    lines.append(f"- Total guards: {len(classified)}")
    for v in ALL_VERDICTS:
        lines.append(f"- {v}: {counts.get(v, 0)}")
    return "\n".join(lines) + "\n"


def check_classification(
    guards: list[Guard], inventory: list[InventoryRow]
) -> tuple[int, list[str]]:
    """Compare classifier output against the inventory.

    Returns ``(exit_code, messages)``. Exit code 0 = no drift, 1 = drift,
    2 = error.
    """

    by_basename = {row.basename: row for row in inventory}
    messages: list[str] = []
    drift = 0

    for g in sorted(guards, key=lambda x: x.basename):
        if g.basename not in by_basename:
            messages.append(
                f"  - missing row: scripts/{g.basename} is not in the inventory"
            )
            drift += 1
            continue
        actual = classify(g)
        expected = by_basename[g.basename].verdict
        if actual != expected:
            messages.append(
                f"  - verdict drift: scripts/{g.basename}: doc says "
                f"{expected!r}, classifier says {actual!r}"
            )
            drift += 1

    if drift:
        return 1, messages
    return 0, messages


# ─── CLI ────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Survey + classify scripts/check-* hygiene guards."
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--check",
        action="store_true",
        help=(
            "CI mode. Verify the inventory's verdict for every guard "
            "matches the classifier output. Exit 0 on match, 1 on drift."
        ),
    )
    mode_group.add_argument(
        "--regenerate",
        action="store_true",
        help=(
            "Print a fully regenerated survey table to stdout. Manual "
            "Notes are intentionally lost."
        ),
    )
    mode_group.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print '<basename>\\t<verdict>' for every guard, one per line.",
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=None,
        help="Override the scripts/ directory (default: <repo>/scripts).",
    )
    parser.add_argument(
        "--doc",
        type=Path,
        default=None,
        help=(
            "Override the inventory doc path "
            "(default: <repo>/docs/dx/check-script-fix-block-coverage.md)."
        ),
    )
    return parser


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """Resolve scripts_dir + doc paths with CLI override support."""

    repo_root = Path(__file__).resolve().parent.parent
    scripts_dir = args.scripts_dir or (repo_root / "scripts")
    doc = args.doc or (repo_root / "docs/dx/check-script-fix-block-coverage.md")
    return scripts_dir, doc


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    scripts_dir, doc = _resolve_paths(args)
    self_basename = Path(__file__).name

    if not scripts_dir.is_dir():
        print(f"ERROR: scripts/ dir not found: {scripts_dir}", file=sys.stderr)
        return 2

    guards = discover_guards(scripts_dir, self_basename=self_basename)
    if not guards:
        print(
            f"ERROR: no scripts/check-*.{{sh,py}} files found under {scripts_dir}",
            file=sys.stderr,
        )
        return 2

    if args.print_only:
        for g in sorted(guards, key=lambda x: x.basename):
            print(f"{g.basename}\t{classify(g)}")
        return 0

    if args.regenerate:
        sys.stdout.write(regenerate_table(guards))
        return 0

    # --check mode.
    if not doc.is_file():
        print(f"ERROR: inventory doc not found: {doc}", file=sys.stderr)
        return 2

    inventory = parse_inventory(doc)
    if not inventory:
        print(f"ERROR: no inventory rows parsed from {doc}", file=sys.stderr)
        return 2

    exit_code, messages = check_classification(guards, inventory)
    if exit_code == 0:
        print(
            f"OK: classifier verdicts match every row in {doc.relative_to(Path.cwd()) if doc.is_relative_to(Path.cwd()) else doc}",
            file=sys.stderr,
        )
        return 0

    print(
        "FAIL: classifier verdict(s) drift from inventory:",
        file=sys.stderr,
    )
    for msg in messages:
        print(msg, file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Fix: update docs/dx/check-script-fix-block-coverage.md so each row's",
        file=sys.stderr,
    )
    print(
        "Verdict column matches the classifier output, OR adjust the guard so",
        file=sys.stderr,
    )
    print(
        "it emits the markers expected for its intended verdict. To see the",
        file=sys.stderr,
    )
    print("classifier's output for every guard, run:", file=sys.stderr)
    print("", file=sys.stderr)
    print("  python3 scripts/check-fix-block-coverage.py --print", file=sys.stderr)
    print("", file=sys.stderr)
    print("Verdict markers:", file=sys.stderr)
    print(
        "  - self-diagnosing (Fix block): emit `Fix:` / `Fix options:` / "
        "`Remediation:` / `Required shape:` / `Recovery:` (line-anchored)",
        file=sys.stderr,
    )
    print(
        "  - self-diagnosing (actionable text): emit `Fix by` / `Replace with` "
        "/ `Use:` / `Add ` / `rename`",
        file=sys.stderr,
    )
    print(
        '  - wrapper (delegates to helper): `exec python3 <helper>.py "$@"` '
        "with no own Fix block",
        file=sys.stderr,
    )
    print(
        "  - operational health probe: `aws ecs/ssm/secretsmanager`, `curl http`, "
        "`docker run/build`, or psycopg/boto3 imports",
        file=sys.stderr,
    )
    print(
        "  - decision flow (no violation list): header `Exit codes:` block with "
        "decision-shaped labels (DONE/RESUME/UNKNOWN, duplicate/not, etc.)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
