# Investigation: Making Telegram More Responsive

**Issue:** #578
**Date:** 2026-03-10
**Status:** Complete

## Current Architecture

The Telegram command pipeline works as follows:

1. User sends a message to the Telegram bot.
2. Telegram POSTs the update to API Gateway.
3. Lambda (`infra/telegram-bot/handler.py`) validates the sender against an allowlist in Secrets Manager, then enqueues the message to SQS (`judgemind-telegram-inbound-dev`).
4. The background poller daemon (`scripts/tg-poll-daemon.py`) polls SQS every 30 seconds and appends messages to `tmp/tg_inbox.json`.
5. The orchestrator reads the inbox file between task launches via `OrchestratorBridge.read_inbox()`.

**Bottleneck:** Step 5. The orchestrator only checks the inbox between tasks. If a subagent takes 10-20 minutes, the user's message sits unread in `tmp/tg_inbox.json` for the entire duration. The daemon poll interval (step 4) adds another 0-30 seconds, but that is minor compared to the orchestrator check interval.

## Options Analyzed

### Option A: Interrupt file / signal

**Concept:** When a message arrives, create `tmp/tg_interrupt` so the orchestrator can check more frequently.

**Verdict: Not feasible.**

Claude Code does not support interrupting a running tool call. The agent processes tool calls sequentially and can only check for new information between tool calls. A PreToolUse hook *can* run between every tool call, but it cannot inject messages into the conversation context -- it can only block/allow the pending tool call. There is no mechanism for a hook to say "stop what you're doing and check Telegram."

The orchestrator spawns subagents via the Agent tool, which is a single long-running tool call. The orchestrator is blocked on `Agent(...)` for the entire duration of the subagent's work. No PreToolUse hooks fire on the orchestrator during this time because the orchestrator is not making tool calls -- it is waiting for one to return.

### Option B: Separate responder process (plain Python script)

**Concept:** A lightweight Python script that runs alongside the orchestrator. It polls SQS directly, handles simple commands (`status`, `pause`, `stop`) by reading/writing shared state files, and queues complex commands for the orchestrator.

**Verdict: Recommended. This is the most practical approach.**

**How it works:**

1. A new daemon script (`scripts/tg-responder.py`) polls SQS on a short interval (5 seconds).
2. For `status` commands: reads `tmp/dispatcher_state.json` (already written by `DispatcherBridge._save_state()`) and replies directly via the Telegram Bot API. Response time: ~5 seconds.
3. For `pause` / `resume`: writes to `tmp/dispatcher_state.json` (the `paused` flag) and replies via Telegram. The dispatcher reads this file at its next check. Effect latency: bounded by dispatcher check interval, but the user gets an immediate acknowledgment.
4. For `stop #N`: writes to a stop-request file (`tmp/stop_requests.json`) and replies via Telegram. The orchestrator checks this file between tasks and avoids spawning new work for that issue.
5. For `start #N` and free text: queues to `tmp/tg_inbox.json` as today. These inherently require the orchestrator and cannot be handled by a simple script.

**Advantages:**
- Zero additional Claude API cost (plain Python, no LLM).
- Instant responses for the 3 most common interactive commands (status, pause, stop).
- Uses the existing state file that `OrchestratorBridge` already writes.
- Follows the established daemon pattern from `tg-poll-daemon.py` (PID file, stop file, signal handling).
- Can completely replace `tg-poll-daemon.py` (subsumes its SQS polling role).

**Disadvantages:**
- Cannot interpret free-text messages intelligently (but those are rare and can still go through the orchestrator).
- Another process to manage. Mitigated by using the existing daemon lifecycle pattern.

**Cost:** Zero when idle beyond the Python process memory (~20MB RSS). SQS long-polling is free (no per-request charge for empty responses).

### Option C: Claude Code hooks

**Concept:** A PreToolUse or PostToolUse hook checks the inbox and injects a notification.

**Verdict: Not feasible.**

Claude Code hooks have three exit modes:
- Exit 0: allow (silent)
- Exit 2: block with error message on stderr

There is no mechanism for a hook to inject a system message or side-channel notification into the conversation. A hook could block a tool call with an error message containing the Telegram command, but this would:
- Break the subagent's current work (it would see a tool failure).
- Only fire on the subagent, not the orchestrator (the orchestrator is blocked waiting for the Agent tool to return).
- Be extremely fragile and confusing.

Additionally, hooks fire per-tool-call on the *active* agent. The orchestrator makes very few tool calls while waiting for subagents. Hooks on the subagent would not help because the subagent should not be handling Telegram commands.

### Option D: Dedicated Telegram agent (Claude Code session)

**Concept:** A long-running Claude Code session dedicated to Telegram I/O. It polls SQS, responds to commands, and writes task requests to a shared queue.

**Verdict: Feasible but not cost-effective.**

A Claude Code session consumes API tokens continuously, even when idle. The session would need to run a polling loop, which means repeated tool calls (Bash to run a Python script, or direct SQS polling via a script). Each iteration costs tokens for the prompt + response.

**Rough cost estimate:** At minimum, ~1000 tokens per poll cycle (reading state, deciding what to do, sending a reply if needed). At a 5-second interval, that is 720 cycles/hour = ~720K tokens/hour. At Opus pricing (~$15/M input, $75/M output), this could easily be $10-50/hour depending on prompt size. This is unacceptable for a self-funded project that explicitly prioritizes fixed costs over usage-based costs.

The only scenario where this makes sense is if free-text interpretation is a high-priority feature. Even then, the agent should only be invoked on-demand (when a free-text message arrives) rather than polling continuously.

**Hybrid variant:** Use Option B for the polling and simple commands, but have it spawn a one-shot Claude Code invocation (via `claude --print`) for free-text messages that need interpretation. This gets the best of both worlds: zero idle cost, instant responses for simple commands, and intelligent handling of complex messages when they arrive. This is a future enhancement, not needed for the initial implementation.

## Recommendation

**Implement Option B: a standalone Python responder daemon** that subsumes the current `tg-poll-daemon.py`.

### Architecture

```
Telegram -> API Gateway -> Lambda -> SQS
                                      |
                              tg-responder.py
                              /            \
                    simple commands    complex commands
                    (status, pause,    (start #N, free text)
                     resume, stop)           |
                         |              tmp/tg_inbox.json
                    Direct reply            |
                    via Telegram API    Orchestrator reads
                         |              between tasks
                    tmp/dispatcher_state.json
                    (reads for status,
                     writes for pause)
```

### Implementation Plan

1. **New script: `scripts/tg-responder.py`** -- replaces `tg-poll-daemon.py`.
   - Polls SQS every 5 seconds (short poll, not long poll, to keep latency low).
   - Parses commands using the existing `parse_command()` from `telegram_bridge.dispatcher`.
   - Handles `status`: reads `tmp/dispatcher_state.json`, also reads `tmp/agent-status/worker-*.txt` files for live worker state, formats a reply, sends via Telegram Bot API.
   - Handles `pause` / `resume`: updates the `paused` flag in `tmp/dispatcher_state.json`, sends confirmation via Telegram.
   - Handles `stop #N`: appends to `tmp/stop_requests.json`, sends confirmation via Telegram.
   - Handles `start #N` / free text: appends to `tmp/tg_inbox.json` (same as today), sends a "queued for orchestrator" acknowledgment via Telegram.
   - Uses the same daemon lifecycle pattern: PID file, stop file, signal handling.

2. **Modify `OrchestratorBridge`** to check `tmp/stop_requests.json` and the `paused` flag from the state file when making spawn decisions.

3. **Update `CLAUDE.md`** to document the new daemon and its commands.

4. **Deprecate `tg-poll-daemon.py`** since the responder subsumes it.

### Expected Latency

| Command | Current | After |
|---------|---------|-------|
| `status` | 5-20 min | ~5 sec |
| `pause` | 5-20 min | ~5 sec (ack), effect at next orchestrator check |
| `resume` | 5-20 min | ~5 sec (ack), effect at next orchestrator check |
| `stop #N` | 5-20 min | ~5 sec (ack), effect at next orchestrator check |
| `start #N` | 5-20 min | ~5 sec (ack), 5-20 min (execution) |
| Free text | 5-20 min | ~5 sec (ack), 5-20 min (interpretation) |

### Cost

Zero incremental cost. The responder is a plain Python process with negligible CPU and memory usage. SQS polling with `WaitTimeSeconds=0` and a 5-second sleep between polls costs nothing meaningful.

## Decisions Needing Human Input

1. **Should the responder read `tmp/agent-status/worker-*.txt` files for richer status replies?** These files contain per-worker phase information that would make status replies much more informative. Recommendation: yes, this is low-effort and high-value.

2. **Should we add a `--respond` flag to the existing `tg-poll-daemon.py` rather than creating a new script?** This keeps one daemon instead of two. Recommendation: replace entirely -- the poll-only daemon becomes unnecessary when the responder exists.

3. **Future: should free-text messages trigger a one-shot `claude --print` invocation for intelligent interpretation?** This would add cost per free-text message but provide much better UX. Recommendation: defer to a follow-up issue; simple commands cover 90%+ of use cases.
