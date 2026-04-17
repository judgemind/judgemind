#!/usr/bin/env bash
# _guard_self_match_helpers.sh — shared helper for guard self-match tests.
#
# Peer guard scripts (scripts/check-no-*.sh, scripts/check-forbidden-*.sh,
# scripts/check-deprecated-*.sh, etc.) forbid specific string patterns.
# When a CI step name quotes that pattern, the guard matches its own
# invocation and fails on the first CI run.  This happened on PR #2541
# — see issue #2542 for the motivation.
#
# This helper adds a `assert_no_self_match_on_ci_step_name` function
# that each guard's shell test can call once.  It:
#   1. Parses `.github/workflows/ci.yml` for every step whose `run:`
#      line invokes the guard.
#   2. Writes the human-readable step names into a temp file inside
#      the test's TMPDIR_TEST, using an extension the guard will scan.
#   3. Runs the guard against TMPDIR_TEST and records the result
#      using the test's existing assert_passes helper.
#
# If no CI step currently runs the guard, the assertion still passes
# (there's nothing to self-match).
#
# Prerequisites
# ─────────────
# The caller must have already defined:
#   - `$CHECK_SCRIPT`           — absolute path to the guard script.
#   - `$TMPDIR_TEST`            — test's isolated temp dir.
#   - `assert_passes <desc>`    — test-suite helper from the caller.
#   - `reset_tmpdir`            — test-suite helper from the caller.
#
# Usage
# ─────
# After the last numbered test in the file:
#
#   source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
#   assert_no_self_match_on_ci_step_name \
#       "scripts/check-no-ecs-wait-services-stable.sh" "yml"
#
# The first argument is the guard's repo-relative path (this is how
# ci.yml refers to it).  The second argument is the file extension
# to use when writing step names — pick one the guard scans:
#   - check-aws-bool-flags.sh   → "sh"  (scans only *.sh)
#   - check-deprecated-models.sh → "yml" (scans everything)
#   - check-no-*.sh             → "yml" (scans everything)

# Resolve repo root from the location of this helper file.  The helper
# lives at scripts/tests/_guard_self_match_helpers.sh so .. .. is
# always the repo root regardless of where the caller is invoked from.
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT_GUESS="$(cd "$_SELF_DIR/../.." && pwd)"

# Path to the awk extractor that pulls step names out of ci.yml.
_EXTRACTOR_AWK="$_SELF_DIR/_extract_guard_step_names.awk"

# Default ci.yml path — callers can override via CI_YML_PATH env var
# (useful for unit-testing the helper itself).
: "${CI_YML_PATH:=$_REPO_ROOT_GUESS/.github/workflows/ci.yml}"

# assert_no_self_match_on_ci_step_name <guard-repo-path> <extension>
#
# Runs $CHECK_SCRIPT against a synthesized file containing every
# current ci.yml step name that invokes $guard-repo-path.  Reports
# the result via the caller's assert_passes helper.
assert_no_self_match_on_ci_step_name() {
    local guard_repo_path="$1"
    local extension="${2:-yml}"

    if [[ ! -f "$CI_YML_PATH" ]]; then
        # If ci.yml is missing (unusual), don't fail the whole suite —
        # just run the assert_passes with an empty tmpdir so the
        # test count stays in sync.
        echo "WARN: ci.yml not found at $CI_YML_PATH — skipping self-match assertion"
        assert_passes "No self-match on ci.yml step name for $guard_repo_path (ci.yml missing — skipped)"
        return
    fi

    if [[ ! -f "$_EXTRACTOR_AWK" ]]; then
        echo "ERROR: extractor awk script missing at $_EXTRACTOR_AWK"
        return 1
    fi

    # Extract step names (one per line).  Empty output is valid.
    local names_file="$TMPDIR_TEST/ci_step_names.$extension"
    awk -v script="$guard_repo_path" -f "$_EXTRACTOR_AWK" "$CI_YML_PATH" \
        > "$names_file"

    # Run the guard against TMPDIR_TEST using the test's standard
    # assert_passes helper.  If the step names trip the guard,
    # assert_passes reports FAIL.
    assert_passes "No self-match on ci.yml step name for $guard_repo_path"

    # Clean up the synthesized file so subsequent tests in the suite
    # start from a known-empty tmpdir.
    reset_tmpdir
}
