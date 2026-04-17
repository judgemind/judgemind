#!/usr/bin/env bash
# test_check_llm_json_loads.sh — Tests for check-llm-json-loads.sh
#
# Creates temporary files to verify that the checker correctly detects
# raw json.loads calls on LLM response text while allowing legitimate
# non-LLM json.loads calls.
#
# Usage:
#   scripts/tests/test_check_llm_json_loads.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-llm-json-loads.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$name"
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: json.loads(response.text) should fail ────────────────────
create_test_file "bad1.py" 'data = json.loads(response.text)' > /dev/null
assert_fails "json.loads(response.text) is detected"
reset_tmpdir

# ─── Test 2: json.loads(raw_text) should fail ─────────────────────────
create_test_file "bad2.py" 'parsed = json.loads(raw_text)' > /dev/null
assert_fails "json.loads(raw_text) is detected"
reset_tmpdir

# ─── Test 3: json.loads(response.content[0].text) should fail ─────────
create_test_file "bad3.py" 'parsed = json.loads(response.content[0].text)' > /dev/null
assert_fails "json.loads(response.content[0].text) is detected"
reset_tmpdir

# ─── Test 4: json.loads(response.text.strip()) should fail ────────────
create_test_file "bad4.py" 'parsed = json.loads(response.text.strip())' > /dev/null
assert_fails "json.loads(response.text.strip()) is detected"
reset_tmpdir

# ─── Test 5: json.loads(raw_text, strict=False) should PASS ───────────
# The fix pattern — opts into relaxed parsing explicitly.
create_test_file "good1.py" 'parsed = json.loads(raw_text, strict=False)' > /dev/null
assert_passes "json.loads(raw_text, strict=False) is allowed"
reset_tmpdir

# ─── Test 6: json.loads(response.text, strict=False) should PASS ──────
create_test_file "good2.py" 'parsed = json.loads(response.text, strict=False)' > /dev/null
assert_passes "json.loads(response.text, strict=False) is allowed"
reset_tmpdir

# ─── Test 7: parse_llm_json(response.text) should PASS ────────────────
# The centralized helper — does not match json.loads at all.
create_test_file "good3.py" 'parsed = parse_llm_json(response.text)' > /dev/null
assert_passes "parse_llm_json(response.text) is allowed"
reset_tmpdir

# ─── Test 8: json.loads on a plain variable name should PASS ──────────
# Variable names like ``cleaned``, ``config_str``, ``raw`` (not raw_text)
# should not trigger.  Only ``.text`` attribute or the specific
# ``raw_text`` variable is flagged.
create_test_file "good4.py" 'config = json.loads(config_str)' > /dev/null
assert_passes "json.loads(config_str) is allowed"
reset_tmpdir

# ─── Test 9: json.loads(bytes.decode()) should PASS ───────────────────
# A bytes-decoded string is not the .text attribute pattern.
create_test_file "good5.py" 'data = json.loads(blob.decode())' > /dev/null
assert_passes "json.loads(blob.decode()) is allowed"
reset_tmpdir

# ─── Test 10: json.loads inside comment should PASS ───────────────────
create_test_file "good6.py" '# Do not use: json.loads(response.text)' > /dev/null
assert_passes "Comment referencing json.loads(response.text) is allowed"
reset_tmpdir

# ─── Test 11: Multiple suspect calls in one file should fail ──────────
create_test_file "multi.py" 'def foo():
    a = json.loads(response.text)
    b = json.loads(raw_text)
    return a, b' > /dev/null
assert_fails "Multiple suspect json.loads calls in one file are detected"
reset_tmpdir

# ─── Test 12: Non-Python file with matching pattern should PASS ───────
# The check scans *.py only.  JavaScript/TypeScript code is not its scope.
create_test_file "script.js" 'const data = json.loads(response.text);' > /dev/null
assert_passes "Non-Python file is not scanned"
reset_tmpdir

# ─── Test 13: .venv directory should be excluded ──────────────────────
mkdir -p "$TMPDIR_TEST/.venv/lib"
printf '%s\n' 'parsed = json.loads(response.text)' > "$TMPDIR_TEST/.venv/lib/foo.py"
assert_passes ".venv directory is excluded"
reset_tmpdir

# ─── Test 14: tests directory should be excluded ──────────────────────
mkdir -p "$TMPDIR_TEST/tests"
printf '%s\n' 'parsed = json.loads(response.text)' > "$TMPDIR_TEST/tests/test_foo.py"
assert_passes "tests directory is excluded"
reset_tmpdir

# ─── Test 15: Pre-fix state of #2518 would have been caught ───────────
# Reproduces the exact variable-name pattern from the PR #2543 diff for
# ``packages/scraper-framework/src/ingestion/llm_extract.py`` before the
# fix: ``cleaned = strip_llm_json_fences(raw_text); parsed =
# json.loads(cleaned)``.  Note that the *literal* pre-fix code used
# ``json.loads(cleaned)`` — which this check does NOT flag by design,
# because ``cleaned`` is a generic variable name used all over the
# codebase for non-LLM JSON too.  The check instead targets the more
# discriminating patterns (``.text`` attribute access and the specific
# ``raw_text`` variable) that survive in the codebase today.
#
# This test reproduces the pattern that the nlp-pipeline files used at
# the time of the fix — ``json.loads(raw_text)`` — to confirm detection.
create_test_file "prefix2518_nlp.py" 'raw_text = response.content[0].text.strip()
parsed = json.loads(raw_text)' > /dev/null
assert_fails "Pre-fix #2518 pattern json.loads(raw_text) is detected"
reset_tmpdir

# ─── Test 16: Real-world Santa Clara LLM shape is caught ──────────────
# The exact pattern in the pre-fix nlp-pipeline code.
create_test_file "entity_extraction.py" '    raw_text = response.content[0].text.strip()
    parsed = json.loads(raw_text)
    return parsed.get("judge_name")' > /dev/null
assert_fails "Real-world entity extraction pre-fix pattern is detected"
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
