#!/usr/bin/env bash
# test_profile_shell_test.sh — Tests for scripts/profile-shell-test.sh
#
# Builds small fixtures with known section layouts and asserts:
#   * Section markers are detected and timed.
#   * Exit code from the wrapped test is preserved verbatim.
#   * The TSV output has the expected shape (sections × 2 columns).
#   * Top-N summary appears in stdout in sorted order (asserted under a
#     fake python3 clock, never on real sleep timing — #4817).
#   * A 50k-row TSV does not SIGPIPE the summary pipeline (#4817).
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
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

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
# Source the shared assert harness extracted in #4540. Provides pass /
# fail / assert_eq / assert_ge / assert_contains plus the optional
# ``--err-file FILE`` flag that dumps FILE's last 50 lines on
# assertion mismatch. Replaces three inline assert helper definitions
# that previously lived here (pre-#4540).
. "$SCRIPT_DIR/tests/_test-helpers.sh"

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

# ─── Fake clock (#4817) ──────────────────────────────────────────────────
# The profiler reads the time via `python3 -c '...time.time()...'` in
# `_section_now_ms`. Ordering assertions used to depend on real `sleep`
# gaps (0.05 / 0.20 / 0.50 after #4188), but the measured elapsed time
# also includes the python3 start-up in the NEXT section boundary. On a
# loaded CI runner that start-up alone can exceed the 0.3s gap, so the
# "sorted by elapsed desc" check flaked (#4817; PR #4992 CI run
# 43662822823 attempt 1: "expected Test 3: gamma, actual Test 2: beta").
# Widening the sleeps again only moves the threshold.
#
# Instead, put a stub `python3` first on PATH that replays a fixed list
# of epoch-ms values, one per call. Each section's elapsed time is then
# exactly known, so ordering and arithmetic are asserted on content and
# never on scheduling. The values are set in an order that does NOT match
# file order, so the assertions prove the summary really sorts.
FAKE_CLOCK_BIN="$TMPDIR_TEST/fake-clock-bin"
mkdir -p "$FAKE_CLOCK_BIN"
cat > "$FAKE_CLOCK_BIN/python3" <<'FAKE_CLOCK_EOF'
#!/usr/bin/env bash
# Stand-in for _section_now_ms's python3 call: print the next queued
# epoch-ms value from $FAKE_CLOCK_FILE and drop it from the queue.
f="${FAKE_CLOCK_FILE:?FAKE_CLOCK_FILE not set}"
[ -s "$f" ] || exit 0
head -n 1 "$f"
tail -n +2 "$f" > "$f.next"
mv "$f.next" "$f"
FAKE_CLOCK_EOF
chmod +x "$FAKE_CLOCK_BIN/python3"

# fake_clock_set FILE MS... — queue the epoch-ms values the stub returns.
fake_clock_set() {
    local f="$1"
    shift
    printf '%s\n' "$@" > "$f"
}

# summary_rows OUTPUT — print the rows between the "Top N longest
# sections:" header and the "----- end -----" footer.
summary_rows() {
    printf '%s\n' "$1" | awk '/^Top [0-9]+ longest sections:/{flag=1; next} /^----- end -----/{flag=0} flag'
}

# ─── Test 1: Three-section fixture; preserves exit code 0 ────────────────
# Runs under the fake clock. The four python3 calls are: open alpha,
# close alpha / open beta, close beta / open gamma, close gamma (EXIT
# trap). Queued values give alpha=0.200s, beta=0.500s, gamma=0.050s.
fixture1=$(make_fixture "fixture_basic.sh" '#!/usr/bin/env bash
set -euo pipefail

# Test 1: alpha
true

# Test 2: beta
true

# Test 3: gamma
true
exit 0
')
tsv1="$TMPDIR_TEST/fixture1.tsv"
clock1="$TMPDIR_TEST/fixture1.clock"
fake_clock_set "$clock1" 1000000 1000200 1000700 1000750
# #4528 / #4540: capture stderr (do NOT discard) so a future flake
# records the profiler's diagnostic output alongside the wrapped
# fixture's stdout. Without this capture the only signal on rc
# mismatch is "expected 0, actual N" with no context — exactly the
# #4383 failure mode this issue is generalising. The dump fires only
# on the rc assertion failing, via the shared helper's --err-file.
err1="$TMPDIR_TEST/fixture1.err"
out1=$(PATH="$FAKE_CLOCK_BIN:$PATH" FAKE_CLOCK_FILE="$clock1" "$PROFILER" --tsv "$tsv1" --top 5 "$fixture1" 2>"$err1")
exit1=$?
assert_eq "basic fixture exits 0" "$exit1" "0" --err-file "$err1"
sections1=$(wc -l < "$tsv1" | tr -d ' ')
assert_eq "basic fixture records 3 sections" "$sections1" "3"
assert_contains "basic fixture summary names section 3" "$out1" "Test 3: gamma"
assert_contains "basic fixture summary names section 1" "$out1" "Test 1: alpha"
assert_contains "basic fixture summary header present" "$out1" "Top 5 longest sections:"
# The TSV is in file order with the exact elapsed times from the clock.
expected_tsv1=$(printf '0.200\tTest 1: alpha\n0.500\tTest 2: beta\n0.050\tTest 3: gamma')
assert_eq "TSV elapsed values match the fake clock exactly" "$(cat "$tsv1")" "$expected_tsv1"

# ─── Test 2: The profiler's summary is sorted by elapsed desc ────────────
# Asserts on the profiler's own stdout (not a re-sort of the TSV), and on
# content fixed by the fake clock, so it cannot flake on CI load (#4817).
expected_summary1=$(printf '0.500\tTest 2: beta\n0.200\tTest 1: alpha\n0.050\tTest 3: gamma')
assert_eq "summary is sorted by elapsed desc" "$(summary_rows "$out1")" "$expected_summary1"

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
# #4528 / #4540: capture stderr (see same pattern at fixture1 above).
# Dump-on-mismatch handled by the shared helper's --err-file flag.
err5="$TMPDIR_TEST/fixture5.err"
out5=$("$PROFILER" --tsv "$tsv5" "$fixture5" 2>"$err5")
exit5=$?
assert_eq "no-section fixture exits 0" "$exit5" "0" --err-file "$err5"
sections5=$(wc -l < "$tsv5" 2>/dev/null | tr -d ' ' || echo 0)
assert_eq "no-section fixture records 0 sections" "$sections5" "0"
assert_contains "no-section fixture reports 0 sections" "$out5" "Sections recorded: 0"

# ─── Test 7: TSV format is exactly two tab-separated columns ─────────────
# Verify the first row of fixture3.tsv has format "<float>\t<label>".
# fixture3 ran with the real clock (fixture1 uses the fake one), so this
# also checks the real python3 timing path end to end.
first_row=$(head -1 "$tsv3")
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
# #4528 / #4540: capture stderr — Test 8 doesn't capture rc directly,
# but its `top_rows` derived value is computed from out6's stdout. If
# the profiler dies silently, top_rows is 0 and the assertion fires
# with "expected 1, actual 0" and no context. Same flake-hider class
# as the fixture1/fixture5 sites above. Dump-on-mismatch handled by
# the shared helper's --err-file flag.
err6="$TMPDIR_TEST/top1.err"
clock6="$TMPDIR_TEST/top1.clock"
fake_clock_set "$clock6" 1000000 1000200 1000700 1000750
out6=$(PATH="$FAKE_CLOCK_BIN:$PATH" FAKE_CLOCK_FILE="$clock6" "$PROFILER" --tsv "$TMPDIR_TEST/top1.tsv" --top 1 "$fixture1" 2>"$err6")
exit6=$?
assert_eq "--top 1 run exits 0" "$exit6" "0" --err-file "$err6"
# Count rows after "Top 1 longest sections:" header. Stop at "----- end -----".
top_rows=$(summary_rows "$out6" | wc -l | tr -d ' ')
assert_eq "--top 1 prints 1 row" "$top_rows" "1" --err-file "$err6"
assert_eq "--top 1 row is the longest section" "$(summary_rows "$out6")" "$(printf '0.500\tTest 2: beta')"

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

# ─── Test T_issue4528: stderr-dump-on-assertion-failure contract ───────────
# Issue #4528: The fixture1 / fixture5 / Test 8 sites in this file used
# to discard the profiler's stderr to /dev/null. When a CI-only flake
# fired (the canonical example was the rc=2 leak fixed in #4383), the
# only signal was "expected: 0 / actual: 2" with no diagnostic context.
# This PR captures stderr to a file and dumps it on assertion failure.
# This regression test proves the dump fires by stubbing the profiler
# with a wrapper that writes a known marker to stderr and exits non-
# zero, then runs *only* the fixture1 block from this file under that
# stub PATH. Expectation: the captured shell-test output contains the
# stderr marker, proving the dump path executed.
fixture_t4528_dir="$TMPDIR_TEST/t4528"
mkdir -p "$fixture_t4528_dir/bin-stub"
# Stub profile-shell-test.sh that always writes a known stderr marker
# and exits 7 (a value that won't collide with any real profiler rc).
cat > "$fixture_t4528_dir/bin-stub/profile-shell-test.sh" <<'PROFILER_STUB_EOF'
#!/usr/bin/env bash
echo "T_issue4528_STDERR_MARKER: deliberate stub failure" >&2
exit 7
PROFILER_STUB_EOF
chmod +x "$fixture_t4528_dir/bin-stub/profile-shell-test.sh"

# Inline mini-test that mirrors the fixture1 capture-and-dump pattern.
# Runs in a subshell so its FAILURES are captured into a file rather
# than leaked into the parent test's tally.
t4528_out="$TMPDIR_TEST/t4528_capture.txt"
(
    set +e
    PROFILER="$fixture_t4528_dir/bin-stub/profile-shell-test.sh"
    err1="$TMPDIR_TEST/t4528_fixture1.err"
    # Note: do NOT use `|| true` here — it would mask the rc and make
    # exit1 always 0, defeating the regression test. The mirror in
    # fixture1/fixture5 above also does not use `|| true`; the parent
    # shell's `set -uo pipefail` (no `-e`) lets the assignment proceed
    # even when the command exits non-zero.
    out1=$("$PROFILER" --tsv /dev/null --top 5 /dev/null 2>"$err1")
    exit1=$?
    if [[ "$exit1" != "0" ]]; then
        echo "  ── #4528 stderr dump (basic fixture) ──"
        echo "  profiler stderr (last 50 lines):"
        tail -n 50 "$err1" 2>/dev/null | sed 's/^/    /'
        echo "  fixture stdout (last 50 lines):"
        printf '%s\n' "$out1" | tail -n 50 | sed 's/^/    /'
        echo "  ── end #4528 dump ──"
    fi
) > "$t4528_out" 2>&1

t4528_captured=$(cat "$t4528_out")
assert_contains "stderr dump fires on assertion failure (#4528)" \
    "$t4528_captured" "T_issue4528_STDERR_MARKER"
assert_contains "stderr dump emits #4528 dump banner" \
    "$t4528_captured" "── #4528 stderr dump (basic fixture) ──"

# Source-level grep regression: confirm the three patched sites still
# carry the err* capture file + the dump block. If a future refactor
# accidentally reverts to `2>/dev/null`, this fires immediately.
self_path="${BASH_SOURCE[0]}"
self_body=$(cat "$self_path")
assert_contains "fixture1 site captures stderr to err1 (#4528)" \
    "$self_body" 'err1="$TMPDIR_TEST/fixture1.err"'
assert_contains "fixture5 site captures stderr to err5 (#4528)" \
    "$self_body" 'err5="$TMPDIR_TEST/fixture5.err"'
assert_contains "Test 8 site captures stderr to err6 (#4528)" \
    "$self_body" 'err6="$TMPDIR_TEST/top1.err"'

# ─── Test T_issue4817: large TSV does not SIGPIPE the summary ─────────────
# Issue #4817: the summary used to run `sort ... | head -n TOP_N` under
# `set -euo pipefail`. When the TSV is bigger than one pipe/stdio buffer,
# head exits after TOP_N lines, sort dies of SIGPIPE (rc 141) or EPIPE
# ("sort: fflush failed: 'standard output': Broken pipe", rc 2), and the
# profiler exits with that rc even though the wrapped test passed. In CI
# it raced on the ~55-row entrypoint TSV (PR #4816 run 36301672616).
# A 50k-row TSV (~1.5 MB) overflows every pipe buffer, which makes the
# old pipeline fail on every run (20/20 locally before the fix), so this
# is a deterministic regression test. The fixture appends the rows
# straight to the injected $_PROFILE_TSV.
fixture_t4817=$(make_fixture "fixture_t_issue4817.sh" '#!/usr/bin/env bash
set -euo pipefail

# Test 1: bulk
awk '"'"'BEGIN { for (i = 1; i <= 50000; i++) printf "%d.%03d\tTest T%d: bulk row\n", i / 1000, i % 1000, i }'"'"' >> "$_PROFILE_TSV"
exit 0
')
tsv_t4817="$TMPDIR_TEST/fixture_t4817.tsv"
err_t4817="$TMPDIR_TEST/fixture_t4817.err"
out_t4817=$("$PROFILER" --tsv "$tsv_t4817" --top 5 "$fixture_t4817" 2>"$err_t4817")
rc_t4817=$?
assert_eq "50k-row TSV: all-green wrapped test gives rc=0 (#4817)" "$rc_t4817" "0" --err-file "$err_t4817"
sections_t4817=$(wc -l < "$tsv_t4817" | tr -d ' ')
assert_eq "50k-row TSV: 50000 bulk rows + the real section" "$sections_t4817" "50001"
rows_t4817=$(summary_rows "$out_t4817")
assert_eq "50k-row TSV: summary prints exactly --top 5 rows" "$(printf '%s\n' "$rows_t4817" | wc -l | tr -d ' ')" "5"
assert_eq "50k-row TSV: summary leads with the largest row" "$(printf '%s\n' "$rows_t4817" | awk 'NR == 1')" "$(printf '50.000\tTest T50000: bulk row')"

# ─── Summary ─────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
