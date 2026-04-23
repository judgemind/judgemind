#!/usr/bin/env bash
# test_run_py.sh — Tests for run-py.sh venv dispatcher
#
# Creates temporary directory structures to verify that run-py.sh correctly
# reads # venv: headers, finds venvs, handles errors, and passes arguments.
#
# Usage:
#   scripts/tests/test_run_py.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_PY="$SCRIPT_DIR/run-py.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

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

# Create a fake repo with a venv that has deps installed.
# Returns the repo root path.
setup_fake_repo() {
    local repo
    repo=$(make_temp_dir)

    # Create repo root markers: .git directory (not file) + CLAUDE.md
    # run-py.sh uses find_repo_root() which requires both.
    mkdir -p "$repo/.git"
    touch "$repo/CLAUDE.md"

    # Create the run-py.sh in the expected location
    mkdir -p "$repo/scripts"
    cp "$RUN_PY" "$repo/scripts/run-py.sh"
    chmod +x "$repo/scripts/run-py.sh"

    # Create a fake package venv with deps
    local pkg_dir="$repo/packages/test-pkg"
    local venv_dir="$pkg_dir/.venv"
    mkdir -p "$venv_dir/bin"
    mkdir -p "$venv_dir/lib/python3.12/site-packages/somelib-1.0.dist-info"

    # Create a python3 "interpreter" that just runs python3 from PATH
    cat > "$venv_dir/bin/python3" << 'PYSHIM'
#!/usr/bin/env bash
exec python3 "$@"
PYSHIM
    chmod +x "$venv_dir/bin/python3"

    echo "$repo"
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: No arguments
test_no_args() {
    local output
    output=$("$RUN_PY" 2>&1 || true)
    if echo "$output" | grep -q "Usage:"; then
        pass "no arguments prints usage"
    else
        fail "no arguments prints usage" "got: $output"
    fi

    # Should exit 1
    if "$RUN_PY" > /dev/null 2>&1; then
        fail "no arguments exits with code 1" "exit code was 0"
    else
        pass "no arguments exits with code 1"
    fi
}

# Test 2: Nonexistent script
test_nonexistent_script() {
    local output
    output=$("$RUN_PY" /nonexistent/script.py 2>&1 || true)
    if echo "$output" | grep -q "script not found"; then
        pass "nonexistent script prints error"
    else
        fail "nonexistent script prints error" "got: $output"
    fi

    if "$RUN_PY" /nonexistent/script.py > /dev/null 2>&1; then
        fail "nonexistent script exits with code 1" "exit code was 0"
    else
        pass "nonexistent script exits with code 1"
    fi
}

# Test 3: Script without # venv: header runs with system python + warning
test_no_venv_header() {
    local repo
    repo=$(setup_fake_repo)

    cat > "$repo/scripts/no_header.py" << 'PYSCRIPT'
#!/usr/bin/env python3
import sys
print("hello from no_header")
sys.exit(0)
PYSCRIPT

    local output
    local stderr_output
    output=$("$repo/scripts/run-py.sh" "$repo/scripts/no_header.py" 2>"$repo/stderr.txt")
    stderr_output=$(cat "$repo/stderr.txt")

    if echo "$stderr_output" | grep -q "WARNING.*no.*venv.*header"; then
        pass "no venv header prints warning to stderr"
    else
        fail "no venv header prints warning to stderr" "stderr: $stderr_output"
    fi

    if echo "$output" | grep -q "hello from no_header"; then
        pass "no venv header script runs successfully"
    else
        fail "no venv header script runs successfully" "stdout: $output"
    fi
}

# Test 4: Script with # venv: header but missing venv directory
test_missing_venv() {
    local repo
    repo=$(setup_fake_repo)

    cat > "$repo/scripts/missing_venv.py" << 'PYSCRIPT'
#!/usr/bin/env python3
# venv: nonexistent-pkg
print("should not reach here")
PYSCRIPT

    local output
    output=$("$repo/scripts/run-py.sh" "$repo/scripts/missing_venv.py" 2>&1 || true)

    if echo "$output" | grep -q "venv not found"; then
        pass "missing venv prints error"
    else
        fail "missing venv prints error" "got: $output"
    fi

    if echo "$output" | grep -q "python3.12 -m venv"; then
        pass "missing venv shows creation instructions"
    else
        fail "missing venv shows creation instructions" "got: $output"
    fi

    if "$repo/scripts/run-py.sh" "$repo/scripts/missing_venv.py" > /dev/null 2>&1; then
        fail "missing venv exits with code 1" "exit code was 0"
    else
        pass "missing venv exits with code 1"
    fi
}

# Test 5: Script with # venv: header and venv exists but empty (no deps)
test_empty_venv() {
    local repo
    repo=$(setup_fake_repo)

    # Create an empty venv (only pip/setuptools)
    local pkg_dir="$repo/packages/empty-pkg"
    local venv_dir="$pkg_dir/.venv"
    mkdir -p "$venv_dir/bin"
    mkdir -p "$venv_dir/lib/python3.12/site-packages/pip-24.0.dist-info"
    mkdir -p "$venv_dir/lib/python3.12/site-packages/setuptools-69.0.dist-info"

    cat > "$venv_dir/bin/python3" << 'PYSHIM'
#!/usr/bin/env bash
exec python3 "$@"
PYSHIM
    chmod +x "$venv_dir/bin/python3"

    cat > "$repo/scripts/empty_venv.py" << 'PYSCRIPT'
#!/usr/bin/env python3
# venv: empty-pkg
print("should not reach here")
PYSCRIPT

    local output
    output=$("$repo/scripts/run-py.sh" "$repo/scripts/empty_venv.py" 2>&1 || true)

    if echo "$output" | grep -q "no dependencies installed"; then
        pass "empty venv prints dependency error"
    else
        fail "empty venv prints dependency error" "got: $output"
    fi

    if echo "$output" | grep -q "pip install"; then
        pass "empty venv shows install instructions"
    else
        fail "empty venv shows install instructions" "got: $output"
    fi

    if "$repo/scripts/run-py.sh" "$repo/scripts/empty_venv.py" > /dev/null 2>&1; then
        fail "empty venv exits with code 1" "exit code was 0"
    else
        pass "empty venv exits with code 1"
    fi
}

# Test 6: Script with # venv: header and working venv — runs correctly
test_happy_path() {
    local repo
    repo=$(setup_fake_repo)

    cat > "$repo/scripts/happy.py" << 'PYSCRIPT'
#!/usr/bin/env python3
# venv: test-pkg
import sys
print("happy path works")
sys.exit(0)
PYSCRIPT

    local output
    output=$("$repo/scripts/run-py.sh" "$repo/scripts/happy.py" 2>/dev/null)

    if echo "$output" | grep -q "happy path works"; then
        pass "happy path runs script with venv python"
    else
        fail "happy path runs script with venv python" "got: $output"
    fi
}

# Test 7: Arguments pass-through
test_args_passthrough() {
    local repo
    repo=$(setup_fake_repo)

    cat > "$repo/scripts/echo_args.py" << 'PYSCRIPT'
#!/usr/bin/env python3
# venv: test-pkg
import sys
print(" ".join(sys.argv[1:]))
PYSCRIPT

    local output
    output=$("$repo/scripts/run-py.sh" "$repo/scripts/echo_args.py" --flag1 "value with spaces" --flag2 2>/dev/null)

    if [[ "$output" == "--flag1 value with spaces --flag2" ]]; then
        pass "arguments pass through correctly"
    else
        fail "arguments pass through correctly" "got: '$output'"
    fi
}

# Test 8: # venv: header in docstring (line 5-ish) is still found
test_venv_header_in_docstring() {
    local repo
    repo=$(setup_fake_repo)

    cat > "$repo/scripts/deep_header.py" << 'PYSCRIPT'
#!/usr/bin/env python3
"""Some script.

Does something useful.
# venv: test-pkg
"""
import sys
print("deep header found")
PYSCRIPT

    local output
    output=$("$repo/scripts/run-py.sh" "$repo/scripts/deep_header.py" 2>/dev/null)

    if echo "$output" | grep -q "deep header found"; then
        pass "venv header found within first 10 lines"
    else
        fail "venv header found within first 10 lines" "got: $output"
    fi
}

# Test 9: # venv: header past line 10 is NOT found
test_venv_header_too_deep() {
    local repo
    repo=$(setup_fake_repo)

    # Create a script where # venv: is on line 12
    {
        echo '#!/usr/bin/env python3'
        echo '"""'
        echo 'Line 3'
        echo 'Line 4'
        echo 'Line 5'
        echo 'Line 6'
        echo 'Line 7'
        echo 'Line 8'
        echo 'Line 9'
        echo 'Line 10'
        echo '# venv: test-pkg'
        echo '"""'
        echo 'import sys'
        echo 'print("too deep")'
    } > "$repo/scripts/too_deep.py"

    local stderr_output
    "$repo/scripts/run-py.sh" "$repo/scripts/too_deep.py" 2>"$repo/stderr.txt" || true
    stderr_output=$(cat "$repo/stderr.txt")

    if echo "$stderr_output" | grep -q "WARNING.*no.*venv.*header"; then
        pass "venv header past line 10 is not found"
    else
        fail "venv header past line 10 is not found" "stderr: $stderr_output"
    fi
}

# Test 10: Exit code propagation
test_exit_code_propagation() {
    local repo
    repo=$(setup_fake_repo)

    cat > "$repo/scripts/exit42.py" << 'PYSCRIPT'
#!/usr/bin/env python3
# venv: test-pkg
import sys
sys.exit(42)
PYSCRIPT

    local exit_code=0
    "$repo/scripts/run-py.sh" "$repo/scripts/exit42.py" > /dev/null 2>&1 || exit_code=$?

    if [[ $exit_code -eq 42 ]]; then
        pass "exit code propagated from Python script"
    else
        fail "exit code propagated from Python script" "expected 42, got $exit_code"
    fi
}

# Test 11: run-py.sh resolves repo root correctly from inside a worktree
test_worktree_repo_root_resolution() {
    local repo
    repo=$(make_temp_dir)

    # Set up a fake main repo: .git directory + CLAUDE.md
    mkdir -p "$repo/.git"
    touch "$repo/CLAUDE.md"

    # Create main repo package venv
    local pkg_dir="$repo/packages/test-pkg"
    local venv_dir="$pkg_dir/.venv"
    mkdir -p "$venv_dir/bin"
    mkdir -p "$venv_dir/lib/python3.12/site-packages/somelib-1.0.dist-info"
    cat > "$venv_dir/bin/python3" << 'PYSHIM'
#!/usr/bin/env bash
exec python3 "$@"
PYSHIM
    chmod +x "$venv_dir/bin/python3"

    # Set up main repo scripts
    mkdir -p "$repo/scripts"
    cp "$RUN_PY" "$repo/scripts/run-py.sh"
    chmod +x "$repo/scripts/run-py.sh"

    # Create a fake worktree nested under main repo
    local worktree="$repo/.claude/worktrees/agent-abcd1234"
    mkdir -p "$worktree/scripts"
    touch "$worktree/CLAUDE.md"
    # Worktrees have .git as a FILE, not a directory
    echo "gitdir: $repo/.git/worktrees/agent-abcd1234" > "$worktree/.git"

    # Copy run-py.sh into the worktree scripts dir (simulates symlink or copy)
    cp "$RUN_PY" "$worktree/scripts/run-py.sh"
    chmod +x "$worktree/scripts/run-py.sh"

    # Create a test script in the worktree
    cat > "$worktree/scripts/show_venv.py" << 'PYSCRIPT'
#!/usr/bin/env python3
# venv: test-pkg
import sys
print("worktree test works")
PYSCRIPT

    # Run the worktree's run-py.sh — it should resolve REPO_ROOT to $repo
    # (the main repo), not to $worktree. The venv is at
    # $repo/packages/test-pkg/.venv, not $worktree/packages/test-pkg/.venv.
    local output
    local exit_code=0
    output=$("$worktree/scripts/run-py.sh" "$worktree/scripts/show_venv.py" 2>/dev/null) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]] && echo "$output" | grep -q "worktree test works"; then
        pass "run-py.sh from worktree resolves to main repo root for venv lookup"
    else
        fail "run-py.sh from worktree resolves to main repo root for venv lookup" \
            "exit code: $exit_code, output: $output"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_no_args
test_nonexistent_script
test_no_venv_header
test_missing_venv
test_empty_venv
test_happy_path
test_args_passthrough
test_venv_header_in_docstring
test_venv_header_too_deep
test_exit_code_propagation
test_worktree_repo_root_resolution

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
