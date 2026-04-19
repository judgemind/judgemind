#!/usr/bin/env bash
# Probe API keys for dispatcher v2 spike 0.4 without leaking values.
set -euo pipefail
for var in GOOGLE_API_KEY GEMINI_API_KEY GOOGLE_GENAI_USE_VERTEXAI GOOGLE_GENAI_USE_GCA ANTHROPIC_API_KEY; do
    val="${!var:-}"
    if [[ -n "${val}" ]]; then
        echo "${var}: SET (len=${#val})"
    else
        echo "${var}: unset"
    fi
done
