#!/usr/bin/env bash
# PreToolUse hook for Edit/Write tools: blocks writes to the main repo checkout
# when the agent is running inside a worktree.
#
# When a /task subagent is spawned with isolation: "worktree", its CWD is the
# worktree path (.claude/worktrees/agent-<id>/). The Edit/Write tools accept
# absolute paths, so they will happily modify a file in the main repo checkout
# at /path/to/repo/<file> instead of the intended worktree path
# /path/to/repo/.claude/worktrees/agent-<id>/<file>.
#
# This causes silent writes to main: the agent thinks it modified a file in its
# worktree, but actually modified the main repo's working tree, and the change
# never reaches the PR. See #2440 for the motivating incident.
#
# This hook receives the tool input as JSON on stdin. It:
#   1. Determines if the agent is inside a worktree (cwd contains
#      .claude/worktrees/<id>).
#   2. Extracts the target file_path from tool_input.
#   3. Normalizes the path to absolute.
#   4. Checks:
#      - If the target is inside the current worktree -> allow.
#      - If the target is outside the repo root entirely (home dir, /tmp,
#        etc.) -> allow.
#      - If the target is inside {repo_root}/tmp/ -> allow (legitimate
#        cross-worktree location, e.g. agent-status/).
#      - Otherwise (inside main repo but NOT inside current worktree and NOT
#        inside {repo_root}/tmp/) -> block with a helpful message.
#
# When not inside a worktree (interactive session from repo root), the hook
# allows everything unconditionally.
#
# Exit 0 = allow, exit 2 = block with message on stderr.

set -uo pipefail

# Read the JSON input from stdin
INPUT=$(cat)

# Allow tests to override the working directory
EFFECTIVE_CWD="${WORKTREE_GUARD_CWD:-$PWD}"

# Check condition 1: Is cwd inside a worktree?
case "$EFFECTIVE_CWD" in
    */.claude/worktrees/*)
        # cwd is inside a worktree — proceed
        ;;
    *)
        # cwd is NOT inside a worktree — allow unconditionally
        exit 0
        ;;
esac

# Derive repo root: strip everything from /.claude/worktrees/ onward.
REPO_ROOT="${EFFECTIVE_CWD%%/.claude/worktrees/*}"

# Derive the current worktree root. The worktree id is the first path segment
# after .claude/worktrees/. A cwd like
#   /repo/.claude/worktrees/agent-abc/packages/api
# should resolve to worktree root
#   /repo/.claude/worktrees/agent-abc
REST="${EFFECTIVE_CWD#"$REPO_ROOT"/.claude/worktrees/}"
WORKTREE_ID="${REST%%/*}"
WORKTREE_ROOT="$REPO_ROOT/.claude/worktrees/$WORKTREE_ID"

# Extract file_path from tool input
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null)

if [ -z "$FILE_PATH" ]; then
    # No file_path — allow (fail-open, don't block on parse errors)
    exit 0
fi

# Normalize to absolute path. If relative, resolve against the worktree root
# (the expected CWD for a worktree subagent).
case "$FILE_PATH" in
    /*)
        ABS_PATH="$FILE_PATH"
        ;;
    *)
        ABS_PATH="$WORKTREE_ROOT/$FILE_PATH"
        ;;
esac

# Collapse any /./, /../, and trailing slashes via python (portable; realpath
# requires the path to exist on some platforms).
ABS_PATH=$(echo "$ABS_PATH" | python3 -c "import os,sys; print(os.path.normpath(sys.stdin.read().strip()))" 2>/dev/null)
if [ -z "$ABS_PATH" ]; then
    exit 0
fi

# Allow: target is inside the current worktree
case "$ABS_PATH" in
    "$WORKTREE_ROOT"|"$WORKTREE_ROOT"/*)
        exit 0
        ;;
esac

# Allow: target is outside the repo root entirely (home dir, /tmp, etc.)
case "$ABS_PATH" in
    "$REPO_ROOT"|"$REPO_ROOT"/*)
        # Inside repo root — continue to finer-grained checks
        ;;
    *)
        exit 0
        ;;
esac

# Allow: target is inside {repo_root}/tmp/ (e.g. tmp/agent-status/).
# This is a legitimate cross-worktree location used by dispatcher status files.
case "$ABS_PATH" in
    "$REPO_ROOT"/tmp|"$REPO_ROOT"/tmp/*)
        exit 0
        ;;
esac

# Target is inside the main repo checkout but NOT inside the current worktree
# and NOT inside {repo_root}/tmp/. Block with a helpful message.
#
# Suggest the equivalent worktree-prefixed path. The relative-to-repo path is
# the tail after REPO_ROOT/.
REL="${ABS_PATH#"$REPO_ROOT"/}"
SUGGESTED="$WORKTREE_ROOT/$REL"

echo "BLOCKED: $ABS_PATH is inside the main repo checkout but outside your worktree ($WORKTREE_ROOT). Silent writes to the main repo bypass the PR workflow. Retry with the worktree-prefixed path: $SUGGESTED" >&2
exit 2
