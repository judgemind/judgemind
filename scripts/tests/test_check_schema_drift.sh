#!/usr/bin/env bash
# test_check_schema_drift.sh — Tests for the postgres-readiness wait loop
# in scripts/check_schema_drift.sh (#4159).
#
# Covers two acceptance criteria from #4159:
#   1. The wait-loop ceiling is 90s, not 30s, and the error message matches.
#   2. The loop short-circuits and exits in <10s when the postgres container
#      is broken (e.g. exited because of a bad env var, OOM-killed), instead
#      of burning the full 90s polling a dead container.
#
# Strategy:
#   - The script is bash, so we test it by mocking `docker` on PATH and
#     invoking `scripts/check_schema_drift.sh --ci` in a controlled env.
#   - We don't exercise the schema-diff path — we only need to drive the
#     wait loop and observe its behavior on (a) a healthy mocked postgres
#     and (b) a "container exited" mock.
#
# Usage:
#   scripts/tests/test_check_schema_drift.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$SCRIPT_DIR/check_schema_drift.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

# Cleanup of temp directories + PATH restore via the shared helper
# (see #4343).
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
ORIG_PATH_SAVE=""
restore_path() {
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
    fi
}
register_cleanup_hook restore_path

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

# ── Precondition: target script exists and is executable ──────────────────

if [[ ! -x "$TARGET" ]]; then
    echo "FAIL: $TARGET is not executable (or does not exist)" >&2
    exit 1
fi

# ── Test 1: source contains `seq 1 90` and the 90s error message ──────────
#
# Static check — guards the AC's first verify line directly without
# exercising docker at all.

if grep -q 'seq 1 90' "$TARGET"; then
    pass "wait loop iterates 90 times (seq 1 90 present)"
else
    fail "wait loop iterates 90 times (seq 1 90 present)" \
        "expected 'seq 1 90' in $TARGET"
fi

if grep -q 'postgres failed to start within 90 seconds' "$TARGET"; then
    pass "error message reads 'within 90 seconds'"
else
    fail "error message reads 'within 90 seconds'" \
        "expected 'postgres failed to start within 90 seconds' in $TARGET"
fi

if grep -q 'seq 1 30' "$TARGET"; then
    fail "no leftover 'seq 1 30' from the old 30s ceiling" \
        "found 'seq 1 30' in $TARGET — the bump is incomplete"
else
    pass "no leftover 'seq 1 30' from the old 30s ceiling"
fi

if grep -q 'within 30 seconds' "$TARGET"; then
    fail "no leftover '30 seconds' error message" \
        "found 'within 30 seconds' in $TARGET — the bump is incomplete"
else
    pass "no leftover '30 seconds' error message"
fi

# ── Set up a mock docker on PATH for the runtime tests ────────────────────

MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
ORIG_PATH_SAVE="$PATH"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

# Mock docker that simulates a container that NEVER becomes ready and
# whose `inspect .State.Running` returns "false" — i.e. the broken-postgres
# case (POSTGRES_PASSWORD=blank, OOM kill, image start failure).
#
# Behavior:
#   docker run -d ... ............ returns a fake container id
#   docker exec  $CONTAINER pg_isready -U judgemind -q ... exit 1 (never ready)
#   docker inspect -f '{{.State.Running}}' .... echoes "false"
#   docker logs ... echoes a fake error
#   docker rm -f ... exit 0
cat > "$MOCK_BIN_DIR/docker" << 'MOCKDOCKER'
#!/usr/bin/env bash
# Mock for the "broken postgres" test case (Test 2).
case "$1" in
    run)
        # Just print a fake container id and exit 0
        echo "fake-container-id"
        exit 0
        ;;
    exec)
        # pg_isready always fails — postgres never starts
        exit 1
        ;;
    inspect)
        # Container is NOT running
        echo "false"
        exit 0
        ;;
    logs)
        echo "FATAL:  password authentication failed for user \"judgemind\""
        echo "Database is uninitialized and superuser password is not specified."
        exit 0
        ;;
    rm)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
MOCKDOCKER
chmod +x "$MOCK_BIN_DIR/docker"

# ── Test 2: broken postgres → exits in <10s (#4159 AC #2) ─────────────────
#
# With the fast-fail short-circuit in place, the wait loop should detect
# the dead container on the first iteration and exit immediately. We give
# it a 30s wall-clock budget — well under the 90s ceiling — to confirm the
# short-circuit fires. Without the fix the loop would burn ~90s polling a
# dead container.
#
# We invoke check_schema_drift.sh --ci with a working dir that doesn't
# matter because the mocked docker short-circuits before any psql call.

start=$SECONDS
exit_code=0
output=$("$TARGET" --ci 2>&1) || exit_code=$?
elapsed=$((SECONDS - start))

if [[ "$exit_code" -ne 0 ]]; then
    pass "broken postgres → script exits non-zero"
else
    fail "broken postgres → script exits non-zero" \
        "expected non-zero exit, got 0"
fi

if [[ "$elapsed" -lt 10 ]]; then
    pass "broken postgres → exits in <10s (elapsed=${elapsed}s)"
else
    fail "broken postgres → exits in <10s" \
        "elapsed=${elapsed}s — expected <10s, got ${elapsed}s (fast-fail short-circuit may be missing)"
fi

if [[ "$output" == *"is not running"* ]] || [[ "$output" == *"exited unexpectedly"* ]]; then
    pass "broken postgres → useful error message mentions container exit"
else
    fail "broken postgres → useful error message mentions container exit" \
        "expected 'not running' or 'exited unexpectedly' in output, got: $output"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
