#!/usr/bin/env bash
# test_ralph_output_synthesis.sh — Regression test for issue #3782
# (Layer 4 of the silent-ralph saga).
#
# Symptom: claude-p subprocess for the `ralph` phase completes a normal
# conversation (exit_code=0, terminal_reason=completed, no permission
# denials, well under timeout cap) but the model never executes Step 4
# of `.claude/skills/task-v2-ralph/SKILL.md` — the `Write` of
# `{worktree}/tmp/dispatcher-output/ralph.json`. The agent-runner wrapper
# sees `_output == "{}"` and falls through to `ralph_done_marker_missing`
# with empty buffers, even though the inner /ralph subagent successfully
# wrote `{worktree}/tmp/ralph/ralph-done.txt`.
#
# Fix shape (Layer 4 — wrapper-side fallback): when the wrapper sees
# `_output == "{}"` AND phase == "ralph", BEFORE emitting
# `ralph_done_marker_missing`, check whether
# `{worktree}/tmp/ralph/ralph-done.txt` exists. If yes, synthesize a
# structured dispatcher-output/ralph.json from:
#   * verdict — first non-blank line of `ralph-done.txt` (parses
#     `status: SHIP|REVISE|BLOCKED|AC_INFEASIBLE`)
#   * iterations_used — `iteration.txt` first line, integer
#   * changed_files — `git diff --name-only origin/main...HEAD`
#   * summary — `feedback.md` tail or "Step 4 not executed; synthesized
#     from ralph-done.txt"
#   * block_reason — "Layer 4 synthesis: model completed conversation
#     without Step 4 Write" for non-SHIP verdicts; null for SHIP
# Logs `ralph_output_synthesized_from_done_marker` event with the
# resolved verdict.
#
# This test exercises the sourceable helper
# `scripts/dispatcher/agent_runner_synthesize_ralph_output.sh` which
# defines `synthesize_ralph_output_from_done_marker <worktree>`. It also
# performs a static lint asserting that `agent-runner-entrypoint.sh`
# wires the helper into the `_output == "{}"` && `_current == "ralph"`
# branch of `run_claude_phase`'s caller.
#
# Test must FAIL against current code: the helper file does not exist
# yet, and the entrypoint does not source it. Each test case asserts a
# specific contract (exit code, JSON shape, logged event) so the failures
# point at the missing piece.
#
# Usage:
#   scripts/dispatcher/tests/test_ralph_output_synthesis.sh
#
# Exit codes:
#   0 — all assertions passed.
#   1 — one or more assertions failed (regression).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="$SCRIPT_DIR/dispatcher/agent_runner_synthesize_ralph_output.sh"
ENTRYPOINT="$SCRIPT_DIR/dispatcher/agent-runner-entrypoint.sh"

FAILURES=0
TESTS=0

TEMP_DIRS=()
cleanup() {
    set +e
    # ${arr[@]+...} expansion guards against unbound under set -u when
    # cleanup runs before any temp dir was added (early-exit path).
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# Build a fake worktree containing the ralph state files the helper
# reads. Returns the worktree path on stdout.
make_fake_worktree() {
    local verdict="$1"
    local iteration_count="$2"
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local worktree="$tmp/worktree"
    mkdir -p "$worktree/tmp/ralph"

    # Bootstrap a real git repo so `git diff --name-only origin/main...HEAD`
    # produces predictable output. The "remote" is just a local bare
    # repo; the worktree branch contains exactly the files the test
    # cares about.
    local bare="$tmp/bare.git"
    git init -q --bare "$bare"

    git -C "$worktree" init -q --initial-branch main
    git -C "$worktree" config user.email "test@example.com"
    git -C "$worktree" config user.name "Test"
    # Seed an empty initial commit on main so origin/main has a known
    # base.
    printf 'baseline\n' > "$worktree/baseline.txt"
    git -C "$worktree" add baseline.txt
    git -C "$worktree" commit -q -m "init"
    git -C "$worktree" remote add origin "$bare"
    git -C "$worktree" push -q origin main
    git -C "$worktree" checkout -q -b feature

    # Add two known changed files so the helper's
    # `git diff --name-only origin/main...HEAD` returns a deterministic
    # list.
    mkdir -p "$worktree/packages/scraper"
    printf 'changed line\n' > "$worktree/packages/scraper/foo.py"
    printf 'test line\n' > "$worktree/packages/scraper/test_foo.py"
    git -C "$worktree" add packages/scraper/foo.py packages/scraper/test_foo.py
    git -C "$worktree" commit -q -m "feature work"

    # Write the ralph state files.
    cat > "$worktree/tmp/ralph/ralph-done.txt" <<EOF
status: $verdict
iterations: $iteration_count
next-steps: The /task workflow MUST now continue with: A.2b (process summary), A.3 ...
EOF
    printf '%s\n' "$iteration_count" > "$worktree/tmp/ralph/iteration.txt"
    cat > "$worktree/tmp/ralph/feedback.md" <<'EOF'
No prior feedback. This is the first iteration.

## Iteration 2 worker progress

Worker fixed the leading-zero loss in Orange County department regex
and added a regression test asserting department='03' round-trips.
EOF

    printf '%s' "$worktree"
}

# Check helper file exists.

if [[ ! -f "$HELPER" ]]; then
    fail "helper script exists" "expected $HELPER to be present"
    echo
    echo "Tests: $TESTS, Failures: $FAILURES"
    exit 1
fi
pass "helper script $HELPER exists"

# Helper must define the synthesis function when sourced.

if ! ( set +u; source "$HELPER"; type synthesize_ralph_output_from_done_marker >/dev/null 2>&1 ); then
    fail "helper defines synthesize_ralph_output_from_done_marker" \
        "sourcing $HELPER did not define the function"
    echo
    echo "Tests: $TESTS, Failures: $FAILURES"
    exit 1
fi
pass "helper defines synthesize_ralph_output_from_done_marker function"

# ── Test 1: SHIP verdict synthesis ────────────────────────────────────────
#
# Stage a worktree with ralph-done.txt = SHIP, iteration.txt = 3, and a
# known set of changed files. Invoke the helper. Assert the JSON it
# emits has verdict=SHIP, iterations_used=3, the expected changed_files,
# block_reason=null, and a non-empty summary.

run_ship_synthesis_test() {
    local worktree
    worktree=$(make_fake_worktree "SHIP" 3)

    local logfile
    local logdir
    logdir=$(mktemp -d)
    TEMP_DIRS+=("$logdir")
    logfile="$logdir/log.out"

    local jsonfile
    jsonfile="$logdir/json.out"

    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export AGENT_ID="test-agent-ship"
        synthesize_ralph_output_from_done_marker "$worktree"
    ) > "$jsonfile" 2> "$logfile" || true

    # The helper writes structured logs via log() (which goes to fd 3 →
    # stdout in production, but in our isolated source it goes to stdout
    # too via the shim). Function output goes to stdout. Tests need both,
    # so the helper splits: log() writes via a fallback to stderr in the
    # shim path so jsonfile captures only the synthesised JSON.
    local json_content
    json_content=$(cat "$jsonfile")

    # Assert: jq parses the JSON.
    if ! printf '%s' "$json_content" | jq -e . >/dev/null 2>&1; then
        fail "SHIP synthesis emits valid JSON" \
            "got: $json_content"
        return
    fi
    pass "SHIP synthesis emits valid JSON"

    # Assert: verdict=SHIP.
    local verdict
    verdict=$(printf '%s' "$json_content" | jq -r '.verdict')
    if [[ "$verdict" != "SHIP" ]]; then
        fail "SHIP synthesis: verdict=SHIP" \
            "got verdict=$verdict, json=$json_content"
        return
    fi
    pass "SHIP synthesis: verdict=SHIP"

    # Assert: iterations_used=3 (integer).
    local iterations
    iterations=$(printf '%s' "$json_content" | jq -r '.iterations_used')
    if [[ "$iterations" != "3" ]]; then
        fail "SHIP synthesis: iterations_used=3" \
            "got iterations_used=$iterations"
        return
    fi
    pass "SHIP synthesis: iterations_used=3"

    # Assert: changed_files contains both expected paths.
    local changed_count
    changed_count=$(printf '%s' "$json_content" | jq -r '.changed_files | length')
    if [[ "$changed_count" != "2" ]]; then
        fail "SHIP synthesis: changed_files length=2" \
            "got length=$changed_count, files=$(printf '%s' "$json_content" | jq -c '.changed_files')"
        return
    fi
    pass "SHIP synthesis: changed_files length=2"

    if ! printf '%s' "$json_content" \
            | jq -e '.changed_files | index("packages/scraper/foo.py")' >/dev/null 2>&1; then
        fail "SHIP synthesis: changed_files contains packages/scraper/foo.py" \
            "got: $(printf '%s' "$json_content" | jq -c '.changed_files')"
        return
    fi
    pass "SHIP synthesis: changed_files contains packages/scraper/foo.py"

    # Assert: block_reason is null on SHIP.
    local block_reason
    block_reason=$(printf '%s' "$json_content" | jq -r '.block_reason')
    if [[ "$block_reason" != "null" ]]; then
        fail "SHIP synthesis: block_reason=null" \
            "got block_reason=$block_reason"
        return
    fi
    pass "SHIP synthesis: block_reason=null"

    # Assert: summary is a non-empty string.
    local summary
    summary=$(printf '%s' "$json_content" | jq -r '.summary')
    if [[ -z "$summary" || "$summary" == "null" ]]; then
        fail "SHIP synthesis: summary is non-empty" \
            "got summary=$summary"
        return
    fi
    pass "SHIP synthesis: summary is non-empty"

    # Assert: ralph_output_synthesized_from_done_marker event was logged
    # with verdict=SHIP. The helper's log shim writes to stderr when
    # there's no fd 3, so we check the logfile.
    if ! grep -q "ralph_output_synthesized_from_done_marker" "$logfile"; then
        fail "SHIP synthesis: ralph_output_synthesized_from_done_marker logged" \
            "logfile contents: $(cat "$logfile")"
        return
    fi
    pass "SHIP synthesis: ralph_output_synthesized_from_done_marker logged"

    if ! grep -q "verdict.*SHIP" "$logfile"; then
        fail "SHIP synthesis: log line includes verdict=SHIP" \
            "logfile contents: $(cat "$logfile")"
        return
    fi
    pass "SHIP synthesis: log line includes verdict=SHIP"
}

# ── Test 2: BLOCKED verdict synthesis sets block_reason ───────────────────

run_blocked_synthesis_test() {
    local worktree
    worktree=$(make_fake_worktree "BLOCKED" 5)

    local logfile
    local logdir
    logdir=$(mktemp -d)
    TEMP_DIRS+=("$logdir")
    logfile="$logdir/log.out"

    local jsonfile
    jsonfile="$logdir/json.out"

    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export AGENT_ID="test-agent-blocked"
        synthesize_ralph_output_from_done_marker "$worktree"
    ) > "$jsonfile" 2> "$logfile" || true

    local json_content
    json_content=$(cat "$jsonfile")

    if ! printf '%s' "$json_content" | jq -e . >/dev/null 2>&1; then
        fail "BLOCKED synthesis emits valid JSON" \
            "got: $json_content"
        return
    fi
    pass "BLOCKED synthesis emits valid JSON"

    local verdict
    verdict=$(printf '%s' "$json_content" | jq -r '.verdict')
    if [[ "$verdict" != "BLOCKED" ]]; then
        fail "BLOCKED synthesis: verdict=BLOCKED" \
            "got verdict=$verdict"
        return
    fi
    pass "BLOCKED synthesis: verdict=BLOCKED"

    # Assert: block_reason names the Layer 4 synthesis path. Must mention
    # "Layer 4" and "Step 4" so operators can grep for the synthesis
    # cohort in CloudWatch.
    local block_reason
    block_reason=$(printf '%s' "$json_content" | jq -r '.block_reason')
    if [[ "$block_reason" == "null" || -z "$block_reason" ]]; then
        fail "BLOCKED synthesis: block_reason is non-null" \
            "got block_reason=$block_reason"
        return
    fi
    pass "BLOCKED synthesis: block_reason is non-null"

    if ! printf '%s' "$block_reason" | grep -q "Layer 4 synthesis"; then
        fail "BLOCKED synthesis: block_reason mentions Layer 4 synthesis" \
            "got block_reason=$block_reason"
        return
    fi
    pass "BLOCKED synthesis: block_reason mentions Layer 4 synthesis"

    # Assert: ralph_output_synthesized_from_done_marker event with
    # verdict=BLOCKED logged.
    if ! grep -q "ralph_output_synthesized_from_done_marker" "$logfile"; then
        fail "BLOCKED synthesis: synthesis event logged" \
            "logfile contents: $(cat "$logfile")"
        return
    fi
    pass "BLOCKED synthesis: synthesis event logged"
}

# ── Test 3: missing ralph-done.txt → helper returns non-zero ──────────────
#
# When the inner /ralph never wrote ralph-done.txt at all, there's
# nothing to synthesize from. The helper must signal this by returning
# non-zero so the wrapper falls through to the existing
# `ralph_done_marker_missing` log path (Layer 1 instrumentation).

run_missing_ralph_done_test() {
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local worktree="$tmp/worktree"
    mkdir -p "$worktree/tmp/ralph"
    # Deliberately do NOT create ralph-done.txt.

    local rc=0
    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export AGENT_ID="test-agent-missing"
        synthesize_ralph_output_from_done_marker "$worktree"
    ) > /dev/null 2>&1 || rc=$?

    if [[ "$rc" -eq 0 ]]; then
        fail "missing ralph-done.txt: helper returns non-zero" \
            "expected non-zero exit, got 0"
        return
    fi
    pass "missing ralph-done.txt: helper returns non-zero"
}

# ── Test 4: AC_INFEASIBLE verdict passes through verbatim ─────────────────
#
# The Layer 4 synthesis must not silently flatten AC_INFEASIBLE into
# BLOCKED — the daemon's diagnoser routes those to a different path
# (see daemon.py ralph_ac_infeasible).

run_ac_infeasible_synthesis_test() {
    local worktree
    worktree=$(make_fake_worktree "AC_INFEASIBLE" 2)

    local logdir
    logdir=$(mktemp -d)
    TEMP_DIRS+=("$logdir")
    local jsonfile="$logdir/json.out"

    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export AGENT_ID="test-agent-ac-infeasible"
        synthesize_ralph_output_from_done_marker "$worktree"
    ) > "$jsonfile" 2>/dev/null || true

    local verdict
    verdict=$(cat "$jsonfile" | jq -r '.verdict' 2>/dev/null || printf 'parse-fail')
    if [[ "$verdict" != "AC_INFEASIBLE" ]]; then
        fail "AC_INFEASIBLE synthesis: verdict passes through" \
            "got verdict=$verdict"
        return
    fi
    pass "AC_INFEASIBLE synthesis: verdict passes through"
}

# ── Test 5: entrypoint wires the synthesis helper ─────────────────────────
#
# The agent-runner-entrypoint.sh script must source
# `agent_runner_synthesize_ralph_output.sh` near the top (alongside the
# fargate-hook helper) AND the silent-exit ralph branch must call
# `synthesize_ralph_output_from_done_marker` BEFORE
# `log "ralph_done_marker_missing"` so a successful synthesis short-
# circuits the missing-marker path.
#
# #4138: the silent-exit ralph branch was extracted from
# ``phase_loop()``'s inline ``planning|ralph|summary|verify)`` arm into
# ``handle_ralph`` in ``scripts/dispatcher/agent_runner_handlers.sh``,
# mirroring the precedent set by #3775. The synthesis-call/missing-
# marker-log landmarks now live in the handlers file. The ``source``
# of the synthesis helper still lives in the entrypoint (handlers file
# inherits ``synthesize_ralph_output_from_done_marker`` from the same
# scope when the entrypoint sources both files).

run_entrypoint_wires_helper_test() {
    if [[ ! -f "$ENTRYPOINT" ]]; then
        fail "agent-runner-entrypoint.sh exists" "expected at $ENTRYPOINT"
        return
    fi

    # The handlers-file path is the sibling of $ENTRYPOINT and contains
    # ``handle_ralph`` (#4138). Some landmarks moved there.
    local HANDLERS
    HANDLERS="$(dirname "$ENTRYPOINT")/agent_runner_handlers.sh"
    if [[ ! -f "$HANDLERS" ]]; then
        fail "agent_runner_handlers.sh exists" "expected at $HANDLERS"
        return
    fi

    if ! grep -q "agent_runner_synthesize_ralph_output.sh" "$ENTRYPOINT"; then
        fail "entrypoint sources agent_runner_synthesize_ralph_output.sh" \
            "the entrypoint must source the synthesis helper so it's available at runtime"
        return
    fi
    pass "entrypoint sources agent_runner_synthesize_ralph_output.sh"

    # The call to ``synthesize_ralph_output_from_done_marker`` lives in
    # ``handle_ralph`` post-#4138.
    if ! grep -q "synthesize_ralph_output_from_done_marker" "$HANDLERS"; then
        fail "handle_ralph calls synthesize_ralph_output_from_done_marker" \
            "handle_ralph in agent_runner_handlers.sh must invoke the synthesis function in the silent-exit branch"
        return
    fi
    pass "entrypoint calls synthesize_ralph_output_from_done_marker"

    # The synthesis call must come BEFORE the
    # `ralph_done_marker_missing` log line in the same handler body so
    # a successful synthesis short-circuits the missing-marker path. We
    # pin the comparison to the actual `log "ralph_done_marker_missing"`
    # invocation rather than any string mention of the event name (the
    # handlers file has comments referencing the event name elsewhere,
    # including inside the inner-fallback comment block).
    local synth_line
    local missing_line
    synth_line=$(grep -n 'synthesize_ralph_output_from_done_marker "\$REPO_ROOT"' "$HANDLERS" \
        | head -1 | cut -d: -f1 || true)
    missing_line=$(grep -n '^[[:space:]]*log "ralph_done_marker_missing"' "$HANDLERS" \
        | head -1 | cut -d: -f1 || true)

    if [[ -z "$synth_line" || -z "$missing_line" ]]; then
        fail "entrypoint ordering: synth + missing-marker landmarks present" \
            "synth=$synth_line missing=$missing_line"
        return
    fi
    pass "entrypoint ordering: synth + missing-marker landmarks present"

    if (( synth_line >= missing_line )); then
        fail "synthesis runs before ralph_done_marker_missing log" \
            "synthesize_ralph_output_from_done_marker (line $synth_line) must come before ralph_done_marker_missing (line $missing_line)"
        return
    fi
    pass "synthesis runs before ralph_done_marker_missing log"
}

# ── Run tests ─────────────────────────────────────────────────────────────

run_ship_synthesis_test
run_blocked_synthesis_test
run_missing_ralph_done_test
run_ac_infeasible_synthesis_test
run_entrypoint_wires_helper_test

echo
echo "Tests: $TESTS, Failures: $FAILURES"

if (( FAILURES > 0 )); then
    exit 1
fi
exit 0
