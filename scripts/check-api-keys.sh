#!/usr/bin/env bash
# check-api-keys.sh — Verify external API keys are valid and have quota.
#
# Sends a minimal request to each provider to confirm the key works.
# Uses scripts/with-secret.sh for key injection — keys never appear in output.
#
# Usage:
#   scripts/check-api-keys.sh [--provider anthropic|google] [--verbose]
#
# Options:
#   --provider NAME   Check only the named provider (can be repeated)
#   --verbose         Print response details on failure
#
# Checks:
#   - Anthropic (judgemind/anthropic/api-key): lists models via /v1/models
#   - Google GenAI (judgemind/google/api-key): lists models via generativelanguage API
#
# Exit codes:
#   0  All checked keys are valid and working
#   1  One or more keys failed (auth error, quota exceeded, network error)
#   2  All requested providers were skipped (secrets not found)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="us-west-2"

# ─── Internal dispatch ───────────────────────────────────────────────────────
# When called with --_check-anthropic or --_check-google, the script is running
# inside with-secret.sh and the API key is in an environment variable. Perform
# the actual HTTP check and exit with the HTTP status code (or 0/1).

if [[ "${1:-}" == "--_check-anthropic" ]]; then
    # ANTHROPIC_API_KEY is set by with-secret.sh
    exec curl -s -w "\n%{http_code}" \
        -H "x-api-key: ${ANTHROPIC_API_KEY}" \
        -H "anthropic-version: 2023-06-01" \
        "https://api.anthropic.com/v1/models?limit=1"
fi

if [[ "${1:-}" == "--_check-google" ]]; then
    # GOOGLE_API_KEY is set by with-secret.sh
    exec curl -s -w "\n%{http_code}" \
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key=${GOOGLE_API_KEY}"
fi

# ─── Normal entry point ─────────────────────────────────────────────────────

ERRORS=0
SKIPPED=0
CHECKED=0
VERBOSE=false

# Which providers to check (empty = all)
declare -a PROVIDERS=()

# ─── Parse arguments ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)
            if [[ $# -lt 2 ]]; then
                echo "Error: --provider requires an argument" >&2
                exit 1
            fi
            PROVIDERS+=("$2")
            shift 2
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            echo "Usage: scripts/check-api-keys.sh [--provider anthropic|google] [--verbose]" >&2
            exit 1
            ;;
    esac
done

# Default: check all providers
if [[ ${#PROVIDERS[@]} -eq 0 ]]; then
    PROVIDERS=(anthropic google)
fi

# ─── Helpers ─────────────────────────────────────────────────────────────────

pass() {
    echo "  PASS: $1"
}

fail() {
    echo "  FAIL: $1" >&2
    ERRORS=$((ERRORS + 1))
}

info() {
    echo "  INFO: $1"
}

# Check if a secret exists in Secrets Manager (without revealing its value).
# Returns 0 if it exists, 1 if not.
secret_exists() {
    local secret_id="$1"
    aws secretsmanager describe-secret \
        --secret-id "$secret_id" \
        --region "$REGION" \
        --query "Name" \
        --output text >/dev/null 2>&1
}

# Interpret the HTTP status code from a provider check and report pass/fail.
# Args: provider_name http_code response_body
interpret_status() {
    local provider="$1"
    local http_code="$2"
    local body="$3"

    case "$http_code" in
        200)
            pass "$provider API key is valid (HTTP 200)"
            ;;
        401)
            fail "$provider API key is invalid (HTTP 401 — authentication failed)"
            if "$VERBOSE"; then echo "    Response: $body" >&2; fi
            ;;
        403)
            fail "$provider API key is forbidden (HTTP 403 — check permissions or project)"
            if "$VERBOSE"; then echo "    Response: $body" >&2; fi
            ;;
        429)
            fail "$provider API key hit rate/quota limit (HTTP 429)"
            if "$VERBOSE"; then echo "    Response: $body" >&2; fi
            ;;
        "")
            fail "$provider API check failed — no HTTP response (network error or secret retrieval failed)"
            if "$VERBOSE"; then echo "    Raw output: $body" >&2; fi
            ;;
        *)
            fail "$provider API returned unexpected status (HTTP $http_code)"
            if "$VERBOSE"; then echo "    Response: $body" >&2; fi
            ;;
    esac
}

# ─── Check Anthropic ─────────────────────────────────────────────────────────

check_anthropic() {
    local secret_id="judgemind/anthropic/api-key"

    echo ""
    echo "--- Anthropic (${secret_id}) ---"

    if ! secret_exists "$secret_id"; then
        info "Secret '${secret_id}' not found in Secrets Manager — skipping"
        SKIPPED=$((SKIPPED + 1))
        return
    fi

    pass "Secret '${secret_id}' exists"
    CHECKED=$((CHECKED + 1))

    # with-secret.sh injects ANTHROPIC_API_KEY, then re-invokes this script
    # with the --_check-anthropic flag. curl output: body + "\n" + http_code.
    local response
    response=$("${SCRIPT_DIR}/with-secret.sh" \
        -e ANTHROPIC_API_KEY="$secret_id" \
        -- "$0" --_check-anthropic 2>&1) || true

    local http_code
    http_code=$(echo "$response" | tail -n1)
    local body
    body=$(echo "$response" | sed '$d')

    interpret_status "Anthropic" "$http_code" "$body"
}

# ─── Check Google GenAI ──────────────────────────────────────────────────────

check_google() {
    local secret_id="judgemind/google/api-key"

    echo ""
    echo "--- Google GenAI (${secret_id}) ---"

    if ! secret_exists "$secret_id"; then
        info "Secret '${secret_id}' not found in Secrets Manager — skipping"
        SKIPPED=$((SKIPPED + 1))
        return
    fi

    pass "Secret '${secret_id}' exists"
    CHECKED=$((CHECKED + 1))

    local response
    response=$("${SCRIPT_DIR}/with-secret.sh" \
        -e GOOGLE_API_KEY="$secret_id" \
        -- "$0" --_check-google 2>&1) || true

    local http_code
    http_code=$(echo "$response" | tail -n1)
    local body
    body=$(echo "$response" | sed '$d')

    interpret_status "Google GenAI" "$http_code" "$body"
}

# ─── Main ────────────────────────────────────────────────────────────────────

echo "=== API Key Health Check ==="

for provider in "${PROVIDERS[@]}"; do
    case "$provider" in
        anthropic)
            check_anthropic
            ;;
        google)
            check_google
            ;;
        *)
            echo "Error: unknown provider '$provider'. Valid: anthropic, google" >&2
            exit 1
            ;;
    esac
done

# ─── Summary ─────────────────────────────────────────────────────────────────

echo ""
echo "=== Summary ==="
echo "  Checked: $CHECKED provider(s)"
echo "  Skipped: $SKIPPED provider(s) (secret not found)"
echo "  Errors:  $ERRORS"

if [[ "$ERRORS" -gt 0 ]]; then
    echo ""
    echo "One or more API key checks failed." >&2
    exit 1
elif [[ "$CHECKED" -eq 0 ]]; then
    echo ""
    echo "No providers checked — all secrets were missing from Secrets Manager." >&2
    exit 2
else
    echo ""
    echo "All API keys are valid and working."
    exit 0
fi
