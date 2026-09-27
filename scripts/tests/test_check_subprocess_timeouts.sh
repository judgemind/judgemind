#!/usr/bin/env bash
# test_check_subprocess_timeouts.sh — Tests for
# scripts/check-subprocess-timeouts.sh
#
# Verifies that the checker:
#   (a) passes on a file where subprocess.run has an explicit timeout= kwarg
#   (b) fails on a file where subprocess.run has no timeout= kwarg
#   (c) passes on a file where subprocess.run uses a **kwargs splat
#   (d) ignores subprocess.Popen calls (out of scope)
#   (e) passes on the real scripts/dispatcher/daemon.py
#   (f) passes on the whole scripts/ tree after the #3213 fix
#
# Usage:
#   scripts/tests/test_check_subprocess_timeouts.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-subprocess-timeouts.sh"
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

# ─── Test (a): file with timeout= passes ─────────────────────────────────────
write_file "good_timeout.py" <<'EOF'
import subprocess

def run_git():
    result = subprocess.run(
        ["git", "status"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode
EOF
assert_passes "file with explicit timeout= kwarg passes" \
    "$TMPDIR_TEST/good_timeout.py"

# ─── Test (b): file without timeout= fails ───────────────────────────────────
write_file "bad_no_timeout.py" <<'EOF'
import subprocess

def run_git():
    result = subprocess.run(
        ["git", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode
EOF
assert_fails "file with subprocess.run missing timeout= fails" \
    "$TMPDIR_TEST/bad_no_timeout.py"

# ─── Test (c): file with **kwargs splat passes ────────────────────────────────
# The checker treats **kwargs as a pass-through — the wrapper is expected
# to carry a timeout= kwarg from the call site above.
write_file "good_kwargs_splat.py" <<'EOF'
import subprocess
from typing import Any

def _run_helper(cmd: list, **kwargs: Any):
    return subprocess.run(cmd, **kwargs)
EOF
assert_passes "file with **kwargs splat passes (timeout delegated to caller)" \
    "$TMPDIR_TEST/good_kwargs_splat.py"

# ─── Test (d): subprocess.Popen is ignored ───────────────────────────────────
# Popen + proc.wait(timeout=...) is the out-of-scope pattern per #3213
# plan. The checker must NOT flag Popen calls — they are not subprocess.run.
write_file "popen_ignored.py" <<'EOF'
import subprocess

def spawn_child():
    proc = subprocess.Popen(
        ["long-running-process"],
        stdout=subprocess.PIPE,
    )
    proc.wait(timeout=60)
    return proc.returncode
EOF
assert_passes "subprocess.Popen calls are ignored (out of scope per #3213)" \
    "$TMPDIR_TEST/popen_ignored.py"

# ─── Test (e): real scripts/dispatcher/daemon.py passes ──────────────────────
# Regression gate: daemon.py has always had timeout= on every subprocess.run.
# If a future author adds a new unbounded subprocess.run, this test catches it.
assert_passes "real scripts/dispatcher/daemon.py passes" \
    "$REPO_ROOT/scripts/dispatcher/daemon.py"

# ─── Test (f): whole scripts/ tree post-#3213 fix passes ─────────────────────
# The full-tree scan (no argument) must exit 0 after the 13 violation
# sites are fixed in #3213.
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: whole scripts/ tree passes after #3213 fix"
else
    echo "FAIL: whole scripts/ tree has remaining violations (expected all fixed by #3213)"
    "$CHECK_SCRIPT" 2>&1 | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi

# ─── Test (g): urllib.request.urlopen with timeout= passes ───────────────────
write_file "good_urlopen_timeout.py" <<'EOF'
import urllib.request

def fetch():
    req = urllib.request.Request("https://api.example.com/data")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()
EOF
assert_passes "file with urllib.request.urlopen and explicit timeout= passes" \
    "$TMPDIR_TEST/good_urlopen_timeout.py"

# ─── Test (h): urllib.request.urlopen without timeout= fails ─────────────────
write_file "bad_urlopen_no_timeout.py" <<'EOF'
import urllib.request

def fetch():
    req = urllib.request.Request("https://api.example.com/data")
    with urllib.request.urlopen(req) as resp:
        return resp.read()
EOF
assert_fails "file with urllib.request.urlopen missing timeout= fails" \
    "$TMPDIR_TEST/bad_urlopen_no_timeout.py"

# ─── Test (i): from-import urlopen with timeout= passes ──────────────────────
write_file "good_urlopen_from_import_timeout.py" <<'EOF'
from urllib.request import urlopen, Request

def fetch():
    req = Request("https://api.example.com/data")
    with urlopen(req, timeout=30) as resp:
        return resp.read()
EOF
assert_passes "file with from-import urlopen and explicit timeout= passes" \
    "$TMPDIR_TEST/good_urlopen_from_import_timeout.py"

# ─── Test (j): from-import urlopen without timeout= fails ────────────────────
write_file "bad_urlopen_from_import_no_timeout.py" <<'EOF'
from urllib.request import urlopen, Request

def fetch():
    req = Request("https://api.example.com/data")
    with urlopen(req) as resp:
        return resp.read()
EOF
assert_fails "file with from-import urlopen missing timeout= fails" \
    "$TMPDIR_TEST/bad_urlopen_from_import_no_timeout.py"

# ─── Test (k): full-tree mode flags seeded violations across files (#4720) ───
# The no-argument mode scans every scripts/**/*.py in one python3 process.
# Seed a fake repo: two violating files, one clean file, a violation under
# tests/ (excluded), and one file that does not parse (skipped). The scan
# must report exactly the two violations, in sorted path order.
FAKE_REPO="$TMPDIR_TEST/fake_repo"
mkdir -p "$FAKE_REPO/scripts/tests" "$FAKE_REPO/scripts/sub"
cp "$CHECK_SCRIPT" "$FAKE_REPO/scripts/check-subprocess-timeouts.sh"
cat > "$FAKE_REPO/scripts/a_bad.py" <<'EOF'
import subprocess
subprocess.run(["true"])
EOF
cat > "$FAKE_REPO/scripts/b_good.py" <<'EOF'
import subprocess
subprocess.run(["true"], timeout=5)
EOF
cat > "$FAKE_REPO/scripts/sub/c_bad.py" <<'EOF'
from urllib.request import urlopen
urlopen("https://example.com")
EOF
cat > "$FAKE_REPO/scripts/tests/test_ignored.py" <<'EOF'
import subprocess
subprocess.run(["true"])
EOF
cat > "$FAKE_REPO/scripts/d_syntax_error.py" <<'EOF'
def broken(:
EOF
TESTS=$((TESTS + 1))
k_rc=0
k_out="$("$FAKE_REPO/scripts/check-subprocess-timeouts.sh" 2>&1)" || k_rc=$?
k_expected_a="    $FAKE_REPO/scripts/a_bad.py:2: subprocess.run([\"true\"])"
k_expected_c="    $FAKE_REPO/scripts/sub/c_bad.py:2: urlopen(\"https://example.com\")"
k_violation_count="$(printf '%s\n' "$k_out" | grep -c -E '^    /.*\.py:[0-9]+: ' || true)"
if [[ "$k_rc" -eq 1 ]] \
    && printf '%s\n' "$k_out" | grep -qxF "$k_expected_a" \
    && printf '%s\n' "$k_out" | grep -qxF "$k_expected_c" \
    && [[ "$k_violation_count" -eq 2 ]]; then
    echo "PASS: full-tree scan flags exactly the seeded violations (tests/ excluded, syntax errors skipped)"
else
    echo "FAIL: full-tree scan (rc=$k_rc, violations=$k_violation_count)"
    printf '%s\n' "$k_out" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "$TESTS tests run, $FAILURES failed"
if (( FAILURES > 0 )); then
    exit 1
fi
exit 0
