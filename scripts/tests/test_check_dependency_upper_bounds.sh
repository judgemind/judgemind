#!/usr/bin/env bash
# test_check_dependency_upper_bounds.sh — Tests for check-dependency-upper-bounds.py (#4756)
#
# Fixtures (each a synthetic packages/<pkg>/pyproject.toml tree):
#   A — floor-only dep with a fake .venv dist-info  → exit 1, Fix block with
#       the literal bounded spec computed from the resolved version
#   B — floor-only 0.x dep                          → Fix suggests next minor
#   C — floor-only dep, no .venv                    → Fix falls back to
#       <NEXT_MAJOR> plus the pip-show recipe
#   D — all deps bounded (<, ~=, ==, URL), local sibling, reasoned
#       exemption, markers, dev extras unbounded    → exit 0
#   E — exemption with an empty reason does not exempt → exit 1
#   F — the live repo tree passes                   → exit 0
#
# Usage: scripts/tests/test_check_dependency_upper_bounds.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$SCRIPT_DIR/check-dependency-upper-bounds.py"
FAILURES=0
TESTS=0

TMP=$(mktemp -d)
cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT

pass() { TESTS=$((TESTS + 1)); echo "PASS: $1"; }
fail() { TESTS=$((TESTS + 1)); FAILURES=$((FAILURES + 1)); echo "FAIL: $1"; }

# write_pkg <root> <pkg-dir> <project-name> <deps-toml-array-body> [extra-toml]
write_pkg() {
    mkdir -p "$1/packages/$2"
    {
        printf '[project]\nname = "%s"\nversion = "0.1.0"\ndependencies = [\n%s\n]\n' "$3" "$4"
        printf '\n[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n'
        if [ "$#" -ge 5 ]; then
            printf '\n%s\n' "$5"
        fi
    } > "$1/packages/$2/pyproject.toml"
}

# fake_dist <root> <pkg-dir> <dist-name> <version>
fake_dist() {
    mkdir -p "$1/packages/$2/.venv/lib/python3.12/site-packages/$3-$4.dist-info"
}

run_check() {
    python3 "$CHECK" "$@" >"$TMP/out" 2>"$TMP/err"
    echo $?
}

# ── Fixture A ────────────────────────────────────────────────────────
A="$TMP/a"
write_pkg "$A" svc "svc" '    "redis>=5.0",
    "psycopg[binary]>=3.1",'
fake_dist "$A" svc redis 8.1.0
fake_dist "$A" svc psycopg 3.3.6
rc=$(run_check "$A/packages/svc/pyproject.toml")
if [ "$rc" = "1" ]; then pass "A: floor-only deps exit 1"; else fail "A: expected exit 1, got $rc"; fi
if grep -q '^Fix:' "$TMP/err"; then pass "A: Fix block present"; else fail "A: no Fix: block"; cat "$TMP/err"; fi
if grep -qF '"redis>=5.0,<9"' "$TMP/err"; then pass "A: literal redis bound from resolved 8.1.0"; else fail "A: missing redis>=5.0,<9 suggestion"; cat "$TMP/err"; fi
if grep -qF '"psycopg[binary]>=3.1,<4"' "$TMP/err"; then pass "A: extras preserved in suggestion"; else fail "A: missing psycopg[binary]>=3.1,<4"; cat "$TMP/err"; fi
if grep -qF 'unbounded-ok' "$TMP/err"; then pass "A: exemption recipe shown"; else fail "A: exemption recipe missing"; fi

# ── Fixture B ────────────────────────────────────────────────────────
B="$TMP/b"
write_pkg "$B" svc "svc" '    "httpx>=0.27",'
fake_dist "$B" svc httpx 0.28.1
rc=$(run_check "$B/packages/svc/pyproject.toml")
if [ "$rc" = "1" ] && grep -qF '"httpx>=0.27,<0.29"' "$TMP/err"; then
    pass "B: 0.x bound at next minor"
else
    fail "B: expected httpx>=0.27,<0.29 (rc=$rc)"; cat "$TMP/err"
fi

# ── Fixture C ────────────────────────────────────────────────────────
C="$TMP/c"
write_pkg "$C" svc "svc" '    "boto3>=1.34",'
rc=$(run_check "$C/packages/svc/pyproject.toml")
if [ "$rc" = "1" ] && grep -qF '"boto3>=1.34,<NEXT_MAJOR>"' "$TMP/err" && grep -qF 'pip show boto3' "$TMP/err"; then
    pass "C: no .venv falls back to placeholder + pip show recipe"
else
    fail "C: fallback suggestion missing (rc=$rc)"; cat "$TMP/err"
fi

# ── Fixture D ────────────────────────────────────────────────────────
D="$TMP/d"
write_pkg "$D" sibling "sibling-lib" ''
write_pkg "$D" svc "svc" '    "redis>=5.0,<9",
    "rapidfuzz~=3.14",
    "croniter==6.2.4",
    "lxml<7",
    "mylib @ https://example.com/mylib-1.0.tar.gz",
    "pywin32>=300,<400 ; sys_platform == \"win32\"",
    "sibling-lib",
    "Certifi>=2024.2.2",' '[tool.judgemind.dependency-bounds.unbounded-ok]
certifi = "CA bundle must track upstream"'
rc=$(run_check "$D/packages/svc/pyproject.toml")
if [ "$rc" = "0" ]; then pass "D: bounded / sibling / exempt deps pass"; else fail "D: expected exit 0, got $rc"; cat "$TMP/err"; fi

# ── Fixture E ────────────────────────────────────────────────────────
E="$TMP/e"
write_pkg "$E" svc "svc" '    "certifi>=2024.2.2",' '[tool.judgemind.dependency-bounds.unbounded-ok]
certifi = "  "'
rc=$(run_check "$E/packages/svc/pyproject.toml")
if [ "$rc" = "1" ]; then pass "E: empty-reason exemption does not exempt"; else fail "E: expected exit 1, got $rc"; fi

# ── Unbounded marker-only spec still flagged ─────────────────────────
M="$TMP/m"
write_pkg "$M" svc "svc" '    "pywin32>=300 ; python_version < \"4\"",'
rc=$(run_check "$M/packages/svc/pyproject.toml")
if [ "$rc" = "1" ]; then pass "M: < inside an env marker is not an upper bound"; else fail "M: expected exit 1, got $rc"; fi

# ── Usage errors ─────────────────────────────────────────────────────
rc=$(run_check "$TMP/does-not-exist.toml")
if [ "$rc" = "2" ]; then pass "missing file exits 2"; else fail "missing file: expected 2, got $rc"; fi
printf 'not = [valid toml\n' > "$TMP/bad.toml"
rc=$(run_check "$TMP/bad.toml")
if [ "$rc" = "2" ]; then pass "malformed TOML exits 2"; else fail "malformed TOML: expected 2, got $rc"; fi

# ── Fixture F: live tree ─────────────────────────────────────────────
rc=$(run_check)
if [ "$rc" = "0" ]; then pass "F: live packages/*/pyproject.toml pass"; else fail "F: live tree has unbounded deps"; cat "$TMP/err"; fi

echo ""
echo "$TESTS tests, $FAILURES failures"
if [ "$FAILURES" -ne 0 ]; then
    exit 1
fi
exit 0
