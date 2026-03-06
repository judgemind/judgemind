---
description: Pick up and complete a Judgemind GitHub issue autonomously — from worktree setup through PR and review request. Usage: /task (next ready issue), /task #42 (specific issue), /task scrapers (natural-language filter).
argument-hint: "[#issue | category | next]"
---

# /task skill

Pick up one issue from the Judgemind backlog and complete it autonomously. Do not ask for confirmation at any point — work through every step and stop only when the PR is green and review has been requested (or when an investigation task has posted its findings and unblocked any dependents).

---

## Step 0 — Ensure worktree exists

Check whether you are already working inside a worktree (i.e. `{worktree}` is set and the directory exists). If not, run the setup script:

```
scripts/start-worker.sh
```

This resolves the repo root, prunes stale worktrees, claims the lowest available worker number, creates the worktree, configures git hooks, and creates the `tmp/` directory.

**Record the printed worktree path** — it is `{worktree}` for the rest of the session. All subsequent work happens inside `{worktree}`.

If you are already in a worktree, skip this step.

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
Priority order: `priority/critical` > `priority/high` > `priority/medium` > `priority/low`. Within the same priority, prefer lower issue numbers (older issues first). Skip issues already assigned to another agent unless their worktree no longer exists in `git -C $REPO_ROOT worktree list`.

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
8. "Remove worktree" — cleanup

**For investigation tasks (Path B):**
1. "Investigate and document findings"
2. "File follow-up issues"
3. "Post summary and request review"
4. "Unblock dependent issues"
5. "Remove worktree"

Mark each todo `in_progress` when you start it and `completed` when done. If a task has fewer than 3 steps total (e.g. a trivial fix), skip todo creation.

---

## Step 4 — Determine the response type and execute

Read the issue body thoroughly, including linked issues. Check `docs/specs/` for relevant guidance. Look at existing code for patterns — be consistent with what's already there.

If the issue requires a maintainer decision before you can proceed: comment on it, add `status/blocked`, and stop. Do not guess on ambiguous requirements.

---

### Path A: Implementation task (feature, bug fix, refactor)

Follow the full PR Workflow defined in CLAUDE.md. **All commits must be on the worktree branch — never on `main`.** Summary of required substeps:

#### A.1 — Set up dependencies
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

#### A.3 — Stage, commit, and push
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

Immediately open a PR after the first push — never push without creating one. The PR body must include `Closes #N` so the unblock workflow fires on merge:
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
```
gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact
```
If CI fails: diagnose, fix locally, push, return to A.4. Repeat until all checks pass.

#### A.6 — Update the PR test plan
Fetch the current PR body, check off automated steps that passed in CI, run any manual smoke tests in a temporary smoketest worktree (see CLAUDE.md §4.8 for the pattern), write the updated body to `{worktree}/tmp/pr_body.txt`, then:
```
gh pr edit <PR-N> --repo judgemind/judgemind --body-file {worktree}/tmp/pr_body.txt
```

#### A.7 — Merge the PR
The PR has passed the ralph loop review (A.2) and CI is green. Merge it:
```
gh pr merge <PR-N> --repo judgemind/judgemind --squash --delete-branch
```

**Dependent issues will be unblocked automatically** by the `unblock-issues` workflow when the PR merges. No manual unblocking needed.

#### A.8 — Remove worktree
```
scripts/end-worker.sh {worktree}
```

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

Clean up: `scripts/end-worker.sh {worktree}`

---

### Path C: Large or ambiguous task

Break into sub-tasks first (see CLAUDE.md §Creating Sub-Tasks), label them `agent/ready`, then pick up the first sub-task (restart from Step 1).

If you only create sub-tasks and do not pick one up in this session, clean up: `scripts/end-worker.sh {worktree}`

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Multi-line Python always goes in a `.py` file**, never `-c '...'`.
- See CLAUDE.md §Unattended Operation Patterns for the full list.
