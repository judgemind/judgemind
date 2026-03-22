#!/usr/bin/env bash
# check-shard-coverage.sh — Ensure all court test files are listed in CI shard config.
#
# The scraper-court-tests CI job in .github/workflows/ci.yml is split into
# matrix shards with explicit test-paths.  If a developer adds a new test file
# to tests/courts/ without updating the shard configuration, that test will
# silently never run in CI.
#
# This script enumerates every test_*.py file (and test subdirectory) under
# packages/scraper-framework/tests/courts/ and verifies each one is covered
# by at least one shard's test-paths list.  A file is "covered" if it appears
# directly in a shard, or if a parent directory entry covers it.
#
# Usage:
#   scripts/check-shard-coverage.sh          # exits 0 if all covered, 1 if gaps
#   scripts/check-shard-coverage.sh --verbose # list all test files and their shard
#
# Exit codes:
#   0 — All court test files are covered by the shard configuration.
#   1 — One or more test files are not covered by any shard.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CI_YML="$REPO_ROOT/.github/workflows/ci.yml"
COURTS_DIR="$REPO_ROOT/packages/scraper-framework/tests/courts"
VERBOSE="${1:-}"

if [[ ! -f "$CI_YML" ]]; then
    echo "ERROR: Cannot find $CI_YML"
    exit 1
fi

if [[ ! -d "$COURTS_DIR" ]]; then
    echo "ERROR: Cannot find $COURTS_DIR"
    exit 1
fi

# ─── Step 1: Enumerate all test files under tests/courts/ ────────────────
# Paths are relative to packages/scraper-framework/ to match the CI config.

test_files=()
while IFS= read -r filepath; do
    # Convert absolute path to relative (from packages/scraper-framework/)
    rel_path="${filepath#"$REPO_ROOT"/packages/scraper-framework/}"
    test_files+=("$rel_path")
done < <(find "$COURTS_DIR" -name 'test_*.py' -type f | sort)

if [[ ${#test_files[@]} -eq 0 ]]; then
    echo "WARNING: No test_*.py files found in $COURTS_DIR"
    exit 0
fi

# ─── Step 2: Extract shard test-paths from ci.yml ────────────────────────
# We look for lines matching the test-paths entries inside the
# scraper-court-tests matrix.  The format is a YAML folded scalar (>-)
# with one path per line indented under test-paths.
#
# Strategy: find the scraper-court-tests job, then extract all lines that
# look like test paths (tests/courts/...) within the matrix section.

shard_paths=()
in_court_tests=false
in_matrix=false
in_test_paths=false
test_paths_indent=""

while IFS= read -r line; do
    # Detect the scraper-court-tests job
    if [[ "$line" =~ ^[[:space:]]*scraper-court-tests: ]]; then
        in_court_tests=true
        continue
    fi

    # If we're past scraper-court-tests, detect next top-level job and stop
    if $in_court_tests && [[ "$line" =~ ^[[:space:]][[:space:]][a-z] ]] && [[ ! "$line" =~ ^[[:space:]]{4,} ]]; then
        break
    fi

    if ! $in_court_tests; then
        continue
    fi

    # Look for test-paths entries
    if [[ "$line" =~ ^[[:space:]]*test-paths: ]]; then
        in_test_paths=true
        # Determine the indent level of the next line (the paths)
        test_paths_indent=""
        continue
    fi

    if $in_test_paths; then
        # If we haven't set the indent yet, use this line's indent
        if [[ -z "$test_paths_indent" ]]; then
            if [[ "$line" =~ ^([[:space:]]+)tests/ ]]; then
                test_paths_indent="${BASH_REMATCH[1]}"
            else
                # Not a test path line — end of test-paths
                in_test_paths=false
                continue
            fi
        fi

        # If the line starts with the expected indent and is a test path
        if [[ "$line" =~ ^[[:space:]]+tests/courts/ ]]; then
            # Extract the path (trim whitespace)
            path="$(echo "$line" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')"
            shard_paths+=("$path")
        else
            # End of test-paths block
            in_test_paths=false
        fi
    fi
done < "$CI_YML"

if [[ ${#shard_paths[@]} -eq 0 ]]; then
    echo "ERROR: Could not extract any test paths from scraper-court-tests in $CI_YML"
    echo "Expected to find test-paths entries under the scraper-court-tests matrix."
    exit 1
fi

# ─── Step 3: Check coverage ─────────────────────────────────────────────
# A test file is covered if:
#   (a) It appears directly in the shard paths list, OR
#   (b) A directory entry in the shard paths is a prefix of the test file path
#       (e.g., tests/courts/ca/ covers tests/courts/ca/test_la_title_utils.py)

uncovered=()

for test_file in "${test_files[@]}"; do
    covered=false

    for shard_path in "${shard_paths[@]}"; do
        # Direct match
        if [[ "$test_file" == "$shard_path" ]]; then
            covered=true
            break
        fi

        # Directory match: if shard_path ends with /, check if test_file is under it
        if [[ "$shard_path" == */ ]] && [[ "$test_file" == "$shard_path"* ]]; then
            covered=true
            break
        fi
    done

    if $covered; then
        if [[ "$VERBOSE" == "--verbose" ]]; then
            echo "  COVERED: $test_file"
        fi
    else
        uncovered+=("$test_file")
        if [[ "$VERBOSE" == "--verbose" ]]; then
            echo "  MISSING: $test_file"
        fi
    fi
done

# ─── Step 4: Report results ─────────────────────────────────────────────

echo "Court test shard coverage: ${#test_files[@]} test files, ${#shard_paths[@]} shard entries"

if [[ ${#uncovered[@]} -gt 0 ]]; then
    echo ""
    echo "ERROR: ${#uncovered[@]} test file(s) not covered by any shard in ci.yml:"
    echo ""
    for f in "${uncovered[@]}"; do
        echo "  $f"
    done
    echo ""
    echo "Fix: Add the missing file(s) to a shard's test-paths in"
    echo "  .github/workflows/ci.yml under the scraper-court-tests matrix."
    echo ""
    echo "See https://github.com/judgemind/judgemind/issues/1675 for context."
    exit 1
fi

echo "All court test files are covered by the shard configuration."
exit 0
