#!/usr/bin/env bash
# test_wait_budget_hang.sh — a hung gh/aws call cannot hold a long-wait
# helper past its budget (#4850).
#
# Each helper is run against a stub `gh` / `aws` that never returns
# (`sleep 3600`) with a 5s budget. The helper must exit 124 or 125 within the
# budget plus 10s, and name the killed call on stderr. Also unit-tests the
# shared scripts/_wait_budget_lib.sh (timeout rc, passthrough, slow-call log).
#
# The library units do not measure real elapsed time against tight margins
# (#5083): the budget and slow-call arithmetic run on a fake clock
# (WAIT_BUDGET_CLOCK_FILE, advanced by a stub command), and the watchdog
# tick is a stub (ECS_EXEC_SLEEP_BIN) so a hang or a lingering watchdog
# shows up as tens of seconds, not one. The helper cases below are
# end-to-end and keep real ticks: ~7s of work against a 15s limit.
#
# ecs-run-task.sh and run-scraper.sh are covered by their own suites
# (test_ecs_run_task.sh / test_run_scraper.sh, MOCK_DESCRIBE_HANG=1), which
# already have the full launch mocks those helpers need.
#
# Usage:
#   scripts/tests/test_wait_budget_hang.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILURES=0
TESTS=0
BUDGET=5
MAX_SECS=$((BUDGET + 10))

TMP_ROOT=$(mktemp -d)
# shellcheck disable=SC2329  # invoked via trap
cleanup() {
    rm -rf "$TMP_ROOT"
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

# make_bin <name> — a fresh stub dir; prints its path.
make_bin() {
    local dir="$TMP_ROOT/$1"
    mkdir -p "$dir/state"
    echo "$dir"
}

# write_hang_stub <dir> <cmd> — <cmd> never returns.
write_hang_stub() {
    printf '#!/usr/bin/env bash\nexec /bin/sleep 3600\n' > "$1/$2"
    chmod +x "$1/$2"
}

# assert_bounded <label> <rc> <took> <output> <call-name>
assert_bounded() {
    local label="$1" rc="$2" took="$3" output="$4" call="$5"
    if [[ "$rc" == "124" || "$rc" == "125" ]]; then
        pass "$label: exits 124/125 (got $rc)"
    else
        fail "$label: exits 124/125" "exit=$rc output=$output"
    fi
    if [[ "$took" -le "$MAX_SECS" ]]; then
        pass "$label: ends within budget + 10s (${took}s)"
    else
        fail "$label: ends within budget + 10s" "took ${took}s"
    fi
    if grep -q "timed out: ${call}" <<< "$output"; then
        pass "$label: names the killed call ($call)"
    else
        fail "$label: names the killed call ($call)" "output=$output"
    fi
}


# Every case below blocks on a hung call for ~5-10s, so they all run at once
# (in the background) and are asserted afterwards. That keeps this file at
# ~12s of wall clock inside the 10-minute shell-fast CI shard.
RES_DIR="$TMP_ROOT/results"
mkdir -p "$RES_DIR"

# launch <id> <cmd...> — run <cmd> in the background, recording its exit
# code, wall seconds and combined output under $RES_DIR.
launch() {
    local id="$1"
    shift
    (
        start=$SECONDS
        rc=0
        "$@" > "$RES_DIR/$id.out" 2>&1 || rc=$?
        echo "$rc $((SECONDS - start))" > "$RES_DIR/$id.rc"
    ) &
}

RUN_RC=0
RUN_OUT=""
RUN_TOOK=0
# collect <id> — sets RUN_RC, RUN_TOOK and RUN_OUT from a finished launch.
collect() {
    local line
    line=$(cat "$RES_DIR/$1.rc" 2>/dev/null || echo "missing 999")
    RUN_RC="${line%% *}"
    RUN_TOOK="${line##* }"
    RUN_OUT=$(cat "$RES_DIR/$1.out" 2>/dev/null || true)
}

# ── Library probe ───────────────────────────────────────────────────────────

# Fake clock (#5083): the budget reads "now" from a per-case file, and the
# `advance` stub moves it forward, so a "slow" call takes exactly the
# seconds the test says. Clocks start at 100000, well above the real
# $SECONDS, so the watchdog's real-clock --wall-stop never fires on them.
ADVANCE="$TMP_ROOT/advance"
cat > "$ADVANCE" << 'STUB'
#!/usr/bin/env bash
# advance <secs> — move the fake clock forward by <secs>.
read -r now < "$WAIT_BUDGET_CLOCK_FILE"
echo $((now + $1)) > "$WAIT_BUDGET_CLOCK_FILE"
STUB
chmod +x "$ADVANCE"

# new_clock <name> — a fresh fake clock file at 100000; prints its path.
new_clock() {
    echo 100000 > "$TMP_ROOT/clock_$1"
    echo "$TMP_ROOT/clock_$1"
}

# Watchdog tick stubs. A fast tick lets a hung call reach its 5-tick cap in
# ~2.5s instead of 5s; a slow tick makes a watchdog that lingers after its
# call returns cost 30s instead of under one second, so "returns at once"
# can be asserted with a wide margin. `exec` so the watchdog's pkill hits
# the sleep itself.
FAST_TICK="$TMP_ROOT/fast_tick"
printf '#!/usr/bin/env bash\nexec /bin/sleep 0.5\n' > "$FAST_TICK"
SLOW_TICK="$TMP_ROOT/slow_tick"
printf '#!/usr/bin/env bash\nexec /bin/sleep 30\n' > "$SLOW_TICK"
chmod +x "$FAST_TICK" "$SLOW_TICK"

LIB_PROBE="$TMP_ROOT/lib_probe.sh"
cat > "$LIB_PROBE" << PROBE
#!/usr/bin/env bash
set -euo pipefail
source "$SCRIPT_DIR/_wait_budget_lib.sh"
wait_budget_init "\${PROBE_BUDGET:-3}"
case "\$1" in
    hang)
        rc=0
        # A grandchild that outlives the killed call must not block \$(...).
        out=\$(wait_budget_call "probe hang" bash -c 'echo partial; (/bin/sleep 3600) ; true') || rc=\$?
        echo "rc=\$rc out=\$out"
        ;;
    ok)
        rc=0
        out=\$(wait_budget_call "probe ok" bash -c 'echo hello; exit 7') || rc=\$?
        echo "rc=\$rc out=\$out"
        ;;
    slow)
        # Budget 3s: the per-call cap is min(60, 3) floored at 5 = 5s.
        WAIT_SLOW_CALL_SECS=1
        wait_budget_call "probe slow" "$ADVANCE" 2
        ;;
    asleep)
        # 12s on the clock with a 5s cap and no kill: the host slept.
        WAIT_SLOW_CALL_SECS=1
        wait_budget_call "probe asleep" "$ADVANCE" 12
        ;;
    fast)
        WAIT_SLOW_CALL_SECS=1
        wait_budget_call "probe fast" "$ADVANCE" 0
        echo "fast-done"
        ;;
    exhausted)
        # Budget 8s: one call's worth is min(5, 8/4) = 2s.
        wait_budget_exhausted && echo "exhausted-at-start" || echo "not-exhausted-at-start"
        "$ADVANCE" 6
        wait_budget_exhausted && echo "exhausted-at-6" || echo "not-exhausted-at-6"
        "$ADVANCE" 1
        wait_budget_exhausted && echo "exhausted-at-7" || echo "not-exhausted-at-7"
        ;;
esac
PROBE
chmod +x "$LIB_PROBE"

launch lib_hang env ECS_EXEC_SLEEP_BIN="$FAST_TICK" "$LIB_PROBE" hang
launch lib_ok env ECS_EXEC_SLEEP_BIN="$SLOW_TICK" "$LIB_PROBE" ok
launch lib_slow env WAIT_BUDGET_CLOCK_FILE="$(new_clock slow)" "$LIB_PROBE" slow
launch lib_asleep env WAIT_BUDGET_CLOCK_FILE="$(new_clock asleep)" "$LIB_PROBE" asleep
launch lib_fast env WAIT_BUDGET_CLOCK_FILE="$(new_clock fast)" "$LIB_PROBE" fast
launch lib_exhausted env WAIT_BUDGET_CLOCK_FILE="$(new_clock exhausted)" PROBE_BUDGET=8 \
    "$LIB_PROBE" exhausted

# ── Helper cases ────────────────────────────────────────────────────────────

# wait-for-ci.sh: the head-SHA lookup hangs.
BIN=$(make_bin ci_all)
write_hang_stub "$BIN" gh
launch ci_all env PATH="$BIN:$PATH" \
    WAIT_FOR_CI_RERUN_SENTINEL_FILE="$BIN/state/rerun" \
    WAIT_FOR_CI_FLAKE_LOG="$BIN/state/flakes.jsonl" \
    "$SCRIPT_DIR/wait-for-ci.sh" 42 --timeout-secs "$BUDGET" --poll-interval 1

# wait-for-ci.sh: the SHA resolves, then the check-runs read hangs.
BIN=$(make_bin ci_checks)
cat > "$BIN/gh" << 'STUB'
#!/usr/bin/env bash
case "$*" in
    *"pr view"*) echo "abc123def456"; exit 0 ;;
esac
exec /bin/sleep 3600
STUB
chmod +x "$BIN/gh"
launch ci_checks env PATH="$BIN:$PATH" \
    WAIT_FOR_CI_RERUN_SENTINEL_FILE="$BIN/state/rerun" \
    WAIT_FOR_CI_FLAKE_LOG="$BIN/state/flakes.jsonl" \
    "$SCRIPT_DIR/wait-for-ci.sh" 42 --timeout-secs "$BUDGET" --poll-interval 1

BIN=$(make_bin run)
write_hang_stub "$BIN" gh
launch run env PATH="$BIN:$PATH" \
    "$SCRIPT_DIR/wait-for-run.sh" 12345 --timeout-secs "$BUDGET" --poll-interval 1

BIN=$(make_bin wait_task)
write_hang_stub "$BIN" aws
launch wait_task env PATH="$BIN:$PATH" \
    "$SCRIPT_DIR/ecs-wait-task.sh" --timeout-secs "$BUDGET" --poll-interval 1 \
    arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123

BIN=$(make_bin rollout)
write_hang_stub "$BIN" aws
launch rollout env PATH="$BIN:$PATH" AWS_CLI="$BIN/aws" \
    ECS_CLUSTER=c ECS_SERVICE=s NEW_DEPLOYMENT_ID=ecs-svc/123 \
    ROLLOUT_TIMEOUT_SECS="$BUDGET" ROLLOUT_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/wait-for-rollout.sh"

BIN=$(make_bin redeploy_attach)
write_hang_stub "$BIN" aws
launch redeploy_attach env PATH="$BIN:$PATH" ROLLOUT_TIMEOUT_SECS="$BUDGET" ROLLOUT_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/ecs-redeploy.sh" my-svc my-cluster --deployment-id ecs-svc/123

BIN=$(make_bin redeploy_update)
write_hang_stub "$BIN" aws
launch redeploy_update env PATH="$BIN:$PATH" ROLLOUT_TIMEOUT_SECS="$BUDGET" \
    "$SCRIPT_DIR/ecs-redeploy.sh" my-svc my-cluster

# ecs-logs.sh --follow: the stream lookup and first fetch work, later
# fetches hang.
BIN=$(make_bin logs_follow)
cat > "$BIN/aws" << 'STUB'
#!/usr/bin/env bash
state="$(dirname "$0")/state"
case "$*" in
    *"describe-log-streams"*)
        printf 'oneshot/oneshot/abc123\n'
        exit 0
        ;;
    *"get-log-events"*)
        n=$(cat "$state/gle.count" 2>/dev/null || echo 0)
        n=$((n + 1)); echo "$n" > "$state/gle.count"
        if [[ "$n" -eq 1 ]]; then
            echo '{"events":[{"message":"first-line"}],"nextForwardToken":"f/1"}'
            exit 0
        fi
        exec /bin/sleep 3600
        ;;
esac
exit 0
STUB
chmod +x "$BIN/aws"
launch logs_follow env PATH="$BIN:$PATH" ECS_LOGS_FOLLOW_MAX_SECS="$BUDGET" ECS_LOGS_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/ecs-logs.sh" /ecs/judgemind-ingestion-worker-dev --task abc123 --follow

wait

# ── Library assertions ──────────────────────────────────────────────────────

collect lib_hang
# Fast ticks: the 5-tick cap fires after ~2.5s. A grandchild holding the
# capture pipe would block for its full 3600s.
if grep -q "rc=124 out=partial" <<< "$RUN_OUT" && [[ $RUN_TOOK -le 10 ]]; then
    pass "lib: a hung call returns 124 with its partial output, in time (${RUN_TOOK}s)"
else
    fail "lib: a hung call returns 124 with its partial output, in time" "took=${RUN_TOOK}s out=$RUN_OUT"
fi
if grep -q "timed out: probe hang" <<< "$RUN_OUT"; then
    pass "lib: a killed call is logged by name"
else
    fail "lib: a killed call is logged by name" "out=$RUN_OUT"
fi

collect lib_ok
if grep -q "rc=7 out=hello" <<< "$RUN_OUT"; then
    pass "lib: a normal call passes its output and exit status through"
else
    fail "lib: a normal call passes its output and exit status through" "out=$RUN_OUT"
fi
# Slow ticks: a watchdog left running after the call would hold the
# capture for its 30s tick.
if [[ $RUN_TOOK -le 10 ]]; then
    pass "lib: a fast call returns at once (no watchdog linger through a 30s tick, ${RUN_TOOK}s)"
else
    fail "lib: a fast call returns at once (no watchdog linger through a 30s tick)" "took ${RUN_TOOK}s"
fi

collect lib_slow
if grep -q "slow call: probe slow took 2s (per-call limit 5s)" <<< "$RUN_OUT"; then
    pass "lib: a call slower than WAIT_SLOW_CALL_SECS is logged with its duration"
else
    fail "lib: a call slower than WAIT_SLOW_CALL_SECS is logged with its duration" "out=$RUN_OUT"
fi

collect lib_asleep
if grep -q "slow call: probe asleep took 12s wall, longer than its 5s per-call limit without being killed" <<< "$RUN_OUT"; then
    pass "lib: a call past its cap without a kill is logged as a host sleep"
else
    fail "lib: a call past its cap without a kill is logged as a host sleep" "out=$RUN_OUT"
fi

collect lib_fast
if grep -q "fast-done" <<< "$RUN_OUT" && ! grep -q "slow call" <<< "$RUN_OUT"; then
    pass "lib: a call under WAIT_SLOW_CALL_SECS is not logged"
else
    fail "lib: a call under WAIT_SLOW_CALL_SECS is not logged" "out=$RUN_OUT"
fi

collect lib_exhausted
if grep -q "not-exhausted-at-start" <<< "$RUN_OUT" && grep -q "not-exhausted-at-6" <<< "$RUN_OUT" \
    && grep -q "^exhausted-at-7" <<< "$RUN_OUT"; then
    pass "lib: exhausted once less than one call's worth of budget is left"
else
    fail "lib: exhausted once less than one call's worth of budget is left" "out=$RUN_OUT"
fi

# The slow-call log must be greppable in the helpers the issue names.
if grep -q "slow call" "$SCRIPT_DIR/_wait_budget_lib.sh" \
    && grep -q "slow call" "$SCRIPT_DIR/wait-for-ci.sh"; then
    pass "slow-call logging is documented in the lib and wait-for-ci.sh"
else
    fail "slow-call logging is documented in the lib and wait-for-ci.sh"
fi

# ── Helper assertions ───────────────────────────────────────────────────────

collect ci_all
assert_bounded "wait-for-ci (head-SHA lookup hangs)" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "gh pr view"

collect ci_checks
assert_bounded "wait-for-ci (check-runs read hangs)" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "gh api check-runs"

collect run
assert_bounded "wait-for-run" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "gh run view"

collect wait_task
assert_bounded "ecs-wait-task" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "aws ecs describe-tasks"

collect rollout
assert_bounded "wait-for-rollout" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "aws ecs describe-services"

collect redeploy_attach
assert_bounded "ecs-redeploy --deployment-id" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "aws ecs describe-services"

collect redeploy_update
assert_bounded "ecs-redeploy (update-service hangs)" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "aws ecs update-service"
if grep -q -- "--deployment-id" <<< "$RUN_OUT"; then
    pass "ecs-redeploy (update-service hangs): points at --deployment-id, not a blind re-run"
else
    fail "ecs-redeploy (update-service hangs): points at --deployment-id, not a blind re-run" "output=$RUN_OUT"
fi

collect logs_follow
assert_bounded "ecs-logs --follow" "$RUN_RC" "$RUN_TOOK" "$RUN_OUT" "aws logs get-log-events"
if grep -q "first-line" <<< "$RUN_OUT"; then
    pass "ecs-logs --follow: still prints the events it did fetch"
else
    fail "ecs-logs --follow: still prints the events it did fetch" "output=$RUN_OUT"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/${TESTS} passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "${FAILURES} test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
