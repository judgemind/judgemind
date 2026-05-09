#!/usr/bin/env bash
# check-issue-plan-blocked.sh — Detect a recent dispatcher-plan-blocked
# recommendation comment on an issue.
#
# venv: none
# permanent: true
#
# Companion to scripts/check-duplicate-pr.sh and scripts/check-shipped-pr.sh.
# Used by /task Step 4a.3 to short-circuit the path between the duplicate-PR
# check and the gap probe when an earlier plan-phase agent already concluded
# the issue needs operator triage. See issue #4438 for the full failure mode:
#
#   - 2026-04-23: /task-v2-plan returns go=false on issue #2626 with an
#     explicit "operator should close this issue" recommendation. The
#     dispatcher posts the standard plan-blocked comment carrying the
#     <!-- dispatcher-plan-blocked --> sentinel and removes agent/ready.
#   - 2026-05-04: agent/ready drifts back onto #2626 (manual operator
#     action OR auto-restoration via unblock-dependents.sh), and a fresh
#     /task subagent re-claims it. Without this check, the new agent runs
#     plan from scratch and re-derives the same recommendation.
#
# Algorithm:
#   1. Fetch issue comments via `gh issue view <N> --json comments`. Each
#      comment has author.login, authorAssociation, createdAt, and body.
#   2. Filter out comments authored by github-actions[bot] (the only bot
#      account that posts on this repo today). Daemon-posted plan-blocked
#      comments are authored by the operator's gh CLI auth (drewthaler /
#      judgemind-agent), so they are NOT filtered out.
#   3. Pick the latest non-bot comment by createdAt (descending).
#   4. If its body contains the dispatcher-plan-blocked sentinel
#      (<!-- dispatcher-plan-blocked -->), report a `plan-blocked:` line
#      to stdout and exit 0.
#   5. Otherwise, report a `clear:` line and exit 1.
#
# A "latest non-bot comment" gate is the right scope: if a human posted
# AFTER the plan-blocked comment, that's the operator acting on the
# recommendation (re-scoping, asking a question, or just acknowledging).
# We don't want to suppress /task in that case — the human has already
# moved the issue forward. Looking only at the latest comment is the
# minimum that respects operator intent.
#
# Usage:
#   scripts/check-issue-plan-blocked.sh <issue_number>
#   scripts/check-issue-plan-blocked.sh 2626
#   scripts/check-issue-plan-blocked.sh '#2626'        # leading # stripped
#
# Environment variables (testing hooks):
#   CHECK_PLAN_BLOCKED_REPO     — override "judgemind/judgemind"
#   CHECK_PLAN_BLOCKED_GH_BIN   — override "gh" binary path
#
# Exit codes:
#   0 — Plan-blocked recommendation detected on the latest non-bot
#       comment. Stdout: a `plan-blocked:` line citing the comment id
#       and any recommendation token extracted from the footer (e.g.
#       `operator-triage`). Caller pivots to the verify-and-close path
#       documented in .claude/skills/task/SKILL.md §4a.3.
#   1 — No plan-blocked recommendation, or the latest non-bot comment
#       supersedes one (a human posted after the marker). Stdout: a
#       `clear:` line. Caller proceeds with normal /task flow.
#   2 — Error (missing argument, gh CLI unavailable, API failure).
#       Stderr: an `error:` line.

set -uo pipefail

REPO="${CHECK_PLAN_BLOCKED_REPO:-judgemind/judgemind}"
GH_BIN="${CHECK_PLAN_BLOCKED_GH_BIN:-gh}"

PLAN_BLOCKED_SENTINEL="<!-- dispatcher-plan-blocked -->"
PLAN_BLOCKED_FOOTER_PREFIX="<!-- dispatcher-plan-blocked-recommendation:"

# Bot accounts whose comments we filter out before picking the
# "latest non-bot comment". Mirrors scripts/check-issue-author.sh's
# trusted-bot list.
PLAN_BLOCKED_BOT_LOGINS="github-actions[bot]"

# ─── Argument parsing ──────────────────────────────────────────────────────

issue_arg="${1:-}"
if [[ -z "$issue_arg" ]]; then
    echo "error: check-issue-plan-blocked.sh requires an issue number argument (exit 2)" >&2
    echo "  usage: scripts/check-issue-plan-blocked.sh <issue_number>" >&2
    exit 2
fi

issue_num="${issue_arg#\#}"
if ! [[ "$issue_num" =~ ^[0-9]+$ ]]; then
    echo "error: '$issue_arg' is not a valid issue number (exit 2)" >&2
    exit 2
fi

# ─── gh CLI availability ───────────────────────────────────────────────────

if ! command -v "$GH_BIN" >/dev/null 2>&1; then
    echo "error: '$GH_BIN' CLI not found on PATH — cannot check plan-blocked state (exit 2)" >&2
    exit 2
fi

# ─── Fetch issue comments ──────────────────────────────────────────────────

issue_json=""
if ! issue_json=$("$GH_BIN" issue view "$issue_num" --repo "$REPO" --json comments 2>/dev/null); then
    echo "error: failed to fetch comments for issue #${issue_num} from ${REPO} (exit 2)" >&2
    exit 2
fi

# ─── Inspect latest non-bot comment ────────────────────────────────────────
#
# Walk comments newest-first, skip bot accounts, return the first match.
# The "result" line we emit is `plan-blocked:<recommendation>` or `clear:`.
#
# Implemented in python rather than jq+bash because the body may contain
# embedded ``> `` markdown blockquotes and arbitrary markup that bash
# string handling makes brittle. The parser is small and read-only.

result=""
result_exit=0
# Export the env vars so the python helper inherits them. Inline
# command-prefix assignment (`VAR=val cmd`) only applies to the
# immediately-following command, so the original `VAR=val printf ... |
# python3 ...` form leaked the assignments to printf and python3 saw
# the defaults — which silently disabled bot filtering. Export-then-pipe
# is the correct shape.
export BOT_LOGINS="$PLAN_BLOCKED_BOT_LOGINS"
export SENTINEL="$PLAN_BLOCKED_SENTINEL"
export FOOTER_PREFIX="$PLAN_BLOCKED_FOOTER_PREFIX"
result=$(printf '%s' "$issue_json" \
        | python3 "$(dirname "${BASH_SOURCE[0]}")/_check_issue_plan_blocked_inspect.py" \
        2>/dev/null) || result_exit=$?

if [[ "$result_exit" -ne 0 ]]; then
    echo "error: failed to parse comments JSON for issue #${issue_num} (exit 2)" >&2
    exit 2
fi

case "$result" in
    plan-blocked:*)
        recommendation="${result#plan-blocked:}"
        if [[ -z "$recommendation" ]]; then
            recommendation="(unspecified)"
        fi
        echo "plan-blocked: latest non-bot comment on #${issue_num} carries dispatcher-plan-blocked sentinel (recommendation=${recommendation}) (exit 0)"
        exit 0
        ;;
    clear:*)
        # The python helper writes either ``clear:no-comments``,
        # ``clear:no-marker``, or ``clear:superseded`` — surface the
        # reason in the human-readable line so the caller can log it.
        reason="${result#clear:}"
        if [[ -z "$reason" ]]; then
            reason="(unspecified)"
        fi
        echo "clear: no actionable plan-blocked recommendation on #${issue_num} (${reason}) (exit 1)"
        exit 1
        ;;
    *)
        echo "error: helper produced unexpected output for #${issue_num}: '${result}' (exit 2)" >&2
        exit 2
        ;;
esac
