#!/usr/bin/env bash
# test_check_no_hardcoded_llm_provider.sh — Tests for
# check-no-hardcoded-llm-provider.sh.
#
# Creates temporary .py files to verify that the checker correctly
# detects hardcoded ``provider="..."`` literals on LLM-adapter call
# sites while allowing variable forms, the LlmExtractor constructor,
# the opt-out marker, and the adapter file itself.
#
# Usage:
#   scripts/tests/test_check_no_hardcoded_llm_provider.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-hardcoded-llm-provider.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: call_llm with provider="anthropic" should fail ─────────────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(system_prompt="x", user_message="y", provider="anthropic")
'
assert_fails "call_llm(provider=\"anthropic\") is detected"
reset_tmpdir

# ─── Test 2: call_llm with provider="google" should fail ────────────────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(system_prompt="x", user_message="y", provider="google")
'
assert_fails "call_llm(provider=\"google\") is detected"
reset_tmpdir

# ─── Test 3: call_llm_with_images with provider="anthropic" fails ───────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm_with_images

def run() -> None:
    call_llm_with_images(
        system_prompt="x",
        text_message="y",
        images=[],
        provider="anthropic",
    )
'
assert_fails "call_llm_with_images(provider=\"anthropic\") is detected"
reset_tmpdir

# ─── Test 4: create_client with provider="anthropic" fails ──────────────
create_test_file "scraper.py" 'from ingestion.llm_providers import create_client

def setup() -> None:
    client = create_client(provider="anthropic")
    return client
'
assert_fails "create_client(provider=\"anthropic\") is detected"
reset_tmpdir

# ─── Test 5: create_llm_client (renamed import) with literal fails ──────
# create_client is often imported as create_llm_client in worker.py.
create_test_file "scraper.py" 'from ingestion.llm_providers import create_client as create_llm_client

def setup() -> None:
    client = create_llm_client(provider="anthropic")
    return client
'
assert_fails "create_llm_client(provider=\"anthropic\") is detected"
reset_tmpdir

# ─── Test 6: Attribute-style call llm_providers.call_llm fails ──────────
# Match by the bare callee name regardless of attribute prefix.
create_test_file "scraper.py" 'import ingestion.llm_providers as llm_providers

def run() -> None:
    llm_providers.call_llm(
        system_prompt="x",
        user_message="y",
        provider="google",
    )
'
assert_fails "Attribute-style llm_providers.call_llm(provider=\"google\") is detected"
reset_tmpdir

# ─── Test 7: provider=variable should pass ──────────────────────────────
# This is the canonical valid pattern — pass through a variable that
# resolves from the LLM_PROVIDER env var.
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run(provider: str | None = None) -> None:
    call_llm(system_prompt="x", user_message="y", provider=provider)
'
assert_passes "call_llm(provider=variable) is allowed"
reset_tmpdir

# ─── Test 8: provider=self.attribute should pass ────────────────────────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm_with_images

class X:
    def __init__(self) -> None:
        self._provider = None

    def run(self) -> None:
        call_llm_with_images(
            system_prompt="x",
            text_message="y",
            images=[],
            provider=self._provider,
        )
'
assert_passes "call_llm_with_images(provider=self.attr) is allowed"
reset_tmpdir

# ─── Test 9: No provider= arg at all should pass ────────────────────────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(system_prompt="x", user_message="y")
'
assert_passes "call_llm without provider= arg is allowed"
reset_tmpdir

# ─── Test 10: LlmExtractor(provider="google") should pass ───────────────
# Per #4050 Out of Scope: the LlmExtractor constructor's provider= arg is
# a different abstraction (per-court override) and is intentional.
create_test_file "scraper.py" 'from framework.llm_extractor import LlmExtractor

def run() -> None:
    extractor = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")
    extractor.extract_from_pdf(b"")
'
assert_passes "LlmExtractor(provider=\"google\") constructor is allowed (out of scope)"
reset_tmpdir

# ─── Test 11: Opt-out marker on the same line should pass ───────────────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    # This scraper has been audited and Gemini gives wrong results, see #1234.
    call_llm(system_prompt="x", user_message="y", provider="anthropic")  # hardcoded-provider-ok: audited #1234
'
assert_passes "Same-line # hardcoded-provider-ok: marker suppresses the violation"
reset_tmpdir

# ─── Test 12: Opt-out marker on a multi-line call should pass ───────────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(
        system_prompt="x",
        user_message="y",
        provider="anthropic",  # hardcoded-provider-ok: audited #1234
        model=None,
    )
'
assert_passes "Mid-call # hardcoded-provider-ok: marker on multi-line call suppresses"
reset_tmpdir

# ─── Test 13: Opt-out marker on the opening line of a multi-line call ──
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(  # hardcoded-provider-ok: audited #1234
        system_prompt="x",
        user_message="y",
        provider="anthropic",
        model=None,
    )
'
assert_passes "Opening-line # hardcoded-provider-ok: marker suppresses multi-line call"
reset_tmpdir

# ─── Test 14: Opt-out without a reason should NOT suppress ──────────────
# The reason after the colon is required — a bare marker is rejected.
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(system_prompt="x", user_message="y", provider="anthropic")  # hardcoded-provider-ok:
'
assert_fails "Bare # hardcoded-provider-ok: with no reason does not suppress"
reset_tmpdir

# ─── Test 15: llm_providers.py source file is excluded ──────────────────
# When scanning the production scope, the adapter layer itself is
# excluded — that's where the canonical provider= literal lives.
mkdir -p "$TMPDIR_TEST/packages/scraper-framework/src/ingestion"
cat > "$TMPDIR_TEST/packages/scraper-framework/src/ingestion/llm_providers.py" <<'PYEOF'
def _call_anthropic(system_prompt, user_message):
    """Adapter — provider="anthropic" is intentional here."""
    return call_llm(system_prompt=system_prompt, user_message=user_message, provider="anthropic")
PYEOF
assert_passes "llm_providers.py adapter file is excluded from the scan"
reset_tmpdir

# ─── Test 16: tests/ subdirectories are excluded ────────────────────────
mkdir -p "$TMPDIR_TEST/tests"
cat > "$TMPDIR_TEST/tests/test_provider.py" <<'PYEOF'
def test_anthropic():
    client = create_client(provider="anthropic")
    return client
PYEOF
assert_passes "tests/ subdirectories are excluded from the scan"
reset_tmpdir

# ─── Test 17: Other function names are not flagged ──────────────────────
# A call with provider="anthropic" to some unrelated function (not in
# the closed set of LLM-adapter entry points) is not in scope.
create_test_file "scraper.py" 'def my_helper(provider: str = "anthropic") -> None:
    return None

my_helper(provider="anthropic")
'
assert_passes "Unrelated function names with provider= literal are allowed"
reset_tmpdir

# ─── Test 18: Multiple violations in one file are all detected ──────────
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm, create_client

def run() -> None:
    call_llm(system_prompt="x", user_message="y", provider="anthropic")
    create_client(provider="google")
'
assert_fails "Multiple violations in one file are detected"
reset_tmpdir

# ─── Test 19: Single-quoted literal also fails ──────────────────────────
create_test_file "scraper.py" "from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(system_prompt='x', user_message='y', provider='anthropic')
"
assert_fails "Single-quoted provider='anthropic' is detected"
reset_tmpdir

# ─── Test 20: Empty input directory passes ──────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 21: Error message names file:line and the env var fix ─────────
# When the check fails, the error message must include the file path,
# the line number, and a pointer at LLM_PROVIDER as the canonical fix.
create_test_file "scraper.py" 'from ingestion.llm_providers import call_llm

def run() -> None:
    call_llm(system_prompt="x", user_message="y", provider="anthropic")
'
TESTS=$((TESTS + 1))
output="$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)"
if [[ "$output" == *"scraper.py:"*":call_llm(provider=\"anthropic\")"* ]] \
   && [[ "$output" == *"LLM_PROVIDER"* ]]; then
    echo "PASS: Error message names file:line and points at LLM_PROVIDER env var"
else
    echo "FAIL: Error message missing file:line or LLM_PROVIDER pointer"
    echo "  output: $output"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 22: No self-match on ci.yml step name ─────────────────────────
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-hardcoded-llm-provider.sh" "yml"

# ─── Summary ─────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
