#!/usr/bin/env bash
# check-graphql-queries.sh — Validate frontend GraphQL queries against the API schema.
#
# Extracts all `gql` tagged template literals from packages/web/src/ (excluding
# test files) and validates them against the schema defined in
# packages/api/src/graphql/schema.ts.
#
# This catches frontend-API schema mismatches at CI time — e.g., a frontend
# query referencing a field that doesn't exist in the API schema.
#
# Does NOT require running the API server — works offline by parsing the
# schema string and query documents.
#
# Usage:
#   scripts/check-graphql-queries.sh           # exits 0 if clean, 1 if errors
#   scripts/check-graphql-queries.sh [repo-dir] # scan a specific repo root
#
# Exit codes:
#   0 — All queries are valid against the schema.
#   1 — One or more queries reference fields not in the schema.

set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

SCHEMA_FILE="$REPO_ROOT/packages/api/src/graphql/schema.ts"
WEB_SRC_DIR="$REPO_ROOT/packages/web/src"
VALIDATOR_SCRIPT="$REPO_ROOT/packages/web/scripts/validate-graphql-queries.mjs"

# ─── Verify files exist ──────────────────────────────────────────────
if [ ! -f "$SCHEMA_FILE" ]; then
    echo "ERROR: Schema file not found: $SCHEMA_FILE"
    exit 1
fi

if [ ! -d "$WEB_SRC_DIR" ]; then
    echo "ERROR: Web source directory not found: $WEB_SRC_DIR"
    exit 1
fi

if [ ! -f "$VALIDATOR_SCRIPT" ]; then
    echo "ERROR: Validator script not found: $VALIDATOR_SCRIPT"
    exit 1
fi

# ─── Check that Node.js is available ─────────────────────────────────
if ! command -v node > /dev/null 2>&1; then
    echo "ERROR: node is not installed or not in PATH"
    exit 1
fi

# ─── Verify the graphql npm package is resolvable from the validator ─
# The validator imports `graphql` via ESM. Node's ESM resolver anchors the
# lookup at the importer's URL (`packages/web/scripts/validate-graphql-queries.mjs`)
# and walks UP the directory tree looking for `node_modules/graphql`. NODE_PATH
# is a CommonJS-only knob and is ignored by ESM (#4093). The two paths that
# satisfy this resolver:
#   1. Local dev: `npm install` in packages/web/ → packages/web/node_modules/graphql
#   2. CI: `npm install --no-save graphql@^16.8` at repo root → <repo>/node_modules/graphql
# Either is fine; we only need to verify at least one exists so the failure
# mode is a friendly error rather than Node's raw stack trace.
if [ ! -d "$REPO_ROOT/packages/web/node_modules/graphql" ] && \
   [ ! -d "$REPO_ROOT/node_modules/graphql" ]; then
    echo "ERROR: Cannot find 'graphql' npm package."
    echo "  Looked for it at:"
    echo "    $REPO_ROOT/packages/web/node_modules/graphql"
    echo "    $REPO_ROOT/node_modules/graphql"
    echo "  Fix (local): cd packages/web && npm install"
    echo "  Fix (CI):    npm install --no-save graphql@^16.8  # at repo root"
    exit 1
fi

# ─── Run the Node.js validator ───────────────────────────────────────
node "$VALIDATOR_SCRIPT" "$REPO_ROOT"
