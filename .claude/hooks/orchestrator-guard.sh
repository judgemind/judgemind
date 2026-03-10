#!/usr/bin/env bash
# PreToolUse hook for Edit and Write tools: soft-blocks file edits on the main
# branch to remind orchestrators to delegate via /task instead.
#
# This hook receives the tool input as JSON on stdin. It extracts the file_path
# and checks whether the current branch is main/master. If so, it exits non-zero
# to trigger a user confirmation prompt — not a hard block, but friction that
# prevents accidental direct edits from orchestrator sessions.
#
# Branch resolution: the branch is resolved from the *file's* directory, not
# the agent's CWD. This prevents false positives when the CWD is on main but
# the target file lives in a worktree on a feature branch.
#
# Exceptions (always allowed, even on main):
#   - Files under tmp/          (orchestrator temp files)
#   - Files under ~/.claude/    (memory updates)
#   - Files under .claude/      (settings/hook config)
#
# Exit 0 = allow silently, exit 2 = block with message on stderr.

set -uo pipefail

# Read the JSON input from stdin
INPUT=$(cat)

# Extract the file path first — we need it to resolve the correct git branch.
# The file may live in a worktree whose branch differs from the CWD's branch.
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null)

if [ -z "$FILE_PATH" ]; then
    # Can't parse file path — let it through to avoid blocking on hook errors
    exit 0
fi

# Resolve the git branch from the file's directory, not the CWD.
# This prevents false positives when the agent's CWD is on main but the
# target file lives in a worktree on a feature branch.
FILE_DIR=$(dirname "$FILE_PATH")
if [ -d "$FILE_DIR" ]; then
    BRANCH=$(git -C "$FILE_DIR" symbolic-ref --short HEAD 2>/dev/null || echo "")
else
    # Directory doesn't exist yet (new file) — fall back to CWD
    BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")
fi

# If not on main or master, allow everything
if [[ "$BRANCH" != "main" && "$BRANCH" != "master" ]]; then
    exit 0
fi

# On main — check the file path for exceptions

# Exception: tmp/ directories (anywhere in path)
if echo "$FILE_PATH" | grep -qE '(^|/)tmp/' ; then
    exit 0
fi

# Exception: .claude/ directory (relative or absolute)
if echo "$FILE_PATH" | grep -qE '(^|/)\.claude/' ; then
    exit 0
fi

# Exception: ~/.claude/ (user home config)
HOME_CLAUDE="$HOME/.claude/"
if [[ "$FILE_PATH" == "$HOME_CLAUDE"* ]]; then
    exit 0
fi

# On main and editing a production file — soft-block
echo "You're on main. Orchestrators don't edit production files directly." >&2
echo "File an issue and use /task to spawn a subagent instead." >&2
exit 2
