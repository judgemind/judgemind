#!/usr/bin/env bash
# check-apollo-keyfields.sh — Detect GraphQL types without `id` and without
# Apollo cache keyFields.
#
# Parses packages/api/src/graphql/schema.ts to extract all `type` definitions,
# checks which types have an `id` field, and for those without `id`, verifies
# that they have a `keyFields` entry in packages/web/src/lib/apollo-client.ts
# typePolicies.
#
# Types without `id` need either:
#   - keyFields: ['someField'] — for types with a natural unique key
#   - keyFields: false — for types that should not be normalized (edges, etc.)
#   - An entry in the SKIP_LIST — for types that intentionally don't need caching
#
# This prevents Apollo from collapsing distinct items into a single cache entry
# (see #1542, #1762, #1779).
#
# Usage:
#   scripts/check-apollo-keyfields.sh          # exits 0 if clean, 1 if violations
#
# Exit codes:
#   0 — All types without `id` have keyFields or are in the skip list.
#   1 — One or more types are missing keyFields.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SCHEMA_FILE="$REPO_ROOT/packages/api/src/graphql/schema.ts"
APOLLO_FILE="$REPO_ROOT/packages/web/src/lib/apollo-client.ts"

# ─── Skip list ────────────────────────────────────────────────────────
# Types that intentionally do NOT need keyFields configuration:
#
# - AuthPayload: single response from login/register mutations, never in arrays.
# - PlatformStats: singleton aggregate response, never in arrays.
# - PageInfo: embedded single object within each connection, never in arrays.
# - *Connection types: connection wrappers, not array items themselves.
#   They contain edges[] but are not themselves cached as array elements.
# - Query: root query type, implicitly handled by Apollo.
# - Mutation: root mutation type, implicitly handled by Apollo.
SKIP_LIST="AuthPayload PlatformStats PageInfo RulingSearchConnection CaseConnection JudgeConnection RulingConnection DataQualityMetricConnection Query Mutation"

# ─── Verify files exist ──────────────────────────────────────────────
if [ ! -f "$SCHEMA_FILE" ]; then
    echo "ERROR: Schema file not found: $SCHEMA_FILE"
    exit 1
fi

if [ ! -f "$APOLLO_FILE" ]; then
    echo "ERROR: Apollo client file not found: $APOLLO_FILE"
    exit 1
fi

# ─── Extract types without `id` from schema.ts ───────────────────────
# Parse the schema line by line, tracking type definitions and whether
# they contain an `id: ID` field.

types_without_id=""
current_type=""
has_id=false

while IFS= read -r line; do
    # Match "type TypeName {" (with possible leading whitespace)
    if echo "$line" | grep -qE '^[[:space:]]*type[[:space:]]+[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\{'; then
        # Save previous type if it was open and had no id
        if [ -n "$current_type" ] && [ "$has_id" = false ]; then
            types_without_id="$types_without_id $current_type"
        fi
        current_type=$(echo "$line" | sed -E 's/^[[:space:]]*type[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*\{.*/\1/')
        has_id=false
    elif [ -n "$current_type" ]; then
        # Check for id field: "id: ID!" or "id: ID"
        if echo "$line" | grep -qE '^[[:space:]]*id:[[:space:]]*ID'; then
            has_id=true
        fi
        # Check for closing brace (end of type definition)
        if echo "$line" | grep -qE '^[[:space:]]*\}'; then
            if [ "$has_id" = false ]; then
                types_without_id="$types_without_id $current_type"
            fi
            current_type=""
            has_id=false
        fi
    fi
done < "$SCHEMA_FILE"

# Handle last type if file doesn't end properly
if [ -n "$current_type" ] && [ "$has_id" = false ]; then
    types_without_id="$types_without_id $current_type"
fi

# ─── Remove skip-listed types ────────────────────────────────────────
filtered=""
for type_name in $types_without_id; do
    is_skipped=false
    for skip in $SKIP_LIST; do
        if [ "$type_name" = "$skip" ]; then
            is_skipped=true
            break
        fi
    done
    if [ "$is_skipped" = false ]; then
        filtered="$filtered $type_name"
    fi
done
types_without_id="$filtered"

# ─── Check for keyFields in apollo-client.ts ─────────────────────────
missing=""
for type_name in $types_without_id; do
    # Check if the type has a typePolicies entry in apollo-client.ts
    if ! grep -qE "^[[:space:]]*${type_name}:[[:space:]]*\{" "$APOLLO_FILE"; then
        missing="$missing $type_name"
    fi
done

# ─── Sort for deterministic output ──────────────────────────────────
if [ -n "$missing" ]; then
    sorted=$(echo "$missing" | tr ' ' '\n' | grep -v '^$' | sort)
else
    sorted=""
fi

# ─── Report results ─────────────────────────────────────────────────
if [ -n "$sorted" ]; then
    echo "ERROR: Found GraphQL types without \`id\` and without Apollo cache keyFields."
    echo ""
    echo "  The following types in schema.ts do not have an \`id\` field and are"
    echo "  not configured in apollo-client.ts typePolicies. Without keyFields,"
    echo "  Apollo may collapse distinct items into a single cache entry."
    echo ""
    echo "  Missing types:"
    echo ""
    echo "$sorted" | while IFS= read -r type_name; do
        echo "    - $type_name"
    done
    echo ""
    echo "  Fix: Add one of the following to the typePolicies in"
    echo "  packages/web/src/lib/apollo-client.ts:"
    echo ""
    echo "    TypeName: { keyFields: ['uniqueField'] }  — for types with a natural key"
    echo "    TypeName: { keyFields: false }             — for types that should not be normalized"
    echo ""
    echo "  Or add the type to the SKIP_LIST in this script if it intentionally"
    echo "  does not need cache configuration (e.g., singleton responses)."
    echo ""
    echo "  Reference: https://github.com/judgemind/judgemind/issues/1779"
    exit 1
fi

echo "All clean — all GraphQL types without \`id\` have Apollo cache keyFields configured."
exit 0
