#!/usr/bin/env bash
# Apply all migrations (up sections only) to a Postgres database.
# Usage: scripts/apply_migrations.sh [DATABASE_URL]
# Default: postgres://judgemind:localdev@localhost:5432/judgemind

set -euo pipefail

DB_URL="${1:-postgres://judgemind:localdev@localhost:5432/judgemind}"
MIGRATIONS_DIR="packages/api/migrations"

echo "Applying migrations to: $(echo "$DB_URL" | sed 's|://[^@]*@|://***@|')"

# Sort migrations numerically by prefix
for f in $(ls "$MIGRATIONS_DIR"/*.sql | sort -t/ -k3 -V); do
    name=$(basename "$f")
    echo "  Applying $name ..."

    # Extract only the "Up Migration" section: everything before "-- Down Migration"
    up_sql=$(sed '/^-- [Dd]own [Mm]igration/,$d' "$f")

    echo "$up_sql" | psql "$DB_URL" -v ON_ERROR_STOP=1 --quiet 2>&1
    if [ $? -ne 0 ]; then
        echo "  FAILED: $name"
        exit 1
    fi
done

echo "All migrations applied successfully."
