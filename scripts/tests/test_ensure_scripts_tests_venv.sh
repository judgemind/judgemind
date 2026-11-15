#!/usr/bin/env bash
# test_ensure_scripts_tests_venv.sh — Tests for
# scripts/ensure-scripts-tests-venv.sh (#5045).
#
# The helper runs from a copy inside a scratch "checkout" (so its venv
# and requirements file are scratch files), with a stub interpreter
# (SCRIPTS_TESTS_PYTHON) that fakes `-m venv` and `-m pip install` and
# logs each call. No network, no real venv.
#
#   1. first run builds the venv: `-m venv`, then pip install -r
#      <checkout>/scripts/tests/requirements-ci.txt from the checkout
#      root; prints the venv path on stdout only
#   2. second run reuses it: no venv / pip call
#   3. an edited requirements file rebuilds it
#   4. an edited packages/judgemind-config/pyproject.toml rebuilds it
#   5. an interrupted build (no fingerprint) rebuilds it
#   6. pip failure -> exit 1, FAILED + Fix: block, venv removed, and the
#      next run rebuilds
#   7. interpreter older than 3.12 -> exit 3, WARNING + Fix: block
#   8. a dangerous SCRIPTS_TESTS_VENV (the checkout root) is refused
#
# Usage:
#   scripts/tests/test_ensure_scripts_tests_venv.sh

set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ensure-scripts-tests-venv.sh"

pass=0
fail=0
report_pass() { pass=$((pass + 1)); echo "  PASS: $1"; }
report_fail() {
    fail=$((fail + 1))
    echo "  FAIL: $1" >&2
    if [ -n "${2-}" ]; then
        echo "--- output ---" >&2
        echo "$2" >&2
        echo "--- end ---" >&2
    fi
}

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

CHECKOUT="$T/checkout"
mkdir -p "$CHECKOUT/scripts/tests" "$CHECKOUT/packages/judgemind-config"
cp "$SCRIPT" "$CHECKOUT/scripts/ensure-scripts-tests-venv.sh"
chmod +x "$CHECKOUT/scripts/ensure-scripts-tests-venv.sh"
printf 'pytest\nboto3\n' > "$CHECKOUT/scripts/tests/requirements-ci.txt"
printf '[project]\nname = "judgemind-config"\n' > "$CHECKOUT/packages/judgemind-config/pyproject.toml"
HELPER="$CHECKOUT/scripts/ensure-scripts-tests-venv.sh"
VENV="$CHECKOUT/tmp/scripts-tests-venv"
CHECKOUT_ABS="$(cd "$CHECKOUT" && pwd)"

export STUB_LOG="$T/stub-calls.log"
STUB_PY="$T/stub-python"
cat > "$STUB_PY" <<'PY'
#!/usr/bin/env bash
# Stub interpreter for the helper's three call shapes.
case "$1" in
    -c)
        case "$2" in
            *print*) echo "3.12.99" ;;
            *) [ "${STUB_PY_TOO_OLD:-0}" = "1" ] && exit 1 ;;
        esac
        exit 0
        ;;
    -m)
        case "$2" in
            venv)
                echo "venv $3" >> "$STUB_LOG"
                mkdir -p "$3/bin"
                cp "$0" "$3/bin/python"
                exit 0
                ;;
            pip)
                echo "pip cwd=$PWD args=${*:3}" >> "$STUB_LOG"
                if [ "${STUB_PIP_FAIL:-0}" = "1" ]; then
                    echo "ERROR: Could not find a version that satisfies the requirement pytest" >&2
                    exit 1
                fi
                printf '#!/usr/bin/env bash\nexit 0\n' > "$(dirname "$0")/pytest"
                chmod +x "$(dirname "$0")/pytest"
                exit 0
                ;;
        esac
        ;;
esac
echo "stub-python: unexpected call: $*" >&2
exit 99
PY
chmod +x "$STUB_PY"
export SCRIPTS_TESTS_PYTHON="$STUB_PY"

# run_helper: sets $out (stdout), $err (stderr), $rc.
run_helper() {
    : > "$STUB_LOG"
    out="$("$HELPER" 2>"$T/err")" && rc=0 || rc=$?
    err="$(cat "$T/err")"
}

echo "[1] first run builds the venv from the shared requirements file"
run_helper
calls="$(cat "$STUB_LOG")"
if [ "$rc" -ne 0 ]; then
    report_fail "expected exit 0, got $rc" "$err"
elif [ "$out" != "$VENV" ]; then
    report_fail "expected stdout to be exactly the venv path; got '$out'" "$err"
elif ! echo "$calls" | grep -q "^venv $VENV$"; then
    report_fail "expected a '-m venv $VENV' call" "$calls"
elif ! echo "$calls" | grep -q "^pip cwd=$CHECKOUT_ABS args=install .*-r $CHECKOUT_ABS/scripts/tests/requirements-ci.txt"; then
    report_fail "expected pip install -r <checkout>/scripts/tests/requirements-ci.txt from the checkout root" "$calls"
elif [ ! -s "$VENV/.scripts-tests-fingerprint" ]; then
    report_fail "expected a fingerprint file after a good build" "$err"
else
    report_pass "first run builds from scripts/tests/requirements-ci.txt"
fi

echo "[2] second run reuses the venv"
run_helper
calls="$(cat "$STUB_LOG")"
if [ "$rc" -ne 0 ] || [ "$out" != "$VENV" ]; then
    report_fail "expected exit 0 and the venv path; got rc=$rc out='$out'" "$err"
elif [ -n "$calls" ]; then
    report_fail "expected no venv/pip call on reuse" "$calls"
elif ! echo "$err" | grep -q "reusing $VENV"; then
    report_fail "expected a 'reusing' message on stderr" "$err"
else
    report_pass "unchanged inputs reuse the venv"
fi

echo "[3] an edited requirements file rebuilds"
echo "diff-cover" >> "$CHECKOUT/scripts/tests/requirements-ci.txt"
run_helper
if [ "$rc" -ne 0 ] || ! grep -q "^pip " "$STUB_LOG"; then
    report_fail "expected a rebuild after editing requirements-ci.txt (rc=$rc)" "$err"
else
    report_pass "requirements change rebuilds"
fi

echo "[4] an edited judgemind-config pyproject.toml rebuilds"
printf 'version = "0.0.2"\n' >> "$CHECKOUT/packages/judgemind-config/pyproject.toml"
run_helper
if [ "$rc" -ne 0 ] || ! grep -q "^pip " "$STUB_LOG"; then
    report_fail "expected a rebuild after editing judgemind-config/pyproject.toml (rc=$rc)" "$err"
else
    report_pass "judgemind-config change rebuilds"
fi

echo "[5] an interrupted build (no fingerprint) rebuilds"
rm -f "$VENV/.scripts-tests-fingerprint"
run_helper
if [ "$rc" -ne 0 ] || ! grep -q "^venv " "$STUB_LOG"; then
    report_fail "expected a rebuild when the fingerprint is missing (rc=$rc)" "$err"
else
    report_pass "missing fingerprint rebuilds"
fi

echo "[6] pip failure: exit 1, FAILED + Fix:, venv removed, next run rebuilds"
echo "# touch" >> "$CHECKOUT/scripts/tests/requirements-ci.txt"
: > "$STUB_LOG"
out="$(STUB_PIP_FAIL=1 "$HELPER" 2>"$T/err")" && rc=0 || rc=$?
err="$(cat "$T/err")"
if [ "$rc" -ne 1 ]; then
    report_fail "expected exit 1 on pip failure, got $rc" "$err"
elif [ -n "$out" ]; then
    report_fail "expected nothing on stdout on failure; got '$out'" "$err"
elif ! echo "$err" | grep -q "FAILED: could not build the scripts/tests venv"; then
    report_fail "expected a FAILED message" "$err"
elif ! echo "$err" | grep -q "Could not find a version that satisfies"; then
    report_fail "expected pip's own error in the output" "$err"
elif ! echo "$err" | grep -q "^Fix:"; then
    report_fail "expected a Fix: block" "$err"
elif ! echo "$err" | grep -q "PREPUSH_SCRIPTS_TESTS=0 git push"; then
    report_fail "expected the bypass in the Fix: block" "$err"
elif [ -e "$VENV" ]; then
    report_fail "expected the half-built venv to be removed" "$err"
else
    run_helper
    if [ "$rc" -ne 0 ] || ! grep -q "^venv " "$STUB_LOG"; then
        report_fail "expected the next run to rebuild (rc=$rc)" "$err"
    else
        report_pass "pip failure is loud, leaves no venv, and recovers"
    fi
fi

echo "[7] an interpreter older than 3.12: exit 3, WARNING + Fix:"
: > "$STUB_LOG"
out="$(STUB_PY_TOO_OLD=1 "$HELPER" 2>"$T/err")" && rc=0 || rc=$?
err="$(cat "$T/err")"
if [ "$rc" -ne 3 ]; then
    report_fail "expected exit 3, got $rc" "$err"
elif ! echo "$err" | grep -q "WARNING: no Python 3.12+ interpreter"; then
    report_fail "expected the WARNING" "$err"
elif ! echo "$err" | grep -q "^Fix:"; then
    report_fail "expected a Fix: block" "$err"
elif [ -s "$STUB_LOG" ]; then
    report_fail "expected no venv/pip call" "$(cat "$STUB_LOG")"
else
    report_pass "no usable interpreter degrades with exit 3"
fi

echo "[8] the checkout root as SCRIPTS_TESTS_VENV is refused"
out="$(SCRIPTS_TESTS_VENV="$CHECKOUT_ABS" "$HELPER" 2>"$T/err")" && rc=0 || rc=$?
err="$(cat "$T/err")"
if [ "$rc" -ne 2 ] || [ ! -f "$CHECKOUT/scripts/ensure-scripts-tests-venv.sh" ]; then
    report_fail "expected exit 2 and the checkout untouched; got rc=$rc" "$err"
else
    report_pass "dangerous venv path refused"
fi

echo ""
echo "ensure-scripts-tests-venv tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
