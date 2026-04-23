#!/usr/bin/env bash
# PostToolUse hook for Agent tool: detects cwd drift after Agent tool calls.
#
# When the dispatcher's cwd ends up inside .claude/worktrees/ after an Agent
# tool call completes, this hook prints a warning to stdout reminding the
# agent to cd back to the repo root.
#
# This hook is matched to the "Agent" tool via .claude/settings.json. It runs
# after every Agent tool invocation. It must be fast (<100ms) and idempotent.
#
# Exit 0 always — this hook never blocks tool use.

set -uo pipefail

# Check if the current working directory is inside a .claude/worktrees/ path.
# Use $PWD (resolved by the shell) for speed — no subprocess needed.
CWD="${PWD}"

case "$CWD" in
    */.claude/worktrees/*)
        # Derive repo root: strip everything from /.claude/worktrees/ onward
        REPO_ROOT="${CWD%%/.claude/worktrees/*}"
        echo "[cwd-guard] WARNING: cwd drifted to ${CWD}. Run: cd ${REPO_ROOT}"
        ;;
esac

# Always exit 0 — informational only, never block
exit 0
