---
description: Opt-in autonomous work queue manager — launches /task agents, merges PRs, triages issues, and communicates via Telegram. Usage: /dispatcher (default 5 slots), /dispatcher 3 (custom slot count), /dispatcher #589 #590 (specific issues only).
argument-hint: "[max-agents | #issue1 #issue2 ...]"
---

# /dispatcher skill

Enable dispatcher mode for the current interactive session. This transforms the session into an autonomous work queue manager that continuously launches `/task` agents, merges completed PRs, triages issues, and communicates via Telegram.

**Dispatcher mode is opt-in.** Interactive sessions are general-purpose by default. The user invokes `/dispatcher` when they want autonomous queue management.

---

## Arguments

- **No arguments** — run with default settings (5 concurrent agent slots, pick from `agent/ready` backlog by priority)
- **`N`** (e.g. `/dispatcher 3`) — set max concurrent agent slots to N
- **`#N1 #N2 ...`** (e.g. `/dispatcher #589 #590`) — work only on the specified issues, in order

---

## Startup

### 1. Telegram setup (if configured)

Initialize the Telegram bridge and start the responder daemon using the Bash tool with `run_in_background: true`:

```
Bash(command="scripts/run-py.sh scripts/tg-responder.py", run_in_background=true)
```

**NEVER use shell `&`, `nohup`, or multicommand patterns to background the responder.** The Bash tool's `run_in_background` parameter is the only supported way. Shell backgrounding requires compound commands that cannot be allowlisted and always trigger permission prompts.

Send a `session_started` notification (this one runs synchronously — it exits immediately):

```
scripts/run-py.sh scripts/tg-notify.py session_started
```

If Telegram is not configured, both commands exit silently (exit 0) — all bridge calls are no-ops when unconfigured.


### 1b. Prune stale local state files

Local state files (`tmp/dispatcher_state.json`, `tmp/dispatcher_status.json`, `tmp/stop_requests.json`) accumulate stale entries across sessions. Prune them once at startup, **before any `tg-notify.py` lifecycle calls** (which load the state file and could write stale workers back to the status file).

```
scripts/run-py.sh scripts/tg-notify.py prune_state
```

This clears stale workers from `dispatcher_state.json`, resets `active_agents` in `dispatcher_status.json`, and truncates `stop_requests.json` to `[]`. Counters like `prs_since_last_audit` and `session_number` are preserved across sessions.

### 2. Clean up stale issue assignments

When a previous dispatcher or agent session ends unexpectedly (context window exhaustion, crash, terminal closed), issues assigned to the agent account may remain assigned with `agent/ready` but no agent working them. These look "in progress" but are actually abandoned, blocking future pickup.

**Run this cleanup once at startup, before scanning the work queue.**

1. List all open issues assigned to the agent account that have the `agent/ready` label:
   ```
   gh issue list --repo judgemind/judgemind \
       --label agent/ready --state open --assignee @me \
       --json number,title \
       --limit 50
   ```

2. For each assigned issue, check whether an open PR exists that references it:
   ```
   gh pr list --repo judgemind/judgemind --state open \
       --search "Closes #<N> OR Fixes #<N> OR Resolves #<N>" \
       --json number --limit 1
   ```
   Alternatively, check the PR list from startup step 4 for branches containing the issue number.

3. **If an open PR exists:** The work is partially complete. Unassign the issue so the next agent can adopt the existing PR, but leave the PR open as evidence of prior work.

4. **If no open PR exists:** The assignment is fully stale — no work product exists. Unassign the issue:
   ```
   gh issue edit <N> --repo judgemind/judgemind --remove-assignee @me
   ```

5. Log each cleanup action. For example: `Cleaned up stale assignment: #107 "Fix OC scraper date parsing" (no open PR — unassigned)`

6. After processing all stale issues, send a summary notification if any were cleaned up:
   ```
   scripts/run-py.sh scripts/tg-notify.py notify "Cleaned up N stale issue assignments: #X, #Y, #Z"
   ```

**Edge cases:**
- An issue has an open PR but CI is failing and no agent is working it — still unassign so a fresh agent can pick it up and adopt the PR.
- An issue was just assigned by another agent in the current session — unlikely at startup, but the "no open PR" heuristic handles this safely. New assignments will not have PRs yet, but the assigning agent will create one shortly. Even if the cleanup unassigns it prematurely, the agent will re-assign when it pushes its PR.

### 3. Scan the work queue (startup only)

List all open `agent/ready` issues sorted by priority:

```
gh issue list --repo judgemind/judgemind \
    --label agent/ready --state open \
    --json number,title,assignees,labels \
    --limit 50
```

Priority order: `priority/p0` > `priority/p1` > `priority/p2` > `priority/p3`. Within the same priority, prefer lower issue numbers (older first).

If specific issues were passed as arguments, filter to only those issues.

**This initial scan is for startup orientation only.** Do NOT cache this list for use in later dispatch cycles. Each dispatch cycle in the main loop (step 6) must run its own fresh query. Issues may be unblocked, relabeled, or closed between cycles — working from a stale list causes the dispatcher to miss newly-available high-priority work.

### 4. Check for in-flight work

Check for any existing open PRs or assigned issues that may need attention (stale CI, merge conflicts, etc.):

```
gh pr list --repo judgemind/judgemind --state open \
    --json number,title,headRefName,statusCheckRollup,mergeable
```

Handle any in-flight PRs before launching new work (see "PR Merge Policy" below).

### 5. Initialize audit counter

Read `tmp/dispatcher_status.json` to recover the `prs_since_last_audit` counter from a previous session. If the file does not exist or the field is missing, initialize the counter to 0.

### 6. Initialize context rotation counter

Initialize `loop_iterations` to 0. This counter tracks how many main loop iterations have elapsed in this session. It is used to trigger a graceful exit before the context window fills up and causes compaction-related forgetfulness (see "Context-Aware Rotation" below).

Also read `session_number` from `tmp/dispatcher_status.json` (default to 0 if missing). Increment it by 1 and persist it back — this tracks how many times the dispatcher has been restarted by the outer `while :; do` loop. The first invocation is session 1.

### 7. Store max_slots for enforcement

Store the max slot count (from the argument, or 5 if not specified) in a variable `max_slots`. This value is used throughout the session for slot enforcement checks. **Do not change this value during the session** — it is set once at startup and used everywhere.

---

## Main Loop

The dispatcher runs a continuous loop:

1. **Refresh state** — check Telegram inbox, stop requests, and pause state
2. **Process dispatcher inbox** — read and execute subagent instructions (see "Subagent Instruction Channel" below)
3. **Handle in-flight PRs** — merge any that are ready, fix any that are failing
4. **Sync after merges** — pull latest main after each merge (see "Post-merge sync" in Rules)
5. **Check audit trigger** — if `prs_since_last_audit >= 20`, spawn `/audit` (see "Periodic Audit" below)
6. **Fill agent slots** — **run a fresh `gh issue list --label agent/ready` query**, then launch `/task` agents for the highest-priority issues (see "Spawning agents" for the **mandatory fresh query and slot count check**)
7. **Process completions** — handle agent completion/failure notifications
8. **Triage** — close done issues, file new issues for discovered problems
9. **Check context rotation** — increment `loop_iterations` and check if it is time to wind down (see "Context-Aware Rotation" below)
10. **Write checkpoint** — overwrite `tmp/dispatcher_checkpoint.md` with current state and behavioral rules (see "Dispatcher Checkpoint File" below)
11. **Repeat** until shutdown

### Slot management — global concurrent ceiling

**`max_slots` is a global concurrent ceiling, not a per-batch limit.** "Up to N agents" means at most N agents running at the same time across all dispatch cycles — not N new agents per cycle. Multiple dispatch cycles must never stack agents additively (e.g. 5 + 5 + ...). Before each dispatch cycle, the dispatcher counts currently running agents and launches only enough to reach the ceiling: `max(0, max_slots - running_count)`.

**Resource exhaustion warning:** Each subagent runs a full Claude Code process with its own worktree, venv installs, git operations, and test runs. On a dev laptop, 5 concurrent agents is already heavy. Exceeding the ceiling risks OOM kills, system freezes, or session-ending crashes. The ceiling exists to protect the host machine — respect it unconditionally.

- Default max slots: **5** (overridable via argument)
- Track active agents by agent ID and issue number
- When an agent completes, its slot opens immediately
- **HARD RULE: Never exceed `max_slots` total concurrent agents.** The dispatcher's own slot count check is the primary enforcement mechanism.
- Launch `/task` agents **with** `isolation: "worktree"` — Claude Code creates a unique worktree at `.claude/worktrees/agent-<id>/` automatically, eliminating worker number contention and race conditions

### Spawning agents — MANDATORY FRESH QUERY AND SLOT COUNT CHECK

**Before spawning ANY new `/task` agent, you MUST perform BOTH a fresh issue query AND a slot count check. These are not optional. Skipping either check is a bug.**

**Step 0 — Fresh issue query (MANDATORY):**

**NEVER work from a cached or in-memory issue list.** The work queue changes constantly — issues get unblocked by the `unblock-issues` CI workflow, new issues are filed, priorities change, and other agents claim work. A cached list from startup or a previous cycle will miss newly-available high-priority issues.

Run a fresh query every dispatch cycle:

```
gh issue list --repo judgemind/judgemind \
    --label agent/ready --state open \
    --json number,title,assignees,labels \
    --limit 50
```

Use the results of THIS query — not the startup scan, not a previous cycle's results — to pick the next issue to dispatch. Apply the same priority ordering: `priority/p0` > `priority/p1` > `priority/p2` > `priority/p3`, lower issue numbers first within the same priority.

**Step 1 — Count currently running agents** — count agents you have spawned that have not yet completed or failed. This is the length of your tracked active agents list.

**Step 2 — Calculate available slots:** `available = max_slots - active_agent_count`. If `available <= 0`, **DO NOT SPAWN**. Skip to the next step of the main loop. Log: "Slot limit reached (N active, limit M) — not spawning."

**Step 3 —** Only if `available > 0`, proceed to spawn — and spawn **at most `available`** agents in this cycle.

**Both the fresh query and the slot count check must happen at the start of every dispatch cycle, not just once per session.** The issue list and running count both change as issues are unblocked, agents complete, or agents fail between cycles. Always use live data, never cached values.

For each open slot, pick the next highest-priority unassigned `agent/ready` issue from the fresh query results and spawn a `/task` agent using the Agent tool with `isolation: "worktree"`:

```
Agent tool with:
  isolation: "worktree"
  prompt: "/task #N"
  description: "Task #N: <truncated title>"
```

**Agent description format:** When spawning a `/task` subagent via the Agent tool, set the `description` parameter to include a truncated issue title so that task notifications are self-descriptive. Format: `"Task #<N>: <truncated title>"` (3-5 words from the title). Examples:

- `description="Task #1138: rename IPC files"`
- `description="Task #589: OC scraper date fix"`
- `description="Task #42: add ruling text field"`

Drop any `[AREA]` prefix tags from the issue title when truncating. This makes `<task-notification>` summaries like `Agent "Task #1138: rename IPC files" completed` readable without cross-referencing issue numbers.

as a background subagent. Before spawning each agent:

1. **Re-count active agents** — do not rely on a count from a previous loop iteration. Count NOW.
2. If `active_agent_count >= max_slots`: stop spawning. Do not spawn this agent or any more.
3. Call `bridge.refresh_state()` to pick up external pause/resume changes
4. Call `bridge.read_stop_requests()` to consume stop requests
5. Call `bridge.read_dispatcher_inbox()` to process subagent instructions
6. If `bridge.paused` is `True`, skip spawning
7. If `bridge.is_issue_stopped(N)` is `True`, skip that issue
8. Skip issues already being worked on by another slot

**After spawning each agent**, send a `task_started` notification and update the status file:

```
scripts/run-py.sh scripts/tg-notify.py task_started <issue_number> "<title>" <agent_id>
```

This sends the Telegram message **and** updates `tmp/dispatcher_status.json` so the responder daemon has accurate context.

### Filtering task notifications

Not all `<task-notification>` messages require dispatcher action. The platform fires notifications for both subagent completions and background command completions. The dispatcher **must** distinguish between the two:

- **Respond to:** Agent completions — notifications where the `<summary>` starts with `"Agent"` (e.g. `"Agent for task #42 completed"`, `"Agent for task #42 failed"`). These represent `/task` or `/audit` subagent results that require slot bookkeeping, Telegram notification, and potential backfill.
- **Ignore silently:** Background command completions — notifications where the `<summary>` starts with `"Background command"` (e.g. `"Background command completed"`, `"Background command failed"`). These are internal operations run by subagents (Gemini reviews, `gh run watch`, test suites, lint runs, etc.) and need no dispatcher action.

**When a background command notification arrives, do nothing.** Do not acknowledge it, do not print a status message, do not send a Telegram notification. Simply continue the main loop. Responding to these creates noise in the conversation without adding value.

### Processing agent completions

When a `<task-notification>` arrives indicating an agent has completed:

**Step 1 — Send Telegram notification:**

**On success:**
```
scripts/run-py.sh scripts/tg-notify.py task_completed <issue_number> "<summary>" <worker_number>
```

**On failure:**
```
scripts/run-py.sh scripts/tg-notify.py task_failed <issue_number> "<error_summary>" <worker_number>
```

Both commands update the status file automatically. Always send a notification immediately when an agent completes or fails — do not batch them.

**Step 2 — Clean up worktree and re-anchor cwd (MANDATORY):**

**Known quirk:** When a subagent is spawned with `isolation: "worktree"`, the parent process's working directory can drift into the agent's worktree (`.claude/worktrees/agent-<id>/`). Claude Code does not always clean up worktrees promptly, and `cd` resolves back into the stale directory until the worktree is removed.

After every agent completion notification, run the cleanup script to validate the agent is finished and remove its worktree, then re-anchor cwd:

```
. scripts/cleanup_worktree.sh .claude/worktrees/agent-<id>
pwd  # verify — cwd is now at repo root
```

The cleanup script must be **sourced** (`. scripts/...` or `source scripts/...`), not executed, so that the `cd` to repo root persists in the caller's shell. It checks the agent's JSONL session log to confirm the agent has truly finished before removing the worktree. It will refuse to remove worktrees where the agent is still running. **Do NOT** manually `git worktree remove`, `rm -rf`, or `cd /` to work around cwd drift.

All subsequent git commands should use `git -C <repo_root>` regardless, as a defense-in-depth measure against cwd drift.

**Step 3 — Post context comment for failed/incomplete agents:**

When an agent fails or exits without completing its task, implementation context is lost. The next agent picking up the same issue starts from scratch with no knowledge of what the previous agent tried. This step preserves that context by posting a structured comment on the issue.

**When to run this step:** Only for agent failures or incomplete completions. Skip for successful completions where the PR was merged and verification passed. To determine if the agent left work unfinished:

1. Check whether the issue was closed (a merged PR with `Closes #N` would have closed it).
2. Check the agent's status file at `tmp/agent-status/<agent-id>.txt` — the `phase` field indicates where the agent stopped.
3. Check if the agent's worktree still exists at `.claude/worktrees/<agent-id>/` — a remaining worktree with uncommitted changes is a strong signal of incomplete work.

If the issue is still open and the agent did not complete successfully, proceed with context extraction.

**Context extraction — check these artifacts in order:**

The dispatcher reads available artifacts from the agent's worktree and status file to build a context summary. Not all artifacts will exist — extract what is available.

1. **Status file** (`tmp/agent-status/<agent-id>.txt`): Read the `phase` and `summary` fields to determine where the agent stopped and what it was doing.

2. **Ralph review result** (`{worktree}/tmp/ralph/review-result.txt`): If this file exists, read it to determine whether the implementation passed review. A `SHIP` verdict means the approach was validated.

3. **Ralph done marker** (`{worktree}/tmp/ralph/ralph-done.txt`): If this file exists, ralph completed — the implementation is likely sound even if the agent died before committing.

4. **Process summary** (`{worktree}/tmp/process_summary.txt`): If this file exists, the agent already mapped acceptance criteria to implementation. Include relevant parts.

5. **Changed files** (`git -C {worktree} diff --name-only` and `git -C {worktree} diff --cached --name-only`): List the files the agent modified to give the next agent a head start on understanding the approach.

6. **Git diff summary** (`git -C {worktree} diff --stat`): A concise summary of the scope of changes (files changed, insertions, deletions).

7. **Task notification summary**: The `<summary>` text from the `<task-notification>` itself often contains useful context about what the agent accomplished or why it failed.

**Compose and post the context comment:**

Write the comment to `tmp/failed_agent_context_<issue>.txt` using this format:

```
## Prior Agent Context (auto-generated)

A previous agent attempted this issue and made progress before exiting.

**Agent:** <agent-id>
**Phase when stopped:** <phase from status file, or "unknown">
**Failure mode:** <premature end_turn / context exhaustion / crash / unknown>

**What was done:**
<Summary extracted from status file, process summary, or task notification. If ralph completed, note that the implementation approach passed review.>

**Review status:** <SHIP / iterating (iteration N) / not started>

**Files changed:**
<List of files from git diff --name-only, or "no changes detected">

**Guidance for next agent:** <Based on what was accomplished:>
- If ralph passed (SHIP): "The implementation approach above passed review. Reimplement it (do not try to adopt the old worktree) and continue from the step where the previous agent stopped."
- If ralph was iterating: "The implementation was in progress. Review the approach described above and continue iterating."
- If no implementation started: "No implementation progress was made. Start fresh."
```

Post the comment:
```
gh issue comment <N> --repo judgemind/judgemind --body-file tmp/failed_agent_context_<issue>.txt
```

**Determining the failure mode:**

- **Premature end_turn:** The task notification summary mentions the agent completing normally, but the issue is still open and no PR exists. The agent likely emitted text instead of a tool call.
- **Context exhaustion:** The task notification mentions context window limits or compaction.
- **Crash:** The task notification mentions an error or exception.
- **Unknown:** None of the above signals are present. Use "unknown" and include whatever information is available.

**Fallback for unparseable or missing context:**

If the worktree does not exist (already cleaned up) and the status file is missing or empty, post a minimal comment:

```
## Prior Agent Context (auto-generated)

A previous agent (<agent-id>) attempted this issue but exited before completing.

**Phase when stopped:** unknown
**Failure mode:** <inferred from task notification, or "unknown">

No implementation artifacts were recoverable. The next agent should start fresh.
```

This minimal comment still provides value by alerting the next agent that a prior attempt was made, preventing it from being surprised by any partial state (stale branches, open PRs, etc.).

**Step 4 — Clean up the agent's worktree (if needed):**

Worktree cleanup is handled in Step 2 via `. scripts/cleanup_worktree.sh` (sourced, not executed). If the script reports the worktree is already gone, no further action is needed. If cleanup failed in Step 2 (e.g., the script returned an error), log it but do not block the dispatch loop. Stale worktrees do not affect slot counting (slots are tracked by the dispatcher's own agent list, not by worktree directory count).

---

## Context-Aware Rotation

**Problem:** The dispatcher runs in an outer `while :; do claude /dispatcher; done` loop. Over time, the conversation context fills up. When context compaction occurs, the LLM loses track of in-memory state (active workers, what it was doing, pending decisions), causing the dispatcher to become "forgetful" and unreliable.

**Solution:** The dispatcher proactively exits before context gets too large, allowing the outer loop to restart it with a fresh context. All state is persisted to files, so the new session picks up seamlessly.

### When to rotate

At main loop step 9, increment `loop_iterations`. If **all** of these conditions are true, begin graceful wind-down:

1. `loop_iterations >= 40` — enough iterations have elapsed that context is likely getting large
2. No agents are in the middle of spawning (all slots are either occupied by running agents or empty)

The threshold of 40 iterations is conservative — each iteration adds tool calls, command outputs, and notification messages to the context. At typical dispatcher verbosity, 40 iterations approaches the context window limit. If you observe compaction happening earlier, reduce this threshold.

**During the wind-down phase (after the threshold is hit):**

1. **Stop launching new agents.** Set an internal `winding_down` flag. Do not fill empty slots.
2. **Continue processing completions.** Handle `<task-notification>` messages, merge green PRs, send Telegram notifications — all as normal.
3. **Continue processing Telegram commands.** Respond to `status`, `pause`, `resume`, `stop` commands as normal. For `start #N` commands, acknowledge receipt but note the dispatcher is about to restart and will pick it up in the next session.
4. **Wait for all active agents to complete.** Check active agent count each iteration. Once all agents have finished (or reported back), proceed to exit.
5. **Merge any remaining green PRs.** Do one final sweep.
6. **Persist all state.** Ensure `tmp/dispatcher_status.json` and `tmp/dispatcher_state.json` are up to date with: `prs_since_last_audit`, `session_number`, paused state, stopped issues, and recently completed tasks.
7. **Send a rotation notification:**
   ```
   scripts/run-py.sh scripts/tg-notify.py notify "Dispatcher rotating context (session N, M iterations). Restarting momentarily."
   ```
8. **Do NOT stop the responder daemon.** The outer loop will restart the dispatcher immediately, and the responder should keep running to avoid missing Telegram messages.
9. **Do NOT send `session_ended`.** This is a rotation, not a shutdown. The next session will continue seamlessly.
10. **Exit.** Print a summary of what was accomplished in this session, then stop. The outer `while :; do` loop will restart the dispatcher with a fresh context.

### State that persists across rotations

All of this state survives a rotation because it is file-backed:

| State | File | Notes |
|---|---|---|
| Paused flag | `tmp/dispatcher_state.json` | New session reads on startup |
| Active workers | `tmp/dispatcher_state.json` | Pruned on startup (step 1b); new session re-discovers running agents via worktree list + status files |
| PRs since last audit | `tmp/dispatcher_status.json` | Counter continues from where it left off |
| Session number | `tmp/dispatcher_status.json` | Incremented on each startup |
| Stopped issues | `tmp/stop_requests.json` | Cleared on startup (step 1b); stop requests are session-scoped |
| Responder daemon | PID file in `tmp/` | Keeps running across rotations |
| Telegram inbox | `tmp/tg_inbox.json` | New session picks up unprocessed commands |
| Dispatcher checkpoint | `tmp/dispatcher_checkpoint.md` | Behavioral context for compaction recovery (see below) |

### State that does NOT persist (and that's OK)

| State | Why it's OK |
|---|---|
| `loop_iterations` counter | Resets to 0 — that's the point of rotation |
| In-memory `_recently_completed` list | Status file has a snapshot; startup step 4 re-scans open PRs for current state |
| Pending reply tracking | Responder daemon handles timeouts independently |

---

## Dispatcher Checkpoint File

The dispatcher writes `tmp/dispatcher_checkpoint.md` at the end of every main loop iteration (step 10). This file contains both **state** and **behavioral context** — it is the primary recovery mechanism when context compaction occurs or when a new session starts after rotation.

### Why a checkpoint file?

The existing state files (`tmp/dispatcher_status.json`, `tmp/dispatcher_state.json`) track data: agent IDs, counters, pause flags. But they do not encode **what the dispatcher should be doing** — the behavioral rules that prevent degenerate patterns like sleep-and-poll loops, racing agents to merge, or burning context on redundant CI checks. After compaction, the LLM retains the data but loses the discipline. The checkpoint file closes that gap.

### Checkpoint format

The checkpoint is a Markdown file (human-readable, easy for the LLM to parse) with a fixed structure. Overwrite it completely each iteration — do not append.

```markdown
# Dispatcher Checkpoint

## Session
- Session: <session_number>, Loop iteration: <loop_iterations>
- Started: <session_start_time ISO-8601>
- PRs since last audit: <prs_since_last_audit>/<20>
- Winding down: <yes/no>

## Active Agents (mine)
- <agent_id> -> #<issue> "<title>" (<priority>) -- spawned <time>
- <agent_id> -> #<issue> "<title>" (<priority>) -- spawned <time>
(or "None" if no agents are running)

## Waiting For
- task-notification events from the <N> active agents above
(or "Nothing -- no agents running, ready to dispatch" if slots are empty)

## Do NOT
- Sleep-and-poll for PR CI status -- agents manage their own PRs
- Merge PRs while agents are still running -- only merge orphaned PRs
- Proactively check CI on agent PRs -- wait for task-notification, then check if needed
- Clean up worktrees proactively -- only after specific agent completion
- Run code changes on main -- delegate to /task subagents
- Block on long-running operations -- stay responsive to Telegram and events

## Next Actions
- Process next task-notification -> cleanup worktree, re-anchor cwd, send Telegram, free slot
- When slot opens -> fresh `gh issue list --label agent/ready` query, dispatch highest priority
- If prs_since_last_audit >= 20 -> spawn /audit
- Check Telegram inbox for user commands
(adjust based on current state -- e.g., if winding down, note "waiting for agents to finish before exiting")
```

### Writing the checkpoint

At main loop step 10, use the Write tool to overwrite `tmp/dispatcher_checkpoint.md`. The content is assembled from the dispatcher's current in-memory state:

- **Session metadata:** `session_number`, `loop_iterations`, `prs_since_last_audit`, `winding_down` flag
- **Active agents:** from the tracked agent list (agent ID, issue number, title, priority, spawn time)
- **Waiting for:** derived from the active agent list
- **Do NOT:** a fixed set of behavioral rules (copy the list above verbatim — these do not change between iterations)
- **Next actions:** derived from current state (e.g., if all slots are full, "wait for completions"; if winding down, "wait for agents to finish")

The write is cheap — the file is small (~500 bytes) and overwritten each iteration. It does not require any API calls or external queries.

---

## Resume After Compaction

When the dispatcher's context is compacted by the platform (or when a new session starts after rotation), in-memory behavioral discipline is lost. The continuation summary provides data ("5 agents running") but not rules ("wait for events, don't poll"). This section defines how to recover.

### First action after compaction or session start

**Before doing anything else in a new session or after detecting compaction**, read `tmp/dispatcher_checkpoint.md`:

1. **Read the checkpoint file.** If `tmp/dispatcher_checkpoint.md` exists, read it in full. This is the authoritative source of both state and behavioral context.
2. **If the file does not exist** (first-ever session, or file was deleted), proceed with normal startup — there is nothing to recover.

### Reconstruct active agent list

The checkpoint's "Active Agents" section lists every agent spawned in the previous session (or before compaction). Cross-reference this with the current worktree list to determine which agents are still running:

```
git worktree list
```

For each agent listed in the checkpoint:
- If its worktree still exists in `git worktree list` — the agent is likely still running. Add it to the tracked active agent list.
- If its worktree is gone — the agent has completed (or was cleaned up). Do not track it.

Also check `tmp/agent-status/<agent-id>.txt` for each agent — the `phase` field indicates whether the agent is still working or has finished.

### Re-establish behavioral discipline

Read the "Do NOT" section of the checkpoint and **internalize every rule**. These rules exist because the dispatcher has historically fallen into these exact degenerate patterns after compaction:

- **Do NOT sleep-and-poll for PR CI status.** Agents manage their own PRs through the full CI-fix-push cycle. The dispatcher only needs to merge orphaned PRs (where the agent has exited but CI is green).
- **Do NOT merge PRs while agents are still running.** An agent that is still alive will merge its own PR after ralph review and CI pass. If the dispatcher races to merge, it can merge before the agent finishes verification, causing the agent to fail on a missing branch.
- **Do NOT proactively check CI on agent PRs.** This burns context and API budget on redundant checks. Wait for task-notification events — they signal when an agent is done and its PR needs attention.
- **Do NOT clean up worktrees proactively.** Only clean up after a specific agent completion notification confirms the agent is finished.
- **Do NOT run code changes on main.** All code changes are delegated to `/task` subagents.
- **Do NOT block on long-running operations.** Stay responsive to Telegram commands and task-notification events.

### Resume the main loop

After reading the checkpoint and reconstructing state:

1. **Check Telegram inbox** — messages may have accumulated during compaction. Process any pending commands.
2. **Do NOT re-scan PRs or merge anything immediately.** Wait for the next task-notification event to arrive before taking action on PRs. Agents that are still running will manage their own PRs.
3. **Do NOT launch new agents immediately** unless slots are empty and no agents are running. If agents from the previous context are still active (worktrees exist), wait for their completions first.
4. **Continue the main loop from step 1.** The checkpoint has restored enough context to resume normal operation.

### Compaction detection

The dispatcher cannot directly detect when compaction occurs mid-session. However, these signals suggest it may have happened:

- The conversation context suddenly feels "reset" — tool outputs from earlier iterations are no longer visible
- In-memory variables (active agent list, loop counter) are unexpectedly empty or zero
- The dispatcher finds itself about to do something the checkpoint's "Do NOT" list prohibits

If you suspect compaction has occurred mid-session, **read the checkpoint file immediately** before continuing. It is always safe to re-read — the file reflects the state as of the most recent main loop iteration.

---

## Subagent Instruction Channel

Subagents can request actions from the dispatcher by writing to `tmp/dispatcher_inbox.json`. This is a file-based instruction channel similar to `tmp/tg_inbox.json` (Telegram inbox), but for subagent-to-dispatcher communication.

### How subagents write instructions

Subagents use `scripts/dispatcher-request.py` to append entries:

```
scripts/dispatcher-request.py restart_responder --reason "telegram-bridge code updated" --from-issue 733
scripts/dispatcher-request.py notify --message "Found regression in OC scraper" --from-issue 600
scripts/dispatcher-request.py terraform_apply --module telegram-bot --from-issue 712
scripts/dispatcher-request.py run_script --script scripts/tg-set-webhook.sh --from-issue 725
scripts/dispatcher-request.py file_issue --title "Bug report" --description "Details..." --priority p1 --labels area/scraping
```

The script is stdlib-only (no venv needed) and uses file locking for safe concurrent access.

### How the dispatcher reads instructions

In the main loop (step 2), call `bridge.read_dispatcher_inbox()` which returns a list of `DispatcherInstruction` objects. For each instruction:

| Action | How to handle |
|---|---|
| `restart_responder` | Run `scripts/tg-stop-responder.sh`, reinstall telegram-bridge deps, restart responder via `Bash(command="scripts/run-py.sh scripts/tg-responder.py", run_in_background=true)` — **never** use shell `&` |
| `terraform_apply` | Run dev terraform apply for the specified module (see "Auto-apply dev terraform" below) |
| `notify` | Send a Telegram notification via `scripts/run-py.sh scripts/tg-notify.py notify "<message>"` |
| `run_script` | Execute the specified script (validate it starts with `scripts/` for safety) |
| `file_issue` | Create a GitHub issue with `gh issue create` using the provided title, description, priority, and labels |

**Safety rules:**
- Only the pre-defined action types in `InstructionKind` are accepted — unknown actions are logged and skipped
- `run_script` must validate the script path starts with `scripts/` to prevent arbitrary command execution
- Log all instructions and their outcomes
- Send a Telegram notification confirming each instruction was processed

### Inbox file format

`tmp/dispatcher_inbox.json` is a JSON array of instruction objects:

```json
[
  {"action": "restart_responder", "reason": "code updated", "from_issue": 733, "timestamp": "..."},
  {"action": "terraform_apply", "module": "telegram-bot", "from_issue": 712, "timestamp": "..."},
  {"action": "notify", "message": "Found a regression", "from_issue": 600, "timestamp": "..."}
]
```

The file is atomically read and truncated by `read_dispatcher_inbox()`, just like `read_inbox()` and `read_stop_requests()`.

---

## Dispatcher-to-Subagent Message Channel

The dispatcher can send messages to running subagents via their worktree inbox. This is the reverse of the Subagent Instruction Channel above — it allows the dispatcher to push context to running agents without interrupting them.

### How the dispatcher sends messages

Use `scripts/dispatcher-send.py` to write messages to a subagent's worktree inbox:

```
scripts/dispatcher-send.py --worktree /path/to/worktree "Rebase onto latest main, PR #100 touched your files"
scripts/dispatcher-send.py --worktree /path/to/worktree "Issue #42 was deprioritized, finish current work but skip stretch goals"
```

The script is stdlib-only (no venv needed) and uses file locking for safe concurrent access. Messages are written to `{worktree}/tmp/inbox.json`.

### How subagents receive messages

A PostToolUse hook (`check-inbox.sh`) runs after every tool call in the subagent. When messages are present, it echoes them to stdout prefixed with `[dispatcher]`, then truncates the inbox file. The agent sees messages naturally as part of hook output — no special handling needed.

The hook is designed for minimal overhead:
- Checks file existence and size in pure bash (fast path: <1ms when no messages)
- Only invokes Python for JSON parsing when the file has content
- Uses file locking to avoid races with the dispatcher writing concurrently

### When to send messages

Send a message to a running subagent when:
- A PR just merged that touches the same files the agent is modifying (suggest a rebase)
- A user sends a Telegram command relevant to a running agent's work
- An issue's priority or scope changes while an agent is working on it
- The dispatcher is winding down for context rotation and wants agents to wrap up
- A dependency was unblocked or a new constraint was discovered that affects the agent's task

### Message format

`{worktree}/tmp/inbox.json` is a JSON array of message objects:

```json
[
  {"timestamp": "2026-03-19T12:00:00+00:00", "message": "Issue #42 was deprioritized"},
  {"timestamp": "2026-03-19T12:01:00+00:00", "message": "PR #100 just merged, you may need to rebase"}
]
```

### Design constraints

- **One-way only.** The agent does not reply through this channel — it already has `dispatcher-request.py` for dispatcher-bound communication.
- **No guaranteed delivery.** If the agent finishes before checking, the message is lost (and that is fine).
- **No interruption.** The agent sees the message at its next tool call, not immediately.
- **Zero overhead when empty.** The hook exits in <1ms when no messages are pending.


---

## PR Merge Policy

The dispatcher proactively merges PRs when all conditions are met:

1. **CI is green** — all required status checks show `SUCCESS` or `SKIPPED`
2. **No merge conflicts** — `mergeable` is not `CONFLICTING`
3. **The PR was created by a `/task` agent in this session** (or by any agent if the PR has passed ralph review)

Merge command:
```
gh pr merge <N> --repo judgemind/judgemind --squash --delete-branch
```

After merging:
- **Immediately sync to latest main** (see "Post-merge sync" in Rules below) — this must happen before spawning any new agents
- **Send a Telegram notification:**
  ```
  scripts/run-py.sh scripts/tg-notify.py pr_merged <pr_number> "<pr_title>"
  ```
- Check if the merged PR triggers a deploy workflow (see CLAUDE.md "Verify deployment")
- For deployed services, watch the deploy workflow to completion
- **If PR touches `packages/telegram-bridge/` or `scripts/tg-responder.py`** — restart the responder daemon (see "Auto-restart responder daemon" below)
- **If PR touches `infra/terraform/`** — run dev terraform apply (see "Auto-apply dev terraform" below)
- **Increment `prs_since_last_audit`** and persist it to `tmp/dispatcher_status.json`. When the counter reaches 20, the next main loop iteration will trigger an audit (see "Periodic Audit" below).

**Do not merge PRs from external contributors or PRs you did not create** unless the user explicitly asks.

### Handling CI failures

If a PR's CI is failing:
- Check if the failure is in a check the agent could fix (lint, test, type error)
- If so, the `/task` agent should already be handling it — check its status file
- If the agent has exited and CI is still failing, log it and notify via Telegram:
  ```
  scripts/run-py.sh scripts/tg-notify.py notify "CI still failing on PR #<N> after agent exited — needs attention"
  ```
- Do not attempt to fix another agent's PR from the dispatcher — spawn a new `/task` for it if needed

### Handling merge conflicts

If a PR has merge conflicts:
- The owning `/task` agent should handle rebasing
- If the agent has exited, log it and notify via Telegram:
  ```
  scripts/run-py.sh scripts/tg-notify.py notify "PR #<N> has merge conflicts and agent has exited — needs attention"
  ```
- The dispatcher does not rebase other agents' branches

### Auto-restart responder daemon

When a merged PR modifies the responder code (`packages/telegram-bridge/` or `scripts/tg-responder.py`), restart the daemon to pick up the changes. This is quick and runs inline (not delegated to a subagent):

1. Run `scripts/tg-stop-responder.sh` (sends SIGTERM, waits for exit, cleans up PID/stop files)
2. Reinstall telegram-bridge deps if needed
3. Launch the responder using the Bash tool with `run_in_background: true`: `Bash(command="scripts/run-py.sh scripts/tg-responder.py", run_in_background=true)`. **NEVER use shell `&` or multicommand patterns.**
4. Verify new PID file exists
5. Send Telegram notification: "Responder daemon restarted after PR #N merged"

If the restart fails, file a p1 issue and notify via Telegram.

### Auto-apply dev terraform

When a merged PR touches files under `infra/terraform/`, the dispatcher automatically applies the changes to the **dev environment only**. Production applies remain human-only. This runs inline in the dispatcher (not delegated to a subagent) because it is a short, well-defined operation.

This same logic also handles `terraform_apply` instructions from the subagent instruction channel (when a subagent requests a targeted module apply via `scripts/dispatcher-request.py terraform_apply`).

#### Detecting infra PRs

After merging a PR, check whether any changed files match `infra/terraform/**`. Use the PR's file list:

```
gh pr view <N> --repo judgemind/judgemind --json files --jq '.files[].path'
```

If any path starts with `infra/terraform/`, proceed with the apply.

#### Determining which environments to apply

Inspect the changed file paths to decide which environment directories need an apply:

| Changed path pattern | Environment to apply |
|---|---|
| `infra/terraform/environments/dev/` | `environments/dev` |
| `infra/terraform/environments/dns/` | `environments/dns` (requires Cloudflare secret) |
| `infra/terraform/environments/hosting/` | `environments/hosting` (requires Cloudflare secret) |
| `infra/terraform/environments/staging/` | `environments/staging` |
| `infra/terraform/environments/production/` | **Skip** — production is human-only |
| `infra/terraform/modules/` or root `infra/terraform/*.tf` | `environments/dev` (modules are consumed by environments) |

If a subagent `terraform_apply` instruction specifies a `--module`, use `-target=module.<module>` to scope the apply to just that module.

#### Apply procedure

For each environment that needs an apply:

1. **Init the environment:**
   ```
   terraform -chdir=infra/terraform/environments/dev init
   ```

2. **Run plan first** to verify expected changes:
   ```
   terraform -chdir=infra/terraform/environments/dev plan -no-color
   ```

3. **Apply with auto-approve:**
   ```
   terraform -chdir=infra/terraform/environments/dev apply -auto-approve
   ```

4. **For DNS/hosting environments** that need the Cloudflare API token, use secret injection:
   ```
   scripts/with-secret.sh -e CLOUDFLARE_API_TOKEN=judgemind/cloudflare/api-token -- terraform -chdir=infra/terraform/environments/dns apply -auto-approve
   ```

5. **For targeted module applies** (from subagent instructions):
   ```
   terraform -chdir=infra/terraform/environments/dev apply -target=module.<module_name> -auto-approve
   ```

#### Success handling

After a successful apply:
- Send a Telegram notification with the environment and a brief summary:
  ```
  scripts/run-py.sh scripts/tg-notify.py notify "Terraform apply succeeded for dev after PR #<N> merged"
  ```

#### Failure handling

If the apply fails:
1. Send a Telegram notification immediately:
   ```
   scripts/run-py.sh scripts/tg-notify.py notify "Terraform apply FAILED for dev after PR #<N> — filing p1 issue"
   ```
2. File a `priority/p1` issue describing the failure, referencing the merged PR, with `agent/ready` label so an agent can investigate.
3. Do not retry automatically — the filed issue will be picked up by an agent.

#### Safety constraints

- **Never apply to `environments/production/`** — production is human-only, always.
- **Never apply from the root `infra/terraform/` directory** — this creates duplicate resources. Always use environment-specific paths. The preflight hook enforces this.
- **Always run from the main repo checkout** (not a worktree) after pulling latest main.
- **Terraform apply can acquire state locks** — if a lock conflict occurs, wait briefly and retry once. If it fails again, file a p1 issue.

---

## Periodic Audit

The dispatcher triggers a codebase health audit every 20 merged PRs using the `/audit` skill.

### Counter management

- Track `prs_since_last_audit` as an integer counter, persisted in `tmp/dispatcher_status.json`.
- After each PR merge, increment the counter by 1.
- On startup, read the counter from the status file (default to 0 if missing).
- The counter persists across dispatcher restarts via the status file.

### Trigger condition

In the main loop (step 5), after handling merges and syncing:

1. Check if `prs_since_last_audit >= 20`.
2. Check that no `/audit` agent is already running (avoid overlapping audits).
3. If both conditions are met and a slot is available, spawn `/audit` as a background subagent.
4. Reset `prs_since_last_audit` to 0 and persist to `tmp/dispatcher_status.json`.
5. Send a Telegram notification:
   ```
   scripts/run-py.sh scripts/tg-notify.py notify "Launching periodic audit (20 PRs merged since last audit)"
   ```

### Slot usage

The `/audit` agent occupies one agent slot like any `/task` agent. It creates its own worktree internally. The audit does not block other work — other agents continue running in parallel.

### Manual trigger

The user can also trigger an audit manually via Telegram (`start audit`) or by invoking `/audit` directly. Manual triggers reset the counter to 0.

---

## Issue Triage Policy

The dispatcher proactively manages issues:

### Close done issues
- If all sub-tasks of a parent issue are closed and the parent has no remaining work, close the parent
- Comment with a summary of what was completed

### File new issues
- If an agent reports a problem it cannot solve (blocked, needs human decision), the dispatcher notes it for Telegram notification
- If the dispatcher discovers issues during monitoring (stale PRs, repeated CI failures), file tracking issues with appropriate labels

### Unblock dependent issues
- After a PR merges, the `unblock-issues` CI workflow handles this automatically
- For non-PR completions, follow the manual unblock process in CLAUDE.md

### Only ask the user when genuinely needed
- Priority calls (is this p1 or p2?)
- Ambiguous scope (does this issue include X?)
- Architecture choices (should we use approach A or B?)
- Everything else is handled autonomously

---

## Telegram Communication

### Notification script

All outbound Telegram notifications MUST use the committed script `scripts/run-py.sh scripts/tg-notify.py`. This script wraps the `telegram_bridge` package and handles:

- Sending the Telegram message via the bot API
- Updating `tmp/dispatcher_status.json` so the responder daemon has accurate context
- Persisting worker state to `tmp/dispatcher_state.json`
- Exiting silently (exit 0) when Telegram is not configured

**Available commands:**

| Command | Arguments | When to use |
|---|---|---|
| `scripts/run-py.sh scripts/tg-notify.py session_started` | (none) | Dispatcher startup |
| `scripts/run-py.sh scripts/tg-notify.py session_ended` | (none) | Dispatcher shutdown |
| `scripts/run-py.sh scripts/tg-notify.py task_started` | `<issue> <title> <worker>` | After spawning a `/task` agent |
| `scripts/run-py.sh scripts/tg-notify.py task_completed` | `<issue> <summary> <worker>` | Agent completed successfully |
| `scripts/run-py.sh scripts/tg-notify.py task_failed` | `<issue> <error> <worker>` | Agent failed |
| `scripts/run-py.sh scripts/tg-notify.py pr_merged` | `<pr_number> <title>` | After squash-merging a PR |
| `scripts/run-py.sh scripts/tg-notify.py notify` | `<message>` | Free-form notification (blockers, CI issues, deploy status) |
| `scripts/run-py.sh scripts/tg-notify.py prune_state` | (none) | Dispatcher startup (step 1b) — clears stale workers and stop requests |

**IMPORTANT:** Always call `scripts/run-py.sh scripts/tg-notify.py` after every lifecycle event. Do not rely on remembering to send notifications manually — the script handles both the Telegram message and the status file update atomically. If you skip the notification, the responder daemon will have stale status and give incorrect answers to user queries.

### Outbound notification checklist

Send a notification for **every** lifecycle event:

- [ ] **Session started** — at dispatcher startup
- [ ] **Stale assignments cleaned** — after startup cleanup (if any were found)
- [ ] **Agent launched** — after each `/task #N` spawn (include issue number, title, worker number)
- [ ] **Agent completed** — when `<task-notification>` reports success (include issue number, summary, worker number)
- [ ] **Agent failed** — when `<task-notification>` reports failure (include issue number, error, worker number)
- [ ] **PR merged** — after each `gh pr merge` (include PR number and title)
- [ ] **Deploy succeeded/failed** — after watching deploy workflow
- [ ] **Terraform apply succeeded/failed** — after applying dev terraform for infra PRs
- [ ] **Blocker encountered** — when an issue needs human decision
- [ ] **Audit triggered** — when `/audit` is spawned (include PR count since last audit)
- [ ] **Context rotation** — when winding down for a context rotation (include session number and iteration count)
- [ ] **Session ended** — at dispatcher shutdown

### Inbound commands

Process commands from the Telegram inbox and responder daemon:

| Command | Action |
|---|---|
| `status` | Handled by responder daemon directly |
| `start #N` | Spawn `/task #N` in the next available slot |
| `start audit` | Spawn `/audit` immediately, reset `prs_since_last_audit` to 0 |
| `stop #N` | Stop spawning work for issue #N; if an agent is working on it, let it finish |
| `pause` | Stop launching new agents; existing agents continue |
| `resume` | Resume launching new agents |
| `file_issue` | Create a GitHub issue from the user's description; confirm with issue URL via Telegram |
| `discuss` | User wants to discuss something requiring codebase context; formulate a response using file access, code reading, etc. and reply via Telegram |
| `do` | User wants an action performed (merge PR, check CI, etc.); execute the instruction and confirm via Telegram |
| Free text | Interpret and reply via Telegram — check `result["needs_reply"]` |

The `file_issue`, `discuss`, and `do` commands are classified by the Opus interpreter in the responder daemon and written to the inbox as structured entries with an `action` key. The dispatcher reads these via `bridge.read_inbox()` which returns `Command` objects with the appropriate `CommandKind`. Each command's result dict includes the metadata needed to act on it:

- **`file_issue`**: `result["description"]`, `result["priority"]`, `result["labels"]`, `result["reply_to"]`
- **`discuss`**: `result["message"]`, `result["reply_to"]`, `result["needs_reply"]`
- **`do`**: `result["instruction"]`, `result["reply_to"]`, `result["needs_reply"]`

### State file integration

The responder daemon communicates via shared state files (see CLAUDE.md "Responder daemon and state files"):

- Read `tmp/dispatcher_state.json` for pause/resume state
- Read `tmp/stop_requests.json` for stop requests
- Read `tmp/tg_inbox.json` for queued commands (start, file_issue, discuss, do, and free text)
- Read `tmp/dispatcher_inbox.json` for subagent instructions (restart_responder, terraform_apply, notify, run_script, file_issue)

**The dispatcher MUST update `tmp/dispatcher_status.json` after every state change.** The `scripts/run-py.sh scripts/tg-notify.py` script does this automatically for lifecycle events (`task_started`, `task_completed`, `task_failed`, `pr_merged`). For other state changes (pause, resume, slot changes), call `bridge.write_status()` directly or use `scripts/run-py.sh scripts/tg-notify.py notify` to trigger a status file update.

The `tmp/dispatcher_status.json` file includes the `prs_since_last_audit` counter and `session_number` alongside the existing fields (`active_agents`, `open_prs`, `recently_completed`, `queue`, `paused`, `stopped_issues`, `updated_at`).

---

## Shutdown

Shutdown triggers:
- User types `/stop` or asks to stop
- All issues in the queue are complete and no agents are running
- **Context rotation threshold reached** (see "Context-Aware Rotation" above) — this is a **rotation**, not a full shutdown

### Full shutdown procedure (user-initiated or queue empty)

1. Stop launching new agents
2. Wait for all active agents to complete (do not kill them)
3. Merge any remaining green PRs
4. Send `session_ended` Telegram notification:
   ```
   scripts/run-py.sh scripts/tg-notify.py session_ended
   ```
5. Stop the responder daemon (`scripts/tg-stop-responder.sh`)
6. Print a summary of what was accomplished:
   - Issues completed
   - PRs merged
   - Issues filed
   - Blockers remaining

### Context rotation procedure (automatic)

See "Context-Aware Rotation" above for the detailed wind-down steps. Key differences from full shutdown:
- **Do NOT stop the responder daemon** — it keeps running for the next session
- **Do NOT send `session_ended`** — this is a rotation, not an end
- **DO send a rotation notification** via `scripts/run-py.sh scripts/tg-notify.py notify`
- **DO persist all state** to `tmp/` files so the next session picks up seamlessly

---

## Rules

### Responsiveness — the dispatcher's primary constraint

The dispatcher must stay responsive to user interaction and Telegram commands at all times. A blocked dispatcher cannot process pause/resume commands, dispatch new work, merge PRs, or reply to Telegram messages.

- **Never do long-running work in the main agent — delegate to subagents.** "Long-running" means anything that might take more than ~10 seconds: code changes, investigations, deep codebase exploration, issue body rewrites, running tests, large file analysis, or multi-step research.
- **The dispatcher's job is: read messages, make quick decisions, dispatch work, send updates.** It is a dispatcher, not an implementer.
- **Allowed in the main agent:** `gh` CLI calls, quick file reads, Telegram sends, short status checks, writing issue comments, updating labels, spawning subagents, terraform apply for dev environments.
- **Everything else = spawn a subagent.** If you are unsure whether something is "quick enough," it is not — delegate it.
- **Never block on a single long operation.** If a `gh run watch` or similar command could take minutes, run it in a way that does not prevent processing other events in the main loop. Prefer polling with short timeouts over blocking waits.

### No direct code changes on main

The dispatcher MUST NOT make any code changes on `main` itself. All code changes must be delegated to `/task` subagents working in worktrees.

- **Prohibited (code changes):** editing source files, modifying configs, writing scripts, updating documentation content, changing Terraform, or any operation that results in a `git -C <repo_root> commit` on the dispatcher's checkout. If it would show up in `git -C <repo_root> diff`, delegate it to a `/task` subagent.
- **Allowed (non-code operations):** `gh` CLI calls (issue comments, label changes, PR merges, issue creation/editing), reading files for decision-making, writing to `tmp/`, sending Telegram messages, running `git -C <repo_root> fetch`/`git -C <repo_root> pull`, running `terraform apply` for dev environments (applying already-merged code, not changing it). These do not modify committed code and are safe to run inline.

If you catch yourself about to edit a file or stage a commit from the dispatcher agent, **stop and spawn a `/task` subagent instead.**

### Post-merge sync

After each PR merge, the dispatcher MUST pull latest main so that subsequent `/task` agents start from the current tip of the codebase. **Always use `git -C <repo_root>`** to ensure commands work even if the dispatcher's cwd has drifted (see "Processing agent completions" above):

```
git -C <repo_root> fetch origin main
git -C <repo_root> pull origin main --ff-only
```

Do this **immediately** after every `gh pr merge` call, before spawning new agents or processing the next item in the loop. Without this step, new worktrees created by `/task` agents will be based on stale code, leading to merge conflicts or missed changes.

### General rules

- **Never push to main.** All changes go through PRs.
- **Never deploy to production.** Production deploys are human-only.
- **Never set `priority/p0`.** That priority is reserved for humans.
- **Merge your own agents' PRs** when CI is green and ralph has approved.
- **File issues proactively** for discovered problems — don't just observe them.
- **Notify via Telegram** for all significant events, not just when asked. Use `scripts/run-py.sh scripts/tg-notify.py` for every lifecycle event.
- **Default to action.** If a decision is clear and reversible, make it. Only ask for irreversible or ambiguous decisions.
