#!/usr/bin/env bash
# test_check_repo_walk_exclusions_canonical.sh — Tests for
# check-repo-walk-exclusions-canonical.sh.
#
# Covers:
#   a — A check script with hand-rolled --exclude-dir= literals fails.
#   b — A check script with inline `--exclude-dir='X'` flags fails.
#   c — A check script that sources preflight.sh and iterates
#       REPO_WALK_EXCLUSIONS passes.
#   d — A check script that uses neither --exclude-dir= nor any walk
#       (e.g. a wrapper script) is silently passed over.
#   e — Per-check extras (EXTRA_EXCLUDE_DIRS) on top of the canonical
#       list pass.
#   f — The current real `scripts/` directory passes (sanity / regression).
#   g — No self-match on the ci.yml step name that runs this guard.
#   h — Empty scan directory passes.
#
# Usage:
#   scripts/tests/test_check_repo_walk_exclusions_canonical.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-repo-walk-exclusions-canonical.sh"
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

# Stage a fake "scripts" dir under TMPDIR_TEST that the check can scan.
# We need to also stage a fake preflight.sh so the check's own source
# call succeeds (the check sources `$SCRIPT_DIR/preflight.sh`, where
# $SCRIPT_DIR is the directory of the *check itself*, not the scan
# dir — so we don't actually need to stage preflight.sh in the temp
# dir).
stage_check_script() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    chmod +x "$path" 2>/dev/null || true
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

# ─── Test a: hand-rolled EXCLUDE_DIRS array → fail ─────────────────
stage_check_script "check-foo.sh" '#!/usr/bin/env bash
# Hand-rolled — no source, no canonical reference.
EXCLUDE_DIRS=( ".git" ".venv" "node_modules" "__pycache__" )
exclude_args=()
for dir in "${EXCLUDE_DIRS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done
grep -rE "FOO" /tmp "${exclude_args[@]}"
' > /dev/null
assert_fails "Hand-rolled EXCLUDE_DIRS array is rejected"
reset_tmpdir

# ─── Test b: inline --exclude-dir='...' flags → fail ───────────────
stage_check_script "check-bar.sh" "#!/usr/bin/env bash
# Inline literal flags — no source, no canonical reference.
grep -rE \"BAR\" /tmp \\
    --exclude-dir='.git' \\
    --exclude-dir='.venv' \\
    --exclude-dir='node_modules'
" > /dev/null
assert_fails "Inline --exclude-dir literal flags are rejected"
reset_tmpdir

# ─── Test c: canonical-only consumer → pass ────────────────────────
stage_check_script "check-baz.sh" '#!/usr/bin/env bash
# Sources preflight.sh and iterates REPO_WALK_EXCLUSIONS.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done
grep -rE "BAZ" /tmp "${exclude_args[@]}"
' > /dev/null
assert_passes "Canonical-only consumer is accepted"
reset_tmpdir

# ─── Test d: no --exclude-dir at all → silently passes over ───────
stage_check_script "check-quux.sh" '#!/usr/bin/env bash
# Wrapper script — does not walk the repo.
echo "hello"
' > /dev/null
assert_passes "Script that does not use --exclude-dir is ignored"
reset_tmpdir

# ─── Test e: canonical + per-check extras → pass ──────────────────
stage_check_script "check-extras.sh" '#!/usr/bin/env bash
# Sources preflight.sh and adds per-check extras.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"
EXTRA_EXCLUDE_DIRS=(tests test)
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}" ${EXTRA_EXCLUDE_DIRS[@]+"${EXTRA_EXCLUDE_DIRS[@]}"}; do
    exclude_args+=("--exclude-dir=$dir")
done
grep -rE "EXTRA" /tmp "${exclude_args[@]}"
' > /dev/null
assert_passes "Canonical + EXTRA_EXCLUDE_DIRS consumer is accepted"
reset_tmpdir

# ─── Test f: real scripts/ directory → pass (regression) ──────────
# This is the load-bearing assertion: the migration in #4308 must
# leave the real scripts/ directory clean. If a future agent adds a
# new check-*.sh with hand-rolled excludes, this assertion fires.
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" "$SCRIPT_DIR" > /dev/null 2>&1; then
    echo "PASS: Real scripts/ directory is canonical-clean (regression)"
else
    echo "FAIL: Real scripts/ directory has hand-rolled --exclude-dir lists"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test g: empty scan dir → pass ────────────────────────────────
assert_passes "Empty scan directory passes"

# ─── Test h: No self-match on ci.yml step name ────────────────────
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-repo-walk-exclusions-canonical.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
