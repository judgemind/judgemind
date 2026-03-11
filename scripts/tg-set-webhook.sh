#!/usr/bin/env bash
# tg-set-webhook.sh — Register the Telegram webhook URL with the Telegram API.
#
# Reads the bot token from Secrets Manager and the API Gateway URL from
# Terraform output, then calls setWebhook with the correct full URL
# (including the /webhook path). This eliminates manual URL construction.
#
# Usage:
#   scripts/tg-set-webhook.sh [--dry-run] [--verify]
#
# Options:
#   --dry-run   Print the webhook URL that would be set, without calling setWebhook.
#   --verify    After setting, call getWebhookInfo and print the result.
#
# Environment:
#   TG_BOT_TOKEN      Override bot token (skip Secrets Manager lookup).
#   TG_WEBHOOK_URL    Override webhook URL (skip Terraform output lookup).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
SECRET_ID="${TG_SECRET_ID:-judgemind/telegram/bot}"
TF_ENV_DIR="$REPO_ROOT/infra/terraform/environments/dev"
TELEGRAM_API="https://api.telegram.org"

DRY_RUN=false
VERIFY=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --verify) VERIFY=true ;;
        *)
            echo "Unknown option: $arg" >&2
            echo "Usage: scripts/tg-set-webhook.sh [--dry-run] [--verify]" >&2
            exit 1
            ;;
    esac
done

# ── Resolve bot token ──────────────────────────────────────────────────

if [[ -n "${TG_BOT_TOKEN:-}" ]]; then
    BOT_TOKEN="$TG_BOT_TOKEN"
    echo "Using bot token from TG_BOT_TOKEN env var."
else
    echo "Fetching bot token from Secrets Manager ($SECRET_ID)..."
    RAW_SECRET=$(aws secretsmanager get-secret-value \
        --secret-id "$SECRET_ID" \
        --query SecretString \
        --output text \
        --region "$REGION" 2>/dev/null) || {
        echo "Error: failed to retrieve secret '$SECRET_ID'." >&2
        echo "Make sure you have AWS credentials configured and the secret exists." >&2
        exit 1
    }

    BOT_TOKEN=$(echo "$RAW_SECRET" | python3 -c "
import sys, json
data = json.load(sys.stdin)
token = data.get('bot_token', '')
if not token:
    print('Error: bot_token is empty in secret', file=sys.stderr)
    sys.exit(1)
print(token, end='')
" 2>/dev/null) || {
        echo "Error: failed to extract bot_token from secret '$SECRET_ID'." >&2
        exit 1
    }
    echo "Bot token loaded from Secrets Manager."
fi

# ── Resolve webhook URL ────────────────────────────────────────────────

if [[ -n "${TG_WEBHOOK_URL:-}" ]]; then
    WEBHOOK_URL="$TG_WEBHOOK_URL"
    echo "Using webhook URL from TG_WEBHOOK_URL env var."
else
    echo "Fetching webhook URL from Terraform output..."
    if [[ ! -d "$TF_ENV_DIR" ]]; then
        echo "Error: Terraform environment directory not found: $TF_ENV_DIR" >&2
        echo "Set TG_WEBHOOK_URL env var to provide the URL manually." >&2
        exit 1
    fi

    WEBHOOK_URL=$(terraform -chdir="$TF_ENV_DIR" output -raw telegram_webhook_url 2>/dev/null) || {
        echo "Error: failed to get telegram_webhook_url from Terraform output." >&2
        echo "Make sure Terraform is initialized and the telegram_bot module is applied." >&2
        echo "Or set TG_WEBHOOK_URL env var to provide the URL manually." >&2
        exit 1
    }

    if [[ -z "$WEBHOOK_URL" ]]; then
        echo "Error: telegram_webhook_url output is empty." >&2
        exit 1
    fi
    echo "Webhook URL from Terraform: $WEBHOOK_URL"
fi

# Ensure the URL ends with /webhook
if [[ "$WEBHOOK_URL" != */webhook ]]; then
    echo "Warning: URL does not end with /webhook. Appending /webhook." >&2
    WEBHOOK_URL="${WEBHOOK_URL%/}/webhook"
fi

echo ""
echo "Webhook URL: $WEBHOOK_URL"
echo ""

# ── Set the webhook ────────────────────────────────────────────────────

if [[ "$DRY_RUN" == true ]]; then
    echo "[dry-run] Would call setWebhook with URL: $WEBHOOK_URL"
    exit 0
fi

echo "Registering webhook with Telegram..."

# Write the request body to a temp file to avoid quoting issues.
BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT
python3 -c "
import json, sys
json.dump({'url': sys.argv[1]}, sys.stdout)
" "$WEBHOOK_URL" > "$BODY_FILE"

RESPONSE=$(curl -s -X POST \
    "$TELEGRAM_API/bot$BOT_TOKEN/setWebhook" \
    -H Content-Type:application/json \
    -d @"$BODY_FILE")

echo "Response: $RESPONSE"
echo ""

# Check if the response indicates success.
OK=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print('true' if data.get('ok') else 'false', end='')
except Exception:
    print('false', end='')
")

if [[ "$OK" != "true" ]]; then
    echo "Error: setWebhook failed. See response above." >&2
    exit 1
fi

echo "Webhook registered successfully."

# ── Verify (optional) ─────────────────────────────────────────────────

if [[ "$VERIFY" == true ]]; then
    echo ""
    echo "Verifying webhook with getWebhookInfo..."
    INFO=$(curl -s "$TELEGRAM_API/bot$BOT_TOKEN/getWebhookInfo")
    echo "Webhook info: $INFO"

    REGISTERED_URL=$(echo "$INFO" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    result = data.get('result', {})
    print(result.get('url', ''), end='')
except Exception:
    print('', end='')
")

    if [[ "$REGISTERED_URL" == "$WEBHOOK_URL" ]]; then
        echo "Verified: webhook URL matches."
    else
        echo "Warning: registered URL ($REGISTERED_URL) does not match expected ($WEBHOOK_URL)." >&2
        exit 1
    fi
fi
