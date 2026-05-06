#!/usr/bin/env bash
# test_check_vitest_environment_deps.sh — Tests for check-vitest-environment-deps.sh
#
# Creates synthetic packages/<pkg>/{vitest.config.ts,package.json} fixtures
# and verifies the checker correctly flags configs whose declared
# `environment:` value is missing from the same package's package.json.
#
# Mirrors the regression class in #4088 (jsdom transitive-only on fresh
# worktrees).
#
# Usage:
#   scripts/tests/test_check_vitest_environment_deps.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-vitest-environment-deps.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"
    mkdir -p "$TMPDIR_TEST"
}

# Materialize a fixture: packages/<pkg>/vitest.config.ts + package.json.
# Args:
#   $1 — package name (subdir under TMPDIR_TEST/packages)
#   $2 — environment literal to write into vitest.config.ts (or empty for none)
#   $3 — comma-separated devDependency names to declare in package.json
#         (e.g. "vitest,jsdom") — set to empty for an empty devDependencies map
make_fixture() {
    local pkg="$1"
    local env_value="$2"
    local dev_deps_csv="${3:-}"

    local pkg_dir="$TMPDIR_TEST/packages/$pkg"
    mkdir -p "$pkg_dir"

    if [[ -n "$env_value" ]]; then
        cat > "$pkg_dir/vitest.config.ts" <<TSEOF
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: '$env_value',
  },
});
TSEOF
    else
        cat > "$pkg_dir/vitest.config.ts" <<'TSEOF'
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    setupFiles: ['./tests/setup.ts'],
  },
});
TSEOF
    fi

    # Build a package.json with the declared dev deps.
    {
        echo '{'
        echo "  \"name\": \"$pkg\","
        echo '  "version": "0.0.0",'
        echo '  "devDependencies": {'
        if [[ -n "$dev_deps_csv" ]]; then
            local first=1
            local IFS=','
            for dep in $dev_deps_csv; do
                if [[ $first -eq 1 ]]; then
                    first=0
                else
                    echo ','
                fi
                printf '    "%s": "*"' "$dep"
            done
            echo ''
        fi
        echo '  }'
        echo '}'
    } > "$pkg_dir/package.json"
}

assert_passes() {
    local desc="$1"
    local pkgs_dir="$2"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$pkgs_dir" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected exit 0, got non-zero)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    local pkgs_dir="$2"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$pkgs_dir" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected exit 1, got 0)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_output_contains() {
    local desc="$1"
    local pkgs_dir="$2"
    local pattern="$3"
    TESTS=$((TESTS + 1))
    local out
    out=$("$CHECK_SCRIPT" "$pkgs_dir" 2>&1 || true)
    if echo "$out" | grep -qE "$pattern"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (pattern '$pattern' not found in output: $out)"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: jsdom env declared + jsdom devDep present → passes ───────
reset_tmpdir
make_fixture "web" "jsdom" "vitest,jsdom"
assert_passes "jsdom env + jsdom devDep present" "$TMPDIR_TEST/packages"

# ─── Test 2: jsdom env declared + jsdom missing → fails (#4088 shape) ─
reset_tmpdir
make_fixture "web" "jsdom" "vitest"
assert_fails "jsdom env + jsdom missing flagged (the #4088 regression)" "$TMPDIR_TEST/packages"

# ─── Test 3: failure message names the missing env package ────────────
reset_tmpdir
make_fixture "web" "jsdom" "vitest"
assert_output_contains \
    "failure message names the missing env package" \
    "$TMPDIR_TEST/packages" \
    "jsdom"

# ─── Test 4: env=node is the default — no devDep required, passes ─────
reset_tmpdir
make_fixture "api" "node" "vitest"
assert_passes "env=node passes without an extra devDep" "$TMPDIR_TEST/packages"

# ─── Test 5: no environment declaration → passes ──────────────────────
reset_tmpdir
make_fixture "api" "" "vitest"
assert_passes "no environment declaration passes" "$TMPDIR_TEST/packages"

# ─── Test 6: happy-dom env declared + happy-dom devDep present → pass ─
reset_tmpdir
make_fixture "web" "happy-dom" "vitest,happy-dom"
assert_passes "happy-dom env + happy-dom devDep present" "$TMPDIR_TEST/packages"

# ─── Test 7: happy-dom env declared + happy-dom missing → fails ───────
reset_tmpdir
make_fixture "web" "happy-dom" "vitest"
assert_fails "happy-dom env + happy-dom missing flagged" "$TMPDIR_TEST/packages"

# ─── Test 8: env package present in `dependencies` (not just devDeps) → pass
# Some packages may declare jsdom as a runtime dep; the check accepts both.
reset_tmpdir
mkdir -p "$TMPDIR_TEST/packages/web"
cat > "$TMPDIR_TEST/packages/web/vitest.config.ts" <<'TSEOF'
import { defineConfig } from 'vitest/config';
export default defineConfig({ test: { environment: 'jsdom' } });
TSEOF
cat > "$TMPDIR_TEST/packages/web/package.json" <<'JSEOF'
{
  "name": "web",
  "version": "0.0.0",
  "dependencies": {
    "jsdom": "*"
  }
}
JSEOF
assert_passes "env package in dependencies (not devDependencies) accepted" "$TMPDIR_TEST/packages"

# ─── Test 9: multiple packages — one good, one bad → exits 1 ──────────
reset_tmpdir
make_fixture "good-pkg" "jsdom" "vitest,jsdom"
make_fixture "bad-pkg" "jsdom" "vitest"
assert_fails "mixed good+bad packages exits 1 if any package is bad" "$TMPDIR_TEST/packages"

# ─── Test 10: multiple packages, all good → exits 0 ───────────────────
reset_tmpdir
make_fixture "web" "jsdom" "vitest,jsdom"
make_fixture "api" "node" "vitest"
make_fixture "shared" "" "vitest"
assert_passes "multiple packages all good exits 0" "$TMPDIR_TEST/packages"

# ─── Test 11: real repo passes (post-#4088) ───────────────────────────
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: real repo scan exits 0 (post-#4088 baseline)"
else
    echo "FAIL: real repo scan exits non-zero — #4088 regression?"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 12: vitest.config.js variant is also scanned ────────────────
reset_tmpdir
mkdir -p "$TMPDIR_TEST/packages/web-js"
cat > "$TMPDIR_TEST/packages/web-js/vitest.config.js" <<'JSCFG'
module.exports = {
  test: {
    environment: 'jsdom',
  },
};
JSCFG
cat > "$TMPDIR_TEST/packages/web-js/package.json" <<'JSEOF'
{
  "name": "web-js",
  "version": "0.0.0",
  "devDependencies": {
    "vitest": "*"
  }
}
JSEOF
assert_fails "vitest.config.js variant flagged when jsdom missing" "$TMPDIR_TEST/packages"

# ─── Test 13: vitest.config.mjs variant is also scanned ───────────────
reset_tmpdir
mkdir -p "$TMPDIR_TEST/packages/web-mjs"
cat > "$TMPDIR_TEST/packages/web-mjs/vitest.config.mjs" <<'MJSCFG'
export default {
  test: {
    environment: 'jsdom',
  },
};
MJSCFG
cat > "$TMPDIR_TEST/packages/web-mjs/package.json" <<'JSEOF'
{
  "name": "web-mjs",
  "version": "0.0.0",
  "devDependencies": {
    "vitest": "*",
    "jsdom": "*"
  }
}
JSEOF
assert_passes "vitest.config.mjs variant accepted when jsdom present" "$TMPDIR_TEST/packages"

# ─── Test 14: usage error (missing packages dir) → exits 2 ────────────
TESTS=$((TESTS + 1))
set +e
"$CHECK_SCRIPT" "$TMPDIR_TEST/does-not-exist" > /dev/null 2>&1
rc=$?
set -e
if [[ $rc -eq 2 ]]; then
    echo "PASS: missing packages dir exits 2"
else
    echo "FAIL: missing packages dir expected exit 2, got $rc"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
