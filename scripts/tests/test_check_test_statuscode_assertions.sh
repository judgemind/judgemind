#!/usr/bin/env bash
# test_check_test_statuscode_assertions.sh — Tests for
# scripts/check-test-statuscode-assertions.{sh,py}.
#
# Covers the documented behaviour of the guard:
#   * Missing assertion on a title that names HTTP <NNN>     → exit 1
#   * Missing assertion on a title that says "returns <NNN>" → exit 1
#   * Title with assertion present                           → exit 0
#   * Title without an HTTP status reference                 → exit 0
#   * Same-line `// status-assertion-noqa` opt-out           → exit 0
#   * Multi-status title with one missing assertion          → exit 1
#   * Embedded --selftest                                    → exit 0
#   * Regression fixture mirroring #4129 / #4218             → exit 1 (pre-fix), exit 0 (post-fix)
#
# Usage:
#   scripts/tests/test_check_test_statuscode_assertions.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-test-statuscode-assertions.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TMPDIR_TEST="$(mktemp -d)"

cleanup() {
    set +eu
    if [[ -n "$TMPDIR_TEST" && -d "$TMPDIR_TEST" ]]; then
        rm -rf "$TMPDIR_TEST"
    fi
}
trap cleanup EXIT

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

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"
    mkdir -p "$TMPDIR_TEST"
}

# Run the guard against TMPDIR_TEST and capture exit code.
run_guard() {
    local exit_code=0
    "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1 || exit_code=$?
    echo "$exit_code"
}

# ── Precondition: wrapper exists and is executable ─────────────────────────

if [[ ! -x "$CHECK_SCRIPT" ]]; then
    echo "FAIL: $CHECK_SCRIPT is not executable (or does not exist)" >&2
    exit 1
fi

# ── Test 1: Missing assertion on "HTTP 400" title fails ────────────────────

reset_tmpdir
cat > "$TMPDIR_TEST/missing_http.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it('rejects with HTTP 400 when input is bad', async () => {
    const res = await app.inject({ url: '/foo' });
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 1 ]]; then
    pass "exit 1 when title says HTTP 400 but body has no statusCode assertion"
else
    fail "exit 1 when title says HTTP 400 but body has no statusCode assertion" "got exit $got"
fi

# ── Test 2: Missing assertion on "returns 404" title fails ─────────────────

reset_tmpdir
cat > "$TMPDIR_TEST/missing_returns.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it('returns 404 when document does not exist', async () => {
    const res = await app.inject({ url: '/foo' });
    const body = JSON.parse(res.body);
    expect(body).toEqual({});
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 1 ]]; then
    pass "exit 1 when title says 'returns 404' but body has no statusCode assertion"
else
    fail "exit 1 when title says 'returns 404' but body has no statusCode assertion" "got exit $got"
fi

# ── Test 3: Title with assertion present passes ────────────────────────────

reset_tmpdir
cat > "$TMPDIR_TEST/with_assert.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it('returns 400 for invalid UUID', async () => {
    const res = await app.inject({ url: '/foo' });
    expect(res.statusCode).toBe(400);
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 0 ]]; then
    pass "exit 0 when title says 'returns 400' AND body asserts statusCode=400"
else
    fail "exit 0 when title says 'returns 400' AND body asserts statusCode=400" "got exit $got"
fi

# ── Test 4: Title without HTTP status reference passes (unchecked) ─────────

reset_tmpdir
cat > "$TMPDIR_TEST/no_status_ref.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it('rejects gracefully', async () => {
    const res = await app.inject({ url: '/foo' });
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 0 ]]; then
    pass "exit 0 when title doesn't reference an HTTP status"
else
    fail "exit 0 when title doesn't reference an HTTP status" "got exit $got"
fi

# ── Test 5: Same-line // status-assertion-noqa suppresses violation ────────

reset_tmpdir
cat > "$TMPDIR_TEST/with_noqa.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it('returns 400 via direct call', async () => { // status-assertion-noqa
    await expect(callDirectly()).rejects.toThrow(/bad input/);
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 0 ]]; then
    pass "exit 0 when // status-assertion-noqa is on the it() opening line"
else
    fail "exit 0 when // status-assertion-noqa is on the it() opening line" "got exit $got"
fi

# ── Test 6: Multi-status title — one asserted, one not — fails ─────────────

reset_tmpdir
cat > "$TMPDIR_TEST/multi_status.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it('returns 400 for invalid UUID and HTTP 401 for missing auth', async () => {
    const res = await app.inject({ url: '/foo' });
    expect(res.statusCode).toBe(400);
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 1 ]]; then
    pass "exit 1 when multi-status title asserts only one of the named codes"
else
    fail "exit 1 when multi-status title asserts only one of the named codes" "got exit $got"
fi

# ── Test 7: Embedded --selftest passes ─────────────────────────────────────

exit_code=0
"$CHECK_SCRIPT" --selftest > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "embedded --selftest exits 0"
else
    fail "embedded --selftest exits 0" "got exit $exit_code"
fi

# ── Test 8: Regression fixture for #4129 / #4218 ───────────────────────────
#
# AC #2 of #4220: "the script catches the #4129 failure mode by parsing test
# titles that name an HTTP status and requiring a corresponding statusCode
# assertion in the test body." Mirror the pre-#4218 and post-#4218 shapes of
# packages/api/src/graphql/cost-limit-plugin.unit.test.ts:190 here.

reset_tmpdir
# pre-#4218: title says "HTTP 400" but ONLY body shape is asserted. The bug
# this would have caught (#4129: 400 → 500 regression) slipped through under
# this exact shape. Guard MUST flag this.
cat > "$TMPDIR_TEST/pre_4218.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('cost-limit-plugin', () => {
  it('rejects a 40-unit-over-cap query with HTTP 400 + complexityLimitExceeded', async () => {
    const res = await app.inject({ url: '/graphql' });
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
    const exceeded = body.errors[0];
    expect(exceeded.extensions.complexityLimitExceeded).toBe(true);
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 1 ]]; then
    pass "exit 1 on pre-#4218 fixture (would have caught #4129 regression)"
else
    fail "exit 1 on pre-#4218 fixture (would have caught #4129 regression)" "got exit $got"
fi

# post-#4218: same title, but `expect(res.statusCode).toBe(400)` added.
# Guard MUST accept this — it's the fixed shape that ships today.
reset_tmpdir
cat > "$TMPDIR_TEST/post_4218.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('cost-limit-plugin', () => {
  it('rejects a 40-unit-over-cap query with HTTP 400 + complexityLimitExceeded', async () => {
    const res = await app.inject({ url: '/graphql' });
    expect(res.statusCode).toBe(400);
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
    const exceeded = body.errors[0];
    expect(exceeded.extensions.complexityLimitExceeded).toBe(true);
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 0 ]]; then
    pass "exit 0 on post-#4218 fixture (statusCode assertion added)"
else
    fail "exit 0 on post-#4218 fixture (statusCode assertion added)" "got exit $got"
fi

# ── Test 9: Wrong-status assertion still fails ─────────────────────────────
# A title saying "returns 400" with `statusCode === 500` in the body is a
# real bug, not a typo — the guard must catch it.

reset_tmpdir
cat > "$TMPDIR_TEST/wrong_status.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it('returns 400 for invalid input', async () => {
    const res = await app.inject({ url: '/foo' });
    expect(res.statusCode).toBe(500);
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 1 ]]; then
    pass "exit 1 when title says 400 but body asserts statusCode=500"
else
    fail "exit 1 when title says 400 but body asserts statusCode=500" "got exit $got"
fi

# ── Test 10: it.each table form ────────────────────────────────────────────

reset_tmpdir
cat > "$TMPDIR_TEST/each_form.test.ts" << 'EOF'
import { describe, it, expect } from 'vitest';
describe('x', () => {
  it.each([['a'], ['b']])('returns 404 for %s', async (input) => {
    const res = await app.inject({ url: input });
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
  });
});
EOF
got=$(run_guard)
if [[ "$got" -eq 1 ]]; then
    pass "exit 1 on it.each() title without statusCode assertion"
else
    fail "exit 1 on it.each() title without statusCode assertion" "got exit $got"
fi

# ── Test 12: Self-match on CI step name ────────────────────────────────────
# Per docs/agent/code-standards.md §Hygiene-check CI steps: the step's
# `name:` in ci.yml must NOT trip the guard. For this guard, "tripping"
# means the step name contains an HTTP-status reference (e.g. "HTTP 400",
# "returns 404") without a corresponding statusCode assertion. We
# synthesize a *.test.ts fixture whose `it()` title is the actual ci.yml
# step name and run the guard against it — exit 0 means the step name is
# safe; exit 1 means the step name itself would fail CI. See #2541/#2542
# for the precedent (the test-except-pass guard had this exact failure
# mode on its first wiring attempt).
#
# The check is best-effort: if no ci.yml step currently runs this guard
# (e.g. running this test from a branch where the wiring hasn't landed
# yet), we report PASS with a "nothing to self-match" note.

reset_tmpdir
GUARD_REPO_PATH="scripts/check-test-statuscode-assertions.sh"
CI_YML_PATH="$(cd "$SCRIPT_DIR/.." && pwd)/.github/workflows/ci.yml"
EXTRACTOR_AWK="$SCRIPT_DIR/tests/_extract_guard_step_names.awk"

if [[ -f "$CI_YML_PATH" && -f "$EXTRACTOR_AWK" ]]; then
    NAMES_FILE="$TMPDIR_TEST/ci_step_names.txt"
    awk -v script="$GUARD_REPO_PATH" -f "$EXTRACTOR_AWK" "$CI_YML_PATH" > "$NAMES_FILE" 2>/dev/null || true

    if [[ ! -s "$NAMES_FILE" ]]; then
        pass "no ci.yml step references $GUARD_REPO_PATH yet (nothing to self-match)"
    else
        FIXTURE="$TMPDIR_TEST/ci_step_names.test.ts"
        {
            echo "import { describe, it, expect } from 'vitest';"
            echo "describe('ci-yml-step-names', () => {"
            while IFS= read -r step_name; do
                step_name="${step_name//\"/}"
                step_name="${step_name//\'/}"
                echo "  it('${step_name}', async () => {"
                echo "    expect(1).toBe(1);"
                echo "  });"
            done < "$NAMES_FILE"
            echo "});"
        } > "$FIXTURE"
        rm -f "$NAMES_FILE"

        exit_code=0
        "$CHECK_SCRIPT" "$FIXTURE" > /dev/null 2>&1 || exit_code=$?
        if [[ "$exit_code" -eq 0 ]]; then
            pass "no self-match on ci.yml step name for $GUARD_REPO_PATH"
        else
            fail "no self-match on ci.yml step name for $GUARD_REPO_PATH" "guard exit $exit_code — step name itself trips the guard"
        fi
    fi
else
    pass "self-match check skipped (ci.yml or extractor missing)"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
