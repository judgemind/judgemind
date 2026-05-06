#!/usr/bin/env bash
# agent_runner_run_claude_phase.sh — Sourceable helper that provides the
# per-phase claude invocation machinery extracted from
# `agent-runner-entrypoint.sh` (#3775).
#
# Defines:
#   write_phase_input          — build the input JSON for a phase
#   read_phase_output          — read the skill's structured output file
#   phase_to_skill             — map phase-column name → skill suffix
#   run_claude_phase           — invoke claude -p with per-phase timeout
#   claude_phase_timeout_seconds_by_phase — look up per-phase cap
#
# Plus the per-phase timeout constants block.
#
# Why a separate file vs. inline in the entrypoint:
#
#   * Sourceable in tests without dragging in the entrypoint's clone /
#     checkout / phase-loop side effects (mirrors the precedent set by
#     `agent_runner_install_fargate_hook.sh` and
#     `agent_runner_synthesize_ralph_output.sh`).
#   * Survives bash 3.2 (no associative arrays, no `mapfile`).

# ── log() shim ─────────────────────────────────────────────────────────────
#
# When sourced from `agent-runner-entrypoint.sh`, `log` is already
# defined and writes structured JSON to fd 3. When sourced from a test
# (or directly), provide a minimal substitute that writes to stderr so
# tests can assert on the event names without the JSON payload (which
# goes to stdout) being polluted by log noise.

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

# ── die() shim ──────────────────────────────────────────────────────────────
#
# When sourced from `agent-runner-entrypoint.sh`, `die` is already
# defined as the entrypoint's fatal-error helper. When sourced from a
# test (or directly), provide a minimal substitute so callers don't crash.

if ! declare -F die >/dev/null 2>&1; then
    die() {
        printf '%s\n' "$*" >&2
        exit 1
    }
fi

# ── _resolve_timeout_cmd() — portable timeout(1) lookup (#4125) ────────────
#
# GNU coreutils ships ``timeout(1)`` on Linux. macOS BSD does NOT ship a
# ``timeout`` binary out of the box. Operators with ``brew install coreutils``
# get ``gtimeout`` (the ``g``-prefixed coreutils alias). The helper's
# ``run_claude_phase`` wraps ``claude -p`` in ``timeout "$_phase_timeout" ...``
# so a hung claude surfaces as exit 124 — but on a fresh Mac without
# coreutils, bash returns rc=127 ("command not found") instead of rc=124,
# the rc==124 short-circuit branch never fires, and the
# ``test_agent_runner_run_claude_phase.sh`` runtime-timeout regression
# silently fails locally with empty stdout.
#
# Resolve once at call time (cheap — two ``command -v`` checks) and return
# the binary name. Empty string means neither is on PATH; ``run_claude_phase``
# treats that as "exec inner command directly with no timer", which loses
# the rc=124 branch but is strictly better than aborting with rc=127. The
# self-sufficient runtime-timeout regression test stubs ``timeout`` into
# its private ``$_tbin`` directory the same way the entrypoint test does
# (see ``scripts/tests/test_agent_runner_entrypoint.sh`` lines 599-605),
# so the test path doesn't depend on the operator having coreutils
# installed.
#
# Output: prints either ``timeout``, ``gtimeout``, or empty string on
# stdout. Always exits 0.
_resolve_timeout_cmd() {
    if command -v timeout >/dev/null 2>&1; then
        printf 'timeout'
    elif command -v gtimeout >/dev/null 2>&1; then
        printf 'gtimeout'
    else
        printf ''
    fi
}

# ── _ms_now() — portable epoch-milliseconds helper (#4099) ──────────────────
#
# GNU ``date -u +%s%3N`` returns epoch-seconds + 3-digit nanoseconds (i.e.
# milliseconds). BSD ``date`` (macOS) does NOT support the ``%N`` format
# specifier — it silently emits a literal ``N`` appended to the seconds
# (e.g. ``17780132253N``). The ``2>/dev/null || printf 'unknown'`` fallback
# the helper used to wrap around ``date -u +%s%3N`` does NOT trip on this
# case, because BSD date exits 0 with the malformed token. The literal ``N``
# then poisons the downstream ``$((end - start))`` arithmetic with
# ``value too great for base (error token is "17780132253N")``, which under
# ``set -e`` aborts ``scripts/tests/test_agent_runner_entrypoint.sh`` on
# macOS bash 3.2 — blocking any /task agent on a Mac from running the
# entrypoint test suite locally.
#
# Fix: route through python3, which is already a hard dependency of the
# entrypoint (used for the phase-transition shims) and which produces a
# clean integer milliseconds value on every supported platform. The
# fallback to ``printf 'unknown'`` is preserved for the unlikely case
# where ``python3`` is absent so the existing ``unknown``-handling branch
# in ``run_claude_phase`` (and the T3778 test) still has a code path to
# exercise.
#
# Bash 3.2 compatible — no associative arrays, no ``${EPOCHREALTIME}`` (bash 5+).
#
# Output contract: prints either a positive integer (epoch milliseconds) or
# the literal string ``unknown``. Validate python3's stdout is a non-empty
# digit string before accepting it — a stubbed python3 (e.g. tests that
# replace python3 with ``#!/usr/bin/env bash\nexit 0\n``) returns rc=0
# with empty stdout, which would otherwise leak the empty string into the
# downstream ``$((end - start))`` arithmetic. Validating here keeps the
# ``unknown`` branch in ``run_claude_phase`` as the only path that
# produces non-numeric output.
_ms_now() {
    local _ms_now_out
    _ms_now_out=$(python3 -c 'import time; print(int(time.time() * 1000))' 2>/dev/null) \
        || _ms_now_out=""
    case "$_ms_now_out" in
        ''|*[!0-9]*) printf 'unknown' ;;
        *)           printf '%s' "$_ms_now_out" ;;
    esac
}

# ── Per-phase claude -p timeout constants ───────────────────────────────────
#
# Each phase has its own wall-clock cap so ralph (the long-tail phase)
# gets 90 min while cheaper phases keep a tighter 10-30 min ceiling.
# The lookup is delegated to ``claude_phase_timeout_seconds_by_phase()``
# below, which uses a case statement — same pattern as ``phase_to_skill``.
# The CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE name is preserved as a comment
# anchor for ``test_per_phase_timeout.py``'s static lints so future
# refactors that drop the per-phase table still trip the regression
# test.
#
# Other phases (planning, summary, fix_ci, fix_conflict, verify) all
# complete well under 10 min in healthy runs — keep them at 1800 or
# less so a hung claude in those phases still surfaces quickly. Ralph
# gets 90 min to cover the SKILL.md upper bound. Tests override per-
# phase via ``AGENT_RUNNER_<PHASE>_TIMEOUT_OVERRIDE_SECONDS`` env vars
# applied inside the lookup function so the constants themselves stay
# authoritative for non-test paths.
#
# CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE — phase-keyed cap table:
#   planning      1200s  (20 min — plan reads issue + writes plan.json)
#   ralph         5400s  (90 min — long-tail, SKILL.md upper bound)
#   summary        600s  (10 min — pure summary writer)
#   push_and_pr    600s  (10 min — local git + gh pr create)
#   fix_ci        1800s  (30 min — claude reads CI logs + writes patch)
#   fix_conflict  1800s  (30 min — claude resolves merge conflicts)
#   verify        1200s  (20 min — claude reads deploy + posts evidence)
#   retro          600s  (10 min — claude writes retro issues)
CLAUDE_PHASE_TIMEOUT_PLANNING_SECONDS=1200
CLAUDE_PHASE_TIMEOUT_RALPH_SECONDS=5400
CLAUDE_PHASE_TIMEOUT_SUMMARY_SECONDS=600
CLAUDE_PHASE_TIMEOUT_PUSH_AND_PR_SECONDS=600
CLAUDE_PHASE_TIMEOUT_FIX_CI_SECONDS=1800
CLAUDE_PHASE_TIMEOUT_FIX_CONFLICT_SECONDS=1800
CLAUDE_PHASE_TIMEOUT_VERIFY_SECONDS=1200
CLAUDE_PHASE_TIMEOUT_RETRO_SECONDS=600

# Fallback for any phase not listed above. Keeps the legacy 1800s
# ceiling for unknown phases so a future drift between the table and
# the dispatcher's phase vocabulary doesn't silently extend timeouts
# beyond the previous default.
DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS="${AGENT_RUNNER_DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS:-1800}"

# ── write_phase_input ────────────────────────────────────────────────────────

# Shell helper: build the input file for the given phase. The phase
# argument matches the skill-suffix (``plan``, ``ralph``, ``summary``,
# ``fix-ci``, ``verify``, ``retro``), which differs from the phase-
# column value (``planning`` vs ``plan``, ``fix_ci`` vs ``fix-ci``).
# ``run_claude_phase`` does the mapping before calling us.
write_phase_input() {
    _phase_suffix="$1"
    _issue_for_input="${ISSUE_NUMBER:-0}"
    _input_log="$AGENT_WORKSPACE/phase-input-$_phase_suffix.log"
    if ! python3 "$PHASE_INPUT_SHIM" \
        "$_phase_suffix" \
        "$AGENT_ID" \
        "$_issue_for_input" \
        "$REPO_ROOT" \
        > "$_input_log" \
        2>> "$_input_log"; then
        # Issue #3547: capture the shim's stderr tail so CloudWatch
        # operators can distinguish a gh-timeout from a missing DB row
        # without a second deploy-instrument cycle.
        _shim_err_tail=$(tail -c 512 "$_input_log" 2>/dev/null \
            | tr '\n\r\t' '   ' || printf '')
        log "phase_input_write_failed" \
            "phase=$_phase_suffix" \
            "shim_err_tail=$_shim_err_tail"
        return 1
    fi
    log "phase_input_written" "phase=$_phase_suffix"
    return 0
}

# ── read_phase_output ─────────────────────────────────────────────────────────

# Read the skill's structured output from
# ``{repo_root}/tmp/dispatcher-output/<skill_suffix>.json`` and print
# the minified JSON to stdout. Returns non-zero when the file is
# missing / unparseable so ``run_claude_phase`` can fall back to the
# ``.result`` envelope + diag path.
read_phase_output() {
    _phase_suffix="$1"
    _out_path="$REPO_ROOT/tmp/dispatcher-output/$_phase_suffix.json"
    if [[ ! -s "$_out_path" ]]; then
        return 1
    fi
    # ``jq -c '.'`` validates JSON and emits on one line. Failure
    # (malformed file) → non-zero exit, empty stdout, caller falls
    # through to the ``.result`` branch.
    if ! jq -c '.' "$_out_path" 2>/dev/null; then
        return 1
    fi
    return 0
}

# ── phase_to_skill ────────────────────────────────────────────────────────────

# Phase → skill name mapping (#3117). The phase-name constants emitted
# by `scripts/dispatcher/phase_transitions.py` use underscored/present-
# participle names (planning, fix_ci). The actual Claude skills on disk
# are hyphenated/root-verb (task-v2-plan, task-v2-fix-ci). We do NOT
# construct `/task-v2-$_phase` directly; we map to the literal skill
# name via this case statement so any drift (new phase, renamed skill)
# surfaces as an explicit die() instead of a silent "Unknown command"
# string coming back from claude.
#
# Bash 3.2: a case statement is used instead of an associative array
# (declare -A is bash 4+).
phase_to_skill() {
    # $1 = phase name. Prints the matching skill suffix (no `task-v2-`
    # prefix) on stdout. die()s if no mapping exists.
    case "$1" in
        planning)       printf 'plan' ;;
        ralph)          printf 'ralph' ;;
        summary)        printf 'summary' ;;
        fix_ci)         printf 'fix-ci' ;;
        fix_conflict)   printf 'fix-conflict' ;;  # #3225
        verify)         printf 'verify' ;;
        retro)          printf 'retro' ;;
        operational)    printf 'operational' ;;  # #3507
        *)              die "no_skill_mapping_for_phase=$1" ;;
    esac
}

# ── run_claude_phase ──────────────────────────────────────────────────────────

run_claude_phase() {
    _phase="$1"
    _out_file="$AGENT_WORKSPACE/claude-p-$_phase.stdout.json"
    _err_file="$AGENT_WORKSPACE/claude-p-$_phase.stderr.log"

    if [[ "$AGENT_RUNNER_DRY_RUN" == "1" ]]; then
        log "claude_phase_dry_run" "phase=$_phase"
        printf '{}'
        return 0
    fi

    # Map phase name → skill suffix. A bad phase (e.g. accidentally
    # routed `claiming` or `push_and_pr` through this function) aborts
    # the runner via die() so the symptom surfaces at test time, not
    # as a silent "Unknown command" in production CloudWatch.
    _skill=$(phase_to_skill "$_phase")

    # Write the phase's dispatcher-input JSON before invoking claude
    # (#3133). Without this, every task-v2-* skill hits its input-
    # missing guard and returns a plain-string `.result` — which is
    # exactly the failure mode the Step 1 diag captured. The shim is
    # best-effort: if gh is unreachable or the issue is gone, it still
    # writes a partial payload so the skill at least has the base
    # identifier fields and can produce a structured BLOCKED verdict
    # rather than a null-deref.
    write_phase_input "$_skill" || true

    # Do NOT fail the script on a non-zero exit — parse the envelope
    # and let the caller decide. Redirect stderr to a sibling file for
    # triage parity with the daemon.
    #
    # #3190: explicitly anchor cwd to $REPO_ROOT for the `claude -p`
    # invocation. The task-v2-* skills look up their input bundle via
    # the RELATIVE path ``tmp/dispatcher-input/<phase>.json`` — if the
    # child process inherits a cwd that isn't the repo root, the
    # lookup silently misses and the skill emits a string-shaped FAILED
    # verdict, which the daemon classifies as
    # ``verify_infra_failure_post_merge`` (silent no-op verify). The
    # entrypoint runs `cd "$REPO_ROOT"` once at line 190, but wrapping
    # the claude invocation in an explicit subshell is belt-and-
    # suspenders: it guarantees the child process cwd regardless of
    # what any intervening phase handler may have done, AND it gives
    # the ``cwd=`` field on ``claude_phase_begin`` (logged inside the
    # subshell) as a self-diagnosing artifact for the next incident.
    # #3766: pick the per-phase timeout via
    # ``claude_phase_timeout_seconds_by_phase`` (bash 3.2-compatible
    # case-statement lookup; see the function definition below for the
    # full layered-fallback contract). The legacy single-value
    # ``AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS`` env var is still
    # honoured as an operator-emergency global override, but the
    # primary lookup is the per-phase table — ralph in particular gets
    # 5400s (90 min) so the SKILL.md upper bound for the long-tail
    # phase no longer SIGKILLs mid-iteration. The CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE
    # name appears in the constants block above as a comment anchor
    # for the test_per_phase_timeout.py static lints.
    _phase_timeout=$(claude_phase_timeout_seconds_by_phase "$_phase")
    # #4125: pick ``timeout`` (Linux) or ``gtimeout`` (macOS + coreutils);
    # empty string when neither is on PATH (fresh Mac with no coreutils),
    # in which case we exec claude directly without a timer. Runtime
    # ``rc=124`` branch is unreachable in that fallback, but that's
    # strictly better than rc=127 ("command not found") which the bare
    # ``timeout`` invocation produced before this fix.
    _timeout_cmd=$(_resolve_timeout_cmd)
    # #4099: route through ``_ms_now`` (python3-backed) so macOS BSD date
    # doesn't silently emit ``17780132253N`` — see helper docstring above.
    _phase_start_ms=$(_ms_now)
    set +e
    (
        cd "$REPO_ROOT" || exit 127
        log "claude_phase_begin" \
            "phase=$_phase" \
            "skill=$_skill" \
            "cwd=$(pwd)" \
            "timeout_cmd=$_timeout_cmd" \
            "timeout_seconds=$_phase_timeout"
        # #3683: wrap ``claude -p`` in ``timeout`` so a hung or wedged
        # claude process surfaces as a distinct ``claude_phase_timeout``
        # event (exit 124) rather than silently pinning the cap slot for
        # up to 30 minutes until the heartbeat reaper fires.
        # #3766: per-phase timeout — look up at function-call time so a
        # post-init mutation of the table (test override) takes effect.
        # #4125: ``$_timeout_cmd`` resolves to ``timeout`` / ``gtimeout`` /
        # empty (see _resolve_timeout_cmd above). When empty, the leading
        # token expansion vanishes and bash execs ``claude -p ...`` directly.
        $_timeout_cmd ${_timeout_cmd:+"$_phase_timeout"} \
            claude -p "/task-v2-$_skill $AGENT_ID" \
            --output-format json \
            --dangerously-skip-permissions \
            > "$_out_file" \
            2> "$_err_file"
    )
    _rc=$?
    set -e
    # #4099: see ``_ms_now`` helper above — replaces ``date -u +%s%3N``.
    _phase_end_ms=$(_ms_now)
    if [[ "$_phase_start_ms" == 'unknown' || "$_phase_end_ms" == 'unknown' ]]; then
        _phase_duration_ms='unknown'
    else
        _phase_duration_ms=$((_phase_end_ms - _phase_start_ms))
    fi
    _stdout_size=$(wc -c < "$_out_file" 2>/dev/null | tr -d ' ' || printf 'unknown')
    _stderr_size=$(wc -c < "$_err_file" 2>/dev/null | tr -d ' ' || printf 'unknown')
    log "claude_phase_done" "phase=$_phase" "exit_code=$_rc" \
        "stdout_size=$_stdout_size" \
        "stderr_size=$_stderr_size" \
        "duration_ms=$_phase_duration_ms"
    # #3683: emit a distinct event when the timeout fires so CloudWatch
    # Logs Insights queries can grep for ``claude_phase_timeout`` to
    # count incidents. This branch runs BEFORE the non-object-result
    # branch so the failure surfaces with a real exit code rather than
    # silently re-routing through the json-parse path.
    if [[ "$_rc" -eq 124 ]]; then
        # #3832: salvage prelude — if claude already finished before timeout()
        # SIGKILLed the wrapper, the output JSON will have is_error=false and
        # terminal_reason="completed". Route through the same output-resolution
        # chain as the clean-exit branch (see lines ~2176-2267, canonical copy)
        # instead of emitting a misleading BLOCKED envelope.
        # #4125: explicitly require ``_out_file`` to be non-empty (``-s``)
        # before invoking jq. macOS jq-1.6 returns rc=0 with empty output
        # when given an empty input file, so the bare ``jq -e ...`` filter
        # silently fired the salvage branch on a SIGKILLed-empty file —
        # which falls through ``read_phase_output`` (no dispatcher-output
        # written) → ``jq -c '.result'`` (still empty) → empty stdout, no
        # BLOCKED envelope. Linux jq-1.7+ exits rc=2 on empty input which
        # masked the bug in CI. The ``-s`` guard makes the salvage path
        # correct regardless of jq version.
        if [[ -s "$_out_file" ]] && jq -e '.result and .is_error == false and (.terminal_reason == "completed")' \
                "$_out_file" >/dev/null 2>&1; then
            _claude_duration_ms=$(jq -r '.duration_ms // "unknown"' "$_out_file" 2>/dev/null)
            log "claude_phase_timeout_salvaged" \
                "phase=$_phase" \
                "claude_duration_ms=$_claude_duration_ms" \
                "wrapper_deadline_seconds=$_phase_timeout"
            # Output resolution mirrors the clean-exit branch below exactly:
            # 1. dispatcher-output file; 2. .result object; 3. salvage-failed BLOCKED.
            _file_output=""
            if _file_output=$(read_phase_output "$_skill"); then
                printf '%s' "$_file_output"
                return 0
            fi
            if jq -e '.result | type == "object"' "$_out_file" >/dev/null 2>&1; then
                jq -c '.result' "$_out_file" 2>/dev/null || printf '{}'
                return 0
            fi
            # .result is non-object (string-shaped) — fall through to BLOCKED path.
        fi
        log "claude_phase_timeout" \
            "phase=$_phase" \
            "elapsed_seconds=$_phase_timeout"
        # #3766: short-circuit the post-claude output resolution. When
        # ``timeout`` SIGKILLs the claude subprocess, the
        # ``read_phase_output`` path below returns nothing (the skill
        # never wrote dispatcher-output) and the ``.result | type==object``
        # path returns false (claude got SIGKILLed mid-run, leaving the
        # JSON envelope truncated). Without this short-circuit, the
        # function falls through to the empty-result branch and the
        # caller sees ``{}`` → ``ralph_done_marker_missing`` with empty
        # stdout/stderr buffers (because SIGKILL truncated them) — same
        # diagnostic shape as the silent-ralph hook-swap bug fixed in
        # #3757/PR #3761, but a different root cause.
        #
        # Replace ``{}`` with a structured BLOCKED envelope that names
        # the timeout. Both ``transition_from_ralph`` and the daemon's
        # failure-category mapping table dispatch on the
        # ``category="claude_phase_timeout"`` field to route the
        # terminal to a dedicated diagnoser fix shape (bump the cap,
        # investigate runaway iteration count, etc.) rather than the
        # generic ``ralph_not_ship`` path.
        _timeout_output=$(jq -n -c \
            --arg phase "$_phase" \
            --arg elapsed "$_phase_timeout" \
            '{verdict: "BLOCKED",
              category: "claude_phase_timeout",
              block_reason: ("claude -p timed out after " + $elapsed + "s in phase " + $phase),
              claude_phase_timeout: true,
              elapsed_seconds: ($elapsed | tonumber)}' \
            2>/dev/null \
            || printf '{"verdict":"BLOCKED","category":"claude_phase_timeout","block_reason":"claude -p timed out (jq build failed)","claude_phase_timeout":true,"elapsed_seconds":0}')
        log "claude_phase_timeout_envelope_built" \
            "phase=$_phase" \
            "elapsed_seconds=$_phase_timeout"
        printf '%s' "$_timeout_output"
        return 0
    fi

    # Output resolution order (#3133):
    #   1. ``{repo_root}/tmp/dispatcher-output/<skill_suffix>.json`` —
    #      the skill's structured output file. This is how the daemon
    #      reads verdict/plan-body/etc. — see
    #      :meth:`DispatcherDaemon._read_phase_output`. Before #3133
    #      the agent-runner ignored this file entirely and read only
    #      the claude `.result` envelope, which is the conversation
    #      text summary (string), not the structured JSON the skill
    #      actually wrote.
    #   2. ``.result`` as a JSON object — legacy path + defensive
    #      fallback for skills that return a dict directly. Kept for
    #      back-compat with any skill that hasn't migrated to the
    #      dispatcher-output/ contract.
    #   3. ``{}`` with the #3131 diag event — skill crashed before
    #      writing its output and the envelope itself isn't a dict.
    _file_output=""
    if _file_output=$(read_phase_output "$_skill"); then
        log "phase_output_file_read" "phase=$_phase" "skill=$_skill"
        printf '%s' "$_file_output"
        return 0
    fi

    if jq -e '.result | type == "object"' "$_out_file" >/dev/null 2>&1; then
        jq -c '.result' "$_out_file" 2>/dev/null || printf '{}'
    else
        # Self-diagnosing branch (#3131). Every ECS agent today is dying
        # because `.result` comes back non-object; operators have no way
        # to see WHY without a second deploy-instrument cycle. Log the
        # structured triage fields directly on the failure event so the
        # first post-deploy ECS agent surfaces the actual payload in
        # CloudWatch. Preserves the existing `claude_result_non_object`
        # event name (load-bearing for log-insights queries) and adds a
        # sibling `claude_result_non_object_diag` event carrying:
        #
        #   * result_type   — jq's type of `.result` (string/null/number/...).
        #   * result_bytes  — byte length of `.result` stringified.
        #   * result_head_512  — first 512 bytes of `.result` stringified,
        #     newlines replaced with `\n` and double quotes escaped so
        #     the line stays a single JSON-ish record the log() function
        #     can emit without breaking CloudWatch line boundaries.
        #   * stderr_head_1024 — first 1024 bytes of the sibling
        #     `.stderr.log` file, same escaping. Omitted when the stderr
        #     file is empty / missing so the common "no stderr" case
        #     isn't noise.
        #
        # All payload extraction happens in-process via `jq` + shell
        # builtins — no extra tools, no new dependencies. Bash 3.2
        # compatible (`cut -c` + `tr` + `sed`; no `${var:0:512}` slicing
        # on multi-byte-safe boundaries is required because we're only
        # emitting the first 512 BYTES for triage, not interpreting them).
        log "claude_result_non_object" "phase=$_phase" "out_file=$_out_file"

        _result_type=$(jq -r '.result | type' "$_out_file" 2>/dev/null || printf 'unparseable')
        # `.result | tostring` stringifies any type — strings pass
        # through, objects/arrays/null serialize to their JSON form.
        # `-r` emits raw (no wrapping quotes) so the byte count matches
        # the actual payload length. Falls back to the raw file on a
        # parse failure (malformed JSON) so the diag still shows the
        # first 512 bytes of whatever claude wrote.
        _result_str_file="$AGENT_WORKSPACE/claude-p-$_phase.result.str"
        if ! jq -r '.result | tostring' "$_out_file" > "$_result_str_file" 2>/dev/null; then
            cp "$_out_file" "$_result_str_file" 2>/dev/null || printf '' > "$_result_str_file"
        fi
        _result_bytes=$(wc -c < "$_result_str_file" 2>/dev/null | tr -d ' ' || printf '0')

        # Shape for embedding in log() key=value pairs. log() handles
        # double-quote escaping; we handle backslash escaping (log()
        # doesn't) and control-char squashing (so the output stays one
        # line and does not split CloudWatch log events). Order matters:
        # escape backslashes FIRST so we don't double-escape the ones we
        # just added. Use `tr` to replace newlines/CRs/tabs with spaces
        # rather than emitting literal `\n` (which would require
        # teaching log() to not re-escape the backslash). This keeps
        # the triage payload human-readable in CloudWatch at the cost
        # of losing exact whitespace boundaries — fine for a first-512
        # smoke.
        _result_head_512=$(head -c 512 "$_result_str_file" 2>/dev/null \
            | tr '\n\r\t' '   ' \
            | sed -e 's/\\/\\\\/g' || printf '')

        # Stderr tail — only emit when non-empty so the common healthy
        # path doesn't bloat log lines.
        _stderr_file="$AGENT_WORKSPACE/claude-p-$_phase.stderr.log"
        _stderr_head_1024=""
        if [[ -s "$_stderr_file" ]]; then
            _stderr_head_1024=$(head -c 1024 "$_stderr_file" 2>/dev/null \
                | tr '\n\r\t' '   ' \
                | sed -e 's/\\/\\\\/g' || printf '')
        fi

        if [[ -n "$_stderr_head_1024" ]]; then
            log "claude_result_non_object_diag" \
                "phase=$_phase" \
                "result_type=$_result_type" \
                "result_bytes=$_result_bytes" \
                "result_head_512=$_result_head_512" \
                "stderr_head_1024=$_stderr_head_1024"
        else
            log "claude_result_non_object_diag" \
                "phase=$_phase" \
                "result_type=$_result_type" \
                "result_bytes=$_result_bytes" \
                "result_head_512=$_result_head_512"
        fi

        printf '{}'
    fi
}

# ── claude_phase_timeout_seconds_by_phase ─────────────────────────────────────

# Look up the per-phase ``claude -p`` timeout. Bash 3.2-compatible
# replacement for the associative array suggested in the issue spec.
# Returns the cap (seconds) on stdout. Test overrides via env var
# ``AGENT_RUNNER_<PHASE>_TIMEOUT_OVERRIDE_SECONDS`` are honoured here
# so a test setting ``AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS=1``
# drives the rc=124 branch deterministically without mutating any
# global state.
#
# Layered fallback (highest to lowest precedence):
#   1. Per-phase test override env var (uppercased phase + suffix)
#   2. The global ``AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS`` env
#      var (operator emergency override; back-compat with #3683)
#   3. Per-phase constant from the table above
#   4. ``DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS`` (legacy 1800s)
claude_phase_timeout_seconds_by_phase() {
    # Compact one-liner case arms — same style as ``phase_to_skill``
    # — keeps the awk regex in
    # ``scripts/tests/test_agent_runner_entrypoint.sh`` (which matches
    # ``^        fix_conflict\)$`` exactly) from snagging this function
    # by mistake. See the dispatch-arm extraction at T51 in that test.
    case "$1" in
        planning)     printf '%s' "${AGENT_RUNNER_PLANNING_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_PLANNING_SECONDS}}" ;;
        ralph)        printf '%s' "${AGENT_RUNNER_RALPH_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_RALPH_SECONDS}}" ;;
        summary)      printf '%s' "${AGENT_RUNNER_SUMMARY_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_SUMMARY_SECONDS}}" ;;
        push_and_pr)  printf '%s' "${AGENT_RUNNER_PUSH_AND_PR_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_PUSH_AND_PR_SECONDS}}" ;;
        fix_ci)       printf '%s' "${AGENT_RUNNER_FIX_CI_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_FIX_CI_SECONDS}}" ;;
        fix_conflict) printf '%s' "${AGENT_RUNNER_FIX_CONFLICT_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_FIX_CONFLICT_SECONDS}}" ;;
        verify)       printf '%s' "${AGENT_RUNNER_VERIFY_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_VERIFY_SECONDS}}" ;;
        retro)        printf '%s' "${AGENT_RUNNER_RETRO_TIMEOUT_OVERRIDE_SECONDS:-${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$CLAUDE_PHASE_TIMEOUT_RETRO_SECONDS}}" ;;
        # Phase not in the table — fall back to the operator-overridable
        # global. Catches scheduled-skill phases (audit, spotcheck,
        # etc.) and any future drift.
        *)            printf '%s' "${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-$DEFAULT_CLAUDE_PHASE_TIMEOUT_SECONDS}" ;;
    esac
}
