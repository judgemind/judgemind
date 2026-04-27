#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Spec-drift detector for the /ralph reviewer chain.

Given a worktree path, parses {worktree}/tmp/ralph/diff.txt to extract candidate
symbols/paths/claim references, then greps all tracked .md files in the worktree
for stale references or contradictions.

Outputs a list of finding dicts:
    [{"side_a_file": "...", "side_a_line": N, "side_b_file": "...", "side_b_line": N,
      "note": "..."}]

The spec-drift Claude reviewer subagent calls this function, then refines the
candidate findings and emits a SHIP / REVISE / SKIPPED verdict.

This module is stdlib-only (no third-party imports).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Diff parsing helpers
# ---------------------------------------------------------------------------


def _parse_diff(diff_text: str) -> dict[str, Any]:
    """Parse a unified diff and extract candidate change artifacts.

    Returns a dict with:
      - ``symbols``: added/removed function, class, or method names
      - ``paths``: .sh script paths referenced in the diff
      - ``renamed_paths``: list of {from, to} dicts for renamed .sh references
      - ``md_changes``: notable lines added to .md files (rules, action names, etc.)
      - ``action_names``: action-handler method names inferred from daemon.py diffs
    """
    symbols: list[str] = []
    paths: list[str] = []
    renamed_paths: list[dict[str, str]] = []
    md_changes: list[str] = []
    action_names: list[str] = []

    # Track which file we are currently diffing
    current_file = ""
    removed_sh_refs: dict[str, str] = {}  # sh_path -> file that removed it
    added_sh_refs: dict[str, str] = {}  # sh_path -> file that added it

    for line in diff_text.splitlines():
        # Track current file
        if line.startswith("diff --git "):
            m = re.search(r"diff --git a/(\S+) b/(\S+)", line)
            if m:
                current_file = m.group(2)
            continue

        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue

        if line.startswith("--- a/"):
            continue

        # Added lines (skip +++ header)
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]

            # Python function/class/method definitions
            sym_match = re.match(r"\s*(def|class)\s+(\w+)", content)
            if sym_match:
                symbols.append(sym_match.group(2))

            # daemon.py action handler methods — _handle_action_<name>
            action_match = re.match(r"\s*def\s+_handle_action_(\w+)", content)
            if action_match:
                action_names.append(action_match.group(1))

            # Script paths
            for sh_match in re.finditer(r"scripts/[\w/.-]+\.sh", content):
                sh_path = sh_match.group(0)
                if current_file not in added_sh_refs:
                    added_sh_refs[sh_path] = current_file
                paths.append(sh_path)

            # MD rule lines (added to .md files)
            if current_file.endswith(".md") and content.strip().startswith("-"):
                md_changes.append(content.strip())

        # Removed lines (skip --- header)
        elif line.startswith("-") and not line.startswith("---"):
            content = line[1:]

            # Removed script paths
            for sh_match in re.finditer(r"scripts/[\w/.-]+\.sh", content):
                sh_path = sh_match.group(0)
                removed_sh_refs[sh_path] = current_file

    # Detect renames: path removed from one file, different path added in another
    for old_path, old_file in removed_sh_refs.items():
        for new_path, new_file in added_sh_refs.items():
            if old_path != new_path and old_file == new_file:
                renamed_paths.append({"from": old_path, "to": new_path})

    return {
        "symbols": list(set(symbols)),
        "paths": list(set(paths)),
        "renamed_paths": renamed_paths,
        "md_changes": md_changes,
        "action_names": list(set(action_names)),
    }


# ---------------------------------------------------------------------------
# .md file discovery
# ---------------------------------------------------------------------------


def _find_tracked_md_files(worktree: Path) -> list[Path]:
    """Return all tracked .md files in the worktree.

    Falls back to a filesystem glob if git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "*.md", "--", "*.md"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [worktree / p for p in result.stdout.splitlines() if p.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fallback: filesystem glob (useful for fake worktrees in tests)
    return list(worktree.rglob("*.md"))


def _grep_md_file(md_file: Path, pattern: str) -> list[tuple[int, str]]:
    """Return (line_number, line_text) pairs for lines matching pattern in md_file.

    Returns an empty list if the file cannot be read.
    """
    try:
        text = md_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    results = []
    for i, line in enumerate(text.splitlines(), start=1):
        if re.search(pattern, line):
            results.append((i, line.rstrip()))
    return results


# ---------------------------------------------------------------------------
# Finding builders
# ---------------------------------------------------------------------------


def _make_finding(
    side_a_file: str,
    side_a_line: int,
    side_b_file: str,
    side_b_line: int,
    note: str,
) -> dict[str, Any]:
    return {
        "side_a_file": side_a_file,
        "side_a_line": side_a_line,
        "side_b_file": side_b_file,
        "side_b_line": side_b_line,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Check: action names in daemon.py added but not mentioned in SKILL.md files
# ---------------------------------------------------------------------------


def _check_action_name_drift(
    action_names: list[str],
    diff_text: str,
    md_files: list[Path],
    worktree: Path,
) -> list[dict[str, Any]]:
    """Check that newly added daemon action handlers are mentioned in relevant SKILL.md files."""
    findings: list[dict[str, Any]] = []
    if not action_names:
        return findings

    # Find which file introduced these actions (from the diff)
    source_file = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git ") and "daemon.py" in line:
            m = re.search(r"b/(\S+)", line)
            if m:
                source_file = m.group(1)
                break

    for action_name in action_names:
        # Skip trivial action names that appear in many places
        if action_name in ("end", "start", "stop", "init"):
            continue

        # Search all SKILL.md files for references to this action name
        mentioned_in: list[tuple[Path, int]] = []
        for md_file in md_files:
            if "SKILL.md" not in md_file.name and "SKILL.md" not in str(md_file):
                # Only care about SKILL.md files for action list checks
                # but check all .md for symbol references
                pass
            hits = _grep_md_file(md_file, re.escape(action_name))
            for line_no, _ in hits:
                mentioned_in.append((md_file, line_no))

        if not mentioned_in:
            # Action added in daemon.py but not mentioned anywhere in .md docs
            # Find the SKILL.md most relevant to diagnose-failure
            skill_md_candidates = [
                f
                for f in md_files
                if "SKILL.md" in f.name
                and ("diagnose" in str(f).lower() or "ralph" in str(f).lower())
            ]
            if not skill_md_candidates:
                skill_md_candidates = [f for f in md_files if "SKILL.md" in f.name]

            if skill_md_candidates:
                skill_file = skill_md_candidates[0]
                findings.append(
                    _make_finding(
                        side_a_file=source_file or "scripts/dispatcher/daemon.py",
                        side_a_line=1,
                        side_b_file=str(skill_file.relative_to(worktree)),
                        side_b_line=1,
                        note=(
                            f"Action '{action_name}' added in daemon.py but not "
                            f"referenced in {skill_file.name} action list."
                        ),
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check: renamed script paths leave stale references in other .md files
# ---------------------------------------------------------------------------


def _check_renamed_path_drift(
    renamed_paths: list[dict[str, str]],
    diff_text: str,
    md_files: list[Path],
    worktree: Path,
) -> list[dict[str, Any]]:
    """Check that renamed script paths don't leave stale references in other .md files."""
    findings: list[dict[str, Any]] = []
    if not renamed_paths:
        return findings

    # Find the .md file that performed the rename
    updated_files: set[str] = set()
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            m = re.search(r"b/(\S+)", line)
            if m:
                current_file = m.group(1)
        elif line.startswith("+++ b/"):
            current_file = line[6:].strip()
            if current_file.endswith(".md"):
                updated_files.add(current_file)

    for rename in renamed_paths:
        old_path = rename["from"]
        new_path = rename["to"]

        # Find the file that did the rename
        renaming_file = ""
        for uf in updated_files:
            # The updating file should have added new_path
            if new_path in diff_text:
                renaming_file = uf
                break

        # Now look for any .md file still referencing the old path
        for md_file in md_files:
            rel_path = str(md_file.relative_to(worktree))
            if rel_path in updated_files:
                continue  # This file was updated in the diff

            hits = _grep_md_file(md_file, re.escape(old_path))
            for line_no, _ in hits:
                findings.append(
                    _make_finding(
                        side_a_file=renaming_file or "diff",
                        side_a_line=1,
                        side_b_file=rel_path,
                        side_b_line=line_no,
                        note=(
                            f"Script path renamed from '{old_path}' to '{new_path}' "
                            f"in diff, but '{old_path}' still referenced in {md_file.name}."
                        ),
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check: CLAUDE.md rule changes contradict other .md docs
# ---------------------------------------------------------------------------


def _check_md_rule_contradiction(
    md_changes: list[str],
    diff_text: str,
    md_files: list[Path],
    worktree: Path,
) -> list[dict[str, Any]]:
    """Check whether new CLAUDE.md rule lines mention things that other .md files
    reference differently (potential contradiction).
    """
    findings: list[dict[str, Any]] = []
    if not md_changes:
        return findings

    # Identify the source .md file from the diff
    source_file = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git ") and ".md" in line:
            m = re.search(r"b/(\S+\.md)", line)
            if m:
                source_file = m.group(1)
                break

    for rule_line in md_changes:
        # Extract any script paths or tool names mentioned in the rule
        sh_refs = re.findall(r"scripts/[\w/.-]+\.sh", rule_line)
        # Extract negation keywords
        negation = re.search(r"[Nn]ever\s+use\s+([\w/.-]+)", rule_line)
        negated_tool = negation.group(1) if negation else None

        candidates: list[str] = list(sh_refs)
        if negated_tool:
            candidates.append(negated_tool)

        for candidate in candidates:
            for md_file in md_files:
                rel_path = str(md_file.relative_to(worktree))
                if rel_path == source_file:
                    continue  # Don't compare to itself

                hits = _grep_md_file(md_file, re.escape(candidate))
                if hits:
                    # The other .md file mentions the same tool/script
                    # This is a candidate contradiction if the rule negates it
                    if negated_tool and candidate == negated_tool:
                        line_no, line_text = hits[0]
                        findings.append(
                            _make_finding(
                                side_a_file=source_file or "CLAUDE.md",
                                side_a_line=1,
                                side_b_file=rel_path,
                                side_b_line=line_no,
                                note=(
                                    f"CLAUDE.md rule says 'Never use {negated_tool}' "
                                    f"but {md_file.name} positively references it "
                                    f"(line {line_no}: {line_text[:80]!r})."
                                ),
                            )
                        )

    return findings


# ---------------------------------------------------------------------------
# Main detect function
# ---------------------------------------------------------------------------


def detect(worktree_path: str | Path) -> list[dict[str, Any]]:
    """Detect spec-drift candidates for the given worktree.

    Reads {worktree_path}/tmp/ralph/diff.txt, parses it for candidate symbols
    and references, then greps all tracked .md files for stale or contradicting
    references.

    Args:
        worktree_path: Absolute path to the worktree root.

    Returns:
        List of finding dicts, each with keys:
            ``side_a_file``, ``side_a_line``, ``side_b_file``, ``side_b_line``,
            ``note``.
        Returns an empty list when no spec-drift candidates are found.
    """
    worktree = Path(worktree_path)
    diff_path = worktree / "tmp" / "ralph" / "diff.txt"

    if not diff_path.exists():
        return []

    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    if not diff_text.strip():
        return []

    parsed = _parse_diff(diff_text)
    md_files = _find_tracked_md_files(worktree)

    findings: list[dict[str, Any]] = []

    # Check 1: action name drift (daemon.py actions not mentioned in SKILL.md)
    findings.extend(
        _check_action_name_drift(
            parsed["action_names"],
            diff_text,
            md_files,
            worktree,
        )
    )

    # Check 2: renamed script path drift (old .sh path still referenced in other .md)
    findings.extend(
        _check_renamed_path_drift(
            parsed["renamed_paths"],
            diff_text,
            md_files,
            worktree,
        )
    )

    # Check 3: CLAUDE.md rule contradiction (new rule mentions something contradicted
    # by another .md file)
    findings.extend(
        _check_md_rule_contradiction(
            parsed["md_changes"],
            diff_text,
            md_files,
            worktree,
        )
    )

    return findings


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point: python3 scripts/spec_drift_detect.py {worktree_path}"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: spec_drift_detect.py <worktree_path>", file=sys.stderr)
        sys.exit(1)

    worktree_path = sys.argv[1]
    findings = detect(worktree_path)
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
