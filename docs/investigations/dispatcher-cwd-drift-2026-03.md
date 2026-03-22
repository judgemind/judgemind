# Investigation: Dispatcher CWD Drift After Worktree Agent Events

**Issue:** #1497
**Date:** 2026-03-21

## Problem Statement

When the dispatcher spawns or receives completions from `isolation: "worktree"` agents, the Bash tool's cwd drifts into the agent's worktree path (`.claude/worktrees/agent-<id>/`). A plain `cd` back to the main repo often does not stick -- the next Bash invocation still resolves to the worktree. This appears to be an async race condition in Claude Code's worktree lifecycle management.

**Secondary consequence:** If cwd is drifted when the dispatcher spawns a NEW agent with `isolation: "worktree"`, the new worktree is created inside the old one (e.g., `.claude/worktrees/agent-a6bd1d09/.claude/worktrees/agent-abe9e94c`). This was observed in issue #1491.

## Root Cause Analysis

### Observed behavior

| `cd` sticks? | Condition |
|---|---|
| No | Worktree directory still exists on disk, cd attempted immediately after agent event |
| Yes | Worktree directory deleted from disk (auto-cleanup or manual removal) |
| Yes | Enough time elapsed between agent event and cd (async cleanup finished) |

### Root cause

This is a **Claude Code platform behavior**, not a bug in this repo's code. When Claude Code manages `isolation: "worktree"` agents:

1. The platform creates a worktree at `.claude/worktrees/agent-<id>/` relative to the parent's current working directory.
2. The platform's internal process management appears to set or restore the parent's Bash tool cwd to the worktree path at some point during the agent's lifecycle.
3. This cwd update happens **asynchronously** -- it can occur after the parent agent has already run a `cd` back to the repo root.
4. Once the worktree directory no longer exists on disk, the platform's cwd override has no valid target, and `cd` commands work normally.

This is **not fixable in-repo** via code changes to the dispatcher or hooks. The Bash tool's cwd is managed by the Claude Code platform itself, and hooks cannot directly change the parent's cwd (they run in subprocesses).

## Existing Mitigations (already in place)

The following workarounds are already documented and implemented:

1. **`cleanup_worktree.py`** -- Removes the agent's worktree after completion, which eliminates the drift target. Called in the dispatcher's "Processing agent completions" Step 2.

2. **`cd <repo_root>` after cleanup** -- Explicit re-anchor after worktree removal. Works reliably once the directory is gone.

3. **`git -C <repo_root>` defense-in-depth** -- All git commands in the dispatcher use `-C` to operate from the repo root regardless of shell cwd. Documented in `docs/agent/unattended-patterns.md`.

4. **Documentation** -- The issue is documented in:
   - `.claude/skills/dispatcher/SKILL.md` Step 2 ("Clean up worktree and re-anchor cwd")
   - `docs/agent/unattended-patterns.md` ("Dispatcher CWD Drift and `git -C`")

## Can a Hook Fix This?

### PostToolUse hook on Agent (investigated)

A PostToolUse hook with `"matcher": "Agent"` would run after every Agent tool completion. However:

- **Hooks run in subprocesses.** A `cd` in a hook script changes the subprocess's cwd, not the parent Bash tool's cwd. So a hook cannot directly fix the drift.
- **Hooks CAN inject messages.** A hook's stdout is injected into the agent's context. So a hook COULD detect drift and print a warning like: `"WARNING: cwd drifted to .claude/worktrees/. Run cd <repo_root> to re-anchor."` The agent would see this and act on it.
- **The problem is distinguishing drift from normal worktree usage.** When a `/task` subagent runs inside a worktree, its cwd is legitimately inside `.claude/worktrees/`. The drift detection would need to distinguish between "I am a subagent running in my own worktree" (normal) vs. "I am the dispatcher and my cwd has drifted into someone else's worktree" (drift).

### PreToolUse hook on Bash (investigated)

A PreToolUse hook on Bash could detect when commands are about to run from a drifted cwd and warn. However, the same disambiguation problem applies -- it cannot distinguish between "running in my worktree" and "drifted into another agent's worktree."

### Conclusion: hooks can help but cannot fully solve

A PostToolUse hook on Agent completions that prints a cwd re-anchor reminder is a low-cost improvement. It would not prevent drift, but would make it more visible and reduce the chance of the dispatcher forgetting to re-anchor.

The disambiguation problem can be solved by checking an environment variable or marker file that indicates "I am the dispatcher" vs. "I am a subagent." The dispatcher could set a marker file at startup that the hook checks.

## Recommended Approach

### Tier 1: Already done (keep as-is)
- `cleanup_worktree.py` + `cd <repo_root>` after completions
- `git -C <repo_root>` for all dispatcher git commands
- Documentation in dispatcher SKILL.md and unattended-patterns.md

### Tier 2: Add a PostToolUse hook for Agent completions (low effort, moderate value)
Add a PostToolUse hook with `"matcher": "Agent"` that:
1. Checks whether cwd is inside `.claude/worktrees/`
2. If so, prints a warning to stdout reminding the agent to re-anchor
3. Only fires for the dispatcher (not subagents) -- detect via absence of `.agent-lock` in the cwd or presence of a dispatcher marker

This won't prevent drift but will catch cases where the dispatcher forgets to re-anchor after processing a completion.

### Tier 3: Report to Anthropic as a platform improvement (long-term)
The ideal fix is for Claude Code to preserve the parent's cwd when spawning/completing `isolation: "worktree"` agents. This is a platform-level change that cannot be done in-repo.

## Verification Criteria Status

| Criterion | Finding |
|---|---|
| Reliable way to prevent cwd drift | **No** -- this is a Claude Code platform behavior. Not fixable in-repo. |
| Document workaround pattern | **Already done** -- documented in dispatcher SKILL.md and unattended-patterns.md |
| PostToolUse hook on Agent completions | **Feasible** -- would add visibility but cannot prevent drift. Filed as follow-up. |
| Dispatcher can run 5+ agent completions without manual cwd fixups | **Already achievable** with current cleanup_worktree.py + cd pattern. The hook would add redundancy. |
