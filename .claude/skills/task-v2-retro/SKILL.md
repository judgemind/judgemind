---
description: (WIP — dispatcher v2 spike 0.3 stub) Retrospective phase for the per-phase /task-v2 pipeline. Reads full agent history (phase_transitions, failures, PR URL), produces zero-to-many retro issue bodies for the daemon to file.
argument-hint: ""
maxTurns: 20
model: sonnet
---

# /task-v2-retro skill (WIP stub)

**Status:** WIP — extracted from `.claude/skills/task/SKILL.md` §5 (retrospective) for dispatcher v2 spike 0.3.

**Goal:** After a task succeeds end-to-end, review the run for workflow-efficiency and preventative-measures findings. Produce zero or more retro issue bodies; the daemon files them as GitHub issues.

**Input:** `{worktree}/tmp/dispatcher-input/retro.json`:
- `issue_number` (int)
- `pr_number` (int)
- `phase_transitions` (list of `{phase, started_at, ended_at, duration_s, outcome}` — the full per-phase timing log)
- `failures` (list of `{category, details, first_seen, last_seen, count}` — dispatcher.failures rows for this agent)
- `ralph_iterations` (int) — how many worker/reviewer cycles ralph needed
- `ci_attempts` (int) — how many times CI ran (>1 means we hit failures and retried)
- `total_duration_s` (int)
- `diff_stats` (object with `files_changed`, `insertions`, `deletions`)

**Output:** `{worktree}/tmp/dispatcher-output/retro.json`:
- `retro_issues` (list of `{title, body, labels, priority}` — one per actionable finding)
- `no_findings` (bool) — true if the run was clean and nothing is worth filing

---

## Step 1 — Workflow-efficiency review

Look at the phase_transitions + failures + ralph_iterations to identify friction:

- **Was there agent work that a script could do cheaper?** Mechanical transformations, boilerplate setup, repeated fix-retry cycles — file a `type/dx` issue.
- **Did CI fail on something the pre-push hook should have caught?** (e.g., a hygiene check that only exists in CI). File a `type/dx` issue to add the check to `.githooks/pre-push`.
- **Did ralph take more iterations than expected?** If >3 iterations, note what the reviewer kept flagging; a clearer task description or better fixture might reduce iterations next time.
- **Did the agent hit permission prompts?** File a `type/dx` issue to add the pattern to `.claude/settings.json` allow-list.

## Step 2 — Preventative-measures review

Look at the bug or feature and ask:

- **What would have caught this earlier?** A lint rule, type check, test, CI check, or runtime assertion. File an area-labeled issue with `type/dx` or `type/bug` depending on severity.
- **Is this a pattern that could recur?** If the fix pattern applies to other scrapers/endpoints/modules, file an audit-and-fix issue.
- **Were there misleading docs/specs?** If the bug was partially caused by stale docs, file a `type/docs` issue.

## Step 3 — Write retro issue bodies

For each finding, produce a `{title, body, labels, priority}` entry:

- `title` — conventional-commits format for the fix that would close it: `feat(...): add ...`, `fix(...): audit ...`.
- `body` — concrete context + concrete next step an agent can pick up.
- `labels` — include `type/dx` or `type/bug` + relevant area label + `agent/ready` if the issue is fully specified.
- `priority` — `priority/p1` (high-leverage: prevents production bugs or saves significant agent time) or `priority/p2` (nice-to-have / one-off friction). **Never `priority/p0`** — human-only.

Each issue should be scoped tightly — one improvement per issue, pickup-able in a single session.

## Step 4 — Handle "no findings" cleanly

If the run was clean (ralph=1 iteration, ci_attempts=1, no failures), set `no_findings=true` and `retro_issues=[]`. That's fine — don't fabricate issues to seem productive.

## Reminders

- Do not file issues — the daemon does that after reading this skill's output.
- Do not `gh` or MCP write here.
- No `$()`, no heredocs.
