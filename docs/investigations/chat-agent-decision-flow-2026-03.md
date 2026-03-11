# Chat Agent Decision Flow — Bottleneck and Branching Analysis

**Issue:** #777
**Date:** 2026-03-11
**Status:** Complete

## Architecture Overview

The chat agent decision flow has three layers:

1. **Lambda webhook handler** (`infra/telegram-bot/handler.py`) — validates users, enqueues to SQS
2. **Responder daemon** (`scripts/tg-responder.py`) — polls SQS, calls Claude Haiku interpreter, dispatches actions
3. **Orchestrator** (`packages/telegram-bridge/src/telegram_bridge/orchestrator.py`) — reads file-based inbox, executes actions

Message flow: Telegram -> Lambda -> SQS -> Responder -> Claude Haiku -> (reply to Telegram + write to inbox files) -> Orchestrator reads inbox

## Findings

### 1. Dual command parsing creates confusion

**Problem:** Two independent parsing paths exist:

- **Interpreter path** (Claude Haiku): `interpret_message()` in `interpreter.py` handles all messages via LLM. Returns structured JSON with `reply` and `actions`.
- **Legacy parser path**: `parse_command()` in `orchestrator.py` uses regex to match `status`, `start #N`, `stop #N`, `pause`, `resume`. Still called by `OrchestratorBridge.poll_commands()` which reads directly from SQS.

The responder daemon uses the interpreter path and writes parsed commands to `tmp/tg_inbox.json`. The orchestrator reads this via `read_inbox()` which calls `_parse_inbox_entry()`. But `OrchestratorBridge.poll_commands()` (which polls SQS directly) still uses the old `parse_command()` regex parser.

**Impact:** If the orchestrator uses `poll_commands()` instead of `read_inbox()`, messages bypass Claude interpretation entirely. The two paths can also race on the same SQS messages — the responder daemon deletes messages after reading, but if `poll_commands()` runs first, it consumes the message before the responder sees it.

**Recommendation:** Remove the SQS-direct polling path from `OrchestratorBridge`. The responder daemon should be the sole SQS consumer. The orchestrator should only read from file-based inbox/state files.

### 2. "discuss" and "do" actions lack response feedback loop

**Problem:** When the interpreter classifies a message as `discuss` or `do`, the responder sends an immediate acknowledgment ("Passing your question to the orchestrator...") and queues the action to `tmp/tg_inbox.json`. The orchestrator reads this via `read_inbox()` and `handle_command()` sends another acknowledgment ("Forwarded to orchestrator for discussion.").

However, after the orchestrator actually processes the discussion or executes the action, there is no structured mechanism to send the result back to the user. The `handle_command()` method sets `result["needs_reply"] = True` but the orchestrator skill (SKILL.md) does not explicitly describe how to send the response.

**Impact:** The user receives two acknowledgments ("Passing your question..." + "Forwarded to orchestrator...") but may never receive the actual answer. The orchestrator must manually call `bridge.reply()` after processing, but this is not enforced.

**Recommendation:**
- Eliminate the double-acknowledgment: the responder's immediate reply should be the only acknowledgment.
- Add structured response tracking: when the orchestrator processes a `discuss`/`do` command, it should be required to send a reply within a timeout, with a fallback message if it doesn't.

### 3. Status queries hit stale data

**Problem:** When a user asks "what's going on?" or "status", the interpreter answers based on the `orchestrator_status.json` file content. This file is only updated when `tg-notify.py` is called at lifecycle events. Between events (which can be minutes apart during long-running tasks), the status is stale.

The status file at review time showed 5 active agents with phases like "implementing" and "starting" — but these phase values are only as fresh as the last `tg-notify.py` call. The per-worker `tmp/agent-status/worker-N.txt` files are updated more frequently (at every phase transition within a task), and the responder does read these via `read_agent_status_files()`. However, it only uses them as a fallback when no `orchestrator_status.json` exists, not as a supplement to enrich stale status data.

**Impact:** Users get answers that are minutes behind reality. The agent might report a worker is "starting" when it's actually in "ci-watch".

**Recommendation:** Always merge agent-status file data with orchestrator_status.json, preferring the more recently updated source for each worker. Add an `updated_at` timestamp check to warn if status is more than 2 minutes old.

### 4. No "help" or capability discovery

**Problem:** The interpreter system prompt lists available actions but the user has no way to discover what the bot can do. There is no help command or capability listing. New users must guess.

**Recommendation:** Add a special case in the interpreter prompt: when the user asks "help", "what can you do", etc., return a formatted list of capabilities. This costs nothing extra since it's handled within the existing Haiku call.

### 5. `file_issue` priority defaults to p2 without validation

**Problem:** When the interpreter returns a `file_issue` action, the priority field defaults to "p2" in `_parse_inbox_entry()`. But the interpreter might return any string (e.g., "high", "urgent", "critical") which would be passed through as-is to the orchestrator, potentially creating issues with invalid priority labels.

**Recommendation:** Validate priority values in `_parse_inbox_entry()` against the known set (`p1`, `p2`, `p3`) and default to `p2` for unrecognized values.

### 6. Callback query handling path is untested in the responder

**Problem:** The Lambda handler supports `callback_query` updates (inline keyboard responses) and enqueues them with `message_type: "callback"`. But the responder daemon's `dispatch_message()` only reads `message.get("text")` — callback data (which comes as `message.get("data")`) is not extracted. The `ask()` method on `TelegramBridge` polls SQS directly (bypassing the responder), creating a parallel consumption path.

**Impact:** Callbacks work for `ask()` because it polls SQS directly, but if the responder consumes the callback message first, `ask()` will never see it.

**Recommendation:** Either route all callback handling through the responder (preferred for consistency), or ensure the SQS polling paths don't race. At minimum, document the current dual-path behavior.

### 7. Anthropic client created per-message (no reuse)

**Problem:** In `interpret_message()`, a new `anthropic.Anthropic()` client is created for every single message interpretation. Each instantiation sets up a new HTTP connection pool.

**Impact:** Unnecessary connection setup overhead on every message. At the current rate of 20 calls/60s max, this is not a performance crisis, but it's wasteful.

**Recommendation:** Cache the Anthropic client at module level or pass it through from the daemon's initialization.

### 8. Error retry logic is incomplete

**Problem:** When `interpret_message()` raises an exception (not RateLimitError), the responder falls back to a simple acknowledgment and queues the message. But there is no retry — the message is permanently handled as "interpreter unavailable" even if it was a transient API error (network timeout, 5xx from Anthropic).

**Recommendation:** Add a 1-retry with exponential backoff for transient errors before falling back to the simple acknowledgment path.

### 9. Interpreter JSON schema is loosely validated

**Problem:** `_parse_response()` validates that actions have a `type` field but does not validate the type value or required fields per action type. For example, a `start` action without an `issue` field passes validation. The responder's `dispatch_message()` handles this with `if isinstance(issue_num, int)` guards, but invalid actions are silently dropped.

**Recommendation:** Add schema validation for action objects — at minimum, require `issue` field for `start` and `stop` types. Log warnings for actions that are dropped due to missing fields so operators can see when the interpreter is producing malformed output.

## Decision Bottlenecks

### Bottleneck A: "Forward to orchestrator" is too eager

The interpreter prompt says to use `discuss` for anything requiring "codebase context" and `do` for anything it "cannot do". In practice, this means most non-trivial questions get forwarded rather than answered directly. The interpreter has the orchestrator status JSON — it could answer many questions about current state, recent completions, queue depth, etc. without forwarding.

**Fix:** Tighten the forwarding criteria in the prompt. Only forward when the answer genuinely requires reading files, running commands, or making changes. Status-like questions should be answered directly from the status context.

### Bottleneck B: No priority for inbox items

All inbox items (start, file_issue, discuss, do) are processed in FIFO order. A `do` instruction to "merge PR #750" sits behind a `discuss` about architecture preferences. There is no way to express urgency.

**Fix:** Add a priority field to inbox items. The interpreter could assess urgency from the user's tone/words and assign priority. The orchestrator reads in priority order.

## Summary

The core architecture is sound: Lambda webhook -> SQS -> Claude interpreter -> file-based IPC -> orchestrator. The main issues are:

1. **Dual parsing paths** (SQS direct vs. file-based) that can race
2. **Missing response feedback** for discuss/do commands
3. **Stale status data** between lifecycle events
4. **Loose validation** of interpreter output

None of these are critical — the system works. But they create friction and could lead to confusing user experiences (duplicate acks, stale answers, silently dropped actions).
