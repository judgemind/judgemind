#!/usr/bin/env bash
# SessionStart hook: ensure Python 3.12 is available for local test runs.
#
# The codebase uses PEP 695 type parameter syntax which requires Python 3.12+.
# Many CI environments and dev machines ship with 3.11 by default, causing
# agents to fail when they try to run pytest locally.
#
# This script is idempotent — safe to run every session. If python3.12 is
# already on PATH it exits immediately.  If installation fails the session
# continues with a warning (non-blocking).
#
# Ref: https://github.com/judgemind/judgemind/issues/80

set -uo pipefail

# -------------------------------------------------------------------
# 1. Check if python3.12 is already available
# -------------------------------------------------------------------
if command -v python3.12 &>/dev/null; then
    echo "session-start hook: python3.12 is available ($(python3.12 --version))" >&2
    exit 0
fi

echo "session-start hook: python3.12 not found on PATH — attempting install..." >&2

# -------------------------------------------------------------------
# 2. Install via deadsnakes PPA (Ubuntu/Debian)
# -------------------------------------------------------------------
install_deadsnakes() {
    if ! command -v apt-get &>/dev/null; then
        echo "session-start hook: apt-get not available; cannot install python3.12 automatically" >&2
        return 1
    fi

    # Ensure add-apt-repository is available
    if ! command -v add-apt-repository &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq software-properties-common >/dev/null 2>&1 || return 1
    fi

    add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || return 1
    apt-get update -qq >/dev/null 2>&1 || return 1
    apt-get install -y -qq python3.12 python3.12-venv python3.12-dev >/dev/null 2>&1 || return 1
    return 0
}

if install_deadsnakes; then
    if command -v python3.12 &>/dev/null; then
        echo "session-start hook: python3.12 installed successfully ($(python3.12 --version))" >&2
        exit 0
    fi
fi

# -------------------------------------------------------------------
# 3. Graceful fallback — warn but don't block the session
# -------------------------------------------------------------------
echo "session-start hook: WARNING — could not install python3.12." >&2
echo "  You may need to install it manually before running tests." >&2
echo "  On Ubuntu/Debian: sudo apt-get install python3.12 python3.12-venv python3.12-dev" >&2
echo "  On macOS: brew install python@3.12" >&2
exit 0
