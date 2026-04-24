#!/usr/bin/env bash
# test_check_no_git_repo_root.sh — Tests for scripts/check-no-git-repo-root.py
#
# Verifies that the AST-based guard fails on synthetic daemon.py fixtures
# that contain ``git`` subprocess calls anchored to ``self._repo_root()``,
# and passes on the correct ``self._git_parent_root()`` form, non-git
# usages of ``_repo_root()``, docstrings, and the real daemon.py.
#
# Usage:
#   scripts/tests/test_check_no_git_repo_root.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-git-repo-root.py"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

write_file() {
    # $1: file path (relative to TMPDIR_TEST)
    # $2: contents (via stdin)
    local path="$TMPDIR_TEST/$1"
    mkdir -p "$(dirname "$path")"
    cat > "$path"
}

assert_passes() {
    local desc="$1"
    local target="${2:-}"
    TESTS=$((TESTS + 1))
    local cmd_ok=true
    if [[ -n "$target" ]]; then
        "$CHECK_SCRIPT" "$target" > /dev/null 2>&1 || cmd_ok=false
    else
        # Called with no target (e.g. from _guard_self_match_helpers.sh) —
        # run the checker against its default target (daemon.py).
        "$CHECK_SCRIPT" > /dev/null 2>&1 || cmd_ok=false
    fi
    if $cmd_ok; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
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

# ─── Test 1: FAIL — single-line git call with str(self._repo_root()) ────────
write_file "bad_single_line.py" <<'EOF'
import subprocess

class Daemon:
    def do_git(self):
        subprocess.run(["git", "-C", str(self._repo_root()), "worktree", "add", "/tmp/x"], check=False)
EOF
assert_fails "FAIL: single-line git argv with str(self._repo_root())" \
    "$TMPDIR_TEST/bad_single_line.py"

# ─── Test 2: FAIL — multi-line argv list (the actual #2804/#2821 shape) ─────
write_file "bad_multiline.py" <<'EOF'
import subprocess

class Daemon:
    def create_worktree(self):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self._repo_root()),
                "worktree",
                "add",
                "/tmp/worktrees/agent-abc",
                "-b",
                "agent/abc",
                "origin/main",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
EOF
assert_fails "FAIL: multi-line git argv with str(self._repo_root()) (the #2804/#2821 shape)" \
    "$TMPDIR_TEST/bad_multiline.py"

# ─── Test 3: FAIL — self._repo_root() without str() wrapping ────────────────
write_file "bad_no_str.py" <<'EOF'
import subprocess

class Daemon:
    def do_git(self):
        subprocess.run(
            [
                "git",
                "-C",
                self._repo_root(),
                "fetch",
                "origin",
                "main",
            ],
            check=False,
        )
EOF
assert_fails "FAIL: git argv with bare self._repo_root() (no str() wrap)" \
    "$TMPDIR_TEST/bad_no_str.py"

# ─── Test 4: PASS — correct form uses self._git_parent_root() ───────────────
write_file "good_git_parent.py" <<'EOF'
import subprocess

class Daemon:
    def create_worktree(self):
        git_parent = self._git_parent_root()
        subprocess.run(
            [
                "git",
                "-C",
                str(git_parent),
                "worktree",
                "add",
                "/tmp/worktrees/agent-abc",
                "-b",
                "agent/abc",
                "origin/main",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
EOF
assert_passes "PASS: git argv with self._git_parent_root() (the correct form)" \
    "$TMPDIR_TEST/good_git_parent.py"

# ─── Test 5: PASS — self._repo_root() used only for cleanup_script path ─────
# (Mirrors daemon.py line 15497: ``repo_root = self._repo_root()`` followed by
# ``cleanup_script = repo_root / "scripts" / "cleanup_worktree.sh"``.)
write_file "good_non_git_path.py" <<'EOF'
import subprocess
from pathlib import Path

class Daemon:
    def drop_worktree(self, worktree_path):
        repo_root = self._repo_root()
        cleanup_script = repo_root / "scripts" / "cleanup_worktree.sh"
        if cleanup_script.exists():
            subprocess.run(
                [str(cleanup_script), worktree_path],
                check=False,
            )
EOF
assert_passes "PASS: self._repo_root() used only for non-git script path" \
    "$TMPDIR_TEST/good_non_git_path.py"

# ─── Test 6: PASS — self._repo_root() used in tmp path (non-git usage) ──────
# (Mirrors daemon.py line 18185 pattern: tmp_dir = self._repo_root() / "tmp" / ...)
write_file "good_tmp_path.py" <<'EOF'
import subprocess
from pathlib import Path

class Daemon:
    def write_tmp(self):
        tmp_dir = self._repo_root() / "tmp" / "worker-done.txt"
        tmp_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.write_text("done")
EOF
assert_passes "PASS: self._repo_root() used in tmp path, no git subprocess" \
    "$TMPDIR_TEST/good_tmp_path.py"

# ─── Test 7: PASS — docstring mentioning self._repo_root() and git ───────────
# (Mirrors daemon.py docstrings at lines 4432, 4453, 15462.)
write_file "good_docstring.py" <<'EOF'
class Daemon:
    def _repo_root(self):
        """Legacy repo-shaped directory.

        Not the git parent for worktree ops. Phase 3A (#2783) originally
        had ``_create_worktree`` run ``git -C <cwd> worktree add ...``, but
        the container CWD has no ``.git`` child. Use self._git_parent_root()
        for any git subprocess. self._repo_root() is only for non-git paths.
        """
        import os
        from pathlib import Path
        return Path(os.getcwd())
EOF
assert_passes "PASS: docstring mentioning self._repo_root() and git as prose" \
    "$TMPDIR_TEST/good_docstring.py"

# ─── Test 8: PASS — return self._repo_root() (not a subprocess call) ─────────
# (Mirrors daemon.py line 4470: ``return self._repo_root()``.)
write_file "good_return.py" <<'EOF'
class Daemon:
    def _git_parent_root(self):
        if self._cfg.baseline_repo_root is not None:
            return self._cfg.baseline_repo_root
        return self._repo_root()
EOF
assert_passes "PASS: return self._repo_root() is not a git subprocess" \
    "$TMPDIR_TEST/good_return.py"

# ─── Test 9: PASS — gh subprocess using self._repo_root() (not git) ─────────
write_file "good_gh_subprocess.py" <<'EOF'
import subprocess

class Daemon:
    def run_gh(self):
        subprocess.run(
            [
                "gh",
                "--repo",
                str(self._repo_root()),
                "pr",
                "list",
            ],
            check=False,
        )
EOF
assert_passes "PASS: gh subprocess using self._repo_root() is out of scope" \
    "$TMPDIR_TEST/good_gh_subprocess.py"

# ─── Test 10: PASS — empty file ──────────────────────────────────────────────
write_file "empty.py" <<'EOF'
EOF
assert_passes "PASS: empty file has nothing to check" \
    "$TMPDIR_TEST/empty.py"

# ─── Test 11: PASS — file without target class (no Daemon) ───────────────────
write_file "no_class.py" <<'EOF'
import subprocess

def standalone_git():
    subprocess.run(["git", "status"], check=False)
EOF
assert_passes "PASS: file without Daemon class, no self._repo_root() — passes" \
    "$TMPDIR_TEST/no_class.py"

# ─── Test 12: PASS — missing target file exits 0 (nothing to check) ─────────
assert_passes "PASS: missing target file exits 0 (nothing to check)" \
    "$TMPDIR_TEST/does_not_exist.py"

# ─── Test 13: Regression — the real daemon.py passes ────────────────────────
# This is the critical regression guard: the current (post-#2821)
# scripts/dispatcher/daemon.py must pass the check — proving it has no
# existing violations and that the check doesn't false-positive on real code.
DAEMON_PATH="$REPO_ROOT/scripts/dispatcher/daemon.py"
if [[ -f "$DAEMON_PATH" ]]; then
    assert_passes "Regression: real scripts/dispatcher/daemon.py passes (no violations)" \
        "$DAEMON_PATH"
else
    echo "SKIP: $DAEMON_PATH not present — skipping regression test"
fi

# ─── Test 14: No self-match on ci.yml step name ──────────────────────────────
# Ensures the checker does not accidentally flag its own CI step name.
# See scripts/tests/_guard_self_match_helpers.sh and issue #2542.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-git-repo-root.py" "yml"

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
