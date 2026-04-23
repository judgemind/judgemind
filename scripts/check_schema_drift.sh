#!/usr/bin/env bash
# Verify schema.sql matches the result of applying all migrations.
#
# Creates two temporary databases:
#   1. From schema.sql alone
#   2. From migration 1 + all migrations (up sections only)
# Dumps both schemas and diffs them. Exits non-zero if they differ.
#
# Usage:
#   scripts/check_schema_drift.sh                    # uses local Docker postgres
#   scripts/check_schema_drift.sh --ci               # CI mode (starts its own postgres container)
#
# Requires: Docker running

set -euo pipefail

SCHEMA_FILE="packages/api/src/data-access/schema.sql"
MIGRATIONS_DIR="packages/api/migrations"
DB_FROM_SCHEMA="judgemind_check_schema_$$"
DB_FROM_MIGRATIONS="judgemind_check_migrations_$$"

# In CI mode, start a dedicated postgres container
if [[ "${1:-}" == "--ci" ]]; then
    CONTAINER="judgemind-schema-check-$$"
    echo "CI mode: starting temporary postgres container ($CONTAINER)..."
    docker run -d --name "$CONTAINER" \
        -e POSTGRES_DB=postgres \
        -e POSTGRES_USER=judgemind \
        -e POSTGRES_PASSWORD=localdev \
        postgres:16-alpine > /dev/null
    # Wait for postgres to be ready
    for i in $(seq 1 30); do
        if docker exec "$CONTAINER" pg_isready -U judgemind -q 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if ! docker exec "$CONTAINER" pg_isready -U judgemind -q 2>/dev/null; then
        echo "ERROR: postgres failed to start within 30 seconds"
        docker rm -f "$CONTAINER" > /dev/null 2>&1
        exit 1
    fi
    trap "docker rm -f $CONTAINER > /dev/null 2>&1" EXIT
else
    CONTAINER="judgemind-bootstrap-postgres-1"
    # Verify container is running
    if ! docker exec "$CONTAINER" pg_isready -U judgemind -q 2>/dev/null; then
        echo "ERROR: Postgres container '$CONTAINER' is not running."
        echo "Start it with: docker compose up -d postgres"
        exit 1
    fi
fi

echo "=== Checking schema.sql vs migrations ==="

# Helper: run psql inside container
run_psql() {
    docker exec -i "$CONTAINER" psql -U judgemind "$@"
}

# Helper: dump schema (deterministic output for diffing)
dump_schema() {
    local db="$1"
    docker exec "$CONTAINER" pg_dump -U judgemind \
        --schema-only --no-owner --no-privileges --no-tablespaces \
        "$db" \
        | grep -v '^\\\(restrict\|allow\|unrestrict\)' \
        | grep -v '^-- Dumped from\|^-- Dumped by\|^-- PostgreSQL database dump' \
        | grep -v '^ALTER DATABASE' \
        | grep -v '^$' \
        | grep -v '^--$'
}

# Create two temp databases
run_psql -d postgres -c "DROP DATABASE IF EXISTS $DB_FROM_SCHEMA" -q 2>/dev/null
run_psql -d postgres -c "DROP DATABASE IF EXISTS $DB_FROM_MIGRATIONS" -q 2>/dev/null
run_psql -d postgres -c "CREATE DATABASE $DB_FROM_SCHEMA OWNER judgemind" -q
run_psql -d postgres -c "CREATE DATABASE $DB_FROM_MIGRATIONS OWNER judgemind" -q

# DB 1: Apply schema.sql
echo "  Applying schema.sql..."
run_psql -d "$DB_FROM_SCHEMA" -v ON_ERROR_STOP=1 -q < "$SCHEMA_FILE"

# DB 2: Apply migrations (up sections only)
echo "  Applying migrations..."
for f in $(ls "$MIGRATIONS_DIR"/*.sql | sort -t/ -k3 -V); do
    # Replace "ALTER DATABASE judgemind" with the temp DB name so
    # search_path changes apply to the temp DB, not a missing "judgemind".
    up_sql=$(sed '/^-- [Dd]own [Mm]igration/,$d' "$f" | sed "s/ALTER DATABASE judgemind/ALTER DATABASE $DB_FROM_MIGRATIONS/g")
    echo "$up_sql" | run_psql -d "$DB_FROM_MIGRATIONS" -v ON_ERROR_STOP=1 -q 2>&1
done

# Dump both
echo "  Comparing schemas..."
dump_schema "$DB_FROM_SCHEMA" > /tmp/schema_from_file.dump
dump_schema "$DB_FROM_MIGRATIONS" > /tmp/schema_from_migrations.dump

# Clean up databases
run_psql -d postgres -c "DROP DATABASE $DB_FROM_SCHEMA" -q 2>/dev/null
run_psql -d postgres -c "DROP DATABASE $DB_FROM_MIGRATIONS" -q 2>/dev/null

# Diff
if diff -u /tmp/schema_from_migrations.dump /tmp/schema_from_file.dump > /tmp/schema_drift.diff 2>&1; then
    echo "=== PASS: schema.sql matches migrations ==="
    rm -f /tmp/schema_from_file.dump /tmp/schema_from_migrations.dump /tmp/schema_drift.diff
    exit 0
else
    echo "=== FAIL: schema.sql has drifted from migrations ==="
    echo ""
    echo "Diff (migrations on left, schema.sql on right):"
    cat /tmp/schema_drift.diff
    echo ""
    echo "To fix: run scripts/regenerate_schema.sh"
    rm -f /tmp/schema_from_file.dump /tmp/schema_from_migrations.dump /tmp/schema_drift.diff
    exit 1
fi
