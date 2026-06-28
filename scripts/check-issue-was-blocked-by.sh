#!/usr/bin/env bash
# check-issue-was-blocked-by.sh — Detect the "was-blocked-by, now-closed"
# zombie-blocker signal on an issue: the body carries a durable
# `Was-blocked-by: #A, #B, #C (...)` provenance marker (left by
# scripts/unblock-dependents.sh / .github/workflows/unblock-issues.yml
# when they auto-restore agent/ready) AND every cited former blocker is
# now closed-as-completed.
#
# venv: none
# permanent: true
#
# Companion to scripts/check-issue-companion-closed.sh,
# scripts/check-shipped-pr.sh, scripts/check-issue-plan-blocked.sh, and
# scripts/check-near-duplicate-issue.sh. Used by /task Step 4 §4a.5 to
# pivot to verify-and-close when an issue's former blockers have all
# closed-completed — without re-running ralph from scratch.
#
# See issue #4610 for the full rationale and the canonical recurrence
# (#3732 ↔ #4282/#4297/#4370: blockers stripped on auto-unblock, #3732
# sat agent/ready un-verified for ~7 weeks after they all closed).
#
# Algorithm:
#   1. Fetch the issue body via `gh issue view <N> --json body`.
#   2. Extract the `#N` numbers cited inside the `Was-blocked-by:` marker.
#      If no marker → clear:no-marker (exit 1).
#   3. For each cited former blocker #M, fetch its `state` + `stateReason`
#      via `gh issue view M --json state,stateReason`. Build the
#      SIBLING_STATES_JSON map.
#   4. Hand the body and the resolved state map to
#      `_check_issue_was_blocked_by_inspect.py`. The helper fires
#      `was-blocked-by:<nums>` (exit 0) only when EVERY cited former
#      blocker is `state == closed` AND `stateReason == COMPLETED`.
#
# Why all-closed-completed only: if any former blocker is still open or
# closed-as-not_planned, the structural fix the issue was waiting on did
# NOT land as completed — the issue's work may still be needed, so /task
# should run normally rather than pivot to close.
#
# Usage:
#   scripts/check-issue-was-blocked-by.sh <issue_number>
#   scripts/check-issue-was-blocked-by.sh 3732
#   scripts/check-issue-was-blocked-by.sh '#3732'    # leading # stripped
#
# Environment variables (testing hooks):
#   CHECK_WAS_BLOCKED_BY_REPO     — override "judgemind/judgemind"
#   CHECK_WAS_BLOCKED_BY_GH_BIN   — override "gh" binary path
#
# Exit codes:
#   0 — Former blockers all closed-completed. Stdout: a `was-blocked-by:`
#       line naming the closed former blockers. Caller pivots to the
#       §4a.5 verify-and-close branch documented in
#       .claude/skills/task/SKILL.md §4a.5.
#   1 — No actionable signal. Stdout: a `clear:<reason>` line where
#       reason is one of `no-marker`, `not-all-closed-completed`.
#   2 — Error (missing argument, gh CLI unavailable, API failure).
#       Stderr: an `error:` line.

set -uo pipefail

REPO="${CHECK_WAS_BLOCKED_BY_REPO:-judgemind/judgemind}"
GH_BIN="${CHECK_WAS_BLOCKED_BY_GH_BIN:-gh}"

# ─── Argument parsing ──────────────────────────────────────────────────────

issue_arg="${1:-}"
if [[ -z "$issue_arg" ]]; then
    echo "error: check-issue-was-blocked-by.sh requires an issue number argument (exit 2)" >&2
    echo "  usage: scripts/check-issue-was-blocked-by.sh <issue_number>" >&2
    exit 2
fi

issue_num="${issue_arg#\#}"
if ! [[ "$issue_num" =~ ^[0-9]+$ ]]; then
    echo "error: '$issue_arg' is not a valid issue number (exit 2)" >&2
    exit 2
fi

# ─── gh CLI availability ───────────────────────────────────────────────────

if ! command -v "$GH_BIN" >/dev/null 2>&1; then
    echo "error: '$GH_BIN' CLI not found on PATH — cannot check was-blocked-by state (exit 2)" >&2
    exit 2
fi

# ─── Fetch issue body ──────────────────────────────────────────────────────

issue_json=""
if ! issue_json=$("$GH_BIN" issue view "$issue_num" --repo "$REPO" --json body 2>/dev/null); then
    echo "error: failed to fetch body for issue #${issue_num} from ${REPO} (exit 2)" >&2
    exit 2
fi

# Extract the body string. Use python for robust JSON handling — bash's
# string slicing on an embedded JSON-escaped body is fragile when the
# body contains ``"`` characters.
body=""
body_exit=0
body=$(printf '%s' "$issue_json" | python3 -c '
import json
import sys
try:
    payload = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError:
    sys.exit(1)
body = payload.get("body") or ""
if not isinstance(body, str):
    body = str(body)
sys.stdout.write(body)
' 2>/dev/null) || body_exit=$?

if [[ "$body_exit" -ne 0 ]]; then
    echo "error: failed to parse body JSON for issue #${issue_num} (exit 2)" >&2
    exit 2
fi

if [[ -z "$body" ]]; then
    # Empty body means no marker. Treat as `clear:no-marker` without
    # round-tripping the helper.
    echo "clear: no actionable was-blocked-by signal on #${issue_num} (no-marker) (exit 1)"
    exit 1
fi

# ─── Extract the marker's cited former-blocker numbers ─────────────────────
#
# Walk the body for every `#N` inside a `Was-blocked-by:` line. Use python
# so we match the exact same regex the inspector uses (avoids drift).

marker_nums=""
marker_exit=0
marker_nums=$(printf '%s' "$body" | python3 -c '
import re
import sys

MARKER_LINE_REGEX = re.compile(r"^\s*Was-blocked-by:\s*(.*)$", re.MULTILINE)
HASHTAG_REGEX = re.compile(r"#(\d+)\b")
seen: set[str] = set()
out: list[str] = []
text = sys.stdin.read()
for tail in MARKER_LINE_REGEX.findall(text):
    for m in HASHTAG_REGEX.finditer(tail):
        n = m.group(1)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
sys.stdout.write("\n".join(out))
' 2>/dev/null) || marker_exit=$?

if [[ "$marker_exit" -ne 0 ]]; then
    echo "error: failed to extract Was-blocked-by cites from body of issue #${issue_num} (exit 2)" >&2
    exit 2
fi

# Build a JSON map of `{cited_number: {state, stateReason}}`. References
# that don't resolve are skipped — the inspector's `_is_closed_completed`
# returns False on missing entries (which yields not-all-closed-completed).
sibling_states_json="{}"
if [[ -n "$marker_nums" ]]; then
    sibling_states_pairs=""
    while IFS= read -r ref; do
        if [[ -z "$ref" ]]; then
            continue
        fi
        sibling_json=$("$GH_BIN" issue view "$ref" --repo "$REPO" --json state,stateReason 2>/dev/null) || continue
        if [[ -z "$sibling_json" ]]; then
            continue
        fi
        if [[ -z "$sibling_states_pairs" ]]; then
            sibling_states_pairs="${ref}=${sibling_json}"
        else
            sibling_states_pairs+=$'\n'"${ref}=${sibling_json}"
        fi
    done <<< "$marker_nums"

    if [[ -n "$sibling_states_pairs" ]]; then
        export PAIRS="$sibling_states_pairs"
        sibling_states_json=$(python3 -c '
import json
import os
import sys

raw = os.environ.get("PAIRS", "") or ""
out: dict[str, object] = {}
for line in raw.splitlines():
    if not line or "=" not in line:
        continue
    num, _, payload = line.partition("=")
    num = num.strip()
    payload = payload.strip()
    if not num or not payload:
        continue
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        continue
    if isinstance(parsed, dict):
        out[num] = parsed
sys.stdout.write(json.dumps(out))
' 2>/dev/null) || sibling_states_json="{}"
    fi
fi

# ─── Run the inspector ────────────────────────────────────────────────────

result=""
result_exit=0
export SIBLING_STATES_JSON="$sibling_states_json"
result=$(printf '%s' "$body" \
        | python3 "$(dirname "${BASH_SOURCE[0]}")/_check_issue_was_blocked_by_inspect.py" \
        2>/dev/null) || result_exit=$?

if [[ "$result_exit" -ne 0 ]]; then
    echo "error: failed to run was-blocked-by inspector for issue #${issue_num} (exit 2)" >&2
    exit 2
fi

case "$result" in
    was-blocked-by:*)
        cited="${result#was-blocked-by:}"
        if [[ -z "$cited" ]]; then
            cited="(unknown)"
        fi
        # Reformat the bare comma-joined nums into a #-prefixed list.
        cited_pretty=$(printf '%s' "$cited" | python3 -c '
import sys
nums = [n for n in sys.stdin.read().strip().split(",") if n]
sys.stdout.write(", ".join(f"#{n}" for n in nums))
' 2>/dev/null) || cited_pretty="$cited"
        echo "was-blocked-by: issue #${issue_num} former blockers all closed-completed (${cited_pretty}) — pivot to verify-and-close (exit 0)"
        exit 0
        ;;
    clear:*)
        reason="${result#clear:}"
        if [[ -z "$reason" ]]; then
            reason="(unspecified)"
        fi
        echo "clear: no actionable was-blocked-by signal on #${issue_num} (${reason}) (exit 1)"
        exit 1
        ;;
    *)
        echo "error: helper produced unexpected output for #${issue_num}: '${result}' (exit 2)" >&2
        exit 2
        ;;
esac
