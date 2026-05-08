#!/usr/bin/env bash
# test_check_bash_compat.sh — Tests for scripts/check-bash-compat.sh
#
# Verifies that the checker fails on synthetic shell scripts using each
# forbidden bash 4+ construct and passes on known-good shell scripts
# that stick to the bash 3.2 subset. Also regression-tests that the
# current ``scripts/**/*.sh`` tree passes — an existing script using a
# forbidden construct would need to be fixed as part of the same PR
# that introduces this check.
#
# Usage:
#   scripts/tests/test_check_bash_compat.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-bash-compat.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# The check expects a ``scripts/`` subdir; synthesize one inside the
# temp dir. All per-test files are written under $TMPDIR_TEST/scripts/.
mkdir -p "$TMPDIR_TEST/scripts"

write_file() {
    # $1: file path (relative to $TMPDIR_TEST/scripts/)
    # $2: contents (via stdin)
    local path="$TMPDIR_TEST/scripts/$1"
    mkdir -p "$(dirname "$path")"
    cat > "$path"
    chmod +x "$path"
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST/scripts"
    mkdir -p "$TMPDIR_TEST/scripts"
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

# ─── Test 1: Known-bad — mapfile in a script fails ──────────────────────
write_file "bad_mapfile.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
mapfile -t lines < <(echo "a")
echo "${lines[0]}"
EOF
assert_fails "mapfile triggers the check"
reset_tmpdir

# ─── Test 2: Known-bad — readarray fails ────────────────────────────────
write_file "bad_readarray.sh" <<'EOF'
#!/usr/bin/env bash
readarray -t x < <(echo "b")
EOF
assert_fails "readarray triggers the check"
reset_tmpdir

# ─── Test 3: Known-bad — declare -A fails ───────────────────────────────
write_file "bad_declare_A.sh" <<'EOF'
#!/usr/bin/env bash
declare -A colors=([red]=1 [blue]=2)
echo "${colors[red]}"
EOF
assert_fails "declare -A triggers the check"
reset_tmpdir

# ─── Test 4: Known-bad — declare -g fails ───────────────────────────────
write_file "bad_declare_g.sh" <<'EOF'
#!/usr/bin/env bash
f() {
    declare -g X=1
}
f
echo "$X"
EOF
assert_fails "declare -g triggers the check"
reset_tmpdir

# ─── Test 5: Known-bad — local -n nameref fails ─────────────────────────
write_file "bad_local_n.sh" <<'EOF'
#!/usr/bin/env bash
f() {
    local -n ref=$1
    echo "$ref"
}
v=hi
f v
EOF
assert_fails "local -n nameref triggers the check"
reset_tmpdir

# ─── Test 6: Known-bad — declare -n nameref fails ───────────────────────
write_file "bad_declare_n.sh" <<'EOF'
#!/usr/bin/env bash
v=hi
declare -n ref=v
echo "$ref"
EOF
assert_fails "declare -n nameref triggers the check"
reset_tmpdir

# ─── Test 7: Known-bad — typeset -A fails ───────────────────────────────
write_file "bad_typeset_A.sh" <<'EOF'
#!/usr/bin/env bash
typeset -A m=([a]=1)
echo "${m[a]}"
EOF
assert_fails "typeset -A triggers the check"
reset_tmpdir

# ─── Test 8: Known-bad — \${var,,} lowercase expansion fails ────────────
write_file "bad_lowercase.sh" <<'EOF'
#!/usr/bin/env bash
x=HELLO
echo "${x,,}"
EOF
assert_fails "\${var,,} lowercase expansion triggers the check"
reset_tmpdir

# ─── Test 9: Known-bad — \${var,} first-char-lower fails ────────────────
write_file "bad_lowercase_first.sh" <<'EOF'
#!/usr/bin/env bash
x=HELLO
echo "${x,}"
EOF
assert_fails "\${var,} first-char-lower expansion triggers the check"
reset_tmpdir

# ─── Test 10: Known-bad — \${var^^} uppercase expansion fails ───────────
write_file "bad_uppercase.sh" <<'EOF'
#!/usr/bin/env bash
x=hello
echo "${x^^}"
EOF
assert_fails "\${var^^} uppercase expansion triggers the check"
reset_tmpdir

# ─── Test 11: Known-bad — \${var^} first-char-upper fails ───────────────
write_file "bad_uppercase_first.sh" <<'EOF'
#!/usr/bin/env bash
x=hello
echo "${x^}"
EOF
assert_fails "\${var^} first-char-upper expansion triggers the check"
reset_tmpdir

# ─── Test 12: Known-bad — ;;& case fall-through fails ───────────────────
write_file "bad_fallthrough.sh" <<'EOF'
#!/usr/bin/env bash
case $1 in
    a) echo a ;;&
    b) echo b ;;
esac
EOF
assert_fails ";;& case fall-through triggers the check"
reset_tmpdir

# ─── Test 13: Known-bad — |& pipe shorthand fails ───────────────────────
write_file "bad_pipe_and.sh" <<'EOF'
#!/usr/bin/env bash
echo foo |& cat
EOF
assert_fails "|& pipe-both shorthand triggers the check"
reset_tmpdir

# ─── Test 14: Known-good — clean script passes ──────────────────────────
write_file "good_clean.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# A bash 3.2-compatible script that lists files and greps them.
files=()
while IFS= read -r f; do
    files+=("$f")
done < <(find . -type f -name '*.txt')
for f in "${files[@]+"${files[@]}"}"; do
    grep -l "hello" "$f" || true
done
EOF
assert_passes "Clean bash 3.2-compatible script passes"
reset_tmpdir

# ─── Test 15: Known-good — comments referencing forbidden tokens pass ───
write_file "good_comments.sh" <<'EOF'
#!/usr/bin/env bash
# We avoid mapfile and readarray because macOS bash 3.2 lacks them.
# See docs/agent/code-standards.md for the compatibility rules.
# Also avoid: declare -A, declare -g, local -n, declare -n, typeset -A,
# typeset -n, ${var,,}, ${var,}, ${var^^}, ${var^}, ;;& and |&.
echo "hello world"
EOF
assert_passes "Comments mentioning forbidden tokens are exempted"
reset_tmpdir

# ─── Test 16: Known-good — indented comment also exempted ───────────────
write_file "good_indented_comment.sh" <<'EOF'
#!/usr/bin/env bash
case $1 in
    a)
        # ;;& would be nice here but bash 3.2 can't parse it; use
        # separate arms instead.
        echo a
        ;;
    *)
        echo unknown
        ;;
esac
EOF
assert_passes "Indented comment mentioning ;;& is exempted"
reset_tmpdir

# ─── Test 17: Known-good — similar-looking but non-matching tokens pass ─
# ``declare -a`` (lowercase — indexed array) is allowed. Only ``-A``
# (associative) is forbidden. Similarly ``declare -r`` (readonly) is
# fine.
write_file "good_lookalikes.sh" <<'EOF'
#!/usr/bin/env bash
declare -a indexed=(a b c)
declare -r readonly_var="x"
echo "${indexed[0]} $readonly_var"
# Variable names containing the forbidden substrings are fine:
my_mapfile_wrapper() {
    grep -l foo .
}
my_mapfile_wrapper
EOF
assert_passes "declare -a and declare -r (lookalikes) pass"
reset_tmpdir

# ─── Test 18: Known-good — function name containing 'mapfile' passes ────
write_file "good_funcname.sh" <<'EOF'
#!/usr/bin/env bash
make_mapfile() {
    # Intentionally named to confuse a naive substring match.
    echo "ok"
}
make_mapfile
EOF
assert_passes "Function named make_mapfile is not flagged"
reset_tmpdir

# ─── Test 19: Known-good — regular pipe (|) passes ──────────────────────
# ``|&`` is forbidden, but plain ``|`` must not be flagged. The
# distinction matters: ``echo foo | cat`` is the 99% case in shell
# scripts.
write_file "good_plain_pipe.sh" <<'EOF'
#!/usr/bin/env bash
echo foo | cat
echo bar | wc -l
# The canonical bash 3.2 form for pipe-both-stdout-and-stderr:
echo baz 2>&1 | cat
EOF
assert_passes "Plain | pipe and 2>&1 | pattern pass"
reset_tmpdir

# ─── Test 20: Known-good — no *.sh files passes ─────────────────────────
assert_passes "Empty scripts/ directory passes"

# ─── Test 21: Mixed — good + bad → fails (bad catches) ──────────────────
write_file "good_in_mixed.sh" <<'EOF'
#!/usr/bin/env bash
echo ok
EOF
write_file "bad_in_mixed.sh" <<'EOF'
#!/usr/bin/env bash
mapfile -t x < <(echo a)
EOF
assert_fails "Mixed tree fails when any file has a forbidden construct"
reset_tmpdir

# ─── Test 22: Self-match — the check script itself must pass ────────────
# Run the check against its own repo location. The check must not
# trip on its own source file (which spells every forbidden token
# literally in its header comments and pattern arrays).
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" "$REPO_ROOT" > /dev/null 2>&1; then
    echo "PASS: Real repo scripts/ tree passes (no forbidden constructs)"
else
    echo "FAIL: Real repo scripts/ tree contains a forbidden construct"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 23a: Per-construct Fix block emitted (#4346) ─────────────────
# When a violation fires, the report must include a "Fix:" header and a
# per-construct rewrite suggestion that names the actual construct.
write_file "bad_mapfile_for_fix.sh" <<'EOF'
#!/usr/bin/env bash
mapfile -t lines < <(echo "a")
EOF
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "^  Fix:" \
    && echo "$output" | grep -q "\\[mapfile (bash 4+ builtin)\\]" \
    && echo "$output" | grep -q "while IFS= read -r line"; then
    echo "PASS: Test 23a — Fix block names the construct + emits per-construct rewrite"
else
    echo "FAIL: Test 23a — Fix block missing or malformed"
    echo "  Got:"
    echo "$output" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 23b: Fix block only shows constructs that fired (#4346) ──────
# Two violations of different constructs should both appear; constructs
# that did not fire should not.
write_file "bad_two.sh" <<'EOF'
#!/usr/bin/env bash
declare -A m=([a]=1)
echo "${x,,}"
EOF
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "\\[declare -A (associative array, bash 4+)\\]" \
    && echo "$output" | grep -q "\\[\${var,,} / \${var,} (lowercase expansion, bash 4+)\\]" \
    && ! echo "$output" | grep -q "\\[mapfile (bash 4+ builtin)\\]"; then
    echo "PASS: Test 23b — Fix block only emits rewrites for constructs that fired"
else
    echo "FAIL: Test 23b — Fix block emitted unexpected or missing constructs"
    echo "  Got:"
    echo "$output" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 23: No self-match on ci.yml step name ─────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain a forbidden token. If it did, the guard would
# fail on its first CI run (see #2541/#2542 for the class of bug).
#
# The shared helper _guard_self_match_helpers.sh writes extracted step
# names to ``$TMPDIR_TEST/ci_step_names.<ext>`` — outside the
# ``$TMPDIR_TEST/scripts/`` subtree this check scans. Rather than
# restructure the helper, we inline the narrow version here: use awk to
# pull step names that invoke the guard, write them to a .sh file
# inside ``$TMPDIR_TEST/scripts/``, and re-run the check.
CI_YML="$REPO_ROOT/.github/workflows/ci.yml"
EXTRACTOR_AWK="$SCRIPT_DIR/tests/_extract_guard_step_names.awk"
if [[ -f "$CI_YML" && -f "$EXTRACTOR_AWK" ]]; then
    awk -v script="scripts/check-bash-compat.sh" -f "$EXTRACTOR_AWK" \
        "$CI_YML" > "$TMPDIR_TEST/scripts/ci_step_names.sh"
    assert_passes "No self-match on ci.yml step name for scripts/check-bash-compat.sh"
    reset_tmpdir
else
    echo "SKIP: ci.yml or extractor awk missing — self-match assertion skipped"
fi

# ─── Summary ────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
