#!/usr/bin/env bash
# test_ensure_test_redis.sh — Tests for scripts/ensure-test-redis.sh (#5106).
#
# Scenarios without a Redis server (always run):
#   1. nothing listening -> exit 3, WARNING + Fix: docker compose up -d redis
#   2. non-local host -> exit 1, FAILED + Fix: block
#   3. unknown argument / non-numeric port -> exit 2
# Scenario against a real Redis (ENSURE_TEST_REDIS_PORT, default 6379):
#   4. prints redis://localhost:<port>/15 and exits 0
#
# Without a Redis scenario 4 SKIPs, unless REQUIRE_REDIS_TESTS=1 (CI),
# where it FAILs.
#
# Usage:
#   scripts/tests/test_ensure_test_redis.sh

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ensure-test-redis.sh"
REDIS_PORT="${ENSURE_TEST_REDIS_PORT:-6379}"

pass=0
fail=0
skip=0
report_pass() { pass=$((pass + 1)); echo "  PASS: $1"; }
report_fail() {
    fail=$((fail + 1))
    echo "  FAIL: $1" >&2
    if [ -n "${2-}" ]; then
        echo "--- output ---" >&2
        echo "$2" >&2
        echo "--- end ---" >&2
    fi
}
report_skip() { skip=$((skip + 1)); echo "  SKIP: $1"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# run [env assignments...] -- [args...]: sets $out (stdout), $err, $rc.
run() {
    local envs=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
        envs+=("$1")
        shift
    done
    [ $# -gt 0 ] && shift
    out="$(env -u ENSURE_TEST_REDIS_HOST -u ENSURE_TEST_REDIS_PORT -u ENSURE_TEST_REDIS_DB \
        ${envs[@]+"${envs[@]}"} "$SCRIPT" "$@" 2>"$WORK/err")" && rc=0 || rc=$?
    err="$(cat "$WORK/err")"
}

echo "[scenario 1] nothing listening -> exit 3 with WARNING + Fix:"
run "ENSURE_TEST_REDIS_PORT=1" --
if [ "$rc" -ne 3 ]; then
    report_fail "expected exit 3 with nothing on port 1; got $rc" "$err"
elif ! echo "$err" | grep -q "WARNING: no local Redis for the real-Redis tests"; then
    report_fail "expected a WARNING" "$err"
elif ! echo "$err" | grep -q "^Fix:" || ! echo "$err" | grep -q "docker compose up -d redis"; then
    report_fail "expected a Fix: block naming 'docker compose up -d redis'" "$err"
elif [ -n "$out" ]; then
    report_fail "expected nothing on stdout; got '$out'" "$err"
elif echo "$err" | grep -q "Connection refused"; then
    report_fail "expected no raw bash connect noise" "$err"
else
    report_pass "nothing listening -> exit 3, WARNING + Fix:"
fi

echo "[scenario 2] non-local host -> exit 1, refused"
run "ENSURE_TEST_REDIS_HOST=redis.example.com" --
if [ "$rc" -ne 1 ]; then
    report_fail "expected exit 1 for a non-local host; got $rc" "$err"
elif ! echo "$err" | grep -q "is not local" || ! echo "$err" | grep -q "^Fix:"; then
    report_fail "expected 'is not local' and a Fix: block" "$err"
elif [ -n "$out" ]; then
    report_fail "expected nothing on stdout; got '$out'" "$err"
else
    report_pass "non-local host refused"
fi

echo "[scenario 3] usage errors -> exit 2"
run -- --bogus
rc_arg="$rc"
run "ENSURE_TEST_REDIS_PORT=abc" --
rc_port="$rc"
if [ "$rc_arg" -ne 2 ] || [ "$rc_port" -ne 2 ]; then
    report_fail "expected exit 2 for an unknown argument ($rc_arg) and a bad port ($rc_port)" "$err"
else
    report_pass "usage errors exit 2"
fi

echo "[scenario 4] a reachable Redis -> URL on stdout, exit 0"
run "ENSURE_TEST_REDIS_PORT=$REDIS_PORT" --
if [ "$rc" -eq 3 ]; then
    if [ "${REQUIRE_REDIS_TESTS:-0}" = "1" ]; then
        report_fail "REQUIRE_REDIS_TESTS=1 but no Redis on localhost:$REDIS_PORT" "$err"
    else
        report_skip "no Redis on localhost:$REDIS_PORT — scenario 4 not run"
    fi
elif [ "$rc" -ne 0 ]; then
    report_fail "expected exit 0; got $rc" "$err"
elif [ "$out" != "redis://localhost:$REDIS_PORT/15" ]; then
    report_fail "expected redis://localhost:$REDIS_PORT/15; got '$out'" "$err"
else
    report_pass "reachable Redis -> URL printed"
fi

echo ""
echo "ensure-test-redis.sh tests: $pass passed, $fail failed, $skip skipped"
[ "$fail" -eq 0 ]
