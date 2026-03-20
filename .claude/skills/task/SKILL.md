---
description: Pick up and complete a Judgemind GitHub issue autonomously — from worktree setup through PR and review request. Usage: /task (next ready issue), /task #42 (specific issue), /task scrapers (natural-language filter).
argument-hint: "[#issue | category | next] [--max-workers N]"
maxTurns: 200
---

# /task skill

Pick up one issue from the Judgemind backlog and complete it autonomously. Do not ask for confirmation at any point — work through every step and stop only when the PR is green and review has been requested (or when an investigation task has posted its findings and unblocked any dependents).

---

## Step 0 — Ensure worktree exists

Check whether you are already working inside a worktree (i.e. `{worktree}` is set and the directory exists). If not, run the setup script:

```
scripts/start-worker.sh
```

If a `--max-workers N` flag was passed in your arguments, pass it through to the script:

```
scripts/start-worker.sh --max-workers N
```

This provides a hard programmatic limit on the number of concurrent worker worktrees, enforced by the script itself. The orchestrator passes this flag to prevent exceeding its slot limit.

This resolves the repo root, prunes stale worktrees, claims the lowest available worker number, creates the worktree, configures git hooks, and creates the `tmp/` directory.

**Record the printed worktree path** — it is `{worktree}` for the rest of the session. All subsequent work happens inside `{worktree}`.

If you are already in a worktree, skip this step.

### Status file setup

After creating or entering a worktree, set up the agent status file so the orchestrator can monitor progress. Derive the worker number from the worktree path (e.g. `worker-2` → `2`).

The status file lives at `{repo_root}/tmp/agent-status/worker-N.txt` (in the **repo root's** `tmp/`, not the worktree's `tmp/`). Create the directory if needed:

```
mkdir -p {repo_root}/tmp/agent-status
```

The status file format is:

```
issue: #<N>
phase: <phase>
updated: <ISO-8601 timestamp>
summary: <one-line description of current activity>
```

Phases (in typical order): `claiming`, `setup`, `ralph-worker (iteration N)`, `ralph-reviewer (iteration N)`, `pushing`, `ci-watch`, `ci-fix`, `merging`, `deploying`, `verifying`, `retrospective`, `done`, `blocked`.

**Write a status update at every major step transition** — use the Write tool to overwrite the status file. The first update should be written immediately after worktree setup with phase `claiming`.

---

## Step 1 — Identify the issue

Interpret `$ARGUMENTS` as follows:

### Empty or "next"
List all open, unassigned `agent/ready` issues and pick the highest-priority one:
```
gh issue list --repo judgemind/judgemind \
    --label agent/ready --state open \
    --json number,title,assignees,labels \
    --limit 20
```
Priority order: `priority/p0` > `priority/p1` > `priority/p2` > `priority/p3`. Within the same priority, prefer lower issue numbers (older issues first). Skip issues already assigned to another agent unless their worktree no longer exists in `git -C $REPO_ROOT worktree list`.

### `#N` (e.g. `/task #42`)
Work on that specific issue regardless of its current labels or assignment. Fetch it:
```
gh issue view 42 --repo judgemind/judgemind --json number,title,body,labels,assignees
```

### Natural language (e.g. `/task scrapers`, `/task next perf bug`, `/task SF tentatives`)
List `agent/ready` issues, then pick the one that best matches the description. Prefer exact label or area matches; fall back to title/body keyword matches. If multiple candidates are equally good, pick the highest-priority unassigned one. Briefly note which issue you chose and why before proceeding.

---

## Step 2 — Claim the issue and rename the conversation

Assign it to yourself:
```
gh issue edit <N> --repo judgemind/judgemind --add-assignee @me
```

Write the claim comment to a temp file, then post it:
```
gh issue comment <N> --repo judgemind/judgemind --body-file {worktree}/tmp/claim_comment.txt
```
Comment content: `Picking this up in worker-N.`

**Rename this conversation** so it is identifiable in the sidebar:
- Format: `#<N> — <short title>` (drop any `[AREA]` prefix tag from the issue title)
- Run: `/rename #<N> — <short title>`

---

## Step 3 — Create todo list for progress tracking

After claiming the issue, create todos using `TaskCreate` to track your major workflow steps. This makes progress visible and prevents skipping steps.

**For implementation tasks (Path A):**
1. "Set up dependencies" — venvs/node_modules for affected packages
2. "Implement and review (ralph loop)" — the core implementation phase
3. "Commit and push" — stage, commit, create PR
4. "Watch CI and fix failures" — monitor CI, resolve any failures
5. "Verify no merge conflicts" — check mergeable status
6. "Update PR test plan" — check off test plan items
7. "Merge PR" — squash merge after CI is green
8. "Verify deployment and post evidence" — watch deploy, verify feature works, post evidence comment
9. "Retrospective" — identify workflow efficiencies and preventative measures
10. "Remove worktree" — cleanup

**For investigation tasks (Path B):**
1. "Investigate and document findings"
2. "File follow-up issues"
3. "Post summary and request review"
4. "Unblock dependent issues"
5. "Retrospective" — identify workflow efficiencies and preventative measures
6. "Remove worktree"

Mark each todo `in_progress` when you start it and `completed` when done. If a task has fewer than 3 steps total (e.g. a trivial fix), skip todo creation.

---

## Step 4 — Determine the response type and execute

Read the issue body thoroughly, including linked issues. Check `docs/specs/` for relevant guidance. Look at existing code for patterns — be consistent with what's already there.

If the issue requires a maintainer decision before you can proceed: comment on it, add `status/blocked`, and stop. Do not guess on ambiguous requirements.

---

### Path A: Implementation task (feature, bug fix, refactor)

Follow the full PR Workflow defined in CLAUDE.md. **All commits must be on the worktree branch — never on `main`.** Summary of required substeps:

**IMPORTANT — Completion contract:** This task is NOT done after implementation or after ralph says SHIP. The task requires completing ALL substeps A.1 through A.9. After ralph returns, you MUST continue with A.3 (commit/push), A.4 (merge conflicts), A.5 (CI), A.6 (PR update), A.7 (merge), A.8 (deploy verification), and A.9 (retrospective). Stopping after ralph is a bug — see issue #721.

#### A.1 — Set up dependencies
Write status: `phase: setup`, `summary: Installing dependencies for <packages>`.

For Python packages you will touch, create a venv:
```
python3.12 -m venv {worktree}/packages/<pkg>/.venv
```
Then install: `.venv/bin/pip install -e ".[dev]" --quiet`

For TypeScript packages: `npm install` from the package directory.

Skip this for Terraform-only or docs-only tasks.

#### A.2 — Implement and review (ralph loop)
- **For testable code tasks** (Python, TypeScript): use the `/ralph` loop — iterative work-then-review with fresh context each iteration. See `.claude/skills/ralph/SKILL.md`. This replaces the old `/tdd` + self-review steps. `/ralph` handles implementation (TDD), pre-PR checks, and cross-perspective review internally. It returns when the reviewer subagent says SHIP.
- **For non-testable tasks** (Terraform, DB migrations, CI/CD, docs): implement directly, then run all applicable pre-PR checks (see CLAUDE.md §Pre-PR Checks) and review your own diff before continuing.
- If `/ralph` exits with a blocker (STUCK or max iterations), the issue has already been commented on and labeled `status/blocked`. Clean up the worktree (`scripts/end-worker.sh {worktree}`) and stop.

**POST-RALPH CHECKPOINT — Do not skip this.** After `/ralph` returns:
1. Read `{worktree}/tmp/ralph/ralph-done.txt` to confirm ralph completed with SHIP status.
2. Read `{worktree}/tmp/ralph/review-result.txt` to verify the final verdict is SHIP.
3. If both confirm SHIP, **immediately continue to A.3 below.** Do not stop, do not return, do not consider the task done. The code is implemented but not yet committed, pushed, or merged — the task is only halfway complete.

**POST-RALPH SELF-RECOVERY GUARD:** Before proceeding to A.3, verify that the task is genuinely incomplete by running these checks:
1. Run `git -C {worktree} status` to confirm there are uncommitted changes (there should be — ralph implements but does not commit).
2. Run `git -C {worktree} log --oneline -1` to see the latest commit — it should NOT contain the current issue number (the implementation hasn't been committed yet).
3. Check whether a PR already exists for this branch: `gh pr list --repo judgemind/judgemind --head <branch-name> --json number --limit 1`. It should return an empty list (no PR yet).

If any of these checks show that work remains (uncommitted changes exist, no PR yet), you MUST continue to A.3. Do not exit. Do not return. Do not consider the task done. Exiting at this point is a critical workflow failure (#721).

#### A.3 — Stage, commit, and push
Write status: `phase: pushing`, `summary: Staging, committing, and pushing to remote`.

Stage the files you changed (prefer naming specific files over `git add .`):
```
git -C {worktree} add <files>
```

Write the commit message to a file, then commit:
```
git -C {worktree} commit -F {worktree}/tmp/commit_msg.txt
git -C {worktree} push -u origin <branch>
```
Commit message format: `feat(area): description (#N)` (conventional commits).

Immediately open a PR after the first push — never push without creating one. **Before creating, check for duplicate PRs** to avoid wasting CI minutes on conflicting duplicates:

```
source {worktree}/scripts/preflight.sh
preflight_no_duplicate_pr <N>
```

- If it returns **0** (duplicate found), the existing PR number is printed to stdout. **Adopt that PR** instead of creating a new one — push to the existing branch and use `gh pr edit` to update the body if needed.
- If it returns **1** (no duplicate), proceed to create the PR normally.
- If it returns **2** (error), proceed to create the PR (fail-open).

The PR body must include `Closes #N` so the unblock workflow fires on merge.

**PR body template — use this structure for all PRs:**

Write the PR body to `{worktree}/tmp/pr_body.txt` using this template:

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
- [ ] <Verification step from the table in A.8, specific to this change type>
- [ ] Verification evidence posted on issue (see A.8)
```

The **Post-deploy verification** section must include at least one concrete verification step appropriate for the change type (see the verification table in A.8). For changes with no deployed component (docs, CI config, tooling scripts not deployed), replace the post-deploy section with:

```
### Post-deploy verification
- [ ] N/A — no deployed component (docs/CI/tooling only)
```

Create the PR:
```
gh pr create --repo judgemind/judgemind \
    --title "..." \
    --body-file {worktree}/tmp/pr_body.txt \
    --base main
```

#### A.4 — Verify no merge conflicts
```
gh pr view <PR-N> --repo judgemind/judgemind --json mergeable,mergeStateStatus
```
If `mergeable` is `CONFLICTING`, rebase and resolve:
```
git -C {worktree} fetch origin main
git -C {worktree} rebase origin/main
```
Resolve conflicts, `git rebase --continue`, then push with `--force-with-lease`.

#### A.5 — Monitor CI and iterate until green
Write status: `phase: ci-watch`, `summary: Watching CI run <run-id>`.

```
gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact
```
If CI fails: write status `phase: ci-fix`, `summary: Fixing CI failure: <brief reason>`. Diagnose, fix locally, push, return to A.4. Repeat until all checks pass.

#### A.6 — Update the PR test plan
Fetch the current PR body, check off the **Automated checks** items that passed in CI. Do NOT check off **Post-deploy verification** items yet — those are checked in A.8 after merge and deploy. Write the updated body to `{worktree}/tmp/pr_body.txt`, then:
```
gh pr edit <PR-N> --repo judgemind/judgemind --body-file {worktree}/tmp/pr_body.txt
```

#### A.7 — Merge the PR
Write status: `phase: merging`, `summary: Squash merging PR #<N>`.

The PR has passed the ralph loop review (A.2) and CI is green. Merge it:
```
gh pr merge <PR-N> --repo judgemind/judgemind --squash --delete-branch
```

**Dependent issues will be unblocked automatically** by the `unblock-issues` workflow when the PR merges. No manual unblocking needed.

#### A.8 — Verify deployment and post evidence (MANDATORY)
Write status: `phase: deploying`, `summary: Watching deploy pipeline for <workflow>`.

**A task is NOT done when the PR merges. A task is done when the change is deployed, verified working, AND verification evidence is posted.** The worktree stays alive until verification passes.

**Determine if this change has a deployed component:**
- Changes to `packages/api/`, `packages/scraper-framework/`, `packages/web/`, `infra/terraform/`, or scripts run via ECS → **has deployed component** → continue to Step 1.
- Changes to docs, CI config, `.claude/`, tooling scripts, or library code with no deployed service → **no deployed component** → skip to the evidence comment (Step 3) and post a skip-reason comment.

**Step 1 — Watch the deploy workflow:**

1. Identify the relevant deploy workflow based on which packages were modified:
   - `packages/api/` or API routes → `deploy-api.yml`
   - `packages/scraper-framework/` or scraper code → `deploy-scraper.yml`
   - `packages/web/` or frontend → `deploy-production.yml`
   - `infra/terraform/` → `terraform.yml`
2. Watch the deploy workflow that triggers on the merge to `main`:
   ```
   gh run list --repo judgemind/judgemind --workflow "<deploy-workflow>.yml" --branch main --limit 1 --json databaseId -q '.[0].databaseId'
   gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact
   ```
3. If the deploy **fails**: file a new `priority/p1` issue describing the deploy failure, reference the merged PR, and add `agent/ready`. Do NOT consider the original task complete — comment on the original issue noting the deploy failure and linking the new issue.
4. If the deploy **succeeds**: continue to Step 2.

**Step 2 — Functional verification (required):**

Write status: `phase: verifying`, `summary: Verifying feature works in dev environment`.

A successful deploy only means the new image is running — not that the service works. Verify the feature is actually functional:

| Change type | Verification | Required evidence |
|---|---|---|
| **DB migration + code** | Confirm migration applied (column/table exists via `scripts/dev-db-query.sh`) AND service processes a request without errors | DB query output showing the column/table exists + a successful request/response |
| **API endpoint** | Hit the endpoint on dev (`curl https://dev.api.judgemind.org/graphql`), confirm expected response shape and no errors | The curl response (status code + relevant body snippet) |
| **Ingestion pipeline** | Confirm the worker processes at least one message successfully (check ECS logs via `scripts/ecs-logs.sh /ecs/judgemind-ingestion-worker-dev --lines 50`) | Log lines showing successful message processing |
| **Scraper** | Check ECS logs for the next scheduled run, confirm documents are captured without errors | Log lines showing successful document capture |
| **Frontend** | Confirm the affected page loads on `dev.judgemind.org` and renders the expected content | Screenshot via `scripts/screenshot.py` or the page content showing the feature works |
| **DX/tooling** | Run the tool in a representative scenario and confirm expected output | Command output showing the tool works correctly |
| **Backfill / data migration script** | Execute the script against dev via `scripts/ecs-run-task.sh` (or locally if appropriate). Confirm the expected data changes applied — e.g., query dev DB via `scripts/dev-db-query.sh` to check row counts, null rates, or sample records. | DB query results showing the data changed (e.g., "2444/2444 rulings now have ruling_text_html") |

**Script-producing tasks:** If a task produces a backfill, migration, or one-off fixup script that is meant to be run, executing it on dev and verifying results is part of the definition of done. When filing issues that include "create a backfill script" or similar, always include "backfill executed on dev and results verified" in the acceptance criteria.

If functional verification **fails**: diagnose the issue. If it's a simple fix, fix it in a follow-up PR. If it's complex, file a `priority/p1` issue with details, reference the merged PR, and add `agent/ready`.

**Step 3 — Post verification evidence comment (MANDATORY — no exceptions):**

After verification succeeds (or after determining the change has no deployed component), you MUST post a verification evidence comment on the issue. This is a hard gate — the task cannot proceed to A.9 without this comment.

Write the comment to `{worktree}/tmp/verification_evidence.txt`, then post it:
```
gh issue comment <N> --repo judgemind/judgemind --body-file {worktree}/tmp/verification_evidence.txt
```

**For deployed changes**, the comment must include concrete evidence from the verification table above. Example formats:

```
## Verification Evidence

**Change type:** API endpoint
**Environment:** dev

**Evidence:**
curl response from dev.api.judgemind.org:
- Status: 200
- Response body (relevant snippet):
  {"data":{"ruling":{"id":"abc123","rulingTextHtml":"<p>The motion is GRANTED...</p>"}}}

Post-deploy verification: PASSED
```

```
## Verification Evidence

**Change type:** Backfill script
**Environment:** dev

**Evidence:**
DB query after backfill:
SELECT COUNT(*) FROM rulings WHERE ruling_text_html IS NOT NULL;
-- Result: 2444 (was 0 before backfill)

SELECT COUNT(*) FROM rulings WHERE ruling_text_html IS NULL;
-- Result: 0

Post-deploy verification: PASSED
```

**For non-deployed changes** (docs, CI, `.claude/`, tooling), the comment must state the skip reason:

```
## Verification Evidence

**Change type:** Documentation / agent workflow
**Skip reason:** No deployed component — changes are to .claude/skills/ and CLAUDE.md only.

Post-deploy verification: N/A (no deployed component)
```

**GATE CHECK:** Do not proceed to A.9 until the verification evidence comment has been posted. A task closed without a verification evidence comment is a workflow failure.

After posting, update the PR test plan to check off the post-deploy verification items:
1. Fetch the current PR body.
2. Check off the **Post-deploy verification** checkboxes.
3. Write updated body to `{worktree}/tmp/pr_body.txt` and update with `gh pr edit --body-file`.

#### A.9 — Proceed to retrospective

Continue to Step 5.

---

### Path B: Investigation task

Write findings to `docs/investigations/<slug>-<YYYY-MM>.md` and/or into the issue body.

#### B.1 — File follow-up issues for every actionable finding

Do not just list recommendations — **create GitHub issues** for each concrete next step so the work is tracked and can be picked up by agents. For each follow-up:

- Write the issue body to `{worktree}/tmp/followup_N.txt`, then create it with `gh issue create --body-file`.
- Reference the investigation as the parent: include `Parent: #<investigation-issue>` in the body.
- Label with appropriate area and type labels.
- Add `agent/ready` if the issue is fully specified and ready for work. If it requires a human decision first, note that in the body and omit `agent/ready`.

If the investigation reveals no actionable follow-ups (everything is working as expected), state that explicitly in the findings.

#### B.2 — Post summary and request review

Post a summary comment on the investigation issue listing the findings and linking all follow-up issues created. Add the `status/review` label.

Then manually unblock any issues that were waiting on this one. Search for them:
```
gh issue list --repo judgemind/judgemind --state open \
    --search "Blocked by #<your-issue>" \
    --json number,title,body
```
For each result, check every `Blocked by #X` line. If **all** referenced issues are now closed:
1. Remove the `status/blocked` label and add `agent/ready`.
2. Remove the resolved `Blocked by #N` lines from the issue body (write to a temp file and use `gh issue edit <N> --body-file`).

If any blocker is still open, leave the issue as blocked.

Continue to Step 5.

---

### Path C: Large or ambiguous task

Break into sub-tasks first (see CLAUDE.md §Creating Sub-Tasks), label them `agent/ready`, then pick up the first sub-task (restart from Step 1).

If you only create sub-tasks and do not pick one up in this session, clean up: `scripts/end-worker.sh {worktree}` (skip Step 5 — no retrospective needed for task breakdown).

---

## Step 5 — Retrospective

Write status: `phase: retrospective`, `summary: Filing retro issues`.

After completing a task (Path A or Path B), reflect on the work before cleaning up. This step produces concrete improvements to the codebase and workflow — not just observations.

### 5a — Workflow efficiency

Review what you did during this task and ask:

- **Was there agent work that a script could do cheaper?** For example: boilerplate setup, repeated lint-fix-retry cycles, mechanical transformations, or data gathering that could be a CLI tool. If so, file an issue to create the script/tool.
- **Did you hit permission prompts or workflow friction that slowed you down?** If so, file an issue to add the pattern to CLAUDE.md's "Unattended Operation Patterns" section or to `.claude/settings.json`.
- **Did the ralph loop take more iterations than necessary?** If a clearer task description, better test fixtures, or a pre-built utility would have reduced iterations, file an issue for that.

### 5b — Preventative measures

Review the bug or problem you just fixed and ask:

- **What would have caught this earlier?** Could a lint rule, type check, test, CI check, or runtime assertion have detected this class of bug before it reached production? If so, file an issue to add that check.
- **Is this a pattern that could recur?** If the same kind of bug could appear in other scrapers, endpoints, or modules, file an issue to audit and fix those too — or to add a shared utility/base class that prevents the bug by construction.
- **Were there missing or misleading docs/specs?** If the issue was caused or complicated by stale documentation, file an issue to update it.

### 5c — File issues

For each actionable finding from 5a and 5b:

- Write the issue body to `{worktree}/tmp/retro_N.txt`, then create it with `gh issue create --body-file`.
- Label with `type/dx` (workflow improvements) or the appropriate area label (preventative measures).
- Set priority based on impact: `priority/p1` for things that would prevent production bugs or save significant agent time across many tasks; `priority/p2` for nice-to-have workflow improvements or one-off friction. **Never set `priority/p0`** — that priority is reserved for humans.
- Add `agent/ready` so the issue can be picked up autonomously.
- Keep issue scope tight — one improvement per issue. An agent should be able to pick it up and complete it in a single session.

If the task was trivial and there are genuinely no improvements to make, that's fine — skip filing. But default to filing. The bar is "would this save time or prevent bugs across future tasks?"

### 5d — Remove worktree

Write status: `phase: done`, `summary: Task complete. Removing worktree.`

**Only remove the worktree after deployment verification passes** (or after confirming the change has no deployed component). Never clean up immediately after merge — the worktree is needed for debugging if verification fails.

```
scripts/end-worker.sh {worktree}
```

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Multi-line Python always goes in a `.py` file**, never `-c '...'`.
- See CLAUDE.md §Unattended Operation Patterns for the full list.
