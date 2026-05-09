#!/usr/bin/env bash
# check-near-duplicate-issue.sh — Detect near-duplicate issues filed
# shortly after a sibling closed.
#
# venv: none
# permanent: true
#
# Companion to scripts/check-shipped-pr.sh. The shipped-PR check finds
# merged-but-unclosed PRs whose code already shipped the issue's work,
# subject to the #4353 date-ordering guard (a PR cannot have shipped an
# issue that didn't exist yet). This script catches the missed signal
# in that regime: when an issue is filed shortly AFTER a sibling
# closed, AND the new issue's title / body overlap heavily with the
# sibling, the new issue is almost certainly a near-duplicate. The
# agent picking up the new issue should READ the sibling and the PR
# that closed it before re-implementing the same helper.
#
# Concrete worked example (the issue this script was filed for):
#   - #4321 (closed 2026-05-08T17:07Z, by PR #4325) — "feat(dx):
#     generic splitter-carry-forward drain helper". Body cites
#     ``scripts/drain_splitter_carry_forward_clusters.py``.
#   - #4355 (filed 2026-05-08T19:49Z, ~2.5h later) — "tooling:
#     drain_splitter_carry_forward_clusters.py canonical drain helper".
#     Title shares ``drain``/``splitter``/``carry``/``forward``/
#     ``clusters``/``helper``; body cites the same script path.
#   - The probe emits ``near-duplicate: #4321 (PR #4325)`` —
#     informational signal, the agent decides whether the current
#     issue is genuinely distinct.
#
# Algorithm: see scripts/_check_near_duplicate_issue.py for the full
# algorithm, scoring channels, and threshold rationale.
#
# Usage:
#   scripts/check-near-duplicate-issue.sh <issue_number>
#   scripts/check-near-duplicate-issue.sh 4355
#   scripts/check-near-duplicate-issue.sh '#4355'         # leading # stripped
#
# Environment variables (testing hooks + tuning knobs):
#   CHECK_NEAR_DUP_REPO            — override "judgemind/judgemind"
#   CHECK_NEAR_DUP_GH_BIN          — override "gh" binary path (for tests)
#   CHECK_NEAR_DUP_WINDOW_DAYS     — lookback window in days (default 7)
#   CHECK_NEAR_DUP_TITLE_THRESHOLD — title-token overlap floor (default 2)
#   CHECK_NEAR_DUP_PATH_THRESHOLD  — path overlap floor (default 1)
#   CHECK_NEAR_DUP_LIMIT           — candidate cap (default 30)
#
# Exit codes:
#   0 — Near-duplicate match. Stdout: ``near-duplicate: #<closed_issue>
#       (PR #<closing_pr>) [channel: <title|path|both>] overlap=<list>``
#       on the first line, plus a second-line tab-separated payload
#       ``<closed_issue>\t<closing_pr>\t<channel>\t<overlap>`` for
#       machine consumption. Caller (e.g. /task §4b) prompts the agent
#       to read the closed issue and PR before pivoting to ralph.
#   1 — No near-duplicate. Stdout: ``ok: no near-duplicate issue for
#       #<N> (exit 1)``. Caller proceeds with normal /task flow.
#   2 — Error (missing arg, gh CLI unavailable, API failure).
#       Stderr: ``error: ...``.

set -uo pipefail

REPO="${CHECK_NEAR_DUP_REPO:-judgemind/judgemind}"
GH_BIN="${CHECK_NEAR_DUP_GH_BIN:-gh}"

# ─── Argument parsing ──────────────────────────────────────────────────────

issue_arg="${1:-}"
if [[ -z "$issue_arg" ]]; then
    echo "error: check-near-duplicate-issue.sh requires an issue number argument (exit 2)" >&2
    echo "  usage: scripts/check-near-duplicate-issue.sh <issue_number>" >&2
    exit 2
fi

issue_num="${issue_arg#\#}"
if ! [[ "$issue_num" =~ ^[0-9]+$ ]]; then
    echo "error: '$issue_arg' is not a valid issue number (exit 2)" >&2
    exit 2
fi

# ─── gh CLI availability ───────────────────────────────────────────────────

if ! command -v "$GH_BIN" >/dev/null 2>&1; then
    echo "error: '$GH_BIN' CLI not found on PATH — cannot check near-duplicate issues (exit 2)" >&2
    exit 2
fi

# ─── Fetch the current issue's title + body + createdAt ────────────────────

issue_json=""
if ! issue_json=$("$GH_BIN" issue view "$issue_num" --repo "$REPO" \
        --json body,title,createdAt 2>/dev/null); then
    echo "error: failed to fetch issue #${issue_num} from ${REPO} (exit 2)" >&2
    exit 2
fi

# ─── Run the probe ─────────────────────────────────────────────────────────
#
# The Python helper does the heavy lifting: list recently-closed issues
# in the window, fetch each candidate's title/body/closing-PR, score
# overlap, and emit the first match. The helper is opt-out via
# CHECK_NEAR_DUP_DISABLE=1 so callers running against shallow / non-git
# fixtures or specific bash-mock test setups can suppress the channel
# without removing the wiring.

probe_disabled="${CHECK_NEAR_DUP_DISABLE:-}"
if [[ -n "$probe_disabled" ]]; then
    echo "ok: near-duplicate probe disabled via CHECK_NEAR_DUP_DISABLE — skipping (exit 1)"
    exit 1
fi

probe_out=""
probe_exit=0
probe_out=$(printf '%s' "$issue_json" | \
    CHECK_NEAR_DUP_REPO="$REPO" \
    CHECK_NEAR_DUP_GH_BIN="$GH_BIN" \
    CHECK_NEAR_DUP_ISSUE="$issue_num" \
    python3 \
    "$(dirname "${BASH_SOURCE[0]}")/_check_near_duplicate_issue.py" \
    2>/dev/null) || probe_exit=$?

if [[ "$probe_exit" -eq 2 ]]; then
    echo "error: near-duplicate probe failed for issue #${issue_num} (exit 2)" >&2
    exit 2
fi

if [[ "$probe_exit" -ne 0 || -z "$probe_out" ]]; then
    echo "ok: no near-duplicate issue for #${issue_num} (exit 1)"
    exit 1
fi

# Parse ``near-duplicate:<closed>\t<pr>\t<channel>\t<overlap>``. Defensive
# manual splits — IFS read consumes a subshell and the blunt instrument
# parses are easier to keep robust than wider IFS games.
if [[ "$probe_out" != near-duplicate:* ]]; then
    echo "error: malformed probe output: '$probe_out' (exit 2)" >&2
    exit 2
fi

rest="${probe_out#near-duplicate:}"
closed_issue="${rest%%$'\t'*}"
rest="${rest#*$'\t'}"
closing_pr="${rest%%$'\t'*}"
rest="${rest#*$'\t'}"
channel="${rest%%$'\t'*}"
overlap=""
if [[ "$rest" == *$'\t'* ]]; then
    overlap="${rest#*$'\t'}"
fi

if ! [[ "$closed_issue" =~ ^[0-9]+$ ]]; then
    echo "error: malformed closed-issue field: '$closed_issue' (exit 2)" >&2
    exit 2
fi

# Pretty-print the human-readable line, then emit the machine-readable
# payload on a second line so callers can mechanically parse without
# re-running the probe. The caller's bash script can read both lines —
# the first is for transcript readability, the second is for piping.
if [[ -n "$closing_pr" ]]; then
    echo "near-duplicate: #${closed_issue} (PR #${closing_pr}) [channel: ${channel}] overlap=${overlap} (exit 0)"
else
    echo "near-duplicate: #${closed_issue} [channel: ${channel}] overlap=${overlap} (exit 0)"
fi
echo "${closed_issue}"$'\t'"${closing_pr}"$'\t'"${channel}"$'\t'"${overlap}"
exit 0
