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
#   4. Apply SHARD_FILTER (#4067) — the long-pole shell tests are
#      partitioned into a dedicated shard so the rest can run in parallel:
#        * SHARD_FILTER unset (default) — run every discovered test
#          (legacy behavior; out-of-band callers stay working).
#        * SHARD_FILTER=slow            — run ONLY tests listed in
#          SLOW_TESTS.
#        * SHARD_FILTER=not-slow        — skip every test listed in
#          SLOW_TESTS; run the rest.
#      SLOW_TESTS is a whitespace-separated list of repo-relative paths
#      (same shape as SKIP_TESTS). Defaults to the long-pole entrypoint
#      test that issue #4067 split out.
#   5. Warn on (and skip) files that are not executable.
#   6. Run the remainder. Exit 1 if any failed, 0 otherwise.
#
# Environment variables:
#   SKIP_TESTS    — whitespace-separated list of repo-relative test paths
#                   to defer (e.g. "scripts/tests/test_pre_push.sh").
#   SHARD_FILTER  — one of "", "slow", "not-slow" (see #4 above).
#   SLOW_TESTS    — whitespace-separated list of repo-relative paths
#                   considered "slow". See the inline assignment below
#                   for the current list and observed wall-clocks.
#   TESTS_DIR     — tests directory (default: "scripts/tests"). Testing
#                   hook: the unit test sets this to a temp directory.
#
# Exit codes:
#   0 — all discovered tests passed (or none were discovered).
#   1 — one or more tests failed, OR SHARD_FILTER is invalid.

set -uo pipefail
shopt -s nullglob

: "${SKIP_TESTS:=}"
: "${TESTS_DIR:=scripts/tests}"
: "${SHARD_FILTER:=}"
# SLOW_TESTS — paths that should run on the shell-slow shard, not shell-fast.
# Tracked observed wall-clock from CI run 25401898705 (#4067):
#   * test_agent_runner_entrypoint.sh    — ~11 min  (long pole)
# Add a path here when one climbs past ~5 min on a triggering CI run.
# test_dev_db_query.sh (~5m25s) is the next-largest single test; it stays in
# shell-fast for now so shell-slow doesn't sequentially run two long poles
# (which would push max parallel wall-clock past the issue's ≤10 min target
# in the wrong direction).
: "${SLOW_TESTS:=scripts/tests/test_agent_runner_entrypoint.sh}"

case "$SHARD_FILTER" in
    ""|slow|not-slow)
        ;;
    *)
        echo "::error::Invalid SHARD_FILTER='$SHARD_FILTER'. Expected '', 'slow', or 'not-slow'." >&2
        exit 1
        ;;
esac

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

# Returns 0 if $1 is listed in $SLOW_TESTS (whitespace-separated).
is_slow() {
    local target="$1"
    local entry
    for entry in $SLOW_TESTS; do
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
filtered=0

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
    # SHARD_FILTER (#4067): partition between fast and slow shards.
    case "$SHARD_FILTER" in
        slow)
            if ! is_slow "$t"; then
                filtered=$((filtered + 1))
                continue
            fi
            ;;
        not-slow)
            if is_slow "$t"; then
                filtered=$((filtered + 1))
                continue
            fi
            ;;
    esac
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

echo "Ran $ran shell test(s). Helpers skipped (underscore prefix): $helpers. Skipped (not executable): $skipped. Deferred to dedicated jobs: $deferred. Filtered by SHARD_FILTER='$SHARD_FILTER': $filtered. Failed: $failed."

if [ "$failed" -gt 0 ]; then
    echo "::error::$failed shell test(s) failed."
    exit 1
fi
exit 0
