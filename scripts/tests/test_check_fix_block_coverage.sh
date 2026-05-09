#!/usr/bin/env bash
# test_check_fix_block_coverage.sh — Tests for scripts/check-fix-block-coverage.py
#
# Synthesizes a fixture tree of representative hygiene-guard shapes and
# asserts the classifier produces the expected verdict for each.  Also
# exercises the --check, --regenerate, and --print modes against the
# fixture so the CLI surface is regression-protected.
#
# Usage:
#   scripts/tests/test_check_fix_block_coverage.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-fix-block-coverage.py"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# All fixture guards live under $TMPDIR_TEST/scripts/.
FIXTURE_SCRIPTS="$TMPDIR_TEST/scripts"
FIXTURE_DOC="$TMPDIR_TEST/docs/dx/check-script-fix-block-coverage.md"
mkdir -p "$FIXTURE_SCRIPTS"
mkdir -p "$(dirname "$FIXTURE_DOC")"

write_executable() {
    # $1: filename under $FIXTURE_SCRIPTS
    # stdin: file contents
    local path="$FIXTURE_SCRIPTS/$1"
    cat > "$path"
    chmod +x "$path"
}

write_python_helper() {
    # $1: filename under $FIXTURE_SCRIPTS
    # stdin: file contents
    local path="$FIXTURE_SCRIPTS/$1"
    cat > "$path"
}

reset_fixture() {
    rm -rf "$FIXTURE_SCRIPTS"
    mkdir -p "$FIXTURE_SCRIPTS"
}

assert_verdict() {
    # $1: guard basename
    # $2: expected verdict
    local basename="$1"
    local expected="$2"
    TESTS=$((TESTS + 1))
    local actual
    actual=$(python3 "$CHECK_SCRIPT" --print --scripts-dir "$FIXTURE_SCRIPTS" \
                | awk -F'\t' -v n="$basename" '$1 == n {print $2}')
    if [[ "$actual" == "$expected" ]]; then
        echo "PASS: $basename → $expected"
    else
        echo "FAIL: $basename → expected '$expected', got '$actual'" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Fixture A: Fix-block-shaped guard ────────────────────────────────────

reset_fixture
write_executable "check-foo-fix.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-fix.sh — synthetic Fix-block fixture.
#
# Exit codes:
#   0 — No violations.
#   1 — Violations.

set -euo pipefail

if [[ -e "/nonexistent" ]]; then
    echo "ERROR: nonexistent file detected." >&2
    echo "" >&2
    echo "  Fix:" >&2
    echo "    Remove the offending file." >&2
    exit 1
fi
exit 0
EOF
assert_verdict "check-foo-fix.sh" "self-diagnosing (Fix block)"

# ─── Fixture B: Actionable text (no labelled Fix: block) ─────────────────

reset_fixture
write_executable "check-foo-actionable.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-actionable.sh — synthetic actionable-text fixture.
#
# Exit codes:
#   0 — No violations.
#   1 — Violations.

set -euo pipefail

if [[ -e "/nonexistent" ]]; then
    echo "ERROR: nonexistent file detected." >&2
    echo "Replace with a valid path or remove the reference." >&2
    exit 1
fi
exit 0
EOF
assert_verdict "check-foo-actionable.sh" "self-diagnosing (actionable text)"

# ─── Fixture C: Wrapper (delegates to helper) ─────────────────────────────

reset_fixture
write_executable "check-foo-wrap.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-wrap.sh — synthetic wrapper fixture.
#
# Exit codes:
#   0 — No violations.
#   1 — Violations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-foo-wrap.py" "$@"
EOF
write_python_helper "check-foo-wrap.py" <<'EOF'
#!/usr/bin/env python3
"""check-foo-wrap.py — synthetic helper for check-foo-wrap.sh."""
import sys
print("Fix: rewrite the offending statement.", file=sys.stderr)
sys.exit(0)
EOF
# Companion-pair dedup means only the .sh appears in the discovered guard
# list — assert against the .sh's verdict, which should be "wrapper".
assert_verdict "check-foo-wrap.sh" "wrapper (delegates to helper)"

# ─── Fixture D: Operational health probe (aws-flavor) ────────────────────

reset_fixture
write_executable "check-foo-ops.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-ops.sh — synthetic operational fixture.
#
# Exit codes:
#   0 — Healthy.
#   1 — Unhealthy.

set -euo pipefail

aws ecs describe-services \
    --cluster judgemind-dev \
    --services judgemind-foo-dev > /dev/null
EOF
assert_verdict "check-foo-ops.sh" "operational health probe"

# ─── Fixture E: Decision flow (verdict-token emit) ───────────────────────

reset_fixture
write_executable "check-foo-decide.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-decide.sh — synthetic decision-flow fixture.
#
# Exit codes:
#   0 — Trusted decision.
#   1 — Untrusted decision.

set -euo pipefail

if [[ "${1:-}" == "good" ]]; then
    echo "TRUSTED: input matched."
    exit 0
fi
echo "UNTRUSTED: input rejected." >&2
exit 1
EOF
assert_verdict "check-foo-decide.sh" "decision flow (no violation list)"

# ─── Fixture F: NEEDS UPGRADE (only file:line, no Fix marker) ────────────

reset_fixture
write_executable "check-foo-needs.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-needs.sh — synthetic NEEDS-UPGRADE fixture.
#
# Exit codes:
#   0 — Clean.
#   1 — Violations.

set -euo pipefail

if [[ -e "/nonexistent" ]]; then
    echo "scripts/foo.sh:42: bad pattern detected" >&2
    exit 1
fi
exit 0
EOF
assert_verdict "check-foo-needs.sh" "NEEDS UPGRADE"

# ─── Fixture G: Wrapper that emits its own Fix block (Fix block wins) ────

reset_fixture
write_executable "check-foo-wrap-fix.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-wrap-fix.sh — synthetic wrapper-with-own-Fix-block fixture.
#
# Exit codes:
#   0 — Clean.
#   1 — Violations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/check-foo-wrap-fix.py"
if ! python3 "$HELPER" "$@"; then
    echo "" >&2
    echo "  Fix: edit the offending file and re-run." >&2
    exit 1
fi
EOF
write_python_helper "check-foo-wrap-fix.py" <<'EOF'
#!/usr/bin/env python3
"""Helper that just prints file:line violations."""
import sys
print("scripts/foo.py:1: bad", file=sys.stderr)
sys.exit(1)
EOF
# Wrapper's own Fix: marker beats wrapper detection per priority order.
assert_verdict "check-foo-wrap-fix.sh" "self-diagnosing (Fix block)"

# ─── Fixture H: Underscore-named .py with hyphen-named .sh sibling ───────

reset_fixture
write_executable "check-foo-pair.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-pair.sh — synthetic .sh wrapper for the underscore-named
# .py helper. Emits its own Fix: block in the failure-fallback path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! python3 "$SCRIPT_DIR/check_foo_pair.py" "$@"; then
    echo "" >&2
    echo "  Fix: address the violations above." >&2
    exit 1
fi
EOF
write_python_helper "check_foo_pair.py" <<'EOF'
#!/usr/bin/env python3
"""check_foo_pair.py — helper that emits only file:line."""
import sys
print("scripts/foo.py:42: bad", file=sys.stderr)
sys.exit(1)
EOF
# .sh has its own Fix block, so its verdict is Fix block.
assert_verdict "check-foo-pair.sh" "self-diagnosing (Fix block)"
# .py is underscore-named with hyphen-named .sh sibling; it inherits
# the .sh's verdict.
assert_verdict "check_foo_pair.py" "self-diagnosing (Fix block)"

# ─── CLI mode: --check passes when doc matches classifier ────────────────

reset_fixture
write_executable "check-foo-cli.sh" <<'EOF'
#!/usr/bin/env bash
# check-foo-cli.sh — synthetic CLI-test fixture.

set -euo pipefail

echo "Fix: do the thing." >&2
exit 0
EOF
cat > "$FIXTURE_DOC" <<'EOF'
# Hygiene-Guard Fix-Block Coverage

| # | Guard | Verdict | Notes |
|---|-------|---------|-------|
| 1 | `scripts/check-foo-cli.sh` | self-diagnosing (Fix block) | synthetic fixture for CLI tests |

## Summary

- Total guards: 1
EOF
TESTS=$((TESTS + 1))
if python3 "$CHECK_SCRIPT" --check \
        --scripts-dir "$FIXTURE_SCRIPTS" \
        --doc "$FIXTURE_DOC" > /dev/null 2>&1; then
    echo "PASS: --check exits 0 when verdict matches"
else
    echo "FAIL: --check should exit 0 when verdict matches" >&2
    FAILURES=$((FAILURES + 1))
fi

# ─── CLI mode: --check fails when doc disagrees ──────────────────────────

cat > "$FIXTURE_DOC" <<'EOF'
# Hygiene-Guard Fix-Block Coverage

| # | Guard | Verdict | Notes |
|---|-------|---------|-------|
| 1 | `scripts/check-foo-cli.sh` | wrapper (delegates to helper) | wrong verdict on purpose |

## Summary

- Total guards: 1
EOF
TESTS=$((TESTS + 1))
if python3 "$CHECK_SCRIPT" --check \
        --scripts-dir "$FIXTURE_SCRIPTS" \
        --doc "$FIXTURE_DOC" > /dev/null 2>&1; then
    echo "FAIL: --check should exit 1 when verdict disagrees" >&2
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: --check exits non-zero on verdict drift"
fi

# ─── CLI mode: --regenerate prints the table ─────────────────────────────

TESTS=$((TESTS + 1))
regenerated=$(python3 "$CHECK_SCRIPT" --regenerate \
        --scripts-dir "$FIXTURE_SCRIPTS" 2>/dev/null)
if grep -q '| 1 | `scripts/check-foo-cli.sh` | self-diagnosing (Fix block) |' \
        <<< "$regenerated"; then
    echo "PASS: --regenerate prints the canonical row"
else
    echo "FAIL: --regenerate output missing expected row" >&2
    echo "Got:" >&2
    echo "$regenerated" >&2
    FAILURES=$((FAILURES + 1))
fi

# ─── CLI mode: --print emits one tab-separated line per guard ────────────

TESTS=$((TESTS + 1))
printed=$(python3 "$CHECK_SCRIPT" --print \
        --scripts-dir "$FIXTURE_SCRIPTS" 2>/dev/null)
if [[ "$printed" == *"check-foo-cli.sh"$'\t'"self-diagnosing (Fix block)"* ]]; then
    echo "PASS: --print emits the expected line"
else
    echo "FAIL: --print output missing expected line" >&2
    echo "Got: $printed" >&2
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Tests run: $TESTS"
echo "Failures:  $FAILURES"

if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
exit 0
