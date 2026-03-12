---
description: Opt-in autonomous work queue manager — launches /task agents, merges PRs, triages issues, and communicates via Telegram. Usage: /orchestrator (default 5 slots), /orchestrator 3 (custom slot count), /orchestrator #589 #590 (specific issues only).
argument-hint: "[max-agents | #issue1 #issue2 ...]"
---

# /orchestrator skill

Enable orchestrator mode for the current interactive session. This transforms the session into an autonomous work queue manager that continuously launches `/task` agents, merges completed PRs, triages issues, and communicates via Telegram.

**Orchestrator mode is opt-in.** Interactive sessions are general-purpose by default. The user invokes `/orchestrator` when they want autonomous queue management.

---

## Arguments

- **No arguments** — run with default settings (5 concurrent agent slots, pick from `agent/ready` backlog by priority)
- **`N`** (e.g. `/orchestrator 3`) — set max concurrent agent slots to N
- **`#N1 #N2 ...`** (e.g. `/orchestrator #589 #590`) — work only on the specified issues, in order

---

## Startup

### 1. Telegram setup (if configured)

Initialize the Telegram bridge and start the responder daemon:

```
scripts/tg-responder.py
```

Send a `session_started` notification:

```
scripts/tg-notify.py session_started
```

If Telegram is not configured, both commands exit silently (exit 0) — all bridge calls are no-ops when unconfigured.

### 2. Scan the work queue

List all open `agent/ready` issues sorted by priority:

```
gh issue list --repo judgemind/judgemind \
    --label agent/ready --state open \
    --json number,title,assignees,labels \
    --limit 50
```

Priority order: `priority/p0` > `priority/p1` > `priority/p2` > `priority/p3`. Within the same priority, prefer lower issue numbers (older first).

If specific issues were passed as arguments, filter to only those issues.

### 3. Check for in-flight work

Check for any existing open PRs or assigned issues that may need attention (stale CI, merge conflicts, etc.):

```
gh pr list --repo judgemind/judgemind --state open \
    --json number,title,headRefName,statusCheckRollup,mergeable
```

Handle any in-flight PRs before launching new work (see "PR Merge Policy" below).

### 4. Initialize audit counter

Read `tmp/orchestrator_status.json` to recover the `prs_since_last_audit` counter from a previous session. If the file does not exist or the field is missing, initialize the counter to 0.

---

## Main Loop

The orchestrator runs a continuous loop:

1. **Refresh state** — check Telegram inbox, stop requests, and pause state
2. **Process orchestrator inbox** — read and execute subagent instructions (see "Subagent Instruction Channel" below)
3. **Handle in-flight PRs** — merge any that are ready, fix any that are failing
4. **Sync after merges** — pull latest main after each merge (see "Post-merge sync" in Rules)
5. **Check audit trigger** — if `prs_since_last_audit >= 20`, spawn `/audit` (see "Periodic Audit" below)
6. **Fill agent slots** — launch `/task` agents for the next highest-priority issues
7. **Process completions** — handle agent completion/failure notifications
8. **Triage** — close done issues, file new issues for discovered problems
9. **Repeat** until shutdown

### Slot management

- Default max slots: **5** (overridable via argument)
- Track active agents by worker number and issue number
- When an agent completes, its slot opens immediately
- Never exceed the max slot count
- Launch `/task` agents **without** `isolation: "worktree"` — the skill manages its own worktree

### Spawning agents

For each open slot, pick the next highest-priority unassigned `agent/ready` issue and spawn:

```
/task #N
```

as a background subagent. Before spawning:

1. Call `bridge.refresh_state()` to pick up external pause/resume changes
2. Call `bridge.read_stop_requests()` to consume stop requests
3. Call `bridge.read_orchestrator_inbox()` to process subagent instructions
4. If `bridge.paused` is `True`, skip spawning
5. If `bridge.is_issue_stopped(N)` is `True`, skip that issue
6. Skip issues already being worked on by another slot

**After spawning each agent**, send a `task_started` notification and update the status file:

```
scripts/tg-notify.py task_started <issue_number> "<title>" <worker_number>
```

This sends the Telegram message **and** updates `tmp/orchestrator_status.json` so the responder daemon has accurate context.

### Filtering task notifications

Not all `<task-notification>` messages require orchestrator action. The platform fires notifications for both subagent completions and background command completions. The orchestrator **must** distinguish between the two:

- **Respond to:** Agent completions — notifications where the `<summary>` starts with `"Agent"` (e.g. `"Agent for task #42 completed"`, `"Agent for task #42 failed"`). These represent `/task` or `/audit` subagent results that require slot bookkeeping, Telegram notification, and potential backfill.
- **Ignore silently:** Background command completions — notifications where the `<summary>` starts with `"Background command"` (e.g. `"Background command completed"`, `"Background command failed"`). These are internal operations run by subagents (Gemini reviews, `gh run watch`, test suites, lint runs, etc.) and need no orchestrator action.

**When a background command notification arrives, do nothing.** Do not acknowledge it, do not print a status message, do not send a Telegram notification. Simply continue the main loop. Responding to these creates noise in the conversation without adding value.

### Processing agent completions

When a `<task-notification>` arrives indicating an agent has completed:

**On success:**
```
scripts/tg-notify.py task_completed <issue_number> "<summary>" <worker_number>
```

**On failure:**
```
scripts/tg-notify.py task_failed <issue_number> "<error_summary>" <worker_number>
```

Both commands update the status file automatically. Always send a notification immediately when an agent completes or fails — do not batch them.

---

## Subagent Instruction Channel

Subagents can request actions from the orchestrator by writing to `tmp/orchestrator_inbox.json`. This is a file-based instruction channel similar to `tmp/tg_inbox.json` (Telegram inbox), but for subagent-to-orchestrator communication.

### How subagents write instructions

Subagents use `scripts/orchestrator-request.py` to append entries:

```
scripts/orchestrator-request.py restart_responder --reason "telegram-bridge code updated" --from-issue 733
scripts/orchestrator-request.py notify --message "Found regression in OC scraper" --from-issue 600
scripts/orchestrator-request.py terraform_apply --module telegram-bot --from-issue 712
scripts/orchestrator-request.py run_script --script scripts/tg-set-webhook.sh --from-issue 725
scripts/orchestrator-request.py file_issue --title "Bug report" --description "Details..." --priority p1 --labels area/scraping
```

The script is stdlib-only (no venv needed) and uses file locking for safe concurrent access.

### How the orchestrator reads instructions

In the main loop (step 2), call `bridge.read_orchestrator_inbox()` which returns a list of `OrchestratorInstruction` objects. For each instruction:

| Action | How to handle |
|---|---|
| `restart_responder` | Run `scripts/tg-stop-responder.sh`, reinstall telegram-bridge deps, restart `scripts/tg-responder.py` |
| `terraform_apply` | Run dev terraform apply for the specified module (see "Auto-apply dev terraform" below) |
| `notify` | Send a Telegram notification via `scripts/tg-notify.py notify "<message>"` |
| `run_script` | Execute the specified script (validate it starts with `scripts/` for safety) |
| `file_issue` | Create a GitHub issue with `gh issue create` using the provided title, description, priority, and labels |

**Safety rules:**
- Only the pre-defined action types in `InstructionKind` are accepted — unknown actions are logged and skipped
- `run_script` must validate the script path starts with `scripts/` to prevent arbitrary command execution
- Log all instructions and their outcomes
- Send a Telegram notification confirming each instruction was processed

### Inbox file format

`tmp/orchestrator_inbox.json` is a JSON array of instruction objects:

```json
[
  {"action": "restart_responder", "reason": "code updated", "from_issue": 733, "timestamp": "..."},
  {"action": "terraform_apply", "module": "telegram-bot", "from_issue": 712, "timestamp": "..."},
  {"action": "notify", "message": "Found a regression", "from_issue": 600, "timestamp": "..."}
]
```

The file is atomically read and truncated by `read_orchestrator_inbox()`, just like `read_inbox()` and `read_stop_requests()`.

---

## PR Merge Policy

The orchestrator proactively merges PRs when all conditions are met:

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
  scripts/tg-notify.py pr_merged <pr_number> "<pr_title>"
  ```
- Check if the merged PR triggers a deploy workflow (see CLAUDE.md "Verify deployment")
- For deployed services, watch the deploy workflow to completion
- **If PR touches `packages/telegram-bridge/` or `scripts/tg-responder.py`** — restart the responder daemon (see "Auto-restart responder daemon" below)
- **If PR touches `infra/terraform/`** — run dev terraform apply (see "Auto-apply dev terraform" below)
- **Increment `prs_since_last_audit`** and persist it to `tmp/orchestrator_status.json`. When the counter reaches 20, the next main loop iteration will trigger an audit (see "Periodic Audit" below).

**Do not merge PRs from external contributors or PRs you did not create** unless the user explicitly asks.

### Handling CI failures

If a PR's CI is failing:
- Check if the failure is in a check the agent could fix (lint, test, type error)
- If so, the `/task` agent should already be handling it — check its status file
- If the agent has exited and CI is still failing, log it and notify via Telegram:
  ```
  scripts/tg-notify.py notify "CI still failing on PR #<N> after agent exited — needs attention"
  ```
- Do not attempt to fix another agent's PR from the orchestrator — spawn a new `/task` for it if needed

### Handling merge conflicts

If a PR has merge conflicts:
- The owning `/task` agent should handle rebasing
- If the agent has exited, log it and notify via Telegram:
  ```
  scripts/tg-notify.py notify "PR #<N> has merge conflicts and agent has exited — needs attention"
  ```
- The orchestrator does not rebase other agents' branches

### Auto-restart responder daemon

When a merged PR modifies the responder code (`packages/telegram-bridge/` or `scripts/tg-responder.py`), restart the daemon to pick up the changes. This is quick and runs inline (not delegated to a subagent):

1. Run `scripts/tg-stop-responder.sh` (sends SIGTERM, waits for exit, cleans up PID/stop files)
2. Reinstall telegram-bridge deps if needed
3. Launch `scripts/tg-responder.py` (inherits current flags/config)
4. Verify new PID file exists
5. Send Telegram notification: "Responder daemon restarted after PR #N merged"

If the restart fails, file a p1 issue and notify via Telegram.

### Auto-apply dev terraform

When a merged PR touches files under `infra/terraform/`, the orchestrator automatically applies the changes to the **dev environment only**. Production applies remain human-only. This runs inline in the orchestrator (not delegated to a subagent) because it is a short, well-defined operation.

This same logic also handles `terraform_apply` instructions from the subagent instruction channel (when a subagent requests a targeted module apply via `scripts/orchestrator-request.py terraform_apply`).

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
  scripts/tg-notify.py notify "Terraform apply succeeded for dev after PR #<N> merged"
  ```

#### Failure handling

If the apply fails:
1. Send a Telegram notification immediately:
   ```
   scripts/tg-notify.py notify "Terraform apply FAILED for dev after PR #<N> — filing p1 issue"
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

The orchestrator triggers a codebase health audit every 20 merged PRs using the `/audit` skill.

### Counter management

- Track `prs_since_last_audit` as an integer counter, persisted in `tmp/orchestrator_status.json`.
- After each PR merge, increment the counter by 1.
- On startup, read the counter from the status file (default to 0 if missing).
- The counter persists across orchestrator restarts via the status file.

### Trigger condition

In the main loop (step 5), after handling merges and syncing:

1. Check if `prs_since_last_audit >= 20`.
2. Check that no `/audit` agent is already running (avoid overlapping audits).
3. If both conditions are met and a slot is available, spawn `/audit` as a background subagent.
4. Reset `prs_since_last_audit` to 0 and persist to `tmp/orchestrator_status.json`.
5. Send a Telegram notification:
   ```
   scripts/tg-notify.py notify "Launching periodic audit (20 PRs merged since last audit)"
   ```

### Slot usage

The `/audit` agent occupies one agent slot like any `/task` agent. It creates its own worktree internally. The audit does not block other work — other agents continue running in parallel.

### Manual trigger

The user can also trigger an audit manually via Telegram (`start audit`) or by invoking `/audit` directly. Manual triggers reset the counter to 0.

---

## Issue Triage Policy

The orchestrator proactively manages issues:

### Close done issues
- If all sub-tasks of a parent issue are closed and the parent has no remaining work, close the parent
- Comment with a summary of what was completed

### File new issues
- If an agent reports a problem it cannot solve (blocked, needs human decision), the orchestrator notes it for Telegram notification
- If the orchestrator discovers issues during monitoring (stale PRs, repeated CI failures), file tracking issues with appropriate labels

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

All outbound Telegram notifications MUST use the committed script `scripts/tg-notify.py`. This script wraps the `telegram_bridge` package and handles:

- Sending the Telegram message via the bot API
- Updating `tmp/orchestrator_status.json` so the responder daemon has accurate context
- Persisting worker state to `tmp/orchestrator_state.json`
- Exiting silently (exit 0) when Telegram is not configured

**Available commands:**

| Command | Arguments | When to use |
|---|---|---|
| `scripts/tg-notify.py session_started` | (none) | Orchestrator startup |
| `scripts/tg-notify.py session_ended` | (none) | Orchestrator shutdown |
| `scripts/tg-notify.py task_started` | `<issue> <title> <worker>` | After spawning a `/task` agent |
| `scripts/tg-notify.py task_completed` | `<issue> <summary> <worker>` | Agent completed successfully |
| `scripts/tg-notify.py task_failed` | `<issue> <error> <worker>` | Agent failed |
| `scripts/tg-notify.py pr_merged` | `<pr_number> <title>` | After squash-merging a PR |
| `scripts/tg-notify.py notify` | `<message>` | Free-form notification (blockers, CI issues, deploy status) |

**IMPORTANT:** Always call `scripts/tg-notify.py` after every lifecycle event. Do not rely on remembering to send notifications manually — the script handles both the Telegram message and the status file update atomically. If you skip the notification, the responder daemon will have stale status and give incorrect answers to user queries.

### Outbound notification checklist

Send a notification for **every** lifecycle event:

- [ ] **Session started** — at orchestrator startup
- [ ] **Agent launched** — after each `/task #N` spawn (include issue number, title, worker number)
- [ ] **Agent completed** — when `<task-notification>` reports success (include issue number, summary, worker number)
- [ ] **Agent failed** — when `<task-notification>` reports failure (include issue number, error, worker number)
- [ ] **PR merged** — after each `gh pr merge` (include PR number and title)
- [ ] **Deploy succeeded/failed** — after watching deploy workflow
- [ ] **Terraform apply succeeded/failed** — after applying dev terraform for infra PRs
- [ ] **Blocker encountered** — when an issue needs human decision
- [ ] **Audit triggered** — when `/audit` is spawned (include PR count since last audit)
- [ ] **Session ended** — at orchestrator shutdown

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

The `file_issue`, `discuss`, and `do` commands are classified by the Opus interpreter in the responder daemon and written to the inbox as structured entries with an `action` key. The orchestrator reads these via `bridge.read_inbox()` which returns `Command` objects with the appropriate `CommandKind`. Each command's result dict includes the metadata needed to act on it:

- **`file_issue`**: `result["description"]`, `result["priority"]`, `result["labels"]`, `result["reply_to"]`
- **`discuss`**: `result["message"]`, `result["reply_to"]`, `result["needs_reply"]`
- **`do`**: `result["instruction"]`, `result["reply_to"]`, `result["needs_reply"]`

### State file integration

The responder daemon communicates via shared state files (see CLAUDE.md "Responder daemon and state files"):

- Read `tmp/orchestrator_state.json` for pause/resume state
- Read `tmp/stop_requests.json` for stop requests
- Read `tmp/tg_inbox.json` for queued commands (start, file_issue, discuss, do, and free text)
- Read `tmp/orchestrator_inbox.json` for subagent instructions (restart_responder, terraform_apply, notify, run_script, file_issue)

**The orchestrator MUST update `tmp/orchestrator_status.json` after every state change.** The `scripts/tg-notify.py` script does this automatically for lifecycle events (`task_started`, `task_completed`, `task_failed`, `pr_merged`). For other state changes (pause, resume, slot changes), call `bridge.write_status()` directly or use `scripts/tg-notify.py notify` to trigger a status file update.

The `tmp/orchestrator_status.json` file includes the `prs_since_last_audit` counter alongside the existing fields (`active_agents`, `open_prs`, `recently_completed`, `queue`, `paused`, `stopped_issues`, `updated_at`).

---

## Shutdown

Shutdown triggers:
- User types `/stop` or asks to stop
- All issues in the queue are complete and no agents are running

Shutdown procedure:
1. Stop launching new agents
2. Wait for all active agents to complete (do not kill them)
3. Merge any remaining green PRs
4. Send `session_ended` Telegram notification:
   ```
   scripts/tg-notify.py session_ended
   ```
5. Stop the responder daemon (`scripts/tg-stop-responder.sh`)
6. Print a summary of what was accomplished:
   - Issues completed
   - PRs merged
   - Issues filed
   - Blockers remaining

---

## Rules

### Responsiveness — the orchestrator's primary constraint

The orchestrator must stay responsive to user interaction and Telegram commands at all times. A blocked orchestrator cannot process pause/resume commands, dispatch new work, merge PRs, or reply to Telegram messages.

- **Never do long-running work in the main agent — delegate to subagents.** "Long-running" means anything that might take more than ~10 seconds: code changes, investigations, deep codebase exploration, issue body rewrites, running tests, large file analysis, or multi-step research.
- **The orchestrator's job is: read messages, make quick decisions, dispatch work, send updates.** It is a dispatcher, not an implementer.
- **Allowed in the main agent:** `gh` CLI calls, quick file reads, Telegram sends, short status checks, writing issue comments, updating labels, spawning subagents, terraform apply for dev environments.
- **Everything else = spawn a subagent.** If you are unsure whether something is "quick enough," it is not — delegate it.
- **Never block on a single long operation.** If a `gh run watch` or similar command could take minutes, run it in a way that does not prevent processing other events in the main loop. Prefer polling with short timeouts over blocking waits.

### No direct code changes on main

The orchestrator MUST NOT make any code changes on `main` itself. All code changes must be delegated to `/task` subagents working in worktrees.

- **Prohibited (code changes):** editing source files, modifying configs, writing scripts, updating documentation content, changing Terraform, or any operation that results in a `git commit` on the orchestrator's checkout. If it would show up in `git diff`, delegate it to a `/task` subagent.
- **Allowed (non-code operations):** `gh` CLI calls (issue comments, label changes, PR merges, issue creation/editing), reading files for decision-making, writing to `tmp/`, sending Telegram messages, running `git fetch`/`git pull`, running `terraform apply` for dev environments (applying already-merged code, not changing it). These do not modify committed code and are safe to run inline.

If you catch yourself about to edit a file or stage a commit from the orchestrator agent, **stop and spawn a `/task` subagent instead.**

### Post-merge sync

After each PR merge, the orchestrator MUST pull latest main so that subsequent `/task` agents start from the current tip of the codebase:

```
git fetch origin main
git pull origin main --ff-only
```

Do this **immediately** after every `gh pr merge` call, before spawning new agents or processing the next item in the loop. Without this step, new worktrees created by `/task` agents will be based on stale code, leading to merge conflicts or missed changes.

### General rules

- **Never push to main.** All changes go through PRs.
- **Never deploy to production.** Production deploys are human-only.
- **Never set `priority/p0`.** That priority is reserved for humans.
- **Merge your own agents' PRs** when CI is green and ralph has approved.
- **File issues proactively** for discovered problems — don't just observe them.
- **Notify via Telegram** for all significant events, not just when asked. Use `scripts/tg-notify.py` for every lifecycle event.
- **Default to action.** If a decision is clear and reversible, make it. Only ask for irreversible or ambiguous decisions.
