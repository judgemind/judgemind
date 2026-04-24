#!/usr/bin/env bash
# check-dispatcher-image-versions.sh — Verify dispatcher image Python ≥ 3.12 and Node ≥ 20.
#
# Builds (or accepts) a dispatcher Docker image and asserts that both
# Python and Node meet the minimum version requirements for the dispatcher
# daemon (#2972).
#
# Usage:
#   scripts/check-dispatcher-image-versions.sh [--build <Dockerfile>] [--image <tag>]
#
#   --build <Dockerfile>   Build the image from this Dockerfile (default: Dockerfile.dispatcher)
#   --image <tag>          Use an already-built image tag instead of building
#
# Exit codes:
#   0 — Python ≥ 3.12 AND Node ≥ 20 confirmed.
#   1 — Version requirement not met (clear FAIL message printed to stderr).
#
# venv: none
# permanent: true

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── Argument parsing ────────────────────────────────────────────────────────
MODE="build"
DOCKERFILE="Dockerfile.dispatcher"
IMAGE_TAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build)
            MODE="build"
            DOCKERFILE="$2"
            shift 2
            ;;
        --image)
            MODE="image"
            IMAGE_TAG="$2"
            shift 2
            ;;
        *)
            echo "Usage: $0 [--build <Dockerfile>] [--image <tag>]" >&2
            exit 1
            ;;
    esac
done

# ─── Build or use existing image ─────────────────────────────────────────────
if [[ "$MODE" == "build" ]]; then
    IMAGE_TAG="judgemind/dispatcher:ci-version-check-$$"
    echo "Building dispatcher image from $DOCKERFILE..."
    docker build -f "$REPO_ROOT/$DOCKERFILE" -t "$IMAGE_TAG" "$REPO_ROOT"
    # Clean up the image on exit
    cleanup() {
        docker rmi "$IMAGE_TAG" > /dev/null 2>&1 || true
    }
    trap cleanup EXIT
fi

FAILURES=0

# ─── Python version check ─────────────────────────────────────────────────────
python_version_output=$(docker run --rm --entrypoint python "$IMAGE_TAG" --version 2>&1)
if echo "$python_version_output" | grep -qE "^Python 3\.(1[2-9]|[2-9][0-9])\."; then
    echo "OK: $python_version_output"
else
    echo "FAIL: dispatcher image Python is below 3.12 (got: $python_version_output)" >&2
    FAILURES=$((FAILURES + 1))
fi

# ─── Node version check ───────────────────────────────────────────────────────
node_version_output=$(docker run --rm --entrypoint node "$IMAGE_TAG" --version 2>&1)
if echo "$node_version_output" | grep -qE "^v(2[0-9]|[3-9][0-9])\."; then
    echo "OK: node $node_version_output"
else
    echo "FAIL: dispatcher image Node is below 20 (got: $node_version_output)" >&2
    FAILURES=$((FAILURES + 1))
fi

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
