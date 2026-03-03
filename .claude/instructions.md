# Judgemind — Agent Instructions

Read this file before starting any task. It defines how you work in this codebase.

## Project Context

Judgemind is a free, open-source legal research platform replacing Trellis.law. Read the specs in `docs/specs/` for full context. The key things to know:

- **Tentative rulings are ephemeral.** If a scraper is down, data is permanently lost. Scraper reliability is the highest priority in the system.
- **Two data pipelines.** Model A captures California tentative rulings (ephemeral, high urgency). Model B extracts judge analytics from dockets/documents (all other states, persistent data, NLP-dependent).
- **Self-funded and free.** Every architecture decision must consider cost. Prefer fixed-cost over usage-based. Never assume unlimited budget.
- **API-first.** The web app is a client of the API. Every UI feature has an API endpoint.

## Starting a New Session

Do these steps in order at the start of every session. Do not wait for the user to tell you which worker number to use or which task to work on.

### Step 0 — Resolve the repo root

The shell may be invoked from inside a previous session's worktree. Always resolve the **main** repo root (not a worktree root) by stripping the worktree suffix from the git common dir:
```
git rev-parse --absolute-git-dir
```
If the output ends with `/.git` (e.g. `/home/user/myproject/.git`), strip `/.git` — that is **REPO_ROOT**.
If the output contains `/worktrees/` (e.g. `/home/user/myproject/.git/worktrees/worker-2`), strip from `/.git` onward — the prefix is **REPO_ROOT**.

Substitute this literal value everywhere `$REPO_ROOT` appears in these instructions. Never hardcode a path; always resolve it fresh each session.

### Step 1 — Claim your worker number

First prune stale worktree references (idempotent, safe to always run):
```
git -C $REPO_ROOT worktree prune
```
Then list active worktrees:
```
git -C $REPO_ROOT worktree list
```
Examine the output. Worker paths follow the pattern `worktrees/worker-N`. Pick the **lowest integer N ≥ 1 not already present** in the list. That is your worker number for this session.

Example: if the list shows `worktrees/worker-1` and `worktrees/worker-3`, claim **worker-2**.

### Step 2 — Create your worktree (always, no exceptions)

Every agent session must work in an isolated git worktree, never directly in the main repo. Run these sequentially (split to avoid `$()` prompts):
```
date +%Y%m%d-%H%M
git -C $REPO_ROOT worktree add \
    $REPO_ROOT/worktrees/worker-N -b worker-N/session-YYYYMMDD-HHMM
mkdir -p $REPO_ROOT/worktrees/worker-N/tmp
```
All subsequent work happens inside `$REPO_ROOT/worktrees/worker-N`.
Use `{worktree}/tmp/` for **all** temporary files (scripts, PR bodies, etc.) — this directory is gitignored and scoped to your worker, so there are no permission prompts and no collisions between workers.

When the session is done, remove the worktree:
```
git -C $REPO_ROOT worktree remove $REPO_ROOT/worktrees/worker-N
```

### Step 3 — Pick the next task

List open issues ready for an agent:
```
gh issue list --repo judgemind/judgemind \
    --label agent/ready --state open \
    --json number,title,assignees,labels \
    --limit 20
```

Pick the highest-priority unassigned issue. Priority order:
1. `priority/critical` → `priority/high` → `priority/medium` → `priority/low`
2. Within the same priority, prefer lower issue numbers (older issues).
3. Skip issues already assigned to another agent unless their worktree no longer exists in the `worktree list` output.

Then claim it:
```
gh issue edit <N> --repo judgemind/judgemind --add-assignee @me
gh issue comment <N> --repo judgemind/judgemind --body "Picking this up in worker-N."
```

### Step 4 — Work autonomously until the PR is green

- Read the issue thoroughly, including linked issues.
- Check `docs/specs/` for relevant guidance (product spec, architecture spec, investigation reports).
- Look at existing code for patterns. Be consistent with what's already there.
- If the issue is large or ambiguous, break it into sub-tasks first (see **Creating Sub-Tasks**), label them `agent/ready`, then pick up the first sub-task.
- If you need a decision from the maintainer, comment on the issue, label it `status/blocked`, and pick up a different task. Do not guess on ambiguous requirements.
- Implement, run pre-commit checks, commit, push, and open a PR.
- After pushing, watch CI and iterate until green (see **Git Workflow**).
- Do not ask the user for confirmation during any of these steps.

## Tool Use Rules

When operating as an agent in this repo:

- **Use dedicated tools for file operations** — never use Bash for `cat`, `ls`, `grep`, `find`. Use Read, Glob, and Grep instead.
- **Use Bash only for shell-only operations** — git, gh CLI, running tests, pip install, terraform, etc.
- **Bash commands prompt for confirmation** — this is intentional. Do not try to circumvent it. Work around prompts using the patterns in "Unattended Operation Patterns" below.
- `sudo` and `rm` always prompt; split commands to avoid triggering prompts unnecessarily.

## Accounts & Deployed Infrastructure

**GitHub:** org `judgemind/judgemind`, active account `judgeminder` (scopes: gist, project, read:org, repo, workflow).

**AWS:** account `155326049300`, user `admin`, region `us-west-2`. This is the Judgemind AWS account, not a personal account.

**Deployed resources (dev):**
- Terraform state: S3 bucket `judgemind-terraform-state`, DynamoDB lock table `judgemind-terraform-locks`
- Document archive: S3 bucket `judgemind-document-archive-dev`
- Assets: S3 bucket `judgemind-assets-dev`

## Code Standards

### Python (scrapers, NLP pipeline)
- Python 3.12+, using `.venv` in each package directory
- Run tests: `.venv/bin/pytest tests/ -v`
- Install deps: `.venv/bin/pip install -e ".[dev]"`
- Type hints on all function signatures
- pytest for testing; ruff for linting and formatting
- Dependencies managed via pyproject.toml
- Async where appropriate (httpx for HTTP, playwright for browser automation)

### TypeScript (API, frontend)
- Strict mode always
- Node.js 20+ for API; activate with `source ~/.nvm/nvm.sh && nvm install 20 --no-progress` (nvm is the version manager; `nvm install` is idempotent if already installed)
- Next.js 14+ for frontend
- ESLint + Prettier
- Jest or Vitest for testing

### General
- All code must have tests. Scrapers must have regression tests against archived pages in `tests/fixtures/`.
- Never hardcode secrets, API keys, credentials, or URLs to live court sites in source code. Use environment variables.
- Never commit large binary files. Use `.gitignore`.
- Write clear docstrings/comments for non-obvious logic. Court data has many edge cases — document them.

## Pre-Commit Checks (Required Before Every Commit)

Run these checks locally before committing to catch CI failures early.

**Python packages** (from the package directory, e.g. `packages/scraper-framework/`):
```
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
```
If either fails, fix the errors before committing. Auto-fix most lint issues with `ruff check --fix`.

**TypeScript packages** (from the package directory):
```
npm run lint
npm run typecheck
```

**Terraform** (from `infra/terraform/`):
```
terraform fmt -check -recursive
terraform validate
```

These mirror the exact CI steps. A commit that fails any of these checks will break CI.

## Git Workflow

- Commit messages follow conventional commits: `feat(scraping): implement OC PDF link scraper (#42)`
- Always branch from `main` (`feat/issue-{N}-short-description`), open a PR, wait for CI to pass, then request human review. Never merge your own PRs. Never push directly to `main`.
- **A PR is not done until it has no conflicts and CI is green.**
  - After opening a PR, check for merge conflicts: `gh pr view <N> --repo judgemind/judgemind --json mergeable,mergeStateStatus`
  - If `mergeable` is `CONFLICTING`, rebase onto main and resolve conflicts before doing anything else:
    ```
    git -C $REPO_ROOT/worktrees/worker-N fetch origin main
    git -C $REPO_ROOT/worktrees/worker-N rebase origin/main
    ```
    Resolve any conflicts, then `git rebase --continue`, then push with `--force-with-lease`.
  - After pushing, watch CI: `gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact`
  - If CI fails, diagnose the failure, fix it, push again, and repeat until green. Only then comment on the issue linking the PR and add the `status/review` label.

### Updating the PR Test Plan

After CI passes, **always** update the PR test plan checkboxes before considering a task done:

1. Fetch the current PR body:
   ```
   gh pr view <N> --repo judgemind/judgemind --json body -q .body
   ```
2. Check off each automated step that passed in CI (typecheck, lint, test). Leave manual steps unchecked until you run them.
3. For manual smoke tests (e.g. `npm run dev` + `curl /health`): the PR branch isn't on `main` yet, so create a temporary detached worktree from the branch:
   ```
   git -C $REPO_ROOT fetch origin <branch>
   git -C $REPO_ROOT worktree add $REPO_ROOT/worktrees/smoketest FETCH_HEAD
   # run smoke test...
   git -C $REPO_ROOT worktree remove $REPO_ROOT/worktrees/smoketest
   ```
4. Write the updated body to `{worktree}/tmp/pr_body.txt` and update the PR:
   ```
   gh pr edit <N> --repo judgemind/judgemind --body-file {worktree}/tmp/pr_body.txt
   ```

## Task Dependencies

Issues can be blocked on other issues. The system uses these conventions:

- Blocked issues carry `status/blocked` and do **not** have `agent/ready`. Agents skip them.
- A dependency is listed in the issue body as `Blocked by #N` (one line per blocker) under a `## Dependencies` heading.

### When you finish a task

Search for open issues that were waiting on yours:
```
gh issue list --repo judgemind/judgemind --state open \
    --search "Blocked by #<your-issue>" \
    --json number,title,body
```

For each result, re-read its body and check every `Blocked by #X` line. If **all** referenced issues are now closed:
1. Remove the `status/blocked` label.
2. Add the `agent/ready` label.
3. Remove the resolved `Blocked by #N` lines from the issue body (write updated body to a temp file and use `gh issue edit <N> --body-file`).

If any blocker is still open, leave the issue as blocked.

## Creating Sub-Tasks

If a task naturally breaks into 2+ independent pieces of work, create child issues:

- Each child issue must follow the issue template.
- Reference the parent: "Parent: #42" in the issue body.
- Sub-tasks should be self-contained — another agent should be able to pick one up independently.
- Label child issues appropriately (area, priority, type).
- Add `agent/ready` label if the sub-task is fully specified and ready for work.

## Investigation Tasks

Investigation tasks produce documentation, not code:

- Write findings directly in the issue body or as a markdown file in `docs/investigations/`.
- Always end with: recommended next steps, sub-tasks to create, and decisions that need human input.
- Be specific about what you found and what you couldn't determine.

## Scraper Development Rules

Key paths:
- Framework base classes: `packages/scraper-framework/src/framework/`
- California courts: `packages/scraper-framework/src/courts/ca/`

- **Never run production scraping from your development environment.** Production scraping runs only from deployed infrastructure. However, fetching a page or PDF from a live court site to understand its structure and create real test fixtures is required and expected — never build scrapers against fake or synthetic data.
- Every scraper must implement the base `Scraper` class from the framework.
- Every scraper must report health metrics after each run.
- Every captured document gets a SHA-256 content hash for version tracking.
- Raw content is always archived to object storage before any processing.
- Scraper configurations (URLs, selectors, schedules) are separate from scraper logic.

## Infrastructure Code

- Terraform for all AWS resources. No clicking in the console.
- Every resource must be in a module.
- Use variables for anything environment-specific (instance sizes, counts, etc.).
- Tag all resources with `project=judgemind` and `environment={dev|staging|production}`.
- Never commit AWS credentials or state files. Use remote state in S3.

### Pre-PR Checklist for Terraform Tasks

Before marking a Terraform PR ready, complete ALL of the following locally:

1. `terraform fmt -check -recursive infra/terraform/` — fix any formatting issues
2. Validate the **root** `infra/terraform/` config AND each environment: `terraform -chdir=infra/terraform init -backend=false && terraform validate`. The root config is the CI integration point — new required module variables must be added there too.
3. **Import any pre-existing resources** that were created outside Terraform before they were managed by code. Run `terraform import` for each, then verify with `terraform plan` that it shows no unexpected changes.
4. `terraform -chdir=infra/terraform/environments/<env> plan -no-color` (with real backend) — confirm the plan is clean or shows only expected changes (no destroys of existing resources).
5. `terraform apply` if the plan looks correct — verify `No changes` on a second `plan` afterward.
6. Check all test plan items in the PR body before requesting review.

## Unattended Operation Patterns

These patterns avoid permission prompts and allow the agent to run without interruption:

- **Git outside the working directory:** use `git -C /absolute/path <subcommand>` instead of `cd /path && git <subcommand>`. Compound commands with `cd` trigger a safety prompt.
- **Multi-line content for `gh` commands:** write to a temp file and use `--body-file {worktree}/tmp/file.txt`. Never use backticks or command substitution inside quoted strings passed to `gh`.
- **Multi-line Python scripts — ALWAYS use a file, no exceptions:** NEVER pass multi-line Python via `python3 -c "..."` or `-c '...'`. Even single-line-looking scripts with semicolons count. Always write the code to `{worktree}/tmp/script.py` first using the Write tool, then run `.venv/bin/python3 {worktree}/tmp/script.py`. This is a hard rule — inline `-c` code triggers a prompt every time and is never acceptable.
- **Tmp directory isolation:** always use `{worktree}/tmp/` for all temp files — it is gitignored, scoped to your worker, and requires no special permissions. Never use `/tmp/` directly; multiple workers share it and collide on common filenames.
- **Dynamic values in shell commands:** never embed `$(...)` command substitution inside a command that needs approval. Run the inner command first to get the value, then use the literal value in the next command. Example: run `date +%Y%m%d-%H%M` first, then use the printed date string in the subsequent command.
- **No quoted strings in compound shell commands:** a hook rejects commands that contain quoted characters (e.g. `"text"` or `'text'`) combined with `&&` or `;`. Instead of `cmd1 && echo "label" && cmd2`, make two separate tool calls — one per command.
- **Commit messages and multi-line strings:** use the Write tool to write content to a file, then reference it — never use `$(cat <<EOF ...)` or heredoc in a shell command. For commits: `git commit -F {worktree}/tmp/commit_msg.txt`. For PR bodies: `gh pr create --body-file {worktree}/tmp/pr_body.txt`.

## Session Triggers

- When the user says "let's go" or an equivalent phrase, immediately execute Steps 1–3 of "Starting a New Session" (claim worker number, create worktree, pick next task), then work autonomously without waiting for further instruction.

## Improving the Agent Workflow

When you encounter a permission prompt for a command that is **clearly safe and non-destructive** (read-only operations, local file writes, running tests, formatting tools, creating branches), and the prompt could be avoided with a better command pattern:

1. **Work around it immediately** using the patterns above or by splitting the command.
2. **File a GitHub issue** to track the improvement:
   - Title: `[DX] Agent workflow: avoid prompt for <description>`
   - Label: `type/dx` (create it if it doesn't exist)
   - Body: describe what triggered the prompt, the workaround used, and the specific line to add to the "Unattended Operation Patterns" section of `.claude/instructions.md`.

Do **not** file issues for prompts that exist for good reason — pushing to remote, opening PRs, merging, deploying, deleting branches, or any action that affects shared state. Those prompts are intentional.

## Memory and Instructions Updates

- Prefer updating `.claude/instructions.md` in the repo over writing to `~/.claude` project memory.
- Only use local `~/.claude` memory for things that cannot go in the repo (e.g. cross-repo or cross-project preferences).

## Things You Must Not Do

- Do not merge PRs.
- Do not deploy to production.
- Do not make architectural decisions that contradict the specs without flagging them as `type/decision` issues.
- Do not scrape live court websites during development.
- Do not store secrets in code, config files, or commit history.
- Do not add dependencies without justification in the PR description.
