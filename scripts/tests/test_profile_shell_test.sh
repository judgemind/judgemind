#!/usr/bin/env bash
# test_profile_shell_test.sh — Tests for scripts/profile-shell-test.sh
#
# Builds small fixtures with known section layouts and asserts:
#   * Section markers are detected and timed.
#   * Exit code from the wrapped test is preserved verbatim.
#   * The TSV output has the expected shape (sections × 2 columns).
#   * Top-N summary appears in stdout in sorted order.
#   * The custom --section-pattern argument works.
#   * EXIT-trap clobbering (user installs ``trap cleanup EXIT``) is
#     handled — _section_close still runs.
#   * The full reference run against
#     scripts/tests/test_agent_runner_entrypoint.sh produces ≥ 30 TSV
#     rows AND the wrapped test still reports its full PASS count
#     (#4176 AC).
#
# Usage:
#   scripts/tests/test_profile_shell_test.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILER="$SCRIPT_DIR/profile-shell-test.sh"
TESTS_DIR="$SCRIPT_DIR/tests"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute scripts/tests/.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# ─── Helpers ─────────────────────────────────────────────────────────────

# Make a fixture .sh file under the temp dir; chmod +x so the wrapper can
# run it. Returns the absolute path.
make_fixture() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    chmod +x "$path"
    echo "$path"
}

assert_eq() {
    local desc="$1"
    local actual="$2"
    local expected="$3"
    TESTS=$((TESTS + 1))
    if [[ "$actual" = "$expected" ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc"
        echo "  expected: $expected"
        echo "  actual:   $actual"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_ge() {
    local desc="$1"
    local actual="$2"
    local floor="$3"
    TESTS=$((TESTS + 1))
    if [[ "$actual" -ge "$floor" ]]; then
        echo "PASS: $desc (got $actual, floor $floor)"
    else
        echo "FAIL: $desc"
        echo "  expected ≥ $floor"
        echo "  actual:    $actual"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_contains() {
    local desc="$1"
    local haystack="$2"
    local needle="$3"
    TESTS=$((TESTS + 1))
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc"
        echo "  expected to contain: $needle"
        echo "  haystack: $haystack"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: Three-section fixture; preserves exit code 0 ────────────────
# NOTE — timing-jitter constraint (#4188): the sort-by-elapsed-desc
# assertion below (Test 2) requires a strict ordering Test 3 > Test 2 >
# Test 1 by wall-clock elapsed time. On a busy CI runner, scheduling
# jitter can add 10s-100s of milliseconds to a `sleep` call, so the gaps
# between sleeps must be wide enough that no realistic jitter can
# reorder them. The 0.05 / 0.20 / 0.50 schedule below leaves a 4× gap
# between successive sleeps, which makes a flip statistically
# infeasible. The previous 0.02 / 0.04 / 0.06 schedule had only a 1.5×
# gap and flaked under load (see #4188 / PR #4187 CI run 25433409933).
# If you are tempted to reduce these sleeps to "speed up the test" —
# don't. The deterministic ordering matters more than the ~0.7s of
# extra wall-clock time.
fixture1=$(make_fixture "fixture_basic.sh" '#!/usr/bin/env bash
set -euo pipefail

# Test 1: alpha
sleep 0.05

# Test 2: beta
sleep 0.20

# Test 3: gamma
sleep 0.50
exit 0
')
tsv1="$TMPDIR_TEST/fixture1.tsv"
out1=$("$PROFILER" --tsv "$tsv1" --top 5 "$fixture1" 2>/dev/null)
exit1=$?
assert_eq "basic fixture exits 0" "$exit1" "0"
sections1=$(wc -l < "$tsv1" | tr -d ' ')
assert_eq "basic fixture records 3 sections" "$sections1" "3"
assert_contains "basic fixture summary names section 3" "$out1" "Test 3: gamma"
assert_contains "basic fixture summary names section 1" "$out1" "Test 1: alpha"
assert_contains "basic fixture summary header present" "$out1" "Top 5 longest sections:"

# ─── Test 2: Top of summary is the longest section ───────────────────────
# Section 3 is 0.06s so it should appear first (sorted desc).
first_label=$(sort -t '	' -k1,1 -n -r "$tsv1" | head -1 | cut -f2-)
assert_eq "summary is sorted by elapsed desc" "$first_label" "Test 3: gamma"

# ─── Test 3: Exit code is preserved on non-zero exits ────────────────────
fixture2=$(make_fixture "fixture_fail.sh" '#!/usr/bin/env bash
# Test 1: passing
true
# Test 2: failing
exit 7
')
tsv2="$TMPDIR_TEST/fixture2.tsv"
"$PROFILER" --tsv "$tsv2" "$fixture2" >/dev/null 2>&1
exit2=$?
assert_eq "non-zero exit code is propagated (rc=7)" "$exit2" "7"
sections2=$(wc -l < "$tsv2" | tr -d ' ')
# Test 2 calls exit before _section_close runs at end-of-script, but our
# header trap catches EXIT and closes Test 2. Expect 2 sections.
assert_eq "fail fixture records both sections via EXIT trap" "$sections2" "2"

# ─── Test 4: User-installed ``trap cleanup EXIT`` is chained ─────────────
fixture3=$(make_fixture "fixture_trap.sh" '#!/usr/bin/env bash
set -euo pipefail
USER_CLEANUP_FILE="'"$TMPDIR_TEST"'/user_cleanup_marker"
cleanup_user() {
    echo "user-cleanup" > "$USER_CLEANUP_FILE"
}
trap cleanup_user EXIT

# Test 1: alpha
sleep 0.01

# Test 2: beta
sleep 0.02

exit 0
')
tsv3="$TMPDIR_TEST/fixture3.tsv"
"$PROFILER" --tsv "$tsv3" "$fixture3" >/dev/null 2>&1
sections3=$(wc -l < "$tsv3" | tr -d ' ')
assert_eq "user-trap fixture records both sections" "$sections3" "2"
if [[ -f "$TMPDIR_TEST/user_cleanup_marker" ]]; then
    user_cleanup_marker_content=$(cat "$TMPDIR_TEST/user_cleanup_marker")
    assert_eq "user cleanup ran" "$user_cleanup_marker_content" "user-cleanup"
else
    TESTS=$((TESTS + 1))
    echo "FAIL: user cleanup ran (marker file missing)"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 5: Custom --section-pattern works ──────────────────────────────
fixture4=$(make_fixture "fixture_custom_pattern.sh" '#!/usr/bin/env bash
# T57a: alpha sub-test
sleep 0.01
# T57b: beta sub-test
sleep 0.02
# T57c: gamma sub-test
sleep 0.03
exit 0
')
tsv4="$TMPDIR_TEST/fixture4.tsv"
"$PROFILER" --section-pattern '^# T[0-9]+[a-z]?:' --tsv "$tsv4" "$fixture4" >/dev/null 2>&1
sections4=$(wc -l < "$tsv4" | tr -d ' ')
assert_eq "custom pattern matches T57a/b/c" "$sections4" "3"

# ─── Test 6: No-section-match fixture is graceful ────────────────────────
fixture5=$(make_fixture "fixture_no_sections.sh" '#!/usr/bin/env bash
echo hello
exit 0
')
tsv5="$TMPDIR_TEST/fixture5.tsv"
out5=$("$PROFILER" --tsv "$tsv5" "$fixture5" 2>/dev/null)
exit5=$?
assert_eq "no-section fixture exits 0" "$exit5" "0"
sections5=$(wc -l < "$tsv5" 2>/dev/null | tr -d ' ' || echo 0)
assert_eq "no-section fixture records 0 sections" "$sections5" "0"
assert_contains "no-section fixture reports 0 sections" "$out5" "Sections recorded: 0"

# ─── Test 7: TSV format is exactly two tab-separated columns ─────────────
# Verify the first row of fixture1.tsv has format "<float>\t<label>".
first_row=$(head -1 "$tsv1")
# Count tabs.
tab_count=$(printf '%s' "$first_row" | tr -cd '\t' | wc -c | tr -d ' ')
assert_eq "TSV row has exactly one tab" "$tab_count" "1"
# Elapsed column matches \d+\.\d{3}.
elapsed_col=$(printf '%s' "$first_row" | cut -f1)
if printf '%s' "$elapsed_col" | grep -qE '^[0-9]+\.[0-9]{3}$'; then
    TESTS=$((TESTS + 1))
    echo "PASS: TSV elapsed column is <secs>.<3-digit-ms>"
else
    TESTS=$((TESTS + 1))
    echo "FAIL: TSV elapsed column format unexpected: '$elapsed_col'"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 8: --top N controls number of rows printed ─────────────────────
out6=$("$PROFILER" --tsv "$TMPDIR_TEST/top1.tsv" --top 1 "$fixture1" 2>/dev/null)
# Count rows after "Top 1 longest sections:" header. Stop at "----- end -----".
top_rows=$(printf '%s' "$out6" | awk '/^Top 1 longest sections:/{flag=1; next} /^----- end -----/{flag=0} flag' | wc -l | tr -d ' ')
assert_eq "--top 1 prints 1 row" "$top_rows" "1"

# ─── Test 9: Bash 3.2 compatibility check ────────────────────────────────
TESTS=$((TESTS + 1))
if "$SCRIPT_DIR/check-bash-compat.sh" "$SCRIPT_DIR/.." >/dev/null 2>&1; then
    echo "PASS: profile-shell-test.sh is bash 3.2 compatible"
else
    echo "FAIL: profile-shell-test.sh has bash 4+ constructs"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 10: Error on missing file ──────────────────────────────────────
"$PROFILER" "$TMPDIR_TEST/does_not_exist.sh" >/dev/null 2>&1
missing_rc=$?
assert_eq "missing file errors with rc=2" "$missing_rc" "2"

# ─── Test 11: Error on missing argument ──────────────────────────────────
"$PROFILER" >/dev/null 2>&1
no_arg_rc=$?
assert_eq "no-arg errors with rc=2" "$no_arg_rc" "2"

# ─── Test 12: --help exits 0 ─────────────────────────────────────────────
"$PROFILER" --help >/dev/null 2>&1
help_rc=$?
assert_eq "--help exits 0" "$help_rc" "0"

# ─── Test 13: AC integration — reference test runs at 30+ sections ──────
# Issue #4176 acceptance: profiling test_agent_runner_entrypoint.sh
# produces ≥ 30 TSV rows AND its PASS count is preserved (or better,
# never worse).
#
# This is the slow integration check — it actually runs the full
# 36-section entrypoint test through the profiler. On the long-pole
# shard runner this takes ~3-4 minutes, but the entrypoint test is
# already on the slow shard so we're not making CI any slower than it
# already is. To skip locally during quick iteration, set
# PROFILE_SKIP_AC=1.
ENTRYPOINT_TEST="$TESTS_DIR/test_agent_runner_entrypoint.sh"
if [[ -n "${PROFILE_SKIP_AC:-}" ]]; then
    echo "SKIP: AC integration (PROFILE_SKIP_AC set)"
elif [[ ! -x "$ENTRYPOINT_TEST" ]]; then
    echo "SKIP: AC integration (entrypoint test not found / not executable)"
else
    ac_tsv="$TMPDIR_TEST/entrypoint.tsv"
    # #4383: capture stderr (do NOT discard) so a future flake records
    # the profiler's structured beacon — `profile-shell-test:
    # wrapped_exit=N sections=N tsv=PATH` — alongside the wrapped
    # test's stdout. Without this capture, the only signal on failure
    # is rc=2 with no diagnostic context.
    ac_stderr="$TMPDIR_TEST/entrypoint.err"
    ac_out=$("$PROFILER" --tsv "$ac_tsv" --top 5 "$ENTRYPOINT_TEST" 2>"$ac_stderr")
    ac_rc=$?

    ac_sections=$(wc -l < "$ac_tsv" 2>/dev/null | tr -d ' ' || echo 0)
    assert_ge "entrypoint test profiled to ≥ 30 sections" "$ac_sections" "30"

    # Extract the wrapped test's PASS count from its own output line.
    # Format: "Results: 474/474 passed".
    pass_line=$(printf '%s' "$ac_out" | grep -E '^Results: [0-9]+/[0-9]+ passed' | tail -1)
    if [[ -n "$pass_line" ]]; then
        passed=$(printf '%s' "$pass_line" | awk -F'[ /]' '{print $2}')
        total=$(printf '%s' "$pass_line" | awk -F'[ /]' '{print $3}')
        assert_ge "entrypoint test PASS count ≥ 468 (issue baseline)" "$passed" "468"
        assert_eq "entrypoint test runs all 474 sub-tests" "$total" "474"
        # Profiler must exit with the wrapped test's rc — accept either
        # 0 (all green) or non-zero if a flake hits, but flag the
        # mismatch.
        if [[ "$passed" -eq "$total" ]]; then
            ac_rc_before=$TESTS
            assert_eq "profiler exit matches wrapped test (all green → rc=0)" "$ac_rc" "0"
            # #4383: when the assertion fails, dump the captured stderr
            # and the tail of stdout so the next CI flake is
            # self-diagnosing. Detection: the assert_eq above incremented
            # FAILURES if it failed.
            if [[ "$ac_rc" != "0" ]]; then
                echo "  ── #4383 diagnostic dump ──"
                echo "  profiler stderr (last 50 lines):"
                tail -n 50 "$ac_stderr" 2>/dev/null | sed 's/^/    /'
                echo "  wrapped test stdout (last 50 lines):"
                printf '%s\n' "$ac_out" | tail -n 50 | sed 's/^/    /'
                echo "  ── end #4383 dump ──"
            fi
        fi
    else
        TESTS=$((TESTS + 1))
        echo "FAIL: entrypoint test output did not include 'Results: N/N passed'"
        FAILURES=$((FAILURES + 1))
    fi
fi

# ─── Test T_issue4183: default pattern covers Test N + T_issue<N> + T<N><a-z> ─
# Issue #4183 acceptance: the default --section-pattern must match all three
# header forms in `scripts/tests/`:
#   * legacy `# Test N:`       (T44–T59 grandfathered)
#   * new   `# Test T_issue<N>:`
#   * new   `# Test T<N><a-z>:` (same-issue disambiguation)
# Without this, an agent profiling a newly-authored test sees
# "Sections recorded: 0" and has to re-run with an explicit
# --section-pattern, defeating the "single-tool-invocation surfaces the
# long pole" property the profiler exists for. See #4183 for the bug.
fixture_t4183=$(make_fixture "fixture_t_issue4183.sh" '#!/usr/bin/env bash
set -euo pipefail

# Test 1: legacy sequential
sleep 0.01

# Test T_issue3656: post-#3666 issue-numbered convention
sleep 0.02

# Test T3656a: same-issue disambiguation letter suffix
sleep 0.03

exit 0
')
tsv_t4183="$TMPDIR_TEST/fixture_t4183.tsv"
"$PROFILER" --tsv "$tsv_t4183" "$fixture_t4183" >/dev/null 2>&1
sections_t4183=$(wc -l < "$tsv_t4183" | tr -d ' ')
assert_eq "default pattern catches Test N + T_issue<N> + T<N><a-z>" "$sections_t4183" "3"

# ─── Test T_issue4383: trap rc-no-leak contract under set -e ──────────────
# Issue #4383: when the wrapped test exits with `set -e` in force, the
# chained EXIT trap (`_section_close; cleanup`) runs under `set -e`. Any
# non-zero rc inside `_section_close` (e.g. a flaky `python3 -c` call,
# a sed race) propagates as the script's exit code — overriding the
# wrapped test's `exit 0`. The fix in `scripts/profile-shell-test.sh`
# wraps `_section_close` and `_section_record` with local `set +e` and
# `|| true`'s every interior command so the trap chain CANNOT leak rc.
# This test proves that contract by running a fixture that:
#   1. Sets `set -euo pipefail` (so `set -e` is in force at exit).
#   2. Installs a user `trap user_cleanup EXIT` that the rewriter chains.
#   3. Stubs `python3` on PATH to ALWAYS exit 2 — guaranteeing
#      `_section_now_ms` would propagate rc=2 if `_section_close` ever
#      let it. (The fixture stubs python3 instead of relying on a
#      timing flake; this gives a deterministic regression.)
#   4. Reaches `exit 0`.
# Expectation: the profiler exits 0 (the wrapped test's rc), NOT 2
# (the trap-injected `python3` rc).
fixture_t4383_dir="$TMPDIR_TEST/t4383"
mkdir -p "$fixture_t4383_dir/bin-stub"
# python3 stub — always exits 2.
cat > "$fixture_t4383_dir/bin-stub/python3" <<'PYTHON3_STUB_EOF'
#!/usr/bin/env bash
exit 2
PYTHON3_STUB_EOF
chmod +x "$fixture_t4383_dir/bin-stub/python3"

# The fixture must live under the standard fixture dir AND have a name
# the section pattern matches, otherwise the profiler sees zero sections.
fixture_t4383=$(make_fixture "fixture_t_issue4383.sh" '#!/usr/bin/env bash
# Wrapped test that exits with set -e in force AND a user EXIT trap,
# AND a stubbed python3 that always exits 2. Proves the profiler does
# NOT leak the python3 rc=2 to its own exit code.
set -euo pipefail
user_cleanup() {
    : # no-op cleanup
}
trap user_cleanup EXIT

# Test 1: alpha
true

# Test 2: beta
true

exit 0
')
tsv_t4383="$TMPDIR_TEST/fixture_t4383.tsv"
# Run the profiler with python3 forced to exit 2 via PATH override.
PATH="$fixture_t4383_dir/bin-stub:$PATH" "$PROFILER" --tsv "$tsv_t4383" "$fixture_t4383" >/dev/null 2>&1
rc_t4383=$?
assert_eq "profiler does not leak trap rc=2 when wrapped test exits 0 under set -e" "$rc_t4383" "0"

# ─── Summary ─────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
