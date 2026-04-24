#!/usr/bin/env bash
# test_dispatcher_helpers_agent_timeline.sh — Tests for
# scripts/dispatcher/helpers/agent-timeline.sh (#3147).
#
# Exercises the new ECS-mode sections added by #3147:
#   * ECS agent-runner info block (task_arn + exec-cmd + CPU/mem).
#   * Ralph iterations block (one row per dispatcher.ralph_patches
#     entry for the agent).
#   * Graceful degradation when metrics or iterations are missing.
#   * Subprocess-mode agents produce no new sections (existing output
#     unchanged).
#
# Strategy: swap the helper's dependencies with stubs by prepending a
# shim PATH. The helper looks up ``aws`` via ``command -v aws``; we put
# a stub ``aws`` earlier in PATH and point the helper at a stub
# ``dev-db-query.sh`` by copying the helper into a scratch tree with a
# replaced path.
#
# bash 3.2 compatible (no `mapfile`, `declare -A`, namerefs, process
# substitution into arrays).
#
# Usage:
#   scripts/tests/test_dispatcher_helpers_agent_timeline.sh
#
# Exit codes:
#   0 — all tests passed.
#   1 — one or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HELPER="$REPO_ROOT/scripts/dispatcher/helpers/agent-timeline.sh"
QUERY_LIB="$REPO_ROOT/scripts/dispatcher/helpers/_query_lib.sh"

if [[ ! -x "$HELPER" ]]; then
    echo "FATAL: $HELPER missing or not executable" >&2
    exit 1
fi

FAILURES=0
TESTS=0
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
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
        echo "---DETAIL---"
        echo "$2"
        echo "---END---"
    fi
}

# Copy the helper into a scratch directory with its `dev-db-query.sh`
# path redirected to a stub. Bash-3.2 safe — no associative arrays or
# process substitution into mapfile.
stage_helper() {
    local stub_db="$1"
    local scratch="$TMPDIR_TEST/helpers"
    mkdir -p "$scratch"
    # We can't easily rewrite the helper's dev_db path (it's derived
    # from script_dir). Simplest: lay down a fake scripts tree
    # mirroring the real layout so $dev_db resolves to our stub.
    mkdir -p "$scratch/scripts/dispatcher/helpers"
    cp "$HELPER" "$scratch/scripts/dispatcher/helpers/agent-timeline.sh"
    cp "$QUERY_LIB" "$scratch/scripts/dispatcher/helpers/_query_lib.sh"
    cp "$stub_db" "$scratch/scripts/dev-db-query.sh"
    chmod +x "$scratch/scripts/dispatcher/helpers/agent-timeline.sh"
    chmod +x "$scratch/scripts/dev-db-query.sh"
    echo "$scratch/scripts/dispatcher/helpers/agent-timeline.sh"
}

# Emit a dev-db-query.sh stub at $1 that returns the timeline JSON
# payload at $2 for the ``agent-timeline fetch`` call, and a
# single-row array-of-one for the ``agent-timeline resolve`` call.
#
# The resolve query matches on `LIKE 'prefix%'` so the stub echoes a
# deterministic `agent_id` row whose prefix matches anything.
make_stub_db() {
    local out="$1"
    local fetch_payload="$2"
    local resolve_payload="$3"
    # Pretty-print the JSON so the closing `]` is on its own line —
    # the real dev-db-query.sh output is pretty-printed, and
    # _query_lib.sh's `_query_lib_json_only` awk keys on `^[` / `^]`
    # anchored at column 0.
    local fetch_pretty
    fetch_pretty=$(printf '%s' "$fetch_payload" | jq .)
    local resolve_pretty
    resolve_pretty=$(printf '%s' "$resolve_payload" | jq .)
    cat >"$out" <<EOF
#!/usr/bin/env bash
# Stub dev-db-query.sh for agent-timeline tests.
sql="\$1"
ssm_banner() {
    echo "Starting session with SessionId: ecs-execute-command-stub"
}
ssm_trailer() {
    echo ""
    echo "Exiting session with sessionId: ecs-execute-command-stub"
}
if [[ "\$sql" == *"FROM dispatcher.agents WHERE agent_id::text LIKE"* ]]; then
    ssm_banner
    cat <<'__JSON__'
${resolve_pretty}
__JSON__
    ssm_trailer
    exit 0
fi
if [[ "\$sql" == *"jsonb_build_object"* ]]; then
    ssm_banner
    cat <<'__JSON__'
${fetch_pretty}
__JSON__
    ssm_trailer
    exit 0
fi
echo "[]"
EOF
    chmod +x "$out"
}

# Emit an `aws` stub that returns the given JSON payload for a
# `cloudwatch get-metric-data` invocation. Use "EMPTY" to get an
# empty-results response.
make_stub_aws() {
    local out="$1"
    local mode="$2"    # "full" | "empty" | "error"
    cat >"$out" <<'PREAMBLE'
#!/usr/bin/env bash
# Stub aws CLI for agent-timeline tests.
# Only handles `cloudwatch get-metric-data`; anything else exits 0 quietly.
if [[ "$1" != "cloudwatch" || "$2" != "get-metric-data" ]]; then
    exit 0
fi
PREAMBLE
    case "$mode" in
        full)
            cat >>"$out" <<'EOF'
cat <<'__JSON__'
{
  "MetricDataResults": [
    {"Id": "cpu", "Label": "CpuUtilized", "Values": [50.0, 70.0, 90.0]},
    {"Id": "mem", "Label": "MemoryUtilized", "Values": [200.0, 220.0, 240.0]}
  ]
}
__JSON__
EOF
            ;;
        empty)
            cat >>"$out" <<'EOF'
cat <<'__JSON__'
{
  "MetricDataResults": [
    {"Id": "cpu", "Label": "CpuUtilized", "Values": []},
    {"Id": "mem", "Label": "MemoryUtilized", "Values": []}
  ]
}
__JSON__
EOF
            ;;
        error)
            cat >>"$out" <<'EOF'
echo "An error occurred" >&2
exit 1
EOF
            ;;
    esac
    chmod +x "$out"
}

# Run the helper with PATH pre-pended to hit our aws stub.
run_helper() {
    local helper_path="$1"
    local prefix="$2"
    local aws_stub="$3"          # empty string → no aws shim (use
                                 # AGENT_TIMELINE_SKIP_METRICS=1)
    local path_prefix=""
    if [[ -n "$aws_stub" ]]; then
        local bin_dir="$TMPDIR_TEST/bin"
        mkdir -p "$bin_dir"
        cp "$aws_stub" "$bin_dir/aws"
        chmod +x "$bin_dir/aws"
        path_prefix="$bin_dir:"
    fi
    PATH="${path_prefix}$PATH" \
        AGENT_TIMELINE_SKIP_METRICS="${AGENT_TIMELINE_SKIP_METRICS:-}" \
        bash "$helper_path" "$prefix"
}

# ─── Fixtures ────────────────────────────────────────────────────────

# A resolve payload that pretends the prefix maps to a unique agent_id.
RESOLVE_ONE='[
  {"id": "fe28f05f-0000-0000-0000-000000000000", "issue_number": 2825, "phase": "done", "status": "succeeded"}
]'

# Timeline fetch payload — ECS mode, two ralph_patches rows. The
# `ecs_metrics` key is NOT populated here — the helper splices in the
# CloudWatch result at render time.
FETCH_ECS_WITH_ITERS='[{"timeline": {
  "agent": {
    "agent_id": "fe28f05f-0000-0000-0000-000000000000",
    "issue_number": 2825,
    "pr_number": null,
    "phase": "done",
    "status": "succeeded",
    "model_override": null,
    "priority": "p1",
    "issue_title": "test task",
    "failure_summary": null,
    "execution_mode": "ecs",
    "agent_task_arn": "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/e76f7f9a96494a41aba692e4e96e8407",
    "ralph_iterations_observed": 2,
    "started_epoch": 1777003702,
    "ended_epoch": 1777005385,
    "spawned_utc": "2026-04-24T04:28:22Z",
    "spawned_pt":  "2026-04-23 21:28:22 PT",
    "ended_utc":   "2026-04-24T04:56:25Z",
    "ended_pt":    "2026-04-23 21:56:25 PT",
    "merged_utc":  null,
    "lifetime":    "0:28:02",
    "lifetime_seconds": 1682
  },
  "transitions": [],
  "failures": [],
  "diagnoses": [],
  "retry_markers": [],
  "ralph_iterations": [
    {
      "iteration_n": 1,
      "verdict": "LOOP",
      "commit_sha": "abc1234567890deadbeef",
      "commit_sha_short": "abc1234",
      "commit_subject": "refactor: extract helper",
      "created_utc": "2026-04-24T04:47:22Z",
      "created_pt":  "21:47:22 PT"
    },
    {
      "iteration_n": 2,
      "verdict": "LOOP",
      "commit_sha": "def5678901234cafebabe",
      "commit_sha_short": "def5678",
      "commit_subject": "test: cover edge case",
      "created_utc": "2026-04-24T05:03:11Z",
      "created_pt":  "22:03:11 PT"
    }
  ]
}}]'

FETCH_ECS_NO_ITERS='[{"timeline": {
  "agent": {
    "agent_id": "fe28f05f-0000-0000-0000-000000000000",
    "issue_number": 2825,
    "pr_number": null,
    "phase": "plan",
    "status": "running",
    "model_override": null,
    "priority": "p1",
    "issue_title": "test task",
    "failure_summary": null,
    "execution_mode": "ecs",
    "agent_task_arn": "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123",
    "ralph_iterations_observed": 0,
    "started_epoch": 1777003702,
    "ended_epoch": null,
    "spawned_utc": "2026-04-24T04:28:22Z",
    "spawned_pt":  "2026-04-23 21:28:22 PT",
    "ended_utc":   null,
    "ended_pt":    null,
    "merged_utc":  null,
    "lifetime":    "0:05:00 (still running)",
    "lifetime_seconds": 300
  },
  "transitions": [],
  "failures": [],
  "diagnoses": [],
  "retry_markers": [],
  "ralph_iterations": []
}}]'

FETCH_SUBPROC='[{"timeline": {
  "agent": {
    "agent_id": "01f7cb0a-0000-0000-0000-000000000000",
    "issue_number": 2663,
    "pr_number": 3140,
    "phase": "done",
    "status": "succeeded",
    "model_override": null,
    "priority": "p2",
    "issue_title": "subprocess test",
    "failure_summary": null,
    "execution_mode": "subprocess",
    "agent_task_arn": null,
    "ralph_iterations_observed": 0,
    "started_epoch": 1776981378,
    "ended_epoch": 1776986167,
    "spawned_utc": "2026-04-23T23:56:18Z",
    "spawned_pt":  "2026-04-23 16:56:18 PT",
    "ended_utc":   "2026-04-24T01:16:07Z",
    "ended_pt":    "2026-04-23 18:16:07 PT",
    "merged_utc":  "2026-04-24T01:16:07Z",
    "lifetime":    "1:19:48",
    "lifetime_seconds": 4789
  },
  "transitions": [],
  "failures": [],
  "diagnoses": [],
  "retry_markers": [],
  "ralph_iterations": []
}}]'

RESOLVE_SUBPROC='[
  {"id": "01f7cb0a-0000-0000-0000-000000000000", "issue_number": 2663, "phase": "done", "status": "succeeded"}
]'

# ─── Test 1: ECS-mode with iterations + metrics ──────────────────────
stub_db="$TMPDIR_TEST/stub_db_1.sh"
stub_aws="$TMPDIR_TEST/stub_aws_1.sh"
make_stub_db "$stub_db" "$FETCH_ECS_WITH_ITERS" "$RESOLVE_ONE"
make_stub_aws "$stub_aws" "full"
helper_path=$(stage_helper "$stub_db")
set +e
out=$(run_helper "$helper_path" "fe28f05f" "$stub_aws" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e

if [[ "$rc" == "0" ]]; then
    pass "ecs-with-iters: helper exits 0"
else
    fail "ecs-with-iters: helper exit code" "rc=$rc out=$out err=$(cat "$TMPDIR_TEST/err.log")"
fi

if echo "$out" | grep -q "── ECS agent-runner ──"; then
    pass "ecs-with-iters: ECS agent-runner section rendered"
else
    fail "ecs-with-iters: missing ECS agent-runner header" "$out"
fi

if echo "$out" | grep -q "task_id:  e76f7f9a96494a41aba692e4e96e8407"; then
    pass "ecs-with-iters: task_id extracted from ARN"
else
    fail "ecs-with-iters: task_id not extracted correctly" "$out"
fi

if echo "$out" | grep -qF "aws ecs execute-command --cluster judgemind-dev --task e76f7f9a96494a41aba692e4e96e8407 --container agent-runner --interactive --command /bin/bash"; then
    pass "ecs-with-iters: exec-cmd one-liner is complete and copy-pasteable"
else
    fail "ecs-with-iters: exec-cmd missing or wrong shape" "$out"
fi

# Values 50, 70, 90 → avg 70.0
if echo "$out" | grep -qE "task CPU: 70(\.0)? units"; then
    pass "ecs-with-iters: CPU average computed from datapoints"
else
    fail "ecs-with-iters: CPU line wrong" "$(echo "$out" | grep CPU || echo '(no CPU line)')"
fi

# Values 200, 220, 240 → avg 220.0
if echo "$out" | grep -qE "task mem: 220(\.0)? MiB"; then
    pass "ecs-with-iters: memory average computed from datapoints"
else
    fail "ecs-with-iters: mem line wrong" "$(echo "$out" | grep mem || echo '(no mem line)')"
fi

if echo "$out" | grep -q "── Ralph iterations"; then
    pass "ecs-with-iters: Ralph iterations section rendered"
else
    fail "ecs-with-iters: missing Ralph iterations section" "$out"
fi

if echo "$out" | grep -q "iter=1" && echo "$out" | grep -q "iter=2"; then
    pass "ecs-with-iters: both iterations displayed"
else
    fail "ecs-with-iters: iterations missing from output" "$out"
fi

if echo "$out" | grep -qF "refactor: extract helper"; then
    pass "ecs-with-iters: commit subject rendered"
else
    fail "ecs-with-iters: commit subject missing" "$out"
fi

if echo "$out" | grep -q "abc1234"; then
    pass "ecs-with-iters: short commit SHA rendered"
else
    fail "ecs-with-iters: short SHA missing" "$out"
fi

# ─── Test 2: ECS-mode with NO iterations ────────────────────────────
stub_db2="$TMPDIR_TEST/stub_db_2.sh"
stub_aws2="$TMPDIR_TEST/stub_aws_2.sh"
make_stub_db "$stub_db2" "$FETCH_ECS_NO_ITERS" "$RESOLVE_ONE"
make_stub_aws "$stub_aws2" "full"
helper_path2=$(stage_helper "$stub_db2")
set +e
out2=$(run_helper "$helper_path2" "fe28f05f" "$stub_aws2" 2>"$TMPDIR_TEST/err2.log")
rc2=$?
set -e

if [[ "$rc2" == "0" ]]; then
    pass "ecs-no-iters: helper exits 0"
else
    fail "ecs-no-iters: exit code" "rc=$rc2 err=$(cat "$TMPDIR_TEST/err2.log")"
fi

if echo "$out2" | grep -qF "(no iterations yet)"; then
    pass "ecs-no-iters: shows '(no iterations yet)'"
else
    fail "ecs-no-iters: missing 'no iterations yet' marker" "$out2"
fi

if echo "$out2" | grep -q "── ECS agent-runner ──"; then
    pass "ecs-no-iters: ECS section still rendered (just without iterations)"
else
    fail "ecs-no-iters: ECS section missing" "$out2"
fi

# ─── Test 3: ECS-mode with empty CloudWatch response ─────────────────
stub_db3="$TMPDIR_TEST/stub_db_3.sh"
stub_aws3="$TMPDIR_TEST/stub_aws_3.sh"
make_stub_db "$stub_db3" "$FETCH_ECS_WITH_ITERS" "$RESOLVE_ONE"
make_stub_aws "$stub_aws3" "empty"
helper_path3=$(stage_helper "$stub_db3")
set +e
out3=$(run_helper "$helper_path3" "fe28f05f" "$stub_aws3" 2>"$TMPDIR_TEST/err3.log")
rc3=$?
set -e

if [[ "$rc3" == "0" ]]; then
    pass "ecs-empty-metrics: helper exits 0 (no fatal on empty metrics)"
else
    fail "ecs-empty-metrics: exit code" "rc=$rc3 err=$(cat "$TMPDIR_TEST/err3.log")"
fi

if echo "$out3" | grep -qF "task CPU: (metrics unavailable)"; then
    pass "ecs-empty-metrics: CPU shows '(metrics unavailable)'"
else
    fail "ecs-empty-metrics: CPU not gracefully degraded" "$(echo "$out3" | grep -E 'task (CPU|mem)')"
fi

if echo "$out3" | grep -qF "task mem: (metrics unavailable)"; then
    pass "ecs-empty-metrics: memory shows '(metrics unavailable)'"
else
    fail "ecs-empty-metrics: mem not gracefully degraded" "$(echo "$out3" | grep -E 'task (CPU|mem)')"
fi

# Iterations block still works even though metrics don't.
if echo "$out3" | grep -q "iter=1"; then
    pass "ecs-empty-metrics: iterations still render despite metrics failure"
else
    fail "ecs-empty-metrics: iterations missing" "$out3"
fi

# ─── Test 4: ECS-mode with aws-cli error ─────────────────────────────
stub_db4="$TMPDIR_TEST/stub_db_4.sh"
stub_aws4="$TMPDIR_TEST/stub_aws_4.sh"
make_stub_db "$stub_db4" "$FETCH_ECS_WITH_ITERS" "$RESOLVE_ONE"
make_stub_aws "$stub_aws4" "error"
helper_path4=$(stage_helper "$stub_db4")
set +e
out4=$(run_helper "$helper_path4" "fe28f05f" "$stub_aws4" 2>"$TMPDIR_TEST/err4.log")
rc4=$?
set -e

if [[ "$rc4" == "0" ]]; then
    pass "ecs-aws-error: helper exits 0 even when aws cli errors"
else
    fail "ecs-aws-error: exit code" "rc=$rc4 err=$(cat "$TMPDIR_TEST/err4.log")"
fi

if echo "$out4" | grep -qF "(metrics unavailable)"; then
    pass "ecs-aws-error: shows '(metrics unavailable)' on aws-cli failure"
else
    fail "ecs-aws-error: metrics not gracefully degraded" "$out4"
fi

# ─── Test 5: Subprocess-mode — no new sections ───────────────────────
stub_db5="$TMPDIR_TEST/stub_db_5.sh"
make_stub_db "$stub_db5" "$FETCH_SUBPROC" "$RESOLVE_SUBPROC"
helper_path5=$(stage_helper "$stub_db5")
# No aws stub needed — helper should never invoke aws for subprocess
# mode. But to be extra safe, don't put aws on PATH at all.
set +e
out5=$(AGENT_TIMELINE_SKIP_METRICS= run_helper "$helper_path5" "01f7cb0a" "" 2>"$TMPDIR_TEST/err5.log")
rc5=$?
set -e

if [[ "$rc5" == "0" ]]; then
    pass "subprocess: helper exits 0"
else
    fail "subprocess: exit code" "rc=$rc5 err=$(cat "$TMPDIR_TEST/err5.log")"
fi

if echo "$out5" | grep -q "── ECS agent-runner ──"; then
    fail "subprocess: ECS section leaked into subprocess-mode output (should be hidden)" "$out5"
else
    pass "subprocess: no ECS agent-runner section (correct)"
fi

if echo "$out5" | grep -q "── Ralph iterations"; then
    fail "subprocess: Ralph iterations section leaked into subprocess-mode output" "$out5"
else
    pass "subprocess: no Ralph iterations section (correct)"
fi

# The existing sections should still be there unchanged.
if echo "$out5" | grep -q "── retry_markers ──"; then
    pass "subprocess: existing retry_markers section preserved"
else
    fail "subprocess: retry_markers section missing" "$out5"
fi

# ─── Test 6: Env override — AGENT_TIMELINE_SKIP_METRICS ──────────────
# Setting AGENT_TIMELINE_SKIP_METRICS should bypass the aws call for
# ECS agents — useful for offline/dry-run operator use.
stub_db6="$TMPDIR_TEST/stub_db_6.sh"
make_stub_db "$stub_db6" "$FETCH_ECS_WITH_ITERS" "$RESOLVE_ONE"
helper_path6=$(stage_helper "$stub_db6")
set +e
out6=$(AGENT_TIMELINE_SKIP_METRICS=1 run_helper "$helper_path6" "fe28f05f" "" 2>"$TMPDIR_TEST/err6.log")
rc6=$?
set -e

if [[ "$rc6" == "0" ]]; then
    pass "skip-metrics env: helper exits 0"
else
    fail "skip-metrics env: exit code" "rc=$rc6 err=$(cat "$TMPDIR_TEST/err6.log")"
fi

if echo "$out6" | grep -qF "(metrics unavailable)"; then
    pass "skip-metrics env: shows '(metrics unavailable)'"
else
    fail "skip-metrics env: metrics line wrong" "$(echo "$out6" | grep -E 'task (CPU|mem)')"
fi

# Iterations still work even when metrics are suppressed.
if echo "$out6" | grep -q "iter=1" && echo "$out6" | grep -q "iter=2"; then
    pass "skip-metrics env: iterations rendered normally"
else
    fail "skip-metrics env: iterations missing" "$out6"
fi

# ─── Summary ─────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
