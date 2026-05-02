#!/usr/bin/env bash
# test_check_ci_passed_coverage.sh — Integration test for check-ci-passed-coverage.py (#3919)
#
# Runs the check against two synthetic ci.yml fixtures:
#   Fixture A — has a job missing from ci-passed.needs:  → must exit 1
#   Fixture B — all jobs covered                         → must exit 0
#
# Usage:
#   scripts/tests/test_check_ci_passed_coverage.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-ci-passed-coverage.py"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

mkdir -p "$TMPDIR_TEST/.github/workflows"

# Fixture A — extra-job is defined but NOT in ci-passed.needs:
write_fixture_missing() {
    cat > "$TMPDIR_TEST/.github/workflows/ci.yml" <<'EOF'
name: CI
on:
  push:
jobs:
  detect-changes:
    runs-on: ubuntu-latest
    steps:
      - run: echo detecting

  scripts-tests:
    needs: detect-changes
    runs-on: ubuntu-latest
    steps:
      - run: pytest

  extra-job:
    runs-on: ubuntu-latest
    steps:
      - run: echo extra

  ci-passed:
    needs: [scripts-tests]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Check all jobs passed or were skipped
        run: echo ok
EOF
}

# Fixture B — all jobs (except allow-listed) are in ci-passed.needs:
write_fixture_complete() {
    cat > "$TMPDIR_TEST/.github/workflows/ci.yml" <<'EOF'
name: CI
on:
  push:
jobs:
  detect-changes:
    runs-on: ubuntu-latest
    steps:
      - run: echo detecting

  scripts-tests:
    needs: detect-changes
    runs-on: ubuntu-latest
    steps:
      - run: pytest

  extra-job:
    runs-on: ubuntu-latest
    steps:
      - run: echo extra

  ci-passed:
    needs: [scripts-tests, extra-job]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Check all jobs passed or were skipped
        run: echo ok
EOF
}

run_check() {
    python3 "$CHECK_SCRIPT" --ci-yml "$TMPDIR_TEST/.github/workflows/ci.yml"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    local out
    local rc
    out=$(run_check 2>&1)
    rc=$?
    if [[ "$rc" -ne 1 ]]; then
        echo "FAIL: $desc (expected exit 1, got $rc)"
        echo "  output: $out"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    local out
    local rc
    out=$(run_check 2>&1)
    rc=$?
    if [[ "$rc" -ne 0 ]]; then
        echo "FAIL: $desc (expected exit 0, got $rc)"
        echo "  output: $out"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# ─── Test 1: job missing from ci-passed.needs: is flagged ────────────────────
write_fixture_missing
assert_fails "extra-job missing from ci-passed.needs: is flagged"

# ─── Test 2: all jobs present in ci-passed.needs: passes ─────────────────────
write_fixture_complete
assert_passes "all jobs listed in ci-passed.needs: passes"

# ─── Test 3: help flag works ──────────────────────────────────────────────────
TESTS=$((TESTS + 1))
if python3 "$CHECK_SCRIPT" --help 2>&1 | grep -q "ci-passed"; then
    echo "PASS: --help describes the script"
else
    echo "FAIL: --help output does not include expected description"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
