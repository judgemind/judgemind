#!/usr/bin/env bash
# Dump gemini CLI help in full — for spike 0.4 flag discovery.
set -euo pipefail
GEMINI_BIN="${GEMINI_BIN:-$(command -v gemini || true)}"
if [[ -z "${GEMINI_BIN}" ]]; then
    echo "gemini not found" >&2
    exit 2
fi
echo "=== gemini --help (full) ==="
"${GEMINI_BIN}" --help 2>&1 || true
echo
echo "=== gemini hooks --help ==="
"${GEMINI_BIN}" hooks --help 2>&1 || true
echo
echo "=== gemini hooks migrate --help ==="
"${GEMINI_BIN}" hooks migrate --help 2>&1 || true
echo
echo "=== search for turn-related flags ==="
"${GEMINI_BIN}" --help 2>&1 | grep -iE "turn|iteration|step|max" || echo "(no turn-related options in top-level help)"
