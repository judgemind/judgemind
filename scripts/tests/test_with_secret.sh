#!/usr/bin/env bash
# test_with_secret.sh — Tests for scripts/with-secret.sh terse-default behavior
#
# Verifies:
#   a. --verbose / JM_VERBOSE=1 flag accepted
#   b. Success path is silent by default
#   c. Failure paths stay loud (Error: in stderr)
#
# Usage:
#   scripts/tests/test_with_secret.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/with-secret.sh"
FAILURES=0
TESTS=0

TEMP_DIRS=()
ORIG_PATH_SAVE=""

cleanup() {
    set +eu
    for d in "${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
    fi
}
trap cleanup EXIT

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

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    echo "$dir"
}

# Mock aws that returns a secret value.
setup_mock_aws_success() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/aws" << 'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "secretsmanager" && "${2:-}" == "get-secret-value" ]]; then
    echo "mysecretvalue"
    exit 0
fi
exit 0
MOCK
    chmod +x "$tmpdir/bin/aws"
    echo "$tmpdir"
}

# Mock aws that fails to fetch the secret.
setup_mock_aws_fail() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/aws" << 'MOCK'
#!/usr/bin/env bash
exit 1
MOCK
    chmod +x "$tmpdir/bin/aws"
    echo "$tmpdir"
}

# ── Precondition: script exists and is executable ─────────────────────────

if [[ ! -x "$SCRIPT" ]]; then
    echo "FAIL: $SCRIPT is not executable (or does not exist)" >&2
    exit 1
fi

ORIG_PATH_SAVE="$PATH"

# ── Test 1: Usage error — no -e spec ─────────────────────────────────────

exit_code=0
stderr_output=$("$SCRIPT" -- echo hello 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero when no -e spec"
else
    fail "exits non-zero when no -e spec" "expected non-zero, got 0"
fi

if echo "$stderr_output" | grep -qi "error"; then
    pass "no -e spec error contains Error:"
else
    fail "no -e spec error contains Error:" "got: $stderr_output"
fi

# ── Test 2: Default-mode success is silent (no extra output) ─────────────

tmpdir=$(setup_mock_aws_success)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

# The command to exec is "true" so it succeeds silently
all_output=$("$SCRIPT" -e MYVAR=judgemind/secret -- true 2>&1) || true
line_count=$(echo "$all_output" | grep -c . || true)

if [[ "$line_count" -eq 0 ]]; then
    pass "default-mode success is silent (0 lines)"
else
    fail "default-mode success is silent" "got $line_count lines: $all_output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 3: --verbose shows Resolved lines ───────────────────────────────

tmpdir=$(setup_mock_aws_success)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

verbose_output=$("$SCRIPT" --verbose -e MYVAR=judgemind/secret -- true 2>&1) || true

if echo "$verbose_output" | grep -qi "resolved\|MYVAR"; then
    pass "--verbose shows Resolved line"
else
    fail "--verbose shows Resolved line" "got: $verbose_output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 4: JM_VERBOSE=1 shows Resolved lines ────────────────────────────

tmpdir=$(setup_mock_aws_success)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

env_verbose_output=$(JM_VERBOSE=1 "$SCRIPT" -e MYVAR=judgemind/secret -- true 2>&1) || true

if echo "$env_verbose_output" | grep -qi "resolved\|MYVAR"; then
    pass "JM_VERBOSE=1 shows Resolved line"
else
    fail "JM_VERBOSE=1 shows Resolved line" "got: $env_verbose_output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 5: Failure path stderr contains Error: ──────────────────────────

tmpdir=$(setup_mock_aws_fail)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
stderr_fail=$("$SCRIPT" -e MYVAR=judgemind/secret -- true 2>&1 >/dev/null) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero on aws failure"
else
    fail "exits non-zero on aws failure" "expected non-zero, got 0"
fi

if echo "$stderr_fail" | grep -qi "error"; then
    pass "failure path stderr contains Error:"
else
    fail "failure path stderr contains Error:" "got: $stderr_fail"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
