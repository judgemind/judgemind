#!/usr/bin/env bash
# Send a Telegram notification to all allowed users.
#
# Reads the bot token and user IDs from AWS Secrets Manager
# (secret: judgemind/telegram/bot) and sends a plain-text message
# via the Telegram Bot API sendMessage endpoint.
#
# Usage:
#   scripts/notify-telegram.sh "Your message here"
#   echo "multi-line message" | scripts/notify-telegram.sh -
#   scripts/notify-telegram.sh --message-file /path/to/file.txt
#
# Environment:
#   AWS credentials must be configured (for Secrets Manager access).
#   AWS_REGION defaults to us-west-2 if not set.
#
# Exit codes:
#   0  — message sent successfully to at least one recipient (or no recipients configured)
#   1  — usage error or secret retrieval failure
#   2  — all sends failed (logged to stderr)

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SECRET_ID="${TELEGRAM_SECRET_ID:-judgemind/telegram/bot}"
AWS_REGION="${AWS_REGION:-us-west-2}"

usage() {
    echo "Usage: $SCRIPT_NAME <message>"
    echo "       $SCRIPT_NAME -                      (read from stdin)"
    echo "       $SCRIPT_NAME --message-file <path>   (read from file)"
    exit 1
}

# Parse arguments
MESSAGE=""
if [ $# -eq 0 ]; then
    usage
elif [ "$1" = "--message-file" ]; then
    [ $# -ge 2 ] || usage
    MESSAGE="$(cat "$2")"
elif [ "$1" = "-" ]; then
    MESSAGE="$(cat)"
else
    MESSAGE="$1"
fi

if [ -z "$MESSAGE" ]; then
    echo "$SCRIPT_NAME: empty message, nothing to send" >&2
    exit 0
fi

# Fetch the Telegram secret from Secrets Manager
SECRET_JSON="$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_ID" \
    --region "$AWS_REGION" \
    --query SecretString \
    --output text 2>/dev/null)" || {
    echo "$SCRIPT_NAME: failed to fetch Telegram secret — skipping notification" >&2
    exit 0
}

# Extract bot token and user IDs using Python (available on all GHA runners)
BOT_TOKEN="$(echo "$SECRET_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('bot_token',''))")"
USER_IDS="$(echo "$SECRET_JSON" | python3 -c "import sys,json; print(' '.join(str(x) for x in json.load(sys.stdin).get('allowed_user_ids',[])))")"

if [ -z "$BOT_TOKEN" ]; then
    echo "$SCRIPT_NAME: bot token is empty — Telegram not configured, skipping" >&2
    exit 0
fi

if [ -z "$USER_IDS" ]; then
    echo "$SCRIPT_NAME: no allowed_user_ids configured — skipping" >&2
    exit 0
fi

# JSON-escape the message text for the Telegram API payload.
ESCAPED_TEXT="$(echo "$MESSAGE" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")"

# Send message to each user
SEND_FAILURES=0
for CHAT_ID in $USER_IDS; do
    # Build the JSON payload in a temp file to avoid quoting issues.
    PAYLOAD_FILE="$(mktemp)"
    cat > "$PAYLOAD_FILE" <<PAYLOAD_EOF
{"chat_id": ${CHAT_ID}, "text": ${ESCAPED_TEXT}, "parse_mode": "HTML", "disable_web_page_preview": true}
PAYLOAD_EOF

    HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d @"$PAYLOAD_FILE")" || true

    rm -f "$PAYLOAD_FILE"

    if [ "$HTTP_CODE" = "200" ]; then
        echo "$SCRIPT_NAME: sent to chat_id=$CHAT_ID (HTTP $HTTP_CODE)"
    else
        echo "$SCRIPT_NAME: failed to send to chat_id=$CHAT_ID (HTTP $HTTP_CODE)" >&2
        SEND_FAILURES=$((SEND_FAILURES + 1))
    fi
done

# Count total recipients for comparison
TOTAL_RECIPIENTS=0
for _ in $USER_IDS; do
    TOTAL_RECIPIENTS=$((TOTAL_RECIPIENTS + 1))
done

if [ "$SEND_FAILURES" -eq "$TOTAL_RECIPIENTS" ] && [ "$TOTAL_RECIPIENTS" -gt 0 ]; then
    echo "$SCRIPT_NAME: all $SEND_FAILURES send(s) failed" >&2
    exit 2
elif [ "$SEND_FAILURES" -gt 0 ]; then
    echo "$SCRIPT_NAME: $SEND_FAILURES of $TOTAL_RECIPIENTS send(s) failed (partial delivery)" >&2
fi

exit 0
