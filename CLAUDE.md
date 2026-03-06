# Judgemind — Agent Instructions

**STOP. Read this entire file before doing anything else.** Do not explore the codebase, do not read other files, do not respond substantively to the user's request until you have read this file. This file defines your mandatory workflow — deviating from it is a bug.

## Critical Rules — Read First

These are the most frequently violated rules. **A PreToolUse hook enforces the shell rules automatically**, but you must internalize all of them. See `docs/preflight-checklist.md` for the complete machine-readable checklist.

### NEVER — Shell Commands
- **NEVER** use `$()` command substitution in any Bash command. Run the inner command as a separate tool call and use the literal result. For secrets, use `scripts/with-secret.sh` (see "Secrets Retrieval" below).
- **NEVER** use heredocs (`<<EOF`) in Bash commands. Write content to a file with the Write tool, then pass via `--body-file` or `-F`.
- **NEVER** use `python3 -c "..."` for inline scripts. Write to `{worktree}/tmp/script.py` and run the file.
- **NEVER** combine quoted strings with `&&` or `;`. Split into separate tool calls.
- **NEVER** prefix scripts with `bash` — run `scripts/start-worker.sh`, not `bash scripts/start-worker.sh`.

### NEVER — Workflow
- **NEVER** commit directly to `main` during autonomous task work. All `/task` work happens on worktree branches via PRs. (The user may direct you to commit to `main` during interactive sessions — that's fine.)
- **You MAY merge your own PRs** if the PR has passed the `/ralph` review loop (A.2) and CI is green. Use `gh pr merge <N> --repo judgemind/judgemind --squash --delete-branch`.
- **NEVER** deploy to production. Production deploys are human-only.
- **NEVER** skip pre-PR checks. Run lint, format, AND tests locally before pushing.
- **NEVER** share venvs between worktrees. Each worktree gets its own `.venv`.

### ALWAYS — Before Acting
- **ALWAYS** Read a file before Writing to it (the Write tool fails on existing files you haven't read).
- **ALWAYS** pull latest code (`git fetch origin main && git rebase origin/main`) before analyzing or modifying files.
- **ALWAYS** use `{worktree}/tmp/` for temp files, never `/tmp/`.
- **ALWAYS** use dedicated tools (Read, Glob, Grep) instead of Bash for file operations.
- **ALWAYS** watch CI to completion (`gh run watch`) before doing anything else after pushing.
- **ALWAYS** create a PR immediately after your first push to a branch.

## Project Context

Judgemind is a free, open-source legal research platform replacing Trellis.law. Read the specs in `docs/specs/` for full context. The key things to know:

- **Tentative rulings are ephemeral.** If a scraper is down, data is permanently lost. Scraper reliability is the highest priority in the system.
- **Two data pipelines.** Model A captures California tentative rulings (ephemeral, high urgency). Model B extracts judge analytics from dockets/documents (all other states, persistent data, NLP-dependent).
- **Self-funded and free.** Every architecture decision must consider cost. Prefer fixed-cost over usage-based. Never assume unlimited budget.
- **API-first.** The web app is a client of the API. Every UI feature has an API endpoint.

## Starting a New Session

Wait for the user's instruction before deciding what to do. Sessions may involve interactive work on the main branch, or autonomous task work via `/task`. Do not assume which mode — let the user direct you.

### Available Skills

- **`/task`** — Full autonomous pipeline: ensures a worktree exists (runs setup if needed), claims an issue, implements it, opens a PR, and requests review. Accepts `#N`, natural language filters, or no argument (picks highest priority). **This is the primary way to start autonomous work.**
- **`/ralph`** — Iterative work-review loop with fresh context each iteration. Spawns a worker subagent (TDD) and a reviewer subagent. Loops until the reviewer says SHIP or max iterations (5) reached. Called by `/task` automatically for testable code tasks. Can also be invoked manually after claiming an issue.
- **`/tdd`** — Test-driven implementation for code tasks (Python, TypeScript). Called by `/ralph` internally as the worker phase. Can also be invoked standalone for manual workflows. **Not for** Terraform, DB migrations, CI/CD, docs, or investigation tasks.

### Worktree setup (manual)

Run this from anywhere inside the repo (including from inside an existing worktree):

```
scripts/start-worker.sh
```

The script handles everything:

- Resolves the repo root regardless of where the shell started
- Ensures `main` is checked out and up to date
- Prunes stale worktree metadata
- Removes abandoned worktrees (branches merged into main, or session branches from a previous day)
- Claims the lowest available worker number, retrying automatically if another agent races to the same number
- Creates the worktree, configures git hooks, and creates the `tmp/` directory

It prints the worktree path on stdout, e.g. `/path/to/worktrees/worker-2`. **Record this value** — it is `{worktree}` for the rest of the session.

All subsequent work happens inside `{worktree}`. Use `{worktree}/tmp/` for **all** temporary files (scripts, PR bodies, etc.) — this directory is gitignored and scoped to your worker, so there are no permission prompts and no collisions between workers.

**Venv isolation:** each agent must create its own venv inside the worktree for every Python package it works in. Never use the venv from the main repo or another worktree — multiple agents on the same machine will stomp on each other if they share a venv. The `/task` skill sets up venvs in step A.1 after identifying which packages are needed. If setting up manually:

```
python3.12 -m venv {worktree}/packages/<pkg>/.venv
cd {worktree}/packages/<pkg> && .venv/bin/pip install -e ".[dev]" --quiet
```

Only install venvs for packages you actually work in during the session.

### Step 3 — Pick up a task

Use the `/task` skill to claim and work on an issue. Run it after completing Steps 0–2:

- `/task` — picks the next highest-priority unassigned `agent/ready` issue
- `/task #42` — works on a specific issue number
- `/task scrapers` / `/task next perf bug` / etc. — natural-language filter over the backlog

The skill works autonomously from issue selection through PR and review request. The PR workflow it follows is defined in the next section.

## PR Workflow (authoritative — applies to all task work)

**Single-issue rule:** each PR addresses exactly one issue. Do not combine unrelated changes in a single PR. If an issue is large or ambiguous, break it into sub-tasks first (see **Creating Sub-Tasks**), label them `agent/ready`, then pick up the first sub-task.

**All commits must be made on the worktree branch created in Step 2, never directly on `main`.** Every change goes through a PR — no direct pushes to `main`, ever.

Complete every substep in order. A task is not done until substep 4.10 is finished. Do not ask the user for confirmation during any of these steps.

#### 4.1 — Sync the worktree to latest main

Before touching any code, fetch and rebase onto the latest `origin/main`:

```
git -C {worktree} fetch origin main
git -C {worktree} rebase origin/main
```

This ensures your branch starts from the current tip of main, not from whenever the worktree was created.

#### 4.2 — Understand the problem

- Read the issue thoroughly, including linked issues.
- Check `docs/specs/` for relevant guidance (product spec, architecture spec, investigation reports).
- Look at existing code for patterns. Be consistent with what's already there.
- If you need a decision from the maintainer, comment on the issue, label it `status/blocked`, and pick up a different task. Do not guess on ambiguous requirements.

#### 4.3 — Implement and verify locally

- **For testable code tasks** (Python, TypeScript): use the `/ralph` loop — iterative work-then-review with fresh context each iteration. See `.claude/skills/ralph/SKILL.md`. `/ralph` handles implementation (TDD), pre-PR checks, and cross-perspective review internally.
- **For non-testable tasks** (Terraform, DB migrations, CI/CD, docs): implement directly, then run all applicable pre-PR checks (see "Pre-PR Checks" section — lint, format, AND tests) and review your own diff before continuing.
- Fix any failures before proceeding. Do not push code that fails local checks.

#### 4.4 — Push, open a PR, and immediately watch CI

Push the branch, open a PR, then start watching CI **in the same step** — do not do anything else until CI completes:

```
git push -u origin <branch>
gh pr create --repo judgemind/judgemind ...
gh run list --repo judgemind/judgemind --branch <branch> --limit 1 --json databaseId -q '.[0].databaseId'
gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact
```

**Never leave the CI watch step unfinished.** Do not remove the worktree, do not update the PR body, do not do anything else until `gh run watch` exits. If CI is green, continue to 4.5. If CI fails, go to 4.7.

#### 4.5 — Verify no merge conflicts

- Check for merge conflicts:
  ```
  gh pr view <N> --repo judgemind/judgemind --json mergeable,mergeStateStatus
  ```
- If `mergeable` is `CONFLICTING`, rebase onto main and resolve conflicts:
  ```
  git -C $REPO_ROOT/worktrees/worker-N fetch origin main
  git -C $REPO_ROOT/worktrees/worker-N rebase origin/main
  ```
  Resolve any conflicts, then `git rebase --continue`, then push with `--force-with-lease`, then return to 4.4 to watch CI again.

#### 4.6 — CI is green — confirm before proceeding

After `gh run watch` exits cleanly, verify all required checks passed:
```
gh pr view <N> --repo judgemind/judgemind --json statusCheckRollup
```
All checks must show `SUCCESS` or `SKIPPED`. Any `FAILURE` goes to 4.7.

#### 4.7 — Fix CI failures (repeat until green)

- If CI fails, diagnose the failure, fix it locally, push again, and return to 4.4.
- Repeat the 4.4 -> 4.5 -> 4.6 -> 4.7 loop until CI is green. **The worktree must not be removed until this loop exits cleanly.**

#### 4.8 — Update the PR test plan

- Fetch the current PR body:
  ```
  gh pr view <N> --repo judgemind/judgemind --json body -q .body
  ```
- Check off each automated step that passed in CI (typecheck, lint, test). Leave manual steps unchecked until you run them.
- For manual smoke tests (e.g. `npm run dev` + `curl /health`), create a temporary worktree:
  ```
  git -C $REPO_ROOT fetch origin <branch>
  git -C $REPO_ROOT worktree add $REPO_ROOT/worktrees/smoketest FETCH_HEAD
  # run smoke test...
  git -C $REPO_ROOT worktree remove $REPO_ROOT/worktrees/smoketest
  ```
- Write the updated body to `{worktree}/tmp/pr_body.txt` and update:
  ```
  gh pr edit <N> --repo judgemind/judgemind --body-file {worktree}/tmp/pr_body.txt
  ```

#### 4.9 — Link the issue and request review

- Comment on the issue linking the PR.
- Add the `status/review` label to the issue.

#### 4.10 — Remove your worktree

The branch must stay (it backs the open PR), but the worktree directory is no longer needed. Run:

```
scripts/end-worker.sh {worktree}
```

This is the last step of every task. A task is not complete until the worktree is removed.

## Tool Use Rules

When operating as an agent in this repo:

- **Use dedicated tools for file operations** — never use Bash for `cat`, `ls`, `grep`, `find`. Use Read, Glob, and Grep instead.
- **Always Read before Write** — if a file might already exist (e.g. any path in `tmp/`), use the Read tool first before writing, even if you don't need the existing content. The Write tool requires this for existing files and will fail otherwise.
- **Use Bash only for shell-only operations** — git, gh CLI, running tests, pip install, terraform, etc.
- **Bash commands prompt for confirmation** — this is intentional. Do not try to circumvent it. Work around prompts using the patterns in "Unattended Operation Patterns" below.
- `sudo` and `rm` always prompt; split commands to avoid triggering prompts unnecessarily.

## Accounts & Deployed Infrastructure

**GitHub:** org `judgemind/judgemind`, active account `judgemind-agent` (scopes: gist, project, read:org, repo, workflow).

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

## Pre-PR Checks (MANDATORY — No Exceptions)

**Every agent (including subagents) MUST run ALL applicable checks locally and verify they pass BEFORE pushing a branch or creating a PR.** Skipping these wastes CI minutes and blocks merges. A PR that fails CI is not done — it's broken.

> **Note:** The `.githooks/pre-push` hook automatically runs ruff, eslint, and terraform fmt on changed packages before every push. If you configured `core.hooksPath` during worktree setup (Step 2), common lint and format issues will be caught automatically and the push will be blocked until they are fixed. This does **not** replace running the full check suite (including tests) — it only gates on fast, deterministic checks.
>
> The pre-push hook also checks whether a PR exists for the branch being pushed. For new branches it prints a reminder to create a PR; for existing branches with no PR it prints a warning. **Always create a PR immediately after your first push to a branch** — do not push and move on without one.

Run checks from each package directory you modified. If any check fails, fix it before pushing.

**Python packages** (from the package directory, e.g. `packages/scraper-framework/`):

```
.venv/bin/ruff check src/ tests/           # Lint (rules: E, F, I, N, UP, ANN)
.venv/bin/ruff format --check src/ tests/   # Format check
.venv/bin/pytest tests/ -v --tb=short       # Tests (scraper-framework also supports -n auto)
```

If lint fails, auto-fix with `.venv/bin/ruff check --fix src/ tests/` then `.venv/bin/ruff format src/ tests/`.

Common ruff pitfalls that agents keep hitting:

- **I001** (unsorted imports): `ruff check --fix` resolves this. Always run it.
- **F401** (unused imports): Remove any import you don't actually use.
- **UP017** (datetime.UTC): Use `datetime.now(datetime.UTC)`, not `datetime.now(timezone.utc)`.
- **Format != Lint**: `ruff check` and `ruff format` are **separate commands**. You must run BOTH.

**TypeScript packages** (from the package directory):

```
npm run lint                                # ESLint
npm run typecheck                           # tsc --noEmit
npm test                                    # Vitest
```

For `packages/web/`, also run `npm run build` to catch build errors.

**Terraform** (from `infra/terraform/`):

```
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

### Subagent Responsibilities

#### Worktree Isolation (mandatory for branch work)

When spawning subagents that will work on **different branches** (e.g. fixing multiple PRs in parallel, implementing features on separate branches), the parent agent **MUST** pass `isolation: "worktree"` in the Agent tool call. Without this, subagents share the parent's working directory and will cause branch checkout conflicts, stash races, and leave the parent on the wrong branch.

Rules:

- **Never run `git checkout` or `git switch` in the parent's working directory from a subagent.** This changes the branch for the parent and every other subagent sharing that directory.
- If the `isolation: "worktree"` parameter is not available (e.g. the subagent is doing non-git work like API calls or documentation generation), the subagent must **not** check out a different branch in the shared working directory.
- A subagent that needs to work on a specific branch but is not already worktree-isolated must create its own worktree before doing any branch-specific work:
  ```
  git -C $REPO_ROOT worktree add $REPO_ROOT/worktrees/sub-<task> <branch>
  ```
  and clean it up when finished:
  ```
  git -C $REPO_ROOT worktree remove $REPO_ROOT/worktrees/sub-<task>
  ```

#### Pre-PR Checks

When you spawn a subagent to implement a feature or fix, the subagent MUST:

1. Install dependencies and set up the venv/node_modules.
2. Run ALL lint, format, and test commands listed above for every package it touched.
3. Fix any failures before committing.
4. Only push after all local checks pass.

Do NOT rely on CI to catch issues that local checks would have caught. If a subagent creates a PR that fails CI on checks it could have run locally, that is a bug in the subagent's workflow.

## Git Workflow

- Commit messages follow conventional commits: `feat(scraping): implement OC PDF link scraper (#42)`
- Always work on the worktree branch created in Step 2. Open a PR, wait for CI to pass. You may merge your own PRs after the ralph review loop (A.2) and CI are green. Never push directly to `main`.
- **A PR is not done until it has no conflicts and CI is green.** Follow the complete post-push checklist in the PR Workflow section (substeps 4.4–4.8) — do not skip any step.

## Task Dependencies

Issues can be blocked on other issues. The system uses these conventions:

- Blocked issues carry `status/blocked` and do **not** have `agent/ready`. Agents skip them.
- A dependency is listed in the issue body as `Blocked by #N` (one line per blocker) under a `## Dependencies` heading.

### When you finish a task

**Implementation tasks (PRs):** dependent issues are unblocked automatically by the `unblock-issues` CI workflow when the PR merges. No manual action needed — but the PR body must include `Closes #N` so the workflow knows which issue was resolved.

**Non-PR completions (investigations, sub-task breakdowns):** manually unblock dependent issues. Search for open issues waiting on yours:

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
- Be specific about what you found and what you couldn't determine.
- Always end with: decisions that need human input (if any).
- **Always file follow-up issues** for every actionable finding. Don't just recommend next steps — create the issues so the work is tracked and can be picked up. Label them `agent/ready` if fully specified, or flag them for human input if a decision is needed first. Reference the investigation issue as the parent (see §Creating Sub-Tasks).

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
- **Run scripts directly, never with a `bash` prefix:** use `scripts/start-worker.sh`, not `bash scripts/start-worker.sh`. The `Bash(scripts/*)` permission pattern only matches commands that start with `scripts/`; prepending `bash` breaks the match and triggers a prompt.
- **Multi-line content for `gh` commands:** write to a temp file and use `--body-file {worktree}/tmp/file.txt`. Never use backticks or command substitution inside quoted strings passed to `gh`.
- **Multi-line Python scripts — ALWAYS use a file, no exceptions:** NEVER pass multi-line Python via `python3 -c "..."` or `-c '...'`. Even single-line-looking scripts with semicolons count. Always write the code to `{worktree}/tmp/script.py` first using the Write tool, then run `.venv/bin/python3 {worktree}/tmp/script.py`. This is a hard rule — inline `-c` code triggers a prompt every time and is never acceptable.
- **Tmp directory isolation:** always use `{worktree}/tmp/` for all temp files — it is gitignored, scoped to your worker, and requires no special permissions. Never use `/tmp/` directly; multiple workers share it and collide on common filenames.
- **Dollar-paren `$()` is NEVER allowed in any Bash command — no exceptions.** Command substitution always triggers a prompt. This includes `--body` with cat, heredocs embedded in commands, `git commit -m` with cat, and any other form. If you need a dynamic value, run the command that produces it first as a separate tool call, then use the literal string in the next command. **This also applies to commit messages and strings passed to `-m`: if the message text contains the literal characters `$` followed by `(`, the hook fires. Write the message to a file and use `-F` instead.**
- **Secrets retrieval — use `scripts/with-secret.sh`:** Never run `aws secretsmanager get-secret-value` as a standalone command — the secret value will appear in chat output. Never write secrets to disk. Instead, use the wrapper script to inject secrets as env vars and run a command in one step:
  ```
  scripts/with-secret.sh -e CF_API_TOKEN=judgemind/cloudflare/api-token -- terraform apply
  scripts/with-secret.sh -e DB_USER=judgemind/dev/db/connection:.username -e DB_PASS=judgemind/dev/db/connection:.password -- ./run.sh
  ```
  The `-e VAR=secret-id` form uses the raw SecretString. The `-e VAR=secret-id:.field` form extracts a JSON key. Multiple `-e` flags can be chained.
- **No quoted strings in compound shell commands:** a hook rejects commands that contain quoted characters (e.g. `"text"` or `'text'`) combined with `&&` or `;`. Instead of `cmd1 && echo "label" && cmd2`, make two separate tool calls — one per command.
- **Multi-line content for `gh` or `git` commands:** always write the content to a file first using the Write tool, then pass it with `--body-file` or `-F`. Never use heredocs or `$()` in shell commands. For commits: `git commit -F {worktree}/tmp/commit_msg.txt`. For PR/issue bodies: `gh issue create --body-file {worktree}/tmp/body.txt`.

## Session Triggers

- When the user asks to pick up work (e.g. "let's go", "start", "pick up a task"), invoke `/task`. It handles everything — worktree setup (if needed), issue selection, implementation, PR, and review request. Proceed autonomously without waiting for further instruction.
- `/task` automatically uses the `/tdd` TDD workflow for testable code tasks (Python, TypeScript). For infrastructure, migrations, and docs it implements directly.
- If the user gives a specific instruction without invoking `/task`, work on it directly. Not every session requires a worktree or a GitHub issue.

## Improving the Agent Workflow

When you encounter a permission prompt for a command that is **clearly safe and non-destructive** (read-only operations, local file writes, running tests, formatting tools, creating branches), and the prompt could be avoided with a better command pattern:

1. **Work around it immediately** using the patterns above or by splitting the command.
2. **File a GitHub issue** to track the improvement:
   - Title: `[DX] Agent workflow: avoid prompt for <description>`
   - Label: `type/dx` (create it if it doesn't exist)
   - Body: describe what triggered the prompt, the workaround used, and the specific line to add to the "Unattended Operation Patterns" section of `CLAUDE.md`.

Do **not** file issues for prompts that exist for good reason — pushing to remote, opening PRs, merging, deploying, deleting branches, or any action that affects shared state. Those prompts are intentional.

## Memory and Instructions Updates

- Prefer updating `CLAUDE.md` in the repo root over writing to `~/.claude` project memory.
- Only use local `~/.claude` memory for things that cannot go in the repo (e.g. cross-repo or cross-project preferences).

## Things You Must Not Do

- Do not merge PRs unless they have passed the ralph review loop (A.2) and CI is green.
- Do not deploy to production.
- Do not make architectural decisions that contradict the specs without flagging them as `type/decision` issues.
- Do not scrape live court websites during development.
- Do not store secrets in code, config files, or commit history.
- Do not add dependencies without justification in the PR description.
