# telegram-bridge

Python client library for bidirectional communication between Judgemind's dispatcher agents and the maintainer via Telegram. Fully opt-in -- if no Telegram credentials are configured, all methods silently become no-ops.

## Key Entry Points

- **`src/telegram_bridge/client.py`** -- `TelegramBridge` class: async client for sending Telegram notifications and polling for inbound commands. Thread-safe.
- **`src/telegram_bridge/dispatcher.py`** -- `DispatcherBridge`: higher-level interface for the dispatcher agent. Manages command queues, pending replies, and status cards. (The old `orchestrator.py` is a backward-compat shim.)
- **`src/telegram_bridge/interpreter.py`** -- Natural language message interpretation using Claude. Converts free-text Telegram messages into structured dispatcher commands.
- **`src/telegram_bridge/formatting.py`** -- HTML formatting utilities for Telegram messages (escaping, GitHub ref linking, message splitting for the 4096-char limit).
- **`src/telegram_bridge/validation.py`** -- Payload validation for inbound Telegram webhook events.

## What It Consumes (Inputs)

- **AWS Secrets Manager** -- Bot token and chat ID from `judgemind/telegram/bot` secret.
- **AWS SQS** -- Inbound message queue (messages forwarded from Telegram webhook via API Gateway + Lambda).
- **Telegram Bot API** -- Direct HTTPS calls for sending messages.
- **Anthropic API** -- Claude for natural language command interpretation.

## What It Produces (Outputs)

- **Telegram messages** -- Status updates, task notifications, and replies sent to the configured chat.
- **Structured commands** -- Parsed dispatcher instructions (start task, pause, resume, status query) from natural language input.

## Install, Test, and Run Locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Lint and format
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/

# Run tests
.venv/bin/pytest tests/ -v
```

See `docs/telegram-setup.md` for infrastructure setup and `docs/agent/telegram-reference.md` for the agent integration guide.
