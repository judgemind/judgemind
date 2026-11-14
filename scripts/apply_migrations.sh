#!/usr/bin/env bash
# Apply migrations (up sections only) to a Postgres database.
#
# permanent: true
#
# Usage:
#   scripts/apply_migrations.sh [DATABASE_URL]
#   scripts/apply_migrations.sh --recreate [DATABASE_URL]
# Default URL: postgres://judgemind:localdev@localhost:5432/judgemind
#
# Re-runnable (#4950). Each applied migration is recorded in
# public.pgmigrations, the same table and naming node-pg-migrate uses
# (file name without .sql, see packages/api/scripts/seed-and-migrate.mjs),
# so a second run applies only the migrations that are new since the
# first. Before #4950 every run started again at 1_initial-schema.sql and
# failed on an existing database, so a local database that had missed a
# migration (e.g. 54) could only be fixed by hand.
#
# A database that already has tables but no public.pgmigrations was
# migrated by an older copy of this script, and nothing says which
# migrations it has. The script refuses it with a Fix: block rather than
# guess. --recreate drops and recreates the database first, then applies
# every migration. It only works on a local host (localhost, 127.0.0.1,
# ::1, postgres, or a unix socket), so it cannot drop a remote database.

set -euo pipefail

RECREATE=0
DB_URL=""
for arg in "$@"; do
    case "$arg" in
        --recreate) RECREATE=1 ;;
        -h|--help)
            sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            echo "apply_migrations.sh: unknown option: $arg" >&2
            exit 2
            ;;
        *) DB_URL="$arg" ;;
    esac
done
DB_URL="${DB_URL:-postgres://judgemind:localdev@localhost:5432/judgemind}"
# Relative to this script, so it runs from any working directory.
MIGRATIONS_DIR="${APPLY_MIGRATIONS_DIR:-$(cd "$(dirname "$0")/.." && pwd)/packages/api/migrations}"
MASKED_URL="$(echo "$DB_URL" | sed 's|://[^@]*@|://***@|')"

if ! command -v psql >/dev/null 2>&1; then
    echo "FAILED: psql not found; cannot apply migrations." >&2
    echo "Fix:" >&2
    echo "  brew install libpq && brew link --force libpq    # macOS" >&2
    echo "  sudo apt-get install -y postgresql-client        # Debian/Ubuntu" >&2
    exit 1
fi

# -X skips ~/.psqlrc, whose settings (e.g. \x auto) would change the
# unaligned output parsed below.
psql_q() {
    psql "$1" -X -v ON_ERROR_STOP=1 -Atq -c "$2"
}

# url_host / url_dbname / url_with_db: minimal libpq URL parsing.
url_host() {
    echo "$1" | sed -E 's#^[a-zA-Z]+://##; s#^[^@/]*@##; s#[:/?].*$##'
}
url_dbname() {
    echo "$1" | sed -E 's#^[a-zA-Z]+://[^/]*/?##; s#\?.*$##'
}
url_with_db() {
    echo "$1" | sed -E "s#^([a-zA-Z]+://[^/]*)/[^?]*#\\1/$2#"
}

if [ "$RECREATE" = 1 ]; then
    host="$(url_host "$DB_URL")"
    db="$(url_dbname "$DB_URL")"
    case "$host" in
        ""|localhost|127.0.0.1|::1|postgres) ;;
        *)
            echo "FAILED: --recreate only drops a local database; host is '$host'." >&2
            exit 1
            ;;
    esac
    if [ -z "$db" ] || [ "$db" = "postgres" ]; then
        echo "FAILED: --recreate needs a database name in the URL (not 'postgres')." >&2
        exit 1
    fi
    admin_url="$(url_with_db "$DB_URL" postgres)"
    echo "Recreating database '$db' ..."
    psql_q "$admin_url" "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)"
    psql_q "$admin_url" "CREATE DATABASE \"$db\""
fi

echo "Applying migrations to: $MASKED_URL"

has_tracking="$(psql_q "$DB_URL" "SELECT to_regclass('public.pgmigrations') IS NOT NULL")"
if [ "$has_tracking" != "t" ]; then
    n_tables="$(psql_q "$DB_URL" "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema')")"
    if [ "$n_tables" != "0" ]; then
        echo "" >&2
        echo "FAILED: $MASKED_URL has $n_tables table(s) but no public.pgmigrations," >&2
        echo "so there is no record of which migrations it already has (#4950)." >&2
        echo "It was migrated by an older apply_migrations.sh. Recreate it:" >&2
        echo "Fix:" >&2
        echo "  scripts/apply_migrations.sh --recreate '$DB_URL'" >&2
        echo "  (drops every row in that database; derived.* is rebuildable with" >&2
        echo "  scripts/rebuild_db.sh, public.* local users/alerts are not)" >&2
        exit 1
    fi
    psql_q "$DB_URL" "CREATE TABLE public.pgmigrations (id SERIAL PRIMARY KEY, name varchar(255) NOT NULL, run_on timestamp NOT NULL)"
fi

applied="$(psql_q "$DB_URL" "SELECT name FROM public.pgmigrations")"
n_applied=0
n_skipped=0

# Sort migrations numerically by their N_ prefix.
for file in $(cd "$MIGRATIONS_DIR" && ls -- *.sql | sort -n); do
    f="$MIGRATIONS_DIR/$file"
    name="${file%.sql}"
    if printf '%s\n' "$applied" | grep -Fxq -- "$name"; then
        n_skipped=$((n_skipped + 1))
        continue
    fi
    echo "  Applying $file ..."

    # Extract only the "Up Migration" section: everything before "-- Down Migration"
    up_sql=$(sed '/^-- [Dd]own [Mm]igration/,$d' "$f")

    # One psql session per migration, as before #4950: a migration's
    # ALTER DATABASE ... SET search_path (15_schema-reorganization) only
    # reaches the next migration through a new session. The record is
    # written in the same session (one connection, not two, per
    # migration), after the migration; ON_ERROR_STOP skips it when the
    # migration fails. The lone ';' ends a final statement that has no
    # semicolon of its own.
    if ! printf '%s\n;\nINSERT INTO public.pgmigrations (name, run_on) VALUES (%s, now());\n' \
            "$up_sql" "'$name'" \
            | psql "$DB_URL" -X -v ON_ERROR_STOP=1 --quiet 2>&1; then
        echo "  FAILED: $file"
        exit 1
    fi
    n_applied=$((n_applied + 1))
done

echo "Migrations up to date: applied $n_applied, already applied $n_skipped."
