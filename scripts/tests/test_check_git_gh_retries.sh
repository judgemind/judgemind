#!/usr/bin/env bash
# test_check_git_gh_retries.sh — Tests for
# scripts/check-git-gh-retries.sh
#
# Verifies that the checker:
#   - passes on synthetic daemon.py contents with well-annotated git/gh
#     subprocess.run sites
#   - fails on unannotated ``subprocess.run(["git"|"gh", ...])`` sites
#   - correctly resolves the ``cmd = [...]; subprocess.run(cmd, ...)``
#     name-binding pattern
#   - tolerates non-git/gh subprocess.run calls (python, ruff, etc.)
#   - passes on the real ``scripts/dispatcher/daemon.py``.
#
# Usage:
#   scripts/tests/test_check_git_gh_retries.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-git-gh-retries.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

write_file() {
    local path="$TMPDIR_TEST/$1"
    mkdir -p "$(dirname "$path")"
    cat > "$path"
}

assert_passes() {
    local desc="$1"
    local target="$2"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$target" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        "$CHECK_SCRIPT" "$target" 2>&1 | sed 's/^/    /'
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    local target="$2"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$target" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# ─── Test 1: direct list literal with cold-path comment passes ────────────
write_file "good_direct.py" <<'EOF'
import subprocess

def handler():
    # cold-path (#3089): local fs op, no network.
    subprocess.run(
        ["git", "-C", "/tmp", "rev-parse", "HEAD"],
        capture_output=True,
    )
EOF
assert_passes "direct list literal with nearby cold-path annotation" \
    "$TMPDIR_TEST/good_direct.py"

# ─── Test 2: direct list literal WITHOUT annotation fails ─────────────────
write_file "bad_direct.py" <<'EOF'
import subprocess

def handler():
    subprocess.run(
        ["git", "-C", "/tmp", "rev-parse", "HEAD"],
        capture_output=True,
    )
EOF
assert_fails "direct list literal without cold-path annotation" \
    "$TMPDIR_TEST/bad_direct.py"

# ─── Test 3: name-bound cmd with cold-path annotation passes ──────────────
write_file "good_cmd_name.py" <<'EOF'
import subprocess

def handler():
    # cold-path (#3089): local op.
    cmd = ["gh", "auth", "setup-git"]
    subprocess.run(cmd, capture_output=True)
EOF
assert_passes "name-bound cmd with cold-path annotation" \
    "$TMPDIR_TEST/good_cmd_name.py"

# ─── Test 4: name-bound cmd WITHOUT annotation fails ──────────────────────
write_file "bad_cmd_name.py" <<'EOF'
import subprocess

def handler():
    cmd = ["gh", "auth", "setup-git"]
    subprocess.run(cmd, capture_output=True)
EOF
assert_fails "name-bound cmd without cold-path annotation" \
    "$TMPDIR_TEST/bad_cmd_name.py"

# ─── Test 5: non-git/gh subprocess.run is ignored ─────────────────────────
write_file "other_subprocess.py" <<'EOF'
import subprocess

def handler():
    subprocess.run(["python", "-c", "print(1)"], capture_output=True)
    subprocess.run(["ruff", "check", "."], capture_output=True)
EOF
assert_passes "non-git/gh subprocess.run is ignored" \
    "$TMPDIR_TEST/other_subprocess.py"

# ─── Test 6: function-parameter cmd is NOT flagged (retry helper shape) ──
# The shared ``_subprocess_with_retry`` helper takes ``cmd`` as a
# function parameter. The checker must NOT flag the internal
# ``subprocess.run(cmd, ...)`` as missing an annotation, because cmd
# is unresolvable (it's a parameter, not an in-scope Assign).
write_file "helper_param.py" <<'EOF'
import subprocess

class D:
    def _subprocess_with_retry(self, cmd, timeout):
        # No cold-path annotation needed — cmd is a function parameter,
        # so the checker cannot resolve its shape. The helper itself
        # is the retry wrapper.
        subprocess.run(cmd, timeout=timeout, capture_output=True)
EOF
assert_passes "function-parameter cmd is not flagged" \
    "$TMPDIR_TEST/helper_param.py"

# ─── Test 7: annotation outside the window fails ──────────────────────────
write_file "too_far.py" <<'EOF'
import subprocess

# cold-path (#3089): this annotation is too far from the call below.
def handler():
    # filler line 1
    pass

def other():
    pass

# filler
EOF
# Append 70 lines of filler to push the annotation well beyond the 60-line window.
python3 -c "
p = '$TMPDIR_TEST/too_far.py'
with open(p, 'a') as f:
    for i in range(70):
        f.write(f'# filler line {i}\n')
    f.write('''
def later_handler():
    subprocess.run([\"gh\", \"issue\", \"view\", \"1\"])
''')
"
assert_fails "annotation outside the 60-line window fails" \
    "$TMPDIR_TEST/too_far.py"

# ─── Test 8: annotation with wrong issue number fails ─────────────────────
# Only ``# cold-path (#3089)`` is accepted — a generic cold-path
# comment without the issue reference should not satisfy the check.
write_file "wrong_annotation.py" <<'EOF'
import subprocess

def handler():
    # cold-path: this is a local op (no issue reference).
    subprocess.run(["git", "status"], capture_output=True)
EOF
assert_fails "annotation missing issue reference fails" \
    "$TMPDIR_TEST/wrong_annotation.py"

# ─── Test 9: the real daemon.py passes ────────────────────────────────────
# Regression gate: after #3089 lands, the real daemon.py must satisfy
# the checker indefinitely. If a future author adds a new git/gh
# subprocess.run, they must either route through the retry helper or
# add a ``# cold-path (#3089)`` comment.
assert_passes "real scripts/dispatcher/daemon.py passes" \
    "$REPO_ROOT/scripts/dispatcher/daemon.py"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "$TESTS tests run, $FAILURES failed"
if (( FAILURES > 0 )); then
    exit 1
fi
exit 0
