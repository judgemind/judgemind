# Telegram Integration — Agent Reference

> **When to read this:** only when working on Telegram-related tasks, the dispatcher, or the responder daemon.

## Overview

The Telegram bridge lets agents send lifecycle notifications and receive inbound commands via a Telegram bot. It is **opt-in** — if the secret is missing or the bot token is empty, all bridge calls silently become no-ops and no existing workflows are affected.

**Architecture:** Telegram webhook POST -> API Gateway -> Lambda (`infra/telegram-bot/handler.py`) -> SQS queue. The Python client (`packages/telegram-bridge/`) reads commands from SQS and sends replies via the Telegram Bot API.

**Secrets Manager secret:** `judgemind/telegram/bot` — JSON structure:
```json
{
  "bot_token": "<Telegram bot token from BotFather>",
  "allowed_user_ids": [123456789]
}
```
The `allowed_user_ids` array controls who can send commands. Messages from unlisted users are silently dropped by the Lambda.

**Infrastructure (Terraform module `telegram_bot`):**
- Lambda: `judgemind-telegram-webhook-dev`
- API Gateway (HTTP): `judgemind-telegram-webhook-dev`
- SQS queue: `judgemind-telegram-inbound-dev`
- Secret: `judgemind/telegram/bot`

See `docs/telegram-setup.md` for end-to-end setup instructions.

## Session Triggers — Telegram Commands

When Telegram is configured (bot token in Secrets Manager `judgemind/telegram/bot` and SQS queue `judgemind-telegram-inbound-dev`), the dispatcher can receive inbound commands from Telegram and send lifecycle notifications. Use `packages/telegram-bridge/` — specifically the `OrchestratorBridge` class.

**Lifecycle notifications:** call `session_started()` when an interactive session begins, `task_started()` / `task_completed()` / `task_failed()` around `/task` agent invocations, and `session_ended()` when shutting down.

**Inbound messages:** All Telegram messages are interpreted as free text by a Claude API call (Opus) in the responder daemon. The daemon responds directly with natural-language replies and extracts actionable commands (start, pause, resume, stop) for the dispatcher. No special command syntax is required — users can write naturally.

The dispatcher uses `bridge.read_inbox()` to pick up commands from the file-based inbox. The responder daemon handles the interpretation and reply, so the dispatcher only sees pre-parsed actions.

If Telegram is not configured, all bridge calls are silent no-ops. No existing workflows are affected.

## Dispatcher Status File

The dispatcher must call `bridge.write_status()` after every state change (task start, complete, fail, pause, resume). This writes `tmp/dispatcher_status.json` containing:
- Active agents: issue number, title, worker number, phase
- Open PRs: number, CI status, mergeable
- Recently completed tasks: issue number, outcome
- Queue: next issues by priority
- Paused/stopped state

The responder daemon reads this file to provide context to the Claude interpreter, enabling it to give informed, specific answers about dispatcher state.

## Responder Daemon and State Files

The standalone **responder daemon** (`scripts/tg-responder.py`) interprets all Telegram messages via a Claude API call (Opus). It receives the user's message and the current dispatcher status, generates a natural-language reply, and extracts any actionable commands. It communicates with the dispatcher via shared state files:

- **`tmp/dispatcher_status.json`** — written by `DispatcherBridge.write_status()`. The responder reads this to provide context to the Claude interpreter. Contains active agents, open PRs, queue, and paused/stopped state.
- **`tmp/dispatcher_state.json`** — the responder writes `paused` flag changes here. The dispatcher must call `bridge.refresh_state()` before each spawn decision to pick up `pause`/`resume` changes made out-of-loop.
- **`tmp/stop_requests.json`** — the responder appends stop requests here (JSON array of `{"issue_number": N, "timestamp": "..."}`). The dispatcher reads and clears this file by calling `bridge.read_stop_requests()`, which returns newly stopped issue numbers and accumulates them in `bridge.stopped_issues`. Use `bridge.is_issue_stopped(N)` to check before spawning.
- **`tmp/tg_inbox.json`** — queued `start` commands extracted by the interpreter, read by `bridge.read_inbox()`.

**Dispatcher spawn loop pattern:**
1. Call `bridge.write_status()` to update the status file for the responder.
2. Call `bridge.refresh_state()` to pick up external `paused` changes.
3. Call `bridge.read_stop_requests()` to consume new stop requests.
4. Check `bridge.paused` — if `True`, skip spawning.
5. Before spawning issue `#N`, check `bridge.is_issue_stopped(N)` — if `True`, skip it.
6. Call `bridge.read_inbox()` to get inbound `start` commands.

**Secrets required:**
- `judgemind/telegram/bot` — bot token and allowed user IDs (existing)
- `judgemind/anthropic/api-key` — Anthropic API key for Claude interpreter, or set `ANTHROPIC_API_KEY` env var. If missing, the daemon falls back to simple acknowledgments.

To start the responder daemon: `scripts/tg-responder.py`. To stop it: `scripts/tg-stop-responder.sh`.

## Unattended Operation Patterns — Telegram

- **Telegram bridge notifications:** the `TelegramBridge` and `OrchestratorBridge` classes are async. In synchronous contexts, use `asyncio.run()` or schedule on an existing event loop. The bridge auto-initialises lazily on first use — no explicit setup needed beyond passing the secret ID and SQS queue URL. If the secret is missing or empty, all calls are silent no-ops, so it is safe to call unconditionally.
- **Stopping background daemons:** Use `scripts/tg-stop-responder.sh` to stop the Telegram responder daemon. It reads the PID file, sends SIGTERM, waits up to 10 seconds for graceful shutdown, and escalates to SIGKILL if needed. For new background daemons, follow the same PID file convention (`tmp/foo.pid`) and handle SIGTERM for graceful shutdown.
