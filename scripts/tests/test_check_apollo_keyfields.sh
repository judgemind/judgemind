#!/usr/bin/env bash
# test_check_apollo_keyfields.sh — Tests for scripts/check-apollo-keyfields.sh.
#
# Each test builds a fake repo (packages/api/src/graphql/schema.ts plus
# packages/web/src/lib/apollo-client.ts) next to a copy of the check, runs
# it, and asserts the exit code and the reported type names. Added with the
# #4720 speed-up, which replaced the per-line ``echo | grep`` / ``echo |
# sed`` parsing with bash's built-in ``[[ =~ ]]``.
#
# Usage:
#   scripts/tests/test_check_apollo_keyfields.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-apollo-keyfields.sh"
FAILURES=0
TESTS=0

# Cleanup of temp directories on exit via the shared helper (see #4343).
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"

TMPDIR_TEST="$(mktemp -d)"
register_temp_dir "$TMPDIR_TEST"

FAKE="$TMPDIR_TEST/repo"
SCHEMA="$FAKE/packages/api/src/graphql/schema.ts"
APOLLO="$FAKE/packages/web/src/lib/apollo-client.ts"
mkdir -p "$FAKE/scripts" "$(dirname "$SCHEMA")" "$(dirname "$APOLLO")"
cp "$CHECK_SCRIPT" "$FAKE/scripts/check-apollo-keyfields.sh"

OUT_FILE="$TMPDIR_TEST/out.txt"

run_check() {
    local rc=0
    "$FAKE/scripts/check-apollo-keyfields.sh" > "$OUT_FILE" 2>&1 || rc=$?
    echo "$rc"
}

# expect <desc> <expected_rc> <expected missing types, space-separated or "">
expect() {
    local desc="$1"
    local want_rc="$2"
    local want_types="$3"
    TESTS=$((TESTS + 1))
    local rc
    rc="$(run_check)"
    local got_types
    got_types="$(sed -n 's/^    - //p' "$OUT_FILE" | tr '\n' ' ' | sed 's/ $//')"
    if [[ "$rc" == "$want_rc" && "$got_types" == "$want_types" ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (rc=$rc want $want_rc; types='$got_types' want '$want_types')"
        sed 's/^/    /' "$OUT_FILE"
        FAILURES=$((FAILURES + 1))
    fi
}

write_apollo() {
    cat > "$APOLLO"
}

# ─── Test 1: every type has id → passes ─────────────────────────────────
cat > "$SCHEMA" <<'EOF'
export const typeDefs = `#graphql
  type Ruling {
    id: ID!
    text: String
  }
  type Judge {
      id: ID
  }
`;
EOF
write_apollo <<'EOF'
export const cache = { typePolicies: {} };
EOF
expect "types that all declare id pass" 0 ""

# ─── Test 2: types without id and without keyFields are reported ────────
# Covers: a type with no id, a nested-brace-free multi-field type, an
# indented opener with extra spaces, and a trailing type left open at EOF.
cat > "$SCHEMA" <<'EOF'
export const typeDefs = `#graphql
  type Ruling {
    id: ID!
  }
  type Zeta {
    name: String
    identifier: String
  }
    type   Alpha   {
    count: Int
  }
  type Keyed {
    value: String
  }
  type Trailing {
    x: Int
EOF
write_apollo <<'EOF'
export const cache = {
  typePolicies: {
    Keyed: { keyFields: ['value'] },
  },
};
EOF
expect "types without id or keyFields are reported, sorted" 1 "Alpha Trailing Zeta"

# ─── Test 3: SKIP_LIST types are not reported ───────────────────────────
cat > "$SCHEMA" <<'EOF'
export const typeDefs = `#graphql
  type PageInfo {
    hasNextPage: Boolean
  }
  type Query {
    ruling: Ruling
  }
  type Ruling {
    id: ID!
  }
`;
EOF
write_apollo <<'EOF'
export const cache = { typePolicies: {} };
EOF
expect "SKIP_LIST types (PageInfo, Query) are not reported" 0 ""

# ─── Test 4: keyFields: false counts as configured ──────────────────────
cat > "$SCHEMA" <<'EOF'
  type Stats {
    total: Int
  }
EOF
write_apollo <<'EOF'
    Stats: { keyFields: false },
EOF
expect "keyFields: false entry satisfies the check" 0 ""

# ─── Test 5: the real repo passes ───────────────────────────────────────
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: real repo schema.ts / apollo-client.ts pass"
else
    echo "FAIL: real repo fails check-apollo-keyfields.sh"
    "$CHECK_SCRIPT" 2>&1 | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if (( FAILURES > 0 )); then
    exit 1
fi
exit 0
