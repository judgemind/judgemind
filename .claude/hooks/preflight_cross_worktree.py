#!/usr/bin/env python3
"""Cross-worktree write detection for preflight-bash.sh Check 11.

Reads COMMAND, REPO_ROOT, WORKTREE_ROOT from the environment. Parses the
command with shlex and identifies destinations for write verbs (cp, mv,
tar -C, shell redirection). If any destination is an absolute path inside
REPO_ROOT but outside WORKTREE_ROOT and outside REPO_ROOT/tmp/, prints a
BLOCKED message to stdout and exits 0. Otherwise prints nothing and exits 0.

The shell wrapper treats any stdout as a block signal.

This file lives next to preflight-bash.sh in .claude/hooks/ and is exercised
by .claude/hooks/test_preflight_hook.py under Check 11.
"""

from __future__ import annotations

import os
import shlex
import sys


def _dest_is_inside(dest: str, root: str) -> bool:
    """True if `dest` is `root` or inside `root`."""
    dest = os.path.normpath(dest)
    root = os.path.normpath(root)
    if dest == root:
        return True
    return dest.startswith(root + os.sep)


def _is_blocked(dest: str, repo_root: str, worktree_root: str) -> bool:
    """Is `dest` a destination we should block on?"""
    # Only check absolute paths. Relative paths resolve against CWD (the
    # worktree), so they cannot escape into the main repo via normal use.
    if not dest.startswith("/"):
        return False

    norm = os.path.normpath(dest)

    # Outside repo entirely — allow.
    if not _dest_is_inside(norm, repo_root):
        return False

    # Inside current worktree — allow.
    if _dest_is_inside(norm, worktree_root):
        return False

    # Inside $REPO_ROOT/tmp/ — allow (cross-worktree allowlist for status files).
    repo_tmp = os.path.join(repo_root, "tmp")
    if _dest_is_inside(norm, repo_tmp):
        return False

    # Inside repo, outside worktree, outside tmp — block.
    return True


def _extract_destinations(command: str) -> list[tuple[str, str]]:
    """Return list of (verb, destination) tuples found in the command.

    `verb` is a short human-readable label ("cp", "mv", "tar -C",
    "redirection >"). `destination` is the path as it appears in the
    command (not yet normalized or resolved).

    Parsing strategy:
      1. Split the command with shlex into tokens.
      2. Scan for verbs. Each write-verb handler extracts the destination
         path(s) it would target.
      3. Redirection operators (`>`, `>>`) are detected as standalone tokens
         (shlex preserves them as-is because they are not quoted).

    Conservative by design — if parsing fails (shlex raises ValueError on
    unbalanced quotes), we return an empty list and the hook does not block.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unparseable command (e.g. unbalanced quotes) — fail open.
        return []

    results: list[tuple[str, str]] = []

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]

        # Redirection operators: > <path>, >> <path>
        #   shlex preserves these as standalone tokens.
        if tok in (">", ">>"):
            if i + 1 < n:
                results.append((f"redirection {tok}", tokens[i + 1]))
                i += 2
                continue
            i += 1
            continue

        # Token with a trailing redirection attached (rare but possible):
        #   echo foo>/path — shlex splits differently in posix mode;
        #   shlex would give "foo>/path" as one token. We handle leading ">"
        #   prefixes on a token too.
        if tok.startswith(">>") and len(tok) > 2:
            results.append(("redirection >>", tok[2:]))
            i += 1
            continue
        if tok.startswith(">") and not tok.startswith(">>") and len(tok) > 1:
            results.append(("redirection >", tok[1:]))
            i += 1
            continue

        # cp / mv — the destination is the last non-flag positional argument.
        #   To find the "command group" we walk forward until we hit a
        #   shell separator (|, &&, ;, ||) or end of command.
        if tok == "cp" or tok == "mv":
            verb = tok
            j = i + 1
            positional: list[str] = []
            while j < n:
                t = tokens[j]
                # Stop at shell separators.
                if t in ("|", "||", "&&", ";", "&"):
                    break
                # Skip flags. `cp -r`, `cp --preserve=mode`, etc.
                if t.startswith("-"):
                    j += 1
                    continue
                positional.append(t)
                j += 1
            if len(positional) >= 2:
                # Destination is the last positional arg.
                results.append((verb, positional[-1]))
            i = j
            continue

        # tar — look for -C <dir> or --directory=<dir> anywhere in the
        #   command-group.
        if tok == "tar":
            j = i + 1
            while j < n:
                t = tokens[j]
                if t in ("|", "||", "&&", ";", "&"):
                    break
                if t == "-C" and j + 1 < n:
                    results.append(("tar -C", tokens[j + 1]))
                    j += 2
                    continue
                if t.startswith("--directory="):
                    results.append(("tar --directory", t[len("--directory=") :]))
                    j += 1
                    continue
                j += 1
            i = j
            continue

        i += 1

    return results


def main() -> int:
    command = os.environ.get("COMMAND", "")
    repo_root = os.environ.get("REPO_ROOT", "")
    worktree_root = os.environ.get("WORKTREE_ROOT", "")

    if not command or not repo_root or not worktree_root:
        return 0

    destinations = _extract_destinations(command)
    if not destinations:
        return 0

    for verb, dest in destinations:
        if _is_blocked(dest, repo_root, worktree_root):
            normalized = os.path.normpath(dest)
            rel = normalized[len(repo_root) + 1 :] if normalized.startswith(
                repo_root + "/"
            ) else normalized
            suggested = os.path.join(worktree_root, rel)
            print(
                f"BLOCKED: Bash command writes to {normalized} via '{verb}', "
                f"which is inside the main repo checkout but outside your "
                f"worktree ({worktree_root}). Silent writes to the main repo "
                f"bypass the PR workflow. Write into your worktree instead: "
                f"{suggested}. Legitimate cross-worktree writes must target "
                f"{repo_root}/tmp/ (e.g. tmp/agent-status/). See CLAUDE.md "
                f"§Critical Rules and issue #2455."
            )
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
