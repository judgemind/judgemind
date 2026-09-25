#!/usr/bin/env bash
# PreToolUse hook for Bash tool — Fargate dispatcher container edition.
#
# Narrow replacement for .claude/hooks/preflight-bash.sh when the hook is
# running inside the Fargate dispatcher container. Compared to the
# operator-local hook, this one drops all the rules whose purpose was to
# prevent the *interactive* Claude CLI from stalling on permission prompts.
# In the Fargate container, subagent Claude processes run with
# `--dangerously-skip-permissions`, so there are no prompts to stall on —
# those rules become pure drag (issue #2982).
#
# Kept (4 rules, all real safety):
#
#   - `git push` to main/master                     — branch-protection proxy
#   - `git worktree add` inside an existing worktree — orphan-worktree prevention
#   - cross-worktree writes into the main repo       — bypass of PR workflow (#2440)
#   - bare `git stash pop` / `git stash apply`       — cross-worktree stash pollution (#2749)
#
# These four are NOT implemented here. They live in ONE place,
# .claude/hooks/preflight_shared_checks.sh, which this hook and the
# operator-local hook both source (#4703). Fix or widen a shared check
# there and both environments pick it up.
#
# Dropped (all interactive-prompt-prevention):
#
#   - `$(...)` command substitution
#   - `<<EOF` heredocs
#   - `python -c "..."` inline scripts
#   - quoted strings combined with `&&` / `;`
#   - `cd` in compound commands
#   - empty-quotes bypass patterns
#   - long-running commands without sufficient timeout
#   - `run_in_background` inside worktree subagents
#   - terraform apply/destroy from the root infra path (no operator sessions in Fargate)
#
# Those rules still apply on an operator's laptop. The Fargate images
# (Dockerfile.dispatcher, -v3, -agent-runner) stage this file as
# /app/fargate-hooks/preflight-bash.sh, next to preflight_shared_checks.sh
# and preflight_cross_worktree.py. daemon.py / agent_runner_install_fargate_hook.sh
# copy all three over the worktree's .claude/hooks/ copies, leaving the
# operator-local hook untouched everywhere else.
#
# This hook receives the tool input as JSON on stdin. It extracts the "command"
# field and checks it against the 4 safety patterns.
#
# Exit 0 = allow, exit 2 = block with message on stderr.

set -uo pipefail

# Locate the shared checks. Installed over .claude/hooks/preflight-bash.sh,
# the library sits next to this file. Run in place from scripts/ (tests,
# local runs), it is in the repo's .claude/hooks/. Fail closed if neither
# exists: without the library the four safety rules would silently vanish.
HOOK_DIR="$(dirname "$0")"
if [ -f "$HOOK_DIR/preflight_shared_checks.sh" ]; then
    PREFLIGHT_SHARED_LIB="$HOOK_DIR/preflight_shared_checks.sh"
else
    PREFLIGHT_SHARED_LIB="$(dirname "$HOOK_DIR")/.claude/hooks/preflight_shared_checks.sh"
fi
if [ ! -f "$PREFLIGHT_SHARED_LIB" ]; then
    echo "BLOCKED: preflight hook library missing: preflight_shared_checks.sh was not found next to $0 or at $PREFLIGHT_SHARED_LIB. The shared safety checks cannot run, so every Bash command is blocked. Check that the Fargate image stages it in \$DISPATCHER_FARGATE_HOOKS_DIR and the hook swap copied it. See #4703." >&2
    exit 2
fi
# shellcheck source=../.claude/hooks/preflight_shared_checks.sh
source "$PREFLIGHT_SHARED_LIB"

# Read the JSON input from stdin
INPUT=$(cat)

# Extract the command field using python3 (always available on the container).
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)

if [ -z "$COMMAND" ]; then
    # Can't parse — let it through, don't block on hook errors
    exit 0
fi

# Allow tests to override the working directory for the worktree-aware checks.
EFFECTIVE_CWD="${PREFLIGHT_CWD:-$PWD}"

# Produce a copy of $COMMAND with single- and double-quoted substrings removed.
# Used by the stash check so the literal string "git stash pop" inside a PR
# title or commit message does not false-positive. Same approach as the
# operator-local hook.
STRIPPED_COMMAND=$(printf '%s' "$COMMAND" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g")

# The four shared safety checks (0, 10, 11, 12), in canonical order.
preflight_run_shared_checks || exit $?

# All checks passed
exit 0
