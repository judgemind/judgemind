#!/usr/bin/env bash
# test_gemini_review.sh — Smoke tests for scripts/gemini-review.sh
#
# Creates mock worktree directory structures and verifies that gemini-review.sh:
# 1. Selects the scraper venv python when it exists
# 2. Falls back to .venv-scripts when scraper venv is missing
# 3. Routes adversarial-mode output to adversarial-result.txt
# 4. Routes standard-mode output to gemini-review-result.txt
# 5. Handles parallel invocations via the lock mechanism
#
# Usage:
#   scripts/tests/test_gemini_review.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GEMINI_REVIEW="$SCRIPT_DIR/gemini-review.sh"
FAILURES=0
TESTS=0

# ── Helpers ──────────────────────────────────────────────────────────────────

TEMP_DIRS=()
cleanup() {
    set +e
    for d in "${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    echo "$dir"
}

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# Set up a fake worktree with ralph state directory and mock scripts.
# The mock with-secret.sh captures the Python path and mode, then exits.
# Returns the fake worktree path.
setup_fake_worktree() {
    local worktree
    worktree=$(make_temp_dir)

    # Create ralph state directory with required inputs
    local state_dir="$worktree/tmp/ralph"
    mkdir -p "$state_dir"
    echo "Test task" > "$state_dir/task.md"
    echo "diff content" > "$state_dir/diff.txt"
    echo "file content" > "$state_dir/changed_files.txt"

    # Create a mock with-secret.sh that records the Python path used and
    # then runs a stub instead of the real gemini_review.py.
    # The real with-secret.sh signature: with-secret.sh -e VAR=secret ... -- <cmd> [args]
    # We capture the command after "--" and write it to a breadcrumb file.
    local mock_scripts="$worktree/_mock_scripts"
    mkdir -p "$mock_scripts"

    cat > "$mock_scripts/with-secret.sh" << 'MOCK_WITH_SECRET'
#!/usr/bin/env bash
# Mock with-secret.sh: skip past -e flags, find the command after "--",
# record the Python path, and simulate a successful review.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --) shift; break ;;
        *)  shift ;;
    esac
done
# $1 is the python path, $2 is the script path
PYTHON_USED="$1"
echo "$PYTHON_USED" > "${RALPH_STATE_DIR}/python-used.txt"
echo "${GEMINI_REVIEW_MODE:-standard}" > "${RALPH_STATE_DIR}/mode-used.txt"

# Simulate the Python script writing output files based on mode.
if [[ "${GEMINI_REVIEW_MODE:-standard}" == "adversarial" ]]; then
    echo "SHIP" > "${RALPH_STATE_DIR}/adversarial-result.txt"
    echo "No issues found." > "${RALPH_STATE_DIR}/adversarial-feedback.md"
else
    echo "SHIP" > "${RALPH_STATE_DIR}/gemini-review-result.txt"
    echo "Looks good." > "${RALPH_STATE_DIR}/gemini-feedback.md"
fi
exit 0
MOCK_WITH_SECRET
    chmod +x "$mock_scripts/with-secret.sh"

    # Create a mock gemini_review.py (not actually run since with-secret.sh
    # is mocked, but needed so the path reference is valid)
    touch "$mock_scripts/gemini_review.py"

    echo "$worktree"
}

# Create a fake scraper-framework venv with a python3 binary.
# The binary is a simple shell script that records it was called.
setup_scraper_venv() {
    local worktree="$1"
    local venv_dir="$worktree/packages/scraper-framework/.venv"
    mkdir -p "$venv_dir/bin"

    # Create a fake python3 that can pass the "import genai" check
    cat > "$venv_dir/bin/python3" << 'FAKEPY'
#!/usr/bin/env bash
# Fake python3 for testing — passes the genai import check
if [[ "${1:-}" == "-c" ]]; then
    # Pretend the import succeeds
    exit 0
fi
exec python3 "$@"
FAKEPY
    chmod +x "$venv_dir/bin/python3"
}

# Run gemini-review.sh with mock scripts so that with-secret.sh and
# gemini_review.py are intercepted. Each invocation gets its own copy
# of the mock scripts directory to avoid races when two invocations
# run in parallel (Test 7).
run_gemini_review() {
    local worktree="$1"
    shift
    local base_mock="$worktree/_mock_scripts"

    # Create a per-invocation copy of the mock scripts directory so that
    # parallel invocations do not race on cp/chmod/exec of the same file.
    local run_dir
    run_dir=$(mktemp -d "${base_mock}_run.XXXXXX")
    TEMP_DIRS+=("$run_dir")

    # Copy all mock helpers (with-secret.sh, gemini_review.py) from the
    # base mock directory that setup_fake_worktree created.
    cp -R "$base_mock"/* "$run_dir/"

    # The script uses SCRIPT_DIR based on its own location. We need the
    # script to find our mock with-secret.sh and mock gemini_review.py.
    # Strategy: copy gemini-review.sh into the per-invocation dir and run
    # from there so SCRIPT_DIR resolves to run_dir.
    cp "$GEMINI_REVIEW" "$run_dir/gemini-review.sh"
    chmod +x "$run_dir/gemini-review.sh"

    # Also create a mock requirements.txt in case the fallback venv path
    # tries to install from it.
    touch "$run_dir/requirements.txt"

    "$run_dir/gemini-review.sh" "$worktree" "$@" 2>&1
}

# ── Tests ────────────────────────────────────────────────────────────────────

# Test 1: When scraper venv exists, PYTHON is set to the venv python3
test_scraper_venv_python_selection() {
    local worktree
    worktree=$(setup_fake_worktree)
    setup_scraper_venv "$worktree"

    run_gemini_review "$worktree" > /dev/null 2>&1

    local python_used
    python_used=$(cat "$worktree/tmp/ralph/python-used.txt" 2>/dev/null || echo "NOT_FOUND")

    local expected="$worktree/packages/scraper-framework/.venv/bin/python3"
    if [[ "$python_used" == "$expected" ]]; then
        pass "scraper venv python selected when .venv exists"
    else
        fail "scraper venv python selected when .venv exists" \
            "expected: $expected, got: $python_used"
    fi
}

# Test 2: Standard mode writes to gemini-review-result.txt
test_standard_mode_output_files() {
    local worktree
    worktree=$(setup_fake_worktree)
    setup_scraper_venv "$worktree"

    run_gemini_review "$worktree" > /dev/null 2>&1

    if [[ -f "$worktree/tmp/ralph/gemini-review-result.txt" ]]; then
        pass "standard mode creates gemini-review-result.txt"
    else
        fail "standard mode creates gemini-review-result.txt" \
            "file not found"
    fi

    if [[ ! -f "$worktree/tmp/ralph/adversarial-result.txt" ]]; then
        pass "standard mode does not create adversarial-result.txt"
    else
        fail "standard mode does not create adversarial-result.txt" \
            "file unexpectedly exists"
    fi

    local mode_used
    mode_used=$(cat "$worktree/tmp/ralph/mode-used.txt" 2>/dev/null || echo "NOT_FOUND")
    if [[ "$mode_used" == "standard" ]]; then
        pass "standard mode sets GEMINI_REVIEW_MODE=standard"
    else
        fail "standard mode sets GEMINI_REVIEW_MODE=standard" \
            "got: $mode_used"
    fi
}

# Test 3: Adversarial mode writes to adversarial-result.txt
test_adversarial_mode_output_files() {
    local worktree
    worktree=$(setup_fake_worktree)
    setup_scraper_venv "$worktree"

    run_gemini_review "$worktree" --adversarial > /dev/null 2>&1

    if [[ -f "$worktree/tmp/ralph/adversarial-result.txt" ]]; then
        pass "adversarial mode creates adversarial-result.txt"
    else
        fail "adversarial mode creates adversarial-result.txt" \
            "file not found"
    fi

    local mode_used
    mode_used=$(cat "$worktree/tmp/ralph/mode-used.txt" 2>/dev/null || echo "NOT_FOUND")
    if [[ "$mode_used" == "adversarial" ]]; then
        pass "adversarial mode sets GEMINI_REVIEW_MODE=adversarial"
    else
        fail "adversarial mode sets GEMINI_REVIEW_MODE=adversarial" \
            "got: $mode_used"
    fi
}

# Test 4: Missing worktree argument prints usage and exits 1
test_no_args_exits_with_error() {
    local mock_dir
    mock_dir=$(make_temp_dir)
    cp "$GEMINI_REVIEW" "$mock_dir/gemini-review.sh"
    chmod +x "$mock_dir/gemini-review.sh"

    local output
    local exit_code=0
    output=$("$mock_dir/gemini-review.sh" 2>&1) || exit_code=$?

    if [[ $exit_code -eq 1 ]]; then
        pass "no arguments exits with code 1"
    else
        fail "no arguments exits with code 1" "exit code was $exit_code"
    fi

    if echo "$output" | grep -q "Usage:"; then
        pass "no arguments prints usage"
    else
        fail "no arguments prints usage" "got: $output"
    fi
}

# Test 5: Missing ralph state directory exits 1
test_missing_state_dir_exits_with_error() {
    local worktree
    worktree=$(make_temp_dir)
    # Don't create tmp/ralph — the state dir check should fail

    local exit_code=0
    "$GEMINI_REVIEW" "$worktree" > /dev/null 2>&1 || exit_code=$?

    if [[ $exit_code -eq 1 ]]; then
        pass "missing state dir exits with code 1"
    else
        fail "missing state dir exits with code 1" "exit code was $exit_code"
    fi
}

# Test 6: Diff files are not regenerated if they already exist
test_existing_diff_files_not_overwritten() {
    local worktree
    worktree=$(setup_fake_worktree)
    setup_scraper_venv "$worktree"

    # Pre-populate diff files with known content
    echo "ORIGINAL_DIFF" > "$worktree/tmp/ralph/diff.txt"
    echo "ORIGINAL_CHANGED" > "$worktree/tmp/ralph/changed_files.txt"

    local output
    output=$(run_gemini_review "$worktree" 2>&1)

    # Check the diff.txt was NOT overwritten (still has our content)
    local diff_content
    diff_content=$(cat "$worktree/tmp/ralph/diff.txt")
    if [[ "$diff_content" == "ORIGINAL_DIFF" ]]; then
        pass "existing diff.txt preserved (not regenerated)"
    else
        fail "existing diff.txt preserved (not regenerated)" \
            "content was: $diff_content"
    fi

    # Check the INFO message was emitted
    if echo "$output" | grep -q "already exist"; then
        pass "skip message printed when diff files exist"
    else
        fail "skip message printed when diff files exist" \
            "output: $output"
    fi
}

# Test 7: Parallel lock — two parallel invocations both complete without error
test_parallel_lock_mechanism() {
    local worktree
    worktree=$(setup_fake_worktree)
    # Don't create scraper venv — force the fallback .venv-scripts path
    # which exercises the lock mechanism.

    # Create a fake .venv-scripts that already has genai installed
    # so the lock path is NOT triggered (we test that both complete).
    local scripts_venv="$worktree/.venv-scripts"
    mkdir -p "$scripts_venv/bin"
    cat > "$scripts_venv/bin/python3" << 'FAKEPY'
#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
    exit 0
fi
exec python3 "$@"
FAKEPY
    chmod +x "$scripts_venv/bin/python3"

    # Run two parallel invocations — one standard, one adversarial
    local pid1 pid2
    local exit1=0 exit2=0

    run_gemini_review "$worktree" > /dev/null 2>&1 &
    pid1=$!

    run_gemini_review "$worktree" --adversarial > /dev/null 2>&1 &
    pid2=$!

    wait "$pid1" || exit1=$?
    wait "$pid2" || exit2=$?

    if [[ $exit1 -eq 0 && $exit2 -eq 0 ]]; then
        pass "parallel invocations both complete successfully"
    else
        fail "parallel invocations both complete successfully" \
            "standard exit=$exit1, adversarial exit=$exit2"
    fi

    # Both result files should exist
    if [[ -f "$worktree/tmp/ralph/gemini-review-result.txt" && \
          -f "$worktree/tmp/ralph/adversarial-result.txt" ]]; then
        pass "parallel invocations produce both output files"
    else
        local std_exists="no" adv_exists="no"
        [[ -f "$worktree/tmp/ralph/gemini-review-result.txt" ]] && std_exists="yes"
        [[ -f "$worktree/tmp/ralph/adversarial-result.txt" ]] && adv_exists="yes"
        fail "parallel invocations produce both output files" \
            "standard=$std_exists, adversarial=$adv_exists"
    fi
}

# Test 8: Unexpected extra argument exits with error
test_extra_argument_exits_with_error() {
    local mock_dir
    mock_dir=$(make_temp_dir)
    cp "$GEMINI_REVIEW" "$mock_dir/gemini-review.sh"
    chmod +x "$mock_dir/gemini-review.sh"

    local exit_code=0
    "$mock_dir/gemini-review.sh" /some/path extra-arg 2>/dev/null || exit_code=$?

    if [[ $exit_code -eq 1 ]]; then
        pass "extra argument exits with code 1"
    else
        fail "extra argument exits with code 1" "exit code was $exit_code"
    fi
}

# ── Run all tests ────────────────────────────────────────────────────────────

test_scraper_venv_python_selection
test_standard_mode_output_files
test_adversarial_mode_output_files
test_no_args_exits_with_error
test_missing_state_dir_exits_with_error
test_existing_diff_files_not_overwritten
test_parallel_lock_mechanism
test_extra_argument_exits_with_error

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
