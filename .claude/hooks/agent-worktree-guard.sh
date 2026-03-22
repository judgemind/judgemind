#!/usr/bin/env bash
# PreToolUse hook for Agent tool: blocks worktree-isolated agent spawns when
# cwd has drifted into an existing worktree.
#
# When the dispatcher's cwd is inside .claude/worktrees/ and it tries to spawn
# a new agent with isolation: "worktree", the new worktree would be created
# inside the existing one (nested worktrees), causing path confusion.
#
# This hook receives the tool input as JSON on stdin. It checks:
#   1. Whether the current working directory is inside .claude/worktrees/
#   2. Whether the Agent tool input contains isolation: "worktree"
#
# If BOTH conditions are true, it blocks the spawn (exit 2) with a helpful
# error message. Otherwise, it allows the call (exit 0).
#
# Exit 0 = allow, exit 2 = block with message on stderr.

set -uo pipefail

# Read the JSON input from stdin
INPUT=$(cat)

# Allow tests to override the working directory
EFFECTIVE_CWD="${AGENT_GUARD_CWD:-$PWD}"

# Check condition 1: Is cwd inside a worktree?
case "$EFFECTIVE_CWD" in
    */.claude/worktrees/*)
        # cwd is inside a worktree — check condition 2
        ;;
    *)
        # cwd is NOT inside a worktree — allow unconditionally
        exit 0
        ;;
esac

# Check condition 2: Does the Agent call use isolation: "worktree"?
# Extract the isolation field from tool_input using python3.
ISOLATION=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('isolation',''))" 2>/dev/null)

if [ "$ISOLATION" = "worktree" ]; then
    # Both conditions met — block the spawn
    REPO_ROOT="${EFFECTIVE_CWD%%/.claude/worktrees/*}"
    echo "BLOCKED: cwd is inside a worktree. Run cd ${REPO_ROOT} before spawning agents with isolation: worktree." >&2
    exit 2
fi

# isolation is not "worktree" — allow
exit 0
