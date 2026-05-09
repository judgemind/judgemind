#!/usr/bin/env bash
# check-issue-companion-closed.sh — Detect "companion-closed" obsoletion
# signal on an issue: the body cites a sibling #N that is now closed-as-
# completed AND the cite appears inside companion framing ("when #N
# lands", "removed when #N", "blocked on #N", etc.).
#
# venv: none
# permanent: true
#
# Companion to scripts/check-shipped-pr.sh, scripts/check-issue-plan-blocked.sh,
# and scripts/check-near-duplicate-issue.sh. Used by /task Step 4 to
# short-circuit the path between §4a.3 (plan-blocked check) and §4b
# (gap probe) when the current issue's intent is obsoleted by an
# already-closed companion issue.
#
# See issue #4557 for the full rationale and the canonical worked
# example (#4409 ↔ #4408 ↔ PR #4416, 2026-05-09).
#
# Algorithm:
#   1. Fetch the issue body via `gh issue view <N> --json body`.
#   2. Extract every `#NNN` reference from the body.
#   3. For each referenced #M, fetch its `state` + `stateReason` via
#      `gh issue view M --json state,stateReason`. Skip references that
#      do not resolve (deleted issues, PR numbers that don't exist, etc.).
#   4. Hand the body and the resolved state map to
#      `_check_issue_companion_closed_inspect.py`. The helper:
#        a. Splits the body into blank-line-delimited paragraph chunks.
#        b. For each chunk, finds `#N` references and tests for
#           companion-framing keywords (when, after, until, once, removed
#           when, blocked on, blocked by, depends on) in the SAME chunk.
#        c. If any companion-framed `#N` resolves to a `state == closed`
#           AND `stateReason == COMPLETED` sibling, emits
#           `companion-closed:<N>` (exit 0).
#        d. Otherwise emits `clear:<reason>` (exit 1).
#
# Why per-paragraph framing: bare hashtags like "see #4408 for context"
# or "Closes #4408" are not companion framing; they're informational
# links. A paragraph-scoped check captures the canonical worked example
# ("This is a temporary caveat that should be removed when the
# structural fix in #4408 lands.") while avoiding false-fires on
# unrelated cites elsewhere in the body. See issue #4557 §"Proposed
# change" for the framing list.
#
# Why closed-AS-COMPLETED only: a `not_planned` close (operator decided
# the work isn't needed) does NOT obsolete the dependent issue — it
# means the structural fix isn't coming, so the temporary-caveat docs
# might still be needed. Same for still-open siblings. Only "the
# blocker landed AND was completed" produces the obsoletion signal.
#
# Usage:
#   scripts/check-issue-companion-closed.sh <issue_number>
#   scripts/check-issue-companion-closed.sh 4409
#   scripts/check-issue-companion-closed.sh '#4409'    # leading # stripped
#
# Environment variables (testing hooks):
#   CHECK_COMPANION_CLOSED_REPO     — override "judgemind/judgemind"
#   CHECK_COMPANION_CLOSED_GH_BIN   — override "gh" binary path
#
# Exit codes:
#   0 — Companion-closed obsoletion detected. Stdout: a
#       `companion-closed:<N>` line citing the sibling issue. Caller
#       pivots to the §4b verify-and-close branch documented in
#       .claude/skills/task/SKILL.md §4a.4.
#   1 — No actionable signal. Stdout: a `clear:<reason>` line where
#       reason is one of `no-references`, `no-companion`, or
#       `no-closed-completed`.
#   2 — Error (missing argument, gh CLI unavailable, API failure).
#       Stderr: an `error:` line.

set -uo pipefail

REPO="${CHECK_COMPANION_CLOSED_REPO:-judgemind/judgemind}"
GH_BIN="${CHECK_COMPANION_CLOSED_GH_BIN:-gh}"

# ─── Argument parsing ──────────────────────────────────────────────────────

issue_arg="${1:-}"
if [[ -z "$issue_arg" ]]; then
    echo "error: check-issue-companion-closed.sh requires an issue number argument (exit 2)" >&2
    echo "  usage: scripts/check-issue-companion-closed.sh <issue_number>" >&2
    exit 2
fi

issue_num="${issue_arg#\#}"
if ! [[ "$issue_num" =~ ^[0-9]+$ ]]; then
    echo "error: '$issue_arg' is not a valid issue number (exit 2)" >&2
    exit 2
fi

# ─── gh CLI availability ───────────────────────────────────────────────────

if ! command -v "$GH_BIN" >/dev/null 2>&1; then
    echo "error: '$GH_BIN' CLI not found on PATH — cannot check companion-closed state (exit 2)" >&2
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
    # Empty body means no references. Treat as `clear:no-references`
    # without round-tripping the helper.
    echo "clear: no #N references in body of #${issue_num} (no-references) (exit 1)"
    exit 1
fi

# ─── Extract referenced issue numbers ──────────────────────────────────────
#
# Walk the body for every `#NNN` token. Use python so we can match the
# exact same regex the inspector uses (avoids classifier drift).

referenced_nums=""
referenced_exit=0
referenced_nums=$(printf '%s' "$body" | python3 -c '
import re
import sys

HASHTAG_REGEX = re.compile(r"#(\d+)\b")
seen: set[str] = set()
out: list[str] = []
for m in HASHTAG_REGEX.finditer(sys.stdin.read()):
    n = m.group(1)
    if n in seen or n == "":
        continue
    seen.add(n)
    out.append(n)
sys.stdout.write("\n".join(out))
' 2>/dev/null) || referenced_exit=$?

if [[ "$referenced_exit" -ne 0 ]]; then
    echo "error: failed to extract hashtag references from body of issue #${issue_num} (exit 2)" >&2
    exit 2
fi

# Build a JSON map of `{cited_number: {state, stateReason}}`. Skip
# references that don't resolve (deleted issues, PR numbers, etc.) by
# treating their state as missing — the inspector's `_is_closed_completed`
# returns False on missing entries.
sibling_states_json="{}"
if [[ -n "$referenced_nums" ]]; then
    # Self-reference suppression: skip the current issue's own number
    # (the body's "Parent: #N" line or self-cites).
    sibling_states_pairs=""
    while IFS= read -r ref; do
        if [[ -z "$ref" || "$ref" == "$issue_num" ]]; then
            continue
        fi
        sibling_json=$("$GH_BIN" issue view "$ref" --repo "$REPO" --json state,stateReason 2>/dev/null) || continue
        if [[ -z "$sibling_json" ]]; then
            continue
        fi
        # Append a JSON pair as a one-line `<num>=<json>`. The python
        # accumulator below assembles them into a proper map.
        if [[ -z "$sibling_states_pairs" ]]; then
            sibling_states_pairs="${ref}=${sibling_json}"
        else
            sibling_states_pairs+=$'\n'"${ref}=${sibling_json}"
        fi
    done <<< "$referenced_nums"

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
        | python3 "$(dirname "${BASH_SOURCE[0]}")/_check_issue_companion_closed_inspect.py" \
        2>/dev/null) || result_exit=$?

if [[ "$result_exit" -ne 0 ]]; then
    echo "error: failed to run companion-closed inspector for issue #${issue_num} (exit 2)" >&2
    exit 2
fi

case "$result" in
    companion-closed:*)
        cited="${result#companion-closed:}"
        if [[ -z "$cited" ]]; then
            cited="(unknown)"
        fi
        echo "companion-closed: issue #${issue_num} cites closed-completed companion #${cited} in companion-framed clause (exit 0)"
        exit 0
        ;;
    clear:*)
        reason="${result#clear:}"
        if [[ -z "$reason" ]]; then
            reason="(unspecified)"
        fi
        echo "clear: no actionable companion-closed signal on #${issue_num} (${reason}) (exit 1)"
        exit 1
        ;;
    *)
        echo "error: helper produced unexpected output for #${issue_num}: '${result}' (exit 2)" >&2
        exit 2
        ;;
esac
