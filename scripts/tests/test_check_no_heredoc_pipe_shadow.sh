#!/usr/bin/env bash
# test_check_no_heredoc_pipe_shadow.sh — Tests for
# check-no-heredoc-pipe-shadow.sh.
#
# Creates temporary shell-script fixtures to verify the checker
# detects the forbidden `... | python3 << HEREDOC` + stdin-read
# pattern while allowing the safe alternatives (heredoc-without-pipe,
# pipe-without-heredoc, comment lines).
#
# Usage:
#   scripts/tests/test_check_no_heredoc_pipe_shadow.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-heredoc-pipe-shadow.sh"
FAILURES=0
TESTS=0

# Use a temp dir outside the repo's `tmp/` (which the check excludes).
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    local path="$TMPDIR_TEST/$name"
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

# ─── Test 1: The canonical bad pattern is detected ────────────────────────
# This is the exact shape that bit issue #4252 / motivated #4267:
#   $(echo "$JSON" | python3 << 'PY'
#     import sys, json
#     data = json.load(sys.stdin)  # <-- bug
#     print(data["foo"])
#   PY
#   )
create_test_file "bad_canonical.sh" '#!/usr/bin/env bash
JSON='\''{"foo": 1}'\''
RESULT=$(echo "$JSON" | python3 << '\''PY'\''
import sys, json
data = json.load(sys.stdin)
print(data["foo"])
PY
)
echo "$RESULT"'
assert_fails "Canonical bad pattern (echo \$JSON | python3 << 'PY' ... json.load(sys.stdin) ... PY) is detected"
reset_tmpdir

# ─── Test 2: Bad pattern with double-quoted tag ──────────────────────────
create_test_file "bad_dquote_tag.sh" '#!/usr/bin/env bash
echo "$X" | python3 <<"PY"
import sys, json
print(json.load(sys.stdin))
PY'
assert_fails "Bad pattern with double-quoted heredoc tag is detected"
reset_tmpdir

# ─── Test 3: Bad pattern with bare (unquoted) tag ────────────────────────
create_test_file "bad_bare_tag.sh" '#!/usr/bin/env bash
echo "$X" | python3 << EOF
import sys
data = sys.stdin.read()
print(data)
EOF'
assert_fails "Bad pattern with bare unquoted tag is detected"
reset_tmpdir

# ─── Test 4: Bad pattern with sys.stdin.read() ───────────────────────────
create_test_file "bad_stdin_read.sh" '#!/usr/bin/env bash
echo "data" | python3 << '\''PY'\''
import sys
content = sys.stdin.read()
print(len(content))
PY'
assert_fails "Bad pattern using sys.stdin.read() is detected"
reset_tmpdir

# ─── Test 5: Bad pattern with sys.stdin.readlines() ──────────────────────
create_test_file "bad_stdin_readlines.sh" '#!/usr/bin/env bash
cat foo.txt | python3 << '\''PY'\''
import sys
for line in sys.stdin.readlines():
    print(line.strip())
PY'
assert_fails "Bad pattern using sys.stdin.readlines() is detected"
reset_tmpdir

# ─── Test 6: Bad pattern using <<- (dash form, allows tab-indented terminator) ─
# Note: bash <<- only strips leading TABs, not arbitrary whitespace. We
# write a literal tab before the terminator.
printf '%s\n' '#!/usr/bin/env bash' \
    'echo "$X" | python3 <<- '\''PY'\''' \
    '	import sys, json' \
    '	data = json.load(sys.stdin)' \
    '	print(data)' \
    '	PY' > "$TMPDIR_TEST/bad_dash_form.sh"
assert_fails "Bad pattern using <<- dash form is detected"
reset_tmpdir

# ─── Test 7: Bad pattern with -u flag between python3 and << ─────────────
create_test_file "bad_with_flag.sh" '#!/usr/bin/env bash
echo "$X" | python3 -u <<'\''PY'\''
import sys, json
print(json.load(sys.stdin))
PY'
assert_fails "Bad pattern with python3 -u flag is detected"
reset_tmpdir

# ─── Test 8: Heredoc WITHOUT a pipe is allowed ───────────────────────────
# Python reads the heredoc as source code; stdin is unset. This is the
# safe pattern (recipe #2 from the docstring).
create_test_file "good_no_pipe.sh" '#!/usr/bin/env bash
python3 << '\''PY'\''
import sys, json
# json.load would fail here too, but stdin is /dev/tty, not a heredoc.
print("hello")
PY'
assert_passes "Heredoc without pipe predecessor is allowed"
reset_tmpdir

# ─── Test 9: Pipe + python3 -c (no heredoc) is allowed ───────────────────
# Recipe #1 from the docstring — the canonical safe form.
create_test_file "good_pipe_dash_c.sh" '#!/usr/bin/env bash
echo "$X" | python3 -c "import sys, json; print(json.load(sys.stdin))"'
assert_passes "Pipe + python3 -c (no heredoc) is allowed"
reset_tmpdir

# ─── Test 10: Heredoc + pipe but Python body does NOT read stdin ─────────
# This is technically an unusual but legal shape: the pipe data goes
# unused, but the Python code does not call json.load/stdin.read, so
# there is no silent miscompile of JSON parsing. Don't flag it — too
# many false positives. The check is precise to the json.load /
# sys.stdin.read shape that produces the "Expecting value" footgun.
create_test_file "good_pipe_no_stdin_read.sh" '#!/usr/bin/env bash
echo "ignored" | python3 << '\''PY'\''
import sys
print("hello world")
PY'
assert_passes "Pipe + heredoc but no stdin-read in body is allowed"
reset_tmpdir

# ─── Test 11: Heredoc + tmpfile + argv (recipe #3) is allowed ────────────
create_test_file "good_tmpfile_argv.sh" '#!/usr/bin/env bash
TMP=$(mktemp -d)
printf '\''%s'\'' "$JSON" > "$TMP/payload.json"
python3 << '\''PY'\'' "$TMP/payload.json"
import json, sys
with open(sys.argv[1]) as fh:
    data = json.load(fh)
print(data)
PY'
assert_passes "Heredoc + tmpfile + argv pattern (recipe #3) is allowed"
reset_tmpdir

# ─── Test 12: Comment line documenting the pattern is allowed ────────────
# A shell comment that mentions the bad pattern as cautionary context
# should not trigger the check (the comment is not executable).
create_test_file "good_comment.sh" '#!/usr/bin/env bash
# Beware: "echo X | python3 << '\''PY'\''" with json.load(sys.stdin)
# silently parses the heredoc as JSON. Use the tmpfile pattern instead.
echo "$X" | python3 -c "print(input())"'
assert_passes "Comment lines describing the pattern are allowed"
reset_tmpdir

# ─── Test 13: Markdown files are not scanned ─────────────────────────────
# Documentation that shows the bad pattern as a teaching example must
# not trigger the check. The check only scans *.sh and *.bash.
create_test_file "docs/teaching.md" '
Bad pattern:
```bash
echo "$X" | python3 << '\''PY'\''
import sys, json
data = json.load(sys.stdin)
PY
```'
assert_passes "Markdown documentation showing the pattern is allowed (not scanned)"
reset_tmpdir

# ─── Test 14: docs/investigations/*.sh are still excluded ────────────────
# Post-mortem .sh repros under docs/investigations/ are allowed to
# contain the pattern as historical context.
create_test_file "docs/investigations/repro_2026_05.sh" '#!/usr/bin/env bash
echo "$X" | python3 << '\''PY'\''
import sys, json
data = json.load(sys.stdin)
PY'
assert_passes "docs/investigations/*.sh repros are allowed to reference the pattern"
reset_tmpdir

# ─── Test 15: Empty directory passes ─────────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 16: .git directory is excluded ─────────────────────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf '%s\n' '#!/usr/bin/env bash' \
    'echo "$X" | python3 << '\''PY'\''' \
    'import sys, json' \
    'json.load(sys.stdin)' \
    'PY' > "$TMPDIR_TEST/.git/objects/badfile.sh"
assert_passes ".git directory contents are excluded"
reset_tmpdir

# ─── Test 17: node_modules is excluded ───────────────────────────────────
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf '%s\n' '#!/usr/bin/env bash' \
    'echo "$X" | python3 << '\''PY'\''' \
    'import sys, json' \
    'json.load(sys.stdin)' \
    'PY' > "$TMPDIR_TEST/node_modules/some-pkg/install.sh"
assert_passes "node_modules directory is excluded"
reset_tmpdir

# ─── Test 18: tmp/ is excluded ───────────────────────────────────────────
mkdir -p "$TMPDIR_TEST/tmp"
printf '%s\n' '#!/usr/bin/env bash' \
    'echo "$X" | python3 << '\''PY'\''' \
    'json.load(sys.stdin)' \
    'PY' > "$TMPDIR_TEST/tmp/scratch.sh"
assert_passes "tmp/ directory is excluded"
reset_tmpdir

# ─── Test 19: Multiple violations in one file are detected ───────────────
create_test_file "multi.sh" '#!/usr/bin/env bash
echo "$A" | python3 << '\''PY'\''
import sys, json
print(json.load(sys.stdin))
PY
echo "---"
echo "$B" | python3 << '\''QY'\''
import sys
print(sys.stdin.read())
QY'
assert_fails "Multiple violations in one file are detected"
reset_tmpdir

# ─── Test 20: Bad block followed by clean block does not falsely silence ─
# State machine must reset cleanly: a clean second block should NOT
# mask a violation in the first block.
create_test_file "bad_then_good.sh" '#!/usr/bin/env bash
echo "$A" | python3 << '\''PY'\''
import sys, json
print(json.load(sys.stdin))
PY

python3 << '\''QY'\''
print("safe heredoc, no pipe")
QY'
assert_fails "Bad block followed by safe block still flags the bad block"
reset_tmpdir

# ─── Test 21: tag must match exactly (substring of TAG inside body OK) ──
# A line containing `PY` as part of a longer token in the body must
# NOT be mistaken for the heredoc terminator (terminator is a whole
# line equal to the tag).
create_test_file "tag_substring.sh" '#!/usr/bin/env bash
echo "$X" | python3 << '\''PY'\''
import sys, json
# This line mentions PYTHON but is not the terminator.
data = json.load(sys.stdin)
print(data)
PY'
assert_fails "Substring of tag in body does not falsely terminate the heredoc"
reset_tmpdir

# ─── Test 22: Different tags (PY vs EOF) don't interfere ─────────────────
create_test_file "different_tags.sh" '#!/usr/bin/env bash
echo "$X" | python3 << EOF
import sys
print(sys.stdin.read())
EOF
echo "---"
python3 << PY
print("safe block")
PY'
assert_fails "Bad EOF-tagged block is detected even when followed by a clean PY block"
reset_tmpdir

# ─── Test 23: Self-match assertion on ci.yml step name ───────────────────
# The CI step name that runs this guard must not itself contain the
# forbidden pattern. See #2541/#2542 for the class of bug.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-heredoc-pipe-shadow.sh" "sh"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
