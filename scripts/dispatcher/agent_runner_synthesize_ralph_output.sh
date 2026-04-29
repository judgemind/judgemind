#!/usr/bin/env bash
# agent_runner_synthesize_ralph_output.sh — Sourceable helper that
# synthesises a structured `dispatcher-output/ralph.json` from
# `{worktree}/tmp/ralph/ralph-done.txt` when the inner `/task-v2-ralph`
# skill completes its conversation cleanly but never executes Step 4
# (the Write of the dispatcher-output file). Layer 4 of the silent-ralph
# saga (#3782).
#
# Layer recap:
#   * Layer 1 (#3694 / PR #3750) — instrument the silent exit so the
#     failure became self-diagnosing.
#   * Layer 2 (#3757 / PR #3761) — Fargate hook swap; permission denials
#     dropped to zero.
#   * Layer 3 (#3766 / PR #3772) — per-phase claude-p timeout; SIGKILL
#     silent exits dropped to zero.
#   * Layer 4 (this file, #3782) — model finishes the conversation
#     without doing the Step 4 Write. The wrapper-side fallback
#     synthesises the dispatcher-output JSON from the inner
#     `ralph-done.txt` (which IS reliably written — issue #721 SKILL.md
#     §3b makes it CRITICAL). This converts the silent-exit cohort from
#     `ralph_done_marker_missing` → `ralph_not_ship` (BLOCKED) into a
#     proper terminal verdict matching what ralph actually achieved.
#
# Contract — `synthesize_ralph_output_from_done_marker <worktree>`:
#
#   * Reads `{worktree}/tmp/ralph/ralph-done.txt`. The first non-blank
#     line is parsed for the verdict token. The expected SKILL.md format
#     is `status: SHIP|REVISE|BLOCKED|AC_INFEASIBLE` (see
#     `.claude/skills/ralph/SKILL.md` §3b), but we also tolerate a bare
#     verdict on the first line (e.g. `SHIP`) for forward-compat with
#     any future SKILL.md change.
#   * Reads `{worktree}/tmp/ralph/iteration.txt` for `iterations_used`.
#     Falls back to `1` when missing/unreadable.
#   * Runs `git -C <worktree> diff --name-only origin/main...HEAD` to
#     produce `changed_files`. Falls back to `[]` when the diff fails.
#   * Tails `{worktree}/tmp/ralph/feedback.md` (last 500 chars, single
#     line) for `summary`. Falls back to a fixed string naming the
#     synthesis path.
#   * Sets `block_reason` to a fixed Layer 4 marker for non-SHIP
#     verdicts, `null` on SHIP. The marker text contains "Layer 4
#     synthesis" so CloudWatch Insights queries can grep the synthesis
#     cohort.
#   * Writes structured JSON to stdout via `jq -n -c`.
#   * Logs `ralph_output_synthesized_from_done_marker` with the
#     resolved verdict and iteration count via the inherited `log()`
#     function (or the stderr-fallback shim when sourced standalone).
#   * Returns 0 on success, 1 when `ralph-done.txt` is missing (so the
#     caller falls through to the existing `ralph_done_marker_missing`
#     path — there's nothing to synthesize from).
#
# Why a separate file vs. inline in the entrypoint:
#
#   * Sourceable in tests without dragging in the entrypoint's clone /
#     checkout / phase-loop side effects (mirrors the precedent set by
#     `agent_runner_install_fargate_hook.sh`).
#   * Survives bash 3.2 (no associative arrays, no `mapfile`).

# ── log() shim ─────────────────────────────────────────────────────────────
#
# When sourced from `agent-runner-entrypoint.sh`, `log` is already
# defined and writes structured JSON to fd 3. When sourced from a test
# (or directly), provide a minimal substitute that writes to stderr so
# tests can assert on the event names without the synthesised JSON
# (which goes to stdout) being polluted by log noise.

if ! declare -F log >/dev/null 2>&1; then
    log() {
        # $1 = event name, rest = key=value pairs.
        _ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        _event="$1"
        shift
        _extra=""
        for kv in "$@"; do
            _k=${kv%%=*}
            _v=${kv#*=}
            _v=$(printf '%s' "$_v" | sed 's/"/\\"/g')
            _extra="$_extra, \"$_k\": \"$_v\""
        done
        # Stderr (not fd 3) so the helper's stdout stays a clean JSON
        # payload that tests can pipe directly into jq.
        printf '{"ts": "%s", "event": "agent_runner.%s", "agent_id": "%s"%s}\n' \
            "$_ts" "$_event" "${AGENT_ID:-unknown}" "$_extra" >&2
    }
fi

# ── synthesize_ralph_output_from_done_marker ──────────────────────────────

synthesize_ralph_output_from_done_marker() {
    # $1 = worktree absolute path. Required.
    local worktree="${1:-}"

    if [[ -z "$worktree" || ! -d "$worktree" ]]; then
        log "ralph_output_synthesis_skip" \
            "reason=worktree_unset_or_missing" \
            "worktree=$worktree"
        return 1
    fi

    local ralph_done="$worktree/tmp/ralph/ralph-done.txt"
    local iteration_file="$worktree/tmp/ralph/iteration.txt"
    local feedback_file="$worktree/tmp/ralph/feedback.md"

    if [[ ! -f "$ralph_done" ]]; then
        # No done-marker means the inner /ralph genuinely never
        # completed — there's nothing to synthesize from. Fall back to
        # the existing `ralph_done_marker_missing` path (Layer 1
        # instrumentation) so the failure stays visible.
        log "ralph_output_synthesis_skip" \
            "reason=ralph_done_missing" \
            "worktree=$worktree"
        return 1
    fi

    # ── Parse verdict ──────────────────────────────────────────────────────
    #
    # Expected SKILL.md format (`.claude/skills/ralph/SKILL.md` §3b):
    #
    #   status: SHIP
    #   iterations: <N>
    #   next-steps: ...
    #
    # Take the first non-blank line, strip a leading "status:" if
    # present, uppercase, and trim whitespace.

    local first_line
    first_line=$(grep -v '^[[:space:]]*$' "$ralph_done" 2>/dev/null | head -1 || printf '')
    # Strip leading "status:" or "Status:" tokens. Bash `${var#prefix}`
    # is bash-3.2-compatible; we lowercase via tr first to handle either
    # case without a regex.
    local lower_line
    lower_line=$(printf '%s' "$first_line" | tr '[:upper:]' '[:lower:]')
    local verdict_raw="$first_line"
    case "$lower_line" in
        status:*)
            verdict_raw="${first_line#*:}"
            ;;
    esac
    # Trim leading/trailing whitespace + uppercase.
    local verdict
    verdict=$(printf '%s' "$verdict_raw" \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        | tr '[:lower:]' '[:upper:]')

    # Map REVISE → BLOCKED (matches the SKILL.md output-contract table —
    # REVISE in `ralph-done.txt` translates to BLOCKED in the dispatcher
    # output with `block_reason="max_iterations reached without SHIP"`).
    # Anything other than the four canonical verdicts becomes BLOCKED so
    # the daemon's transition graph can route it.
    case "$verdict" in
        SHIP|BLOCKED|AC_INFEASIBLE)
            : ;;
        REVISE)
            verdict="BLOCKED"
            ;;
        *)
            log "ralph_output_synthesis_unknown_verdict" \
                "first_line=$first_line"
            verdict="BLOCKED"
            ;;
    esac

    # ── Parse iterations_used ─────────────────────────────────────────────

    local iterations_used=1
    if [[ -f "$iteration_file" ]]; then
        local raw_iter
        raw_iter=$(head -1 "$iteration_file" 2>/dev/null \
            | tr -dc '0-9' || printf '')
        if [[ -n "$raw_iter" ]]; then
            iterations_used="$raw_iter"
        fi
    fi
    # Defensive: zero or negative becomes 1; the SKILL.md contract says
    # iterations_used is `1..max_iterations` for any non-Step-0-blocked
    # path.
    if [[ "$iterations_used" -lt 1 ]]; then
        iterations_used=1
    fi

    # ── Compute changed_files ─────────────────────────────────────────────
    #
    # `git -C <worktree> diff --name-only origin/main...HEAD` produces
    # one path per line. Best-effort: a missing origin/main ref or a
    # detached worktree falls back to an empty list. The helper does
    # not abort on diff failure.

    local changed_files_raw
    if ! changed_files_raw=$(git -C "$worktree" diff --name-only origin/main...HEAD 2>/dev/null); then
        changed_files_raw=""
    fi

    # Build a JSON array via jq. Empty input → `[]`.
    local changed_files_json
    if [[ -z "$changed_files_raw" ]]; then
        changed_files_json='[]'
    else
        # `jq -R -s` slurps the entire input as one string, then
        # `split("\n")` + `map(select(length > 0))` discards trailing
        # blank lines. Bash 3.2 compatible — no readarray.
        changed_files_json=$(printf '%s' "$changed_files_raw" \
            | jq -R -s 'split("\n") | map(select(length > 0))' 2>/dev/null \
            || printf '[]')
    fi

    # ── Compose summary ───────────────────────────────────────────────────
    #
    # Tail the last 500 chars of feedback.md, collapse newlines to
    # spaces, and prepend a synthesis-path marker so operators can
    # quickly distinguish a synthesised summary from a real one written
    # by the ralph skill.

    local synth_marker="Layer 4 synthesis (Step 4 not executed; synthesised from ralph-done.txt)."
    local summary
    if [[ -f "$feedback_file" && -s "$feedback_file" ]]; then
        local feedback_tail
        feedback_tail=$(tail -c 500 "$feedback_file" 2>/dev/null \
            | tr '\n\r\t' '   ' \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
            || printf '')
        if [[ -n "$feedback_tail" ]]; then
            summary="$synth_marker $feedback_tail"
        else
            summary="$synth_marker"
        fi
    else
        summary="$synth_marker"
    fi

    # ── Compose block_reason ──────────────────────────────────────────────

    local block_reason_arg
    case "$verdict" in
        SHIP)
            block_reason_arg=""  # signal to jq to emit null
            ;;
        AC_INFEASIBLE)
            # Daemon reads infeasible_acs separately for AC_INFEASIBLE;
            # block_reason stays null per the SKILL.md output contract
            # (block_reason is null on AC_INFEASIBLE; populate
            # infeasible_acs instead).
            block_reason_arg=""
            ;;
        *)
            block_reason_arg="Layer 4 synthesis: model completed conversation without Step 4 Write"
            ;;
    esac

    # ── Build the JSON ────────────────────────────────────────────────────
    #
    # `jq -n -c` builds the JSON safely with typed string variables —
    # no shell interpolation into the structured output. The
    # `block_reason` arg is empty when the verdict is SHIP/AC_INFEASIBLE;
    # we use `(. // null | if . == "" then null else . end)` to coerce
    # an empty string to JSON null.

    local output
    output=$(jq -n -c \
        --arg verdict "$verdict" \
        --argjson iterations_used "$iterations_used" \
        --argjson changed_files "$changed_files_json" \
        --arg summary "$summary" \
        --arg block_reason "$block_reason_arg" \
        '{
            verdict: $verdict,
            iterations_used: $iterations_used,
            block_reason: (if $block_reason == "" then null else $block_reason end),
            changed_files: $changed_files,
            summary: $summary,
            infeasible_acs: [],
            ralph_output_synthesized_from_done_marker: true
        }' 2>/dev/null) || output=""

    if [[ -z "$output" ]]; then
        # jq build failed — fall back to a minimal object so the caller
        # still gets a valid verdict. The `ralph_output_synthesized_from_done_marker`
        # field is the load-bearing signal that triggers the daemon's
        # downstream pathways.
        output='{"verdict":"BLOCKED","iterations_used":1,"block_reason":"Layer 4 synthesis: jq build failed","changed_files":[],"summary":"Layer 4 synthesis fallback","infeasible_acs":[],"ralph_output_synthesized_from_done_marker":true}'
        log "ralph_output_synthesis_jq_failed" \
            "verdict=$verdict"
    fi

    log "ralph_output_synthesized_from_done_marker" \
        "verdict=$verdict" \
        "iterations_used=$iterations_used" \
        "changed_files_count=$(printf '%s' "$changed_files_json" | jq -r 'length' 2>/dev/null || printf '0')"

    printf '%s' "$output"
    return 0
}
