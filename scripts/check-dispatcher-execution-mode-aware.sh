#!/usr/bin/env bash
# check-dispatcher-execution-mode-aware.sh — Every
# ``UPDATE dispatcher.agents`` / ``SELECT ... FROM dispatcher.agents``
# call site in ``scripts/dispatcher/daemon.py`` must either:
#   (a) mention ``execution_mode`` (or ``agent_task_arn``) within a
#       nearby window, OR
#   (b) carry a nearby ``# exec-mode-agnostic (#N):`` annotation
#       explaining why the call is mode-independent by construction
#       (terminal writers, phase stamps, milestone columns, etc.).
#
# Why this check exists
# ---------------------
# Three Option A rollout bugs (#3091 draft, #3128-era, #3152) all had
# the same shape: a code path authored pre-ECS that assumed the agent
# was a local subprocess under the daemon. The #3158 audit
# (``docs/investigations/dispatcher-execution-mode-audit-2026-04.md``)
# classified every ``dispatcher.agents`` read/write and fixed three
# sites (``_check_stuck_agents``, ``_resume_retrying_agent``,
# ``_handle_force_stop``). This check prevents a fourth site from
# being added silently — every new ``UPDATE dispatcher.agents`` /
# ``SELECT ... dispatcher.agents`` line now either (a) observes the
# mode flag, or (b) carries an inline audit stamp.
#
# Rule
# ----
# Every line in ``scripts/dispatcher/daemon.py`` matching
# ``UPDATE dispatcher.agents`` or ``FROM dispatcher.agents`` (inside a
# SQL string literal — matched by simple text pattern) MUST have at
# least one of the following within ``EXEC_MODE_WINDOW_LINES`` lines
# of surrounding code (window is symmetric: N lines above AND below
# the SQL line, to cover both the docstring-before-method and
# exception-handler-after-SQL shapes):
#
#   - the token ``execution_mode`` appears, OR
#   - the token ``agent_task_arn`` appears, OR
#   - the phrase ``exec-mode-agnostic (#<digits>)`` appears (typically
#     as a ``# exec-mode-agnostic (#3158): <reason>`` inline comment
#     OR as a docstring line beginning with
#     ``exec-mode-agnostic (#3158): <reason>``).
#
# Tracking: issue #3158.
#
# Usage
# -----
#   scripts/check-dispatcher-execution-mode-aware.sh            # scan real daemon.py
#   scripts/check-dispatcher-execution-mode-aware.sh [file]     # scan a specific file
#
# Exit codes
# ----------
#   0 — Every call site is covered.
#   1 — At least one call site is missing nearby coverage.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_FILE="${1:-$REPO_ROOT/scripts/dispatcher/daemon.py}"

# Symmetric window. 60 lines above AND below covers the typical
# SQL-inside-method shape (docstring + SELECT/UPDATE + exception
# handler + return). The #3158 audit measured the maximum
# surrounding-text gap needed for a true-positive at ~55 lines (a
# method with two SQL statements and a long docstring). 60 keeps
# headroom for refactors that grow docstrings.
: "${EXEC_MODE_WINDOW_LINES:=60}"

if [[ ! -f "$TARGET_FILE" ]]; then
    echo "check-dispatcher-execution-mode-aware: no target file at $TARGET_FILE — nothing to check."
    exit 0
fi

# Lines that invoke the table (SELECT / UPDATE / DELETE, case-
# insensitive SQL keywords inside Python string literals). We match
# the literal ``dispatcher.agents`` anywhere on the line and filter
# out obvious false positives: comments (``#`` or ``#:`` prefixed),
# docstring prose, and JOIN clauses already covered by the same
# method. Only lines whose content begins with a Python string
# quote (double quote after whitespace) qualify as a real SQL site.
target_lines=()
while IFS= read -r line; do
    [[ -n "$line" ]] && target_lines+=("$line")
done < <(grep -nE '(UPDATE dispatcher\.agents|FROM dispatcher\.agents[^_])' "$TARGET_FILE" \
    | grep -vE '^[0-9]+:[[:space:]]*#' \
    | grep -E '^[0-9]+:[[:space:]]*"' \
    | cut -d: -f1 || true)

if [[ ${#target_lines[@]} -eq 0 ]]; then
    echo "check-dispatcher-execution-mode-aware: no UPDATE/SELECT sites in $TARGET_FILE — nothing to check."
    exit 0
fi

# Token lines: any line mentioning ``execution_mode`` or
# ``agent_task_arn`` (anywhere — code, comment, docstring). Also:
# lines matching the ``# exec-mode-agnostic (#<digits>)`` annotation.
# We treat all three as coverage signals.
coverage_lines=()
while IFS= read -r line; do
    [[ -n "$line" ]] && coverage_lines+=("$line")
done < <(grep -nE '(execution_mode|agent_task_arn|exec-mode-agnostic \(#[0-9]+\))' "$TARGET_FILE" \
    | cut -d: -f1 || true)

violations=0
missing=()

for target_line in "${target_lines[@]}"; do
    window_start=$((target_line - EXEC_MODE_WINDOW_LINES))
    (( window_start < 1 )) && window_start=1
    window_end=$((target_line + EXEC_MODE_WINDOW_LINES))

    found=0
    if [[ ${#coverage_lines[@]} -gt 0 ]]; then
        for c in "${coverage_lines[@]}"; do
            if (( c >= window_start && c <= window_end )); then
                found=1
                break
            fi
        done
    fi

    if (( found == 0 )); then
        missing+=("$target_line")
        violations=$((violations + 1))
    fi
done

if (( violations > 0 )); then
    echo "ERROR: dispatcher.agents SQL call site(s) missing nearby execution_mode awareness."
    echo ""
    echo "  Target: $TARGET_FILE"
    echo "  Window: +/- ${EXEC_MODE_WINDOW_LINES} lines around each SQL line."
    echo ""
    echo "  Every UPDATE dispatcher.agents / SELECT ... FROM dispatcher.agents"
    echo "  must have at least one of these within the window:"
    echo ""
    echo "    - the token execution_mode"
    echo "    - the token agent_task_arn"
    echo "    - the comment # exec-mode-agnostic (#N): <reason>"
    echo ""
    echo "  See the #3158 audit for the classification:"
    echo "  docs/investigations/dispatcher-execution-mode-audit-2026-04.md"
    echo ""
    echo "  Sites missing coverage:"
    echo ""
    # Length-guard the iteration — bash 3.2 trips on ``"${arr[@]}"``
    # when arr=() and the conditional ``missing+=`` inside the
    # earlier loop never fired (e.g. all SQL sites are covered, but
    # ``violations`` was incremented by some other path). See #4479.
    if [ "${#missing[@]}" -gt 0 ]; then
        for line in "${missing[@]}"; do
            snippet="$(sed -n "${line}p" "$TARGET_FILE")"
            echo "    $TARGET_FILE:${line}: ${snippet}"
        done
    fi
    echo ""
    echo "  Fix: add an execution_mode branch, OR annotate the site:"
    echo ""
    echo "    # exec-mode-agnostic (#3158): phase-driven terminal writer;"
    echo "    # mode-specific teardown happens in the caller."
    exit 1
fi

echo "check-dispatcher-execution-mode-aware: ${#target_lines[@]} dispatcher.agents SQL site(s) all covered within +/- ${EXEC_MODE_WINDOW_LINES} lines."
exit 0
