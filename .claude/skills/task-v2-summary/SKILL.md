---
description: (WIP — dispatcher v2 spike 0.3 stub) Summary phase for the per-phase /task-v2 pipeline. Reads the issue body + the git diff, produces a process-summary comment (AC mapping), a conventional commit message, and a PR body.
argument-hint: ""
maxTurns: 30
model: opus
---

# /task-v2-summary skill (WIP stub)

**Status:** WIP — extracted from `.claude/skills/task/SKILL.md` A.2b + A.3 (PR body template) for dispatcher v2 spike 0.3.

**Goal:** Map the ralph-produced diff back to the issue's acceptance criteria, produce three artifacts the daemon needs before opening the PR.

**Input:** `{worktree}/tmp/dispatcher-input/summary.json`:
- `issue_number` (int)
- `issue_title` (str)
- `issue_body` (str)
- `issue_comments` (list, filtered to non-bots)
- `ralph_summary` (str — from ralph's output)
- `changed_files` (list of paths)
- `git_diff` (str — full unified diff)
- `worktree_path` (str)

**Output:** `{worktree}/tmp/dispatcher-output/summary.json`:
- `process_summary_md` (str) — the markdown comment to post on the issue before PR creation
- `commit_message` (str) — conventional-commits: `feat(area): description (#N)`
- `pr_title` (str) — matches commit subject
- `pr_body_md` (str) — full PR body with Summary + Test plan sections
- `unmet_criteria` (list of str) — any AC the implementation didn't meet (triggers daemon to block/return to ralph)

---

## Step 1 — Extract acceptance criteria

Read the issue body and issue comments. Identify all `- [ ]` checkboxes under an "Acceptance criteria" or similar heading. Also capture any criterion mentioned in comments that supersedes the original body.

## Step 2 — Map each criterion to the diff

For each criterion, determine from the `git_diff` and `changed_files` list whether it is:
- **Met** — describe specifically how: which file, which function/test, which line range.
- **Not met** — explain why (out of scope, blocked, requires post-deploy verification).
- **Not applicable** — explain why.

If any criterion is "not met" and the reason is NOT "post-deploy verification" or "not applicable", add it to `unmet_criteria`. The daemon treats `unmet_criteria[]` as a signal to NOT open the PR yet.

## Step 3 — Write the process summary comment

Use this markdown structure in `process_summary_md`:

```
## Process Summary

### What was implemented
<Brief description of the approach — 2-4 sentences, drawn from ralph_summary>

### Acceptance criteria mapping

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | <criterion text> | Met | <file:function or test> |
| 2 | <criterion text> | Met | <file:function or test> |
| 3 | <criterion text> | Not met | <reason — e.g., requires post-deploy verification> |

### Scope decisions
<Any intentional exclusions — what was NOT done and why>
```

## Step 4 — Write commit message + PR title

Conventional commits: `<type>(<area>): <short description> (#<N>)`. Keep under 72 chars for the subject.

Derive `area` from the primary package changed:
- `packages/scraper-framework/` → `scraping`
- `packages/api/` → `api`
- `packages/web/` → `web`
- `packages/nlp-pipeline/` → `nlp`
- `docs/` → `docs`
- `.claude/` → `agent`
- `infra/terraform/` → `infra`

## Step 5 — Write PR body

```
## Summary

<1-3 sentences describing the change>

Closes #<N>

## Test plan

### Automated checks
- [ ] Lint passes
- [ ] Format check passes
- [ ] Tests pass
- [ ] CI green

### Post-deploy verification
- [ ] <Verification step specific to the change type>
- [ ] Verification evidence posted on issue (see A.8)
```

If the change has no deployed component, use:
```
### Post-deploy verification
- [ ] N/A — no deployed component (docs/CI/tooling only)
```

## Reminders

- No `$()`, no heredocs. All outputs go in the JSON file.
- The daemon does NOT invoke `gh pr create` itself — it reads this output and passes the commit_message + pr_body_md to `gh pr create`.
