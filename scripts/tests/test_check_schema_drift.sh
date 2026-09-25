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

# ── Test 1: 90s default ceiling; error message reports real elapsed time ──
#
# Static checks. #4159 bumped the ceiling to 90s; #4663 made the error
# message report the actual elapsed time instead of a hardcoded "90" (the
# old message claimed "within 90 seconds" after only ~1.4s).

if grep -q 'SCHEMA_DRIFT_PG_WAIT_SECS:-90' "$TARGET"; then
    pass "wait loop default ceiling is 90s"
else
    fail "wait loop default ceiling is 90s" \
        "expected 'SCHEMA_DRIFT_PG_WAIT_SECS:-90' in $TARGET"
fi

if grep -q 'postgres failed to start within 90 seconds' "$TARGET"; then
    fail "error message does not hardcode '90 seconds' (#4663)" \
        "found hardcoded 'within 90 seconds' in $TARGET"
else
    pass "error message does not hardcode '90 seconds' (#4663)"
fi

if grep -q 'postgres failed to start within ${elapsed} seconds' "$TARGET"; then
    pass "error message names the elapsed-time variable (#4663)"
else
    fail "error message names the elapsed-time variable (#4663)" \
        "expected 'postgres failed to start within \${elapsed} seconds' in $TARGET"
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

# ── Mock docker #2: scripted pg_isready sequence (#4663) ──────────────────
#
# The official postgres image's entrypoint runs initdb, starts a TEMPORARY
# server, stops it, then starts the real server. A readiness probe can see
# success → failure (restart window) → success. The mock replays a
# per-call script of pg_isready results from $MOCK_PG_SEQ (space-separated
# 0/1 exit codes; the last entry repeats forever) and tracks the call
# count in $MOCK_STATE_DIR. Every other docker invocation succeeds; psql
# calls drain stdin so the script's `echo ... | psql` pipelines never
# SIGPIPE under `set -o pipefail`.
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MOCK_STATE_DIR=$(mktemp -d)
register_temp_dir "$MOCK_STATE_DIR"
export MOCK_STATE_DIR

cat > "$MOCK_BIN_DIR/docker" << 'MOCKDOCKER'
#!/usr/bin/env bash
# Mock for the scripted-readiness test cases (Tests 3 and 4).
case "$1" in
    run)
        echo "fake-container-id"
        exit 0
        ;;
    exec)
        if [[ " $* " == *" pg_isready "* ]]; then
            count_file="$MOCK_STATE_DIR/pg_isready_calls"
            n=0
            [[ -f "$count_file" ]] && n=$(cat "$count_file")
            echo $((n + 1)) > "$count_file"
            read -r -a seq <<< "$MOCK_PG_SEQ"
            idx=$n
            if (( idx >= ${#seq[@]} )); then
                idx=$(( ${#seq[@]} - 1 ))
            fi
            exit "${seq[$idx]}"
        fi
        if [[ " $* " == *" psql "* ]]; then
            cat > /dev/null
        fi
        if [[ " $* " == *" pg_dump "* ]]; then
            # Non-empty, identical dumps → the schema diff passes (an empty
            # dump would make the script's `grep -v` filter exit 1).
            echo "CREATE TABLE mock_table (id integer);"
        fi
        exit 0
        ;;
    inspect)
        echo "true"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
MOCKDOCKER
chmod +x "$MOCK_BIN_DIR/docker"

# ── Test 3: readiness flaps across the init restart → no false failure ────
#
# Regression test for #4663 (PR #4569, run 36066310129): the old loop broke
# on the FIRST pg_isready success (the temporary init server), and the
# one-shot post-loop check then landed in the restart window and failed
# with "within 90 seconds" after ~1.4s. Sequence: ready (temp server),
# not ready (restart), then ready forever (real server). The script must
# tolerate the flap and proceed.

rm -f "$MOCK_STATE_DIR/pg_isready_calls"
exit_code=0
output=$(cd "$REPO_ROOT" && MOCK_PG_SEQ="0 1 0" "$TARGET" --ci < /dev/null 2>&1) || exit_code=$?

if [[ "$exit_code" -eq 0 ]]; then
    pass "readiness flap (ready → restart → ready) → script exits 0"
else
    fail "readiness flap (ready → restart → ready) → script exits 0" \
        "exit=$exit_code output: $output"
fi

if [[ "$output" == *"failed to start"* ]]; then
    fail "readiness flap → no 'failed to start' error" "output: $output"
else
    pass "readiness flap → no 'failed to start' error"
fi

# ── Test 4: never ready (but running) → error reports real elapsed time ───
#
# With a 3s ceiling (via SCHEMA_DRIFT_PG_WAIT_SECS) and a probe that never
# succeeds, the script must exit non-zero and report the real elapsed
# seconds, not a hardcoded ceiling.

rm -f "$MOCK_STATE_DIR/pg_isready_calls"
exit_code=0
start=$SECONDS
output=$(cd "$REPO_ROOT" && MOCK_PG_SEQ="1" SCHEMA_DRIFT_PG_WAIT_SECS=3 "$TARGET" --ci < /dev/null 2>&1) || exit_code=$?
elapsed=$((SECONDS - start))

if [[ "$exit_code" -ne 0 ]]; then
    pass "never ready → script exits non-zero"
else
    fail "never ready → script exits non-zero" "expected non-zero exit, got 0"
fi

if [[ "$output" =~ "ERROR: postgres failed to start within "([0-9]+)" seconds" ]] \
    && (( BASH_REMATCH[1] >= 3 && BASH_REMATCH[1] <= elapsed + 1 )); then
    pass "never ready → error reports real elapsed time (${BASH_REMATCH[1]}s)"
else
    fail "never ready → error reports real elapsed time" \
        "expected 'failed to start within <3..$((elapsed + 1))> seconds', got: $output"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
