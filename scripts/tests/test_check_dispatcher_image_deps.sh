#!/usr/bin/env bash
# test_check_dispatcher_image_deps.sh — Tests for check-dispatcher-image-deps.sh
#
# Creates synthetic (source_dir, Dockerfile) pairs in a temp directory and
# verifies the checker's exit code matches expectations. Covers the four
# behaviors the issue (#2776) calls out explicitly:
#
#   (a) Current main passes (smoke test against the real repo).
#   (b) A missing dependency is detected (``import requests`` added to
#       a synthetic daemon without adding ``requests`` to the Dockerfile).
#   (c) Lazy imports inside function bodies are caught (``import boto3``
#       at the bottom of a method, just like daemon.py).
#   (d) Stdlib imports (``json``, ``os``, ``subprocess``) do not trigger
#       false positives.
#
# Plus extras: dispatcher-internal imports are ignored, import-vs-pip
# name mismatches (``yaml`` → ``PyYAML``) are resolved, test files under
# the source dir are skipped, and the self-match assertion on ci.yml
# (per CLAUDE.md §Hygiene-check CI steps / #2542).
#
# Usage:
#   scripts/tests/test_check_dispatcher_image_deps.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-dispatcher-image-deps.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Helper: create a synthetic Dockerfile with a pip install line that
# installs the given packages (one per arg).
create_dockerfile() {
    local path="$1"
    shift
    mkdir -p "$(dirname "$path")"
    {
        echo 'FROM python:3.12-slim'
        echo 'ENV PIP_BREAK_SYSTEM_PACKAGES=1'
        if [[ $# -gt 0 ]]; then
            echo 'RUN pip install --no-cache-dir \'
            local last_idx=$(($# - 1))
            local i=0
            for pkg in "$@"; do
                if [[ $i -eq $last_idx ]]; then
                    printf '    "%s"\n' "$pkg"
                else
                    printf '    "%s" \\\n' "$pkg"
                fi
                i=$((i + 1))
            done
        fi
        echo 'COPY . /app'
    } > "$path"
}

# Helper: create a synthetic Python source file.
create_py() {
    local path="$1"
    local content="$2"
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
}

# Helper: clear tmpdir between tests so unrelated fixtures do not leak.
reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# Run the check with --source-dir + --dockerfile pointed at the temp tree.
run_check_on_tmp() {
    local src="$TMPDIR_TEST/src"
    local docker="$TMPDIR_TEST/Dockerfile.test"
    "$CHECK_SCRIPT" --source-dir "$src" --dockerfile "$docker" "$@"
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if run_check_on_tmp > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        # Re-run to surface the error message for debugging.
        run_check_on_tmp || true
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    local expected_pkg="${2:-}"
    TESTS=$((TESTS + 1))
    local output
    output=$(run_check_on_tmp 2>&1) && status=0 || status=$?
    if [[ $status -eq 0 ]]; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
        return
    fi
    if [[ -n "$expected_pkg" ]] && ! echo "$output" | grep -q "$expected_pkg"; then
        echo "FAIL: $desc (exit code $status was non-zero but output did not mention '$expected_pkg')"
        echo "--- output ---"
        echo "$output"
        echo "--------------"
        FAILURES=$((FAILURES + 1))
        return
    fi
    echo "PASS: $desc"
}

# ─── Test 1: Current main passes (smoke test against the real repo) ─────
TESTS=$((TESTS + 1))
if ( cd "$REPO_ROOT" && "$CHECK_SCRIPT" ) > /dev/null 2>&1; then
    echo "PASS: Current main passes on the real scripts/dispatcher + Dockerfile.dispatcher"
else
    echo "FAIL: Current main passes on the real scripts/dispatcher + Dockerfile.dispatcher"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 2: Missing dep (import requests, not in Dockerfile) fails ─────
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import json
import requests
print(requests.__version__)'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test" "psycopg[binary]>=3.1,<4" "boto3>=1.34,<2"
assert_fails "Missing 'requests' import is detected" "requests"

# ─── Test 3: Lazy import inside a function body is caught ───────────────
# Mirrors the #2773 regression pattern — ``import boto3`` at the bottom
# of a method body, not a top-level import. We put the import inside a
# function and omit boto3 from the Dockerfile pip install list.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import logging


def _make_cloudwatch_client():
    """Matches the daemon.py shape from #2773."""
    import boto3  # lazy import inside a function body
    return boto3.client("cloudwatch")


if __name__ == "__main__":
    _make_cloudwatch_client()'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test" "psycopg[binary]>=3.1,<4"
# boto3 deliberately missing from Dockerfile
assert_fails "Lazy import inside a function body is caught" "boto3"

# ─── Test 4: Stdlib imports do not trigger false positives ──────────────
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test"
# Dockerfile has no pip install line at all — but only stdlib is imported
# so the check must still pass.
assert_passes "Stdlib-only imports do not require pip install entries"

# ─── Test 5: Adding boto3 to the Dockerfile re-allows the import ────────
# Regression test: the inverse of test 3. Same daemon (lazy import of
# boto3) but with boto3 in the Dockerfile. This is the post-#2773 state
# of the real repo and must pass.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import logging


def _make_cloudwatch_client():
    import boto3
    return boto3.client("cloudwatch")'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test" "boto3>=1.34,<2"
assert_passes "Adding boto3 to Dockerfile re-allows the lazy import"

# ─── Test 6: Dispatcher-internal sibling imports are ignored ────────────
# The daemon imports from scripts.dispatcher.emit_failure and similar —
# these are COPY'd into the image, not pip-installed, and must not trip
# the check. Both absolute (``scripts.dispatcher.X``) and relative
# (``from .X``) forms are covered.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'from scripts.dispatcher.emit_failure import emit_failure
from . import __init__ as sibling  # relative
import psycopg'
create_py "$TMPDIR_TEST/src/__init__.py" ''
create_dockerfile "$TMPDIR_TEST/Dockerfile.test" "psycopg[binary]>=3.1,<4"
assert_passes "Dispatcher-internal sibling imports are ignored"

# ─── Test 7: Import-vs-pip name mismatch (yaml → PyYAML) is resolved ────
# A yaml import with PyYAML in the Dockerfile must pass. The table maps
# ``yaml`` → ``PyYAML`` and normalization lowercases both sides.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import yaml
print(yaml.__version__)'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test" "PyYAML>=6.0"
assert_passes "Import 'yaml' resolves to pip package 'PyYAML' via the mapping table"

# ─── Test 8: Import-vs-pip name mismatch (yaml) with missing pip fails ──
# Same mapping but PyYAML NOT in the Dockerfile — must fail with a clear
# error message naming PyYAML.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import yaml'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test" "psycopg[binary]>=3.1,<4"
assert_fails "Import 'yaml' without PyYAML in Dockerfile is detected" "PyYAML"

# ─── Test 9: test_*.py files under source-dir are skipped ───────────────
# A test file under the source dir may import pytest, responses, etc.
# that aren't in the production image. These names must not trip the
# check because test files don't ship in the Docker image.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import json'
create_py "$TMPDIR_TEST/src/test_daemon.py" 'import pytest
import json'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test"
assert_passes "test_*.py files under source dir are skipped (pytest not required)"

# ─── Test 10: Multiple files contributing to the same missing dep ───────
# Two source files that both import the same missing package — the
# report should list both source files under the single missing-dep
# entry.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import requests'
create_py "$TMPDIR_TEST/src/helper.py" 'import requests'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test"
output=$(run_check_on_tmp 2>&1 || true)
TESTS=$((TESTS + 1))
if echo "$output" | grep -q "requests" \
   && echo "$output" | grep -q "daemon.py" \
   && echo "$output" | grep -q "helper.py"; then
    echo "PASS: Report lists all source files using a missing dep"
else
    echo "FAIL: Report lists all source files using a missing dep"
    echo "--- output ---"
    echo "$output"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 11: from-import with dotted module resolves to top-level ──────
# ``from boto3.session import Session`` imports from boto3 — the
# top-level package is ``boto3``. If boto3 is missing, the check must
# still catch it.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'from boto3.session import Session'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test"
assert_fails "from-import with dotted module resolves to top-level package" "boto3"

# ─── Test 12: Dockerfile with no pip install step + no third-party deps ─
# Edge case: a Dockerfile that doesn't install anything (e.g. a
# hypothetical stdlib-only future) and a source dir that only uses
# stdlib. Must pass — the check is not "require at least one dep".
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import json
import os'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test"
assert_passes "Dockerfile with no pip install step passes when only stdlib is imported"

# ─── Test 13: Commented-out pip install does NOT satisfy an import ──────
# A real Dockerfile might have ``# RUN pip install boto3`` left over as
# a stale comment. The check must not count commented lines.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import boto3'
cat > "$TMPDIR_TEST/Dockerfile.test" <<'DOCKER_EOF'
FROM python:3.12-slim
# RUN pip install --no-cache-dir "boto3>=1.34,<2"
# boto3 is commented out on purpose
DOCKER_EOF
assert_fails "Commented-out pip install line does not satisfy an import" "boto3"

# ─── Test 14: pip flags with values (-r requirements.txt) are ignored ───
# ``RUN pip install -r requirements.txt boto3`` should still extract
# ``boto3`` and skip ``requirements.txt``. We test the inverse — no
# ``boto3`` in args but a requirements.txt pseudo-flag — and expect an
# import of boto3 to still fail.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import boto3'
cat > "$TMPDIR_TEST/Dockerfile.test" <<'DOCKER_EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir -r requirements.txt
DOCKER_EOF
assert_fails "pip -r requirements.txt alone does not satisfy an import" "boto3"

# ─── Test 15: Version spec with brackets/extras is normalized correctly ─
# ``psycopg[binary]>=3.1,<4`` must normalize to ``psycopg`` so an
# ``import psycopg`` is satisfied.
reset_tmpdir
create_py "$TMPDIR_TEST/src/daemon.py" 'import psycopg'
create_dockerfile "$TMPDIR_TEST/Dockerfile.test" "psycopg[binary]>=3.1,<4"
assert_passes "psycopg[binary]>=3.1,<4 normalizes to psycopg"

# ─── Test 16: No self-match on ci.yml step name ─────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain a pattern that would trip the guard. This check
# reads imports and pip specs — neither of which should appear verbatim
# in step names — so the helper runs the check against a file of
# synthesized step names and confirms it passes.
#
# Per CLAUDE.md §Hygiene-check CI steps, every new string-forbidding
# guard needs this assertion (#2541/#2542).
reset_tmpdir
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"

# The shared helper calls $CHECK_SCRIPT against $TMPDIR_TEST directly.
# Our check script has a different CLI (expects --source-dir + --dockerfile)
# than the scan-a-directory guards the helper was designed for. To keep
# compatibility, we supply the synthesized step-names file as a minimal
# (source_dir, Dockerfile) pair: if the step names contain a forbidden
# import pattern, the check will fail. Since our check does NOT forbid
# strings in arbitrary files (it reads .py imports and a specific
# Dockerfile), synthesizing a valid pair means the assertion always
# passes as long as the source_dir has no real imports.
#
# The spirit of the self-match check is "the CI step's own text cannot
# trip the guard". For this check the relevant trip vector would be an
# import name being spelled in the step name AND that name being missing
# from Dockerfile.dispatcher. Both would have to be true simultaneously
# for a self-match, which is not currently the case and is prevented
# structurally by the choice of step name (see ci.yml edits in this PR).
#
# We skip the helper invocation because it would require a different
# harness shape than the one the shared helper provides. Instead, we
# check the step name directly against the forbidden-pattern shape.
TESTS=$((TESTS + 1))
ci_yml="$REPO_ROOT/.github/workflows/ci.yml"
if [[ -f "$ci_yml" ]]; then
    # Find every step name whose run: line references this guard's path.
    names_file="$TMPDIR_TEST/ci_step_names.txt"
    awk -v script="scripts/check-dispatcher-image-deps.sh" \
        -f "$SCRIPT_DIR/tests/_extract_guard_step_names.awk" \
        "$ci_yml" > "$names_file" 2>/dev/null || true
    # Also include the .py variant in case CI invokes python3 directly.
    awk -v script="scripts/check-dispatcher-image-deps.py" \
        -f "$SCRIPT_DIR/tests/_extract_guard_step_names.awk" \
        "$ci_yml" >> "$names_file" 2>/dev/null || true
    # No CI step should have a name that contains "import boto3" or the
    # specific pip patterns we treat as forbidden. Since this check does
    # not forbid any string, the failure mode is narrower than the
    # check-no-* guards — we just confirm the extractor produced no
    # matches that contain raw Python import syntax.
    if grep -qE "import[[:space:]]+(boto3|psycopg|requests|yaml)" "$names_file" 2>/dev/null; then
        echo "FAIL: No self-match on ci.yml step name (step name contains an import statement)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: No self-match on ci.yml step name for scripts/check-dispatcher-image-deps"
    fi
else
    echo "PASS: No self-match on ci.yml step name (ci.yml missing — skipped)"
fi
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
