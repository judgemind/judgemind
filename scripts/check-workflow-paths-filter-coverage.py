#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-workflow-paths-filter-coverage.py — Verify that every shell script
invoked from the runner inside a GitHub Actions workflow (or a composite
action it references) is present in that workflow's `paths:` filter, so
changes to the shared script trigger the workflow on the PR that
introduces them.

Motivation (#4084)
------------------
Issue #4073 / PR #4077 fixed two instances of the same bug shape: a
shell script invoked from a deploy workflow was missing from the
workflow's `on.push.paths:` filter. A regression in the script only
surfaces on the next *unrelated* workflow run — blinding the gate in
the meantime. The convention has been applied four times so far
(#2592 wait-for-rollout, #2608 ecs-post-deploy-healthcheck, #4073 the
two scripts in PR #4077). This check enforces it structurally.

What the check does
-------------------
1. Walk every workflow file under ``.github/workflows/*.yml`` (and
   ``*.yaml``).
2. For each workflow with at least one ``paths:`` filter (under
   ``on.push`` or ``on.pull_request``), extract the positive and
   negative path entries.
3. Find every ``run:`` block in the workflow body. For each composite
   action referenced via ``uses: ./.github/actions/<name>``, also
   scan that action's ``run:`` blocks.
4. In each ``run:`` block, find every reference matching
   ``scripts/<path>.(sh|py)`` — those are runner-side script
   invocations. Strings inside ``with:`` blocks are NOT scanned (a
   ``command:`` input to ``ecs-oneshot`` runs *inside* the launched
   container, not on the runner — so it's intentionally excluded).
5. For each found script, assert it matches at least one positive
   glob in EVERY ``paths:`` filter present in the workflow, and is
   NOT excluded by a negative entry.
6. Report violations and exit 1; exit 0 if all clean.

Glob syntax: GitHub Actions uses picomatch globs. Reuses the same
glob_to_regex() convention as ``check-ci-job-skipped.py``.

Usage
-----
    scripts/check-workflow-paths-filter-coverage.py
    scripts/check-workflow-paths-filter-coverage.py --repo-root /path
    scripts/check-workflow-paths-filter-coverage.py --workflows-dir DIR
                                                    --actions-dir DIR

Exit codes
----------
    0 — All clean: every runner-side script invocation is covered
        by the workflow's ``paths:`` filter.
    1 — One or more workflows have a runner-side script invocation
        missing from a ``paths:`` filter.
    2 — Script error (cannot parse a workflow file, missing dir,
        etc.).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# -----------------------------------------------------------------------------
# Glob matching (GitHub Actions / dorny/paths-filter / picomatch syntax)
# -----------------------------------------------------------------------------
# GitHub Actions paths filter uses picomatch globs:
#   - ``**`` matches any sequence (including zero or more directory
#     boundaries).
#   - ``*`` matches any character except ``/``.
#   - ``?`` matches a single character except ``/``.
#   - Other chars are literal.
#
# This is the same convention used by ``check-ci-job-skipped.py``; the
# glob_to_regex implementation is intentionally a near-copy so the
# behavior stays consistent.


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Convert a picomatch-ish glob to a compiled regex anchored to full path."""
    glob = glob.strip().strip("'\"")

    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            # Look for `**` (optionally followed by `/`)
            if i + 1 < n and glob[i + 1] == "*":
                # `**/` matches any sequence of dirs including none
                if i + 2 < n and glob[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                # `/**` at end or elsewhere: match anything (incl empty).
                out.append(".*")
                i += 2
                continue
            # Single `*`: match anything except slash.
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in r".+^$()[]{}|\\":
            out.append(re.escape(c))
            i += 1
        else:
            out.append(re.escape(c))
            i += 1

    pattern = "^" + "".join(out) + "$"
    return re.compile(pattern)


def path_matches_any(path: str, compiled_globs: list[re.Pattern[str]]) -> bool:
    for pat in compiled_globs:
        if pat.match(path):
            return True
    return False


# -----------------------------------------------------------------------------
# Workflow / action YAML parsing (line-oriented; no PyYAML dep needed)
# -----------------------------------------------------------------------------
#
# We only need three kinds of structure out of these files:
#
#   1. The ``on.push.paths`` and ``on.pull_request.paths`` lists. Each
#      entry is a YAML list element ``- '...'`` or ``- "..."`` or
#      ``- ...``; a leading ``!`` marks a negative (exclusion) entry.
#
#   2. The text of every ``run:`` block — both single-line ``run: foo``
#      and multi-line ``run: |``-style folded scalars. We need the
#      content to grep for ``scripts/...`` references.
#
#   3. The list of composite actions referenced via
#      ``uses: ./.github/actions/<name>`` (and the ``with:`` block
#      following each, which we DO NOT include in the runner-side
#      run-block content because ``with:`` values are inputs to the
#      action, not commands executed on the runner).
#
# The parser is a small line-level state machine. It does not aim to
# handle every YAML edge case — only the structures actually used in
# this repo's workflows. ``check-ci-job-skipped.py`` follows the same
# pattern for ``ci.yml``, and the constraint has held up well.


# A `run: |` block-scalar header (multi-line). Indent of the colon-bearing
# line determines where the block content begins (one level deeper).
# Both forms are accepted:
#   `<indent>run: |`                            (mapping form)
#   `<indent>- run: |`                          (list-item form)
RUN_BLOCK_RE = re.compile(r"^(\s*(?:-\s+)?)run:\s*\|[\s+\-]*\s*$")
# A `run: <single-line-command>` — captures the command portion. Same
# list-item-or-mapping leading allowance.
RUN_INLINE_RE = re.compile(r"^(\s*(?:-\s+)?)run:\s+([^|>].*)$")
# A `uses: ./.github/actions/<name>` reference — captures the action path.
USES_LOCAL_ACTION_RE = re.compile(
    r"^\s*(?:-\s+)?uses:\s+(\./\.github/actions/[A-Za-z0-9_\-./]+)\s*$"
)
# An entry inside a paths-filter list: `- '<glob>'` or `- "<glob>"` or `- <glob>`.
PATH_ENTRY_RE = re.compile(r"^(\s*)-\s+(.+?)\s*$")


@dataclass
class WorkflowPaths:
    """Paths-filter content under one of ``on.push.paths`` or ``on.pull_request.paths``."""

    trigger: str  # "push" or "pull_request"
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)

    def is_covered(self, script_path: str) -> bool:
        """Return True if ``script_path`` is covered by this filter.

        Coverage = matches at least one positive glob AND does not
        match any negative glob.
        """
        pos = [glob_to_regex(g) for g in self.positives]
        neg = [glob_to_regex(g) for g in self.negatives]
        if not path_matches_any(script_path, pos):
            return False
        if path_matches_any(script_path, neg):
            return False
        return True


@dataclass
class RunBlock:
    """A runner-side ``run:`` block — text content + source location."""

    file_path: Path  # the workflow or action file the block lives in
    line_number: int  # 1-indexed line where the `run:` keyword appears
    content: str  # the block's actual command text (joined lines)


@dataclass
class WorkflowFile:
    """Parsed view of a single workflow file."""

    path: Path
    paths_filters: list[WorkflowPaths]
    run_blocks: list[RunBlock]
    composite_actions: list[Path]  # absolute paths to local action.yml files


def parse_yaml_file(
    path: Path,
    repo_root: Path,
    is_workflow: bool,
) -> WorkflowFile:
    """Parse a workflow or action file into a WorkflowFile structure.

    For composite-action files (is_workflow=False) we only collect
    ``run:`` blocks and nested ``uses:`` — paths_filters is empty.
    """
    text = path.read_text()
    lines = text.splitlines()

    paths_filters: list[WorkflowPaths] = []
    run_blocks: list[RunBlock] = []
    composite_actions: list[Path] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # `run: |` — multi-line block scalar
        m_run_block = RUN_BLOCK_RE.match(line)
        if m_run_block:
            run_indent = len(m_run_block.group(1))
            block_lines: list[str] = []
            j = i + 1
            block_indent: int | None = None
            while j < n:
                bl = lines[j]
                # Empty / blank lines belong to the block (they're inside the scalar).
                if not bl.strip():
                    block_lines.append("")
                    j += 1
                    continue
                bl_indent = len(bl) - len(bl.lstrip(" "))
                if bl_indent <= run_indent:
                    break
                if block_indent is None:
                    block_indent = bl_indent
                block_lines.append(bl)
                j += 1
            run_blocks.append(
                RunBlock(
                    file_path=path, line_number=i + 1, content="\n".join(block_lines)
                )
            )
            i = j
            continue

        # `run: <single line command>` — single-line invocation
        m_run_inline = RUN_INLINE_RE.match(line)
        if m_run_inline:
            content = m_run_inline.group(2)
            run_blocks.append(
                RunBlock(file_path=path, line_number=i + 1, content=content)
            )
            i += 1
            continue

        # `uses: ./.github/actions/<name>` — collect for later recursion
        m_uses = USES_LOCAL_ACTION_RE.match(line)
        if m_uses:
            # m_uses.group(1) is like "./.github/actions/myaction".
            # ``Path("./.github/actions/...")`` simplifies the leading
            # `./` automatically, so a plain join with repo_root works.
            ref = m_uses.group(1)
            if ref.startswith("./"):
                ref = ref[2:]
            action_dir = repo_root / ref
            # Try common filenames: action.yml then action.yaml
            for fname in ("action.yml", "action.yaml"):
                candidate = action_dir / fname
                if candidate.is_file():
                    composite_actions.append(candidate.resolve())
                    break
            i += 1
            continue

        # `on:` block — only relevant for workflow files
        if is_workflow and stripped == "on:":
            paths_filters.extend(_parse_on_block(lines, i))
            # _parse_on_block doesn't advance — let outer loop continue.
            # The block content is parsed inline; we just keep walking.
            i += 1
            continue

        i += 1

    return WorkflowFile(
        path=path,
        paths_filters=paths_filters,
        run_blocks=run_blocks,
        composite_actions=composite_actions,
    )


def _parse_on_block(lines: list[str], on_line_idx: int) -> list[WorkflowPaths]:
    """Parse the ``on:`` block starting at ``on_line_idx`` and return
    paths-filter entries for triggers we care about (``push``, ``pull_request``).

    The block ends when we encounter a top-level (zero-indent) line.
    Within the block, each trigger header is at indent 2:
        on:
          push:
            branches: [main]
            paths:
              - 'foo/**'
          pull_request:
            paths:
              - 'foo/**'
    We support both ``paths:`` (global to the trigger) and the
    occasional ``branches:`` interleaving.
    """
    out: list[WorkflowPaths] = []
    n = len(lines)
    # The ``on:`` line itself.
    # Walk subsequent lines as long as they are indented OR blank.
    # Track when we enter a trigger sub-block (`push:` or `pull_request:`).
    current_trigger: str | None = None
    # Indent of the trigger header line (e.g. `  push:` -> 2).
    trigger_indent: int | None = None
    i = on_line_idx + 1
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        leading = len(line) - len(line.lstrip(" "))
        if leading == 0:
            # Left the on: block.
            break

        # Trigger header at indent 2 or deeper-but-shallowest level
        m_trigger = re.match(
            r"^(\s+)(push|pull_request|workflow_dispatch|schedule):\s*$", line
        )
        if m_trigger and (
            trigger_indent is None or len(m_trigger.group(1)) <= trigger_indent
        ):
            current_trigger = m_trigger.group(2)
            trigger_indent = len(m_trigger.group(1))
            i += 1
            continue

        # `paths:` block under the current trigger
        if current_trigger in ("push", "pull_request") and re.match(
            r"^\s+paths:\s*$", line
        ):
            paths_indent = len(line) - len(line.lstrip(" "))
            wp = WorkflowPaths(trigger=current_trigger)
            j = i + 1
            while j < n:
                pl = lines[j]
                if not pl.strip():
                    j += 1
                    continue
                pl_indent = len(pl) - len(pl.lstrip(" "))
                if pl_indent <= paths_indent:
                    break
                m_entry = PATH_ENTRY_RE.match(pl)
                if m_entry:
                    val = m_entry.group(2).strip()
                    # Strip wrapping quotes
                    if (val.startswith("'") and val.endswith("'")) or (
                        val.startswith('"') and val.endswith('"')
                    ):
                        val = val[1:-1]
                    if val.startswith("!"):
                        wp.negatives.append(val[1:])
                    else:
                        wp.positives.append(val)
                j += 1
            out.append(wp)
            i = j
            continue

        i += 1

    return out


# -----------------------------------------------------------------------------
# Script-invocation extraction
# -----------------------------------------------------------------------------
# We look for tokens of the form ``scripts/<path>.(sh|py)`` inside ``run:``
# block content. The token may appear in many forms:
#
#   bash scripts/foo.sh
#   python3 scripts/foo.py
#   "${GITHUB_WORKSPACE}/scripts/foo.sh"
#   ./scripts/foo.sh
#   scripts/foo.sh --some-flag
#
# All of them have the literal substring ``scripts/<path>``. The regex
# below captures the bare path component, with a leading word boundary
# (or path-prefix character) to avoid matching things like
# ``packages/scripts/...`` or ``my-scripts/foo.sh``.
#
# Extensions: ``.sh`` and ``.py`` cover every shared-script case in the
# current workflows. ``.mjs`` (e.g. ``scripts/seed-and-migrate.mjs``)
# always runs *inside* the container — it's the value of a
# ``command:`` input to the ecs-oneshot action — so it's intentionally
# excluded; if a future workflow invokes a ``.mjs`` script directly on
# the runner, we'll add it here.
#
# Distinguishing top-level ``scripts/`` vs nested ``packages/scripts/``:
#
#   `bash scripts/foo.sh`               — match
#   `"${GITHUB_WORKSPACE}/scripts/foo.sh"` — match (via `}/`)
#   `./scripts/foo.sh`                  — match (via `./`)
#   `packages/scripts/foo.sh`           — DO NOT match (alphabetic
#                                          char before the `/`).
#
# Both "good" cases above have the immediately-preceding character of
# ``scripts/`` be ``/``. The "bad" case also has ``/`` immediately
# before. We disambiguate by what's BEFORE that slash: if it's a
# ``}``, ``.``, or non-word char, it's a substitution / dot-slash, NOT
# a path component. If it's a word char (letter/digit/_/-), it's
# nested.
#
# Two-arm regex:
#   1. ``(?<![A-Za-z0-9_\-/])scripts/...`` — covers leads like start
#      of line, whitespace, quotes, ``;`` ``&`` ``(``.
#   2. ``(?<=[}.])/(scripts/...)`` — covers ``}/scripts/`` and
#      ``./scripts/`` cases where a ``/`` is permitted because of
#      the preceding char.
SCRIPT_REF_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9_\-/])(?P<a>scripts/[A-Za-z0-9_\-./]+\.(?:sh|py))"
    r"|"
    r"(?<=[})\.])/(?P<b>scripts/[A-Za-z0-9_\-./]+\.(?:sh|py))"
    r")"
)


def find_script_invocations(content: str) -> list[str]:
    """Return the list of ``scripts/...`` references found in ``content``.

    Filters comment-only lines (lines whose first non-whitespace char
    is ``#``) since those are documentation, not invocations.
    Deduplicates while preserving order of first appearance.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in SCRIPT_REF_RE.finditer(line):
            ref = m.group("a") or m.group("b")
            if ref and ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


# -----------------------------------------------------------------------------
# Main check
# -----------------------------------------------------------------------------


@dataclass
class Violation:
    workflow: Path
    trigger: str  # "push" or "pull_request"
    script_path: str
    invocation_file: Path  # workflow itself, OR a composite action it uses
    invocation_line: int


def check_workflow(
    wf: WorkflowFile,
    repo_root: Path,
    workflow_files: dict[Path, WorkflowFile],
    action_files: dict[Path, WorkflowFile],
) -> list[Violation]:
    """Return list of violations for this workflow."""
    if not wf.paths_filters:
        return []  # workflow_dispatch-only / no path gating

    # Collect all run-block references in the workflow itself.
    invocations: list[tuple[str, Path, int]] = []
    for rb in wf.run_blocks:
        for ref in find_script_invocations(rb.content):
            invocations.append((ref, rb.file_path, rb.line_number))

    # Recursively collect from composite actions (one level — actions
    # don't typically reference other local actions in this repo).
    seen_actions: set[Path] = set()
    pending = list(wf.composite_actions)
    while pending:
        action_path = pending.pop()
        if action_path in seen_actions:
            continue
        seen_actions.add(action_path)
        action = action_files.get(action_path)
        if action is None:
            continue
        for rb in action.run_blocks:
            for ref in find_script_invocations(rb.content):
                invocations.append((ref, rb.file_path, rb.line_number))
        # Composite actions can `uses:` other composite actions.
        for sub in action.composite_actions:
            if sub not in seen_actions:
                pending.append(sub)

    # Validate each invocation against EVERY paths filter on the workflow.
    violations: list[Violation] = []
    for script_path, inv_file, inv_line in invocations:
        for pf in wf.paths_filters:
            if not pf.is_covered(script_path):
                violations.append(
                    Violation(
                        workflow=wf.path,
                        trigger=pf.trigger,
                        script_path=script_path,
                        invocation_file=inv_file,
                        invocation_line=inv_line,
                    )
                )
    return violations


def gather_files(
    workflows_dir: Path,
    actions_dir: Path,
    repo_root: Path,
) -> tuple[dict[Path, WorkflowFile], dict[Path, WorkflowFile]]:
    workflows: dict[Path, WorkflowFile] = {}
    if workflows_dir.is_dir():
        for f in sorted(workflows_dir.iterdir()):
            if f.is_file() and f.suffix in (".yml", ".yaml"):
                resolved = f.resolve()
                workflows[resolved] = parse_yaml_file(
                    resolved, repo_root, is_workflow=True
                )

    actions: dict[Path, WorkflowFile] = {}
    if actions_dir.is_dir():
        for action_dir in sorted(actions_dir.iterdir()):
            if not action_dir.is_dir():
                continue
            for fname in ("action.yml", "action.yaml"):
                candidate = action_dir / fname
                if candidate.is_file():
                    resolved = candidate.resolve()
                    actions[resolved] = parse_yaml_file(
                        resolved, repo_root, is_workflow=False
                    )
                    break

    return workflows, actions


def format_violation(v: Violation, repo_root: Path) -> str:
    workflow_rel = _rel(v.workflow, repo_root)
    invocation_rel = _rel(v.invocation_file, repo_root)
    return (
        f"  {workflow_rel} (on.{v.trigger}.paths):\n"
        f"    invokes  {v.script_path}\n"
        f"    via      {invocation_rel}:{v.invocation_line}\n"
        f"    but      {v.script_path} is NOT in the {v.trigger}-paths filter\n"
    )


def _rel(p: Path, repo_root: Path) -> str:
    try:
        return str(p.relative_to(repo_root))
    except ValueError:
        return str(p)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Verify that every shell script invoked from a runner-side "
            "run: block in a GitHub Actions workflow (or composite action "
            "it references) is present in the workflow's paths: filter."
        )
    )
    ap.add_argument(
        "--repo-root",
        default=None,
        help="Repo root (default: auto-detect from this script's location).",
    )
    ap.add_argument(
        "--workflows-dir",
        default=None,
        help="Override workflows dir (default: <repo-root>/.github/workflows).",
    )
    ap.add_argument(
        "--actions-dir",
        default=None,
        help="Override composite-actions dir (default: <repo-root>/.github/actions).",
    )
    args = ap.parse_args()

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        # scripts/check-workflow-paths-filter-coverage.py -> two parents up.
        repo_root = Path(__file__).resolve().parent.parent

    workflows_dir = (
        Path(args.workflows_dir)
        if args.workflows_dir
        else repo_root / ".github" / "workflows"
    )
    actions_dir = (
        Path(args.actions_dir)
        if args.actions_dir
        else repo_root / ".github" / "actions"
    )

    if not workflows_dir.is_dir():
        print(f"ERROR: workflows dir not found: {workflows_dir}", file=sys.stderr)
        return 2

    try:
        workflows, actions = gather_files(workflows_dir, actions_dir, repo_root)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to parse workflow files: {exc}", file=sys.stderr)
        return 2

    all_violations: list[Violation] = []
    for wf in workflows.values():
        all_violations.extend(check_workflow(wf, repo_root, workflows, actions))

    if not all_violations:
        return 0

    print(
        "check-workflow-paths-filter-coverage: "
        "one or more workflows invoke a shell script that is NOT in their paths: filter.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    # Group by workflow + script for readability.
    seen: set[tuple[str, str, str]] = set()
    for v in all_violations:
        key = (str(v.workflow), v.trigger, v.script_path)
        if key in seen:
            continue
        seen.add(key)
        print(format_violation(v, repo_root), file=sys.stderr)
    print(
        "Fix: add an entry for the script under the workflow's "
        "on.<trigger>.paths block.\n"
        "See https://github.com/judgemind/judgemind/issues/4084 for background "
        "(and #4073, #2592, #2608 for prior instances).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
