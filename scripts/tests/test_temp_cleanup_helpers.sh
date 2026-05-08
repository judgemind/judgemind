#!/usr/bin/env bash
# test_temp_cleanup_helpers.sh — Regression test for issue #4343.
#
# Exercises scripts/tests/_temp_cleanup_helpers.sh — the shared
# EXIT-trap cleanup helper that the migration in #4343 swaps in for
# the hand-rolled ``TEMP_DIRS=() / cleanup() / trap cleanup EXIT``
# boilerplate across ~12+ shell test fixtures.
#
# What this verifies:
#
#   AC1 — Helper sources cleanly and exposes ``register_temp_dir`` +
#         ``register_temp_file`` as functions.
#   AC2 — Sourcing the helper installs an EXIT trap eagerly at
#         source time. This is the eager-install contract: the trap
#         must be installed in the shell that sources the helper, not
#         deferred until a register_* call (which might happen inside
#         a command-substitution subshell, leaving the parent shell
#         with no trap). See the helper's "When the trap is installed"
#         header comment for the full rationale.
#   AC3 — ``register_temp_dir`` rm-rfs registered directories on EXIT
#         even when the array contains zero, one, or many entries.
#   AC4 — ``register_temp_file`` rm-fs registered files on EXIT.
#   AC5 — Mixed registrations (dirs + files) clean up correctly in
#         the same trap.
#   AC6 — Bash 3.2 compatibility — the iteration form must be the
#         guarded ``${arr[@]+...}`` idiom, not the naive
#         ``"${arr[@]}"`` which trips bash 3.2 + ``set -u`` on an
#         empty array.
#   AC7 — Idempotent re-sourcing preserves registrations rather than
#         resetting the arrays.
#   AC8 — Argument validation: each helper rejects 0 or 2+ arguments,
#         and rejects an empty path argument, with exit/return code 2.
#
# Test strategy
# -------------
# Each scenario spawns a child bash process that sources the helper,
# registers some paths, and exits. The parent then verifies the paths
# no longer exist (cleanup happened) and the child exited cleanly.
#
# The "lazy trap install" check (AC2) inspects ``trap -p EXIT`` from
# inside a child shell that has sourced the helper but not called any
# register_* function — the output should be empty.
#
# Usage:
#   scripts/tests/test_temp_cleanup_helpers.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"

FAILURES=0
TESTS=0

# This test fixture itself uses the helper it is testing, to confirm
# the helper is usable in real test code. Its own scratch directory is
# registered for cleanup. (This is a self-host smoke — the AC tests
# below spawn fresh child shells so the helper's behavior is validated
# in isolation, not just via this fixture's own usage.)
. "$HELPER"

SELF_TMP=$(mktemp -d)
register_temp_dir "$SELF_TMP"

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# ── Precondition: helper exists ───────────────────────────────────────────
if [[ ! -f "$HELPER" ]]; then
    echo "FAIL: $HELPER does not exist" >&2
    exit 1
fi

# ── AC1: helper sources cleanly and exposes the two functions ────────────
out=$(bash -c "set -uo pipefail; . '$HELPER'; \
declare -F register_temp_dir > /dev/null && \
declare -F register_temp_file > /dev/null && \
echo OK" 2>&1) || true
if [[ "$out" == "OK" ]]; then
    pass "AC1: helper sources and exposes register_temp_dir + register_temp_file"
else
    fail "AC1: helper does not expose both functions" "got: $out"
fi

# ── AC2: eager trap install — sourcing installs an EXIT trap now ─────────
# ``trap -p EXIT`` after sourcing must show our cleanup function. We
# match against the function name rather than a fixed string so a
# refactor of the trap printf format (bash version drift) does not
# break the assertion.
out=$(bash -c "set -uo pipefail; . '$HELPER'; trap -p EXIT" 2>&1) || true
if [[ "$out" == *"_temp_cleanup_helpers__cleanup"* ]]; then
    pass "AC2: sourcing the helper eagerly installs the EXIT trap"
else
    fail "AC2: helper did not install EXIT trap at source time" "got: $out"
fi

# AC2b — eager-install means a register_* call made inside a command-
# substitution subshell (e.g. ``bindir=$(make_stub_bin)``) does NOT
# trip the dir-removed-before-test-uses-it footgun the lazy-install
# design had. With lazy install, the FIRST register_* inside ``$()``
# would install the trap in that subshell only; when the subshell
# exited, its trap would fire and rm the dir, breaking the parent
# test that just captured the path. With eager install at source
# time, the parent shell has the trap already; the subshell does not
# inherit it (POSIX bash semantics for command substitution), so the
# subshell's exit does NOT trigger any cleanup, and the directory
# survives for the parent test to use. This is the bug that surfaced
# during the #4343 migration of test_notify_telegram_exit_codes.sh —
# locking it in via a regression test.
PROBE_OUT=$(mktemp)
register_temp_file "$PROBE_OUT"
bash -c "set -uo pipefail; . '$HELPER'; \
mk_subshell() { local d; d=\$(mktemp -d); register_temp_dir \"\$d\"; echo \"\$d\"; }; \
got=\$(mk_subshell); \
test -d \"\$got\" && echo 'SUBSHELL_DID_NOT_REMOVE' > '$PROBE_OUT'" || {
    fail "AC2b: child shell exited non-zero"
}
if grep -q SUBSHELL_DID_NOT_REMOVE "$PROBE_OUT"; then
    pass "AC2b: register inside \$() does not remove dir via subshell trap"
else
    fail "AC2b: dir registered in subshell was removed before parent could use it" \
         "(would break the bindir=\$(make_stub_bin) pattern — eager install regressed?)"
fi

# ── AC3: register_temp_dir cleans up registered directories ───────────────

# Empty-registration path: ensure the trap fires cleanly even when
# only one register call is made on an otherwise-empty array.
SCRATCH=$(mktemp -d)
register_temp_dir "$SCRATCH"
PATH_FILE="$SCRATCH/paths.txt"

bash -c "set -uo pipefail; . '$HELPER'; \
d1=\$(mktemp -d); register_temp_dir \"\$d1\"; \
echo \"\$d1\" > '$PATH_FILE'" || {
    fail "AC3: child shell exited non-zero with single registration"
}

if [[ -s "$PATH_FILE" ]]; then
    d1=$(head -1 "$PATH_FILE")
    if [[ -d "$d1" ]]; then
        fail "AC3: directory $d1 still exists after child exit"
    else
        pass "AC3: single registered directory removed on exit"
    fi
else
    fail "AC3: paths.txt empty — child did not record path"
fi

# Multi-registration path: register 5 dirs, all should be removed.
PATH_FILE2="$SCRATCH/paths2.txt"
bash -c "set -uo pipefail; . '$HELPER'; \
for i in 1 2 3 4 5; do \
    d=\$(mktemp -d); \
    register_temp_dir \"\$d\"; \
    echo \"\$d\" >> '$PATH_FILE2'; \
done" || {
    fail "AC3: child shell exited non-zero with multi-registration"
}

if [[ -s "$PATH_FILE2" ]]; then
    all_gone=true
    while IFS= read -r d; do
        if [[ -d "$d" ]]; then
            all_gone=false
            fail "AC3: directory $d still exists after child exit"
        fi
    done < "$PATH_FILE2"
    if $all_gone; then
        pass "AC3: 5 registered directories all removed on exit"
    fi
else
    fail "AC3: paths2.txt empty — child did not record paths"
fi

# ── AC4: register_temp_file cleans up registered files ────────────────────
PATH_FILE3="$SCRATCH/paths3.txt"
bash -c "set -uo pipefail; . '$HELPER'; \
for i in 1 2 3; do \
    f=\$(mktemp); \
    register_temp_file \"\$f\"; \
    echo \"\$f\" >> '$PATH_FILE3'; \
done" || {
    fail "AC4: child shell exited non-zero with file registration"
}

if [[ -s "$PATH_FILE3" ]]; then
    all_gone=true
    while IFS= read -r f; do
        if [[ -e "$f" ]]; then
            all_gone=false
            fail "AC4: file $f still exists after child exit"
        fi
    done < "$PATH_FILE3"
    if $all_gone; then
        pass "AC4: registered files all removed on exit"
    fi
else
    fail "AC4: paths3.txt empty — child did not record paths"
fi

# ── AC5: mixed dir + file registrations clean up together ─────────────────
PATH_FILE4="$SCRATCH/paths4.txt"
bash -c "set -uo pipefail; . '$HELPER'; \
d=\$(mktemp -d); register_temp_dir \"\$d\"; \
f=\$(mktemp); register_temp_file \"\$f\"; \
echo \"DIR \$d\" >> '$PATH_FILE4'; \
echo \"FILE \$f\" >> '$PATH_FILE4'" || {
    fail "AC5: child shell exited non-zero with mixed registration"
}

if [[ -s "$PATH_FILE4" ]]; then
    all_gone=true
    while IFS= read -r line; do
        kind="${line%% *}"
        path="${line#* }"
        case "$kind" in
            DIR)  [[ -d "$path" ]] && { all_gone=false; fail "AC5: dir $path still exists"; } ;;
            FILE) [[ -e "$path" ]] && { all_gone=false; fail "AC5: file $path still exists"; } ;;
        esac
    done < "$PATH_FILE4"
    if $all_gone; then
        pass "AC5: mixed dir+file registrations cleaned up together"
    fi
else
    fail "AC5: paths4.txt empty — child did not record paths"
fi

# ── AC6: bash 3.2 + set -u empty-array iteration safety ───────────────────
# The helper itself sets up the empty array ``_TEMP_CLEANUP_HELPERS__DIRS=()``
# and iterates it via ``${arr[@]+...}`` in the trap body. This iteration
# shape is what the issue's verify command checks. We assert the
# repo-wide check passes on the tests/ directory after the helper landed.
out=$("$SCRIPT_DIR/check-bash-set-u-empty-array.sh" "$SCRIPT_DIR/tests/" 2>&1) || {
    fail "AC6: check-bash-set-u-empty-array.sh failed on scripts/tests/" "$out"
}
if [[ "$out" == *"all clean"* ]]; then
    pass "AC6: scripts/tests/ passes check-bash-set-u-empty-array.sh"
fi

# AC6b: also run the check in syntax-check mode against the helper file
# itself. ``bash -n`` validates parsing (catches ``${arr[@]+...}``-form
# typos); the empty-array check above validates semantics.
if bash -n "$HELPER" 2>/dev/null; then
    pass "AC6b: helper passes bash -n syntax check"
else
    fail "AC6b: helper failed bash -n syntax check"
fi

# ── AC7: idempotent re-sourcing preserves registrations ───────────────────
# A test which sources both this helper and another helper that also
# sources this one should not lose its registrations on the second
# source. We verify by sourcing twice in the same shell, registering
# in between, and ensuring the array still has the entry after the
# second source.
out=$(bash -c "set -uo pipefail; \
. '$HELPER'; \
d=\$(mktemp -d); \
register_temp_dir \"\$d\"; \
. '$HELPER'; \
echo \"COUNT=\${#_TEMP_CLEANUP_HELPERS__DIRS[@]}\"" 2>&1) || true
if [[ "$out" == "COUNT=1" ]]; then
    pass "AC7: re-sourcing preserves registrations"
else
    fail "AC7: re-sourcing reset registrations" "got: $out"
fi

# ── AC8: argument validation ──────────────────────────────────────────────

# 8a — register_temp_dir with no arg returns 2.
# Use ``exit \$?`` after the call so the child shell propagates the
# function's return code as its own exit code (without ``set -e``,
# the default is to keep going and exit with 0). We don't enable
# ``set -e`` here because the helper's ``return 2`` is exactly the
# return-code form ``set -e`` would not intercept on a ``[[ ]]``
# guard inside the function — propagating via ``exit \$?`` on the
# next line is the simplest way to surface the function's return
# code without relying on ``set -e`` semantics.
code=0
bash -c ". '$HELPER'; register_temp_dir; exit \$?" >/dev/null 2>&1 || code=$?
if [[ "$code" -eq 2 ]]; then
    pass "AC8a: register_temp_dir with no arg returns 2"
else
    fail "AC8a: register_temp_dir with no arg" "expected return 2, got $code"
fi

# 8b — register_temp_dir with empty string returns 2.
code=0
bash -c ". '$HELPER'; register_temp_dir ''; exit \$?" >/dev/null 2>&1 || code=$?
if [[ "$code" -eq 2 ]]; then
    pass "AC8b: register_temp_dir with empty path returns 2"
else
    fail "AC8b: register_temp_dir with empty path" "expected return 2, got $code"
fi

# 8c — register_temp_dir with two args returns 2.
code=0
bash -c ". '$HELPER'; register_temp_dir a b; exit \$?" >/dev/null 2>&1 || code=$?
if [[ "$code" -eq 2 ]]; then
    pass "AC8c: register_temp_dir with two args returns 2"
else
    fail "AC8c: register_temp_dir with two args" "expected return 2, got $code"
fi

# 8d — register_temp_file with no arg returns 2.
code=0
bash -c ". '$HELPER'; register_temp_file; exit \$?" >/dev/null 2>&1 || code=$?
if [[ "$code" -eq 2 ]]; then
    pass "AC8d: register_temp_file with no arg returns 2"
else
    fail "AC8d: register_temp_file with no arg" "expected return 2, got $code"
fi

# ── AC9: cleanup hook fires on exit, BEFORE the rm phase ──────────────────
# We register a hook that writes a marker file and then registers the
# parent directory for cleanup. The hook must run while the dir still
# exists; after the trap finishes, the dir is gone.
HOOK_TMP=$(mktemp -d)
register_temp_dir "$HOOK_TMP"
HOOK_OUT="$HOOK_TMP/hook.out"
bash -c "set -uo pipefail; . '$HELPER'; \
d=\$(mktemp -d); \
register_temp_dir \"\$d\"; \
mark_hook() { echo \"\$d\" > '$HOOK_OUT'; test -d \"\$d\" && echo 'DIR_PRESENT' >> '$HOOK_OUT'; }; \
register_cleanup_hook mark_hook; \
exit 0" || {
    fail "AC9: child shell with hook exited non-zero"
}
if [[ -s "$HOOK_OUT" ]]; then
    if grep -q DIR_PRESENT "$HOOK_OUT"; then
        recorded_dir=$(head -1 "$HOOK_OUT")
        if [[ -d "$recorded_dir" ]]; then
            fail "AC9: registered dir $recorded_dir was not removed after trap"
        else
            pass "AC9: hook ran before rm phase, dir removed afterwards"
        fi
    else
        fail "AC9: hook ran but DIR_PRESENT marker missing"
    fi
else
    fail "AC9: hook did not run (HOOK_OUT empty)"
fi

# AC9b — multiple hooks fire in registration order.
HOOK_OUT2="$HOOK_TMP/hook2.out"
bash -c "set -uo pipefail; . '$HELPER'; \
h1() { echo 1 >> '$HOOK_OUT2'; }; \
h2() { echo 2 >> '$HOOK_OUT2'; }; \
h3() { echo 3 >> '$HOOK_OUT2'; }; \
register_cleanup_hook h1; \
register_cleanup_hook h2; \
register_cleanup_hook h3" || {
    fail "AC9b: child shell with multi-hook exited non-zero"
}
if [[ -s "$HOOK_OUT2" ]]; then
    expected=$'1\n2\n3'
    actual=$(cat "$HOOK_OUT2")
    if [[ "$actual" == "$expected" ]]; then
        pass "AC9b: hooks run in registration order"
    else
        fail "AC9b: hooks ran out of order" "expected '$expected', got '$actual'"
    fi
else
    fail "AC9b: no hook output (HOOK_OUT2 empty)"
fi

# AC9c — register_cleanup_hook arg validation.
code=0
bash -c ". '$HELPER'; register_cleanup_hook; exit \$?" >/dev/null 2>&1 || code=$?
if [[ "$code" -eq 2 ]]; then
    pass "AC9c: register_cleanup_hook with no arg returns 2"
else
    fail "AC9c: register_cleanup_hook with no arg" "expected return 2, got $code"
fi

code=0
bash -c ". '$HELPER'; register_cleanup_hook ''; exit \$?" >/dev/null 2>&1 || code=$?
if [[ "$code" -eq 2 ]]; then
    pass "AC9d: register_cleanup_hook with empty arg returns 2"
else
    fail "AC9d: register_cleanup_hook with empty arg" "expected return 2, got $code"
fi

# AC9e — failing hook does not abort the rm phase.
HOOK_OUT3="$HOOK_TMP/hook3.out"
bash -c "set -uo pipefail; . '$HELPER'; \
d=\$(mktemp -d); \
echo \"\$d\" > '$HOOK_OUT3'; \
register_temp_dir \"\$d\"; \
broken_hook() { return 1; }; \
register_cleanup_hook broken_hook" || {
    fail "AC9e: child shell with broken hook exited non-zero"
}
if [[ -s "$HOOK_OUT3" ]]; then
    recorded_dir=$(head -1 "$HOOK_OUT3")
    if [[ -d "$recorded_dir" ]]; then
        fail "AC9e: dir $recorded_dir survived a failing hook (rm phase aborted)"
    else
        pass "AC9e: failing hook does not abort rm phase"
    fi
else
    fail "AC9e: HOOK_OUT3 empty"
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "========================================="
echo "Tests run: $TESTS"
echo "Failures:  $FAILURES"
echo "========================================="

if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
exit 0
