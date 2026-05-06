#!/usr/bin/env bash
# test_check_bash_set_u_empty_array.sh — Tests for
# scripts/check-bash-set-u-empty-array.sh.
#
# Verifies that the checker correctly flags the bash 5.x footgun where
# ``declare -a <name>`` (without ``=()``) is later expanded as
# ``${#<name>[@]}`` under ``set -u``, while NOT flagging the safe
# variants (``=()`` initializer, append-then-read, no nounset).
#
# Each test writes a synthetic shell file under ``$TMPDIR_TEST/scripts/``
# and invokes the check against ``$TMPDIR_TEST``.
#
# Usage:
#   scripts/tests/test_check_bash_set_u_empty_array.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-bash-set-u-empty-array.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

mkdir -p "$TMPDIR_TEST/scripts"

write_file() {
    # $1: file path (relative to $TMPDIR_TEST/scripts/)
    # contents via stdin
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

# ─── Test 1: Bare declare -a + set -u + ${#name[@]} read → fails ────────
write_file "bad_declare_a_size_read.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a missing
echo "${#missing[@]}"
EOF
assert_fails "declare -a + set -u + \${#name[@]} read triggers the check"
reset_tmpdir

# ─── Test 2: Bare declare -a + set -euo pipefail + read → fails ─────────
write_file "bad_set_euo_pipefail.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
declare -a unresolved
if (( ${#unresolved[@]} > 0 )); then
    echo "found"
fi
EOF
assert_fails "declare -a + set -euo pipefail + size read triggers the check"
reset_tmpdir

# ─── Test 3: Bare declare -a + set -o nounset + read → fails ────────────
write_file "bad_set_o_nounset.sh" <<'EOF'
#!/usr/bin/env bash
set -o nounset
declare -a items
echo "${items[@]}"
EOF
assert_fails "declare -a + set -o nounset + array read triggers the check"
reset_tmpdir

# ─── Test 4: Bare typeset -a (synonym) + set -u + read → fails ──────────
write_file "bad_typeset_a.sh" <<'EOF'
#!/usr/bin/env bash
set -u
typeset -a items
echo "${#items[@]}"
EOF
assert_fails "typeset -a + set -u + size read triggers the check"
reset_tmpdir

# ─── Test 5: Safe — name=() initializer + set -u + read → passes ────────
# This is the canonical fix the check suggests.
write_file "good_empty_initializer.sh" <<'EOF'
#!/usr/bin/env bash
set -u
missing=()
echo "${#missing[@]}"
EOF
assert_passes "name=() initializer with set -u and size read passes"
reset_tmpdir

# ─── Test 6: Safe — declare -a name=() inline assignment → passes ───────
write_file "good_declare_inline.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a missing=()
echo "${#missing[@]}"
EOF
assert_passes "declare -a name=() inline assignment passes"
reset_tmpdir

# ─── Test 7: Safe — declare -a + append-before-read → passes ────────────
# ``+=`` on an undeclared array still binds it, so a subsequent
# ``${#name[@]}`` read is safe even on bash 5.x with set -u.
write_file "good_declare_then_append.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a items
items+=("first")
items+=("second")
echo "${#items[@]}"
EOF
assert_passes "declare -a + append-before-read (correct existing pattern) passes"
reset_tmpdir

# ─── Test 8: Safe — declare -a + plain assign + read → passes ──────────
write_file "good_declare_then_assign.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a items
items=("a" "b")
echo "${#items[@]}"
EOF
assert_passes "declare -a + plain assign-before-read passes"
reset_tmpdir

# ─── Test 9: Safe — no set -u + declare -a + read → passes ─────────────
# Without nounset the read is harmless on every bash version.
write_file "good_no_nounset.sh" <<'EOF'
#!/usr/bin/env bash
declare -a items
echo "${#items[@]}"
EOF
assert_passes "declare -a + read without set -u (no nounset, no bug) passes"
reset_tmpdir

# ─── Test 10: Safe — set -u + declare -a + only @ expansion (no read) ───
# Just declaring without ever reading is safe; the check should not
# flag a bare declare that is never expanded.
write_file "good_declare_no_read.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a items
echo "ok"
EOF
assert_passes "declare -a never expanded passes"
reset_tmpdir

# ─── Test 11: Safe — comments referencing the pattern → passes ─────────
write_file "good_comments.sh" <<'EOF'
#!/usr/bin/env bash
# Historical: 'declare -a missing' under 'set -u' would trip
# 'unbound variable' on bash 5.x when ${#missing[@]} was read.
# Fix: missing=() instead of declare -a missing.
set -u
missing=()
echo "${#missing[@]}"
EOF
assert_passes "Comments referencing the bad pattern are exempted"
reset_tmpdir

# ─── Test 12: Safe — substring/lookalike name → passes ─────────────────
# ``foo_bar=(...)`` should NOT count as an assignment to ``foo``, and
# ``${#foo_bar[@]}`` should NOT count as a read of ``foo``. The
# check uses word-boundary anchoring on the name.
write_file "good_lookalike_names.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a foo
foo_bar=("x" "y")
echo "${#foo_bar[@]}"
foo=()
echo "${#foo[@]}"
EOF
assert_passes "Lookalike names (foo_bar) are not confused with the declared name (foo)"
reset_tmpdir

# ─── Test 13: Mixed — one bad, one good in same file → fails ───────────
write_file "mixed_bad_and_good.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a bad_one
declare -a good_one
good_one=("x")
echo "${#bad_one[@]}"
echo "${#good_one[@]}"
EOF
assert_fails "Mixed file flags only the bad declare and not the good one"
reset_tmpdir

# ─── Test 14: Multiple violations across files → fails ─────────────────
write_file "bad_a.sh" <<'EOF'
#!/usr/bin/env bash
set -u
declare -a x
echo "${#x[@]}"
EOF
write_file "bad_b.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
declare -a y
echo "${y[@]}"
EOF
assert_fails "Violations across multiple files are detected"
reset_tmpdir

# ─── Test 15: Self-scan — real repo scripts/ tree → passes ─────────────
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" "$REPO_ROOT" > /dev/null 2>&1; then
    echo "PASS: Real repo scripts/ tree passes (no violations)"
else
    echo "FAIL: Real repo scripts/ tree contains a violation"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 16: No self-match on ci.yml step name ────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain a forbidden token (e.g. literal "declare -a" in
# the step's name: field). See #2541/#2542.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-bash-set-u-empty-array.sh" "sh"

# ─── Summary ───────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
