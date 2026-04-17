#!/usr/bin/env bash
# test_check_test_except_pass.sh — Tests for check-test-except-pass.py
#
# Creates temporary Python files that live inside a fake ``tests/`` tree
# and verifies the checker detects the forbidden
# `try/except Exception: pass` pattern while allowing legitimate code.
#
# Usage:
#   scripts/tests/test_check_test_except_pass.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-test-except-pass.py"
FAILURES=0
TESTS=0

# All fixture files land inside a ``tests/`` subdirectory so the checker
# picks them up.
TMPDIR_TEST=$(mktemp -d)
TESTS_DIR="$TMPDIR_TEST/tests"
mkdir -p "$TESTS_DIR"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local path="$TESTS_DIR/$name"
    printf '%s\n' "$content" > "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if python3 "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if python3 "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TESTS_DIR"/*
}

# ─── Test 1: try/except Exception: pass inside tests/ fails ──────────────
create_test_file "test_bad_exception.py" 'def test_thing():
    try:
        do_it()
    except Exception:
        pass'
assert_fails "except Exception: pass detected"
reset_tmpdir

# ─── Test 2: try/except BaseException: pass inside tests/ fails ─────────
create_test_file "test_bad_baseexception.py" 'def test_thing():
    try:
        do_it()
    except BaseException:
        pass'
assert_fails "except BaseException: pass detected"
reset_tmpdir

# ─── Test 3: bare try/except: pass inside tests/ fails ──────────────────
create_test_file "test_bad_bare.py" 'def test_thing():
    try:
        do_it()
    except:
        pass'
assert_fails "bare except: pass detected"
reset_tmpdir

# ─── Test 4: narrowed handler is fine ───────────────────────────────────
create_test_file "test_narrow.py" 'def test_thing():
    try:
        do_it()
    except ValueError:
        pass'
assert_passes "except ValueError: pass is allowed (narrow)"
reset_tmpdir

# ─── Test 5: handler that does real work is fine ────────────────────────
create_test_file "test_real_work.py" 'def test_thing():
    try:
        do_it()
    except Exception as e:
        assert str(e) == "expected"'
assert_passes "except Exception with assertion body is allowed"
reset_tmpdir

# ─── Test 6: handler that re-raises is fine ─────────────────────────────
create_test_file "test_reraise.py" 'def test_thing():
    try:
        do_it()
    except Exception:
        raise'
assert_passes "except Exception: raise is allowed"
reset_tmpdir

# ─── Test 7: # noqa: BLE001 escape hatch opts out of the check ──────────
create_test_file "test_noqa.py" 'def test_cleanup():
    try:
        do_it()
    except Exception:  # noqa: BLE001
        pass'
assert_passes "# noqa: BLE001 escape hatch is honoured"
reset_tmpdir

# ─── Test 8: # noqa without BLE001 code is NOT a valid escape hatch ─────
create_test_file "test_noqa_wrong.py" 'def test_thing():
    try:
        do_it()
    except Exception:  # noqa: F401
        pass'
assert_fails "# noqa without BLE001 is not an escape hatch"
reset_tmpdir

# ─── Test 9: violation nested inside a with/for still detected ──────────
create_test_file "test_nested.py" 'def test_thing():
    for item in [1, 2, 3]:
        try:
            do_it(item)
        except Exception:
            pass'
assert_fails "nested try/except Exception: pass is detected"
reset_tmpdir

# ─── Test 10: non-test files outside tests/ are ignored ─────────────────
# Create a file with the forbidden pattern in a non-test location.
BAD_SRC="$TMPDIR_TEST/not_tests/bad_src.py"
mkdir -p "$TMPDIR_TEST/not_tests"
printf '%s\n' 'def f():
    try:
        g()
    except Exception:
        pass' > "$BAD_SRC"
assert_passes "files outside tests/ are ignored"
rm -rf "$TMPDIR_TEST/not_tests"
reset_tmpdir

# ─── Test 11: handler tuple including Exception is blind ────────────────
create_test_file "test_tuple.py" 'def test_thing():
    try:
        do_it()
    except (Exception, OSError):
        pass'
assert_fails "except (Exception, OSError): pass detected via tuple"
reset_tmpdir

# ─── Test 12: handler tuple of narrow exceptions is allowed ─────────────
create_test_file "test_tuple_narrow.py" 'def test_thing():
    try:
        do_it()
    except (ValueError, KeyError):
        pass'
assert_passes "except (ValueError, KeyError): pass is allowed"
reset_tmpdir

# ─── Test 13: empty tests dir passes ────────────────────────────────────
assert_passes "empty tests directory passes"

# ─── Test 14: syntax-error file is skipped (pass, not a hard failure) ───
create_test_file "test_broken.py" 'def test_broken(
    this is not valid python'
assert_passes "syntax error file is skipped"
reset_tmpdir

# ─── Test 15: handler that calls log function is allowed ────────────────
create_test_file "test_log.py" 'def test_thing():
    try:
        do_it()
    except Exception:
        log.info("failed")'
assert_passes "except Exception: log.info(...) is allowed"
reset_tmpdir

# ─── Test 16: except Exception as e: pass is still blind ────────────────
create_test_file "test_as_e.py" 'def test_thing():
    try:
        do_it()
    except Exception as e:
        pass'
assert_fails "except Exception as e: pass detected"
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
