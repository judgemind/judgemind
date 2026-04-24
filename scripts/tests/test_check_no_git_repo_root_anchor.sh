#!/usr/bin/env bash
# test_check_no_git_repo_root_anchor.sh — Tests for
# scripts/check-no-git-repo-root-anchor.sh
#
# Verifies that the checker:
#   - fails when a subprocess.run git list literal contains self._repo_root()
#     inline or via a name-bound cmd variable
#   - passes when the anchor is self._git_parent_root() (inline or
#     name-bound)
#   - passes on docstring prose that mentions _repo_root() (not real code)
#   - passes on non-git subprocesses that legitimately use _repo_root()
#   - passes on the real scripts/dispatcher/daemon.py
#
# Usage:
#   scripts/tests/test_check_no_git_repo_root_anchor.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-git-repo-root-anchor.sh"
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

# assert_passes <desc> [target]
# When target is omitted the default is used for the self-match helper.
assert_passes() {
    local desc="$1"
    local target="${2:-$TMPDIR_TEST/ci_step_names.yml}"
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

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: inline git list with self._repo_root() → FAIL ───────────────
write_file "t1_inline_bad.py" <<'EOF'
import subprocess

class Daemon:
    def _repo_root(self):
        return "/app"
    def do_git(self):
        subprocess.run(
            ["git", "-C", str(self._repo_root()), "status"],
            check=True,
        )
EOF
assert_fails "inline git list with str(self._repo_root()) is detected" \
    "$TMPDIR_TEST/t1_inline_bad.py"

# ─── Test 2: name-bound cmd list with self._repo_root() → FAIL ───────────
write_file "t2_namebound_bad.py" <<'EOF'
import subprocess

class Daemon:
    def _repo_root(self):
        return "/app"
    def do_git(self):
        cmd = ["git", "-C", str(self._repo_root()), "worktree", "add", "path"]
        subprocess.run(cmd, check=True)
EOF
assert_fails "name-bound cmd list with str(self._repo_root()) is detected" \
    "$TMPDIR_TEST/t2_namebound_bad.py"

# ─── Test 3: git list with self._git_parent_root() inline → PASS ─────────
write_file "t3_good_inline.py" <<'EOF'
import subprocess

class Daemon:
    def _git_parent_root(self):
        return "/baseline"
    def do_git(self):
        subprocess.run(
            ["git", "-C", str(self._git_parent_root()), "status"],
            check=True,
        )
EOF
assert_passes "inline git list with str(self._git_parent_root()) is allowed" \
    "$TMPDIR_TEST/t3_good_inline.py"

# ─── Test 4: name-bound cmd from self._git_parent_root() → PASS ──────────
write_file "t4_good_namebound.py" <<'EOF'
import subprocess

class Daemon:
    def _git_parent_root(self):
        return "/baseline"
    def do_git(self):
        git_parent = self._git_parent_root()
        cmd = ["git", "-C", str(git_parent), "worktree", "add", "path"]
        subprocess.run(cmd, check=True)
EOF
assert_passes "name-bound cmd anchored at _git_parent_root() is allowed" \
    "$TMPDIR_TEST/t4_good_namebound.py"

# ─── Test 5: docstring prose mentioning self._repo_root() → PASS ─────────
# Docstrings are string literals, not code; the AST walker does not visit
# their textual content as a Call node.  Only executable list literals
# inside subprocess.run calls are flagged.
write_file "t5_docstring.py" <<'EOF'
import subprocess

class Daemon:
    def do_git(self):
        """Run git -C self._repo_root() status.

        Note: legacy callers used self._repo_root() here; production now
        requires self._git_parent_root().
        """
        pass
EOF
assert_passes "docstring prose mentioning self._repo_root() is ignored" \
    "$TMPDIR_TEST/t5_docstring.py"

# ─── Test 6: non-git subprocess using _repo_root() → PASS ────────────────
# Cleanup-script lookups and tmp-dir paths legitimately use _repo_root().
# The check only fires on list literals whose first element is "git".
write_file "t6_nongit_subprocess.py" <<'EOF'
import subprocess

class Daemon:
    def _repo_root(self):
        return "/app"
    def run_cleanup(self, cleanup_script):
        subprocess.run(
            [str(cleanup_script), str(self._repo_root())],
            check=True,
        )
EOF
assert_passes "non-git subprocess using _repo_root() is allowed" \
    "$TMPDIR_TEST/t6_nongit_subprocess.py"

# ─── Test 7: real scripts/dispatcher/daemon.py → PASS ────────────────────
REAL_DAEMON="$REPO_ROOT/scripts/dispatcher/daemon.py"
if [[ -f "$REAL_DAEMON" ]]; then
    assert_passes "real scripts/dispatcher/daemon.py has no _repo_root() git anchors" \
        "$REAL_DAEMON"
fi

# ─── Self-match assertion ─────────────────────────────────────────────────
# The ci.yml step name that runs this guard must not itself contain the
# forbidden Python pattern.  This assertion catches the class of failure
# from #2541 / #2542.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-git-repo-root-anchor.sh" "yml"

# ─── Summary ─────────────────────────────────────────────────────────────
echo ""
if (( FAILURES > 0 )); then
    echo "$FAILURES of $TESTS tests FAILED"
    exit 1
fi
echo "all $TESTS tests passed"
exit 0
