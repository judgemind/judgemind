#!/usr/bin/env bash
# test_check_no_basicconfig_with_extra.sh — Tests for
# check-no-basicconfig-with-extra.sh.
#
# Issue #4376.  The guard forbids ``logging.basicConfig`` +
# ``extra=`` co-occurrence in scripts/*.py files that do not also
# call ``configure_structlog``.  The combination silently drops every
# ``extra=`` field from CloudWatch output (see #4368).
#
# Tests below build temp .py files in TMPDIR_TEST and point the guard
# at that directory, mirroring the test_check_no_redos_pattern.sh
# pattern.
#
# Usage:
#   scripts/tests/test_check_no_basicconfig_with_extra.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-basicconfig-with-extra.sh"
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
    echo "$path"
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

# ─── Test 1: The exact #4368 anti-pattern is caught ──────────────────────
# logging.basicConfig + logger.info(..., extra={...}) + no configure_structlog.
create_test_file "antipattern.py" 'import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


def run():
    logger.info("processed", extra={"s3_key": "ca/foo/raw/abc.pdf"})
'
assert_fails "Exact #4368 anti-pattern (basicConfig + extra= + no configure_structlog) is caught"
reset_tmpdir

# ─── Test 2: One-line bad form is caught ─────────────────────────────────
create_test_file "oneline.py" 'import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
logger.warning("oops", extra={"k": "v"})
'
assert_fails "One-line basicConfig + extra= is caught"
reset_tmpdir

# ─── Test 3: configure_structlog present → passes ────────────────────────
# The post-#4368 fix shape — basicConfig is gone, replaced with
# configure_structlog.
create_test_file "fixed.py" 'import logging
from framework.logging import configure_structlog

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)


def run():
    logger.info("processed", extra={"s3_key": "ca/foo/raw/abc.pdf"})
'
assert_passes "configure_structlog (no basicConfig) passes"
reset_tmpdir

# ─── Test 4: Both basicConfig AND configure_structlog → passes ───────────
# If a contributor has been deliberate enough to call both, we trust
# them — the structlog config supersedes basicConfig at runtime.
create_test_file "both.py" 'import logging
from framework.logging import configure_structlog

logging.basicConfig(level=logging.INFO)
configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)
logger.info("processed", extra={"k": "v"})
'
assert_passes "basicConfig AND configure_structlog (deliberate config) passes"
reset_tmpdir

# ─── Test 5: basicConfig but no extra= calls → passes ────────────────────
# The bug class is latent, not active.  The guard does not flag this
# defensively-migrated shape (#4373 covered the latent shape; this
# guard is for new regressions that pair extra= with basicConfig).
create_test_file "latent.py" 'import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def run():
    logger.info("no extras here")
'
assert_passes "basicConfig with no extra= calls is not flagged (latent shape)"
reset_tmpdir

# ─── Test 6: extra= without basicConfig → passes ─────────────────────────
# A library module that uses logger.info(..., extra=...) but does not
# own the global config is fine — the entrypoint calls
# configure_structlog elsewhere.
create_test_file "library.py" 'import logging

logger = logging.getLogger(__name__)


def emit(key: str) -> None:
    logger.info("processed", extra={"key": key})
'
assert_passes "extra= calls without basicConfig (library module) pass"
reset_tmpdir

# ─── Test 7: Comment-only references do not trip the AST check ───────────
# Many post-#4373 scripts retain a comment like "# Use configure_structlog
# so any extra= ... — the previous logging.basicConfig format dropped..."
# AST inspection ignores comments, so the comment alone must not flag.
create_test_file "comment_only.py" '"""A migrated script.

The previous logging.basicConfig format string silently dropped every
extra= field — see #4368.
"""
import logging
from framework.logging import configure_structlog

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)
logger.info("processed", extra={"k": "v"})
'
assert_passes "Comment/docstring references to basicConfig and extra= do not flag"
reset_tmpdir

# ─── Test 8: from logging import basicConfig → still detected ────────────
# Caller imported the function directly; the AST walk still recognises
# the bare ``basicConfig(...)`` name.
create_test_file "bare_import.py" 'import logging
from logging import basicConfig

basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
logger.info("processed", extra={"k": "v"})
'
assert_fails "from logging import basicConfig + bare basicConfig() is caught"
reset_tmpdir

# ─── Test 9: log.warning() (not logger.warning) is detected ──────────────
# We accept any receiver name — "log", "logger", "LOGGER", "self.log"
# all pass the attribute-name match.
create_test_file "alt_receiver.py" 'import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("my-module")
log.warning("oops", extra={"k": "v"})
'
assert_fails "Alternative receiver name (log.warning instead of logger.warning) is caught"
reset_tmpdir

# ─── Test 10: logger.exception() with extra= is detected ─────────────────
create_test_file "exception.py" 'import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
try:
    raise RuntimeError("boom")
except RuntimeError:
    logger.exception("failed", extra={"k": "v"})
'
assert_fails "logger.exception with extra= is caught"
reset_tmpdir

# ─── Test 11: logger.log(level, msg, extra=...) is detected ──────────────
create_test_file "log_method.py" 'import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
logger.log(logging.INFO, "processed", extra={"k": "v"})
'
assert_fails "logger.log(level, msg, extra=) is caught"
reset_tmpdir

# ─── Test 12: Empty file passes ──────────────────────────────────────────
create_test_file "empty.py" ''
assert_passes "Empty .py file passes"
reset_tmpdir

# ─── Test 13: Pure `pass` stub passes ────────────────────────────────────
create_test_file "stub.py" 'pass
'
assert_passes "Pure stub .py file passes"
reset_tmpdir

# ─── Test 14: Syntactically broken Python is skipped silently ────────────
create_test_file "broken.py" 'import logging
def broken(:  # syntax error
    pass
'
assert_passes "Syntactically broken Python is skipped silently"
reset_tmpdir

# ─── Test 15: extra= as positional dict (not kwarg) is NOT flagged ───────
# logger.info("msg", extra) is illegal anyway (signature mismatch);
# the heuristic only matches the kwarg form, which is the only shape
# that compiles and ships.
create_test_file "positional.py" 'import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
extra = {"k": "v"}
logger.info("msg")  # no extra= kwarg
logger.info("msg2 %r", extra)  # extra is positional, not kwarg
'
assert_passes "extra used as variable name (not kwarg) is not flagged"
reset_tmpdir

# ─── Test 16: Multiple extra= calls report once per file ─────────────────
# The Python scanner emits one entry per *file*, not per call site —
# the wrapper aggregates and the violation count is the file count.
create_test_file "multi_extras.py" 'import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
logger.info("a", extra={"k": 1})
logger.warning("b", extra={"k": 2})
logger.error("c", extra={"k": 3})
'
assert_fails "File with multiple extra= calls is flagged once"
reset_tmpdir

# ─── Test 17: The guard emits a Fix block in stderr ──────────────────────
# Per the Fix-block contract (docs/agent/code-standards.md
# §Hygiene-check guards: Fix-block contract), the guard must emit a
# copy-pasteable Fix block on the violation path.  Build a violating
# fixture and assert the Fix block content shows up.
create_test_file "fixblock.py" 'import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
logger.info("oops", extra={"k": "v"})
'
TESTS=$((TESTS + 1))
err_out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if printf '%s' "$err_out" | grep -q '^[[:space:]]*Fix:' \
   && printf '%s' "$err_out" | grep -q 'configure_structlog' \
   && printf '%s' "$err_out" | grep -q 'framework.logging'; then
    echo "PASS: error output emits a Fix: block naming configure_structlog and framework.logging"
else
    echo "FAIL: error output did not emit the expected Fix: block"
    echo "  output was:"
    printf '%s\n' "$err_out" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 18: file:lineno is named in the violation report ───────────────
create_test_file "src/named.py" 'import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
logger.info("oops", extra={"k": "v"})
'
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST/src" 2>&1 || true)
if printf '%s' "$output" | grep -qE 'src/named\.py:[0-9]+:.*basicConfig'; then
    echo "PASS: error output names file:line of violation"
else
    echo "FAIL: error output did not include file:line for violation"
    echo "  output was:"
    printf '%s\n' "$output" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 19: Direct file-path argument works ────────────────────────────
TESTS=$((TESTS + 1))
single_file="$TMPDIR_TEST/single.py"
mkdir -p "$TMPDIR_TEST"
printf 'import logging\nlogging.basicConfig(level=logging.INFO, format="%%(asctime)s %%(message)s")\nlogger = logging.getLogger(__name__)\nlogger.info("oops", extra={"k": "v"})\n' \
    > "$single_file"
if "$CHECK_SCRIPT" "$single_file" > /dev/null 2>&1; then
    echo "FAIL: Direct .py file path argument detects violations (expected failure, got success)"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: Direct .py file path argument detects violations"
fi
reset_tmpdir

# ─── Test 20: scripts/*.py production scope passes on origin/main ────────
# AC verify line: ``scripts/check-no-basicconfig-with-extra.sh`` (no
# args) exits 0 against the worktree's scripts/*.py.  This duplicates
# the AC's verify command into the test suite so a regression that
# adds the anti-pattern to a real script is caught at PR time, not
# at CI time.
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: production scope (scripts/*.py) passes on this worktree"
else
    echo "FAIL: production scope (scripts/*.py) does NOT pass on this worktree"
    echo "  output was:"
    "$CHECK_SCRIPT" 2>&1 | sed 's/^/    /' || true
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 21: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden token.  See
# docs/agent/code-standards.md §Hygiene-check CI steps and #2541/#2542.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-basicconfig-with-extra.sh" "py"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
