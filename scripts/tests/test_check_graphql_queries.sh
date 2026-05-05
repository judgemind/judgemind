#!/usr/bin/env bash
# test_check_graphql_queries.sh — Tests for check-graphql-queries.sh
#
# Creates temporary directory structures with synthetic GraphQL schemas and
# frontend query files to verify that the checker correctly detects mismatches.
#
# Requires: Node.js and the `graphql` npm package (resolved from the real
# repo's packages/api/node_modules/ or packages/web/node_modules/).
#
# Usage:
#   scripts/tests/test_check_graphql_queries.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-graphql-queries.sh"
VALIDATOR_SCRIPT="$REPO_ROOT/packages/web/scripts/validate-graphql-queries.mjs"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Set up the directory structure the script expects.
#
# IMPORTANT: ESM resolution for `import 'graphql'` walks UP from the validator's
# file URL. To exercise that path realistically, we symlink the validator at
# `$TMPDIR_TEST/packages/web/scripts/` AND symlink real `node_modules/`
# directories from the host repo so the resolver finds the package. (#4093)
setup_repo() {
    rm -rf "$TMPDIR_TEST"/*
    mkdir -p "$TMPDIR_TEST/packages/api/src/graphql"
    mkdir -p "$TMPDIR_TEST/packages/web/src/app"
    mkdir -p "$TMPDIR_TEST/packages/web/scripts"
    # Symlink the validator script at the new location so check-graphql-queries.sh
    # finds it under $REPO_ROOT/packages/web/scripts/.
    ln -sf "$VALIDATOR_SCRIPT" "$TMPDIR_TEST/packages/web/scripts/validate-graphql-queries.mjs"
    # Make the `graphql` npm package resolvable from the validator. Prefer the
    # host repo's packages/web/node_modules/, fall back to the host repo's
    # top-level node_modules/ (CI installs it there via npm install --no-save).
    if [ -d "$REPO_ROOT/packages/web/node_modules" ]; then
        ln -sf "$REPO_ROOT/packages/web/node_modules" "$TMPDIR_TEST/packages/web/node_modules"
    elif [ -d "$REPO_ROOT/node_modules" ]; then
        ln -sf "$REPO_ROOT/node_modules" "$TMPDIR_TEST/node_modules"
    fi
}

# Write a schema.ts file with the given SDL content
write_schema() {
    local sdl="$1"
    cat > "$TMPDIR_TEST/packages/api/src/graphql/schema.ts" <<SCHEMA_END
export const typeDefs = \`#graphql
$sdl
\`;
SCHEMA_END
}

# Write a .tsx file with a gql query
write_query_file() {
    local filename="$1"
    local query="$2"
    local dir
    dir=$(dirname "$TMPDIR_TEST/packages/web/src/$filename")
    mkdir -p "$dir"
    cat > "$TMPDIR_TEST/packages/web/src/$filename" <<QUERY_END
import { gql } from '@apollo/client';
const MY_QUERY = gql\`
$query
\`;
QUERY_END
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: Valid query — should pass ───────────────────────────────
setup_repo
write_schema '
  type Court {
    id: ID!
    county: String!
    courtName: String!
  }
  type Query {
    court(id: ID!): Court
  }
'
write_query_file "app/CourtPage.tsx" '
  query CourtDetail($id: ID!) {
    court(id: $id) {
      id
      county
      courtName
    }
  }
'
assert_passes "Valid query matching schema passes"

# ─── Test 2: Query references non-existent field — should fail ───────
setup_repo
write_schema '
  type Court {
    id: ID!
    county: String!
  }
  type Query {
    court(id: ID!): Court
  }
'
write_query_file "app/CourtPage.tsx" '
  query CourtDetail($id: ID!) {
    court(id: $id) {
      id
      county
      courtName
    }
  }
'
assert_fails "Query with non-existent field is detected"

# ─── Test 3: No queries in source files — should pass ────────────────
setup_repo
write_schema '
  type Query {
    health: String!
  }
'
# Write a tsx file without any gql queries
mkdir -p "$TMPDIR_TEST/packages/web/src/app"
cat > "$TMPDIR_TEST/packages/web/src/app/Page.tsx" <<'EOF'
export default function Page() {
  return <div>Hello</div>;
}
EOF
assert_passes "Source files with no gql queries pass"

# ─── Test 4: Multiple queries, one invalid — should fail ─────────────
setup_repo
write_schema '
  type Court {
    id: ID!
    county: String!
  }
  type Judge {
    id: ID!
    canonicalName: String!
  }
  type Query {
    court(id: ID!): Court
    judge(id: ID!): Judge
  }
'
# Valid query
write_query_file "app/CourtPage.tsx" '
  query CourtDetail($id: ID!) {
    court(id: $id) {
      id
      county
    }
  }
'
# Invalid query — references non-existent field
write_query_file "app/JudgePage.tsx" '
  query JudgeDetail($id: ID!) {
    judge(id: $id) {
      id
      canonicalName
      department
    }
  }
'
assert_fails "One invalid query among multiple is detected"

# ─── Test 5: Test files (__tests__/) are skipped ─────────────────────
setup_repo
write_schema '
  type Query {
    health: String!
  }
'
# Write a test file with a fake query — should be skipped
mkdir -p "$TMPDIR_TEST/packages/web/src/lib/__tests__"
cat > "$TMPDIR_TEST/packages/web/src/lib/__tests__/auth.test.ts" <<'EOF'
import { gql } from '@apollo/client';
const TEST_QUERY = gql`
  query TestQuery {
    nonExistentField {
      id
    }
  }
`;
EOF
assert_passes "Test files in __tests__/ directories are skipped"

# ─── Test 6: Mutation query is validated — should pass ────────────────
setup_repo
write_schema '
  type AuthPayload {
    accessToken: String!
  }
  type Query {
    health: String!
  }
  type Mutation {
    login(email: String!, password: String!): AuthPayload!
  }
'
write_query_file "lib/auth-mutations.ts" '
  mutation Login($email: String!, $password: String!) {
    login(email: $email, password: $password) {
      accessToken
    }
  }
'
assert_passes "Valid mutation query passes"

# ─── Test 7: Mutation with non-existent field — should fail ──────────
setup_repo
write_schema '
  type AuthPayload {
    accessToken: String!
  }
  type Query {
    health: String!
  }
  type Mutation {
    login(email: String!, password: String!): AuthPayload!
  }
'
write_query_file "lib/auth-mutations.ts" '
  mutation Login($email: String!, $password: String!) {
    login(email: $email, password: $password) {
      accessToken
      refreshToken
    }
  }
'
assert_fails "Mutation with non-existent field is detected"

# ─── Test 8: Nested type fields validated — should pass ──────────────
setup_repo
write_schema '
  type Court {
    id: ID!
    courtName: String!
  }
  type Case {
    id: ID!
    caseNumber: String!
    court: Court
  }
  type Query {
    case(id: ID!): Case
  }
'
write_query_file "app/CasePage.tsx" '
  query CaseDetail($id: ID!) {
    case(id: $id) {
      id
      caseNumber
      court {
        courtName
      }
    }
  }
'
assert_passes "Nested type field queries pass"

# ─── Test 9: Nested type with invalid field — should fail ────────────
setup_repo
write_schema '
  type Court {
    id: ID!
    courtName: String!
  }
  type Case {
    id: ID!
    caseNumber: String!
    court: Court
  }
  type Query {
    case(id: ID!): Case
  }
'
write_query_file "app/CasePage.tsx" '
  query CaseDetail($id: ID!) {
    case(id: $id) {
      id
      caseNumber
      court {
        courtName
        county
      }
    }
  }
'
assert_fails "Nested type with invalid field is detected"

# ─── Test 10: Error output includes file path ────────────────────────
setup_repo
write_schema '
  type Court {
    id: ID!
  }
  type Query {
    court(id: ID!): Court
  }
'
write_query_file "app/CourtPage.tsx" '
  query CourtDetail($id: ID!) {
    court(id: $id) {
      id
      missingField
    }
  }
'
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "app/CourtPage.tsx"; then
    echo "PASS: Error output includes the file path"
else
    echo "FAIL: Error output does not include the file path"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 11: Error mentions the invalid field name ──────────────────
TESTS=$((TESTS + 1))
if echo "$output" | grep -q "missingField"; then
    echo "PASS: Error output mentions the invalid field name"
else
    echo "FAIL: Error output does not mention the invalid field name"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 12: Input types and enum types are validated ────────────────
setup_repo
write_schema '
  enum MetricResolution {
    hourly
    daily
  }
  input SearchFilters {
    county: String
  }
  type SearchResult {
    id: ID!
    county: String
  }
  type SearchConnection {
    totalHits: Int!
  }
  type Query {
    search(filters: SearchFilters, resolution: MetricResolution): SearchConnection!
  }
'
write_query_file "app/SearchPage.tsx" '
  query Search($filters: SearchFilters, $resolution: MetricResolution) {
    search(filters: $filters, resolution: $resolution) {
      totalHits
    }
  }
'
assert_passes "Input types and enum arguments are validated correctly"

# ─── Test 13: Dynamic gql queries with ${} are skipped ────────────────
setup_repo
write_schema '
  type Court {
    id: ID!
    county: String!
  }
  type Query {
    court(id: ID!): Court
  }
'
# Write a file with a dynamic gql query using template interpolation.
# The ${params} and ${fields} would be invalid GraphQL if parsed literally,
# but the checker should skip these because they are dynamic.
mkdir -p "$TMPDIR_TEST/packages/web/src/app"
cat > "$TMPDIR_TEST/packages/web/src/app/DynamicQuery.tsx" <<'DYNAMIC_EOF'
import { gql } from '@apollo/client';
function buildQuery(ids) {
  const params = ids.map((_, i) => `$id${i}: ID!`).join(', ');
  const fields = ids.map((_, i) => `j${i}: court(id: $id${i}) { id county }`).join('\n');
  return gql`
    query DynamicCourts(${params}) {
      ${fields}
    }
  `;
}
DYNAMIC_EOF
assert_passes "Dynamic gql queries with template expressions are skipped"

# ─── Test 14: Schema with escaped backticks — should pass ────────────
setup_repo
# Write schema.ts with escaped backticks in description strings
mkdir -p "$TMPDIR_TEST/packages/api/src/graphql"
cat > "$TMPDIR_TEST/packages/api/src/graphql/schema.ts" <<'ESCAPED_EOF'
export const typeDefs = `#graphql
  type PageInfo {
    hasNextPage: Boolean!
    """Opaque cursor — pass as \`after\` to fetch the next page."""
    endCursor: String
  }
  type Query {
    health: String!
  }
`;
ESCAPED_EOF
# Write a query file that queries valid fields
write_query_file "app/Page.tsx" '
  query Health {
    health
  }
'
assert_passes "Schema with escaped backticks in descriptions is parsed correctly"

# ─── Test 15: Module schema files extend the core schema ─────────────
# Mirrors the real layout where `packages/api/src/graphql/dispatcher/schema.ts`
# declares `extend type Query` additions that the frontend references via
# gql queries. The validator must concatenate all per-module SDL blocks so
# those additions are visible.
setup_repo
write_schema '
  type Query {
    health: String!
  }
'
# Write a per-module schema file that extends Query with a new field.
mkdir -p "$TMPDIR_TEST/packages/api/src/graphql/dispatcher"
cat > "$TMPDIR_TEST/packages/api/src/graphql/dispatcher/schema.ts" <<'MODULE_EOF'
export const dispatcherTypeDefs = `#graphql
  type DispatcherState {
    queueDepth: Int!
  }
  extend type Query {
    dispatcherState: DispatcherState!
  }
`;
MODULE_EOF
write_query_file "app/DispatcherPage.tsx" '
  query DispatcherState {
    dispatcherState {
      queueDepth
    }
  }
'
assert_passes "Per-module schema.ts files extend the core schema"

# ─── Test 16: Module schema referencing missing core type still fails ─
setup_repo
write_schema '
  type Query {
    health: String!
  }
'
mkdir -p "$TMPDIR_TEST/packages/api/src/graphql/dispatcher"
cat > "$TMPDIR_TEST/packages/api/src/graphql/dispatcher/schema.ts" <<'MODULE_EOF'
export const dispatcherTypeDefs = `#graphql
  type DispatcherState {
    queueDepth: Int!
  }
  extend type Query {
    dispatcherState: DispatcherState!
  }
`;
MODULE_EOF
# Query references a field not present in either core or module schema.
write_query_file "app/DispatcherPage.tsx" '
  query DispatcherState {
    dispatcherState {
      queueDepth
      missingField
    }
  }
'
assert_fails "Module schema still surfaces invalid fields"

# ─── Test 17: Regression for #4093 — packages/web/node_modules/graphql alone is sufficient ─
# The previous validator lived at scripts/validate-graphql-queries.mjs and relied
# on NODE_PATH to resolve the `graphql` package. ESM ignores NODE_PATH, so when
# only packages/web/node_modules/graphql/ existed (the local-dev path),
# `npm run check` failed with ERR_MODULE_NOT_FOUND. The fix (#4093) moved the
# validator to packages/web/scripts/ so ESM's natural upward resolution finds
# the package. Verify that even when packages/web/node_modules is the *only*
# place graphql lives — i.e. no top-level node_modules — the check still passes.
TESTS=$((TESTS + 1))
TMPDIR_LOCAL=$(mktemp -d)
local_cleanup() {
    rm -rf "$TMPDIR_LOCAL"
}
mkdir -p "$TMPDIR_LOCAL/packages/api/src/graphql"
mkdir -p "$TMPDIR_LOCAL/packages/web/src/app"
mkdir -p "$TMPDIR_LOCAL/packages/web/scripts"
# Validator at the new location.
ln -sf "$VALIDATOR_SCRIPT" "$TMPDIR_LOCAL/packages/web/scripts/validate-graphql-queries.mjs"
# Schema and a valid query.
cat > "$TMPDIR_LOCAL/packages/api/src/graphql/schema.ts" <<'SCHEMA_EOF'
export const typeDefs = `#graphql
  type Query {
    health: String!
  }
`;
SCHEMA_EOF
cat > "$TMPDIR_LOCAL/packages/web/src/app/Page.tsx" <<'QUERY_EOF'
import { gql } from '@apollo/client';
const Q = gql`
  query Health {
    health
  }
`;
QUERY_EOF
# Crucially, only symlink packages/web/node_modules — NOT a top-level
# node_modules. This locks in the local-dev layout where the prior bug fired.
if [ -d "$REPO_ROOT/packages/web/node_modules/graphql" ]; then
    ln -sf "$REPO_ROOT/packages/web/node_modules" "$TMPDIR_LOCAL/packages/web/node_modules"
elif [ -d "$REPO_ROOT/node_modules/graphql" ]; then
    # CI runs `npm install --no-save graphql@^16.8` at repo root and does NOT
    # populate packages/web/node_modules. In that environment we still want to
    # exercise the same code path: place a node_modules fixture at the
    # packages/web/ level by symlinking the host's root node_modules.
    ln -sf "$REPO_ROOT/node_modules" "$TMPDIR_LOCAL/packages/web/node_modules"
else
    echo "FAIL: Test 17 setup — neither packages/web/node_modules/graphql nor"
    echo "      node_modules/graphql exists at REPO_ROOT=$REPO_ROOT. Run npm"
    echo "      install in packages/web/ before invoking this test."
    FAILURES=$((FAILURES + 1))
    local_cleanup
    # fall through to summary
fi
if [ -L "$TMPDIR_LOCAL/packages/web/node_modules" ]; then
    if "$CHECK_SCRIPT" "$TMPDIR_LOCAL" > /dev/null 2>&1; then
        echo "PASS: #4093 — validator resolves graphql via packages/web/node_modules only"
    else
        echo "FAIL: #4093 — validator could not resolve graphql with only"
        echo "      packages/web/node_modules populated (this is the regression)."
        FAILURES=$((FAILURES + 1))
    fi
fi
local_cleanup

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
