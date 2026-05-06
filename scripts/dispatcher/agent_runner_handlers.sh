#!/usr/bin/env bash
# agent_runner_handlers.sh — Sourceable per-phase case-arm handlers
# extracted from `agent-runner-entrypoint.sh`'s `phase_loop()` (#4138).
#
# Defines, in roughly the order the case-statement arms appeared inline:
#
#   handle_planning
#   handle_ralph
#   handle_summary
#   handle_verify
#   handle_claiming
#   handle_push_and_pr_arm
#   handle_fix_conflict_arm
#   handle_fix_ci_arm
#   handle_operational
#   handle_awaiting_ci_arm
#   handle_merge_arm
#   handle_awaiting_deploy_arm
#   handle_retro_or_setup
#   handle_unknown_phase
#
# Each handler owns the case-arm's pre/post bookkeeping
# (``persist_phase_output``, ``transition_for``,
# ``dispatch_transition_action``, ``advance_phase``,
# ``agent_runner_reaped_failure``) for a single phase. The existing
# inner side-effect handlers (``handle_push_and_pr``,
# ``handle_fix_conflict``, ``handle_fix_ci``, ``handle_awaiting_ci``,
# ``handle_merge``, ``handle_awaiting_deploy``, ``handle_scheduled_skill``)
# keep their names and contracts; only the *case-statement-arm* bodies
# move here.
#
# All handlers are bash 3.2 compatible (no associative arrays, no
# ``mapfile``).
#
# # ralph baseline-rebase ``continue`` → handler return
#
# The original case body used ``continue`` after the baseline-rebase
# block to skip the rest of the ralph arm and let the loop re-tick. Inside
# a function, ``continue`` does not propagate to the calling loop.
# ``handle_ralph`` instead returns 0 after dispatching the baseline
# transition: the dispatched action has already advanced the agent's
# phase row, so when ``phase_loop`` ticks again it observes the new
# phase and routes accordingly — exact same control flow as the prior
# ``continue``.
#
# Why a separate file vs. inline in the entrypoint:
#
#   * Mirrors the precedent set by ``agent_runner_run_claude_phase.sh``
#     (#3775).
#   * Keeps ``phase_loop`` a thin dispatcher (one function call per
#     phase) so reviewers can read the loop control logic without
#     scrolling through 600 lines of per-phase bookkeeping.
#   * Sub-task C (#4136 / planned) will convert the test from
#     ``bash $ENTRYPOINT`` invocations to direct function calls against
#     the sourced handlers — extraction is the prerequisite.

# ── log() / die() shims ────────────────────────────────────────────────────
#
# When sourced from ``agent-runner-entrypoint.sh``, ``log`` and ``die``
# are already defined and behave correctly. When sourced directly (from
# a test or for ad-hoc inspection), provide minimal substitutes so
# callers don't crash. Same pattern as
# ``agent_runner_run_claude_phase.sh``.

if ! declare -F log >/dev/null 2>&1; then
    log() {
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
        printf '{"ts": "%s", "event": "agent_runner.%s", "agent_id": "%s"%s}\n' \
            "$_ts" "$_event" "${AGENT_ID:-unknown}" "$_extra" >&2
    }
fi

if ! declare -F die >/dev/null 2>&1; then
    die() {
        printf '%s\n' "$*" >&2
        exit 1
    }
fi

# ── Shared helper — claude-driven skill phase (planning/summary/verify) ────
#
# planning, summary, and verify share the exact arm body inherited from
# the original ``planning|ralph|summary|verify)`` case: invoke
# ``run_claude_phase``, persist the output, compute the transition, and
# dispatch via ``dispatch_transition_action``. ``handle_ralph`` does NOT
# call this helper because ralph layers a baseline-rebase prelude, a
# HEAD-watcher subshell, a Layer 4 silent-exit fallback, and a
# ``ralph_patches`` persist on top of the same skeleton.
_handle_claude_skill_phase_arm() {
    _phase="$1"
    _output=$(run_claude_phase "$_phase")
    persist_phase_output "$_phase" "$_output"
    _transition=$(transition_for "$_phase" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    # #3581 — single-source-of-truth dispatch. The helper handles every
    # TransitionAction enum value AND every FAILURE_HINT_* constant
    # uniformly across all phases. CI guard
    # scripts/check-transition-dispatch-vocabulary.sh asserts the
    # helper's vocabulary stays in sync with the Python module.
    dispatch_transition_action "$_phase" "$_action" "$_next" "$_status" "$_hint" "$_output"
}

# ── Claude-driven skill phases ─────────────────────────────────────────────

handle_planning() {
    _handle_claude_skill_phase_arm "planning"
}

handle_summary() {
    _handle_claude_skill_phase_arm "summary"
}

handle_verify() {
    _handle_claude_skill_phase_arm "verify"
}

# ── ralph (claude-driven, plus baseline-rebase + watcher + layer-4) ────────

handle_ralph() {
    # #3225 secondary mitigation — run a baseline
    # ``git fetch origin main && git rebase origin/main`` at
    # the START of ralph (before any claude iterations). This
    # cuts the conflict surface by the duration of ralph
    # (~20-25 min) so the agent works against the latest main
    # from the beginning. On conflict, short-circuit the
    # claude invocation and emit the same rebase_failed
    # envelope that push_and_pr emits — transition_from_ralph
    # routes to fix_conflict.
    if [[ "${AGENT_RUNNER_RALPH_BASELINE_REBASE:-1}" == "1" ]] && \
       [[ "$AGENT_RUNNER_DRY_RUN" != "1" ]]; then
        log "ralph_baseline_rebase_begin"
        set +e
        git -C "$REPO_ROOT" fetch origin main \
            > "$AGENT_WORKSPACE/ralph-baseline-fetch.stdout.log" \
            2> "$AGENT_WORKSPACE/ralph-baseline-fetch.stderr.log"
        _baseline_fetch_rc=$?
        set -e
        log "ralph_baseline_fetch_done" "exit_code=$_baseline_fetch_rc"
        if [[ "$_baseline_fetch_rc" -eq 0 ]]; then
            set +e
            git -C "$REPO_ROOT" rebase origin/main \
                > "$AGENT_WORKSPACE/ralph-baseline-rebase.stdout.log" \
                2> "$AGENT_WORKSPACE/ralph-baseline-rebase.stderr.log"
            _baseline_rebase_rc=$?
            set -e
            log "ralph_baseline_rebase_done" "exit_code=$_baseline_rebase_rc"
            if [[ "$_baseline_rebase_rc" -ne 0 ]]; then
                # Capture conflict files, stash markers, abort,
                # emit rebase_failed envelope. Same shape as
                # push_and_pr's conflict path so the
                # fix_conflict phase can consume either.
                _fix_conflict_stage="$AGENT_WORKSPACE/fix-conflict"
                mkdir -p "$_fix_conflict_stage/conflict-markers"
                set +e
                git -C "$REPO_ROOT" diff --name-only --diff-filter=U \
                    > "$_fix_conflict_stage/conflict-files.txt" \
                    2> "$AGENT_WORKSPACE/git-diff-conflict-files.stderr.log"
                set -e
                while IFS= read -r _cfile; do
                    if [[ -z "$_cfile" ]]; then
                        continue
                    fi
                    _safe=$(printf '%s' "$_cfile" | tr '/' '__')
                    if [[ -f "$REPO_ROOT/$_cfile" ]]; then
                        cp "$REPO_ROOT/$_cfile" \
                            "$_fix_conflict_stage/conflict-markers/$_safe" \
                            2>/dev/null || true
                    fi
                done < "$_fix_conflict_stage/conflict-files.txt"
                set +e
                git -C "$REPO_ROOT" rev-parse ORIG_HEAD \
                    > "$_fix_conflict_stage/orig-head.txt" 2>/dev/null || true
                git -C "$REPO_ROOT" merge-base ORIG_HEAD origin/main \
                    > "$_fix_conflict_stage/merge-base.txt" 2>/dev/null || true
                git -C "$REPO_ROOT" rebase --abort \
                    > "$AGENT_WORKSPACE/ralph-baseline-rebase-abort.stdout.log" \
                    2> "$AGENT_WORKSPACE/ralph-baseline-rebase-abort.stderr.log"
                set -e
                _baseline_conflict_files_json="[]"
                if [[ -s "$_fix_conflict_stage/conflict-files.txt" ]]; then
                    _baseline_conflict_files_json=$(jq -R -s -c \
                        'split("\n") | map(select(length > 0))' \
                        "$_fix_conflict_stage/conflict-files.txt" 2>/dev/null \
                        || printf '[]')
                fi
                # #3465: capture the last ~50 lines of
                # ralph-baseline-rebase.stderr.log (size-capped at
                # ~5 KB) so the diagnoser can inspect the rebase
                # failure reason when no unmerged files are present.
                _baseline_rebase_stderr_tail=$(tail -n 50 \
                    "$AGENT_WORKSPACE/ralph-baseline-rebase.stderr.log" \
                    2>/dev/null \
                    | head -c 5120 \
                    | jq -Rs '.' 2>/dev/null \
                    || printf '""')
                log "ralph_baseline_rebase_conflict" \
                    "exit_code=$_baseline_rebase_rc" \
                    "conflict_files_json=$_baseline_conflict_files_json"
                # #3651: post-rebase empty-diff guard — direct
                # sibling of #3614/PR #3645's fix in
                # ``handle_push_and_pr``. When the start-of-ralph
                # baseline rebase fails AND the conflict-files
                # list is empty AND the post-abort ahead-count
                # collapses to 0, the agent's commits were
                # already in main (typically because a sibling
                # PR landed the same fix first, or the daemon
                # is retrying an already-fixed issue). Pre-#3651
                # this fell through to the no_unmerged_files
                # envelope, which transition_from_ralph routed
                # to the diagnoser as
                # ``push_and_pr_no_unmerged_files`` — terminal-
                # failing the agent on what is actually a benign
                # success (the cluster of stuck issues #2777,
                # #2832, #2854, #3297, #3407, #3574, #3581,
                # tripping the circuit breaker repeatedly).
                #
                # The fix: if the conflict-files list is empty
                # AND the rebase --abort returned HEAD to a
                # commit already in origin/main (ahead-count 0),
                # emit the existing ``{"no_op": true}`` envelope.
                # ``transition_from_ralph`` (#3651) routes that
                # to PHASE_NO_OP terminal succeeded — exactly
                # the right outcome for "fix is already in
                # main." The new log event
                # ``ralph_baseline_no_unmerged_files_already_applied``
                # distinguishes this advance event from the
                # pre-existing
                # ``ralph_baseline_route_to_diagnoser`` (which
                # still fires when the rebase actually failed
                # for a non-already-applied reason — same code
                # path as #3465). Mirrors PR #3645's
                # ``push_and_pr_no_unmerged_files_already_applied``.
                _ralph_baseline_output=""
                if [[ "$_baseline_conflict_files_json" == "[]" ]]; then
                    # #3675: stronger semantic check than the
                    # pre-#3675 ``rev-list --count`` check (see
                    # the matching commentary in
                    # ``handle_push_and_pr``). ``rev-list``
                    # counts commit objects, not the diff that
                    # would actually ship — ORIG_HEAD's
                    # commits are still distinct objects even
                    # when they became semantically redundant
                    # with main during the rebase (which is
                    # why rebase produced empty commits and
                    # exited 128). ``git diff --quiet``
                    # answers the actual question: "is there
                    # any change to ship?" Exit 0 = no diff =
                    # already in main = emit no_op.
                    if _post_rebase_no_diff_to_main; then
                        log "ralph_baseline_no_unmerged_files_already_applied" \
                            "reason=rebase_failed_but_diff_to_main_is_empty"
                        _ralph_baseline_output=$(printf '{"no_op": true, "rebase_dropped_all_commits": true, "diff_to_main_empty": true, "rebase_stderr_tail": %s, "source_phase": "ralph"}' \
                            "$_baseline_rebase_stderr_tail")
                    else
                        # #3465 path: rebase actually failed for
                        # a non-already-applied reason (corrupt
                        # state, fetch issue, etc.) — route to
                        # the diagnoser as before.
                        _ralph_baseline_output=$(printf '{"rebase_failed": true, "no_unmerged_files": true, "rebase_stderr_tail": %s, "source_phase": "ralph"}' \
                            "$_baseline_rebase_stderr_tail")
                    fi
                else
                    # #3225 path: real rebase conflict — route
                    # to fix_conflict with the file bundle.
                    _ralph_baseline_output=$(printf '{"rebase_failed": true, "conflict_files": %s, "rebase_stderr_tail": %s, "source_phase": "ralph"}' \
                        "$_baseline_conflict_files_json" "$_baseline_rebase_stderr_tail")
                fi
                persist_phase_output "ralph" "$_ralph_baseline_output"
                _bt=$(transition_for "ralph" "$_ralph_baseline_output")
                _ba=$(printf '%s' "$_bt" | cut -f1)
                _bn=$(printf '%s' "$_bt" | cut -f2)
                _bs=$(printf '%s' "$_bt" | cut -f3)
                _bh=$(printf '%s' "$_bt" | cut -f4)
                # #3581 — single-source-of-truth dispatch via
                # the helper. See dispatch_transition_action
                # above advance_phase. Replaces the per-phase
                # case-statement that landed in #3573 (ralph
                # baseline route_to_diagnoser arm).
                dispatch_transition_action "ralph_baseline" "$_ba" "$_bn" "$_bs" "$_bh" "$_ralph_baseline_output"
                # #4138: original pre-extraction code used
                # ``continue`` here to skip the rest of the
                # arm and re-tick the loop. Returning from this
                # function has the same effect — the agent's
                # phase has been advanced (or terminal-failed)
                # by ``dispatch_transition_action`` and the
                # next ``phase_loop`` tick reads the new phase
                # row and routes accordingly.
                return 0
            fi
        else
            log "ralph_baseline_fetch_failed_skipping" \
                "exit_code=$_baseline_fetch_rc"
        fi
    fi
    # #3144: start the HEAD-watcher subshell so the long
    # ralph phase emits per-iteration observability to
    # CloudWatch + dispatcher.ralph_patches. The watcher is
    # stopped unconditionally after run_claude_phase returns
    # (success or failure) so the subshell never outlives
    # the phase it instruments.
    start_ralph_head_watcher
    _output=$(run_claude_phase "ralph")
    stop_ralph_head_watcher
    # #3694 — ralph silent-exit instrumentation. The original
    # symptom: three consecutive ECS agents on three different
    # issues exited with ``output_json={}`` and ``stderr_tail=""``,
    # producing zero diagnostic data. The worker subprocess
    # terminated cleanly without writing a verdict marker, so the
    # daemon classified the failure as ``ralph_not_ship`` with no
    # block_reason.
    #
    # Layer 1 (this commit) makes the failure self-diagnosing —
    # capture the worker's stdout/stderr tails into
    # ``phase_outputs.log_text``, emit a structured
    # ``ralph_done_marker_missing`` event, and replace the empty
    # ``output_json`` with a payload carrying the tails as
    # ``block_reason``. The daemon's ``_collect_failure_details``
    # already forwards ``block_reason`` → ``failures.details
    # .stderr_tail`` (see daemon.py ~12420), so the next
    # occurrence will surface a populated stderr_tail in the
    # diagnoser's verbatim-quote pull instead of the current
    # empty string. Layer 2 (the actual fix to whatever is
    # making the worker exit silently) lands once the new logs
    # capture self-diagnosing data.
    _ralph_log_tail_file=""
    _ralph_stdout_file="$AGENT_WORKSPACE/claude-p-ralph.stdout.json"
    _ralph_stderr_file="$AGENT_WORKSPACE/claude-p-ralph.stderr.log"
    # Build a merged tail file (last 2000 bytes of stdout +
    # last 2000 bytes of stderr) for log_text persistence.
    # Unconditional capture — even SHIP-path runs benefit
    # from having the worker narrative in the DB for
    # post-mortem when CI / verify later reveals a problem.
    _ralph_log_tail_file="$AGENT_WORKSPACE/claude-p-ralph.log_text.txt"
    {
        printf '=== claude-p-ralph.stdout.json (last 2000 bytes) ===\n'
        if [[ -s "$_ralph_stdout_file" ]]; then
            tail -c 2000 "$_ralph_stdout_file" 2>/dev/null
        fi
        printf '\n=== claude-p-ralph.stderr.log (last 2000 bytes) ===\n'
        if [[ -s "$_ralph_stderr_file" ]]; then
            tail -c 2000 "$_ralph_stderr_file" 2>/dev/null
        fi
        printf '\n'
    } > "$_ralph_log_tail_file" 2>/dev/null || true
    # Detect the silent-exit shape — ralph returned ``{}`` (a
    # string-equal check is sufficient because run_claude_phase
    # only emits ``{}`` for the non-object .result fallback;
    # any legitimate ralph output, even an empty BLOCKED, is at
    # least ``{"verdict": "..."}``).
    if [[ "$_output" == "{}" ]]; then
        # Layer 4 silent-ralph fallback (#3782). Before
        # falling through to the ``ralph_done_marker_missing``
        # path, check whether the inner ``/task-v2-ralph``
        # skill DID write its inner ``ralph-done.txt`` (Step
        # 3b of the inner SKILL.md is marked CRITICAL — and
        # is reliably written) but the outer wrapper's Step
        # 4 Write of ``dispatcher-output/ralph.json`` was
        # skipped (the model finished its conversation
        # without doing the final tool call). Synthesize the
        # dispatcher-output JSON from the inner state files.
        # On success, ``_output`` carries a structured verdict
        # matching what ralph actually achieved; on failure
        # (ralph-done.txt missing — the genuinely-silent
        # case), the helper returns non-zero and we fall
        # through to the original Layer 1 instrumentation.
        _layer4_synth=""
        if _layer4_synth=$(synthesize_ralph_output_from_done_marker "$REPO_ROOT" 2>>"$AGENT_WORKSPACE/claude-p-ralph.stderr.log"); then
            if [[ -n "$_layer4_synth" ]]; then
                _output="$_layer4_synth"
            fi
        fi
    fi
    # If the Layer 4 synthesis didn't produce a verdict, fall
    # through to Layer 1 instrumentation. Re-check
    # ``_output == "{}"`` so a successful synthesis above
    # short-circuits this block entirely.
    if [[ "$_output" == "{}" ]]; then
        _ralph_stdout_tail=""
        _ralph_stderr_tail=""
        if [[ -s "$_ralph_stdout_file" ]]; then
            _ralph_stdout_tail=$(tail -c 500 "$_ralph_stdout_file" 2>/dev/null \
                | tr '\n\r\t' '   ' | sed -e 's/\\/\\\\/g')
        fi
        if [[ -s "$_ralph_stderr_file" ]]; then
            _ralph_stderr_tail=$(tail -c 500 "$_ralph_stderr_file" 2>/dev/null \
                | tr '\n\r\t' '   ' | sed -e 's/\\/\\\\/g')
        fi
        # ``run_claude_phase`` already captured the worker's
        # exit code into the ``claude_phase_done`` log line;
        # we don't have it in scope here directly, so emit a
        # placeholder. The stdout/stderr tails are the
        # high-value signal (they contain the actual error
        # lines if the worker printed any), and a future
        # iteration can plumb the rc through if needed.
        log "ralph_done_marker_missing" \
            "phase=ralph" \
            "worker_exit_code=unknown" \
            "worker_stdout_tail=$_ralph_stdout_tail" \
            "worker_stderr_tail=$_ralph_stderr_tail"
        # Replace the empty ``{}`` with a structured payload
        # so the daemon's failures.details.stderr_tail (which
        # mirrors block_reason for the ralph_not_ship terminal,
        # see daemon.py ~12420) carries the diagnostic tails
        # rather than "". jq -n -c builds the JSON safely with
        # the tails as typed string variables — no shell
        # interpolation into the SQL.
        _output=$(jq -n -c \
            --arg stdout_tail "$_ralph_stdout_tail" \
            --arg stderr_tail "$_ralph_stderr_tail" \
            '{verdict: "BLOCKED",
              category: "ralph_not_ship",
              block_reason: ("ralph_done_marker_missing: worker exited 0 with no done-marker. " +
                             "stdout_tail=" + $stdout_tail + " stderr_tail=" + $stderr_tail),
              worker_stdout_tail: $stdout_tail,
              worker_stderr_tail: $stderr_tail,
              ralph_done_marker_missing: true}' 2>/dev/null \
            || printf '{"category":"ralph_not_ship","block_reason":"ralph_done_marker_missing: jq build failed","ralph_done_marker_missing":true}')
    fi
    persist_phase_output "ralph" "$_output" "$_ralph_log_tail_file"
    # Mirror the daemon's post-SHIP ralph_patches persist.
    _verdict=$(printf '%s' "$_output" | jq -r '.verdict // ""')
    if [[ "$_verdict" == "SHIP" ]]; then
        persist_ralph_patch
    fi
    _transition=$(transition_for "ralph" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    # #3581 — single-source-of-truth dispatch. The helper
    # (defined alongside advance_phase) handles every
    # TransitionAction enum value AND every FAILURE_HINT_*
    # constant emitted by phase_transitions.py uniformly across
    # all phases. CI guard scripts/check-transition-dispatch-vocabulary.sh
    # asserts the helper's vocabulary stays in sync with the
    # Python module.
    dispatch_transition_action "ralph" "$_action" "$_next" "$_status" "$_hint" "$_output"
}

# ── Mechanical phases ──────────────────────────────────────────────────────

handle_claiming() {
    # Mechanical pseudo-phase (#3117): the daemon writes
    # phase='claiming' only briefly while it inserts the
    # dispatcher.agents row; the claim itself is a pure DB
    # write, not an LLM pass. Any ECS task whose phase column
    # reads `claiming` at boot is a post-claim lifecycle
    # artifact — the claim already happened before the task
    # was launched. Advance immediately to `planning` so the
    # phase loop starts the real work without shelling out to
    # a nonexistent `/task-v2-claiming` skill.
    log "claiming_no_op_advance_to_planning"
    persist_phase_output "claiming" '{"no_op": true}'
    advance_phase "planning"
}

handle_push_and_pr_arm() {
    # Mechanical phase (#3117). See handle_push_and_pr for the
    # git push + gh pr create implementation. On success the
    # transition shim advances to awaiting_ci (or to no_op
    # terminal if the worktree was clean at ralph SHIP time).
    _output=$(handle_push_and_pr)
    persist_phase_output "push_and_pr" "$_output"
    _transition=$(transition_for "push_and_pr" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    # #3581 — single-source-of-truth dispatch via the helper.
    # See dispatch_transition_action above advance_phase for
    # the action-vocabulary handling. Replaces the per-phase
    # case-statement that landed in #3543 (route_to_diagnoser
    # arm) and #3558 (push_and_pr_no_unmerged_files hint).
    dispatch_transition_action "push_and_pr" "$_action" "$_next" "$_status" "$_hint" "$_output"
}

handle_fix_conflict_arm() {
    # #3225: fix_conflict phase. Budget-gated claude-resolution
    # of rebase conflicts. handle_fix_conflict enforces the
    # per-agent FIX_CONFLICT_MAX_ATTEMPTS budget, invokes the
    # claude skill, applies resolved_files as a new commit on
    # verdict=resolved, and emits the output envelope the
    # transition shim uses to advance.
    _output=$(handle_fix_conflict)
    _fc_verdict=$(printf '%s' "$_output" | jq -r '.verdict // ""' 2>/dev/null || printf '')
    log "fix_conflict_handler_done" "verdict=$_fc_verdict"
    persist_phase_output "fix_conflict" "$_output"
    log "fix_conflict_persist_done"
    _transition=$(transition_for "fix_conflict" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    log "fix_conflict_transition_shim_done" \
        "action=$_action" \
        "next=$_next" \
        "status=$_status" \
        "hint=$_hint"
    log "fix_conflict_dispatched_action" "action=$_action"
    # #3581 — single-source-of-truth dispatch via the helper.
    # See dispatch_transition_action above advance_phase.
    dispatch_transition_action "fix_conflict" "$_action" "$_next" "$_status" "$_hint" "$_output"
}

handle_fix_ci_arm() {
    # #3245: fix_ci phase. Split out of the generic Claude-phase
    # case because — unlike planning/ralph/summary/verify — the
    # /task-v2-fix-ci skill explicitly defers git ops to "the
    # daemon" (SKILL.md lines 64 + 160). Before #3245 the ECS
    # entrypoint ran fix_ci through the generic arm and never
    # staged / committed / pushed the skill's patch; every agent
    # with initially-red CI looped 40 iterations without adding
    # a single commit. handle_fix_ci mirrors the subprocess
    # daemon's ``_run_fix_ci`` + ``_apply_fix_ci_patch`` (daemon.py
    # ~line 12697 / 12979): PATCHED → stage + commit + push +
    # back to awaiting_ci; FLAKY → back to awaiting_ci without
    # commit; BLOCKED / unrecognized → fix_ci_failed terminal.
    # handle_fix_ci owns its own advance_phase /
    # agent_runner_reaped_failure calls so the dispatch case
    # just invokes the handler and lets the next loop tick
    # observe the new phase row.
    handle_fix_ci >/dev/null
}

handle_operational() {
    # #3507: operational phase. Runs the /task-v2-operational
    # skill for tasks that need only a script run / DB query / gh
    # action — no code change, no PR. Modeled on the verify)
    # arm: run_claude_phase + persist_phase_output +
    # transition_for dispatch.
    #
    # Verdicts from /task-v2-operational:
    #   succeeded → advance_with_status → operational_done/succeeded
    #   blocked   → advance_with_status → operational_failed/needs_review
    #   failed / missing / unrecognized → route_to_diagnoser
    #     → agent_runner_reaped_failure "operational_failed"
    _output=$(run_claude_phase "operational")
    persist_phase_output "operational" "$_output"
    _transition=$(transition_for "operational" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    log "operational_transition_shim_done" \
        "action=$_action" \
        "next=$_next" \
        "status=$_status" \
        "hint=$_hint"
    # #3581 — single-source-of-truth dispatch via the helper.
    # See dispatch_transition_action above advance_phase.
    dispatch_transition_action "operational" "$_action" "$_next" "$_status" "$_hint" "$_output"
}

handle_awaiting_ci_arm() {
    # #3176: real implementation — poll ``gh pr view`` rollup,
    # advance to ``merge`` on green, ``fix_ci`` on red, exit
    # with terminal failure on timeout.
    _output=$(handle_awaiting_ci)
    persist_phase_output "awaiting_ci" "$_output"
    _rollup_state=$(printf '%s' "$_output" | jq -r '.rollup_state // ""' 2>/dev/null)
    _timeout=$(printf '%s' "$_output" | jq -r '.timeout // false' 2>/dev/null)
    if [[ "$_timeout" == "true" ]]; then
        # agent_runner_reaped_failure already set the terminal
        # phase + status in the DB; next tick will observe it
        # as terminal and exit.
        :
    elif [[ "$_rollup_state" == "green" ]]; then
        advance_phase "merge"
    elif [[ "$_rollup_state" == "red" ]]; then
        advance_phase "fix_ci"
    else
        # No recognizable state — shouldn't happen, but don't
        # spin. Treat as failure to surface the bug.
        log "awaiting_ci_unrecognized_output" "output=$_output"
        agent_runner_reaped_failure \
            "awaiting_ci_failed" \
            "unrecognized_output" \
            "$_output"
    fi
}

handle_merge_arm() {
    # #3176: real implementation — squash-merge with
    # branch-delete, auto-unstick on stale-rollup (one-shot),
    # terminal failure on other non-zero exits.
    _output=$(handle_merge)
    persist_phase_output "merge" "$_output"
    _merged=$(printf '%s' "$_output" | jq -r '.merged // false' 2>/dev/null)
    _auto_unstick=$(printf '%s' "$_output" | jq -r '.auto_unstick_retry // false' 2>/dev/null)
    _merge_failed=$(printf '%s' "$_output" | jq -r '.merge_failed // false' 2>/dev/null)
    if [[ "$_merged" == "true" ]]; then
        # Mirror daemon's ``_merge_pr_and_advance``: flip
        # status=succeeded the moment the squash-merge lands,
        # advance phase to awaiting_deploy. A crash mid-write
        # still leaves a ``status='succeeded' phase=merge``
        # row recoverable by the next tick.
        advance_phase "awaiting_deploy" "succeeded"
    elif [[ "$_auto_unstick" == "true" ]]; then
        # Go back to awaiting_ci so the next poll sees the
        # post-empty-commit rollup.
        advance_phase "awaiting_ci"
    elif [[ "$_merge_failed" == "true" ]]; then
        # agent_runner_reaped_failure already set terminal state.
        :
    else
        log "merge_unrecognized_output" "output=$_output"
        agent_runner_reaped_failure \
            "merge_failed" \
            "unrecognized_output" \
            "$_output"
    fi
}

handle_awaiting_deploy_arm() {
    # #3176: real implementation — poll deploy workflows on
    # the merge SHA, advance to verify on success / no-run
    # grace, terminal failure on timeout / any deploy
    # workflow failure.
    _output=$(handle_awaiting_deploy)
    persist_phase_output "awaiting_deploy" "$_output"
    _deploy_state=$(printf '%s' "$_output" | jq -r '.deploy_state // ""' 2>/dev/null)
    _timeout=$(printf '%s' "$_output" | jq -r '.timeout // false' 2>/dev/null)
    if [[ "$_timeout" == "true" ]]; then
        :
    elif [[ "$_deploy_state" == "success" || "$_deploy_state" == "none" ]]; then
        advance_phase "verify"
    elif [[ "$_deploy_state" == "failure" ]]; then
        :
    else
        log "awaiting_deploy_unrecognized_output" "output=$_output"
        agent_runner_reaped_failure \
            "awaiting_deploy_failed" \
            "unrecognized_output" \
            "$_output"
    fi
}

handle_retro_or_setup() {
    # Remaining mechanical stubs — Stage 3+ will wire these to
    # real implementations (retro posts the issue comment +
    # closes the loop; setup is currently unreachable under the
    # Stage 1b happy path).
    _current="$1"
    _next=""
    case "$_current" in
        setup)            _next="ralph" ;;
        retro)            _next="retro_done" ;;
    esac
    log "mechanical_phase_stub" "phase=$_current" "next=$_next"
    persist_phase_output "$_current" '{"stub": true}'
    advance_phase "$_next"
}

handle_unknown_phase() {
    # Issue #3455 — descriptive terminal instead of
    # ``agent_runner_route_stub``. The catch-all fires when
    # the phase column reads a value the dispatch loop
    # doesn't recognize — that's a code-drift bug (a new
    # phase landed in dispatcher.agents without being
    # wired here), not a semantic failure, so no diagnoser
    # routing.
    _current="$1"
    log "phase_unknown" "phase=$_current"
    agent_runner_reaped_failure \
        "phase_unknown" \
        "phase_unknown" \
        "dispatch loop saw unknown phase=$_current (not wired in agent-runner-entrypoint.sh)"
}
