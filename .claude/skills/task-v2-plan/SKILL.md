---
description: (WIP — dispatcher v2 spike 0.3 stub) Plan phase for the per-phase /task-v2 pipeline. Reads an issue + comments, produces a plan document, scope-check findings, and a go/no-go signal. Called once per task by the dispatcher daemon before /task-v2-ralph.
argument-hint: "#<issue-number>"
maxTurns: 40
model: opus
---

# /task-v2-plan skill (WIP stub)

**Status:** WIP — extracted from `.claude/skills/task/SKILL.md` for dispatcher v2 spike 0.3 (per-phase context budget measurement, issue #2685). Not production-ready; kept for Phase 1 scaffolding per spec §6a.

**Goal:** Read a Judgemind issue, produce a plan document the daemon can feed into `/task-v2-ralph`. No worktree writes, no git operations — pure reading + writing to `{worktree}/tmp/dispatcher-output/plan.json`.

**Input:** `{worktree}/tmp/dispatcher-input/plan.json` with fields:
- `issue_number` (int)
- `issue_title` (str)
- `issue_body` (str)
- `issue_comments` (list of `{author, date, body}` objects, filtered to non-bots)
- `worktree_path` (str)
- `repo_root` (str)

**Output:** `{worktree}/tmp/dispatcher-output/plan.json` with fields:
- `go` (bool) — proceed to ralph?
- `block_reason` (str or null) — if go=false, why
- `plan_text` (str) — human-readable plan for ralph to execute
- `acceptance_criteria` (list of str) — extracted from issue body
- `scope_check` (list of `{search_pattern, locations_found, in_scope}` objects)
- `relevant_files` (list of paths) — files ralph should touch or reference
- `relevant_docs` (list of paths) — docs/specs/ entries that guide this work

---

## Step 1 — Understand the problem

Read the issue body thoroughly, including all comments. Identify:
- The concrete problem or feature.
- The acceptance criteria (typically `- [ ]` checkboxes).
- Related issue / PR references.
- Linked specs in `docs/specs/`.

Examine existing code for patterns. Be consistent with what's there.

## Step 2 — Scope completeness check

Before recommending go, search the codebase for all locations affected by the described change. If the issue mentions fixing or changing X in one file, grep for X across the entire codebase. List all locations that use, render, or implement the same pattern. Either:
- **Expand scope**: list the additional locations in `relevant_files` with a note.
- **Out of scope**: list the additional locations in `scope_check[*].locations_found` with `in_scope: false`, and note in `plan_text` that follow-up issues should be filed for the missed locations.

## Step 3 — Resolve ambiguity

If the issue requires a maintainer decision before you can proceed, set `go=false` and write `block_reason` explaining what decision is needed. Do not guess on ambiguous requirements.

If acceptance criteria are missing or too vague to verify mechanically, set `go=false` with `block_reason="acceptance criteria need sharpening"`.

## Step 4 — Write the plan

For a go=true task, write a concise plan (≤500 words) that covers:
1. What will be changed, per file.
2. What tests will be added or updated.
3. The scope boundary — what is intentionally NOT done.
4. How each acceptance criterion will be verified (pointer to test / diff / behavior).

Write the output JSON to `{worktree}/tmp/dispatcher-output/plan.json`. Exit 0 on success.

## What this skill does NOT do

- Does not install dependencies (daemon's job before ralph).
- Does not modify code (ralph's job).
- Does not commit, push, or open a PR (daemon's job after ralph).
- Does not post issue comments (handled by `/task-v2-summary` after the implementation is done).

## Reminders

- No `$()`, no heredocs, no `python -c`. See CLAUDE.md Critical Rules.
- All temp files go in `{worktree}/tmp/`.
- Prefer MCP for GitHub reads (`mcp__github__get_issue`). Keep `gh` for writes.
