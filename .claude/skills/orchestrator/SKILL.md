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

Send a `session_started` notification via the bridge. If Telegram is not configured, skip silently — all bridge calls are no-ops when unconfigured.

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

---

## Main Loop

The orchestrator runs a continuous loop:

1. **Refresh state** — check Telegram inbox, stop requests, and pause state
2. **Handle in-flight PRs** — merge any that are ready, fix any that are failing
3. **Fill agent slots** — launch `/task` agents for the next highest-priority issues
4. **Process completions** — handle agent completion/failure notifications
5. **Triage** — close done issues, file new issues for discovered problems
6. **Repeat** until shutdown

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
3. If `bridge.paused` is `True`, skip spawning
4. If `bridge.is_issue_stopped(N)` is `True`, skip that issue
5. Skip issues already being worked on by another slot

Send a `task_started` Telegram notification for each launch.

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
- Send a Telegram notification
- Check if the merged PR triggers a deploy workflow (see CLAUDE.md "Verify deployment")
- For deployed services, watch the deploy workflow to completion

**Do not merge PRs from external contributors or PRs you did not create** unless the user explicitly asks.

### Handling CI failures

If a PR's CI is failing:
- Check if the failure is in a check the agent could fix (lint, test, type error)
- If so, the `/task` agent should already be handling it — check its status file
- If the agent has exited and CI is still failing, log it and notify via Telegram
- Do not attempt to fix another agent's PR from the orchestrator — spawn a new `/task` for it if needed

### Handling merge conflicts

If a PR has merge conflicts:
- The owning `/task` agent should handle rebasing
- If the agent has exited, log it and notify via Telegram
- The orchestrator does not rebase other agents' branches

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

### Outbound notifications

Send Telegram messages for:
- Session started/ended
- Agent launched (issue number and title)
- Agent completed (issue number, PR number, merge status)
- Agent failed (issue number, error summary)
- PR merged
- Deploy succeeded/failed
- Blocker encountered (needs human decision)
- Status summary (when requested)

### Inbound commands

Process commands from the Telegram inbox and responder daemon:

| Command | Action |
|---|---|
| `status` | Handled by responder daemon directly |
| `start #N` | Spawn `/task #N` in the next available slot |
| `stop #N` | Stop spawning work for issue #N; if an agent is working on it, let it finish |
| `pause` | Stop launching new agents; existing agents continue |
| `resume` | Resume launching new agents |
| Free text | Interpret and reply via Telegram — check `result["needs_reply"]` |

### State file integration

The responder daemon communicates via shared state files (see CLAUDE.md "Responder daemon and state files"):

- Read `tmp/orchestrator_state.json` for pause/resume state
- Read `tmp/stop_requests.json` for stop requests
- Read `tmp/tg_inbox.json` for queued start commands and free text

---

## Shutdown

Shutdown triggers:
- User types `/stop` or asks to stop
- `tmp/tg_responder.stop` file is created
- All issues in the queue are complete and no agents are running

Shutdown procedure:
1. Stop launching new agents
2. Wait for all active agents to complete (do not kill them)
3. Merge any remaining green PRs
4. Send `session_ended` Telegram notification
5. Stop the responder daemon (create `tmp/tg_responder.stop`)
6. Print a summary of what was accomplished:
   - Issues completed
   - PRs merged
   - Issues filed
   - Blockers remaining

---

## Rules

- **Never modify committed code directly.** All code changes go through `/task` agents in worktrees.
- **Never push to main.** All changes go through PRs.
- **Never deploy to production.** Production deploys are human-only.
- **Never set `priority/p0`.** That priority is reserved for humans.
- **Merge your own agents' PRs** when CI is green and ralph has approved.
- **File issues proactively** for discovered problems — don't just observe them.
- **Notify via Telegram** for all significant events, not just when asked.
- **Default to action.** If a decision is clear and reversible, make it. Only ask for irreversible or ambiguous decisions.
