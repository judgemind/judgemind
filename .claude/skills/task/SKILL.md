---
description: Pick up and complete a Judgemind GitHub issue autonomously — from issue claim through PR and review request. Usage: /task (next ready issue), /task #42 (specific issue), /task scrapers (natural-language filter).
argument-hint: "[#issue | category | next]"
maxTurns: 200
---

# /task skill

Pick up one issue from the Judgemind backlog and complete it autonomously. Do not ask for confirmation at any point — work through every step and stop only when the PR is green and review has been requested (or when an investigation task has posted its findings, closed the issue, and unblocked any dependents).

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation anywhere in a `/task` agent. All work runs synchronously in the foreground. The `/task` agent is already a background subagent from the dispatcher's perspective — further backgrounding causes completion notifications to surface in the wrong context (the dispatcher), leading to confusion and lost results.

**IMPORTANT — Post-compaction recovery.** If your context was just autocompacted (your summary references "previous conversation"), you are NOT done with the task. Autocompaction preserves *what was done* but not the procedural imperative *what still needs to happen next* (see #2545). Before emitting any final report or `end_turn`, run the status-file-driven recovery check described in §A.0 (implementation tasks) or §B.0 (investigation tasks). The status file at `{worktree}/tmp/agent-status.txt` is your authoritative "where am I" anchor — re-read this SKILL.md from the section named by your current `phase` and continue. Only `phase=done`, `phase=verified`, or `phase=blocked` means stop.

**IMPORTANT — MCP-first for GitHub reads.** Prefer `mcp__github__*` tools for reads (issue/PR lookup, status, files, comments, search). Keep `gh` for writes (comment, edit, create, merge, close) — the MCP server currently has no auth token so all writes fail. Keep `gh` permanently for `gh run watch`, `gh run list --workflow`, `gh pr edit --body-file`, and anything else without an MCP equivalent. Decision table: `docs/agent/github-api-access.md`. Full inventory: `docs/agent/gh-to-mcp-migration.md`.

Loading a deferred MCP tool requires a one-time `ToolSearch` call to pull its schema — e.g. `ToolSearch query="select:mcp__github__get_issue,mcp__github__list_issues,mcp__github__get_pull_request,mcp__github__get_pull_request_status"`. Once loaded, the tool is callable for the rest of the session.

---

## Step 0 — Verify worktree exists

When spawned by the dispatcher with `isolation: "worktree"`, Claude Code automatically creates a unique worktree at `.claude/worktrees/agent-<id>/`. The agent is already inside this worktree when it starts.

**Verify you are in a worktree** by checking that your working directory contains `.claude/worktrees/` in the path. If not, stop and report the error — all task work requires a worktree created by the dispatcher's `isolation: "worktree"` mode.

**Record the working directory** — it is `{worktree}` for the rest of the session. All subsequent work happens inside `{worktree}`.

Create `{worktree}/tmp/` if it does not already exist:

```
mkdir -p {worktree}/tmp
```

### Status file setup

After confirming the worktree, set up the agent status file so the dispatcher can monitor progress **and so you can recover from autocompact**.

**Determining the agent-id (`AGENT_ID` env var precedence):**

1. **If the `AGENT_ID` environment variable is set, use that value verbatim** as `{agent-id}`. The dispatcher-v3 task-runner ECS task launches `claude -p --worktree=agent-<uuid> "/task #N"` with `AGENT_ID=<uuid>` exported in the env. Using the launcher-assigned id ensures `/task`'s claim comment and every `progress.sh` milestone call correlate with the dispatcher's DB rows for the same agent. See the dispatcher-v3 spec (§11 OQ#5 and §4.3) for the full launcher contract — and #3873 for the issue that landed this env-var precedence.
2. **Otherwise, fall back to the cwd-derived id** (e.g. `agent-ab4722a2` from `.claude/worktrees/agent-ab4722a2`, or `worker-2` from `worktrees/worker-2`). This is today's behavior — Agent-tool spawn via the dispatcher-v2 daemon does not export `AGENT_ID`, so the cwd-derived path remains the fallback.

Quick check (env var first, cwd fallback): `echo "${AGENT_ID:-$(basename "$PWD")}"`.

The chosen value is `{agent-id}` for the rest of this skill — it appears in the claim comment in Step 2 and at every milestone call to `scripts/dispatcher/progress.sh` (see "Milestone progress reporting" below).

### Milestone progress reporting (dispatcher-v3 cockpit)

`scripts/dispatcher/progress.sh "$AGENT_ID" <milestone> [optional detail]` is a best-effort cockpit beacon that does ONE UPDATE on the `dispatcher.agents` row keyed by `agent_id` and exits 0 unconditionally — missing args, missing `DATABASE_URL`, missing `psql`, DB connect failure, and successful UPDATE all return 0. The helper exists so the cockpit can show "where is this agent right now" without reintroducing a phase state machine. See `docs/specs/dispatcher-v3-spec.md` §4.3 and #3973.

Call this helper at every natural milestone in the /task workflow so the cockpit's `dispatcher.agents.current_milestone` column advances through the run instead of staying NULL. The recipe used at every site below is:

```
scripts/dispatcher/progress.sh "$AGENT_ID" <milestone> [detail] || true
```

The trailing `|| true` is defense-in-depth — `progress.sh` already returns 0 on every error path, but explicit `|| true` makes the intent visible to anyone reading the SKILL and survives copy-paste into contexts where `set -e` is in force. The whole call is silent on success and prints a one-line "swallowed" note to stderr on failure; it never blocks /task.

Cohabitation note: dispatcher-v2 daemon-managed agents do not call this helper, so their `current_milestone*` columns stay NULL — exactly the expected v2 behavior per migration 56's column comments. v3 agents call it, the cockpit reads it, nothing else does.

The terminal phases (`done`, `verified`, `blocked`) and the initial `claiming` row are written by the dispatcher-v3 launcher's `_mark_agent_terminal` / row-INSERT paths — NOT by /task. /task only writes the in-between milestones: `planning`, `ralph`, `summary`, `push_and_pr`, `awaiting_ci`, `fix_ci`, `merge`, `awaiting_deploy`, `verify`, `retro`. See the per-step recipes in Path A / Step 5 below.

The status file lives at `{worktree}/tmp/agent-status.txt`. The `{worktree}/tmp/` directory was already created above — no additional `mkdir` needed.

The status file format is:

```
issue: #<N>
phase: <phase>
updated: <ISO-8601 timestamp>
summary: <one-line description of current activity>
autocompact_count: <integer, optional — increment on each post-compaction recovery>
final_phase: <phase, optional — written only when the task ends>
```

Phases (in typical order): `claiming`, `setup`, `ralph-worker (iteration N)`, `ralph-reviewer (iteration N)`, `pushing`, `ci-watch`, `ci-fix`, `merging`, `deploying`, `verifying`, `retrospective`, `done`, `blocked`.

**Terminal phases** (the task is actually finished): `done`, `verified`, `blocked`. Any other phase means work remains — you MUST continue.

**Write a status update at every major step transition** — use the Write tool to overwrite the status file. The first update should be written immediately after worktree setup with phase `claiming`. The status file is your post-compaction recovery anchor — see §A.0 and §B.0 below.

### Phase timing instrumentation

At every phase transition, also record the timing via `scripts/phase_timer.py`. This tracks wall-clock duration for each phase so we can identify bottlenecks and answer questions like "how long does CI take?" or "what's the overhead of worktree setup vs actual implementation?"

**At each phase start** (alongside the status file write), run:
```
python3 {worktree}/scripts/phase_timer.py start {worktree} <phase>
```

The timer automatically closes the previous phase when a new one starts, so you only need `start` calls at each transition — no explicit `end` calls are needed during normal flow.

**For ralph-reviewer phases**, after all three reviewers complete, end the phase with per-reviewer timing detail (see the ralph SKILL.md for how to capture per-reviewer seconds):
```
python3 {worktree}/scripts/phase_timer.py end {worktree} --detail '{"gemini_standard": <secs>, "gemini_adversarial": <secs>, "claude": <secs>}'
```

**At task completion** (in Step 5d, before cleanup), generate the timing summary:
```
python3 {worktree}/scripts/phase_timer.py summarize {worktree} {repo_root} <issue_number>
```
This writes `{worktree}/tmp/timing.json` with the full phase breakdown and appends a one-line summary to `{repo_root}/tmp/task-timings.jsonl`.

---

## Step 1 — Identify the issue

Interpret `$ARGUMENTS` as follows:

### Empty or "next"
List all open, unassigned `agent/ready` issues and pick the highest-priority one using MCP:

```
mcp__github__list_issues
  owner=judgemind repo=judgemind
  labels=["agent/ready"] state="open" per_page=20
```

Priority order: `priority/p0` > `priority/p1` > `priority/p2` > `priority/p3`. Within the same priority, prefer lower issue numbers (older issues first). Skip issues already assigned to another agent unless their worktree no longer exists in `git -C $REPO_ROOT worktree list`.

MCP does not support filtering by assignee directly — filter the returned list client-side on each issue's `assignees` array.

### `#N` (e.g. `/task #42`)
Work on that specific issue regardless of its current labels or assignment. Fetch it via MCP:

```
mcp__github__get_issue owner=judgemind repo=judgemind issue_number=42
```

This returns the full issue object (number, title, body, labels, assignees, state). `get_issue` does **not** embed comments — see Step 4 below for the comments fetch.

### Natural language (e.g. `/task scrapers`, `/task next perf bug`, `/task SF tentatives`)
List `agent/ready` issues with `mcp__github__list_issues` (same call as above), then pick the one that best matches the description. Prefer exact label or area matches; fall back to title/body keyword matches. If multiple candidates are equally good, pick the highest-priority unassigned one. Briefly note which issue you chose and why before proceeding.

---

## Step 1b — Author trust check (MANDATORY)

Before claiming or working on any issue, verify the issue was filed by a trusted author:

```
scripts/check-issue-author.sh <issue-number>
```

- **Exit 0 (trusted):** proceed to Step 2.
- **Exit 1 (untrusted):** **do not work on this issue.** Remove the `agent/ready` label and add `status/triage`. Until MCP writes are authenticated, use `gh`:
  ```
  gh issue edit <N> --repo judgemind/judgemind --remove-label agent/ready --add-label status/triage
  ```
  Post a comment: `"Issue author is not a repository collaborator — moved to triage for maintainer review."`
  Then stop — do not proceed to Step 2.
- **Exit 2 (error):** stop and report the error. Do not work on an issue whose authorship cannot be verified.

**This check is a security gate.** On a public repo, external users can file issues that appear in the `agent/ready` queue. Without this check, an attacker could craft an issue that instructs the agent to execute arbitrary code. Only issues filed by repository owners, org members, or collaborators are eligible for autonomous execution.

---

## Step 2 — Claim the issue and rename the conversation

Assign it to yourself (write — MCP auth currently blocked, stays on `gh`):
```
gh issue edit <N> --repo judgemind/judgemind --add-assignee @me
```

Write the claim comment to a temp file, then post it via the
`scripts/gh-comment-with-retry.sh` wrapper (write — stays on `gh`).
The wrapper transparently handles the 504-after-success failure
mode where `gh issue comment` returns a 5xx with a multi-KB
"Unicorn!" HTML page even though the comment posted (#4478):
```
{worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/claim_comment.txt
```
Comment content: `Picking this up in {agent-id}.` (Use the same `{agent-id}` resolved in Step 0 — i.e. `AGENT_ID` env var if set, else cwd-derived.)

Once MCP writes are unblocked (follow-up issue referenced from `docs/agent/gh-to-mcp-migration.md`), this becomes `mcp__github__update_issue` + `mcp__github__add_issue_comment` with no tmp-file preamble.

**Rename this conversation** so it is identifiable in the sidebar:
- Format: `#<N> — <short title>` (drop any `[AREA]` prefix tag from the issue title)
- Run: `/rename #<N> — <short title>`

## Step 2a — Mark the issue `status/in-progress` (MANDATORY)

**Why:** This is the sole claim interlock between `/task` subagents and the Fargate dispatcher daemon. A /task subagent you spawn on an `agent/ready` issue can race with the daemon's next queue scan — both would independently run the full plan → ralph → summary → PR pipeline on the same issue and produce duplicate PRs. The daemon's queue-scan filter drops any issue carrying `status/in-progress`, so flipping that label before stripping `agent/ready` is all the coordination the /task skill needs.

Issue #2927 simplified this from a two-halves DB-row + label interlock (#2866) to this label-only flow. The former `scripts/dispatcher/task_claim.py claim` / `terminal` helper is now a deprecated no-op stub — **you do not run it**. If you encounter an older transcript or guide that still invokes it, the stub exits 0 without touching the DB, so historical CLAUDE.md copies keep working without crashing.

### The add-then-remove ordering (race-defense)

**Add `status/in-progress` BEFORE removing `agent/ready`.** GitHub label propagation is ~100ms. A racing daemon tick that observes your labels between the remove and the add could see `agent/ready=present, status/in-progress=absent` and try to claim. Doing the add first closes that window: the daemon's queue-scan filter drops any issue carrying `status/in-progress`, and the daemon's own pre-claim label recheck inside `_atomic_claim` re-reads the label set at claim time so a race in the other direction is caught atomically too.

The tiny residual race — both the operator's add and the daemon's scan complete within the same ~100ms of each other — is accepted (issue #2927). Worst case: one operator PR + one daemon PR on the same issue, a human notices and closes one.

Run the two edits as separate tool calls, in this order:

```
gh issue edit <N> --repo judgemind/judgemind --add-label status/in-progress
gh issue edit <N> --repo judgemind/judgemind --remove-label agent/ready
```

Both calls are idempotent (`gh` ignores a label that is already present / already absent). The `agent/ready` remove is optional in the narrow case where the operator manually spawned you on an issue that never carried the label (e.g. a rerun of a previous /task session on an issue already closed + reopened) — but running it is harmless.

Exit codes:

- **`0`** on each edit — continue to Step 3.
- Any non-zero — a GitHub API failure. Retry once; if it fails again, stop and report the error. The operator can re-issue the command manually or wait for rate limits to recover.

### MANDATORY teardown on terminal

On any terminal state (PR merged, verification passed, blocker, investigation closed, STUCK exit), remove the label:

```
gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress
```

No DB write. The label remove is idempotent — `gh` exits 0 if the label is already absent (for example, the daemon already swept it off during its own parallel teardown). The teardown appears in the Path A/B per-step recipes below (A.7, B.2, and the STUCK-exit path inside A.2). Do not skip it — leaving the label in place keeps the issue hidden from the daemon's queue scan forever.

---
---

## Step 3 — Create todo list for progress tracking

After claiming the issue, create todos using `TaskCreate` to track your major workflow steps. This makes progress visible and prevents skipping steps.

**For implementation tasks (Path A):**
1. "Set up dependencies" — venvs/node_modules for affected packages
2. "Implement and review (ralph loop)" — the core implementation phase
3. "Post process summary" — map implementation to acceptance criteria
4. "Commit and push" — stage, commit, create PR
5. "Watch CI and fix failures" — monitor CI, resolve any failures
6. "Verify no merge conflicts" — check mergeable status
7. "Update PR test plan" — check off test plan items
8. "Merge PR" — squash merge after CI is green
9. "Verify deployment and post evidence" — watch deploy, verify feature works, post evidence comment
10. "Retrospective" — identify workflow efficiencies and preventative measures

**For investigation tasks (Path B):**
1. "Investigate and document findings"
2. "Update contradicted source-file docstrings"
3. "File follow-up issues"
4. "Post summary and close issue"
5. "Unblock dependent issues"
6. "Retrospective" — identify workflow efficiencies and preventative measures

Mark each todo `in_progress` when you start it and `completed` when done. If a task has fewer than 3 steps total (e.g. a trivial fix), skip todo creation.

---

## Step 4 — Determine the response type and execute

Read the issue body thoroughly, including linked issues. Check `docs/specs/` for relevant guidance. Look at existing code for patterns — be consistent with what's already there.

**Fetch issue comments for full context.** Issue comments often contain scope clarifications, additional acceptance criteria, and implementation notes from prior attempts. `mcp__github__get_issue` does not embed comments, so fetch them explicitly via `gh` (MCP has `add_issue_comment` for posting but no first-class "list comments on an issue" tool):

```
gh issue view <N> --repo judgemind/judgemind --json number,title,body,labels,assignees,comments
```

Include non-bot comments (filter out comments from `github-actions[bot]`, `judgemind-agent`, etc.) in the context passed to the worker's `task.md`. Append them under a `## Issue Comments` heading with the author and date for each comment.

**Scope completeness check:** Before implementing, search the codebase for all locations affected by the change. If the issue mentions fixing or changing X in one file, grep for X across the entire codebase. List all locations that use, render, or implement the same pattern. If the issue's scope doesn't cover all of them, either expand scope to include them or file follow-up issues for the missed locations so they are tracked. Document the scope check results (what you searched for, what you found) in your implementation notes or the PR body. For backfill migrations specifically (PRs touching `packages/api/migrations/*.sql` with an UPDATE/DELETE/INSERT against existing rows), also follow the row-class coverage checklist in `docs/agent/issue-authoring.md` §Backfill Migrations.

If the issue requires a maintainer decision before you can proceed: comment on it, block it with `scripts/block-issue.sh <issue> <blocker>` (if a specific blocking issue exists) or just add `status/blocked` manually, and stop. Do not guess on ambiguous requirements.

### Step 4a — Duplicate-PR check (adoption pivot, MANDATORY before dependency install / ralph)

**Why this runs before A.1 / Path-B setup:** If a prior agent already shipped (or partially shipped) this issue as an open PR, running ralph from scratch wastes the full implement + review compute cycle. The check is cheap (one `gh pr list` call) and lets the agent pivot immediately — either *adopt* the existing PR and drive it to merge, or bail with a comment. A.3 keeps a second invocation as defense-in-depth against concurrent-agent races that opened a PR between this check and our own push (see #3098).

Run the check as a single tool call:

```
{worktree}/scripts/check-duplicate-pr.sh <N>
```

Exit codes (pass-through from `preflight_no_duplicate_pr`; see `scripts/preflight.sh`):

- **Exit 1 (`ok:` line on stdout) — no duplicate.** Continue to the path branches below (Path A → A.1, Path B → B.1). This is the common case.
- **Exit 0 (`duplicate:` line on stdout with PR number) — an open PR already addresses this issue.** Do NOT proceed to A.1 / dependency install / ralph. Pivot to the adoption decision below.
- **Exit 2 (`error:` line on stderr) — check failed (gh unavailable, API error, etc.).** Fail-open: continue to the path branches, and let A.3's second check catch any duplicate we missed.

#### 4a.1 — Adoption decision (only runs on exit 0)

Fetch the existing PR's state via MCP:

```
mcp__github__get_pull_request owner=judgemind repo=judgemind pull_number=<existing-PR>
mcp__github__get_pull_request_status owner=judgemind repo=judgemind pull_number=<existing-PR>
```

Evaluate:

- **`state: OPEN`, `mergeable: MERGEABLE` / `mergeable_state: clean`, `statusCheckRollup` all SUCCESS/SKIPPED, and an AC-mapping process-summary comment already exists on the issue:** the prior agent got us to the merge line and stopped. **Adopt-to-merge.** Skip A.1 (no code changes needed), skip A.2 (ralph), skip A.2b (summary already posted). Jump to A.7 (`gh pr merge --squash --delete-branch`), then A.8 (deploy verification + evidence), then A.9 (retro). Release the `status/in-progress` label as usual.
- **`state: OPEN`, CI red or `mergeable_state: dirty`/`unstable`, or the PR body shows an obviously incomplete implementation (missing ACs, unchecked Automated-checks boxes, reviewer REVISE):** the prior agent stalled mid-flight. **Adopt-to-iterate.** Check out the existing PR's branch into this worktree, continue from the step that makes sense (A.4 for merge conflicts, A.5 for CI failures, or A.2 to finish ralph on top of the existing diff), and drive it through to merge. To re-bind the worktree to the existing branch:
  ```
  git -C {worktree} fetch origin pull/<existing-PR>/head:adopt-<existing-PR>
  git -C {worktree} checkout adopt-<existing-PR>
  git -C {worktree} branch --set-upstream-to=origin/<existing-PR-branch>
  ```
  (Use the PR's `headRefName` from `mcp__github__get_pull_request` for `<existing-PR-branch>`.) If the existing branch name conflicts with the auto-created worktree branch, a throwaway `adopt-<N>` tracking branch is fine — the push target is the original PR's head ref, not this worktree's branch name.
- **`state: CLOSED` (merged or declined):** MCP returned a stale hit for a PR that has since closed. Treat as no duplicate and continue to Path A / Path B normally.
- **Ambiguous case** (e.g. the existing PR addresses a *different* sub-scope of the issue and the current task is a legitimate follow-up, or the existing PR is stalled with no clear path forward): post a comment on the issue explaining the adoption decision and exit terminal. Write the comment to `{worktree}/tmp/adoption_bail_comment.txt`, then post via `scripts/gh-comment-with-retry.sh` (the wrapper transparently handles the 504-after-success failure mode #4478):
  ```
  {worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/adoption_bail_comment.txt
  gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress
  ```
  The comment should name the existing PR, explain why the current agent can't adopt it (stalled, scope mismatch, author disagreement), and either re-add `agent/ready` or leave the label off for human review. Then stop — worktree cleanup is automatic.

The adoption-to-merge path should be the common case when this check fires — if a prior agent got far enough to open a PR, they almost always got far enough to need someone to push the merge button.

#### 4a.2 — Already-shipped check (zombie-issue pivot, MANDATORY when 4a returned exit 1)

**Why this runs after 4a:** `check-duplicate-pr.sh` only finds *open* PRs. It cannot catch the case where the issue's code already shipped via a *closed and merged* PR that didn't auto-close the issue — typically because the PR carried a placeholder title (`WIP: ralph output`) or a null body with no `Closes #N` keyword, so GitHub's auto-close never fired and the issue stayed `agent/ready` indefinitely. Issue #2831 ↔ PR #3229 is the canonical example: PR shipped 2026-04-24, issue stayed `agent/ready` for 12 days until a human noticed and closed it manually. CLAUDE.md and `pr-title-check.yml` (#3994) now ban placeholder titles, so the bug class stops accruing — but the back-catalog of pre-#3994 issues can keep re-entering the queue. This check finds them in one cheap `gh issue view` + a few `gh api commits` calls per file path in the issue body, and pivots /task to a "verify and close" path.

Run the check as a single tool call (only after `check-duplicate-pr.sh` returned exit 1):

```
{worktree}/scripts/check-shipped-pr.sh <N>
```

Exit codes:

- **Exit 1 (`not-shipped:` line on stdout) — no high-confidence shipped match.** Continue to Step 4b. This is the common case.
- **Exit 0 (`shipped:` line on stdout + JSON summary) — a closed PR merged onto `main` already shipped this issue's work.** Do NOT proceed to Step 4b / A.1 / ralph. Pivot to the verify-and-close decision below.
- **Exit 2 (`error:` line on stderr) — check failed (gh unavailable, API error, etc.).** Fail-open: continue to Step 4b.

The high-confidence threshold the script applies is **≥1 added overlap OR ≥2 total overlap on *target-context* candidate paths** (with the PR's `mergedAt` non-null and `baseRefName == main`). The extractor classifies issue-body file paths into two contexts:

- **target-context** — paths in narrative prose / Proposal / AC text. These are the load-bearing locations the issue intends to change.
- **search-context** — paths cited only inside `Verify:` lines, `grep` / `pytest` / `aws` / `curl` / `rg` invocations, or fenced shell-output blocks. These are search arguments, not change targets.

Search-context overlaps never count toward the threshold by themselves (#4340 — fixes the false-positive where issue Verify lines list files purely as grep arguments). A single *modified*-file overlap is intentionally below the threshold — that case routinely fires on adjacent scripts the issue cites as references rather than load-bearing targets. The "added" classification uses the authoritative `changeType: "ADDED"` signal from `gh pr view --json files` (or `status: "added"` from the raw GitHub REST API), falling back to a `deletions == 0 && additions > 0` heuristic only when neither authoritative signal is present.

When the issue's title or body classifies as **audit-class** — any of `audit`, `investigate` / `investigation`, `refactor` / `refactoring`, `migrate` / `migration`, `extend` / `extension`, `tighten`, `harden`, `additional` (word-boundary-anchored, case-insensitive) — the threshold tightens to **≥2 target-context overlaps AND ≥1 target-context overlap is ADDED** (#4223, refined #4501). Audit / investigation / refactor issues are by intent asking for *more* work on existing files, so a single added or any all-modified overlap is too weak a signal — the canonical FP is "prior unrelated PR touched the file the audit cites." The original #4223 rule required EVERY overlap to be ADDED; #4501 refined that to "≥1 ADDED" because audit issues that prescribe BOTH a modification to an existing file AND creation of a new file (the #3310 ↔ #3319 shape) were being over-penalized — when the candidate PR demonstrably created at least one of the target files the issue prescribed, that's a strong creation-style signal even when other overlaps are modifications. Date-ordering (#4353) closes the easy case; this tightening closes the residual where the unrelated PR merged AFTER the issue was filed. The classifier helper lives at `scripts/_check_shipped_pr_classify_issue.py` and is called by the bash wrapper before invoking the overlap helper.

##### 4a.2.1 — Verify-and-close pivot (only runs on exit 0)

Read the JSON summary from stdout (the `shipped_pr` field names the merged PR; `overlap_files` and `added_files` show what landed). Then:

1. **Run any acceptance-criteria `Verify:` commands that look mechanical.** Walk the issue body, extract each `Verify:` line, and run only those that are concrete commands (e.g. `Verify: scripts/foo.sh exits 0`, `Verify: pytest -k test_x`, `Verify: grep -n <pattern> <file>`). Skip free-form prose verifies ("Verify: manual sanity — flip a hex...") — those need a human.
2. **All-green path:** if every mechanical `Verify:` command exits clean, the issue is done. Post a verification-evidence comment quoting the verify commands + their output + naming the shipped PR via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478), close the issue with `--reason completed`, run `scripts/unblock-dependents.sh <N>`, and remove `status/in-progress`. Skip Path A entirely — there is no PR to file.

   ```
   {worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/verification_evidence.txt
   gh issue close <N> --repo judgemind/judgemind --reason completed
   {worktree}/scripts/unblock-dependents.sh <N>
   gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress
   ```

   This is exactly the manual flow that closed #2831 today — turning that flow into a one-shot pivot is the entire point of this check.
3. **Any-failure path:** if any mechanical `Verify:` command fails (or the issue has no mechanical verifies and a human-judgment residual remains), post a comment naming the partially-shipped PR + the failed verify line, leave the issue open with `agent/ready` re-attached, and **fall through to Step 4b.** Normal /task flow continues from there. The comment should make explicit that the agent ran `check-shipped-pr.sh`, found PR #N, but at least one AC is still unmet — so a human or a follow-up agent can decide whether to file a follow-up issue or close.
4. **Reduced docs-only PR is NOT the default here.** The verify-and-close pivot is for "the AC's intent has already been satisfied by the shipped PR; we just need to close the loop." If a docs delta is genuinely missing (e.g. the AC said "wire this into SKILL.md and ship a Verify command" and only the script shipped), treat it as the any-failure path: post a comment, leave the issue open, fall through.

The verify-and-close path should be the common case when this check fires — pre-#3994 placeholder-title PRs almost always landed everything the AC asked for, the only thing missing is the issue closure.

---

### Step 4b — Verify gap is real (current state probe)

**Trigger condition:** Run this step when any of these are true — (a) the issue title or body contains a gap-assertion verb ("enable", "add", "introduce", "expose", "document"); (b) the issue carries a `type/dx` or `type/infra` label; (c) the issue's acceptance criteria contain an explicit literal command on a `Verify:` line — `Verify: pytest ...`, `Verify: ./scripts/...`, `Verify: grep ...`, `Verify: npm ...`, etc. Issues with no gap-assertion signal AND no literal `Verify:` command (e.g. pure bug fixes against active production failures, investigation tasks, refactoring) skip this step entirely.

**Default rule — run the AC's `Verify:` command first.** If the issue's acceptance criteria provide a literal `Verify:` command, run that command **verbatim** as the §4b probe before falling back to a verb-keyed probe below. The AC author already wrote the cheapest possible state check — running it first short-circuits the "what to grep for" decision and produces probe output that is *also* the canonical evidence to cite in the issue comment if the gap is already satisfied (see "Decision tree" below). This applies whether the AC's `Verify:` line is `pytest -k <test_name>`, `grep -n <pattern> <file>`, `./scripts/<probe>.sh`, an `aws` describe call, or any other concrete command.

**What to do:** Pick the probe pattern that matches the issue's verb (see `docs/agent/issue-authoring.md §Verify the gap exists before filing` for the full probe catalog) and run it as a single tool call:

- *"Enable AWS setting Y"* → `aws ecs describe-clusters --include SETTINGS` or grep `infra/terraform/` for the Terraform attribute.
- *"Add metric / alarm"* → `aws cloudwatch list-metrics --namespace <ns>` / `aws cloudwatch describe-alarms --alarm-name-prefix <prefix>`.
- *"Add / document X"* → `grep -r "X" docs/ .claude/skills/ CLAUDE.md` or `mcp__github__search_code` for the concept.
- *"Fix code bug / introduce helper"* → grep for the function name or error-message string across `packages/`.
- *"fix(test) — failing test reproduction"* — when the issue title starts with `fix(test)` (or carries `type/dx` AND has a `Verify: ./scripts/...` / `Verify: pytest ...` / `Verify: npm ...` line in the body), run that verify line **verbatim against the worktree's freshly-rebased base** (Step A.1a already aligned the worktree to `origin/main`). If it returns clean (no FAIL output, exit 0, the failing assertion the issue cites no longer fires), pivot to the "Gap already satisfied" branch and post a comment naming the PR that landed the fix — `git log --oneline --all -- <test-or-source-path>` is the fastest way to identify the load-bearing commit. This pattern came out of the #4178 / #4173 retro: #4178 was filed on a stale baseline 7.5 hours after #4173 had merged the macOS jq-1.6 empty-file salvage-prelude fix, and the agent saved the entire ralph cycle by running the verify line as one of its first tool calls.
- *"test(...) — test-creation issue with `pytest -k <test_name>` Verify clause"* — when the issue title starts with `test(` (e.g. `test(dispatcher): ...`) and the AC has a `Verify: pytest ... -k <test_name>` line, run that pytest command **verbatim** as the probe. If it passes (exit 0, the named test exists and is green on `origin/main`), pivot to "Gap already satisfied" — the test was almost certainly added by an earlier PR that didn't include `Closes #N` (typical placeholder-titled PR like `WIP: ralph output` with empty body), so the issue stayed `agent/ready` even though the work shipped. Use `git log --oneline --all -S '<test_name>' -- <test-file-glob>` to identify the load-bearing commit and the PR that landed it. The post-comment + close-with-`--reason completed` path is the same as the `fix(test)` row above, since the AC's intent is exhausted by "the test passes." This pattern came out of the #2889 / #3253 retro: issue #2889 stayed `agent/ready` for 10 days because PR #3253 (titled `WIP: ralph output`, empty body, no `Closes #2889` keyword) added `test_phase_constants_cover_all_declared_phases` without firing the auto-close — running the AC's `pytest -k test_phase_constants_cover_all_declared_phases` line as one of the agent's first tool calls would have closed the loop in seconds. Issue #3994's CI guard now rejects placeholder titles at the source; this row closes the back-catalog of stale issues already filed against pre-#3994 PRs. Running pytest is also more reliable than grepping the test file for the test name verbatim — a renamed-but-equivalent test (e.g. `test_phase_constants_consistency` vs the AC's `test_phase_constants_cover_all_declared_phases`) can still satisfy the AC's intent, and pytest collection by `-k` substring matches both.

**Decision tree:**

- **Gap confirmed real** (the setting is off, the metric does not exist, the doc is absent, the bug is still present): continue to Path A / Path B as usual.
- **Gap already satisfied** (probe output shows the state already matches the issue's goal): do NOT abandon the task. **The canonical evidence to cite in the comment is the AC's literal `Verify:` command output** — quote the command verbatim alongside its observed result (exit 0 + relevant stdout snippet for `pytest -k <test_name>`; matching grep hit + line numbers for `grep -n ...`; etc.). The AC author already designated this as the machine-checkable signal of done; the comment is just relaying that signal back. Then propose one of two reduced-scope outcomes:
  - **Reduced docs-only PR** — when there is a residual docs delta worth producing (e.g. "document that Container Insights is enabled and cite the Terraform attribute that landed it"). The AC's intent usually has a residual of this shape; complete it as a minimal PR before proceeding to merge.
  - **Verification-only close** — when there is no remaining docs delta, which is the common case for `fix(test)` and `test(...)` issues whose acceptance criteria are exhausted by "the test passes." The AC's `Verify:` command output IS the verification evidence; post it, name the PR that landed the fix, close the issue with `--reason completed`, and run `scripts/unblock-dependents.sh <N>` if anything was blocking on it. Skip Path A entirely — there is no PR to file. The label-interlock teardown (`gh issue edit <N> --remove-label status/in-progress`) still runs at the close.

  Write the comment to `{worktree}/tmp/gap_probe_comment.txt` and post it via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478):
  ```
  {worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/gap_probe_comment.txt
  ```
- **Probe ambiguous** (e.g. the AWS API returns unexpected output, the grep hits unrelated files, the state is partially configured): treat as "gap real" and continue to Path A.

Exit codes:

- **Gap real or ambiguous** → continue to Path A / Path B.
- **Gap already satisfied (docs delta remains)** → post comment, pivot to reduced (docs-only) scope, continue as Path A with the reduced scope.
- **Gap already satisfied (no docs delta — typical `fix(test)` and `test(...)`)** → post comment with the AC's `Verify:` command output as evidence, close issue with `--reason completed`, release `status/in-progress` label, run `scripts/unblock-dependents.sh <N>`. Skip Path A — there is no PR.

### Step 4c — Verify the issue's hypothesis (5-minute probe)

**Trigger condition:** Run this step when the issue is a bug-fix or investigation-style task that names a specific function, regex, layer, or code path the filer believes is broken — i.e. the issue body contains a *suspected root cause* and a *prescribed fix*. Skip when (a) the issue is purely additive and §4b's gap probe already covered it, (b) the symptom and the broken layer are visibly the same line (one-line typo/copy fix), or (c) the issue is `type/dx` workflow / docs / template work where there is no upstream "wrong return value" to verify.

**Why this exists — the verify-the-hypothesis-first pattern.** Issue bodies that mix observed symptoms with a prescribed fix can lock the agent into the wrong-hypothesis path. If the hypothesis is wrong, extending the regex / patching the named function does nothing — the symptom stays unchanged and a full ralph cycle is wasted (or, worse, a no-op regex change merges and the real bug stays unfixed). The fix is to verify the hypothesis with a cheap probe before writing any implementation code. This is the agent-side complement to `docs/agent/issue-authoring.md` §"Hypothesis vs. evidence" — same lesson, applied at pickup time. Same shape as `docs/agent/investigation-patterns.md` §"Instrument before you guess" — don't anchor on an unconfirmed hypothesis.

**Canonical worked example — #4251.** The prescribed fix was "extend `_HEARING_DATE_PROBATE_RE` to match the dept-38 PDF format." Investigation revealed the regex was already returning the correct date — the broken layer was `is_plausible_hearing_date` rejecting the correctly-extracted value because dept-38 master probate calendars publish 30+ days ahead, outside the civil ±14-day window the filter enforces. A 5-minute probe (run `_cc_hearing_date_from_pdf` on the sample PDF and inspect the return value) would have surfaced that immediately. An agent who blindly extended the regex would have found the symptom didn't move and burned the full iteration cap rediscovering the actual broken layer.

**What to do:**

1. **Read the issue body and identify the hypothesis explicitly.** State it back to yourself in one sentence: "The issue claims `<function/regex/layer>` is broken because `<reason>` and the fix is `<edit>`."

2. **Run the issue's verification steps if it lists them.** When the issue body has a `## Hypothesis verification steps` section (or equivalent — `## Verify before fixing`, etc.), run those steps **verbatim** as the §4c probe. The filer already wrote the cheapest possible falsification check; running it first short-circuits the "what to probe for" decision and produces probe output that is also free verification evidence to cite in the A.2b process summary.

3. **Otherwise, run a generic 5-minute probe.** Pick the pattern that matches the hypothesis shape:

   - **"Function X returns wrong value"** — fetch the smallest reproducer mentioned in the issue (S3 key, sample input, DB row id), run X on it, compare the return value to the expected value. If X returns the expected value, the hypothesis is wrong — the symptom must come from a downstream caller or filter. Write the probe to `{worktree}/tmp/verify.py` and run it. Example shape, drawing on #4251:
     ```python
     # {worktree}/tmp/verify.py
     from packages.scraper_framework.src.courts.ca.oc_tentatives import _cc_hearing_date_from_pdf
     pdf_bytes = open("{worktree}/tmp/sample.pdf", "rb").read()
     print(repr(_cc_hearing_date_from_pdf(pdf_bytes)))
     # If this prints the expected date, the regex is fine; the bug is downstream.
     ```
   - **"Regex Y doesn't match format Z"** — extract the regex from the named module, run it against a sample of format Z, inspect the match groups. Same falsification pattern: if it matches, the hypothesis is wrong.
   - **"Filter / validator F rejects valid input"** — run F on the input the issue says is being rejected. If F accepts it, the rejection is happening somewhere else.
   - **"Query Q returns N rows but should return M"** — run Q against dev (`scripts/dev-db-query.sh`), inspect the actual rows. If the count matches expected, the symptom is in a downstream rendering / aggregation layer.

4. **Trace from observed symptom through every layer to the named hypothesis.** Even if the named function does return the wrong value, ask: is there a SELECT / dispatch / filter upstream that controls whether this code path even runs on the affected rows? The trace-select-before-validating-fix lesson (an inner branch can be dead code if the SELECT/dispatch filters out the rows) applies here — verify the full call chain, not just the bottom layer the issue points at.

**Decision tree:**

- **Hypothesis confirmed** — the named function/regex/layer does in fact produce the wrong value, AND the trace-select check confirms the affected rows actually flow through that code path. Proceed with the prescribed fix as Path A normally.
- **Hypothesis falsified** — the named function/regex/layer returns the expected value when probed directly. **Do NOT implement the prescribed fix.** Post a comment on the issue stating: (a) the hypothesis verification you ran, (b) the actual return value (with sample input cited), (c) the next layer to inspect (typically the immediate caller or a filter the value passes through). Then either:
  - Continue investigating to root-cause from observed symptoms (treat the issue as Path B / investigation), OR
  - Re-scope the issue: post a comment proposing the corrected hypothesis and prescribed fix, and proceed with Path A against the new scope.

  Write the comment to `{worktree}/tmp/hypothesis_falsified_comment.txt` and post it via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478):
  ```
  {worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/hypothesis_falsified_comment.txt
  ```
- **Probe ambiguous** (the function returns something unexpected but not clearly right or wrong; the sample input may not be representative): instrument before guessing. Add a structured log to the suspected layer that captures the raw input/output it actually saw, ship that as an instrumentation-only PR (or local probe), and re-trigger. See `docs/agent/investigation-patterns.md` §"Instrument before you guess." Don't extend the prescribed fix on top of an ambiguous probe.

**Cost / benefit.** A 5-minute probe is cheap relative to a 5-15 minute ralph iteration plus the PR / CI / merge round trip the wrong-fix path incurs. Even when the probe ratifies the hypothesis (the common case for well-written issues), the probe output is also free verification evidence to cite in the A.2b process summary and A.8 verification-evidence comment.

---


### Path A: Implementation task (feature, bug fix, refactor)

Follow the full PR Workflow defined in CLAUDE.md. **All commits must be on the worktree branch — never on `main`.** Summary of required substeps:

**IMPORTANT — Completion contract:** This task is NOT done after implementation or after ralph says SHIP. The task requires completing ALL substeps A.1 through A.9. After ralph returns, you MUST continue with A.2b (process summary), A.3 (commit/push), A.4 (merge conflicts), A.5 (CI), A.6 (PR update), A.7 (merge), A.8 (deploy verification), and A.9 (retrospective). Stopping after ralph is a bug — see issue #721.

#### A.0 — Post-compaction recovery (READ FIRST after any context reset)

**When this applies:** Your context just went through autocompaction (the conversation summary references "previous conversation"), or you are otherwise starting a turn without a clear memory of which A.x phase you are in. Autocompaction preserves implementation details but elides procedural imperatives, so the summary alone cannot tell you whether the task is complete — the **status file** is authoritative (see #2545).

**What to do — mechanical procedure:**

1. **Run the recovery check:**
   ```
   {worktree}/scripts/check-task-recovery.sh {worktree}
   ```
   - **Exit 0 (`DONE`):** the status file shows a terminal phase (`done`, `verified`, `blocked`). The task is actually complete. You may emit your final report and end the turn.
   - **Exit 1 (`RESUME`):** work remains. The script prints the next required step (e.g. `A.2b — post process summary on issue`). Continue from that step — do NOT emit `end_turn`, do NOT produce a final report.
   - **Exit 2 (`UNKNOWN`):** the status file is missing or malformed. Assume work remains. Re-read this SKILL.md from the top and reconstruct phase from git state (uncommitted changes? PR open? CI green?) before proceeding.

2. **Increment `autocompact_count`** in the status file so the dispatcher can track the pattern. If the file currently has no `autocompact_count` field, add one initialized to `1`. Otherwise increment the existing value.

3. **Re-read this SKILL.md** from the step named in the `RESUME` output — e.g. if the script says "next step: A.2b", read A.2b and onward before taking any further action.

4. **Do NOT emit `end_turn`** until `check-task-recovery.sh` returns exit 0 (`DONE`). This is the same completion contract as the normal flow, but made explicit because the compacted summary may have dropped it.

**Why this section exists:** Two confirmed incidents (#2500, #2502) showed /task agents emitting `end_turn` after autocompact with `phase=ralph-worker (1)` in the status file — i.e. still mid-implementation, with uncommitted changes, no PR, and no merge. The agents read their own "iteration 1 COMPLETE" artifact as a done-signal and stopped. The recovery script is the mechanical self-check that prevents this failure mode.

#### A.1a — Refresh worktree base onto origin/main (MANDATORY)

**First action after worktree verification.** When Claude Code creates a worktree with `isolation: "worktree"`, the branch is cut from whatever `origin/main` was at the *moment of worktree creation* — which can be many merged PRs stale by the time the agent actually begins editing. Refresh the base before touching any files so the ralph loop and subsequent push don't hit surprise merge-bases (see #3075).

The step runs `git fetch origin main` + `git rebase origin/main` under the hood, wrapped behind the `preflight_branch_fresh --fetch` helper (already defined in `scripts/preflight.sh`). Use the standalone wrapper — prevent stale-worktree-base merge-base surprises — see #3075:

```
{worktree}/scripts/preflight-branch-fresh.sh --fetch
```

`preflight-branch-fresh.sh` is a thin wrapper around `preflight_branch_fresh` in `scripts/preflight.sh`. The wrapper lets the check run in a single Bash tool call — `source scripts/preflight.sh && preflight_branch_fresh --fetch` trips the preflight hook's "quoted strings combined with &&" check. Same pattern as `check-duplicate-pr.sh` (see #2706).

- **Exit 0 (fresh):** the worktree branch is at or ahead of `origin/main`. No rebase needed — continue to A.1.
- **Exit 1 (behind origin/main):** a `PREFLIGHT FAIL: Branch is N commit(s) behind origin/main.` message was printed to stderr. Run the rebase as a separate tool call (CLAUDE.md §ALWAYS requires fetch-then-rebase; the wrapper already fetched, so only the rebase is left):
  ```
  git -C {worktree} rebase origin/main
  ```
  - **Rebase succeeds (clean fast-forward):** re-run `scripts/preflight-branch-fresh.sh` (no `--fetch` — we just fetched) to confirm the gap is closed, then continue to A.1.
  - **Rebase fails with conflicts — STOP, do NOT auto-resolve.** This is the #3075 case where another PR landed that changed a file we would be editing. The agent has not yet started work, so there is no reasonable way to resolve conflicts against intent that has not yet been captured in code. Abort the rebase, post a block on the issue, release the `status/in-progress` label, and stop:
    ```
    git -C {worktree} rebase --abort
    ```
    Write a short block comment to `{worktree}/tmp/rebase_block.txt` naming the conflicting files and the competing origin/main commit(s), then post via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478) and update labels (writes — stay on `gh`):
    ```
    {worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/rebase_block.txt
    gh issue edit <N> --repo judgemind/judgemind --add-label status/blocked --remove-label status/in-progress
    ```
    Stop — do not proceed to A.1. Worktree cleanup is automatic. A follow-up agent (or a human) can pick the issue up after the conflicting PR has been triaged.

The daemon-side path (Fargate, `_create_worktree` in `scripts/dispatcher/daemon.py`) already runs `_baseline_fetch_origin_main` before `git worktree add` — see `scripts/dispatcher/tests/test_daemon_baseline_clone.py::TestCreateWorktreeBaselineMode`. A.1a exists because Claude Code's local `isolation: "worktree"` mode has no equivalent hook: the platform cuts the branch from whatever `origin/main` happens to be at worktree-creation time, which is minutes-to-hours before the agent actually starts editing.

#### A.1 — Set up dependencies
Write status: `phase: setup`, `summary: Installing dependencies for <packages>`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} setup`

For Python packages you will touch, run the helper (use `timeout: 1200000` since the first install can exceed 2 minutes):
```
{worktree}/scripts/install-package-venv.sh <pkg>     # e.g. scraper-framework, nlp-pipeline
```

The helper creates `packages/<pkg>/.venv`, installs the local `judgemind-config` sibling first when required, then installs the target package with `[dev]` extras. Plain `pip install -e ".[dev]"` **fails** for `scraper-framework` and `nlp-pipeline` because `judgemind-config` is an unpublished local dependency (see #2491). The helper is idempotent — re-running it is a no-op when the venv is already populated.

For TypeScript packages (use `timeout: 1200000` as npm install may exceed 2 minutes):
```
npm install
```

Skip this for Terraform-only or docs-only tasks.

#### A.2 — Implement and review (ralph loop)

**Cockpit milestone:** at the top of A.2 — *before* dispatching ralph or starting direct implementation — emit the `planning` milestone so the cockpit shows the agent has finished setup and is now planning the implementation:

```
scripts/dispatcher/progress.sh "$AGENT_ID" planning || true
```

Then, immediately before invoking `/ralph` (testable code paths), emit the `ralph` milestone so the cockpit can distinguish "agent is reading the issue" from "agent is in the iterate-implement-review loop":

```
scripts/dispatcher/progress.sh "$AGENT_ID" ralph || true
```

For non-testable tasks that skip /ralph, the `planning` milestone is the only one in this section — the next milestone is `summary` at the top of A.2b.

- **For testable code tasks** (Python, TypeScript): use the `/ralph` loop — iterative work-then-review with fresh context each iteration. See `.claude/skills/ralph/SKILL.md`. This replaces the old `/tdd` + self-review steps. `/ralph` handles implementation (TDD), pre-PR checks, and cross-perspective review internally. It returns when the reviewer subagent says SHIP.
- **For non-testable tasks** (Terraform, DB migrations, CI/CD, docs): implement directly, then run all applicable pre-PR checks (see `docs/agent/code-standards.md` §Pre-PR Checks) and review your own diff before continuing.
- **For ingestion/extraction pipeline tasks** (scraper changes, LLM prompt changes, enrichment logic): use the local dev stack to iterate. The local DB + S3 cache enables fast iteration without deploying to dev. Run `scripts/rebuild_db.sh --skip-reset` to re-process documents through the pipeline and verify data correctness against source documents. See `docs/agent/local-dev.md`. **Prioritize correctness over completeness** — verify that extracted fields match the source document, not just that fields are populated.
**When the agent determines the work cannot proceed for non-ralph reasons.** Sometimes the agent (not /ralph) discovers — during scope-completeness checks, dependency setup, or initial probing on dev — that the requested work cannot proceed because of an upstream condition. Common cases:

- The prescribed command (e.g. `rebuild_db.py --county <name>`) cannot drain the bug class the issue is asking about (LLM-cache-resident extraction bugs, OpenSearch admin password rotation, billing/credit balance depleted).
- An external secret has rotated, or an external account/budget has hit a hard limit.
- An infra-level service is down and the failure is operator-only to resolve.

In these cases, posting a thorough BLOCKED verification-evidence comment is necessary but **not sufficient**. Two follow-ups are MANDATORY before stopping, otherwise the issue stays `agent/ready` and the next agent re-investigates the same upstream condition (see #4035 root cause):

1. **Block the dependent issue against every identified upstream blocker.** For each upstream cause:
   - If a GitHub issue already tracks it, run `scripts/block-issue.sh <this-issue> <existing-tracker>` — this adds `Blocked by #<existing>` to the body, sets `status/blocked`, and removes `agent/ready`.
   - If no GitHub issue tracks it yet, use `scripts/block-on-new-issue.sh <this-issue> --title "..." --body-file <path> --priority p1 [--label area/...]` — this files the tracker AND wires `Blocked by #<new>` atomically. Do NOT add `agent/ready` to the new tracker if its resolution is operator-only (billing, secrets, account-level) — leaving an operator-only issue `agent/ready` just sends another agent down the same dead end.
2. **Verify the dependent issue is now BLOCKED, not READY.** After step 1, the dependent issue must carry `status/blocked` (and not `agent/ready`). `block-issue.sh` already removes `agent/ready` as part of its label edit; the post-condition you're confirming is that the daemon's queue scan will skip this issue on its next tick.

The `Blocked by #N` line + `status/blocked` label is the only signal that auto-restores `agent/ready` when the upstream blocker closes (via the `unblock-issues` workflow / `scripts/unblock-dependents.sh`). A bare BLOCKED comment with `agent/ready` left in place produces the failure mode in #4035 — another agent picks the issue up hours/days later and runs the same investigation again. See `docs/agent/task-dependencies.md` for the full mechanics.

After both follow-ups land, **release the label interlock** as usual:
```
gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress
```
Then stop — the worktree will be cleaned up automatically.

- If `/ralph` exits with a blocker (STUCK or max iterations), the issue has already been commented on and blocked (via `scripts/block-issue.sh` or `status/blocked` label). Before stopping, **release the label interlock** so the issue isn't permanently hidden from future agents:
  ```
  gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress
  ```
  Idempotent — exits 0 if the label is already absent. Then stop — the worktree will be cleaned up automatically by Claude Code (if spawned with `isolation: "worktree"`) or by the dispatcher.

**POST-RALPH CHECKPOINT — Do not skip this.** After `/ralph` returns:
1. Read `{worktree}/tmp/ralph/ralph-done.txt` to confirm ralph completed with SHIP status.
2. Read `{worktree}/tmp/ralph/review-result.txt` to verify the final verdict is SHIP.
3. If both confirm SHIP, **immediately continue to A.2b below.** Do not stop, do not return, do not consider the task done. The code is implemented but not yet committed, pushed, or merged — the task is only halfway complete.

**POST-RALPH SELF-RECOVERY GUARD:** Before proceeding to A.2b, verify that the task is genuinely incomplete by running these checks:
1. Run `git -C {worktree} status` to confirm there are uncommitted changes (there should be — ralph implements but does not commit).
2. Run `git -C {worktree} log --oneline -1` to see the latest commit — it should NOT contain the current issue number (the implementation hasn't been committed yet).
3. Check whether a PR already exists for this branch: `mcp__github__list_pull_requests owner=judgemind repo=judgemind head="judgemind:<branch-name>" per_page=1`. It should return an empty list (no PR yet).
4. Run the authoritative recovery check: `{worktree}/scripts/check-task-recovery.sh {worktree}`. It must return exit 1 (`RESUME`). If it returns 0 (`DONE`), that means the status file shows a terminal phase you did not intend — update the status file to reflect the actual phase and re-run the check.

If any of these checks show that work remains (uncommitted changes exist, no PR yet), you MUST continue to A.2b. Do not exit. Do not return. Do not consider the task done. Exiting at this point is a critical workflow failure (#721, #2545).

#### A.2b — Post process summary on issue (MANDATORY)

**Cockpit milestone:** at the top of A.2b — before extracting acceptance criteria or writing the summary — emit the `summary` milestone:

```
scripts/dispatcher/progress.sh "$AGENT_ID" summary || true
```

Before committing or creating a PR, post a process summary comment on the GitHub issue. This creates an auditable record and forces explicit verification of each acceptance criterion.

**Step 1 — Extract acceptance criteria.** Read the issue body and identify all acceptance criteria (typically `- [ ]` checkboxes). Also check issue comments for any additional or modified criteria.

**Step 2 — Map each criterion to the implementation.** For EACH acceptance criterion:
- State whether it is **met**, **not met**, or **not applicable**.
- If met: describe specifically how — reference the file(s), function(s), or test(s) that satisfy it.
- If not met: explain why (e.g., out of scope, blocked on something, requires post-deploy verification).
- If not applicable: explain why.

**Step 3 — Write and post the summary.** Write the comment to `{worktree}/tmp/process_summary.txt` with this structure:

```
## Process Summary

### What was implemented
<Brief description of the approach — 2-4 sentences>

### Acceptance criteria mapping

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | <criterion text> | Met | <file/function/test that satisfies it> |
| 2 | <criterion text> | Met | <file/function/test that satisfies it> |
| 3 | <criterion text> | Not met | <reason — e.g., requires post-deploy verification> |

### Scope decisions
<Any intentional exclusions or scope boundaries — what was NOT done and why>
```

Post it via `scripts/gh-comment-with-retry.sh` (write — stays on `gh` until MCP writes land). The wrapper transparently handles the 504-after-success failure mode (#4478) so a flaky GitHub response doesn't surface as a duplicate comment or a false-failure block on the PR push:
```
{worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/process_summary.txt
```

**GATE CHECK:** If any acceptance criterion is "not met" and the reason is NOT "requires post-deploy verification" or "not applicable," do NOT proceed to A.3. Go back to A.2 and address the gap first. The process summary is a self-check — if it reveals unmet criteria, the implementation is not complete.

#### A.3 — Stage, commit, and push
Write status: `phase: pushing`, `summary: Staging, committing, and pushing to remote`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} pushing`

**Cockpit milestone:** before staging files / running `git push`, emit the `push_and_pr` milestone so the cockpit reflects "agent is leaving the worktree":

```
scripts/dispatcher/progress.sh "$AGENT_ID" push_and_pr || true
```

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

Immediately open a PR after the first push — never push without creating one. **Before creating, re-run the duplicate-PR check** as a cheap second guard — the primary check already ran in Step 4a (see #3098). This second invocation catches the narrow race where a concurrent agent (dispatcher double-claim, operator re-dispatch) opened a PR between our Step 4a check and our push:

```
{worktree}/scripts/check-duplicate-pr.sh <N>
```

(`check-duplicate-pr.sh` is a thin wrapper around `preflight_no_duplicate_pr` in `scripts/preflight.sh`. The wrapper lets the check run in a single Bash tool call — `source scripts/preflight.sh && preflight_no_duplicate_pr <N>` trips the preflight hook's "quoted strings combined with &&" check. See #2706.)

- If it returns **0** (duplicate found), a `duplicate:` line is printed to stdout containing the existing PR number. This is the concurrent-race case — our Step 4a check saw no duplicate, but one appeared while we were running ralph. **Adopt that PR** instead of creating a new one — push your local changes to the existing branch and use `gh pr edit` to update the body if needed. (The full adoption decision tree lives in Step 4a.1 — apply the same logic here.)
- If it returns **1** (no duplicate), an `ok:` line is printed to stdout. Proceed to create the PR normally.
- If it returns **2** (error), an `error:` line is printed to stderr. Proceed to create the PR (fail-open).

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

Create the PR via the `gh-pr-with-retry.sh` wrapper (#4527). The wrapper invokes `gh pr create` first and falls back to `gh api -X POST /repos/.../pulls` on the explicit `GraphQL: API rate limit already exceeded` stderr marker — same shape as `gh-comment-with-retry.sh` (#4503). Auth, validation, and other 5xx failures pass through unchanged. Pass the worktree branch explicitly to `--head` (the wrapper does not infer it from `git rev-parse`):
```
{worktree}/scripts/gh-pr-with-retry.sh create \
    --title "..." \
    --body-file {worktree}/tmp/pr_body.txt \
    --base main \
    --head <branch>
```

#### A.4 — Verify no merge conflicts
Read merge status via MCP:

```
mcp__github__get_pull_request owner=judgemind repo=judgemind pull_number=<PR-N>
```

Check the `mergeable` field in the response. If `mergeable` is `false` (or `mergeable_state` / `mergeStateStatus` is `dirty`), rebase and resolve:
```
git -C {worktree} fetch origin main
git -C {worktree} rebase origin/main
```
Resolve conflicts, `git rebase --continue`, then push with `--force-with-lease`.

**Do NOT treat `mergeStateStatus: UNSTABLE` as a conflict or a merge-blocker.** UNSTABLE only means "at least one check run on this SHA didn't succeed" — and because GitHub computes it over the *entire history* of check runs on the SHA (not just the latest attempt), a stale failed run from an earlier CI attempt keeps the PR `UNSTABLE` forever even after a successful rerun flips the rollup green. The authoritative merge gate checks the **latest** conclusion per check — see the recipe in A.7 and `docs/agent/code-standards.md` §Interpreting mergeStateStatus (UNSTABLE-but-green).

#### A.5 — Monitor CI and iterate until green
Write status: `phase: ci-watch`, `summary: Watching CI run <run-id>`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} "ci-watch (1)"`

**Cockpit milestone:** before starting the CI watch, emit the `awaiting_ci` milestone (with the PR number as the optional detail) so the cockpit can show "agent is parked on CI for PR <N>":

```
scripts/dispatcher/progress.sh "$AGENT_ID" awaiting_ci "PR #<PR-N>" || true
```

**Run CI watches in the foreground** — do not use `run_in_background`. You cannot proceed until CI finishes, so background execution just generates unnecessary `<task-notification>` noise for the dispatcher. **Use `timeout: 1200000`** as CI runs typically take 10-25 minutes.

**Do NOT use the `Monitor` tool for CI watch.** Monitor's bash-while-loop pattern fires a wake-up event on every state-string change, and `gh pr view --json mergeable` flickers between `UNKNOWN` and `MERGEABLE` while CI is still running. Each flicker is an unproductive turn for you and the calling dispatcher — observed root cause behind C2 (#3909), C3 (#3922), C4 (#3927), F2 (#3926) prematurely yielding before they could merge their own PRs. If `scripts/wait-for-ci.sh` gets auto-backgrounded by the harness, **just retry it directly** — do NOT switch to Monitor. Synchronous polling with the canonical helper is the only sanctioned CI-watch path.

Use `scripts/wait-for-ci.sh` as the canonical PR CI gate:

```
scripts/wait-for-ci.sh <PR-N>
```

This polls the check-runs API with `filter=latest` (deduplicates re-runs) and exits 0 via either of two paths: (a) the canonical-merge-gate fast-path — `mergeable == MERGEABLE`, any `ci-passed` entry is `success`, no latest check has failed — fires immediately even if stale `in_progress` entries from a superseded CI run linger in the response (#4069); (b) the all-checks-complete fallback — `pending == 0`, `ci-passed` is `success`, no failures, `mergeStateStatus` is `CLEAN` or `UNSTABLE` — fires when CI legitimately drains to zero pending. Stdout names the path explicitly with `canonical merge gate green` or `all checks complete`. Exit 1 = failure, Exit 2 = timeout.

**Exit 3 — REBASE_REQUIRED (#4412).** When CI is green (`ci-passed=success`, no latest failures) but `mergeStateStatus=DIRTY` (a concurrent merge landed on origin/main that conflicts with this PR's diff), `wait-for-ci.sh` exits 3 immediately on the first poll iteration where this is true rather than continuing to poll until timeout. There is no path forward by waiting — the agent must rebase before the PR can merge. On exit 3, follow the A.4 rebase recipe (`git fetch origin main && git rebase origin/main && git push --force-with-lease`), then re-enter the CI watch loop, incrementing the `ci-watch (N)` phase counter and re-emitting `awaiting_ci`. The exit-3 path is the rebase-fast-path equivalent of A.4's merge-conflict handling — both bottom out in the same rebase + force-push, but exit 3 surfaces the signal in <1s instead of the ~10 min the script previously burned re-logging `still waiting...` until timeout.

For workflow-run-level watching (deploy workflows in §A.8), `gh run watch` stays as the fallback:

```
gh run watch <run-id> --repo judgemind/judgemind --interval 60 --exit-status --compact
```

For quick status polls without watching, `mcp__github__get_pull_request_status` returns the combined check rollup in one MCP call.

If CI fails: write status `phase: ci-fix`, `summary: Fixing CI failure: <brief reason>`. Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} ci-fix`. Emit the `fix_ci` cockpit milestone (with a brief failure-class detail so the cockpit can group recurring failure patterns) before diagnosing:

```
scripts/dispatcher/progress.sh "$AGENT_ID" fix_ci "<brief failure class — e.g. ruff, pytest, typecheck>" || true
```

Diagnose, fix locally, push, return to A.4. On the next CI watch, increment the attempt number in the phase name (e.g. `ci-watch (2)`) and re-emit `awaiting_ci` for the new run. Repeat until all checks pass.

#### A.6 — Update the PR test plan
Fetch the current PR body via MCP:

```
mcp__github__get_pull_request owner=judgemind repo=judgemind pull_number=<PR-N>
```

Check off the **Automated checks** items that passed in CI. Do NOT check off **Post-deploy verification** items yet — those are checked in A.8 after merge and deploy. Write the updated body to `{worktree}/tmp/pr_body.txt`, then update via the `gh-pr-with-retry.sh` wrapper (#4527 — same GraphQL-quota REST fallback as A.3):
```
{worktree}/scripts/gh-pr-with-retry.sh edit <PR-N> --body-file {worktree}/tmp/pr_body.txt
```

#### A.7 — Merge the PR
Write status: `phase: merging`, `summary: Squash merging PR #<N>`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} merging`

**Cockpit milestone:** immediately before running `gh pr merge`, emit the `merge` milestone so the cockpit shows the agent has crossed the merge gate:

```
scripts/dispatcher/progress.sh "$AGENT_ID" merge "PR #<PR-N>" || true
```

The PR has passed the ralph loop review (A.2) and CI is green. **Before merging, confirm the merge gate is green.** The gate is:

1. `mergeable == MERGEABLE` (GitHub can compute a merge commit — no conflicts), AND
2. The required status check `ci-passed` has `conclusion: SUCCESS` on its latest run, AND
3. No *latest* check has `conclusion: FAILURE`.

`SKIPPED` is fine, and `CANCELLED` on a non-required check (typically Vercel / Smoke Test concurrency cancellation) is fine. `mergeStateStatus == UNSTABLE` is **not** a blocker when the three gates above pass — see `docs/agent/code-standards.md` §Interpreting mergeStateStatus (UNSTABLE-but-green) for why. Use the one-line recipe:

```
gh pr view <PR-N> --repo judgemind/judgemind \
  --json mergeable,statusCheckRollup \
  --jq '{mergeable, rollup: [.statusCheckRollup[] | {name, conclusion}]}'
```

If `mergeable` is `MERGEABLE`, `ci-passed` is `SUCCESS`, and nothing is `FAILURE`, merge via the `gh-pr-with-retry.sh` wrapper (#4527). The wrapper invokes `gh pr merge` first and falls back to `gh api -X PUT /repos/.../pulls/<N>/merge` on the explicit `GraphQL: API rate limit already exceeded` stderr marker, plus a follow-up `DELETE /repos/.../git/refs/heads/<head>` when `--delete-branch` is passed (mirroring the one-shot semantic of native `gh pr merge`). The two #4058 / #4231 fallbacks documented below are unchanged — they handle different failure modes (`base branch policy prohibits the merge` and `5xx / 504 Gateway Timeout`) that the wrapper does NOT auto-recover from:
```
{worktree}/scripts/gh-pr-with-retry.sh merge <PR-N> --squash --delete-branch
```

**Fallback — `gh pr merge` rejects with `base branch policy prohibits the merge` (#4058).**

`gh pr merge` runs a stricter pre-flight than the underlying REST API: it consults `mergeStateStatus` (and a few related fields) directly and refuses to call `PUT /repos/.../pulls/N/merge` whenever GitHub returns `BLOCKED` — even when the actual branch-protection requirements are satisfied. The trigger is GitHub's PR-rollup cache lagging the per-check conclusion updates: after a CI rerun completes successfully, the latest `ci-passed` check is `SUCCESS` and the canonical merge gate (mergeable=MERGEABLE, ci-passed=SUCCESS, no FAILURE) is green, but `mergeStateStatus` can stay `BLOCKED` for several seconds (occasionally tens of seconds) before GitHub recomputes it. The REST `PUT /merge` endpoint re-evaluates branch protection at the moment of the call against the latest required-check conclusions and accepts the merge — it does not rely on the cached `mergeStateStatus`. `gh pr merge --auto` is also not a workaround on this repo: GitHub returns "Auto merge is not allowed for this repository."

**Note (#4068, 2026-05):** an earlier version of this section attributed the BLOCKED state to "a phantom rollup entry: a check that registered on the SHA but never reported a final conclusion." That framing was incorrect — the rollup entries that look like phantoms in `gh pr view --json statusCheckRollup` (those with `conclusion: null`) are legitimate `StatusContext` entries from third-party commit-status integrations like Vercel and codecov/patch. `StatusContext` entries use the `state` field, not `conclusion`, by GraphQL schema design; they are not phantoms. The canonical CI rollup classifier in `scripts/dispatcher/phase_transitions.py` (`_ci_rollup_state`) has handled them correctly via `__typename` branching since PR #3200 (2025-12). Across 283 dispatcher-managed merges in the 14 days before #4068's investigation, this fallback fired zero times — the fallback is rarely needed in practice but kept here for the residual transient-rollup-lag case described above. See `docs/investigations/phantom-rollup-blocked-but-green-2026-05.md` for the full evidence chain.

**Use the API-merge fallback only when the canonical merge gate above is green** — i.e. all three of:

1. `mergeable == MERGEABLE`, AND
2. The required `ci-passed` check has `conclusion: SUCCESS` on its latest run, AND
3. No *latest* check has `conclusion: FAILURE`.

If those three are green and `gh pr merge --squash --delete-branch` rejects with stderr containing `base branch policy prohibits the merge`, fall through to the REST API. Both calls below are write operations — they stay on `gh` (no MCP equivalents):

```
gh api /repos/judgemind/judgemind/pulls/<PR-N>/merge -X PUT -f merge_method=squash
gh api /repos/judgemind/judgemind/git/refs/heads/<branch-name> -X DELETE
```

The first call performs the squash-merge; on success its JSON response includes `{"merged": true, "message": "Pull Request successfully merged", "sha": "..."}`. The second call deletes the head branch — `gh pr merge --delete-branch` does this automatically, the API path does not, so it is a separate explicit call. `<branch-name>` is the PR's `headRefName` from `mcp__github__get_pull_request` (the worktree branch — typically `worktree-agent-<id>` or the original ralph branch name).

If the canonical merge gate above is **not** green, do NOT use this fallback — diagnose the failing check first. The fallback is for the transient-rollup-lag case where the REST endpoint is willing but `gh pr merge`'s pre-flight is not, not for bypassing real CI failures or branch-protection requirements.

A worked example from #4053 (the PR that surfaced #4058):

```
$ gh pr merge 4053 --squash --delete-branch
X Pull request judgemind/judgemind#4053 is not mergeable: the base branch policy prohibits the merge.

$ gh pr view 4053 --json mergeable,mergeStateStatus --jq '{mergeable, mergeStateStatus}'
{"mergeStateStatus":"BLOCKED","mergeable":"MERGEABLE"}

$ gh api /repos/judgemind/judgemind/pulls/4053/merge -X PUT -f merge_method=squash
{"merged":true,"message":"Pull Request successfully merged","sha":"00396b68..."}

$ gh api /repos/judgemind/judgemind/git/refs/heads/worktree-agent-... -X DELETE
```

**Fallback — `gh pr merge` returns 5xx / 504 Gateway Timeout (#4231).**

GitHub's `gh pr merge` periodically returns transient `5xx` / `504 Gateway Timeout` responses (sometimes wrapped in a multi-KB HTML error page that floods the agent transcript) **after the underlying REST endpoint has already accepted the squash on the GitHub side.** The 504 looks like a merge failure but is not — the merge already happened. Naively retrying `gh pr merge` here is wrong because the second call will fail with `Pull request is already closed` or similar, and the loud HTML response makes the failure mode look worse than it is. A concrete observed instance: PR #4230 (issue #4227, 2026-05-06) — two retries of `gh pr merge --squash --delete-branch` both returned 5xx; the PR had actually merged on the first call (`merged_at: 2026-05-06T15:54:59Z`), but `--delete-branch` did not run, leaving the head ref alive.

**Recipe — on 5xx / 504 / HTML response from `gh pr merge`, do NOT retry the merge call. Re-fetch PR state and treat closed-with-merged_at as success.**

```
mcp__github__get_pull_request owner=judgemind repo=judgemind pull_number=<PR-N>
```

Inspect the response:

- **`state: "closed"` and `merged_at: "<timestamp>"` is set** → the squash succeeded. The 5xx was a response-side failure, not a merge-side failure. Skip re-running `gh pr merge` entirely. If the head branch still exists (check `headRefName` against `gh api /repos/judgemind/judgemind/git/refs/heads/<branch>`), delete it explicitly with the same call from the `#4058` recipe above:
  ```
  gh api /repos/judgemind/judgemind/git/refs/heads/<branch-name> -X DELETE
  ```
  If that DELETE returns 422 / "Reference does not exist," the branch is already gone — no action needed. Continue to A.8.
- **`state: "open"` and `merged_at: null`** → the merge truly did not happen. Diagnose the underlying GitHub 5xx (rate limit, GitHub Status incident, branch protection) and retry `gh pr merge` once. If the second attempt also returns 5xx **and** PR state is still open, fall through to the API-merge fallback above (`gh api .../pulls/<N>/merge -X PUT`) — the REST endpoint and GraphQL endpoints take different paths through GitHub's infra and the REST one usually succeeds when GraphQL is degraded.
- **`state: "closed"` with `merged_at: null`** → the PR was declined or auto-closed during the failed merge attempt; do not re-attempt. Comment on the issue explaining and stop.

The dispatcher daemon takes a *different* path for this same failure mode — it pushes an empty commit to force a fresh rollup evaluation and re-enters `awaiting_ci` (#2641, see `docs/specs/dispatcher-v2-spec.md` §"Merge-phase stale-rollup auto-unstick"). That path exists because the daemon must drive the phase machine forward without operator intervention; the `/task` interactive path can take the simpler API-merge shortcut because the agent has already verified the canonical gate is green.

**Dependent issues will be unblocked automatically** by the `unblock-issues` workflow when the PR merges. No manual unblocking needed.

**Release the label interlock (label-only flow, #2927).** Remove the `status/in-progress` label so the daemon can see the issue as terminal and (on future reopens) pick it up again. Run this *before* A.8 deploy-watch so the signal releases promptly even if deploy verification drags:

```
gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress
```

Idempotent — exits 0 if the label is already absent. A failure here is logged but does not block A.8.

#### A.8 — Verify deployment and post evidence (MANDATORY)
Write status: `phase: deploying`, `summary: Watching deploy pipeline for <workflow>`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} deploying`

**Cockpit milestone:** at the top of A.8, before identifying the deploy workflow or starting the deploy watch, emit the `awaiting_deploy` milestone:

```
scripts/dispatcher/progress.sh "$AGENT_ID" awaiting_deploy || true
```

**A task is NOT done when the PR merges. A task is done when the change is deployed, verified working, AND verification evidence is posted.** The worktree stays alive until verification passes.

**Determine if this change has a deployed component:**
- Changes to `packages/api/`, `packages/scraper-framework/`, `packages/web/`, `infra/terraform/`, or scripts run via ECS → **has deployed component** → continue to Step 1.
- Changes to docs, CI config, `.claude/`, tooling scripts, or library code with no deployed service → **no deployed component** → skip to the evidence comment (Step 3) and post a skip-reason comment.

**Quick health check via MCP (optional):** before watching the deploy workflow, you can call `mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `api_operation: "DescribeServices"` against the affected dev service (e.g. `judgemind-ingestion-worker-dev`, `judgemind-api-dev`) to capture the current task-definition revision and `runningCount`. After the deploy lands, a second `DescribeServices` call confirms the revision bumped and the new tasks are healthy without leaving the agent context.

**Step 1 — Watch the deploy workflow:**

1. Identify the relevant deploy workflow based on which packages were modified:
   - `packages/api/` or API routes → `deploy-api.yml`
   - `packages/scraper-framework/` or scraper code → `deploy-scraper.yml`
   - `packages/web/` or frontend → `deploy-production.yml`
   - `infra/terraform/` → `terraform.yml`
2. **Run deploy watches in the foreground** — do not use `run_in_background`. **Use `timeout: 1200000`** as deploys can take several minutes. (`gh run list --workflow` and `gh run watch` have no MCP equivalent — stay on `gh`.)
   ```
   gh run list --repo judgemind/judgemind --workflow "<deploy-workflow>.yml" --branch main --limit 1 --json databaseId -q '.[0].databaseId'
   gh run watch <run-id> --repo judgemind/judgemind --interval 60 --exit-status --compact
   ```
3. If the deploy **fails**: file a new `priority/p1` issue describing the deploy failure, reference the merged PR, and add `agent/ready`. Do NOT consider the original task complete — comment on the original issue noting the deploy failure and linking the new issue.
4. If the deploy **succeeds**: continue to Step 2.

**Step 2 — Functional verification and acceptance criteria re-check (required):**

Write status: `phase: verifying`, `summary: Verifying feature works in dev environment`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} verifying`

**Cockpit milestone:** at the start of Step 2, emit the `verify` milestone:

```
scripts/dispatcher/progress.sh "$AGENT_ID" verify || true
```

A successful deploy only means the new image is running — not that the service works. Verify the feature is actually functional:

**Rendering is not exercising.** For any new user-visible affordance — a button, menu item, form, toggle, API endpoint, config flag, schema field — the verification must show the affordance being *exercised*, not merely present. "The page renders" / "the service returns 200" / "the endpoint exists in the schema" is NOT evidence. Click the button and observe the downstream effect. Hit the endpoint and verify the DB row changed. Toggle the flag and capture both branches of the behavior. If you cannot exercise the control in dev (e.g., it requires MFA re-auth and MFA isn't wired up), that is a hard STOP — the feature is not ready to ship and must be feature-flagged off or the PR reverted. See CLAUDE.md "No unreachable affordances."

| Change type | Verification | Required evidence |
|---|---|---|
| **DB migration + code** | Confirm migration applied (column/table exists via `scripts/dev-db-query.sh`) AND service processes a request without errors | DB query output showing the column/table exists + a successful request/response |
| **API endpoint** | Hit the endpoint on dev (`curl https://dev.api.judgemind.org/graphql`), confirm expected response shape AND any state change the endpoint is supposed to cause (DB row updated, job enqueued, etc.) | The curl response (status + body snippet) AND the state-change artifact (DB query result, log line, enqueued job id) |
| **Ingestion pipeline** | Confirm the worker processes at least one message successfully. Prefer `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` against `/ecs/judgemind-ingestion-worker-dev` for an ad-hoc Insights query; fall back to `scripts/ecs-logs.sh /ecs/judgemind-ingestion-worker-dev --lines 50` for the recent-N-lines convenience or `--follow` for live tail. | Log lines showing successful message processing |
| **Scraper** | Check ECS logs for the next scheduled run, confirm documents are captured without errors. Same MCP-first pattern as ingestion (Insights query against `/ecs/judgemind-scraper-dev`). | Log lines showing successful document capture |
| **Frontend — read-only page** | Confirm the affected page loads on `dev.judgemind.org` and renders the expected content | Screenshot via `scripts/run-py.sh scripts/screenshot.py` or page content showing the feature rendered |
| **Frontend — new interactive control** (button, form, toggle, link that triggers an action) | Exercise the control on `dev.judgemind.org` (actually click / submit / toggle) AND capture the downstream effect it produced | Screenshot or log of the control being used + concrete evidence of the state change it caused (DB row, log line, API response, visible UI transition to the new state). "Button renders" is NOT sufficient. |
| **DX/tooling** | Run the tool in a representative scenario and confirm expected output | Command output showing the tool works correctly |
| **Backfill / data migration script** | Execute the script against dev via `scripts/ecs-run-task.sh` (or locally if appropriate). <!-- scripts/ecs-run-task.sh stays for stream-logs propagation (not replaceable by MCP — see docs/agent/aws-to-mcp-migration.md) --> Confirm the expected data changes applied — e.g., query dev DB via `scripts/dev-db-query.sh` to check row counts, null rates, or sample records. | DB query results showing the data changed (e.g., "2444/2444 rulings now have ruling_text_html") |

**Script-producing tasks:** If a task produces a backfill, migration, or one-off fixup script that is meant to be run, executing it on dev and verifying results is part of the definition of done. When filing issues that include "create a backfill script" or similar, always include "backfill executed on dev and results verified" in the acceptance criteria.

**Dispatcher PRs — concurrent-merge cancellation:** When two dispatcher PRs merge within a few minutes of each other, `deploy-dispatcher.yml`'s `concurrency: cancel-in-progress: true` can cancel the earlier PR's `deploy-to-dev` stage even after a successful build-and-push. The later PR's rollout is usually a superset and still ships the earlier PR's code — but the observability is misleading (your deploy run shows CANCELLED). Use `scripts/verify-pr-in-deployed-sha.sh <pr-number>` as the go-to check for "did my PR actually land in the deployed dispatcher?" The helper fetches the PR's merge SHA via `gh pr view --json mergeCommit`, reads the latest `version_sha` from the dispatcher's CloudWatch startup log in `/ecs/judgemind-dispatcher-dev`, and runs `git merge-base --is-ancestor`. Exit 0 = landed, exit 1 = not yet deployed, exit 2 = usage/data error (missing arg, PR not merged, CloudWatch empty, git can't resolve the deployed SHA locally). See #3076.

**Acceptance criteria re-verification (MANDATORY for deployed changes):**

After verifying deployment health, go back to the issue's acceptance criteria and verify EACH one against the live environment. This is distinct from the A.2b process summary (which verifies against code/tests) — this step verifies against deployed reality.

For each acceptance criterion:
- **Frontend criteria**: take a screenshot or fetch page content showing the criterion is met.
- **Data criteria**: run the specific SQL query or API call that demonstrates the criterion.
- **API criteria**: hit the specific endpoint and confirm the expected response.
- **Behavior criteria**: trigger the specific scenario and capture the result.

Include the per-criterion verification results in the evidence comment (Step 3).

If functional verification **fails**: diagnose the issue. If it's a simple fix, fix it in a follow-up PR. If it's complex, file a `priority/p1` issue with details, reference the merged PR, and add `agent/ready`.

**Step 3 — Post verification evidence comment (MANDATORY — no exceptions):**

After verification succeeds (or after determining the change has no deployed component), you MUST post a verification evidence comment on the issue. This is a hard gate — the task cannot proceed to A.9 without this comment.

Write the comment to `{worktree}/tmp/verification_evidence.txt`, then post it via `scripts/gh-comment-with-retry.sh` (write — stays on `gh` until MCP writes land). The wrapper transparently handles the 504-after-success failure mode (#4478):
```
{worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/verification_evidence.txt
```

**For deployed changes**, the comment must include concrete evidence from the verification table above AND per-criterion verification results. Example format:

```
## Verification Evidence

**Change type:** API endpoint
**Environment:** dev

**Deployment health:**
curl response from dev.api.judgemind.org:
- Status: 200
- Response body (relevant snippet):
  {"data":{"ruling":{"id":"abc123","rulingTextHtml":"<p>The motion is GRANTED...</p>"}}}

**Acceptance criteria verification:**

| # | Criterion | Verified | Evidence |
|---|-----------|----------|----------|
| 1 | <criterion text> | Yes | <curl output, DB query result, or screenshot showing it works> |
| 2 | <criterion text> | Yes | <specific evidence> |
| 3 | <criterion text> | N/A (post-deploy) | <reason> |

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
1. Fetch the current PR body via `mcp__github__get_pull_request`.
2. Check off the **Post-deploy verification** checkboxes.
3. Write updated body to `{worktree}/tmp/pr_body.txt` and update with `gh pr edit --body-file`.

#### A.9 — Proceed to retrospective

Continue to Step 5.

---

### Path B: Investigation task

Write findings to `docs/investigations/<slug>-<YYYY-MM>.md` and/or into the issue body.

#### B.0 — Post-compaction recovery (READ FIRST after any context reset)

**When this applies:** Same as §A.0 — your context just went through autocompaction, or you are otherwise starting a turn without a clear memory of which B.x phase you are in.

**What to do:**

1. **Run the recovery check:**
   ```
   {worktree}/scripts/check-task-recovery.sh {worktree}
   ```
   - **Exit 0 (`DONE`):** the status file shows `done`, `verified`, or `blocked`. Stop.
   - **Exit 1 (`RESUME`):** work remains. Continue from the step after the phase in the status file. Investigation-task phases commonly end at `done` only after B.3 (unblock dependents) runs; a phase like `implementing` or `retrospective` still means there is more to do.
   - **Exit 2 (`UNKNOWN`):** re-read this SKILL.md and determine phase from git / GitHub state.

2. **Increment `autocompact_count`** in the status file.

3. **Re-read this SKILL.md** from the step named in the `RESUME` output (B.1, B.1.5, B.2, B.3, or Step 5).

4. **Do NOT emit `end_turn`** until the recovery check returns exit 0.

#### B.1 — File follow-up issues for every actionable finding

Do not just list recommendations — **create GitHub issues** for each concrete next step so the work is tracked and can be picked up by agents. For each follow-up:

- Write the issue body to `{worktree}/tmp/followup_N.txt`, then create it with `gh issue create --body-file` (write — stays on `gh` until MCP writes land).
- Reference the investigation as the parent: include `Parent: #<investigation-issue>` in the body.
- Label with appropriate area and type labels.
- Add `agent/ready` if the issue is fully specified and ready for work. If it requires a human decision first, note that in the body and omit `agent/ready`.

If the investigation reveals no actionable follow-ups (everything is working as expected), state that explicitly in the findings.

#### B.1.5 — Update contradicted source-file docstrings (MANDATORY)

An investigation frequently invalidates claims made in source-file docstrings, inline comments, or nearby `README.md` files. Those are the source-of-truth location for "how this module works" — standalone investigation docs are not discoverable when reading the code, so they go stale the moment the investigation lands. Update the docstrings as part of the investigation's resolution, not as a separate follow-up that may never be picked up.

**Required actions:**

1. **Grep the codebase for any claims contradicted by the investigation's findings.** Search source-file docstrings, inline comments, and nearby README files (including the per-fixture readmes under `tests/fixtures/`) for statements the investigation has shown to be wrong. For a scraper investigation, grep the scraper module's docstring and comments; for an ingestion investigation, grep the relevant `enrichment/` or `transcription/` modules. Example patterns to grep: specific regex claims, format descriptions, "case number format", "always", "never", field-availability claims, etc.
2. **List each stale location you find** in the investigation's findings document, with file path + line numbers + the incorrect text + the corrected text.
3. **Either update them in the same PR as the investigation, or file a follow-up issue** (via B.1) that lists the specific locations and the corrected text verbatim. Do not file a vague "update docstrings" issue — the follow-up must be concrete enough that an agent can pick it up and mechanically apply the edits.

**Do not treat "update the docstring" as non-actionable.** The existing B.1 rule ("file follow-up issues for every actionable finding") is not enough by itself, because docstring updates are easy to rationalize as already-covered-by-the-investigation-doc. They are not — the investigation doc lives in `docs/investigations/`, the source docstring lives next to the code, and readers of the code see only the latter.

**Concrete example — #2434:** The investigation in `docs/investigations/unknown-case-numbers-oc-riverside-2026-03.md` documented that OC PDF case-number availability varies per-department (not per-courthouse as `packages/scraper-framework/src/courts/ca/oc_tentatives.py` lines 18-21 claimed). The stale docstring sat in the repo for weeks producing misleading context for anyone reading the scraper. If B.1.5 had existed at the time, the investigation would have either corrected the docstring in the same PR or filed a concrete follow-up naming `oc_tentatives.py:18-21` with the corrected text — either path would have closed the loop. Instead, the docstring drift only surfaced weeks later via #2434.

#### B.2 — Post summary and close the issue

Post a summary comment on the investigation issue listing the findings and linking all follow-up issues created.

**Close the investigation issue after posting findings.** Investigation issues are fully resolved once findings are documented and follow-up issues are filed. Leaving them open causes duplicate agent work — another agent will pick up the still-open issue and re-investigate.

Write the close comment to `{worktree}/tmp/close_comment.txt`, then post via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478) and close (writes — stay on `gh`; `gh issue close --reason completed` has no MCP equivalent):
```
{worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/close_comment.txt
gh issue close <N> --repo judgemind/judgemind --reason completed
```

**Close with a standard summary comment.** Only leave an investigation issue open if it genuinely requires human judgment that cannot be captured in a follow-up issue.

**Release the label interlock (label-only flow, #2927).** Remove the `status/in-progress` label so the daemon can see the issue as terminal:

```
gh issue edit <N> --repo judgemind/judgemind --remove-label status/in-progress
```

Idempotent — exits 0 if the label is already absent.

#### B.3 — Unblock dependent issues

Unblock any issues that were waiting on this one by running `scripts/unblock-dependents.sh <your-issue>`. The script searches for open issues with `Blocked by #<your-issue>`, checks if all blockers are closed, and if so removes `status/blocked`, adds `agent/ready`, and cleans the `Blocked by` lines from the body. Use `--dry-run` to preview changes first.

Continue to Step 5.

---

### Path C: Large or ambiguous task

Break into sub-tasks first (see `docs/agent/issue-authoring.md` §Creating Sub-Tasks), label them `agent/ready`, then pick up the first sub-task (restart from Step 1).

If you only create sub-tasks and do not pick one up in this session, stop — the worktree will be cleaned up automatically by Claude Code (if spawned with `isolation: "worktree"`) or by the dispatcher. Skip Step 5 — no retrospective needed for task breakdown.

---

## Step 5 — Retrospective

Write status: `phase: retrospective`, `summary: Filing retro issues`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} retrospective`

**Cockpit milestone:** at the top of Step 5, emit the `retro` milestone:

```
scripts/dispatcher/progress.sh "$AGENT_ID" retro || true
```

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

- Write the issue body to `{worktree}/tmp/retro_N.txt`, then create it with `gh issue create --body-file` (write — stays on `gh` until MCP writes land).
- Label with `type/dx` (workflow improvements) or the appropriate area label (preventative measures).
- Set priority based on impact: `priority/p1` for things that would prevent production bugs or save significant agent time across many tasks; `priority/p2` for nice-to-have workflow improvements or one-off friction. **Never set `priority/p0`** — that priority is reserved for humans.
- Add `agent/ready` so the issue can be picked up autonomously.
- Keep issue scope tight — one improvement per issue. An agent should be able to pick it up and complete it in a single session.

If the task was trivial and there are genuinely no improvements to make, that's fine — skip filing. But default to filing. The bar is "would this save time or prevent bugs across future tasks?"

### 5d — Generate timing summary

Write status: `phase: done`, `summary: Task complete.`, and add a `final_phase: done` line so the dispatcher's post-mortem tooling can distinguish a properly-ended agent from one that emitted `end_turn` mid-workflow (see #2545).

**Generate the timing summary before the agent exits:**
```
python3 {worktree}/scripts/phase_timer.py summarize {worktree} {repo_root} <issue_number>
```
This writes `{worktree}/tmp/timing.json` with the full phase breakdown and appends a one-line summary to `{repo_root}/tmp/task-timings.jsonl`. The dispatcher can aggregate these across tasks to identify systemic bottlenecks.

**Final recovery self-check** (sanity gate before exit):
```
{worktree}/scripts/check-task-recovery.sh {worktree}
```
This must return exit 0 (`DONE`). If it returns 1 (`RESUME`), the status file was not updated to `done` — fix the status file and re-run. This guards against the #2545 failure mode where an agent writes a retrospective-looking final message while the status file still shows a non-terminal phase.

Worktree cleanup is handled automatically by Claude Code when the agent exits.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Multi-line Python always goes in a `.py` file**, never `-c '...'`.
- **No `run_in_background`.** All commands — CI watches, test suites, deploy watches, and reviewer invocations — must run in the foreground. Subagents are already background tasks from the parent's perspective. Further backgrounding causes `<task-notification>` messages to surface in the wrong context, leading to confusion and lost results.
- **Use `timeout: 1200000`** on Bash commands that may exceed 2 minutes: `pytest`, `gh run watch`, `pip install`, `npm install`, `npm run build`, `terraform apply`, `ruff check` on large codebases, `scripts/ecs-run-task.sh`, `scripts/ecs-run.sh --script`, `scripts/rebuild_db.sh`, and any data-processing script. <!-- scripts/ecs-run-task.sh stays for stream-logs propagation (not replaceable by MCP — see docs/agent/aws-to-mcp-migration.md) -->
- **After any context reset, run §A.0 / §B.0 recovery** — `{worktree}/scripts/check-task-recovery.sh {worktree}` is the authoritative "am I done?" check.
- **Prefer MCP for reads** (`mcp__github__get_issue`, `get_pull_request`, `list_issues`, `list_pull_requests`, `get_pull_request_status`). Keep `gh` for writes and for workflow-run operations. See `docs/agent/github-api-access.md`.
- **Cockpit milestones via `progress.sh`.** Call `scripts/dispatcher/progress.sh "$AGENT_ID" <milestone> || true` at every transition (`planning`, `ralph`, `summary`, `push_and_pr`, `awaiting_ci`, `fix_ci`, `merge`, `awaiting_deploy`, `verify`, `retro`). Best-effort, exits 0 unconditionally — see "Milestone progress reporting" in Step 0 and #3973.
- See CLAUDE.md §Unattended Operation Patterns for the full list.
