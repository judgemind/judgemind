#!/bin/bash
# Create the `judgemind_test` database used by integration tests.
#
# This runs once, on first Postgres container init, via the
# `/docker-entrypoint-initdb.d/` convention. The owning user is the same
# `POSTGRES_USER` that owns the main `judgemind` database so connections
# made with the POSTGRES_PASSWORD succeed against both databases.
#
# We do NOT apply schema.sql to `judgemind_test` here — the schema is
# applied by `applyMigrations()` in packages/api/tests/setup-db.ts via
# node-pg-migrate when the integration test suite runs. That keeps the
# migration code path exercised by tests and avoids schema drift between
# the init-time snapshot and the migration history.
#
# See #3006 for the TEST_DATABASE_URL / hostname-allowlist split this
# dedicated test database supports.

set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname postgres <<-EOSQL
  SELECT 'CREATE DATABASE judgemind_test OWNER ${POSTGRES_USER}'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'judgemind_test')\gexec
EOSQL
