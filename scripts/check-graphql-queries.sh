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
VALIDATOR_SCRIPT="$REPO_ROOT/scripts/validate-graphql-queries.mjs"

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

# ─── Resolve graphql package from known locations ────────────────────
# If NODE_PATH is already set (e.g., by CI or tests), use it.
# Otherwise, look for the graphql package relative to the script's
# actual location (which may differ from REPO_ROOT in tests).
if [ -z "${NODE_PATH:-}" ]; then
    SCRIPT_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    NODE_PATH_CANDIDATES=(
        "$REPO_ROOT/packages/web/node_modules"
        "$REPO_ROOT/packages/api/node_modules"
        "$REPO_ROOT/node_modules"
        "$SCRIPT_REPO_ROOT/packages/web/node_modules"
        "$SCRIPT_REPO_ROOT/packages/api/node_modules"
        "$SCRIPT_REPO_ROOT/node_modules"
    )

    RESOLVED_NODE_PATH=""
    for candidate in "${NODE_PATH_CANDIDATES[@]}"; do
        if [ -d "$candidate/graphql" ]; then
            RESOLVED_NODE_PATH="$candidate"
            break
        fi
    done

    if [ -z "$RESOLVED_NODE_PATH" ]; then
        echo "ERROR: Cannot find 'graphql' npm package."
        echo "  Checked: ${NODE_PATH_CANDIDATES[*]}"
        echo "  Fix: Run 'npm install' in packages/web/ or packages/api/,"
        echo "       or 'npm install graphql' in the repo root."
        exit 1
    fi

    export NODE_PATH="$RESOLVED_NODE_PATH"
fi

# ─── Run the Node.js validator ───────────────────────────────────────
node "$VALIDATOR_SCRIPT" "$REPO_ROOT"
