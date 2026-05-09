#!/usr/bin/env bash
# test_check_fix_block_coverage_complete.sh — end-to-end test for the
# per-guard Fix-block contract added in issue #4405.
#
# The Python helper has its own unit tests at
# scripts/tests/test_check_fix_block_coverage_complete.py.  This shell
# test runs the wrapper script (`scripts/check-fix-block-coverage-complete.sh`)
# against a synthesized scripts/ directory + inventory doc so the
# verify-line scenario in #4405 — "introduce a synthetic
# scripts/check-foo-bar.sh, run the meta-check, and confirm the stderr
# Fix block names a specific row number" — is asserted at the wrapper
# layer.  Catches a future regression where the wrapper drops the
# python3 invocation, mis-passes args, or restores the old generic Fix
# block.
#
# Usage
# -----
#   scripts/tests/test_check_fix_block_coverage_complete.sh
#
# Exit codes
# ----------
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WRAPPER="$REPO_ROOT/scripts/check-fix-block-coverage-complete.sh"
HELPER="$REPO_ROOT/scripts/check_fix_block_coverage_complete.py"

FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Helpers ─────────────────────────────────────────────────────────────

write_synth_inventory() {
    # write_synth_inventory <doc-path> — write a minimal inventory with
    # rows 11/11a/12, 23/23a/24, 37/38, 78/78a/79 — the same scaffolding
    # the unit tests use.  Total guards line set to 12.
    local doc="$1"
    cat > "$doc" <<'EOF'
# Hygiene-Guard Fix-Block Coverage

## Survey

| # | Guard | Verdict | Notes |
|---|-------|---------|-------|
| 11 | `scripts/check-ci-job-skipped.sh` | self-diagnosing (Fix block) | Note. |
| 11a | `scripts/check-ci-guards-skip-list-coverage.sh` | self-diagnosing (Fix block) | Note. |
| 12 | `scripts/check-ci-passed-coverage.sh` | self-diagnosing (Fix block) | Note. |
| 23 | `scripts/check-duplicate-pr.sh` | self-diagnosing (Fix block) | Note. |
| 23a | `scripts/check-fix-block-coverage-complete.sh` | self-diagnosing (Fix block) | Note. |
| 24 | `scripts/check-git-gh-retries.sh` | self-diagnosing (Fix block) | Note. |
| 37 | `scripts/check-no-api-github-fetch.sh` | self-diagnosing (Fix block) | Note. |
| 38 | `scripts/check-no-duplicate-stubs.sh` | self-diagnosing (Fix block) | Note. |
| 78 | `scripts/check-workflow-paths-filter-coverage.sh` | self-diagnosing (Fix block) | Note. |
| 78a | `scripts/check_no_basicconfig_with_extra.py` | self-diagnosing (Fix block) | Note. |
| 79 | `scripts/check_no_redos_pattern.py` | self-diagnosing (Fix block) | Note. |
| 84 | `scripts/check_tf_empty_resource.py` | self-diagnosing (Fix block) | Note. |

## Summary

- Total guards: 12 (synthetic).
EOF
}

write_synth_guard() {
    # write_synth_guard <scripts-dir> <basename>
    local scripts_dir="$1" name="$2"
    local path="$scripts_dir/$name"
    cat > "$path" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$path"
}

# ─── Test 1: Wrapper is happy when every guard is documented ─────────────

reset_tmpdir
mkdir -p "$TMPDIR_TEST/scripts"
write_synth_inventory "$TMPDIR_TEST/inventory.md"

# Stage a guard for every row in the synthetic inventory so the wrapper
# sees a clean diff. The dedup logic only flags hyphen-named .py files
# whose .sh sibling is missing — none here.
for name in \
    check-ci-job-skipped.sh \
    check-ci-guards-skip-list-coverage.sh \
    check-ci-passed-coverage.sh \
    check-duplicate-pr.sh \
    check-fix-block-coverage-complete.sh \
    check-git-gh-retries.sh \
    check-no-api-github-fetch.sh \
    check-no-duplicate-stubs.sh \
    check-workflow-paths-filter-coverage.sh; do
    write_synth_guard "$TMPDIR_TEST/scripts" "$name"
done
# Hyphen-named .py files in the underscore range — staged without .sh
# siblings so the dedup logic keeps them.  Must NOT be flagged because
# they're already in the inventory.
for name in \
    check_no_basicconfig_with_extra.py \
    check_no_redos_pattern.py \
    check_tf_empty_resource.py; do
    cat > "$TMPDIR_TEST/scripts/$name" <<'EOF'
#!/usr/bin/env python3
EOF
done

TESTS=$((TESTS + 1))
last_stderr=$(mktemp)
SCRIPTS_DIR_OVERRIDE="$TMPDIR_TEST/scripts" \
    DOC_OVERRIDE="$TMPDIR_TEST/inventory.md" \
    "$WRAPPER" 2> "$last_stderr"
rc=$?
if [[ $rc -eq 0 ]]; then
    echo "PASS: Test 1 — clean inventory exits 0"
else
    echo "FAIL: Test 1 — expected exit 0, got $rc"
    cat "$last_stderr"
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_stderr"

# ─── Test 2: Synthetic check-foo-bar.sh — issue #4405's verify line ──────

reset_tmpdir
mkdir -p "$TMPDIR_TEST/scripts"
write_synth_inventory "$TMPDIR_TEST/inventory.md"
for name in \
    check-ci-job-skipped.sh \
    check-ci-guards-skip-list-coverage.sh \
    check-ci-passed-coverage.sh \
    check-duplicate-pr.sh \
    check-fix-block-coverage-complete.sh \
    check-git-gh-retries.sh \
    check-no-api-github-fetch.sh \
    check-no-duplicate-stubs.sh \
    check-workflow-paths-filter-coverage.sh; do
    write_synth_guard "$TMPDIR_TEST/scripts" "$name"
done
for name in \
    check_no_basicconfig_with_extra.py \
    check_no_redos_pattern.py \
    check_tf_empty_resource.py; do
    cat > "$TMPDIR_TEST/scripts/$name" <<'EOF'
#!/usr/bin/env python3
EOF
done

# Add the synthetic guard the issue's verify line specifies.
write_synth_guard "$TMPDIR_TEST/scripts" "check-foo-bar.sh"

TESTS=$((TESTS + 1))
last_stderr=$(mktemp)
set +e
SCRIPTS_DIR_OVERRIDE="$TMPDIR_TEST/scripts" \
    DOC_OVERRIDE="$TMPDIR_TEST/inventory.md" \
    "$WRAPPER" 2> "$last_stderr"
rc=$?
set -e

# Expected: exit 1 + stderr names a specific row number + a row template
# with <guard> filled in + the new Total guards: 13 line.
#
# `check-foo-bar.sh` slots between #23a (`check-fix-block-coverage-complete.sh`)
# and #24 (`check-git-gh-retries.sh`) — alphabetically:
#   check-fix-block-coverage-complete.sh < check-foo-bar.sh < check-git-gh-retries.sh
# Prior peer is row 23a, base 23, taken letters {a} → next is `b`.
if [[ $rc -eq 1 ]] \
    && grep -q "Insert at row #23b" "$last_stderr" \
    && grep -q "between #23a \`check-fix-block-coverage-complete.sh\` and #24 \`check-git-gh-retries.sh\`" "$last_stderr" \
    && grep -q "| 23b | \`scripts/check-foo-bar.sh\` | <verdict> | <one-line note> |" "$last_stderr" \
    && grep -q "Total guards: 13" "$last_stderr"; then
    echo "PASS: Test 2 — synthetic check-foo-bar.sh produces #23b row + template + new total"
else
    echo "FAIL: Test 2 — Fix block did not name #23b + row template + Total guards: 13"
    echo "  rc: $rc"
    echo "  stderr:"
    cat "$last_stderr" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_stderr"

# ─── Test 3: Two missing guards both get individual Fix blocks ───────────

reset_tmpdir
mkdir -p "$TMPDIR_TEST/scripts"
write_synth_inventory "$TMPDIR_TEST/inventory.md"
for name in \
    check-ci-job-skipped.sh \
    check-ci-guards-skip-list-coverage.sh \
    check-ci-passed-coverage.sh \
    check-duplicate-pr.sh \
    check-fix-block-coverage-complete.sh \
    check-git-gh-retries.sh \
    check-no-api-github-fetch.sh \
    check-no-duplicate-stubs.sh \
    check-workflow-paths-filter-coverage.sh; do
    write_synth_guard "$TMPDIR_TEST/scripts" "$name"
done
for name in \
    check_no_basicconfig_with_extra.py \
    check_no_redos_pattern.py \
    check_tf_empty_resource.py; do
    cat > "$TMPDIR_TEST/scripts/$name" <<'EOF'
#!/usr/bin/env python3
EOF
done
write_synth_guard "$TMPDIR_TEST/scripts" "check-foo-bar.sh"
write_synth_guard "$TMPDIR_TEST/scripts" "check-zzz-trailing.sh"

TESTS=$((TESTS + 1))
last_stderr=$(mktemp)
set +e
SCRIPTS_DIR_OVERRIDE="$TMPDIR_TEST/scripts" \
    DOC_OVERRIDE="$TMPDIR_TEST/inventory.md" \
    "$WRAPPER" 2> "$last_stderr"
rc=$?
set -e

# Two missing guards: total should be 12 + 2 = 14.
# check-foo-bar.sh → row 23b (between 23a and 24).
# check-zzz-trailing.sh sorts after every existing hyphen-named row but
# before every underscore-named row. The last hyphen row in the synthetic
# inventory is #78 (check-workflow-paths-filter-coverage.sh).  Its
# letter-sibling slots (78a) is taken by check_no_basicconfig_with_extra.py,
# so check-zzz-trailing.sh slots at 78b.  But wait — alphabetically
# check-zzz-* < check_no_* (hyphen < underscore), so prior is row 78
# (no taken letters at base 78 in the prior set up to and including the
# prior position) — actually check_no_basicconfig_with_extra.py is at
# 78a, so taken_letters at base=78 includes {a} and the next free letter
# is `b`. Result: row #78b.
if [[ $rc -eq 1 ]] \
    && grep -q "Insert at row #23b" "$last_stderr" \
    && grep -q "Insert at row #78b" "$last_stderr" \
    && grep -q "Total guards: 14" "$last_stderr"; then
    echo "PASS: Test 3 — two missing guards produce two distinct Fix blocks + new total: 14"
else
    echo "FAIL: Test 3 — Fix output did not name #23b + #78b + Total guards: 14"
    echo "  rc: $rc"
    echo "  stderr:"
    cat "$last_stderr" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_stderr"

# ─── Test 4: Live wrapper passes against the real inventory ──────────────
# Defensive — production inventory is in sync with production guards;
# the wrapper must exit 0 when invoked with no overrides.

TESTS=$((TESTS + 1))
if "$WRAPPER" > /dev/null 2>&1; then
    echo "PASS: Test 4 — live wrapper exits 0 against the real inventory"
else
    echo "FAIL: Test 4 — live wrapper failed (real inventory out of sync?)"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 5: Helper module is discoverable via python3 invocation ────────
# The wrapper invokes `python3 $REPO_ROOT/scripts/check_fix_block_coverage_complete.py`.
# Sanity check that --help works as a smoke for the helper being on disk
# and importable.

TESTS=$((TESTS + 1))
if python3 "$HELPER" --help > /dev/null 2>&1; then
    echo "PASS: Test 5 — helper module --help works"
else
    echo "FAIL: Test 5 — helper module not importable / --help failed"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
