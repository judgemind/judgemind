#!/usr/bin/env bash
# test_wait_budget_hang.sh — a hung gh/aws call cannot hold a long-wait
# helper past its budget (#4850).
#
# Each helper is run against a stub `gh` / `aws` that never returns
# (`sleep 3600`) with a 5s budget. The helper must exit 124 or 125 within the
# budget plus 10s, and name the killed call on stderr. Also unit-tests the
# shared scripts/_wait_budget_lib.sh (timeout rc, passthrough, slow-call log).
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
        WAIT_SLOW_CALL_SECS=1
        wait_budget_call "probe slow" /bin/sleep 2
        ;;
    exhausted)
        wait_budget_exhausted && echo "exhausted-at-start" || echo "not-exhausted-at-start"
        /bin/sleep "\${PROBE_SLEEP:-2}"
        wait_budget_exhausted && echo "exhausted-late" || echo "not-exhausted-late"
        ;;
esac
PROBE
chmod +x "$LIB_PROBE"

launch lib_hang "$LIB_PROBE" hang
launch lib_ok "$LIB_PROBE" ok
launch lib_slow "$LIB_PROBE" slow
# Budget 8s: one call's worth is min(5, 8/4) = 2s, so after 7s it is spent.
launch lib_exhausted env PROBE_BUDGET=8 PROBE_SLEEP=7 "$LIB_PROBE" exhausted

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
if [[ $RUN_TOOK -le 1 ]]; then
    pass "lib: a fast call returns at once (no watchdog linger, ${RUN_TOOK}s)"
else
    fail "lib: a fast call returns at once (no watchdog linger)" "took ${RUN_TOOK}s"
fi

collect lib_slow
if grep -q "slow call: probe slow took 2s" <<< "$RUN_OUT"; then
    pass "lib: a call slower than WAIT_SLOW_CALL_SECS is logged with its duration"
else
    fail "lib: a call slower than WAIT_SLOW_CALL_SECS is logged with its duration" "out=$RUN_OUT"
fi

collect lib_exhausted
if grep -q "not-exhausted-at-start" <<< "$RUN_OUT" && grep -q "exhausted-late" <<< "$RUN_OUT" \
    && ! grep -q "not-exhausted-late" <<< "$RUN_OUT"; then
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
