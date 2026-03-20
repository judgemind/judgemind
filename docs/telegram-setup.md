# Telegram Bridge — Setup Guide

This guide walks through setting up the Judgemind Telegram bridge from scratch. The bridge is optional — the platform works without it. When configured, it lets agents send lifecycle notifications (task started, completed, failed) and receive inbound commands (status, start, stop, pause, resume) via a Telegram bot.

## Prerequisites

- AWS CLI configured with access to the Judgemind account (155326049300, us-west-2)
- Terraform installed and initialized (`infra/terraform/`)
- A Telegram account

## Step 1 — Create a Telegram bot via BotFather

1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts:
   - Choose a display name (e.g. "Judgemind Agent").
   - Choose a username ending in `bot` (e.g. `judgemind_agent_bot`).
3. BotFather will reply with a **bot token** — a string like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`. Copy it. Do not share it or commit it to the repo.

## Step 2 — Get your Telegram user ID

The bridge uses an allowlist so only authorized users can send commands to the bot. You need your numeric Telegram user ID.

1. Search for [@userinfobot](https://t.me/userinfobot) on Telegram.
2. Send it any message. It will reply with your user ID (a number like `123456789`).
3. Note this number — you will add it to the secret in the next step.

## Step 3 — Store the bot token and user ID in Secrets Manager

The Terraform module creates a placeholder secret at `judgemind/telegram/bot`. Populate it with the real values:

```bash
aws secretsmanager put-secret-value \
    --secret-id judgemind/telegram/bot \
    --secret-string '{"bot_token": "<YOUR_BOT_TOKEN>", "allowed_user_ids": [<YOUR_USER_ID>]}'
```

Replace `<YOUR_BOT_TOKEN>` with the token from Step 1 and `<YOUR_USER_ID>` with the numeric ID from Step 2. Multiple user IDs can be added as a JSON array (e.g. `[111111, 222222]`).

**Secret JSON structure:**

| Key                | Type       | Description                              |
|--------------------|------------|------------------------------------------|
| `bot_token`        | string     | Telegram bot token from BotFather        |
| `allowed_user_ids` | list[int]  | Telegram user IDs authorized to send commands |

## Step 4 — Deploy infrastructure

The Telegram bot infrastructure is defined in the `telegram_bot` Terraform module (`infra/terraform/modules/telegram-bot/`). If it has not been applied yet:

```bash
cd infra/terraform
terraform init
terraform apply -target=module.telegram_bot
```

This creates:
- **Lambda function** (`judgemind-telegram-webhook-dev`) — receives Telegram webhook POSTs, validates the sender, and enqueues messages to SQS.
- **API Gateway** (`judgemind-telegram-webhook-dev`) — HTTP API that routes `POST /webhook` to the Lambda.
- **SQS queue** (`judgemind-telegram-inbound-dev`) — buffer between the webhook and the agent's command polling.
- **Secrets Manager secret** (`judgemind/telegram/bot`) — the secret you populated in Step 3.

After applying, note the API Gateway endpoint URL from the Terraform output:

```bash
terraform output telegram_webhook_url
```

## Step 5 — Deploy the Lambda code

The Terraform module creates the Lambda with a placeholder. Deploy the real handler:

```bash
cd infra/telegram-bot
pip install -t package/ boto3  # boto3 is available in Lambda runtime, but needed locally
cd package && zip -r ../handler.zip . && cd ..
zip handler.zip handler.py
aws lambda update-function-code \
    --function-name judgemind-telegram-webhook-dev \
    --zip-file fileb://handler.zip
```

## Step 6 — Register the webhook URL with Telegram

Tell Telegram to send updates to your API Gateway endpoint. The **full webhook URL** must include the `/webhook` path suffix — the bare API Gateway URL will return 404.

The URL format is:

```
https://<api-gateway-id>.execute-api.us-west-2.amazonaws.com/webhook
```

For example: `https://abc123def4.execute-api.us-west-2.amazonaws.com/webhook`

You can get it from Terraform output:

```bash
terraform -chdir=infra/terraform/environments/dev output -raw telegram_webhook_url
```

### Recommended: use the helper script

The easiest way to register the webhook is the helper script, which reads both the bot token (from Secrets Manager) and the API Gateway URL (from Terraform output) automatically:

```bash
scripts/tg-set-webhook.sh
```

Options:
- `--dry-run` — print the URL that would be set, without calling setWebhook
- `--verify` — after setting, call `getWebhookInfo` to confirm the URL matches

You can also override either value via environment variables:

```bash
TG_BOT_TOKEN=<token> TG_WEBHOOK_URL=<url> scripts/tg-set-webhook.sh
```

### Manual method

If you prefer to set the webhook manually:

```bash
curl -s -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
    -H "Content-Type: application/json" \
    -d '{"url": "<WEBHOOK_URL>"}'
```

Replace `<YOUR_BOT_TOKEN>` with the token from Step 1 and `<WEBHOOK_URL>` with the **full URL including `/webhook`**.

You should get a response like:

```json
{"ok": true, "result": true, "description": "Webhook was set"}
```

To verify the webhook is registered:

```bash
curl -s "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getWebhookInfo"
```

**Common mistake:** omitting the `/webhook` path suffix. The bare API Gateway URL (without `/webhook`) returns 404, and Telegram messages will be silently dropped. Always check with `getWebhookInfo` after setting.

## Step 7 — Test the integration

1. Open your Telegram bot (search for the username you chose in Step 1).
2. Send `status` as a message.
3. Check the Lambda logs to verify the message was received and enqueued:
   ```bash
   aws logs tail /aws/lambda/judgemind-telegram-webhook-dev --since 5m
   ```
4. Verify the message arrived in SQS:
   ```bash
   aws sqs get-queue-attributes \
       --queue-url https://sqs.us-west-2.amazonaws.com/155326049300/judgemind-telegram-inbound-dev \
       --attribute-names ApproximateNumberOfMessages
   ```

If the message count is greater than 0, the webhook pipeline is working end-to-end.

## Supported Commands

Once the bridge is active, the dispatcher polls SQS and recognizes these commands:

| Command        | Action                                               |
|----------------|------------------------------------------------------|
| `status`       | Replies with a summary of running tasks              |
| `start #N`     | Spawns a `/task #N` agent for that issue             |
| `stop #N`      | Notes the stop request; avoids spawning more work    |
| `pause`        | Stops the dispatcher from spawning new task agents |
| `resume`       | Resumes normal task spawning                         |
| *(free text)*  | Forwarded to the dispatcher for interpretation     |

## Automated Testing

### Tier 1 — CI integration tests

The integration tests in `packages/telegram-bridge/tests/test_interpreter_e2e.py` validate the full interpreter-to-payload pipeline without touching Telegram. They run automatically in CI on every change to the telegram-bridge package (~5 seconds, no external dependencies).

What they cover:
- Interpreter output is valid JSON with correct action types
- Formatted replies are safe for Telegram HTML parse mode (no unescaped `<`, `>`, `&`)
- GitHub references (`#N`, `PR #N`) are linkified as clickable `<a>` tags
- Status cards render correctly
- Lambda handler correctly enqueues messages to SQS
- Edge cases: special characters, malformed JSON responses, multiple issue references

Run locally:
```bash
cd packages/telegram-bridge
.venv/bin/pytest tests/test_interpreter_e2e.py -v
```

### Tier 2 — End-to-end smoke test with test bot

The `scripts/tg-smoke-test.py` script validates the full pipeline by sending a real message through the Lambda webhook and verifying the bot's reply via the Telegram API.

#### Setting up a test bot

1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and create a test bot (e.g. `judgemind_test_bot`).
3. Copy the bot token.
4. Store the token in Secrets Manager:
   ```bash
   aws secretsmanager create-secret \
       --name judgemind/telegram/test-bot \
       --secret-string '{"bot_token": "<TEST_BOT_TOKEN>"}'
   ```
   Or update if the secret already exists:
   ```bash
   aws secretsmanager put-secret-value \
       --secret-id judgemind/telegram/test-bot \
       --secret-string '{"bot_token": "<TEST_BOT_TOKEN>"}'
   ```
5. Add the test bot's user ID to the production bot's `allowed_user_ids` in the `judgemind/telegram/bot` secret so the Lambda accepts messages from it.
6. Get the test bot's user ID by sending a message to it and checking the webhook logs, or use [@userinfobot](https://t.me/userinfobot).

#### Running the smoke test

```bash
# Using Secrets Manager for the test bot token:
TG_TEST_USER_ID=<test_bot_user_id> \
TG_TEST_CHAT_ID=<chat_id> \
WEBHOOK_URL=<api_gateway_url>/webhook \
    scripts/tg-smoke-test.py

# Or with explicit token:
TG_TEST_BOT_TOKEN=<token> \
TG_TEST_USER_ID=<test_bot_user_id> \
TG_TEST_CHAT_ID=<chat_id> \
WEBHOOK_URL=<api_gateway_url>/webhook \
    scripts/tg-smoke-test.py

# Validate configuration only (no messages sent):
scripts/tg-smoke-test.py --dry-run --webhook-url <url>

# Custom timeout:
scripts/tg-smoke-test.py --timeout 60
```

The script exits 0 on success, 1 on test failure, and 2 on configuration error.

#### What it checks

1. Webhook accepts the synthetic payload (HTTP 200)
2. Bot replies within the timeout (default 30 seconds)
3. Reply text is non-empty and non-trivial
4. GitHub issue references in the reply are rendered as clickable links (text_link entities)

## Troubleshooting

**Bot doesn't respond to messages:**
- Check that the webhook is registered: `getWebhookInfo` should show your URL.
- Check Lambda logs for errors.
- Verify your user ID is in the `allowed_user_ids` array in the secret.

**Messages are enqueued but agents don't see them:**
- Ensure the SQS queue URL is passed to `TelegramBridge(sqs_queue_url=...)` or `create_orchestrator_bridge(sqs_queue_url=...)`.
- Check that the agent's IAM role has `sqs:ReceiveMessage` and `sqs:DeleteMessage` permissions on the queue.

**Secret not found:**
- Verify the secret exists: `aws secretsmanager describe-secret --secret-id judgemind/telegram/bot`.
- If the Terraform module hasn't been applied, the secret won't exist yet. Run `terraform apply -target=module.telegram_bot`.
