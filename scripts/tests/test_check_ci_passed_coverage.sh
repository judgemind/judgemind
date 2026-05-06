#!/usr/bin/env bash
# test_check_ci_passed_coverage.sh — Integration test for check-ci-passed-coverage.py (#3919, #4207)
#
# Runs the check against synthetic ci.yml fixtures covering both directions:
#   Fixture A — has a job missing from ci-passed.needs:    → must exit 1 (#3919)
#   Fixture B — all jobs covered                           → must exit 0
#   Fixture C — ci-passed.needs: has a stale/renamed entry → must exit 1 (#4207)
#   Fixture D — both missing AND stale entries             → must exit 1 (both messages)
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

# Fixture C — ci-passed.needs: contains a stale/renamed entry that no
# longer exists as a top-level job. Models the #2832 failure mode.
write_fixture_stale() {
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

  bare-shadcn-accent-check:
    runs-on: ubuntu-latest
    steps:
      - run: echo accent

  ci-passed:
    needs: [scripts-tests, admin-dispatcher-brand-accent-check]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Check all jobs passed or were skipped
        run: echo ok
EOF
}

# Fixture D — both directions wrong: scripts-tests is missing AND
# admin-dispatcher-brand-accent-check is stale. The script must report
# both failure modes in a single run.
write_fixture_both() {
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

  bare-shadcn-accent-check:
    runs-on: ubuntu-latest
    steps:
      - run: echo accent

  ci-passed:
    needs: [admin-dispatcher-brand-accent-check, bare-shadcn-accent-check]
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

# Assert the check exits 1 AND its combined output contains a substring.
# Used to verify the new failure-mode message names the offending entry.
assert_fails_with() {
    local desc="$1"
    local needle="$2"
    TESTS=$((TESTS + 1))
    local out
    local rc
    out=$(run_check 2>&1)
    rc=$?
    if [[ "$rc" -ne 1 ]]; then
        echo "FAIL: $desc (expected exit 1, got $rc)"
        echo "  output: $out"
        FAILURES=$((FAILURES + 1))
    elif ! grep -qF "$needle" <<<"$out"; then
        echo "FAIL: $desc (output did not contain '$needle')"
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

# ─── Test 3: stale entry in ci-passed.needs: is flagged (#4207) ──────────────
# A renamed/deleted job whose old name is still in ci-passed.needs:
# must exit 1 with a message naming the stale entry.
write_fixture_stale
assert_fails_with \
    "stale entry in ci-passed.needs: is flagged" \
    "admin-dispatcher-brand-accent-check"

# ─── Test 4: stale entry message tells reader to update array ────────────────
write_fixture_stale
assert_fails_with \
    "stale-entry message points reader to update the array" \
    "Fix: remove the stale entry"

# ─── Test 5: missing + stale fixture reports both directions (#4207) ─────────
write_fixture_both
assert_fails_with \
    "both missing and stale are reported in a single run (missing message)" \
    "are NOT listed in ci-passed.needs:"
write_fixture_both
assert_fails_with \
    "both missing and stale are reported in a single run (stale message)" \
    "do not correspond to any top-level job"

# ─── Test 6: docstring documents both directions (#4207) ─────────────────────
TESTS=$((TESTS + 1))
header=$(head -60 "$CHECK_SCRIPT")
if grep -qE 'stale|removed|renamed|nonexistent' <<<"$header"; then
    echo "PASS: docstring documents the stale/renamed/removed direction"
else
    echo "FAIL: docstring does not document the stale/removed direction"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 7: help flag works ──────────────────────────────────────────────────
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
