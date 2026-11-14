#!/usr/bin/env bash
# test_ensure_test_db.sh — Tests for scripts/ensure-test-db.sh (#4950).
#
# Scenarios without a Postgres server (always run):
#   1. no psql on PATH -> exit 3, WARNING + Fix: docker compose up -d postgres
#   2. server unreachable -> exit 3, WARNING + Fix: block
#   3. non-local admin host -> exit 1, FAILED + Fix: block, nothing dropped
# Scenarios against a real Postgres (synthetic migrations, a temp
# "worktree" dir so the real checkout's database is never touched):
#   4. first run creates and migrates the database and prints its URL
#   5. second run reuses it (no rebuild)
#   6. a new migration rebuilds it; the new table exists
#   7. a broken migration exits 1 with FAILED + Fix:, database dropped
#   8. a database whose worktree dir is gone is pruned by the next run
#   9. --drop drops this worktree's database
#
# Postgres: ENSURE_TEST_DB_ADMIN_URL (default the docker-compose server).
# Without one scenarios 4-9 SKIP, unless REQUIRE_PG_TESTS=1 (CI), where
# they FAIL.
#
# Usage:
#   scripts/tests/test_ensure_test_db.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ensure-test-db.sh"
ADMIN_URL="${ENSURE_TEST_DB_ADMIN_URL:-postgresql://judgemind:localdev@localhost:5432/postgres}"
export ENSURE_TEST_DB_ADMIN_URL="$ADMIN_URL"

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

WORK="$(cd "$(mktemp -d)" && pwd -P)"
export APPLY_MIGRATIONS_DIR="$WORK/migrations"
mkdir -p "$APPLY_MIGRATIONS_DIR" "$WORK/wt-a" "$WORK/wt-b"
printf 'CREATE TABLE t_one (id int);\n' > "$APPLY_MIGRATIONS_DIR/1_one.sql"

admin() { psql "$ADMIN_URL" -X -v ON_ERROR_STOP=1 -Atq -c "$1"; }

cleanup() {
    for wt in "$WORK/wt-a" "$WORK/wt-b"; do
        [ -d "$wt" ] && ENSURE_TEST_DB_WORKTREE="$wt" "$SCRIPT" --drop >/dev/null 2>&1
    done
    rm -rf "$WORK"
}
trap cleanup EXIT

# run [env assignments...] -- [args...]: sets $out (stdout), $err, $rc.
run() {
    local envs=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
        envs+=("$1")
        shift
    done
    [ $# -gt 0 ] && shift
    out="$(env ${envs[@]+"${envs[@]}"} "$SCRIPT" "$@" 2>"$WORK/err")" && rc=0 || rc=$?
    err="$(cat "$WORK/err")"
}

echo "[scenario 1] no psql -> exit 3 with WARNING + Fix:"
nopsql_bin="$WORK/nopsql-bin"
mkdir -p "$nopsql_bin"
for tool in bash sed cut tr basename dirname cat git shasum sha256sum mktemp tail grep env; do
    p="$(command -v "$tool" 2>/dev/null)" && ln -sf "$p" "$nopsql_bin/$tool"
done
run "PATH=$nopsql_bin" "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
if [ "$rc" -ne 3 ]; then
    report_fail "expected exit 3 without psql; got $rc" "$err"
elif ! echo "$err" | grep -q "WARNING: no local Postgres"; then
    report_fail "expected a WARNING" "$err"
elif ! echo "$err" | grep -q "^Fix:" || ! echo "$err" | grep -q "docker compose up -d postgres"; then
    report_fail "expected a Fix: block naming 'docker compose up -d postgres'" "$err"
elif [ -n "$out" ]; then
    report_fail "expected nothing on stdout; got '$out'" "$err"
else
    report_pass "no psql -> exit 3, WARNING + Fix:"
fi

echo "[scenario 2] unreachable server -> exit 3 with WARNING + Fix:"
if ! command -v psql >/dev/null 2>&1; then
    report_skip "psql not installed"
else
    run "ENSURE_TEST_DB_ADMIN_URL=postgresql://judgemind:localdev@127.0.0.1:1/postgres" \
        "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
    if [ "$rc" -ne 3 ]; then
        report_fail "expected exit 3 for an unreachable server; got $rc" "$err"
    elif ! echo "$err" | grep -q "cannot connect to postgresql://\*\*\*@127.0.0.1:1/postgres"; then
        report_fail "expected the masked URL in the WARNING" "$err"
    elif ! echo "$err" | grep -q "^Fix:"; then
        report_fail "expected a Fix: block" "$err"
    else
        report_pass "unreachable server -> exit 3, WARNING + Fix:"
    fi
fi

echo "[scenario 3] non-local admin host -> exit 1, refused"
if ! command -v psql >/dev/null 2>&1; then
    report_skip "psql not installed"
else
    run "ENSURE_TEST_DB_ADMIN_URL=postgresql://u:p@db.example.com:5432/postgres" \
        "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
    if [ "$rc" -ne 1 ]; then
        report_fail "expected exit 1 for a non-local host; got $rc" "$err"
    elif ! echo "$err" | grep -q "is not local" || ! echo "$err" | grep -q "^Fix:"; then
        report_fail "expected 'is not local' and a Fix: block" "$err"
    else
        report_pass "non-local admin host refused"
    fi
fi

pg_ok=false
if command -v psql >/dev/null 2>&1 && PGCONNECT_TIMEOUT=3 admin "SELECT 1" >/dev/null 2>&1; then
    pg_ok=true
fi

if [ "$pg_ok" != true ]; then
    if [ "${REQUIRE_PG_TESTS:-0}" = "1" ]; then
        report_fail "REQUIRE_PG_TESTS=1 but no Postgres at $ADMIN_URL"
    else
        report_skip "no Postgres at $ADMIN_URL — scenarios 4-9 not run"
    fi
else
    q() { psql "$1" -X -v ON_ERROR_STOP=1 -Atq -c "$2"; }

    echo "[scenario 4] first run creates and migrates"
    run "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
    url_a="$out"
    db_a="${url_a##*/}"
    if [ "$rc" -ne 0 ]; then
        report_fail "expected exit 0; got $rc" "$err"
    elif ! echo "$err" | grep -q "creating jm_prepush_wt_a_"; then
        report_fail "expected 'creating jm_prepush_wt_a_<hash>'" "$err"
    elif [ "$(q "$url_a" "SELECT count(*) FROM pgmigrations WHERE name = '1_one'" 2>&1)" != "1" ]; then
        report_fail "expected 1_one applied in $url_a" "$err"
    elif ! admin "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = '$db_a'" \
            | grep -q "worktree=$WORK/wt-a$"; then
        report_fail "expected the database comment to record the worktree" "$err"
    else
        report_pass "first run created and migrated $db_a"
    fi

    echo "[scenario 5] second run reuses"
    q "$url_a" "INSERT INTO t_one VALUES (42)" >/dev/null
    run "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
    if [ "$rc" -ne 0 ] || [ "$out" != "$url_a" ]; then
        report_fail "expected exit 0 and the same URL" "$err"
    elif ! echo "$err" | grep -q "reusing $db_a"; then
        report_fail "expected 'reusing $db_a'" "$err"
    elif [ "$(q "$url_a" "SELECT count(*) FROM t_one")" != "1" ]; then
        report_fail "expected the database to be reused, not rebuilt" "$err"
    else
        report_pass "second run reused the database"
    fi

    echo "[scenario 6] a new migration rebuilds"
    printf 'CREATE TABLE t_two (id int);\n' > "$APPLY_MIGRATIONS_DIR/2_two.sql"
    run "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
    if [ "$rc" -ne 0 ]; then
        report_fail "expected exit 0; got $rc" "$err"
    elif ! echo "$err" | grep -q "rebuilding $db_a"; then
        report_fail "expected 'rebuilding $db_a'" "$err"
    elif [ "$(q "$url_a" "SELECT to_regclass('public.t_two') IS NOT NULL")" != "t" ]; then
        report_fail "expected t_two to exist after the rebuild" "$err"
    elif [ "$(q "$url_a" "SELECT count(*) FROM t_one")" != "0" ]; then
        report_fail "expected a fresh database (t_one empty) after the rebuild" "$err"
    else
        report_pass "a new migration rebuilt the database"
    fi

    echo "[scenario 7] a broken migration -> exit 1, FAILED + Fix:, database dropped"
    printf 'SELECT * FROM no_such_table;\n' > "$APPLY_MIGRATIONS_DIR/3_broken.sql"
    run "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
    exists="$(admin "SELECT count(*) FROM pg_database WHERE datname = '$db_a'")"
    if [ "$rc" -ne 1 ]; then
        report_fail "expected exit 1 for a broken migration; got $rc" "$err"
    elif ! echo "$err" | grep -q "FAILED: migrations did not apply"; then
        report_fail "expected 'FAILED: migrations did not apply'" "$err"
    elif ! echo "$err" | grep -q "no_such_table"; then
        report_fail "expected the psql error in the output" "$err"
    elif ! echo "$err" | grep -q "^Fix:"; then
        report_fail "expected a Fix: block" "$err"
    elif [ "$exists" != "0" ]; then
        report_fail "expected the half-built database to be dropped" "$err"
    elif [ -n "$out" ]; then
        report_fail "expected no URL on stdout; got '$out'" "$err"
    else
        report_pass "broken migration -> exit 1, FAILED + Fix:, dropped"
    fi
    rm -f "$APPLY_MIGRATIONS_DIR/3_broken.sql"

    echo "[scenario 8] a removed worktree's database is pruned"
    run "ENSURE_TEST_DB_WORKTREE=$WORK/wt-a" --
    db_a="${out##*/}"
    rmdir "$WORK/wt-a"
    run "ENSURE_TEST_DB_WORKTREE=$WORK/wt-b" --
    url_b="$out"
    gone="$(admin "SELECT count(*) FROM pg_database WHERE datname = '$db_a'")"
    if [ "$rc" -ne 0 ]; then
        report_fail "expected wt-b to provision; got $rc" "$err"
    elif [ "$gone" != "0" ]; then
        report_fail "expected $db_a to be pruned once wt-a was removed" "$err"
    elif ! echo "$err" | grep -q "dropping $db_a (worktree $WORK/wt-a no longer exists)"; then
        report_fail "expected the prune message" "$err"
    else
        report_pass "removed worktree's database pruned"
    fi

    echo "[scenario 9] --drop"
    db_b="${url_b##*/}"
    run "ENSURE_TEST_DB_WORKTREE=$WORK/wt-b" -- --drop
    gone="$(admin "SELECT count(*) FROM pg_database WHERE datname = '$db_b'")"
    if [ "$rc" -ne 0 ] || [ "$gone" != "0" ]; then
        report_fail "expected --drop to drop $db_b" "$err"
    else
        report_pass "--drop dropped the database"
    fi
fi

echo ""
echo "ensure-test-db.sh tests: $pass passed, $fail failed, $skip skipped"
[ "$fail" -eq 0 ]
