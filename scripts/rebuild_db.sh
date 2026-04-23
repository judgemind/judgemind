#!/usr/bin/env bash
# Rebuild the local database from S3 cached content.
#
# Prerequisites: Docker running, local S3 cache populated.
# Drops and recreates the local database, applies migrations,
# then runs rebuild_db.py with LLM extraction enabled.
#
# Usage:
#   scripts/local-rebuild.sh              # full rebuild
#   scripts/local-rebuild.sh --no-llm     # regex-only (no API key needed)
#   scripts/local-rebuild.sh --skip-reset # keep existing data, add missing docs

set -euo pipefail

CACHE_DIR="${S3_CACHE_DIR:-/tmp/judgemind-archive}"
DB_URL="postgres://judgemind:localdev@localhost:5432/judgemind"
REDIS_URL="redis://localhost:6379"
SKIP_RESET=false
USE_LLM=true

for arg in "$@"; do
    case "$arg" in
        --no-llm) USE_LLM=false ;;
        --skip-reset) SKIP_RESET=true ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# --- Preflight checks ---

echo "=== Preflight checks ==="

# Docker
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker is not running. Start Docker Desktop first."
    exit 1
fi
echo "  Docker: running"

# Postgres
if ! docker compose ps postgres 2>/dev/null | grep -qi "up\|running"; then
    echo "  Postgres not running — starting..."
    docker compose up -d postgres redis
    sleep 3
fi
echo "  Postgres: running"

# Redis
if ! docker compose ps redis 2>/dev/null | grep -qi "up\|running"; then
    echo "  Redis not running — starting..."
    docker compose up -d redis
    sleep 2
fi
echo "  Redis: running"

# Local cache
if [ ! -d "$CACHE_DIR/ca" ]; then
    echo "ERROR: Local S3 cache not found at $CACHE_DIR/ca"
    echo "Run first: scripts/run-py.sh scripts/s3_cache.py sync"
    exit 1
fi
echo "  S3 cache: $CACHE_DIR"

# --- Reset database ---

if [ "$SKIP_RESET" = false ]; then
    echo ""
    echo "=== Resetting local database ==="
    PG_URL="postgres://judgemind:localdev@localhost:5432/postgres"
    psql "$PG_URL" -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='judgemind' AND pid <> pg_backend_pid()" -q 2>/dev/null || true
    psql "$PG_URL" -c "DROP DATABASE IF EXISTS judgemind" -q
    psql "$PG_URL" -c "CREATE DATABASE judgemind OWNER judgemind" -q
    scripts/apply_migrations.sh "$DB_URL"
    echo "  Database reset and migrations applied."
else
    echo ""
    echo "=== Skipping database reset (--skip-reset) ==="
fi

# --- Fetch court rosters ---

echo ""
echo "=== Fetching court directory rosters ==="
env DATABASE_URL="$DB_URL" JUDGEMIND_ARCHIVE_BUCKET=judgemind-document-archive-dev \
    S3_CACHE_DIR="$CACHE_DIR" \
    scripts/run-py.sh scripts/fetch_rosters.py

# --- Run rebuild ---

echo ""
echo "=== Starting document rebuild ==="

ENV_VARS=(
    S3_CACHE_DIR="$CACHE_DIR"
    DATABASE_URL="$DB_URL"
    REDIS_URL="$REDIS_URL"
)

if [ "$USE_LLM" = true ]; then
    echo "  LLM extraction: enabled (fetching API key from Secrets Manager)"
    exec scripts/with-secret.sh -e GOOGLE_API_KEY=judgemind/google/api-key -- \
        env "${ENV_VARS[@]}" scripts/run-py.sh scripts/rebuild_db.py
else
    echo "  LLM extraction: disabled (regex only)"
    exec env "${ENV_VARS[@]}" scripts/run-py.sh scripts/rebuild_db.py
fi
