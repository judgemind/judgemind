#!/usr/bin/env bash
# test_run_ci_guards.sh — Unit tests for scripts/run-ci-guards.sh (#4332).
#
# Exercises the umbrella's discovery, skip-list, marker-based opt-out, and
# bypass paths against a synthetic scripts/ tree so we can probe behaviour
# without depending on the real ~70-guard run (which would explode the
# scripts-tests shard duration). End-to-end coverage of the real run lives
# in scripts/tests/test_pre_push.sh scenarios 22 and 23.
#
# Why a synthetic tree
# ────────────────────
# The umbrella resolves its repo root from the script's own location
# (``$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)``), so we copy the
# real umbrella into a throwaway scripts/ directory under TMPDIR_TEST and
# seed exactly the guard files we want each scenario to discover. This
# isolates the test from any real check-*.sh / check-*.py guard the
# operator's working tree happens to contain.
#
# Scenarios covered
# ─────────────────
#   1.  Empty scripts/ tree — exits 2 (discovery error)
#   2.  Single passing guard — exits 0, listed as RUN
#   3.  Single failing guard — exits 1, names the failure
#   4.  Built-in skip list (check-issue-author.sh) — exits 0, listed as SKIP
#   5.  Per-file marker (# ci-guards: skip) — exits 0, listed as SKIP
#   6.  .sh/.py companion de-dup — only the .sh runs
#   7.  --list mode prints discovery without running guards
#   8.  Non-executable .py runs anyway (CI-canonical state for #4332 retro)
#   9.  Non-executable .sh is skipped with reason
#  10.  SKIP_CI_GUARDS=1 emits WARNING and exits 0 without running guards
#  11.  Built-in skip list (check-issue-verify-sql.py) — exits 0, listed
#       as SKIP (#4372 regression — script requires --issue/--body-file
#       and exits 2 with usage error when invoked blind).
#
# Run:
#   scripts/tests/test_run_ci_guards.sh
#
# Exits non-zero if any scenario fails.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UMBRELLA_REAL="$REPO_ROOT/scripts/run-ci-guards.sh"

if [ ! -x "$UMBRELLA_REAL" ]; then
    echo "FAIL: real umbrella not found or not executable at $UMBRELLA_REAL" >&2
    exit 1
fi

TMPDIR_TEST="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_TEST"' EXIT

pass=0
fail=0

report_pass() {
    pass=$((pass + 1))
    echo "  PASS: $1"
}

report_fail() {
    fail=$((fail + 1))
    echo "  FAIL: $1" >&2
    if [ -n "${2-}" ]; then
        echo "--- output ---" >&2
        echo "$2" >&2
        echo "--- end ---" >&2
    fi
}

# Build a synthetic repo with a copy of the real umbrella.
# Returns the synthetic scripts/ directory path on stdout.
seed_synthetic_scripts() {
    local synth_id="$1"
    local synth_root="$TMPDIR_TEST/synth-$synth_id"
    rm -rf "$synth_root"
    mkdir -p "$synth_root/scripts"
    cp "$UMBRELLA_REAL" "$synth_root/scripts/run-ci-guards.sh"
    chmod +x "$synth_root/scripts/run-ci-guards.sh"
    echo "$synth_root/scripts"
}

# Run the synthetic umbrella, capture stdout+stderr and exit code.
# Usage: run_synth <synth-scripts-dir> [args...]
run_synth() {
    local synth_scripts="$1"
    shift
    out_buf="$("$synth_scripts/run-ci-guards.sh" "$@" 2>&1)" \
        && rc_buf=0 || rc_buf=$?
}

# ───────────────────────────────────────────────────────────────────────
# Scenario 1: empty scripts/ tree — exits 2 (discovery error)
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 1] empty scripts/ tree — exits 2 (discovery error)"
synth_scripts="$(seed_synthetic_scripts s1)"
# Don't seed any check-* files.
run_synth "$synth_scripts"
if [ "$rc_buf" -ne 2 ]; then
    report_fail "expected exit 2 on empty tree, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "no scripts/check-"; then
    report_fail "expected 'no scripts/check-*' error message" "$out_buf"
else
    report_pass "empty scripts/ tree exits 2 with discovery error"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 2: single passing guard — exits 0
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 2] single passing guard — exits 0"
synth_scripts="$(seed_synthetic_scripts s2)"
cat > "$synth_scripts/check-pass.sh" <<'SH'
#!/usr/bin/env bash
echo "passing guard"
exit 0
SH
chmod +x "$synth_scripts/check-pass.sh"
run_synth "$synth_scripts"
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected exit 0 with single passing guard, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "all 1 guard(s) passed"; then
    report_fail "expected 'all 1 guard(s) passed' summary" "$out_buf"
else
    report_pass "single passing guard exits 0"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 3: single failing guard — exits 1, names failure
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 3] single failing guard — exits 1, names failure"
synth_scripts="$(seed_synthetic_scripts s3)"
cat > "$synth_scripts/check-fail.sh" <<'SH'
#!/usr/bin/env bash
echo "deliberate failure"
exit 1
SH
chmod +x "$synth_scripts/check-fail.sh"
run_synth "$synth_scripts"
if [ "$rc_buf" -ne 1 ]; then
    report_fail "expected exit 1 with single failing guard, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "FAILED: check-fail.sh"; then
    report_fail "expected 'FAILED: check-fail.sh' in output" "$out_buf"
elif ! echo "$out_buf" | grep -q "deliberate failure"; then
    report_fail "expected guard's stderr to surface in output" "$out_buf"
else
    report_pass "single failing guard exits 1 and names the failure"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 4: built-in skip list — check-issue-author.sh skipped
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 4] built-in skip list — check-issue-author.sh skipped"
synth_scripts="$(seed_synthetic_scripts s4)"
# Seed both a passing guard and an issue-author.sh stub. The latter would
# fail if invoked (it requires an issue number), but the built-in skip
# list should keep it from running.
cat > "$synth_scripts/check-pass.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$synth_scripts/check-pass.sh"
cat > "$synth_scripts/check-issue-author.sh" <<'SH'
#!/usr/bin/env bash
echo "this should never fire — built-in skip list violated"
exit 1
SH
chmod +x "$synth_scripts/check-issue-author.sh"
run_synth "$synth_scripts" --list
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected --list to exit 0, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "SKIP check-issue-author.sh (built-in skip)"; then
    report_fail "expected 'SKIP check-issue-author.sh (built-in skip)'" "$out_buf"
else
    report_pass "built-in skip list excludes check-issue-author.sh"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 5: per-file marker (# ci-guards: skip) — guard skipped
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 5] per-file marker — guard skipped"
synth_scripts="$(seed_synthetic_scripts s5)"
# Marker variant — should be skipped despite being a normal check-*.sh.
cat > "$synth_scripts/check-marker.sh" <<'SH'
#!/usr/bin/env bash
# ci-guards: skip
echo "marker guard ran — bug"
exit 1
SH
chmod +x "$synth_scripts/check-marker.sh"
run_synth "$synth_scripts" --list
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected --list to exit 0, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "SKIP check-marker.sh (# ci-guards: skip)"; then
    report_fail "expected 'SKIP check-marker.sh (# ci-guards: skip)'" "$out_buf"
else
    report_pass "per-file marker excludes check-marker.sh"
fi
# And verify the marker actually prevents execution: a full run should pass.
run_synth "$synth_scripts"
if [ "$rc_buf" -ne 0 ]; then
    report_fail "marker guard ran despite skip marker (rc=$rc_buf)" "$out_buf"
else
    report_pass "marker guard not invoked during full run"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 6: .sh/.py companion de-dup — only .sh runs
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 6] .sh/.py companion de-dup — only .sh runs"
synth_scripts="$(seed_synthetic_scripts s6)"
# Both files exist; .py would fail if invoked, .sh passes.
cat > "$synth_scripts/check-companion.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$synth_scripts/check-companion.sh"
cat > "$synth_scripts/check-companion.py" <<'PY'
import sys
print("py companion ran — bug", file=sys.stderr)
sys.exit(1)
PY
# Intentionally do NOT chmod +x the .py — companion de-dup must work
# regardless of executability.
run_synth "$synth_scripts" --list
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected --list to exit 0, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "RUN  check-companion.sh"; then
    report_fail "expected 'RUN check-companion.sh' in --list output" "$out_buf"
elif ! echo "$out_buf" | grep -q "SKIP check-companion.py (.sh wrapper companion: check-companion.sh)"; then
    report_fail "expected '.sh wrapper companion' skip line" "$out_buf"
else
    report_pass ".sh/.py companion de-dup runs only the .sh"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 7: --list mode prints without running
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 7] --list mode prints discovery without running guards"
synth_scripts="$(seed_synthetic_scripts s7)"
cat > "$synth_scripts/check-shouldnotrun.sh" <<'SH'
#!/usr/bin/env bash
echo "RAN" > "$0.touched"
exit 0
SH
chmod +x "$synth_scripts/check-shouldnotrun.sh"
run_synth "$synth_scripts" --list
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected --list to exit 0, got $rc_buf" "$out_buf"
elif [ -f "$synth_scripts/check-shouldnotrun.sh.touched" ]; then
    report_fail "--list mode invoked the guard (touched sentinel file present)" "$out_buf"
elif ! echo "$out_buf" | grep -q "RUN  check-shouldnotrun.sh"; then
    report_fail "expected 'RUN check-shouldnotrun.sh' in --list output" "$out_buf"
else
    report_pass "--list mode prints discovery without running guards"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 8: non-executable .py runs anyway via python3 (CI-canonical
#              state — see check-sql-columns.py at PR #4325 retro time)
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 8] non-executable .py runs anyway via python3 (#4332)"
synth_scripts="$(seed_synthetic_scripts s8)"
cat > "$synth_scripts/check-py-noexec.py" <<'PY'
import sys
print("py guard ran")
sys.exit(0)
PY
# Intentionally leave -rw-r--r--.
run_synth "$synth_scripts"
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected exit 0 with non-executable .py guard, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "all 1 guard(s) passed"; then
    report_fail "expected 'all 1 guard(s) passed' (non-exec .py should run)" "$out_buf"
else
    report_pass "non-executable .py guard runs via python3 (#4332)"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 9: non-executable .sh is skipped with reason
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 9] non-executable .sh is skipped with reason"
synth_scripts="$(seed_synthetic_scripts s9)"
cat > "$synth_scripts/check-sh-noexec.sh" <<'SH'
#!/usr/bin/env bash
exit 1
SH
# Intentionally do NOT chmod +x.
run_synth "$synth_scripts" --list
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected --list to exit 0, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "SKIP check-sh-noexec.sh (not executable)"; then
    report_fail "expected 'SKIP check-sh-noexec.sh (not executable)' in --list" "$out_buf"
else
    report_pass "non-executable .sh is skipped with reason"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 10: SKIP_CI_GUARDS=1 emits WARNING and exits 0
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 10] SKIP_CI_GUARDS=1 emits WARNING and exits 0"
synth_scripts="$(seed_synthetic_scripts s10)"
# Seed a guard that would fail if invoked — bypass must short-circuit.
cat > "$synth_scripts/check-fail.sh" <<'SH'
#!/usr/bin/env bash
echo "this must never run"
exit 1
SH
chmod +x "$synth_scripts/check-fail.sh"
out_buf="$(SKIP_CI_GUARDS=1 "$synth_scripts/run-ci-guards.sh" 2>&1)" \
    && rc_buf=0 || rc_buf=$?
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected SKIP_CI_GUARDS=1 to exit 0, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "SKIP_CI_GUARDS=1"; then
    report_fail "expected 'SKIP_CI_GUARDS=1' WARNING in output" "$out_buf"
elif ! echo "$out_buf" | grep -q "bypassing scripts/run-ci-guards.sh"; then
    report_fail "expected 'bypassing scripts/run-ci-guards.sh' WARNING" "$out_buf"
elif echo "$out_buf" | grep -q "this must never run"; then
    report_fail "SKIP_CI_GUARDS=1 invoked the guard despite bypass" "$out_buf"
else
    report_pass "SKIP_CI_GUARDS=1 bypasses with WARNING and exits 0"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 11: built-in skip list — check-issue-verify-sql.py skipped
#               (#4372 regression — script requires --issue or --body-file
#               and exits 2 with usage error when invoked blind, which
#               masked real failures in every run-ci-guards.sh run since
#               check-issue-verify-sql.py shipped in #4358).
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 11] built-in skip list — check-issue-verify-sql.py skipped (#4372)"
synth_scripts="$(seed_synthetic_scripts s11)"
# Seed a passing guard plus a check-issue-verify-sql.py stub that mimics
# the real script's argparse failure shape. The built-in skip list must
# keep it from running — if it ran, it would exit 2 and surface as a
# failed guard.
cat > "$synth_scripts/check-pass.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$synth_scripts/check-pass.sh"
cat > "$synth_scripts/check-issue-verify-sql.py" <<'PY'
import sys
print(
    "usage: check-issue-verify-sql.py [-h] (--issue ISSUE | --body-file BODY_FILE)",
    file=sys.stderr,
)
print(
    "check-issue-verify-sql.py: error: one of the arguments --issue --body-file is required",
    file=sys.stderr,
)
sys.exit(2)
PY
# Intentionally do NOT chmod +x — .py guards are invoked via python3 by
# the umbrella, so the +x bit is irrelevant. The skip-list match must
# fire before the umbrella attempts to execute the file at all.
run_synth "$synth_scripts" --list
if [ "$rc_buf" -ne 0 ]; then
    report_fail "expected --list to exit 0, got $rc_buf" "$out_buf"
elif ! echo "$out_buf" | grep -q "SKIP check-issue-verify-sql.py (built-in skip)"; then
    report_fail "expected 'SKIP check-issue-verify-sql.py (built-in skip)' (#4372)" "$out_buf"
else
    report_pass "built-in skip list excludes check-issue-verify-sql.py (#4372)"
fi
# And verify the skip actually prevents execution: a full run should pass.
run_synth "$synth_scripts"
if [ "$rc_buf" -ne 0 ]; then
    report_fail "check-issue-verify-sql.py ran despite skip-list (rc=$rc_buf, #4372)" "$out_buf"
else
    report_pass "check-issue-verify-sql.py not invoked during full run (#4372)"
fi

# ───────────────────────────────────────────────────────────────────────
# Summary
# ───────────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "test_run_ci_guards.sh: $pass passed, $fail failed"
echo "────────────────────────────────────────────────────────────────"

if [ "$fail" -gt 0 ]; then
    exit 1
fi
exit 0
