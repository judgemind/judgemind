---
description: Dispatcher startup subagent — runs the heavy one-shot startup queries (stale-worktree sweep, stale-assignment cleanup, agent/ready queue scan, trust-check, orphan-PR triage) in an isolated subagent context and returns a compact markdown summary. Keeps the dispatcher's main context clear of ~100k tokens of raw GitHub output per session.
argument-hint: "[max_slots=<N>] [skip=<csv>] [only=<#N1,#N2,...>] [agent_account=<login>]"
---

# /dispatcher-startup skill

**Purpose.** Dispatcher startup historically burns roughly 100k tokens of main-context on raw `gh` / `mcp__github__*` output before the first `/task` agent spawns — `list_issues` over a busy `agent/ready` queue can exceed the MCP token ceiling on its own, and each `gh pr view --json statusCheckRollup,mergeable,mergeStateStatus` adds ~12k characters. The dispatcher rotates every ~40 loop iterations, so the main-context tax is paid fresh on every restart. This skill moves that work into a short-lived isolated subagent that returns ~40 lines of markdown.

**Called by:** `/dispatcher` at startup (once per session), replacing inline startup steps 2–5. Not called by `/task`, `/ralph`, `/audit`, `/spotcheck`, or any other skill.

> **MCP-first for GitHub reads.** Same rules as `/dispatcher` — prefer `mcp__github__list_issues`, `mcp__github__list_pull_requests`, `mcp__github__search_issues`, `mcp__github__get_pull_request`, and `mcp__github__get_pull_request_files` over `gh ... --json`. Load schemas with `ToolSearch query="select:mcp__github__list_issues,mcp__github__list_pull_requests,mcp__github__search_issues,mcp__github__get_pull_request"` on first use. Writes stay on `gh` until the MCP write path has a token (see `docs/agent/gh-to-mcp-migration.md`).
>
> **Key MCP gotchas** (copied from `/dispatcher` because they bite immediately):
>
> - Params are **snake_case** (`per_page`, `issue_number`), not camelCase.
> - `list_issues` on a busy queue can exceed the context budget. Use `per_page: 20` and paginate if needed, or fall back to `gh issue list --json number,title,labels,assignees --label agent/ready` for the tighter shape.
> - `get_pull_request_status` only returns legacy commit statuses (Vercel etc.), not GitHub Actions check runs. For CI check-run status, use `mcp__github__get_pull_request` (returns `mergeable`, `mergeable_state`) and/or `gh pr view <N> --json statusCheckRollup,mergeable,mergeStateStatus` as a fallback.

---

## Arguments

Arguments arrive as a whitespace-separated `key=value` string via `$ARGUMENTS`. Parse the following keys; any unrecognised key is ignored:

- **`max_slots=<N>`** — how many top-of-queue candidates to surface for dispatch (default `5`). Only the top `max_slots` candidates are trust-checked; the rest are listed as "queue depth" without per-issue detail.
- **`skip=<csv>`** — comma-separated substrings; any issue whose title contains one of these is excluded from the surfaced candidates (e.g. `skip=dispatcher-v2,spotcheck`). Case-insensitive substring match.
- **`only=<#N1,#N2,...>`** — if set, restrict candidates to the listed issues (in order). Overrides `max_slots` and `skip` for selection, but trust-check and orphan-PR triage still run over the full set.
- **`agent_account=<login>`** — GitHub login for the agent account used for stale-assignment cleanup (default `drewthaler`; agent account swap per `~/.claude/projects/.../memory/MEMORY.md`). If no argument is given, read the memory file and use whatever it says; otherwise fall back to `drewthaler`.

---

## What this skill does

Runs five short-lived sub-steps, then returns a single compact markdown summary.

### 1. Worktree sweep

Run the sweep script once and capture its summary line:

```
scripts/sweep_stale_worktrees.sh
```

The last line is `Cleaned up N stale worktree entries`. Record `N` for the **Cleanups** section of the summary. If `N > 5`, note it — growing orphan counts are a health signal.

### 2. Stale-assignment cleanup

Issues assigned to `<agent_account>` with `agent/ready` that have no open PR referencing them are leftovers from crashed sessions — they block future pickup.

1. List open `agent/ready` issues via MCP (`per_page: 50`). Filter client-side for those whose `assignees` array contains `<agent_account>`.
2. For each matched issue, check whether an open PR references it. Reuse the PR list fetched in step 4 below (single MCP call for both steps — do not duplicate the query). Match on either the PR body containing `#<N>` or the branch name containing the issue number.
3. **If an open PR exists:** unassign the issue (work is partial — leave the PR open as evidence):
   ```
   gh issue edit <N> --repo judgemind/judgemind --remove-assignee <agent_account>
   ```
4. **If no open PR exists:** unassign with the same command. Fully stale assignment.
5. Record each unassignment in the **Cleanups** section of the summary.

Edge cases — see `/dispatcher` SKILL.md §3. The "no open PR" heuristic handles just-assigned-by-current-session cases safely because the assigning agent will re-assign when it pushes its PR.

### 3. Queue scan — `agent/ready` sorted by priority

```
mcp__github__list_issues
  owner: "judgemind"
  repo: "judgemind"
  labels: ["agent/ready"]
  state: "open"
  per_page: 50
```

If the returned payload is too large (>100 KB or >2000 lines), fall back to:

```
gh issue list --repo judgemind/judgemind --label agent/ready --state open \
    --json number,title,labels,assignees --limit 50
```

`--json number,title,labels,assignees` is deliberately tight — no `body`, no `createdAt`, no `milestone`. That alone trims ~40% vs. the default shape.

Sort by priority (`priority/p0` > `priority/p1` > `priority/p2` > `priority/p3`), then by issue number ascending within the same priority. Apply filters in this order:

1. **`only=`** — if set, select those issues in the given order (missing issues are listed as "skipped: not found in queue").
2. **`skip=`** — drop any issue whose title contains one of the substrings (case-insensitive).
3. **Assignment** — drop issues already assigned to another agent whose worktree still exists in `git worktree list`. Re-use the `git worktree list` output from step 1 if you captured it there.

Take the top `max_slots` remaining. Record the overall queue depth (`total_ready`, `after_filters`) for the summary.

### 4. Trust-check the top `max_slots`

For each of the top `max_slots` candidate issues, run:

```
scripts/check-issue-author.sh <N>
```

- **Exit 0 (trusted):** keep in the surfaced queue.
- **Exit 1 (untrusted):** drop from the surfaced queue. Remove `agent/ready`, add `status/triage`:
  ```
  gh issue edit <N> --repo judgemind/judgemind --remove-label agent/ready --add-label status/triage
  ```
  Record the drop in the **Skipped (untrusted)** section. Do NOT spawn, do NOT surface this issue to the dispatcher.
- **Exit 2 (error):** leave labels alone, record in **Skipped (error)** with the exit text, and move on. The next dispatcher cycle will retry.

**Only trust-check the top `max_slots`.** Untrusted issues deeper in the queue will be handled when they bubble up in future cycles.

### 5. Orphan PR triage

```
mcp__github__list_pull_requests
  owner: "judgemind"
  repo: "judgemind"
  state: "open"
  per_page: 50
```

This is the same PR list reused in step 2. For each PR, classify:

- **Ready-to-merge:** CI green, no conflicts, PR was created by the agent account (`user.login == <agent_account>`), ralph-approved (heuristic: branch name starts with `worktree-agent-`). Use `mcp__github__get_pull_request` for `mergeable` / `mergeable_state`; for full check-run rollup fall back to `gh pr view <N> --repo judgemind/judgemind --json statusCheckRollup,mergeable,mergeStateStatus`. **Do not merge from this skill** — just classify and list. The dispatcher merges in its main loop.
- **Orphaned-conflicting:** agent-authored, `mergeable_state` is `dirty` or `CONFLICTING`, and no active worktree exists for the branch (`git worktree list` check).
- **Orphaned-CI-failing:** agent-authored, any required check has `FAILURE`, no active worktree exists.
- **Other-contributor:** `user.login != <agent_account>` — hand off to the human in the summary, never touch.
- **In-flight:** an active worktree exists for the branch — do not classify as orphaned. The owning agent will handle it.

Cap the orphan-PR section at the 10 most recently updated PRs per class.

**CI check calls are the expensive ones** (~12k characters each). To keep the skill's internal context manageable, only call `get_pull_request` / `gh pr view` for agent-authored PRs with no active worktree — i.e. candidates for "orphaned-conflicting" or "orphaned-CI-failing". Skip it for other-contributor and in-flight PRs.

### 6. Side effects — explicit allow-list

This skill performs exactly these write operations, and nothing else:

1. `scripts/sweep_stale_worktrees.sh` side effects (metadata dir removals).
2. `gh issue edit <N> --remove-assignee <agent_account>` for stale assignments.
3. `gh issue edit <N> --remove-label agent/ready --add-label status/triage` for untrusted issues.

**Explicitly forbidden:**

- Never write to `tmp/dispatcher_state.json` or `tmp/dispatcher_checkpoint.md` — those are owned by `/dispatcher`.
- Never spawn `/task`, `/audit`, or any other Agent subagent.
- Never call `gh pr merge`, `gh pr create`, `gh issue create`, `gh issue close`, or `gh issue comment`.
- Never run `terraform apply` or any other infra command.

---

## Output — compact markdown summary

Write the summary to `{worktree}/tmp/dispatcher-startup-summary.md` and print it to stdout so the dispatcher parent sees it in the skill return value. Format:

```
## Dispatcher Startup

### Queue (top <max_slots> of <total_ready> ready)
1. [#<N>](url) p<P> <truncated title>
2. [#<N>](url) p<P> <truncated title>
...

### Cleanups
- Worktree sweep: cleaned <N> stale entries
- Unassigned #<N> (no open PR — stale)
- Unassigned #<M> (PR #<PR> open, work partial)

### Orphan PRs
- Ready-to-merge: PR #<M> (#<N>) — CI green, no conflicts
- Orphaned-conflicting: PR #<M> (#<N>) — rebase needed
- Orphaned-CI-failing: PR #<M> (#<N>) — <failing check>
- Other-contributor: PR #<M> (<login>) — hand off

### Skipped
- Untrusted: #<N> <title> — moved to status/triage (association: <X>)
- Filter: #<N> <title> — matched skip=<pattern>
- Error: #<N> — check-issue-author.sh exit 2
```

**Hard caps:**

- **Total length: ≤40 lines.** If the content would exceed that, truncate each section proportionally (keep queue > cleanups > orphan-PRs > skipped). The dispatcher does not need every detail — counts and top entries are enough.
- **Titles truncated to 80 characters** (suffix with `…` if truncated).
- **No raw JSON output.** Every section is structured markdown.
- **Link format: `[#N](https://github.com/judgemind/judgemind/issues/N)`** — keeps the summary clickable for the user if shared.

If any section is empty, render it as `- (none)` rather than omitting the heading, so the dispatcher can verify that step ran.

---

## Reminders

- No `$()`, no heredocs, no `python -c` — see CLAUDE.md Critical Rules.
- All temp files go in `{worktree}/tmp/`, not `/tmp/`.
- Parallelize independent MCP calls where possible (step 3 and step 5 have no dependency on each other once step 2 has the PR list).
- If an MCP call errors, fall back to the `gh` equivalent immediately — do not retry the MCP call in a loop.
- This skill is expected to run in ≤60 seconds. If it stalls, the dispatcher will not be able to start work.
