---
description: Pick up and complete a Judgemind GitHub issue autonomously — from selection through PR and review request. Usage: /task (next ready issue), /task #42 (specific issue), /task scrapers (natural-language filter).
argument-hint: "[#issue | category | next]"
---

# /task skill

Pick up one issue from the Judgemind backlog and complete it autonomously. Do not ask for confirmation at any point — work through every step and stop only when the PR is green and review has been requested (or when an investigation task has posted its findings and unblocked any dependents).

**Prerequisite:** Steps 0–2 from CLAUDE.md (resolve repo root, claim worker number, create worktree) must already be complete. If they haven't happened yet, do them now before proceeding.

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

## Step 3 — Determine the response type and execute

Read the issue body thoroughly, including linked issues. Check `docs/specs/` for relevant guidance. Look at existing code for patterns — be consistent with what's already there.

If the issue requires a maintainer decision before you can proceed: comment on it, add `status/blocked`, and stop. Do not guess on ambiguous requirements.

---

### Path A: Implementation task (feature, bug fix, refactor)

Follow the full PR Workflow defined in CLAUDE.md. **All commits must be on the worktree branch — never on `main`.** Summary of required substeps:

#### A.1 — Implement and verify locally
- Implement the change on the worktree branch.
- Run ALL pre-PR checks for every package touched (see CLAUDE.md §Pre-PR Checks). Fix any failures before proceeding. Do not push code that fails local checks.

#### A.2 — Commit and push
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

#### A.3 — Verify no merge conflicts
```
gh pr view <PR-N> --repo judgemind/judgemind --json mergeable,mergeStateStatus
```
If `mergeable` is `CONFLICTING`, rebase and resolve:
```
git -C {worktree} fetch origin main
git -C {worktree} rebase origin/main
```
Resolve conflicts, `git rebase --continue`, then push with `--force-with-lease`.

#### A.4 — Monitor CI and iterate until green
```
gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact
```
If CI fails: diagnose, fix locally, push, return to A.3. Repeat until all checks pass.

#### A.5 — Update the PR test plan
Fetch the current PR body, check off automated steps that passed in CI, run any manual smoke tests in a temporary smoketest worktree (see CLAUDE.md §4.7 for the pattern), write the updated body to `{worktree}/tmp/pr_body.txt`, then:
```
gh pr edit <PR-N> --repo judgemind/judgemind --body-file {worktree}/tmp/pr_body.txt
```

#### A.6 — Link the issue and request review
Comment on the issue linking the PR. Add `status/review` label to the issue.

**Dependent issues will be unblocked automatically** by the `unblock-issues` workflow when this PR is merged. No manual unblocking needed.

---

### Path B: Investigation task

Write findings to `docs/investigations/<slug>-<YYYY-MM>.md` and/or into the issue body. End with:
- Recommended next steps
- Sub-tasks to create (see CLAUDE.md §Creating Sub-Tasks)
- Decisions that need human input

Create any sub-tasks, label them `agent/ready`. Post a summary comment on the issue, add `status/review`.

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

---

### Path C: Large or ambiguous task

Break into sub-tasks first (see CLAUDE.md §Creating Sub-Tasks), label them `agent/ready`, then invoke `/task` again to pick up the first sub-task.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Multi-line Python always goes in a `.py` file**, never `-c '...'`.
- See CLAUDE.md §Unattended Operation Patterns for the full list.
