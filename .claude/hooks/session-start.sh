#!/usr/bin/env bash
# SessionStart hook: ensure development environment is properly configured.
#
# This script is idempotent — safe to run every session.

set -uo pipefail

# -------------------------------------------------------------------
# 1. Ensure git hooks path points to .githooks/
# -------------------------------------------------------------------
# The pre-push hook lives in .githooks/ (committed to repo), not .git/hooks/.
# Without this, pre-push checks never run and broken code reaches CI.
CURRENT_HOOKS_PATH=$(git config core.hooksPath 2>/dev/null || true)
if [ "$CURRENT_HOOKS_PATH" != ".githooks" ]; then
    git config core.hooksPath .githooks
    echo "session-start hook: configured core.hooksPath → .githooks" >&2
fi

# -------------------------------------------------------------------
# 2. Check if python3.12 is already available
# -------------------------------------------------------------------
if command -v python3.12 &>/dev/null; then
    echo "session-start hook: python3.12 is available ($(python3.12 --version))" >&2
    exit 0
fi

echo "session-start hook: python3.12 not found on PATH — attempting install..." >&2

# -------------------------------------------------------------------
# 3. Install via deadsnakes PPA (Ubuntu/Debian)
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
# 4. Graceful fallback — warn but don't block the session
# -------------------------------------------------------------------
echo "session-start hook: WARNING — could not install python3.12." >&2
echo "  You may need to install it manually before running tests." >&2
echo "  On Ubuntu/Debian: sudo apt-get install python3.12 python3.12-venv python3.12-dev" >&2
echo "  On macOS: brew install python@3.12" >&2
exit 0
