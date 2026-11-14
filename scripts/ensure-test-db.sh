#!/usr/bin/env bash
# ensure-test-db.sh — a migrated, per-worktree Postgres test database.
#
# permanent: true
#
# Prints a TEST_DATABASE_URL on stdout for a database in the local
# docker-compose Postgres that is migrated to this checkout's
# packages/api/migrations. All messages go to stderr. The pre-push hook
# calls it so the real-Postgres tests (packages/*/tests that read
# TEST_DATABASE_URL, e.g. scraper-framework's *_pg.py and split harness)
# run and count toward diff coverage without the operator exporting
# anything (#4950).
#
# One database per checkout, reused across pushes:
#   name    jm_prepush_<checkout dir name>_<hash of its path>
#   comment "prepush-test-db fingerprint=<sha256> worktree=<path>"
# The fingerprint hashes every migration file's name and content. When
# it matches, the database is reused as is (well under a second). When a
# migration is added or edited, or a build was interrupted (no comment),
# the database is dropped and rebuilt from scratch with
# scripts/apply_migrations.sh (about 3 seconds). Tests run in rolled-back
# transactions, so reuse does not leak rows between runs.
#
# Cleanup: every run also drops jm_prepush_* databases whose recorded
# worktree directory no longer exists, so removed worktrees do not leave
# databases behind. --drop drops this checkout's database; --prune only
# prunes.
#
# Usage:
#   scripts/ensure-test-db.sh            # print the URL (create/migrate as needed)
#   scripts/ensure-test-db.sh --drop     # drop this checkout's database
#   scripts/ensure-test-db.sh --prune    # drop databases of removed worktrees
#
# Environment:
#   ENSURE_TEST_DB_ADMIN_URL  maintenance-database URL on the server
#       (default postgresql://judgemind:localdev@localhost:5432/postgres,
#       the docker-compose service). Only local hosts are accepted.
#   ENSURE_TEST_DB_WORKTREE   checkout to provision for (default: the
#       checkout this script is in).
#
# Exit codes:
#   0  URL printed (or --drop / --prune done)
#   1  error: e.g. a migration failed. A FAILED message and Fix: block
#      are on stderr.
#   3  Postgres unavailable (no psql, server not reachable). A WARNING
#      and Fix: block are on stderr; callers degrade, they do not fail.

set -uo pipefail

ADMIN_URL="${ENSURE_TEST_DB_ADMIN_URL:-postgresql://judgemind:localdev@localhost:5432/postgres}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATIONS_DIR="${APPLY_MIGRATIONS_DIR:-$REPO_ROOT/packages/api/migrations}"
COMMENT_TAG="prepush-test-db"
DB_PREFIX="jm_prepush_"

mode="ensure"
case "${1:-}" in
    "") ;;
    --drop) mode="drop" ;;
    --prune) mode="prune" ;;
    -h|--help)
        sed -n '2,47p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "ensure-test-db.sh: unknown argument: $1" >&2
        exit 2
        ;;
esac

worktree="$(cd "${ENSURE_TEST_DB_WORKTREE:-$REPO_ROOT}" && pwd -P)"

unavailable() {
    echo "WARNING: no local Postgres for the real-Postgres tests: $1" >&2
    echo "Fix:" >&2
    echo "  docker compose up -d postgres     # from the repo root; then re-run" >&2
    if [ -n "${2:-}" ]; then
        echo "  $2" >&2
    fi
    exit 3
}

if ! command -v psql >/dev/null 2>&1; then
    unavailable "psql is not installed" \
        "brew install libpq && brew link --force libpq   # psql client (macOS)"
fi

admin_host="$(echo "$ADMIN_URL" | sed -E 's#^[a-zA-Z]+://##; s#^[^@/]*@##; s#[:/?].*$##')"
case "$admin_host" in
    ""|localhost|127.0.0.1|::1|postgres) ;;
    *)
        echo "FAILED: ENSURE_TEST_DB_ADMIN_URL host '$admin_host' is not local;" >&2
        echo "this script drops and creates databases, so it only runs locally." >&2
        echo "Fix:" >&2
        echo "  unset ENSURE_TEST_DB_ADMIN_URL" >&2
        exit 1
        ;;
esac

# -X skips ~/.psqlrc (e.g. \x auto would change the output parsed here).
psql_admin() {
    PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-3}" psql "$ADMIN_URL" -X -v ON_ERROR_STOP=1 -Atq -c "$1"
}

if ! psql_admin "SELECT 1" >/dev/null 2>&1; then
    unavailable "cannot connect to $(echo "$ADMIN_URL" | sed 's|://[^@]*@|://***@|')"
fi

url_for() {
    echo "$ADMIN_URL" | sed -E "s#^([a-zA-Z]+://[^/]*)/[^?]*#\\1/$1#"
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

# db_name_for <worktree path>: jm_prepush_<sanitized basename>_<8 hex>.
# Postgres identifiers are at most 63 bytes; this stays under 60.
db_name_for() {
    local base hash
    base="$(basename "$1" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '_' | cut -c1-36)"
    hash="$(printf '%s' "$1" | sha256 | cut -c1-8)"
    echo "${DB_PREFIX}${base}_${hash}"
}

drop_db() {
    psql_admin "DROP DATABASE IF EXISTS \"$1\" WITH (FORCE)" >/dev/null
}

# prune: drop jm_prepush_* databases whose worktree directory is gone.
prune() {
    local rows name comment path
    rows="$(psql_admin "SELECT datname || '|' || coalesce(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname LIKE 'jm\\_prepush\\_%'")" || return 0
    while IFS='|' read -r name comment; do
        [ -n "$name" ] || continue
        case "$comment" in
            "$COMMENT_TAG "*worktree=*) path="${comment##*worktree=}" ;;
            *) continue ;;  # unknown or mid-build: leave it
        esac
        if [ ! -d "$path" ]; then
            echo "ensure-test-db: dropping $name (worktree $path no longer exists)" >&2
            drop_db "$name" || true
        fi
    done <<< "$rows"
}

db="$(db_name_for "$worktree")"

case "$mode" in
    prune)
        prune
        exit 0
        ;;
    drop)
        drop_db "$db" && echo "ensure-test-db: dropped $db" >&2
        exit $?
        ;;
esac

prune

if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "FAILED: migrations directory not found: $MIGRATIONS_DIR" >&2
    exit 1
fi

fingerprint="$(
    cd "$MIGRATIONS_DIR" || exit 1
    for f in $(ls -- *.sql | sort -n); do
        echo "== $f"
        cat -- "$f"
    done | sha256
)"
want="$COMMENT_TAG fingerprint=$fingerprint worktree=$worktree"
url="$(url_for "$db")"

have="$(psql_admin "SELECT coalesce(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = '$db'" 2>/dev/null)"
if [ "$have" = "$want" ]; then
    echo "ensure-test-db: reusing $db (migrations unchanged)" >&2
    echo "$url"
    exit 0
fi

if [ -n "$have" ] || psql_admin "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    echo "ensure-test-db: rebuilding $db (migrations changed since it was built)" >&2
else
    echo "ensure-test-db: creating $db" >&2
fi

log="$(mktemp "${TMPDIR:-/tmp}/ensure-test-db.XXXXXX")"
if ! drop_db "$db" 2>"$log" || ! psql_admin "CREATE DATABASE \"$db\"" >/dev/null 2>>"$log"; then
    echo "FAILED: could not create database $db:" >&2
    sed 's/^/  /' "$log" >&2
    rm -f "$log"
    echo "Fix:" >&2
    echo "  PREPUSH_PG=0 git push    # run without the Postgres tests (diff coverage excludes their modules)" >&2
    exit 1
fi

if ! APPLY_MIGRATIONS_DIR="$MIGRATIONS_DIR" "$SCRIPT_DIR/apply_migrations.sh" "$url" >"$log" 2>&1; then
    echo "FAILED: migrations did not apply to $db; CI's 'Apply migrations' step would fail too:" >&2
    tail -n 20 "$log" | sed 's/^/  /' >&2
    rm -f "$log"
    drop_db "$db" 2>/dev/null || true
    echo "Fix:" >&2
    echo "  scripts/apply_migrations.sh --recreate '$url'   # reproduce, fix the migration, re-push" >&2
    exit 1
fi
rm -f "$log"

# Written last: a build interrupted before this point has no comment and
# is rebuilt by the next run.
escaped="${want//\'/\'\'}"
psql_admin "COMMENT ON DATABASE \"$db\" IS '$escaped'" >/dev/null
echo "ensure-test-db: $db is migrated" >&2
echo "$url"
