#!/usr/bin/env bash
# check-terminal-routing-comments.sh — Require a ROUTING comment for every
# ``self._mark_agent_terminal(`` call site in ``scripts/dispatcher/daemon.py``.
#
# Why this check exists
# ---------------------
# The #3062 audit (``docs/investigations/
# agent-terminal-failure-routing-audit-2026-04.md``) classified all 40
# then-extant ``_mark_agent_terminal`` call sites in ``daemon.py`` into
# three buckets: routed through the unified ``_handle_agent_failure``
# path, inline-routed via ``_write_failure``, or intentionally
# unrouted (infra-preemption, correct-outcome terminals, defensive
# invariants, diagnoser-consumer close-outs). The audit annotated
# every site with a ``ROUTING (#3062)`` comment documenting which
# bucket it falls into — the #3054 ``ralph_not_ship`` bug that
# motivated the audit was authored without any such reasoning, and the
# annotations are the artifact that prevents repeat authorship.
#
# As the daemon grows, new terminal call sites get added. Without
# enforcement, an author can add a site without the audit reasoning
# step — re-introducing the #3054 class of bug. This check fails the
# pre-push hook / CI when a call site lacks a nearby ROUTING comment,
# forcing the author to either (a) route it through
# ``_handle_agent_failure``, (b) inline-route it via ``_write_failure``
# + a ``ROUTING`` comment, or (c) document why it is intentionally
# unrouted in a ``ROUTING`` comment.
#
# Tracking: issue #3062 (audit), #3072 (this lint rule).
#
# Rule
# ----
# For every ``self._mark_agent_terminal(`` call site in
# ``scripts/dispatcher/daemon.py``, there MUST be a ``ROUTING (#N)``
# comment (any issue number) within the ``ROUTING_WINDOW_LINES`` lines
# immediately above the call line.
#
# ``ROUTING_WINDOW_LINES`` is 250 by default. The window is large
# enough to cover the audit's two cluster patterns — ``_apply_fix_ci_patch``
# (8 call sites sharing one method docstring, max observed gap 176
# lines) and the 5 ``_consume_action_*`` diagnoser-consumer methods
# (one shared docstring in ``_consume_action_escalate`` covers all 5,
# max observed gap 215 lines) — without demanding redundant per-site
# comments. Call sites outside that window must have their own
# ROUTING comment nearby, which is the cheap-and-correct default.
#
# The check is text-based (not AST-based) so a mid-flight daemon.py
# edit by a parallel branch does not produce a flaky check — any
# ordering of edits that keeps each call site covered passes.
#
# Usage
# -----
#   scripts/check-terminal-routing-comments.sh            # scan the real daemon.py
#   scripts/check-terminal-routing-comments.sh [file]     # scan a specific file
#
# Exit codes
# ----------
#   0 — Every call site has a ROUTING comment within the window.
#   1 — At least one call site is missing a nearby ROUTING comment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_FILE="${1:-$REPO_ROOT/scripts/dispatcher/daemon.py}"

# Window size — see header comment for the rationale behind the
# chosen value. Override with the ROUTING_WINDOW_LINES env var for
# ad-hoc experiments; production always uses the default.
: "${ROUTING_WINDOW_LINES:=250}"

# ─── Fast exit if the target file does not exist ─────────────────────────
# This handles two situations cleanly:
#   (1) the daemon was relocated or removed — in which case this
#       check should not block unrelated PRs; a separate check (or
#       the author) will notice the move.
#   (2) running the test harness against a temp dir that simulates a
#       tree without daemon.py.
if [[ ! -f "$TARGET_FILE" ]]; then
    echo "check-terminal-routing-comments: no target file at $TARGET_FILE — nothing to check."
    exit 0
fi

# ─── Regex patterns ─────────────────────────────────────────────────────
# Call-site pattern: ``self._mark_agent_terminal(`` with any whitespace
# between the identifier and the opening paren. This matches the
# actual invocation, not the ``def`` line or docstring prose.
CALL_PATTERN='self\._mark_agent_terminal[[:space:]]*\('

# ROUTING comment pattern: any ``ROUTING (#<digits>)`` token. Accepts
# the #3062 convention introduced by the audit AND any future
# ``ROUTING (#XXXX)`` variant from subsequent audits.
ROUTING_PATTERN='ROUTING[[:space:]]*\(#[0-9]+\)'

# ─── Locate call sites ──────────────────────────────────────────────────
# We use while-read loops rather than ``mapfile`` so the script runs on
# macOS's stock bash 3.2 — same constraint observed by
# ``scripts/check-bare-shadcn-accent.sh`` and
# ``scripts/check-no-inline-ecs-healthcheck.sh``.
call_lines=()
while IFS= read -r line; do
    [[ -n "$line" ]] && call_lines+=("$line")
done < <(grep -nE "$CALL_PATTERN" "$TARGET_FILE" | cut -d: -f1 || true)

if [[ ${#call_lines[@]} -eq 0 ]]; then
    # The daemon normally has many sites; zero is suspicious but not
    # a check failure. A renamed helper or a large refactor may
    # legitimately produce zero hits here. Log a notice and pass.
    echo "check-terminal-routing-comments: no _mark_agent_terminal call sites in $TARGET_FILE — nothing to check."
    exit 0
fi

# Locate every line that carries a ROUTING token so we can scan
# backwards efficiently. Store a sorted list of line numbers.
routing_lines=()
while IFS= read -r line; do
    [[ -n "$line" ]] && routing_lines+=("$line")
done < <(grep -nE "$ROUTING_PATTERN" "$TARGET_FILE" | cut -d: -f1 || true)

# ─── Check each call site ───────────────────────────────────────────────
violations=0
missing=()

for call_line in "${call_lines[@]}"; do
    # The window covers lines [call_line - ROUTING_WINDOW_LINES, call_line].
    window_start=$((call_line - ROUTING_WINDOW_LINES))
    (( window_start < 1 )) && window_start=1

    found=0
    # ``${routing_lines[@]+...}`` guards empty-array expansion under
    # ``set -u`` on bash 3.2 (macOS). If no ROUTING comments exist at
    # all, every call site is a violation — the loop body never runs
    # and ``found`` stays 0.
    if [[ ${#routing_lines[@]} -gt 0 ]]; then
        for r in "${routing_lines[@]}"; do
            if (( r <= call_line && r >= window_start )); then
                found=1
                break
            fi
        done
    fi

    if (( found == 0 )); then
        missing+=("$call_line")
        violations=$((violations + 1))
    fi
done

if (( violations > 0 )); then
    echo "ERROR: _mark_agent_terminal call site(s) missing a nearby ROUTING comment."
    echo ""
    echo "  Target: $TARGET_FILE"
    echo "  Window: ${ROUTING_WINDOW_LINES} lines above each call site."
    echo ""
    echo "  Every call site must document whether it routes through"
    echo "  _handle_agent_failure (the unified failure path from #3032),"
    echo "  inline-routes via _write_failure, or is intentionally unrouted"
    echo "  (and why). See #3062 audit for the classification convention:"
    echo "  docs/investigations/agent-terminal-failure-routing-audit-2026-04.md"
    echo ""
    echo "  Sites missing a ROUTING (#N) comment in the ${ROUTING_WINDOW_LINES}"
    echo "  lines above the call:"
    echo ""
    # Length-guard the iteration — see #4479.
    if [ "${#missing[@]}" -gt 0 ]; then
        for line in "${missing[@]}"; do
            # Show the offending line in context for fast triage.
            snippet="$(sed -n "${line}p" "$TARGET_FILE")"
            echo "    $TARGET_FILE:${line}: ${snippet}"
        done
    fi
    echo ""
    echo "  Fix: add a comment above the call such as"
    echo ""
    echo "    # ROUTING (#3062): NOT routed through _handle_agent_failure —"
    echo "    # <reason: infra-preemption / correct-outcome / defensive / etc.>"
    echo ""
    echo "  Or route the failure through _handle_agent_failure and reference"
    echo "  the failure category constant in the comment."
    exit 1
fi

echo "check-terminal-routing-comments: ${#call_lines[@]} _mark_agent_terminal call site(s) all covered by a ROUTING comment within ${ROUTING_WINDOW_LINES} lines."
exit 0
