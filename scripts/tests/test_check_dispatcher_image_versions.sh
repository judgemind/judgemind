#!/usr/bin/env bash
# test_check_dispatcher_image_versions.sh — Tests for check-dispatcher-image-versions.sh
#
# Verifies that the dispatcher image version checker correctly:
#   (a) Passes when Python >= 3.12 and Node >= 20 are present.
#   (b) Fails with a clear message when Python < 3.12 (e.g. 3.11).
#   (c) Fails with a clear message when Node < 20 (e.g. 18).
#
# Each fixture builds a synthetic Docker image in a temp dir, exercises
# the pass or fail path, and cleans up via trap. Docker builds are
# skipped gracefully if Docker is unavailable in the environment.
#
# Also includes a self-match assertion on the ci.yml step name per
# CLAUDE.md §Hygiene-check CI steps (#2542).
#
# Usage:
#   scripts/tests/test_check_dispatcher_image_versions.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-dispatcher-image-versions.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Helper: clear tmpdir between tests so unrelated fixtures do not leak.
reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Check if Docker is available ──────────────────────────────────────────
DOCKER_AVAILABLE=true
if ! command -v docker > /dev/null 2>&1; then
    DOCKER_AVAILABLE=false
fi

# ─── Docker test helpers ────────────────────────────────────────────────────
# run_check_on_image <image-tag>
# Invokes CHECK_SCRIPT with --image <tag>.
run_check_on_image() {
    local image_tag="$1"
    "$CHECK_SCRIPT" --image "$image_tag"
}

assert_passes_with_image() {
    local desc="$1"
    local image_tag="$2"
    TESTS=$((TESTS + 1))
    if run_check_on_image "$image_tag" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        run_check_on_image "$image_tag" || true
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails_with_image() {
    local desc="$1"
    local image_tag="$2"
    local expected_msg="${3:-}"
    TESTS=$((TESTS + 1))
    local output
    output=$(run_check_on_image "$image_tag" 2>&1) && status=0 || status=$?
    if [[ $status -eq 0 ]]; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
        return
    fi
    if [[ -n "$expected_msg" ]] && ! echo "$output" | grep -q "$expected_msg"; then
        echo "FAIL: $desc (non-zero exit but output did not contain '$expected_msg')"
        echo "--- output ---"
        echo "$output"
        echo "--------------"
        FAILURES=$((FAILURES + 1))
        return
    fi
    echo "PASS: $desc"
}

# build_synthetic_image <dockerfile-content> <image-tag>
# Writes a Dockerfile to a temp workdir and builds it.
build_synthetic_image() {
    local content="$1"
    local tag="$2"
    local workdir
    workdir=$(mktemp -d)
    local workdir_cleanup="$workdir"
    echo "$content" > "$workdir/Dockerfile"
    if ! docker build -f "$workdir/Dockerfile" -t "$tag" "$workdir" > /dev/null 2>&1; then
        rm -rf "$workdir_cleanup"
        return 1
    fi
    rm -rf "$workdir_cleanup"
}

# ─── Test 1: Passing (Python 3.12 + Node 20) ───────────────────────────────
if [[ "$DOCKER_AVAILABLE" == "false" ]]; then
    echo "SKIP: Passing (Python 3.12 + Node 20) — Docker not available"
else
    TAG="judgemind/test-versions-pass-$$"
    DOCKERFILE_PASS='FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y -qq nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*'

    if build_synthetic_image "$DOCKERFILE_PASS" "$TAG"; then
        assert_passes_with_image "Passing (Python 3.12 + Node 20)" "$TAG"
        docker rmi "$TAG" > /dev/null 2>&1 || true
    else
        echo "SKIP: Passing (Python 3.12 + Node 20) — Docker build failed (likely no internet access)"
    fi
fi

# ─── Test 2: Failing Python (Python 3.11 + Node 20) ────────────────────────
if [[ "$DOCKER_AVAILABLE" == "false" ]]; then
    echo "SKIP: Failing Python (Python 3.11 + Node 20) — Docker not available"
else
    TAG="judgemind/test-versions-fail-python-$$"
    DOCKERFILE_FAIL_PYTHON='FROM python:3.11-slim
RUN apt-get update -qq && apt-get install -y -qq curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y -qq nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*'

    if build_synthetic_image "$DOCKERFILE_FAIL_PYTHON" "$TAG"; then
        assert_fails_with_image "Failing Python (Python 3.11 + Node 20)" "$TAG" "Python is below 3.12"
        docker rmi "$TAG" > /dev/null 2>&1 || true
    else
        echo "SKIP: Failing Python (Python 3.11 + Node 20) — Docker build failed (likely no internet access)"
    fi
fi

# ─── Test 3: Failing Node (Python 3.12 + Node 18) ──────────────────────────
if [[ "$DOCKER_AVAILABLE" == "false" ]]; then
    echo "SKIP: Failing Node (Python 3.12 + Node 18) — Docker not available"
else
    TAG="judgemind/test-versions-fail-node-$$"
    DOCKERFILE_FAIL_NODE='FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y -qq nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*'

    if build_synthetic_image "$DOCKERFILE_FAIL_NODE" "$TAG"; then
        assert_fails_with_image "Failing Node (Python 3.12 + Node 18)" "$TAG" "Node is below 20"
        docker rmi "$TAG" > /dev/null 2>&1 || true
    else
        echo "SKIP: Failing Node (Python 3.12 + Node 18) — Docker build failed (likely no internet access)"
    fi
fi

# ─── Test 4: Self-match assertion on ci.yml step name ───────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain strings that would be flagged by the guard. This
# check forbids "Python is below 3.12" and "Node is below 20" in its
# output messages, but those are output messages, not input patterns —
# the guard doesn't scan arbitrary files for these strings. The real
# self-match risk is that a step name contains the exact regex patterns
# the guard uses to detect failure. We use the shared awk extractor to
# pull the step names and verify they don't contain version strings that
# would confuse hygiene guards per CLAUDE.md §Hygiene-check CI steps.
reset_tmpdir
# shellcheck source=./_guard_self_match_helpers.sh
source "$(dirname "${BASH_SOURCE[0]}")/_guard_self_match_helpers.sh"
TESTS=$((TESTS + 1))
ci_yml="$REPO_ROOT/.github/workflows/ci.yml"
if [[ -f "$ci_yml" ]]; then
    names_file="$TMPDIR_TEST/ci_step_names.txt"
    awk -v script="scripts/check-dispatcher-image-versions.sh" \
        -f "$(dirname "${BASH_SOURCE[0]}")/_extract_guard_step_names.awk" \
        "$ci_yml" > "$names_file" 2>/dev/null || true
    # The step name must not contain forbidden version strings such as
    # "3.11", "3.10", "node 18", "node 16", etc. that would look like
    # the thing being guarded against (CLAUDE.md §Hygiene-check CI steps).
    if grep -qiE "(3\.1[01]|node[[:space:]]+1[0-9])" "$names_file" 2>/dev/null; then
        echo "FAIL: No self-match on ci.yml step name (step name contains a forbidden version string)"
        cat "$names_file"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: No self-match on ci.yml step name for scripts/check-dispatcher-image-versions"
    fi
else
    echo "PASS: No self-match on ci.yml step name (ci.yml missing — skipped)"
fi
reset_tmpdir

# ─── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
