#!/usr/bin/env bash
# test_check_no_graphql_instanceof.sh — Tests for
# check-no-graphql-instanceof.sh.
#
# Issue #4198. The guard forbids `instanceof GraphQL(Object|Interface|
# Union|List|NonNull)Type` in the api graphql walker source — vitest's
# ESM loader and Apollo Server's CJS realm produce two class
# identities for the same conceptual graphql-js type, so `instanceof`
# silently returns `false` for a structurally-correct type. The
# realm-stable workaround is `Symbol.toStringTag`. Centralized helpers
# live in `packages/api/src/graphql/realm-stable-type-checks.ts`.
#
# Tests below build temp files in TMPDIR_TEST and point the guard at
# that directory, mirroring the test_check_no_api_github_fetch.sh
# pattern.
#
# Usage:
#   scripts/tests/test_check_no_graphql_instanceof.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-graphql-instanceof.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
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

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test a: instanceof GraphQLObjectType in a .ts file should fail ──────
create_test_file "walker.ts" 'function isObj(t: unknown): boolean { return t instanceof GraphQLObjectType; }'
assert_fails "instanceof GraphQLObjectType in a .ts file is detected"
reset_tmpdir

# ─── Test b: instanceof GraphQLInterfaceType should fail ─────────────────
create_test_file "walker.ts" 'if (named instanceof GraphQLInterfaceType) { return true; }'
assert_fails "instanceof GraphQLInterfaceType is detected"
reset_tmpdir

# ─── Test c: instanceof GraphQLUnionType should fail ─────────────────────
create_test_file "walker.ts" 'if (named instanceof GraphQLUnionType) { return true; }'
assert_fails "instanceof GraphQLUnionType is detected"
reset_tmpdir

# ─── Test d: instanceof GraphQLList (no Type suffix) should fail ─────────
# graphql-js exports GraphQLList without a Type suffix; the regex
# permits the suffix to be optional for the wrapper pair so both
# `GraphQLList` and `GraphQLListType` (if a future graphql-js fork
# added one) would be caught.
create_test_file "walker.ts" 'if (t instanceof GraphQLList) { return unwrap(t.ofType); }'
assert_fails "instanceof GraphQLList (no Type suffix) is detected"
reset_tmpdir

# ─── Test e: instanceof GraphQLNonNull (no Type suffix) should fail ──────
create_test_file "walker.ts" 'if (t instanceof GraphQLNonNull) { return unwrap(t.ofType); }'
assert_fails "instanceof GraphQLNonNull (no Type suffix) is detected"
reset_tmpdir

# ─── Test f: typeTag-based discrimination should pass ────────────────────
create_test_file "walker.ts" 'if (typeTag(t) === "GraphQLObjectType") { return true; }'
assert_passes "typeTag(t) === 'GraphQLObjectType' (the realm-stable replacement) is not flagged"
reset_tmpdir

# ─── Test g: instanceof GraphQLError should pass ─────────────────────────
# GraphQLError is an error class, not a schema-type discriminator. The
# realm gap only applies to type classes used for selection-set
# walking. `auth-resolvers.ts:351` legitimately throws/catches
# GraphQLError within Apollo's CJS world.
create_test_file "auth.ts" 'if (err instanceof GraphQLError) throw err;'
assert_passes "instanceof GraphQLError (error class) is not flagged"
reset_tmpdir

# ─── Test h: comment lines referencing the pattern should pass ───────────
create_test_file "doc.ts" '// instanceof GraphQLObjectType is forbidden — use Symbol.toStringTag.'
assert_passes "// comment line is not flagged"
reset_tmpdir

create_test_file "doc.ts" ' * `field.type instanceof GraphQLObjectType` returns false across realms.'
assert_passes "JSDoc * comment line is not flagged"
reset_tmpdir

create_test_file "doc.ts" '/* instanceof GraphQLObjectType is forbidden — see #4198. */'
assert_passes "/* block-comment line is not flagged"
reset_tmpdir

# ─── Test i: test files (*.test.ts) should be exempt ─────────────────────
# Vitest builds the schema in the same realm it imports the type
# class from, so `instanceof` is realm-stable inside tests. The
# walker source files imported BY the tests are the ones that must
# avoid `instanceof`.
create_test_file "walker.test.ts" 'expect(t instanceof GraphQLObjectType).toBe(true);'
assert_passes "*.test.ts files are exempt"
reset_tmpdir

create_test_file "walker.unit.test.ts" 'expect(t instanceof GraphQLObjectType).toBe(true);'
assert_passes "*.unit.test.ts files are exempt"
reset_tmpdir

# ─── Test j: non-.ts files should not be scanned ─────────────────────────
# The walker source under packages/api/src/graphql/ is exclusively
# TypeScript. A .md file that mentions the forbidden pattern as
# documentation must not trip the guard.
create_test_file "README.md" 'Avoid `instanceof GraphQLObjectType` — use Symbol.toStringTag.'
assert_passes "*.md files are not scanned"
reset_tmpdir

# ─── Test k: unrelated code with `instanceof Map` should pass ────────────
create_test_file "walker.ts" 'if (t instanceof Map) { return [...t.entries()]; }'
assert_passes "instanceof on unrelated classes (Map) is not flagged"
reset_tmpdir

# ─── Test l: empty directory should pass ─────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test m: .git, node_modules, dist, .next should be excluded ──────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf 'const x = t instanceof GraphQLObjectType;\n' > "$TMPDIR_TEST/.git/objects/badfile.ts"
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf 'const x = t instanceof GraphQLObjectType;\n' > "$TMPDIR_TEST/node_modules/some-pkg/index.ts"
mkdir -p "$TMPDIR_TEST/dist"
printf 'const x = t instanceof GraphQLObjectType;\n' > "$TMPDIR_TEST/dist/built.ts"
mkdir -p "$TMPDIR_TEST/.next"
printf 'const x = t instanceof GraphQLObjectType;\n' > "$TMPDIR_TEST/.next/cached.ts"
assert_passes ".git, node_modules, dist, .next are excluded"
reset_tmpdir

# ─── Test n: file:line message names the violation clearly ───────────────
# The error output must include `<file>:<lineno>:<content>` for each
# violation so an operator can jump straight to the offending line.
create_test_file "src/walker.ts" 'function isObj(t: unknown): boolean { return t instanceof GraphQLObjectType; }'
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if printf '%s' "$output" | grep -qE 'src/walker\.ts:[0-9]+:.*instanceof GraphQLObjectType'; then
    echo "PASS: error output names file:line of violation"
else
    echo "FAIL: error output did not include file:line for violation"
    echo "  output was:"
    printf '%s\n' "$output" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test o: No self-match on ci.yml step name ───────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern. If it does, the guard
# fails on its first CI run (#2541/#2542).
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-graphql-instanceof.sh" "ts"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
