#!/usr/bin/env bash
# test_apply_migrations.sh — Tests for scripts/apply_migrations.sh (#4950).
#
# Runs the script against throwaway databases in a real Postgres, with a
# synthetic migrations directory (APPLY_MIGRATIONS_DIR), and checks:
#   1. a fresh database gets every migration, recorded in pgmigrations
#   2. a migration's ALTER DATABASE ... SET search_path reaches the next
#      migration (one psql session per migration)
#   3. a second run applies nothing (re-runnable)
#   4. a new migration is applied alone, in numeric order (10 after 2)
#   5. a failing migration exits 1 and is not recorded
#   6. a database with tables but no pgmigrations is refused with a
#      Fix: block naming --recreate
#   7. --recreate drops it and applies every migration
#   8. --recreate refuses a non-local host (needs psql, not a server)
#
# Postgres: ENSURE_TEST_DB_ADMIN_URL (default the docker-compose server,
# postgresql://judgemind:localdev@localhost:5432/postgres). Without one
# the database scenarios SKIP, unless REQUIRE_PG_TESTS=1 (CI), where
# they FAIL.
#
# Usage:
#   scripts/tests/test_apply_migrations.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/apply_migrations.sh"
ADMIN_URL="${ENSURE_TEST_DB_ADMIN_URL:-postgresql://judgemind:localdev@localhost:5432/postgres}"

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
DB="jm_test_apply_migrations_$$"
DB_URL="$(echo "$ADMIN_URL" | sed -E "s#^([a-zA-Z]+://[^/]*)/[^?]*#\\1/$DB#")"

admin() { psql "$ADMIN_URL" -X -v ON_ERROR_STOP=1 -Atq -c "$1"; }
q() { psql "$DB_URL" -X -v ON_ERROR_STOP=1 -Atq -c "$1"; }

cleanup() {
    admin "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

export APPLY_MIGRATIONS_DIR="$WORK/migrations"
mkdir -p "$APPLY_MIGRATIONS_DIR"
printf 'CREATE TABLE t_a (id int);\n\n-- Down Migration\nDROP TABLE t_a;\n' > "$APPLY_MIGRATIONS_DIR/1_a.sql"
# No trailing semicolon: the script must still end the statement.
printf 'CREATE TABLE t_b (id int)' > "$APPLY_MIGRATIONS_DIR/2_b.sql"
cat > "$APPLY_MIGRATIONS_DIR/3_schema.sql" <<'SQL'
CREATE SCHEMA s;
CREATE TABLE s.t_s (id int);
DO $$ BEGIN
  EXECUTE format('ALTER DATABASE %I SET search_path = public, s', current_database());
END $$;
SQL
# Unqualified t_s resolves only in a session started after 3_schema.
printf 'INSERT INTO t_s VALUES (1);\n' > "$APPLY_MIGRATIONS_DIR/4_uses_search_path.sql"

run() { out="$("$SCRIPT" "$@" 2>&1)" && rc=0 || rc=$?; }

echo "[scenario 8] --recreate refuses a non-local host"
if ! command -v psql >/dev/null 2>&1; then
    report_skip "psql not installed"
else
    run --recreate "postgresql://u:p@db.example.com:5432/prod"
    if [ "$rc" -eq 0 ]; then
        report_fail "expected --recreate on a remote host to fail" "$out"
    elif ! echo "$out" | grep -q "only drops a local database"; then
        report_fail "expected 'only drops a local database'" "$out"
    else
        report_pass "--recreate refuses a non-local host"
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
        report_skip "no Postgres at $ADMIN_URL — database scenarios not run"
    fi
else
    admin "CREATE DATABASE \"$DB\"" >/dev/null

    echo "[scenario 1-2] fresh database: every migration applied and recorded"
    run "$DB_URL"
    names="$(q "SELECT string_agg(name, ',' ORDER BY id) FROM pgmigrations" 2>&1)"
    if [ "$rc" -ne 0 ]; then
        report_fail "expected a fresh database to migrate (search_path must reach 4_uses_search_path)" "$out"
    elif [ "$names" != "1_a,2_b,3_schema,4_uses_search_path" ]; then
        report_fail "expected pgmigrations 1_a,2_b,3_schema,4_uses_search_path; got '$names'" "$out"
    elif [ "$(q "SELECT count(*) FROM s.t_s")" != "1" ]; then
        report_fail "expected 4_uses_search_path to have inserted into s.t_s" "$out"
    elif ! echo "$out" | grep -q "applied 4, already applied 0"; then
        report_fail "expected 'applied 4, already applied 0'" "$out"
    else
        report_pass "fresh database migrated and recorded; search_path reached the next migration"
    fi

    echo "[scenario 3] second run applies nothing"
    run "$DB_URL"
    if [ "$rc" -ne 0 ] || ! echo "$out" | grep -q "applied 0, already applied 4"; then
        report_fail "expected a re-run to apply nothing" "$out"
    else
        report_pass "re-run applies nothing"
    fi

    echo "[scenario 4] a new migration is applied alone, in numeric order"
    printf 'CREATE TABLE t_c (id int);\n' > "$APPLY_MIGRATIONS_DIR/10_c.sql"
    run "$DB_URL"
    last="$(q "SELECT name FROM pgmigrations ORDER BY id DESC LIMIT 1" 2>&1)"
    if [ "$rc" -ne 0 ] || ! echo "$out" | grep -q "applied 1, already applied 4"; then
        report_fail "expected only 10_c to be applied" "$out"
    elif [ "$last" != "10_c" ]; then
        report_fail "expected 10_c recorded last; got '$last'" "$out"
    else
        report_pass "new migration applied alone"
    fi

    echo "[scenario 5] a failing migration exits 1 and is not recorded"
    printf 'CREATE TABLE t_d (id int);\nSELECT * FROM no_such_table;\n' > "$APPLY_MIGRATIONS_DIR/11_d.sql"
    run "$DB_URL"
    recorded="$(q "SELECT count(*) FROM pgmigrations WHERE name = '11_d'" 2>&1)"
    if [ "$rc" -eq 0 ]; then
        report_fail "expected a failing migration to exit non-zero" "$out"
    elif ! echo "$out" | grep -q "FAILED: 11_d.sql"; then
        report_fail "expected 'FAILED: 11_d.sql'" "$out"
    elif [ "$recorded" != "0" ]; then
        report_fail "a failed migration must not be recorded; count=$recorded" "$out"
    else
        report_pass "failing migration exits 1, not recorded"
    fi
    rm -f "$APPLY_MIGRATIONS_DIR/11_d.sql"

    echo "[scenario 6] tables but no pgmigrations: refused with a Fix: block"
    q "DROP TABLE pgmigrations" >/dev/null
    run "$DB_URL"
    if [ "$rc" -eq 0 ]; then
        report_fail "expected an untracked, non-empty database to be refused" "$out"
    elif ! echo "$out" | grep -q "no public.pgmigrations"; then
        report_fail "expected the 'no public.pgmigrations' reason" "$out"
    elif ! echo "$out" | grep -q "^Fix:"; then
        report_fail "expected a Fix: block" "$out"
    elif ! echo "$out" | grep -q "scripts/apply_migrations.sh --recreate '$DB_URL'"; then
        report_fail "expected the copy-pasteable --recreate command" "$out"
    else
        report_pass "untracked database refused with a --recreate Fix: block"
    fi

    echo "[scenario 7] --recreate drops and re-applies everything"
    run --recreate "$DB_URL"
    count="$(q "SELECT count(*) FROM pgmigrations" 2>&1)"
    if [ "$rc" -ne 0 ]; then
        report_fail "expected --recreate to succeed" "$out"
    elif [ "$count" != "5" ]; then
        report_fail "expected 5 recorded migrations after --recreate; got '$count'" "$out"
    else
        report_pass "--recreate rebuilt the database"
    fi
fi

echo ""
echo "apply_migrations.sh tests: $pass passed, $fail failed, $skip skipped"
[ "$fail" -eq 0 ]
