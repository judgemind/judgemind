#!/usr/bin/env bash
# test_wait_budget_host_sleep.sh — no long-wait helper can run past its
# budget, including when the host sleeps mid-call (#4975).
#
# Background. The Bash tool's 600s cap is wall-clock time and counts time
# the laptop spends asleep, while `sleep N` (macOS) and the per-call
# watchdog's ticks count only awake time. On 2026-11-14 ecs-wait-task.sh was
# moved to the background twice because the host idle-slept (pmset: 932s and
# 558s) while the helper sat in `sleep 60`. A host sleep looks, from inside
# the helper, like one sleep or one watchdog tick taking much longer on the
# wall clock than it asked for. The dynamic cases below simulate exactly
# that with stub `sleep` binaries that oversleep once.
#
# Covers every helper that sources scripts/_wait_budget_lib.sh
# (auto-discovered, so a new helper is covered the day it is added):
#
#   1. Static guard: no raw `sleep`, and no `aws` / `gh` call outside
#      wait_budget_call. Either one is a call the budget cannot bound. A
#      line can opt out with a trailing `# wait-budget: exempt <reason>`.
#      The scanner is self-tested against a fixture first.
#   2. Host sleep during a hung call: every helper, with a hung aws/gh and
#      a watchdog tick that oversleeps, must exit soon after the simulated
#      wake (the watchdog's wall-clock stop), not after its full per-call
#      cap of awake ticks.
#   3. Host sleep during a poll sleep: ecs-wait-task.sh and wait-for-run.sh
#      must exit within one sleep slice of the simulated wake.
#   4. Library units: wait_budget_sleep slicing, the watchdog --wall-stop,
#      and the macOS caffeinate assertion (skipped where caffeinate is
#      missing).
#
# Usage:
#   scripts/tests/test_wait_budget_host_sleep.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILURES=0
TESTS=0
ARN="arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123"

# Simulated host sleep, and the budget the dynamic cases run under. A
# helper with the bug exits around BUDGET + HOST_SLEEP (it finishes its
# awake-time sleep or tick count after the wake); a fixed one exits within
# a couple of seconds of the wake, i.e. around HOST_SLEEP + 2..5s.
# The gap between the two (BUDGET - WAKE_SLACK = 10s) is what the
# assertions use to tell them apart; the slack absorbs process start-up
# under load (~17 cases run in parallel, ~40s total).
HOST_SLEEP=25
BUDGET=20
WAKE_SLACK=10  # wake -> exit: one 2s slice/tick + 2s TERM->KILL grace + load slack

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

# Helpers under test: every script that sources the budget library.
HELPERS=()
for f in "$SCRIPT_DIR"/*.sh; do
    case "$(basename "$f")" in
        _wait_budget_lib.sh) continue ;;
    esac
    if grep -q '_wait_budget_lib\.sh' "$f"; then
        HELPERS+=("$f")
    fi
done

# ── 1. Static guard ─────────────────────────────────────────────────────────

SCANNER="$TMP_ROOT/scan.py"
cat > "$SCANNER" << 'PY'
"""Flag raw `sleep` and unwrapped `aws` / `gh` calls in bash helpers.

Prints one `<file>:<line>: <cmd>: <text>` per finding; exit 1 if any.
Heuristic but conservative: quoted strings without a command
substitution are blanked first (so `echo "aws sso login"` is fine),
comments and heredoc bodies are skipped, continuation lines are joined.
"""
import re
import sys

CMD = re.compile(r"(?:^|[\s(|;&!`])(aws|gh|sleep)(?=\s|$)")
HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
EXEMPT = "wait-budget: exempt"


def logical_lines(text):
    buf, start, heredoc = "", 0, None
    for no, raw in enumerate(text.split("\n"), 1):
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        if not buf:
            start = no
        if not buf and raw.lstrip().startswith("#"):
            continue
        m = HEREDOC.search(raw)
        if raw.endswith("\\"):
            buf += raw[:-1] + " "
        else:
            yield start, buf + raw
            buf = ""
        if m:
            heredoc = m.group(1)


def blank_strings(s):
    def keep_subst(m):
        return m.group(0) if "$(" in m.group(0) else '""'
    s = re.sub(r'"(?:[^"\\]|\\.)*"', keep_subst, s)
    return re.sub(r"'[^']*'", "''", s)


def scan(path):
    findings = []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for no, line in logical_lines(text):
        if EXEMPT in line:
            continue
        s = re.sub(r"(^|\s)#.*$", "", blank_strings(line))
        for m in CMD.finditer(s):
            before = s[: m.start(1)]
            if "wait_budget_call" in before:
                continue
            if re.search(r"(command\s+-v|type|which)\s+$", before):
                continue
            if re.search(r"\b(echo|printf)\b[^|;&]*$", before):
                continue
            findings.append(f"{path}:{no}: {m.group(1)}: {line.strip()[:160]}")
    return findings


all_findings = []
for p in sys.argv[1:]:
    all_findings += scan(p)
print("\n".join(all_findings))
sys.exit(1 if all_findings else 0)
PY

# Scanner self-test: it must flag the bad lines and only those.
FIXTURE="$TMP_ROOT/fixture.sh"
cat > "$FIXTURE" << 'FIX'
#!/usr/bin/env bash
# a comment that mentions aws ecs describe-tasks and sleep 60
sleep 60
out=$(aws ecs describe-tasks --cluster c \
    --tasks t)
if gh run view 1; then :; fi
ok1=$(wait_budget_call "aws ecs describe-tasks" aws ecs describe-tasks \
    --cluster c)
wait_budget_sleep 30
echo "Fix: aws sso login, then sleep a bit"
command -v aws >/dev/null
sleep 2  # wait-budget: exempt fixture
cat > x << 'EOF'
aws s3 ls
EOF
FIX
fixture_out=$(python3 "$SCANNER" "$FIXTURE" || true)
fixture_lines=$(grep -c . <<< "$fixture_out" || true)
if [[ "$fixture_lines" -eq 3 ]] \
    && grep -q "fixture.sh:3: sleep" <<< "$fixture_out" \
    && grep -q "fixture.sh:4: aws" <<< "$fixture_out" \
    && grep -q "fixture.sh:6: gh" <<< "$fixture_out"; then
    pass "static guard: scanner flags raw sleep / unwrapped aws / gh, and nothing else in the fixture"
else
    fail "static guard: scanner self-test" "got ${fixture_lines} findings: $fixture_out"
fi

if [ "${#HELPERS[@]}" -ge 8 ]; then
    pass "static guard: found ${#HELPERS[@]} helpers that source _wait_budget_lib.sh"
else
    fail "static guard: expected >= 8 helpers that source _wait_budget_lib.sh" "got ${#HELPERS[@]}"
fi

for h in ${HELPERS[@]+"${HELPERS[@]}"}; do
    name=$(basename "$h")
    if out=$(python3 "$SCANNER" "$h"); then
        pass "static guard: $name has no raw sleep and no aws/gh call outside wait_budget_call"
    else
        fail "static guard: $name has a call the budget cannot bound" \
            "$out
  Fix: wrap the call as  wait_budget_call \"<label>\" aws ...  and use wait_budget_sleep instead of sleep
  (or add '# wait-budget: exempt <reason>' if it truly runs outside any budget)."
    fi
done

# ── Shared runner ───────────────────────────────────────────────────────────

RES_DIR="$TMP_ROOT/results"
mkdir -p "$RES_DIR"

# A case still running after CASE_MAX_SECS is killed and reported as a
# failure, so a regression fails this test instead of hanging CI.
CASE_MAX_SECS=90

# launch <id> <cmd...> — run in the background; record rc, wall secs, output.
launch() {
    local id="$1"
    shift
    (
        start=$SECONDS
        rc=0
        "$@" > "$RES_DIR/$id.out" 2>&1 &
        pid=$!
        (
            /bin/sleep "$CASE_MAX_SECS"
            echo "test harness: killed after ${CASE_MAX_SECS}s" >> "$RES_DIR/$id.out"
            pkill -KILL -P "$pid" 2>/dev/null
            kill -KILL "$pid" 2>/dev/null
        ) </dev/null >/dev/null 2>&1 &
        guard=$!
        wait "$pid" || rc=$?
        pkill -P "$guard" 2>/dev/null || true
        kill "$guard" 2>/dev/null || true
        echo "$rc $((SECONDS - start))" > "$RES_DIR/$id.rc"
    ) &
}

RUN_RC=0
RUN_OUT=""
RUN_TOOK=0
collect() {
    local line
    line=$(cat "$RES_DIR/$1.rc" 2>/dev/null || echo "missing 999")
    RUN_RC="${line%% *}"
    RUN_TOOK="${line##* }"
    RUN_OUT=$(cat "$RES_DIR/$1.out" 2>/dev/null || true)
}

# make_case <id> — a stub dir for one case; prints its path.
make_case() {
    local dir="$TMP_ROOT/$1"
    mkdir -p "$dir/bin" "$dir/state"
    echo "$dir"
}

# write_oversleep <path> <state-dir> <trigger-mode>
#   A stand-in for `sleep` that, exactly once, sleeps HOST_SLEEP seconds
#   longer than asked: what a host sleep looks like from inside the helper.
#   trigger-mode "first":   oversleep on the first call.
#   trigger-mode "trigger": oversleep on the first call after <state>/trigger
#                           exists (the hung stub creates it), so the jump
#                           lands while a call is in flight.
write_oversleep() {
    local path="$1" state="$2" mode="$3"
    cat > "$path" << STUB
#!/usr/bin/env bash
if [[ "$mode" == "first" || -e "$state/trigger" ]] && mkdir "$state/overslept" 2>/dev/null; then
    /bin/sleep \$(( \${1:-1} + $HOST_SLEEP ))
    exit 0
fi
exec /bin/sleep "\${1:-1}"
STUB
    chmod +x "$path"
}

# write_hang <path> <state-dir> — never returns; marks the trigger first.
write_hang() {
    cat > "$1" << STUB
#!/usr/bin/env bash
: > "$2/trigger"
exec /bin/sleep 300
STUB
    chmod +x "$1"
}

# ── 2. Host sleep during a hung call (every helper) ─────────────────────────
#
# Hung aws/gh + a watchdog tick that oversleeps HOST_SLEEP once. Without the
# wall-clock stop the watchdog still counts BUDGET awake ticks after the
# wake (exit ~ HOST_SLEEP + BUDGET + 2); with it the call dies on the first
# tick after the wake.

# hang_case <id> <stubbed-cmd> <helper argv...>
hang_case() {
    local id="$1" cmd="$2"
    shift 2
    local dir
    dir=$(make_case "$id")
    write_hang "$dir/bin/$cmd" "$dir/state"
    write_oversleep "$dir/tick" "$dir/state" trigger
    launch "$id" env PATH="$dir/bin:$PATH" ECS_EXEC_SLEEP_BIN="$dir/tick" \
        WAIT_BUDGET_NO_CAFFEINATE=1 AWS_CLI="$dir/bin/aws" "$@"
}

HANG_IDS=()
hang_case h_wait_for_ci gh \
    env WAIT_FOR_CI_RERUN_SENTINEL_FILE="$TMP_ROOT/ci-rerun" WAIT_FOR_CI_FLAKE_LOG="$TMP_ROOT/ci-flakes.jsonl" \
    "$SCRIPT_DIR/wait-for-ci.sh" 42 --timeout-secs "$BUDGET" --poll-interval 1
HANG_IDS+=("h_wait_for_ci:wait-for-ci.sh")
hang_case h_wait_for_run gh \
    "$SCRIPT_DIR/wait-for-run.sh" 12345 --timeout-secs "$BUDGET" --poll-interval 1
HANG_IDS+=("h_wait_for_run:wait-for-run.sh")
hang_case h_ecs_wait_task aws \
    "$SCRIPT_DIR/ecs-wait-task.sh" --timeout-secs "$BUDGET" --poll-interval 1 "$ARN"
HANG_IDS+=("h_ecs_wait_task:ecs-wait-task.sh")
hang_case h_wait_for_rollout aws \
    env ECS_CLUSTER=c ECS_SERVICE=s NEW_DEPLOYMENT_ID=ecs-svc/123 \
    ROLLOUT_TIMEOUT_SECS="$BUDGET" ROLLOUT_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/wait-for-rollout.sh"
HANG_IDS+=("h_wait_for_rollout:wait-for-rollout.sh")
hang_case h_ecs_redeploy aws \
    env ROLLOUT_TIMEOUT_SECS="$BUDGET" ROLLOUT_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/ecs-redeploy.sh" my-svc my-cluster --deployment-id ecs-svc/123
HANG_IDS+=("h_ecs_redeploy:ecs-redeploy.sh")
hang_case h_ecs_logs aws \
    env ECS_LOGS_FOLLOW_MAX_SECS="$BUDGET" ECS_LOGS_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/ecs-logs.sh" /ecs/judgemind-ingestion-worker-dev --task abc123 --follow
HANG_IDS+=("h_ecs_logs:ecs-logs.sh --follow")
# Attached launchers: the first launch call hangs (launch is budgeted too).
printf 'print("hi")\n' > "$TMP_ROOT/oneshot.py"
hang_case h_ecs_run_task aws \
    env ECS_RUN_TASK_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/ecs-run-task.sh" --timeout "$BUDGET" "$TMP_ROOT/oneshot.py"
HANG_IDS+=("h_ecs_run_task:ecs-run-task.sh (attached)")
hang_case h_run_scraper aws \
    env RUN_SCRAPER_POLL_INTERVAL=1 \
    "$SCRIPT_DIR/run-scraper.sh" --timeout "$BUDGET" some-scraper
HANG_IDS+=("h_run_scraper:run-scraper.sh (attached)")

# ── 3. Host sleep during a poll sleep ───────────────────────────────────────
#
# The status call answers "still running" at once; the first `sleep` the
# helper runs oversleeps HOST_SLEEP. A helper parked in one long `sleep`
# exits ~ BUDGET + HOST_SLEEP; a sliced one exits ~ 2 + HOST_SLEEP.

dir=$(make_case p_ecs_wait_task)
cat > "$dir/bin/aws" << 'STUB'
#!/usr/bin/env bash
echo '{"tasks":[{"lastStatus":"RUNNING","containers":[{"name":"worker"}]}]}'
STUB
chmod +x "$dir/bin/aws"
write_oversleep "$dir/bin/sleep" "$dir/state" first
launch p_ecs_wait_task env PATH="$dir/bin:$PATH" WAIT_BUDGET_NO_CAFFEINATE=1 \
    "$SCRIPT_DIR/ecs-wait-task.sh" --timeout-secs "$BUDGET" --poll-interval 60 "$ARN"

dir=$(make_case p_wait_for_run)
cat > "$dir/bin/gh" << 'STUB'
#!/usr/bin/env bash
echo "in_progress "
STUB
chmod +x "$dir/bin/gh"
write_oversleep "$dir/bin/sleep" "$dir/state" first
launch p_wait_for_run env PATH="$dir/bin:$PATH" WAIT_BUDGET_NO_CAFFEINATE=1 \
    "$SCRIPT_DIR/wait-for-run.sh" 12345 --timeout-secs "$BUDGET" --poll-interval 60

# ── 4. Library units ────────────────────────────────────────────────────────

LIB_PROBE="$TMP_ROOT/lib_probe.sh"
cat > "$LIB_PROBE" << PROBE
#!/usr/bin/env bash
set -euo pipefail
source "$SCRIPT_DIR/_wait_budget_lib.sh"
case "\$1" in
    sleep)
        wait_budget_init 12
        wait_budget_sleep 60
        echo "slept-until=\$SECONDS"
        ;;
    wallstop)
        rc=0
        ecs_exec_run_with_timeout --wall-stop \$((SECONDS + 2)) 100 /bin/sleep 3600 || rc=\$?
        echo "rc=\$rc kill=\$ECS_EXEC_LAST_KILL"
        ;;
    ticks)
        rc=0
        ecs_exec_run_with_timeout 1 /bin/sleep 3600 || rc=\$?
        echo "rc=\$rc kill=\$ECS_EXEC_LAST_KILL"
        ;;
    caffeinate)
        wait_budget_init 30
        /bin/sleep 1
        if pgrep -P \$\$ caffeinate >/dev/null 2>&1; then
            echo "caffeinate-running"
        else
            echo "no-caffeinate"
        fi
        ;;
esac
PROBE
chmod +x "$LIB_PROBE"

dir=$(make_case l_sleep)
write_oversleep "$dir/bin/sleep" "$dir/state" first
launch l_sleep env PATH="$dir/bin:$PATH" "$LIB_PROBE" sleep
launch l_wallstop "$LIB_PROBE" wallstop
launch l_ticks "$LIB_PROBE" ticks

wait

# ── Assertions ──────────────────────────────────────────────────────────────

MAX_AFTER_WAKE=$((HOST_SLEEP + WAKE_SLACK))

for entry in "${HANG_IDS[@]}"; do
    id="${entry%%:*}"
    label="${entry#*:}"
    collect "$id"
    if [[ "$RUN_TOOK" -le "$MAX_AFTER_WAKE" ]]; then
        pass "hung call + host sleep: $label exits ${RUN_TOOK}s after start (wake at ~$((HOST_SLEEP + 1))s, limit ${MAX_AFTER_WAKE}s)"
    else
        fail "hung call + host sleep: $label must exit within ${MAX_AFTER_WAKE}s" \
            "took ${RUN_TOOK}s rc=$RUN_RC output=$RUN_OUT"
    fi
    if [[ "$RUN_RC" -ne 0 ]]; then
        pass "hung call + host sleep: $label reports a non-zero exit (rc=$RUN_RC)"
    else
        fail "hung call + host sleep: $label reports a non-zero exit" "output=$RUN_OUT"
    fi
    if grep -q "timed out: .*killed at the budget deadline" <<< "$RUN_OUT"; then
        pass "hung call + host sleep: $label says the call was killed at the wall-clock deadline"
    else
        fail "hung call + host sleep: $label says the call was killed at the wall-clock deadline" \
            "output=$RUN_OUT"
    fi
done

for entry in "p_ecs_wait_task:ecs-wait-task.sh" "p_wait_for_run:wait-for-run.sh"; do
    id="${entry%%:*}"
    label="${entry#*:}"
    collect "$id"
    if [[ "$RUN_RC" == "124" ]]; then
        pass "host sleep during poll sleep: $label exits 124 (still running)"
    else
        fail "host sleep during poll sleep: $label exits 124" "rc=$RUN_RC output=$RUN_OUT"
    fi
    if [[ "$RUN_TOOK" -le "$MAX_AFTER_WAKE" ]]; then
        pass "host sleep during poll sleep: $label exits ${RUN_TOOK}s after start (limit ${MAX_AFTER_WAKE}s)"
    else
        fail "host sleep during poll sleep: $label must exit within ${MAX_AFTER_WAKE}s" \
            "took ${RUN_TOOK}s output=$RUN_OUT"
    fi
    if grep -q "host asleep:" <<< "$RUN_OUT"; then
        pass "host sleep during poll sleep: $label logs the host sleep"
    else
        fail "host sleep during poll sleep: $label logs the host sleep" "output=$RUN_OUT"
    fi
done

collect l_sleep
# Budget 12: a single long sleep would end at 12 + HOST_SLEEP; one slice
# ends at 2 + HOST_SLEEP. Measured on the probe's own clock ($SECONDS), so
# process start-up under load does not count.
slept_until=$(sed -n 's/^slept-until=//p' <<< "$RUN_OUT")
if [[ -n "$slept_until" && "$slept_until" -le $((HOST_SLEEP + 6)) ]] && grep -q "host asleep:" <<< "$RUN_OUT"; then
    pass "lib: wait_budget_sleep returns within one slice of a wake past the deadline (${slept_until}s)"
else
    fail "lib: wait_budget_sleep returns within one slice of a wake past the deadline" \
        "slept until ${slept_until:-?}s output=$RUN_OUT"
fi

collect l_wallstop
if grep -q "rc=124 kill=wall" <<< "$RUN_OUT" && [[ "$RUN_TOOK" -le 7 ]]; then
    pass "lib: --wall-stop kills a call before its tick cap and records why (${RUN_TOOK}s)"
else
    fail "lib: --wall-stop kills a call before its tick cap" "took ${RUN_TOOK}s output=$RUN_OUT"
fi

collect l_ticks
if grep -q "rc=124 kill=ticks" <<< "$RUN_OUT"; then
    pass "lib: the tick cap still applies without --wall-stop"
else
    fail "lib: the tick cap still applies without --wall-stop" "output=$RUN_OUT"
fi

if command -v caffeinate >/dev/null 2>&1; then
    out=$("$LIB_PROBE" caffeinate)
    if [[ "$out" == "caffeinate-running" ]]; then
        pass "lib: wait_budget_init holds a caffeinate assertion while the helper runs"
    else
        fail "lib: wait_budget_init holds a caffeinate assertion while the helper runs" "out=$out"
    fi
    out=$(WAIT_BUDGET_NO_CAFFEINATE=1 "$LIB_PROBE" caffeinate)
    if [[ "$out" == "no-caffeinate" ]]; then
        pass "lib: WAIT_BUDGET_NO_CAFFEINATE=1 turns the assertion off"
    else
        fail "lib: WAIT_BUDGET_NO_CAFFEINATE=1 turns the assertion off" "out=$out"
    fi
else
    echo "SKIP: caffeinate not available here (macOS only)"
fi

echo ""
echo "Results: $((TESTS - FAILURES))/${TESTS} passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "${FAILURES} test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
