#!/usr/bin/env bash
# check-task-recovery.sh — Tell a /task agent whether to resume after autocompact.
#
# Autocompaction produces a conversation summary that preserves *what was done*
# but not the procedural imperative *what still needs to happen next*. After
# compaction, a /task agent may read its own "iteration 1 COMPLETE" artifact
# as a done-signal and emit end_turn prematurely — leaving the PR uncommitted,
# unpushed, and unmerged (see #2545).
#
# This script is the post-compaction recovery anchor. It reads the agent's
# status file (tmp/agent-status/<agent-id>.txt) and prints a structured
# verdict the agent can act on:
#
#   DONE     — final phase reached; nothing more to do
#   RESUME   — work incomplete; continue from the step after <phase>
#   UNKNOWN  — could not read status file; assume RESUME and re-read SKILL.md
#
# Usage:
#   scripts/check-task-recovery.sh <worktree>
#
# Exit codes:
#   0 — DONE (task complete; safe to exit)
#   1 — RESUME (work remains; keep going)
#   2 — UNKNOWN (status file missing or malformed; assume RESUME)

set -uo pipefail

WORKTREE="${1:?Usage: check-task-recovery.sh <worktree>}"

# Derive the repo root and agent id from the worktree path.
# Worktree path looks like: /path/to/repo/.claude/worktrees/agent-<id>
if [[ "$WORKTREE" != *"/.claude/worktrees/"* ]]; then
    echo "UNKNOWN: worktree path does not contain .claude/worktrees/" >&2
    exit 2
fi

REPO_ROOT="${WORKTREE%%/.claude/worktrees/*}"
AGENT_ID=$(basename "$WORKTREE")
STATUS_FILE="$REPO_ROOT/tmp/agent-status/$AGENT_ID.txt"

if [ ! -f "$STATUS_FILE" ]; then
    cat >&2 <<EOF
UNKNOWN: status file not found at $STATUS_FILE
Re-read .claude/skills/task/SKILL.md from the start and determine the next step.
EOF
    exit 2
fi

PHASE=$(grep -E '^phase:' "$STATUS_FILE" | head -n 1 | sed -E 's/^phase:[[:space:]]*//')
ISSUE=$(grep -E '^issue:' "$STATUS_FILE" | head -n 1 | sed -E 's/^issue:[[:space:]]*//')

if [ -z "$PHASE" ]; then
    echo "UNKNOWN: phase field empty in $STATUS_FILE" >&2
    exit 2
fi

# Final terminal phases — task is done.
case "$PHASE" in
    done|completed|verified|blocked)
        echo "DONE: issue=$ISSUE phase=$PHASE — task complete, safe to exit."
        exit 0
        ;;
esac

# All other phases indicate ongoing work. Print the next step per the A.x
# workflow contract in .claude/skills/task/SKILL.md.
case "$PHASE" in
    claiming|setup)
        NEXT="A.2 — implement and review (ralph loop) or direct implementation for non-testable tasks"
        ;;
    ralph-worker*|ralph-reviewer*|implementing)
        NEXT="A.2b — post process summary on issue (MANDATORY before commit)"
        ;;
    pushing)
        NEXT="A.4 — verify no merge conflicts, then A.5 — monitor CI"
        ;;
    ci-watch*)
        NEXT="A.5 — continue watching CI; if failed, A.7 — fix CI failures"
        ;;
    ci-fix)
        NEXT="A.3 — re-push, then A.5 — monitor CI again"
        ;;
    merging)
        NEXT="A.8 — verify deployment and post evidence (MANDATORY)"
        ;;
    deploying)
        NEXT="A.8 Step 2 — functional verification against dev environment"
        ;;
    verifying)
        NEXT="A.8 Step 3 — post verification evidence comment (MANDATORY gate), then A.9 — retrospective"
        ;;
    retrospective)
        NEXT="Step 5c — file retro issues, then 5d — generate timing summary"
        ;;
    *)
        NEXT="Re-read .claude/skills/task/SKILL.md and determine which step corresponds to phase='$PHASE'"
        ;;
esac

cat <<EOF
RESUME: issue=$ISSUE phase=$PHASE
The task is NOT done. Next required step: $NEXT
Do NOT emit end_turn until the status file shows phase=done or phase=verified.
Re-read .claude/skills/task/SKILL.md sections A.3 onward (or B.2 onward for investigation tasks) before proceeding.
EOF
exit 1
