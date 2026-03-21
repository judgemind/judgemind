#!/usr/bin/env bash
# PostToolUse hook: check the subagent's worktree inbox for messages from the
# dispatcher.  If messages are present, echo them to stdout (so they appear
# in the agent's context) and truncate the file.
#
# This hook is designed to be extremely fast when no messages are pending:
# it checks file existence and size before doing any JSON parsing.
#
# Exit 0 always — this hook never blocks tool use.

set -uo pipefail

# Determine the worktree path.  The hook runs from the repo root (or a
# worktree root).  We resolve the inbox relative to the git toplevel of
# the current working directory.
TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null || echo "")

if [ -z "$TOPLEVEL" ]; then
    # Not inside a git repo — nothing to check
    exit 0
fi

# Refresh the agent lock file if it exists, keeping it fresh while the agent
# is actively running.  This runs after every tool call (~every few seconds),
# so the lock never becomes stale while the agent is alive.
AGENT_LOCK="${TOPLEVEL}/.agent-lock"
[ -f "$AGENT_LOCK" ] && touch "$AGENT_LOCK"

INBOX="${TOPLEVEL}/tmp/inbox.json"

# Fast path: no inbox file or empty file — nothing to do
if [ ! -f "$INBOX" ]; then
    exit 0
fi

# Check file size — skip if empty (0 bytes)
FILE_SIZE=$(wc -c < "$INBOX" 2>/dev/null || echo "0")
FILE_SIZE=$(echo "$FILE_SIZE" | tr -d '[:space:]')

if [ "$FILE_SIZE" = "0" ]; then
    exit 0
fi

# Delegate to the Python helper for JSON parsing and truncation.
# The helper is co-located with this hook in .claude/hooks/.
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "${HOOK_DIR}/check-inbox-reader.py" "$INBOX" 2>/dev/null

exit 0
