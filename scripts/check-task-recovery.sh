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
    ralph-worker*|ralph-reviewer*)
        # Consult ralph-done.txt to determine which branch of the ralph loop we are in.
        RALPH_DONE="$WORKTREE/tmp/ralph/ralph-done.txt"
        if [ ! -f "$RALPH_DONE" ]; then
            # ralph-done.txt absent — ralph is mid-loop (worker still running or never completed)
            RALPH_ITER=$(echo "$PHASE" | grep -oE '[0-9]+' | head -n 1)
            if [ -z "$RALPH_ITER" ]; then
                RALPH_ITER="unknown"
            fi
            NEXT="resume the ralph loop from iteration $RALPH_ITER — re-read .claude/skills/ralph/SKILL.md Step 2 and continue"
        else
            FIRST_LINE=$(head -n 1 "$RALPH_DONE")
            if echo "$FIRST_LINE" | grep -q "SHIP"; then
                # ralph completed with SHIP — proceed to post-ralph summary step
                NEXT="A.2b — post process summary on issue (MANDATORY before commit)"
            elif echo "$FIRST_LINE" | grep -qE "MAX_ITERATIONS|AC_INFEASIBLE|STUCK"; then
                # ralph hit a terminal blocker — teardown the label interlock
                NEXT="teardown: release status/in-progress label (gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress) per task §A.2 STUCK path, then stop"
            else
                # Unknown content in ralph-done.txt — conservative: point to A.2b
                NEXT="A.2b — post process summary on issue (check ralph-done.txt content first)"
            fi
        fi
        ;;
    implementing)
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
    # /audit phases
    audit-setup)
        NEXT="continue with audit Step 1 — begin category 1.1 (adversarial code review); resume in .claude/skills/audit/SKILL.md §1.1"
        ;;
    audit-category-1.1)
        NEXT="continue with category 1.2 — CLAUDE.md hygiene; resume in .claude/skills/audit/SKILL.md §1.2"
        ;;
    audit-category-1.2)
        NEXT="continue with category 1.3 — architecture drift; resume in .claude/skills/audit/SKILL.md §1.3"
        ;;
    audit-category-1.3)
        NEXT="continue with category 1.4 — test quality; resume in .claude/skills/audit/SKILL.md §1.4"
        ;;
    audit-category-1.4)
        NEXT="continue with category 1.5 — performance; resume in .claude/skills/audit/SKILL.md §1.5"
        ;;
    audit-category-1.5)
        NEXT="continue with category 1.6 — Security; resume in .claude/skills/audit/SKILL.md §1.6"
        ;;
    audit-category-1.6)
        NEXT="continue with category 1.7 — dependency health; resume in .claude/skills/audit/SKILL.md §1.7"
        ;;
    audit-category-1.7)
        NEXT="continue with category 1.8 — CI health; resume in .claude/skills/audit/SKILL.md §1.8"
        ;;
    audit-category-1.8)
        NEXT="continue with category 1.9 — scripts directory hygiene; resume in .claude/skills/audit/SKILL.md §1.9"
        ;;
    audit-category-1.9)
        NEXT="continue with Step 2 — deduplicate findings; resume in .claude/skills/audit/SKILL.md §Step 2"
        ;;
    audit-dedup)
        NEXT="continue with Step 3 — file issues; resume in .claude/skills/audit/SKILL.md §Step 3"
        ;;
    audit-file-issues)
        NEXT="continue with Step 4 — write summary report; resume in .claude/skills/audit/SKILL.md §Step 4"
        ;;
    audit-report)
        NEXT="continue with Step 5 — notify completion and write phase=done; resume in .claude/skills/audit/SKILL.md §Step 5"
        ;;
    # /spotcheck phases
    spotcheck-step-0)
        NEXT="continue with §1 (rulings direction review); resume in .claude/skills/spotcheck/SKILL.md §Step 1"
        ;;
    spotcheck-step-1)
        NEXT="continue with §2 (originals direction review); resume in .claude/skills/spotcheck/SKILL.md §Step 2"
        ;;
    spotcheck-step-2)
        NEXT="continue with §2.5 (S3 orphan check) then §3 (screenshots) and §4 (cross-reference); resume in .claude/skills/spotcheck/SKILL.md §2.5"
        ;;
    spotcheck-step-2.5)
        NEXT="continue with §3 (screenshots, optional) then §4 (cross-reference and file issues); resume in .claude/skills/spotcheck/SKILL.md §Step 3"
        ;;
    spotcheck-step-4)
        NEXT="continue with §5 — write summary report; resume in .claude/skills/spotcheck/SKILL.md §Step 5"
        ;;
    spotcheck-step-5)
        NEXT="write phase=done to the status file — spotcheck is complete"
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
