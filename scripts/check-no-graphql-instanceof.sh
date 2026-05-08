#!/usr/bin/env bash
# check-no-graphql-instanceof.sh — Forbid `instanceof GraphQL*Type`
# discriminator checks in the api graphql walker source.
#
# Background — the realm clash
# ────────────────────────────
# graphql-js maintains separate class identities across its CJS and ESM
# builds (the `instanceOf` helper at `graphql/jsutils/instanceOf.mjs`
# even throws when both realms have loaded). Vitest's ESM loader can
# resolve `graphql` and `graphql/index.mjs` as distinct module
# instances; Apollo Server pins itself to the CJS realm via the
# `/cjs` subpath import in `cost-limit-plugin.ts`. This means
# `field.type instanceof GraphQLObjectType` silently returns `false`
# for a structurally-correct object type when the schema was built in
# one realm and the walker imports `GraphQLObjectType` from the other —
# every type collapses to a scalar leaf with no error or warning.
#
# Issue #4101 hit this exact bug a second time (after #4112 first
# documented the workaround in `cost-rule-estimator.ts`). The cure is
# `Symbol.toStringTag` — graphql-js sets it on every type class, so it
# is realm-stable. See `packages/api/src/graphql/realm-stable-type-checks.ts`
# for the canonical helpers.
#
# What this guard scans
# ─────────────────────
# Any `instanceof GraphQLObjectType` / `GraphQLInterfaceType` /
# `GraphQLUnionType` / `GraphQLList` / `GraphQLNonNull` in any
# `packages/api/src/graphql/**/*.ts` file (excluding `*.test.ts` and
# `*.unit.test.ts`, where the realm is internally consistent because
# vitest builds the schema in the same realm that the test imports
# the type class from).
#
# Note on naming: graphql-js exports `GraphQLList` and `GraphQLNonNull`
# without the `Type` suffix; the three composite type classes carry
# `Type`. The pattern below permits the suffix to be optional for the
# wrapper classes so both shapes are caught.
#
# Comment lines (first non-whitespace is `//`, `*`, or `/*`) are
# skipped so docstrings explaining "why we avoid `instanceof
# GraphQLObjectType`" — like the ones already in
# `cost-rule-estimator.ts` and `cost-breakdown.ts` — do not trip the
# guard.
#
# What is NOT scanned
# ───────────────────
# - `instanceof GraphQLError` is fine — it is an error class, not a
#   schema-type discriminator. The realm gap discussed above only
#   applies to `Symbol.toStringTag`-tagged type classes used for
#   selection-set walking. `GraphQLError` is a thrown-error class with
#   a distinct identity per realm, but resolvers always throw and
#   catch within the same realm (Apollo's CJS world), so `instanceof`
#   on errors works in practice.
# - Test files (`*.test.ts`, `*.unit.test.ts`) — vitest builds its
#   schema in the same realm it imports the type classes from, so
#   `instanceof` is safe inside tests. The walker source files that
#   are imported BY the tests (and BY production Apollo wiring) are
#   the ones that must avoid `instanceof`.
# - Code outside `packages/api/src/graphql/`. Other directories do
#   not import from `graphql` directly.
#
# Usage
# ─────
#   scripts/check-no-graphql-instanceof.sh                # scan default dir
#   scripts/check-no-graphql-instanceof.sh [dir]          # scan a specific directory
#
# Exit codes
# ──────────
#   0 — No violations found.
#   1 — One or more files contain a forbidden `instanceof GraphQL*` use.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT/packages/api/src/graphql}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Pattern ─────────────────────────────────────────────────────────────
# Match `instanceof` followed by one of the realm-fragile graphql-js
# type classes. `Type` is required for the composite trio
# (Object/Interface/Union) and optional for the wrapper pair
# (List/NonNull) since graphql-js exports `GraphQLList` and
# `GraphQLNonNull` without a `Type` suffix.
PATTERN='instanceof[[:space:]]+GraphQL(ObjectType|InterfaceType|UnionType|List(Type)?|NonNull(Type)?)\b'

# ─── Build --exclude-dir arguments from the canonical list ──────────────
# (REPO_WALK_EXCLUSIONS lives in preflight.sh — single source of truth, #4308.)
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Scan ────────────────────────────────────────────────────────────────
violations=0

# Restrict to .ts files. The `--include` filter is honored by GNU grep
# AND by macOS BSD grep when given as a single argument.
raw_matches=$(grep -rnE "$PATTERN" "$SCAN_DIR" \
    --include='*.ts' \
    "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$raw_matches" ]]; then
    while IFS= read -r line; do
        # grep -rn output: <path>:<lineno>:<content>
        file_path="${line%%:*}"
        rest="${line#*:}"
        content="${rest#*:}"

        # Skip test files — vitest builds and consumes the schema in
        # the same realm, so `instanceof` is realm-stable inside tests.
        if [[ "$file_path" == *.test.ts ]] || [[ "$file_path" == *.unit.test.ts ]]; then
            continue
        fi

        # Skip TS/JS comment lines (first non-whitespace is //, /*, or *).
        # Covers JSDoc references to the forbidden pattern that
        # explain why it must be avoided.
        if [[ "$content" =~ ^[[:space:]]*(//|\*|/\*) ]]; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found forbidden 'instanceof GraphQL*' usage in api graphql walker source."
            echo ""
            echo "  graphql-js types must be discriminated by Symbol.toStringTag,"
            echo "  not 'instanceof'. Vitest's ESM loader can resolve 'graphql'"
            echo "  and 'graphql/index.mjs' as distinct module instances, and"
            echo "  Apollo Server pins itself to the CJS realm via the /cjs"
            echo "  subpath import in cost-limit-plugin.ts. 'instanceof' against"
            echo "  the wrong realm's class identity silently returns false and"
            echo "  collapses every type to a scalar leaf — see #4101 for the"
            echo "  recurrence after #4112 first documented the workaround."
            echo ""
            echo "  Use the realm-stable helpers from:"
            echo "    packages/api/src/graphql/realm-stable-type-checks.ts"
            echo ""
            echo "  Examples:"
            echo "    typeTag(field.type) === 'GraphQLObjectType'"
            echo "    isObjectOrInterface(named)   // narrows to CompositeTypeLike"
            echo "    isComposite(named)           // includes union types"
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$raw_matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of forbidden 'instanceof GraphQL*'."
    echo ""
    echo "  Fix: import the realm-stable helpers from"
    echo "  packages/api/src/graphql/realm-stable-type-checks.ts and replace"
    echo "  every 'instanceof GraphQLObjectType' (etc.) call with a"
    echo "  Symbol.toStringTag-based check."
    echo ""
    echo "  See cost-rule-estimator.ts and cost-breakdown.ts for prior art."
    echo "  Test files (*.test.ts, *.unit.test.ts) are exempt — vitest builds"
    echo "  the schema in the same realm the tests import the type class"
    echo "  from, so 'instanceof' is safe inside tests."
    exit 1
fi

echo "All clean — no forbidden 'instanceof GraphQL*' usage in $SCAN_DIR."
exit 0
