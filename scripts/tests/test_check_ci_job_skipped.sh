#!/usr/bin/env bash
# test_check_ci_job_skipped.sh — Integration test for check-ci-job-skipped.py (#2527)
#
# Runs the check against synthetic ci.yml fixtures and synthetic unified
# diffs to verify end-to-end behavior without requiring a git repo state.
#
# Usage:
#   scripts/tests/test_check_ci_job_skipped.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-ci-job-skipped.py"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# The checker locates ci.yml via --ci-yml RELATIVE to --repo-root, so we
# create a fake repo layout with .github/workflows/ci.yml inside TMPDIR_TEST.
mkdir -p "$TMPDIR_TEST/.github/workflows"

write_ci_yml() {
    local include_ci_yml_in_scripts_filter="$1"
    # IMPORTANT: match the indent of the `- 'scripts/**'` line below
    # (14 spaces before the hyphen) so the generated YAML is well-formed.
    local ci_yml_line="              - '.github/workflows/ci.yml'"
    if [[ "$include_ci_yml_in_scripts_filter" != "yes" ]]; then
        ci_yml_line=""
    fi
    cat > "$TMPDIR_TEST/.github/workflows/ci.yml" <<EOF
name: CI
on:
  push:
jobs:
  detect-changes:
    runs-on: ubuntu-latest
    steps:
      - uses: dorny/paths-filter@v4
        id: changes
        with:
          filters: |
            scripts:
              - 'scripts/**'
$ci_yml_line
            hooks:
              - '.claude/hooks/**'

  scripts-tests:
    needs: detect-changes
    if: needs.detect-changes.outputs.scripts == 'true'
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: echo new-implementation-here
EOF
}

# Line 26 is inside scripts-tests body in the fixture with ci.yml in filter;
# line 25 without. We'll compute the exact line of 'Run tests' dynamically
# in the diff fixture by anchoring to hunk header.

# Helper: write a unified diff that modifies a line inside scripts-tests.
# Writes: diff against an imaginary prior version where `echo new-...`
# was `echo old-implementation`.
write_diff_ci_yml_change_only() {
    # We don't know the exact line, but that's fine — we just craft a diff
    # that says "change line N in ci.yml". We use line 25 which is inside
    # the scripts-tests body in the non-ci.yml-in-filter fixture, and 26
    # in the ci.yml-in-filter fixture. Use a range that contains both.
    cat > "$TMPDIR_TEST/diff.patch" <<'EOF'
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index abc..def 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -20,4 +20,4 @@ jobs:
     runs-on: ubuntu-latest
     steps:
       - name: Run tests
-        run: echo old-implementation
+        run: echo new-implementation-here
EOF
}

write_diff_with_matching_script_file() {
    # Diff changes BOTH ci.yml AND a scripts/ file. The script-file change
    # causes the 'scripts' filter to match even if ci.yml isn't in the
    # filter, so the check should pass.
    cat > "$TMPDIR_TEST/diff.patch" <<'EOF'
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index abc..def 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -20,4 +20,4 @@ jobs:
     runs-on: ubuntu-latest
     steps:
       - name: Run tests
-        run: echo old-implementation
+        run: echo new-implementation-here
diff --git a/scripts/new-tool.sh b/scripts/new-tool.sh
new file mode 100644
--- /dev/null
+++ b/scripts/new-tool.sh
@@ -0,0 +1,1 @@
+#!/bin/bash
EOF
}

write_diff_no_ci_yml_change() {
    cat > "$TMPDIR_TEST/diff.patch" <<'EOF'
diff --git a/scripts/unrelated.sh b/scripts/unrelated.sh
index abc..def 100644
--- a/scripts/unrelated.sh
+++ b/scripts/unrelated.sh
@@ -1,1 +1,1 @@
-old
+new
EOF
}

run_check() {
    python3 "$CHECK_SCRIPT" \
        --repo-root "$TMPDIR_TEST" \
        --diff-file "$TMPDIR_TEST/diff.patch"
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

# ─── Test 1: scripts-tests modified, ci.yml NOT in filter, no scripts/ change ──
# This is the #2505 pre-fix scenario: expected to FAIL.
write_ci_yml "no"
write_diff_ci_yml_change_only
assert_fails "Modifying scripts-tests with ci.yml not in filter is flagged"

# ─── Test 2: scripts-tests modified, ci.yml IS in filter ────────────────
# This is the #2505 post-fix scenario: expected to PASS.
write_ci_yml "yes"
write_diff_ci_yml_change_only
assert_passes "Modifying scripts-tests with ci.yml in filter passes"

# ─── Test 3: scripts-tests modified, ci.yml NOT in filter, but a scripts/ file changed ──
# The 'scripts' filter will still match because scripts/new-tool.sh matches.
write_ci_yml "no"
write_diff_with_matching_script_file
assert_passes "Modifying scripts-tests with a matching scripts/ file change passes"

# ─── Test 4: No ci.yml change at all ────────────────────────────────────
write_ci_yml "no"
write_diff_no_ci_yml_change
assert_passes "Unrelated diff with no ci.yml change passes"

# ─── Test 5: Help flag works ─────────────────────────────────────────────
TESTS=$((TESTS + 1))
if python3 "$CHECK_SCRIPT" --help 2>&1 | grep -q "Detect CI jobs"; then
    echo "PASS: --help describes the script"
else
    echo "FAIL: --help output does not include expected description"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ───────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
