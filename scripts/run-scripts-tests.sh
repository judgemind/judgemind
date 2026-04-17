#!/usr/bin/env bash
# run-scripts-tests.sh — auto-discover and run scripts/tests/*.sh shell tests.
#
# This is the runner logic for the `scripts-tests` CI job's "Run all
# scripts/tests shell tests" step. Extracting it into a standalone script
# makes the filter logic unit-testable (see
# scripts/tests/test_scripts_tests_runner.sh).
#
# Discovery rules:
#   1. Glob scripts/tests/*.sh (configurable via TESTS_DIR env var).
#   2. Skip files whose basename starts with an underscore ('_*.sh') —
#      these are shared helpers meant to be sourced, not executed as
#      tests.
#   3. Skip (defer) files listed in SKIP_TESTS (whitespace-separated
#      repo-relative paths) — these run in a dedicated CI job.
#   4. Warn on (and skip) files that are not executable.
#   5. Run the remainder. Exit 1 if any failed, 0 otherwise.
#
# Environment variables:
#   SKIP_TESTS   — whitespace-separated list of repo-relative test paths
#                  to defer (e.g. "scripts/tests/test_pre_push.sh").
#   TESTS_DIR    — tests directory (default: "scripts/tests"). Testing
#                  hook: the unit test sets this to a temp directory.
#
# Exit codes:
#   0 — all discovered tests passed (or none were discovered).
#   1 — one or more tests failed.

set -uo pipefail
shopt -s nullglob

: "${SKIP_TESTS:=}"
: "${TESTS_DIR:=scripts/tests}"

tests=("$TESTS_DIR"/*.sh)
if [ ${#tests[@]} -eq 0 ]; then
    echo "No shell test files found under $TESTS_DIR/ — nothing to run."
    exit 0
fi

# Returns 0 if $1 is listed in $SKIP_TESTS (whitespace-separated).
is_skipped() {
    local target="$1"
    local entry
    for entry in $SKIP_TESTS; do
        if [ "$entry" = "$target" ]; then
            return 0
        fi
    done
    return 1
}

# Returns 0 if the basename of $1 starts with an underscore, indicating
# a shared helper file that should be sourced, not executed as a test.
is_helper() {
    local base
    base="$(basename "$1")"
    case "$base" in
        _*) return 0 ;;
        *)  return 1 ;;
    esac
}

failed=0
ran=0
skipped=0
deferred=0
helpers=0

for t in "${tests[@]}"; do
    if is_helper "$t"; then
        # Shared helpers (e.g. scripts/tests/_guard_self_match_helpers.sh)
        # are sourced by peer tests, not executed standalone. Silently skip.
        helpers=$((helpers + 1))
        continue
    fi
    if is_skipped "$t"; then
        echo "::notice::Deferring $t — covered by a dedicated CI job"
        deferred=$((deferred + 1))
        continue
    fi
    if [ ! -x "$t" ]; then
        echo "::warning::Skipping $t — not executable (chmod +x required)"
        skipped=$((skipped + 1))
        continue
    fi
    echo "::group::$t"
    if ! "$t"; then
        echo "::error::$t failed"
        failed=$((failed + 1))
    fi
    ran=$((ran + 1))
    echo "::endgroup::"
done

echo "Ran $ran shell test(s). Helpers skipped (underscore prefix): $helpers. Skipped (not executable): $skipped. Deferred to dedicated jobs: $deferred. Failed: $failed."

if [ "$failed" -gt 0 ]; then
    echo "::error::$failed shell test(s) failed."
    exit 1
fi
exit 0
