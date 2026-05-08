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

# Cleanup of temp directories on exit via the shared helper (see #4343).
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"

TMPDIR_TEST="$(mktemp -d)"
register_temp_dir "$TMPDIR_TEST"

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

# ─── Shape (B) tests — bare ``arr=()`` + iterate-empty footgun ───────────
# These cover the bash 3.2 (macOS) inverse-direction skew: ``arr=()``
# initialises the array empty, but ``for x in "${arr[@]}"`` while
# still empty trips ``unbound variable`` on bash 3.2 even though the
# size form ``${#arr[@]}`` is fine. Tracked as #4336.

# ─── Test B1: Canonical shape — arr=() + for loop iterate empty ──────────
# This is the verbatim shape from the issue body's `Verify:` line.
write_file "bad_arr_init_iterate.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
arr=()
for x in "${arr[@]}"; do
    echo "got: $x"
done
EOF
assert_fails "arr=() + for x in \"\${arr[@]}\" iterate-empty triggers shape (B)"
reset_tmpdir

# ─── Test B2: ``[*]`` star expansion variant → fails ─────────────────────
write_file "bad_arr_star_expand.sh" <<'EOF'
#!/usr/bin/env bash
set -u
items=()
echo "${items[*]}"
EOF
assert_fails "items=() + \"\${items[*]}\" expansion triggers shape (B)"
reset_tmpdir

# ─── Test B3: Echo-then-iterate (no for loop) → fails ────────────────────
# A bare expansion outside a for loop is the same root-cause class.
write_file "bad_arr_bare_expand.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
xs=()
echo "${xs[@]}"
EOF
assert_fails "xs=() + bare \"\${xs[@]}\" expansion triggers shape (B)"
reset_tmpdir

# ─── Test B4: Safe — arr=() + size-only read → passes ────────────────────
# The size form ``${#arr[@]}`` is bash-3.2-safe on an empty initialised
# array. Pass 3 must NOT flag size-only reads.
write_file "good_arr_size_only.sh" <<'EOF'
#!/usr/bin/env bash
set -u
arr=()
if [[ ${#arr[@]} -eq 0 ]]; then
    echo "empty"
fi
EOF
assert_passes "arr=() + size-only \${#arr[@]} read passes (size form is bash-3.2-safe)"
reset_tmpdir

# ─── Test B5: Safe — arr=() + append-then-iterate → passes ───────────────
write_file "good_arr_append_then_iterate.sh" <<'EOF'
#!/usr/bin/env bash
set -u
arr=()
arr+=("x")
arr+=("y")
for v in "${arr[@]}"; do
    echo "$v"
done
EOF
assert_passes "arr=() + append-then-iterate (canonical safe pattern) passes"
reset_tmpdir

# ─── Test B6: Safe — arr=() + reassign-with-content + iterate → passes ───
write_file "good_arr_reassign_iterate.sh" <<'EOF'
#!/usr/bin/env bash
set -u
arr=()
arr=("a" "b" "c")
for v in "${arr[@]}"; do
    echo "$v"
done
EOF
assert_passes "arr=() + reassign-with-content + iterate passes"
reset_tmpdir

# ─── Test B7: Length-guard alone (without += or [@]+ guard) → fails ─────
# A length guard ``if [ "${#arr[@]}" -gt 0 ]; then`` is the runtime
# fix recommended by the check's report message — it makes the
# iteration safe at run time. But the static check cannot reason
# about ``if`` block scopes, and the AC's static criterion is "no
# ``arr+=(...)`` or ``arr=(...)`` assignment intervenes" (see #4336
# AC#1). So the static check still flags this shape; users should
# either pre-populate the array, use the ``${arr[@]+...}``
# parameter-expansion guard (Test B8), or accept that the iteration
# is unreachable at run time anyway. This test documents the
# limitation so future maintainers don't accidentally weaken the
# check trying to recognise length guards.
write_file "bad_arr_length_guard_only.sh" <<'EOF'
#!/usr/bin/env bash
set -u
arr=()
if [ "${#arr[@]}" -gt 0 ]; then
    for v in "${arr[@]}"; do
        echo "$v"
    done
fi
EOF
assert_fails "arr=() + length-guard alone (no += / [@]+ guard) still flags (static-scan limitation)"
reset_tmpdir

# ─── Test B8: Safe — arr=() + ${arr[@]+...} guarded expansion → passes ──
# The defensive parameter-expansion guard ``${arr[@]+"${arr[@]}"}``
# is bash-3.2-safe even on an empty initialised array because the
# leading ``[@]+...`` substitutes nothing when the array is empty.
write_file "good_arr_param_guard.sh" <<'EOF'
#!/usr/bin/env bash
set -u
arr=()
for v in ${arr[@]+"${arr[@]}"}; do
    echo "$v"
done
EOF
assert_passes "arr=() + \${arr[@]+...} parameter-expansion guard passes"
reset_tmpdir

# ─── Test B9: Safe — arr=() without nounset → passes ─────────────────────
# Without set -u, the iteration of an empty initialised array is a
# no-op on every bash version.
write_file "good_arr_no_nounset.sh" <<'EOF'
#!/usr/bin/env bash
arr=()
for v in "${arr[@]}"; do
    echo "$v"
done
EOF
assert_passes "arr=() + iterate-empty without set -u passes"
reset_tmpdir

# ─── Test B10: Safe — arr=("x") with content → passes (not bare-empty) ──
# A non-empty initialiser is not the bare-empty shape this check
# targets. The Pass 3 declaration regex requires whitespace-only
# between the parens.
write_file "good_arr_init_with_content.sh" <<'EOF'
#!/usr/bin/env bash
set -u
arr=("x" "y")
for v in "${arr[@]}"; do
    echo "$v"
done
EOF
assert_passes "arr=(\"x\" \"y\") with content + iterate passes (not bare-empty)"
reset_tmpdir

# ─── Test B11: Lookalike names (foo_bar vs foo) → passes ─────────────────
# Same anchoring rationale as the existing Test 12: a substring like
# ``foo_bar=("a")`` must not count as an assignment to ``foo``.
write_file "good_lookalike_b.sh" <<'EOF'
#!/usr/bin/env bash
set -u
foo=()
foo_bar=("a" "b")
echo "${#foo_bar[@]}"
foo+=("x")
echo "${foo[@]}"
EOF
assert_passes "Lookalike names (foo_bar) are not confused with the declared name (foo) for shape (B)"
reset_tmpdir

# ─── Test B12: Verbatim issue body Verify clause (sanity) ────────────────
# AC#1 Verify line: drop a probe ``tmp/probe.sh`` containing
# ``set -uo pipefail; arr=(); for x in "${arr[@]}"; do echo "$x"; done``
# and run the check against ``tmp/``. Exits non-zero with the probe
# filename + line number. Reproduce that flow here.
TESTS=$((TESTS + 1))
PROBE_DIR="$TMPDIR_TEST/probe_root"
mkdir -p "$PROBE_DIR/scripts"
cat > "$PROBE_DIR/scripts/probe.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
arr=()
for x in "${arr[@]}"; do
    echo "$x"
done
EOF
chmod +x "$PROBE_DIR/scripts/probe.sh"
PROBE_OUT_FILE="$TMPDIR_TEST/probe_output.txt"
"$CHECK_SCRIPT" "$PROBE_DIR" > "$PROBE_OUT_FILE" 2>&1 && probe_rc=0 || probe_rc=$?
if [[ "$probe_rc" -eq 0 ]]; then
    echo "FAIL: AC#1 Verify probe — expected check to exit non-zero, got 0"
    FAILURES=$((FAILURES + 1))
elif ! grep -q "probe.sh" "$PROBE_OUT_FILE"; then
    echo "FAIL: AC#1 Verify probe — output missing probe filename"
    cat "$PROBE_OUT_FILE"
    FAILURES=$((FAILURES + 1))
elif ! grep -qE "probe.sh:[0-9]+" "$PROBE_OUT_FILE"; then
    echo "FAIL: AC#1 Verify probe — output missing line number"
    cat "$PROBE_OUT_FILE"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: AC#1 Verify probe — check fails with probe filename + line number"
fi
rm -rf "$PROBE_DIR" "$PROBE_OUT_FILE"

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
