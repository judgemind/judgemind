---
description: Summary phase for the per-phase /task-v2 pipeline. Reads the issue body and the git diff, produces a process-summary issue comment (with AC mapping), a conventional commit message, a PR title, and a PR body.
argument-hint: "<agent-id>"
maxTurns: 30
model: haiku
---

# /task-v2-summary skill

Summary phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Maps the ralph-produced diff back to the issue's acceptance criteria and emits three artifacts the daemon needs before opening the PR.

**Prerequisites:** The dispatcher daemon has already (a) run `/task-v2-ralph` with verdict=SHIP, (b) captured the full git diff and changed-file list into the input bundle, (c) written the input bundle to `{worktree}/tmp/dispatcher-input/summary.json`.

**Goal:** Produce `{worktree}/tmp/dispatcher-output/summary.json` with the process-summary comment, commit message, PR title, and PR body. No GitHub writes — the daemon posts the comment and creates the PR after reading this skill's output.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. This subprocess is already a dispatcher-spawned background task.

**IMPORTANT — No side effects.** This phase does not modify code, does not commit, does not push, does not comment on GitHub. The only write is the output JSON at `{worktree}/tmp/dispatcher-output/summary.json`.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/summary.json`. Required fields:

- `agent_id` (str).
- `issue_number` (int).
- `issue_title` (str).
- `issue_body` (str).
- `issue_comments` (list of `{author, date, body}` — non-bots only).
- `ralph_summary` (str) — the 1-3 sentence summary from ralph output.
- `changed_files` (list of path).
- `git_diff` (str) — full unified diff from `git diff origin/main...HEAD`. Post-#2971, ralph's Step 2.5 always commits its work before returning, so the range resolves against a committed HEAD and the diff is non-empty for every non-no-op SHIP. No working-tree-vs-committed-state branching is required on the summary side.
- `worktree_path` (str).
- `repo_root` (str).
- `branch` (str) — worktree branch name.

Optional:

- `plan_acceptance_criteria` (list of str) — from plan output, useful if the issue body AC got edited mid-flight.
- `scope_check` (list) — from plan output; informs the "Scope decisions" section.

If the file is missing or malformed, exit 0 with empty output and `error` field populated; daemon will retry.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/summary.json`:

```
{
  "agent_id": "<echo>",
  "issue_number": <int>,
  "process_summary_md": "<markdown comment body>",
  "commit_message": "<conventional-commits subject + body>",
  "pr_title": "<subject line, matches commit subject without body>",
  "pr_body_md": "<full PR body markdown with Summary + Test plan>",
  "unmet_criteria": ["<criterion text>", ...],
  "pre_pr_check_notes": "<optional notes about lint/tests — prose>"
}
```

`unmet_criteria` signals the daemon: **non-empty means the daemon must NOT open the PR**. Instead it returns the agent to the ralph phase with the list as a hint, or escalates to diagnose if ralph already completed with SHIP.

Exit 0 regardless. The output JSON's shape is the contract — empty `unmet_criteria` == proceed.

---

## Step 1 — Extract acceptance criteria

Read the issue body and `issue_comments`. Identify all `- [ ]` checkboxes under an "Acceptance criteria" heading (or similar). Also capture any criterion mentioned in a non-bot comment that supersedes the original body (re-scoping, adding criteria).

If `plan_acceptance_criteria` is populated, cross-check against your extracted list. If they diverge, prefer the issue body + comments (the source of truth). Note the divergence in `pre_pr_check_notes` so the retro phase can file a follow-up.

## Step 2 — Map each criterion to the diff

For each criterion, determine from `git_diff` and `changed_files` whether it is:

- **Met** — describe specifically how: which file, which function/test, which line range. Cite the test name if a criterion has a `Verify:` line that maps to a test.
- **Not met — post-deploy verification** — criteria that require running against dev (e.g., "GET /api/foo returns 200"). These are legitimately not verifiable pre-deploy; they belong to `/task-v2-verify`.
- **Not met — scope expansion** — criterion was sharpened mid-flight and the diff does not cover it. Add to `unmet_criteria`.
- **Not met — blocked** — criterion is blocked by a dependency that was out of scope. Add to `unmet_criteria` with explanation.
- **Not applicable** — criterion was made obsolete by an earlier PR or the approach shifted. Explain in evidence cell.

If `unmet_criteria` is non-empty, the daemon will NOT open the PR. Add detail to each entry explaining what's missing so the retry hint is actionable.

## Step 3 — Write the process-summary comment

Use this markdown structure in `process_summary_md`. The daemon posts this comment on the issue **before** creating the PR (captures the acceptance-criteria reasoning in the issue thread for maintainer review).

```
## Process Summary

### What was implemented

<2-4 sentences drawn from ralph_summary — what changed, where, and at a high level>

### Acceptance criteria mapping

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | <criterion text, truncate at 80 chars with …> | Met | `path/to/file.py::test_or_function` |
| 2 | <criterion text> | Met | `path/to/other.py` — line range |
| 3 | <criterion text> | Not met — post-deploy | Will be verified by /task-v2-verify |

### Scope decisions

<Any intentional exclusions — what was NOT done and why. Pull from plan.scope_check if present.>

### Follow-ups filed

<Issues the retro phase will file based on scope_check out-of-scope findings. Leave empty if none.>
```

Keep the comment under ~4000 characters. The issue thread gets noisy if summary comments are long.

## Step 4 — Write the commit message and PR title

Conventional-commits format: `<type>(<area>): <short description> (#<N>)`.

Keep the subject under 72 characters.

Derive `type` from the nature of the change:
- `feat` — new user-visible feature.
- `fix` — bug fix.
- `docs` — documentation only.
- `refactor` — no behavior change.
- `perf` — performance improvement.
- `test` — test-only change.
- `chore` — tooling, build, config.
- `cleanup` — removing dead code, deprecated features.
- `spike` — investigation.

Derive `area` from the primary package changed:

| Path | Area |
|---|---|
| `packages/scraper-framework/` | `scraping` |
| `packages/api/` | `api` |
| `packages/web/` | `web` |
| `packages/nlp-pipeline/` | `nlp` |
| `docs/` | `docs` |
| `.claude/` | `agent` |
| `infra/terraform/` | `infra` |
| `scripts/` (agent/dev tooling) | `dx` |
| `.github/workflows/` | `ci` |
| cross-package | most-affected, or omit `(area)` |

If the diff spans multiple areas, pick the dominant one by line count. Example subjects:

- `feat(scraping): capture Orange County PDF metadata (#1234)`
- `fix(ingestion): handle multi-page rulings correctly (#1235)`
- `docs(agent): update task dependencies reference (#1236)`

Set `pr_title` equal to the subject line only (no body — GitHub uses the title separately from the body).

Set `commit_message` to the subject plus an optional body (separated by a blank line). Include `Closes #<N>` in the body if applicable.

**How the commit message reaches main.** The daemon's `push_and_pr` phase runs `git commit --amend -F <message-file>` to rewrite ralph's placeholder commit (`"WIP: ralph output"`) with the `commit_message` from this skill's output. When the PR is squash-merged, GitHub uses the **PR title** for the merged-main commit subject (not the constituent commit messages). So `pr_title` is the on-main authoritative subject; the amended `commit_message` is what maintainers see on the PR's commits tab and in `git log` on the feature branch. Keeping the two in sync (pr_title == subject line of commit_message) is intentional.

## Step 5 — Write the PR body

Use this template for `pr_body_md`:

```
## Summary

<1-3 sentences describing the change and motivation — pull from ralph_summary, sharpen for PR reviewers who have not read the issue>

Closes #<N>

## Test plan

### Automated checks

- [ ] Lint passes (`ruff check` / `npm run lint`)
- [ ] Format check passes (`ruff format --check` / prettier)
- [ ] Tests pass (`pytest` / `npm test`)
- [ ] CI green

### Post-deploy verification

<Fill in with the verification steps specific to change_type — see /task-v2-verify for the matrix>
- [ ] <verification step 1>
- [ ] Verification evidence posted on #<N> (see /task-v2-verify output)
```

If the change has no deployed component (docs, CI, agent config, infra-only with no service restart):

```
### Post-deploy verification

- [ ] N/A — no deployed component (<specify: docs / CI / agent config / …>)
```

If there is a breaking change or schema migration, add a `## Breaking changes` section naming what callers must do.

If new dependencies were introduced, add a `## Dependencies` section listing them with a one-line justification each.

## Step 6 — Write the output JSON

Emit `{worktree}/tmp/dispatcher-output/summary.json` with all fields above. Exit 0.

---

## What this skill does NOT do

- **Does not open the PR.** Daemon does that after consuming this output.
- **Does not post comments.** Daemon posts `process_summary_md` on the issue.
- **Does not commit or push.** Daemon handles git operations. (The daemon's `push_and_pr` runs `git commit --amend -F` to rewrite ralph's placeholder commit with this skill's `commit_message` output — see #2971.)
- **Does not read GitHub directly.** All issue + comment + diff data comes through the input JSON.
- **Does not run tests.** Any pre-PR check reruns happen in ralph's final iteration or the daemon's pre-push hook.

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- This skill is Haiku-tier per spec §18 — keep reasoning tight. The input already contains everything needed; no exploratory Reads or greps beyond the input JSON should be necessary in the common case.
- If the `git_diff` in the input is truncated (>50k chars), the daemon will have flagged it; in that case note "diff truncated" in `pre_pr_check_notes` and base the mapping on the truncated view.
