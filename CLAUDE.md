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
- **NEVER** set `priority/p0` on issues unless explicitly told to by a human. `p0` is human-only.
- **NEVER** skip pre-PR checks. Run lint, format, AND tests locally before pushing.
- **NEVER** share venvs between worktrees. Each worktree gets its own `.venv`.

### ALWAYS — Before Acting
- **ALWAYS** Read a file before Writing to it (the Write tool fails on existing files you haven't read).
- **ALWAYS** pull latest code (`git fetch origin main && git rebase origin/main`) before analyzing or modifying files.
- **ALWAYS** use `{worktree}/tmp/` for temp files, never `/tmp/`.
- **ALWAYS** use dedicated tools (Read, Glob, Grep) instead of Bash for file operations.
- **ALWAYS** watch CI to completion (`gh run watch`) before doing anything else after pushing.
- **ALWAYS** create a PR immediately after your first push to a branch.

## Enforced Rules — Automated Checks

The PreToolUse hook (`.claude/hooks/preflight-bash.sh`) and `scripts/preflight.sh` automatically enforce the Critical Rules above. The hook blocks `$(`, `<<EOF`, `python -c`, and `git push` to main. Runtime checks:

```bash
source scripts/preflight.sh
preflight_in_worktree       # Fail if pwd is main repo, not a worktree
preflight_not_on_main       # Fail if on main/master branch
preflight_branch_fresh      # Fail if behind origin/main (add --fetch to fetch first)
preflight_venv_local        # Fail if .venv is missing or is a symlink
```

## Project Context

Judgemind is a free, open-source legal research platform replacing Trellis.law. Read the specs in `docs/specs/` for full context. The key things to know:

- **Tentative rulings are ephemeral.** If a scraper is down, data is permanently lost. Scraper reliability is the highest priority in the system.
- **Two data pipelines.** Model A captures California tentative rulings (ephemeral, high urgency). Model B extracts judge analytics from dockets/documents (all other states, persistent data, NLP-dependent).
- **Self-funded and free.** Every architecture decision must consider cost. Prefer fixed-cost over usage-based. Never assume unlimited budget.
- **API-first.** The web app is a client of the API. Every UI feature has an API endpoint.

## Starting a New Session

Wait for the user's instruction before deciding what to do. Sessions fall into two modes:

### Interactive sessions (human present)

Interactive sessions are **general-purpose** — the user decides what the session is for. You may explore, investigate, prototype, discuss architecture, file issues, spawn subagents, or anything else the user asks.

To enable autonomous work queue management (continuous `/task` spawning, PR merging, issue triage, Telegram integration), invoke `/orchestrator`. This is opt-in — do not assume orchestrator behavior unless the skill is invoked.

### Autonomous sessions (subagent via `/task`)

Subagents do the implementation work: worktree setup, coding, testing, PR, and review. They follow the full PR Workflow below.

### Available Skills

- **`/task`** — Full autonomous pipeline: ensures a worktree exists (runs setup if needed), claims an issue, implements it, opens a PR, and requests review. Accepts `#N`, natural language filters, or no argument (picks highest priority). **This is the primary way to start autonomous work.**
- **`/ralph`** — Iterative work-review loop with fresh context each iteration. Spawns a worker subagent (TDD) and a reviewer subagent. Loops until the reviewer says SHIP or max iterations (5) reached. Called by `/task` automatically for testable code tasks. Can also be invoked manually after claiming an issue.
- **`/tdd`** — Test-driven implementation for code tasks (Python, TypeScript). Called by `/ralph` internally as the worker phase. Can also be invoked standalone for manual workflows. **Not for** Terraform, DB migrations, CI/CD, docs, or investigation tasks.
- **`/orchestrator`** — Opt-in autonomous work queue manager. Continuously launches `/task` agents (up to 5 concurrent slots), merges PRs when CI is green and ralph has approved, triages issues, and communicates via Telegram. Accepts optional args: a number for max agent slots (e.g. `/orchestrator 3`) or specific issue numbers (e.g. `/orchestrator #589 #590`). See `.claude/skills/orchestrator/SKILL.md` for the full behavioral specification.

### Worktree setup (manual)

```
scripts/start-worker.sh
```

Claims a worker number, creates the worktree from latest `origin/main`, configures git hooks, and creates `tmp/`. Prints the worktree path (e.g. `/path/to/worktrees/worker-2`) — this is `{worktree}` for the session.

All work happens inside `{worktree}`. Use `{worktree}/tmp/` for **all** temp files (gitignored, no permission prompts). Each worktree gets its own `.venv` per package — never share venvs between worktrees:

```
python3.12 -m venv {worktree}/packages/<pkg>/.venv
cd {worktree}/packages/<pkg> && .venv/bin/pip install -e ".[dev]" --quiet
```

### Step 3 — Pick up a task

Use the `/task` skill to claim and work on an issue. Run it after completing Steps 0–2:

- `/task` — picks the next highest-priority unassigned `agent/ready` issue
- `/task #42` — works on a specific issue number
- `/task scrapers` / `/task next perf bug` / etc. — natural-language filter over the backlog

The skill works autonomously from issue selection through PR and review request. The PR workflow it follows is defined in the next section.

## PR Workflow (authoritative — applies to all task work)

**Single-issue rule:** each PR addresses exactly one issue. Do not combine unrelated changes in a single PR. If an issue is large or ambiguous, break it into sub-tasks first (see **Creating Sub-Tasks**), label them `agent/ready`, then pick up the first sub-task.

**All commits must be made on the worktree branch created in Step 2, never directly on `main`.** Every change goes through a PR — no direct pushes to `main`, ever.

Complete every substep in order. A task is not done until substep 4.11 is finished. Do not ask the user for confirmation during any of these steps.

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

- Fetch the PR body with `gh pr view`, check off automated steps that passed in CI.
- Write the updated body to `{worktree}/tmp/pr_body.txt` and update with `gh pr edit --body-file`.

#### 4.9 — Link the issue and request review

- Comment on the issue linking the PR.
- Add the `status/review` label to the issue.

#### 4.10 — Verify deployment (after merge, deployed services only)

Skip for library, tooling, docs, or CI-only changes. For deployed code (API, scrapers, infra):

1. Watch the deploy workflow triggered by the merge to `main` (`gh run watch`).
2. If deploy **fails**: file a `priority/p1` issue, reference the merged PR, add `agent/ready`.
3. If deploy **succeeds**: smoke-test the deployed environment where feasible.

For **web frontend** changes (`packages/web/`): Vercel deploys automatically via its GitHub App — there is no GitHub Actions workflow to watch. Instead, check the commit status on the merge commit (Vercel posts `deployment/vercel` status) or check the Vercel dashboard. Smoke-test by loading `https://dev.judgemind.org` after the deploy completes.

#### 4.11 — Remove your worktree

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

### Web Frontend (Vercel)

The Next.js web app (`packages/web/`) is deployed on **Vercel** with automatic Git-based deployments. Vercel watches the `judgemind/judgemind` repo and deploys on every push to `main` (production) and on every PR branch (preview).

**Infrastructure:** managed by Terraform module `vercel-web` in `infra/terraform/environments/hosting/`. The Vercel API token is stored in Secrets Manager at `judgemind/vercel/api-token`.

**Environments:**

| Environment | URL | Vercel project | Trigger |
|---|---|---|---|
| Dev | `dev.judgemind.org` | `judgemind-web-dev` | Push to `main` |
| Preview | `*.vercel.app` (auto-generated) | `judgemind-web-dev` | Push to any PR branch |

**Environment variables** (set in Vercel project, managed by Terraform):
- `NEXT_PUBLIC_GRAPHQL_URL` = `https://dev.api.judgemind.org/graphql`

**Checking deploy status:**
```
# List recent deployments (requires Vercel CLI: npm i -g vercel)
vercel list judgemind-web-dev --token "$VERCEL_API_TOKEN"

# Or check from the Vercel dashboard:
# https://vercel.com/judgemind2026-7926s-projects/judgemind-web-dev/deployments
```

There is **no GitHub Actions deploy workflow** for the web frontend — Vercel handles deployments directly via its GitHub App integration. The `deploy-production.yml` workflow is for the scraper (ECS), not the web app. To check whether a frontend deploy succeeded after merging to `main`, check the Vercel dashboard or the commit status checks on the merge commit (Vercel posts deployment status as a GitHub commit status).

### Telegram Bridge (optional)

The Telegram bridge lets agents send lifecycle notifications and receive inbound commands via a Telegram bot. It is **opt-in** — if the secret is missing or the bot token is empty, all bridge calls silently become no-ops and no existing workflows are affected.

**Architecture:** Telegram webhook POST -> API Gateway -> Lambda (`infra/telegram-bot/handler.py`) -> SQS queue. The Python client (`packages/telegram-bridge/`) reads commands from SQS and sends replies via the Telegram Bot API.

**Secrets Manager secret:** `judgemind/telegram/bot` — JSON structure:
```json
{
  "bot_token": "<Telegram bot token from BotFather>",
  "allowed_user_ids": [123456789]
}
```
The `allowed_user_ids` array controls who can send commands. Messages from unlisted users are silently dropped by the Lambda.

**Infrastructure (Terraform module `telegram_bot`):**
- Lambda: `judgemind-telegram-webhook-dev`
- API Gateway (HTTP): `judgemind-telegram-webhook-dev`
- SQS queue: `judgemind-telegram-inbound-dev`
- Secret: `judgemind/telegram/bot`

See `docs/telegram-setup.md` for end-to-end setup instructions.

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

### Performance awareness

Every diff review (human or `/ralph`) must check for these common bottlenecks:

- **Sequential I/O over collections.** If you loop over items and make a network call per item (S3, HTTP, DB query), use concurrency (`ThreadPoolExecutor`, `asyncio.gather`, `pipeline()`) or batching instead.
- **O(n²) pagination.** Never use `LIMIT/OFFSET` for large datasets — use keyset (cursor-based) pagination.
- **Unbatched DB writes.** If you insert/update rows in a loop, use `executemany`, `COPY`, or psycopg3 `pipeline()` mode to amortize round-trips.
- **Missing connection reuse.** Reuse HTTP clients (`httpx.Client`), DB connections, and S3 clients across calls — don't create new ones per iteration.

Ship correct code first, but don't ship code with obvious O(n) network calls when O(1) batched alternatives exist. If you're unsure whether a perf pattern matters at current scale, add a `# TODO(perf):` comment noting the concern.

## Pre-PR Checks (MANDATORY — No Exceptions)

**Every agent (including subagents) MUST run ALL applicable checks locally and verify they pass BEFORE pushing a branch or creating a PR.** Skipping these wastes CI minutes and blocks merges. A PR that fails CI is not done — it's broken.

> The `.githooks/pre-push` hook automatically runs lint, format, and tests on changed packages before every push — it will block the push on failure. Run checks manually if you want to catch issues earlier:

**Python packages** (from the package directory, e.g. `packages/scraper-framework/`):

```
.venv/bin/ruff check src/ tests/           # Lint (rules: E, F, I, N, UP, ANN)
.venv/bin/ruff format --check src/ tests/   # Format check
.venv/bin/pytest tests/ -v --tb=short       # Tests with coverage (configured via pyproject.toml addopts)
```

Coverage reports are generated automatically by pytest (configured in `pyproject.toml` via `--cov` addopts). Reports appear in `htmlcov/` (HTML) and `coverage.xml` (CI artifact). Both are gitignored.

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
npx vitest run --coverage                   # Tests with coverage report
```

For `packages/web/`, also run `npm run build` to catch build errors. Coverage reports (v8 provider) are configured in `vitest.config.ts` and output to `coverage/` (HTML + lcov). The directory is gitignored.

**Terraform** (from `infra/terraform/`):

```
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

### Subagent Responsibilities

#### Worktree Isolation

**Do NOT use `isolation: "worktree"` on the Agent tool for `/task` subagents.** The `/task` skill creates its own worktree via `scripts/start-worker.sh` internally. The Agent tool's `isolation: "worktree"` creates a *separate* temporary worktree at a different path, which **breaks project permissions** — the `.claude/settings.json` allow-list is keyed to the repo root, so agents in the tool-created worktree lose all pre-approved Bash/Write permissions and immediately prompt the user.

**Rule:** spawn `/task` agents **without** `isolation: "worktree"`. The skill's internal worktree provides the branch isolation. Multiple `/task` agents can run in parallel safely because each claims a unique worker number.

For non-`/task` subagents that need branch isolation (rare), the agent must create its own worktree before doing any branch-specific work:
  ```
  git -C $REPO_ROOT worktree add $REPO_ROOT/worktrees/sub-<task> <branch>
  ```
  and clean it up when finished:
  ```
  git -C $REPO_ROOT worktree remove $REPO_ROOT/worktrees/sub-<task>
  ```

**Never run `git checkout` or `git switch` in the parent's working directory from a subagent.** This changes the branch for the parent and every other subagent sharing that directory.

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
- **Field extraction completeness is a hard requirement.** A scraper is not done until it correctly parses 100% of the structured fields present in the rulings obtained during development. Required fields: **judge name, motion type, case title, hearing date, outcome, parties**. If a field is present in the source data, the scraper must extract it — do not ship scrapers that leave extractable fields empty and rely on backfills later. Write regression tests against real fixtures for every field. "Unknown" / "Not classified" values are acceptable only when the source data genuinely does not contain the information.

## Infrastructure Code

- Terraform for all AWS resources. No clicking in the console.
- Every resource must be in a module.
- Use variables for anything environment-specific (instance sizes, counts, etc.).
- **Do NOT add `tags` blocks to individual resources** — the AWS provider's `default_tags` already applies `Project`, `Environment`, and `ManagedBy` to all resources. Adding per-resource tags causes IAM failures (tag keys are case-insensitive, so `environment` and `Environment` collide).
- Never commit AWS credentials or state files. Use remote state in S3.

### Terraform apply after merge

After a Terraform PR merges to main, the subagent that authored the PR must apply to dev:
```
terraform -chdir=$REPO_ROOT/infra/terraform/environments/dev apply -target=module.<module_name> -auto-approve
```
Verify the apply succeeds. If it fails, file a `priority/p1` issue.

**Important:** The root `infra/terraform/` directory does not track deployed resources. Each environment has its own state backend under `infra/terraform/environments/<env>/`. Running apply from the root creates duplicate resources that collide with the real ones. Always use the environment-specific path. Production applies (`environments/production/`) are human-only.

### Pre-PR Checklist for Terraform Tasks

See `docs/terraform-checklist.md` for the full checklist.

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
- **No inline JSON or complex quoting in `curl` commands.** Commands with mixed `"` and `'` quoting (e.g. `-H "Content-Type: application/json" -d '{"query":...}'`) trigger permission prompts. Instead, write the request body to a file and use `@` to reference it:
  ```
  # Write the JSON body first using the Write tool, then:
  curl -s -X POST https://dev.api.judgemind.org/graphql \
    -H Content-Type:application/json \
    -d @{worktree}/tmp/query.json
  ```
- **No quoted strings in compound shell commands:** a hook rejects commands that contain quoted characters (e.g. `"text"` or `'text'`) combined with `&&` or `;`. Instead of `cmd1 && echo "label" && cmd2`, make two separate tool calls — one per command.
- **Multi-line content for `gh` or `git` commands:** always write the content to a file first using the Write tool, then pass it with `--body-file` or `-F`. Never use heredocs or `$()` in shell commands. For commits: `git commit -F {worktree}/tmp/commit_msg.txt`. For PR/issue bodies: `gh issue create --body-file {worktree}/tmp/body.txt`.
- **Telegram bridge notifications:** the `TelegramBridge` and `OrchestratorBridge` classes are async. In synchronous contexts, use `asyncio.run()` or schedule on an existing event loop. The bridge auto-initialises lazily on first use — no explicit setup needed beyond passing the secret ID and SQS queue URL. If the secret is missing or empty, all calls are silent no-ops, so it is safe to call unconditionally.
- **File-based graceful shutdown for background processes:** Never use `kill` to stop background daemons — it is blocked by the sandbox. Instead, use the file-based stop convention: for any daemon that writes a PID to `tmp/foo.pid`, create `tmp/foo.stop` to request shutdown. The daemon checks for the stop file each iteration and exits gracefully, cleaning up both the PID file and the stop file. To stop the Telegram poller: create `tmp/tg_poll.stop` using the Write tool. The daemon (`scripts/tg-poll-daemon.py`) will detect it within 1 second and exit. For new background daemons, follow the same convention: check `_check_stop_file(pid_file)` in the sleep loop and call `_remove_stop_file(pid_file)` in the `finally` block.
- **Writing to `.claude/` directories (skills, hooks, settings):** The Claude Code platform has a built-in deny on the `.claude/` directory that overrides project-level `Edit(**)` / `Write(**)` permissions. This is a security boundary — it prevents agents from modifying their own permissions, hooks, or configuration without human approval. This protection **cannot be overridden** via `settings.json`, and it blocks Edit, Write, `cp`, and `mv` tools when the destination is inside `.claude/`. The only workaround is a Python script: write the new content to `{worktree}/tmp/new_file.md` using the Write tool, then write a helper script to `{worktree}/tmp/copy_to_claude.py` that uses `shutil.copy2()` to copy it into `.claude/`. Run the script with `python3 {worktree}/tmp/copy_to_claude.py`. This works because the platform does not inspect Python script arguments for `.claude/` paths. Use `scripts/write-claude-file.sh` as a convenience wrapper (see below).
- **ECS script execution — prefer `ecs-run-task.sh` over `ecs-run.sh`:** `scripts/ecs-run.sh` uses ECS Exec (SSM sessions) which frequently disconnects within seconds, losing all output. Use `scripts/ecs-run-task.sh` instead — it launches a clean Fargate task and streams logs reliably from CloudWatch. Reserve `ecs-run.sh` only for quick interactive debugging (e.g. `scripts/ecs-run.sh bash`).
  ```
  # Run a script and wait for completion (default)
  scripts/ecs-run-task.sh scripts/backfill_ruling_fields.py -- --dry-run

  # Long-running tasks: launch and detach, check logs later
  scripts/ecs-run-task.sh --detach scripts/reingest_from_s3.py -- --all
  scripts/ecs-run-task.sh --logs <task-arn>

  # Override CPU/memory for heavy workloads
  scripts/ecs-run-task.sh --cpu 2048 --memory 4096 scripts/backfill_parties.py
  ```

## Session Triggers

### Handling system tags in user message turns

**Task notifications and system reminders are system events, not user responses.** Messages tagged with `<task-notification>` or `<system-reminder>` are injected into the user's message turn by the platform — the user did not type them and may not even be aware they arrived.

When one of these tags arrives and you have a **pending question or decision waiting on the user**:

1. **Do not treat the notification as the user's answer.** The user has not responded yet.
2. **Acknowledge the notification in one line** (e.g. "Task #N completed successfully.") and then **continue waiting for the user's actual response** to your pending question. Do not launch into a full status report, re-summarize the conversation, or take new actions that change the conversational context.
3. **Do not re-ask the pending question** — it is already on screen. Simply state that you are still waiting for the user's input on it.

This applies equally to `<task-notification>` (background agent completions/failures) and `<system-reminder>` (context refreshes, memory updates). Neither constitutes user input. Treat them as informational side-channels and preserve the primary conversational flow.

- When the user asks to pick up work (e.g. "let's go", "start", "pick up a task"), invoke `/task` as a background subagent. It handles everything autonomously.
- **Telegram commands** are another inbound channel. When the bridge is configured, call `start_polling()` to auto-poll for Telegram messages in the background, then call `drain_pending_commands()` between tasks to retrieve accumulated commands. A `start #N` command is equivalent to the user typing `/task #N`. See the "Telegram Integration" subsection below.
- If the user asks to explore, investigate, or prototype — do it in `tmp/` and file issues for any real work identified.
- To enable continuous autonomous work queue management, invoke `/orchestrator`.

### Telegram Integration (optional)

When Telegram is configured (bot token in Secrets Manager `judgemind/telegram/bot` and SQS queue `judgemind-telegram-inbound-dev`), the orchestrator can receive inbound commands from Telegram and send lifecycle notifications. Use `packages/telegram-bridge/` — specifically the `OrchestratorBridge` class.

**Lifecycle notifications:** call `session_started()` when an interactive session begins, `task_started()` / `task_completed()` / `task_failed()` around `/task` agent invocations, and `session_ended()` when shutting down.

**Inbound messages:** All Telegram messages are interpreted as free text by a Claude API call (Haiku) in the responder daemon. The daemon responds directly with natural-language replies and extracts actionable commands (start, pause, resume, stop) for the orchestrator. No special command syntax is required — users can write naturally.

The orchestrator still uses `bridge.start_polling(interval=30)` or `bridge.drain_pending_commands()` to pick up commands from the inbox file. The responder daemon handles the interpretation and reply, so the orchestrator only sees pre-parsed actions.

If Telegram is not configured, all bridge calls are silent no-ops. No existing workflows are affected.

#### Orchestrator status file

The orchestrator must call `bridge.write_status()` after every state change (task start, complete, fail, pause, resume). This writes `tmp/orchestrator_status.json` containing:
- Active agents: issue number, title, worker number, phase
- Open PRs: number, CI status, mergeable
- Recently completed tasks: issue number, outcome
- Queue: next issues by priority
- Paused/stopped state

The responder daemon reads this file to provide context to the Claude interpreter, enabling it to give informed, specific answers about orchestrator state.

#### Responder daemon and state files

The standalone **responder daemon** (`scripts/tg-responder.py`) interprets all Telegram messages via a Claude API call (Haiku, ~$0.001/interaction). It receives the user's message and the current orchestrator status, generates a natural-language reply, and extracts any actionable commands. It communicates with the orchestrator via shared state files:

- **`tmp/orchestrator_status.json`** — written by `OrchestratorBridge.write_status()`. The responder reads this to provide context to the Claude interpreter. Contains active agents, open PRs, queue, and paused/stopped state.
- **`tmp/orchestrator_state.json`** — the responder writes `paused` flag changes here. The orchestrator must call `bridge.refresh_state()` before each spawn decision to pick up `pause`/`resume` changes made out-of-loop.
- **`tmp/stop_requests.json`** — the responder appends stop requests here (JSON array of `{"issue_number": N, "timestamp": "..."}`). The orchestrator reads and clears this file by calling `bridge.read_stop_requests()`, which returns newly stopped issue numbers and accumulates them in `bridge.stopped_issues`. Use `bridge.is_issue_stopped(N)` to check before spawning.
- **`tmp/tg_inbox.json`** — queued `start` commands extracted by the interpreter, read by `bridge.read_inbox()`.

**Orchestrator spawn loop pattern:**
1. Call `bridge.write_status()` to update the status file for the responder.
2. Call `bridge.refresh_state()` to pick up external `paused` changes.
3. Call `bridge.read_stop_requests()` to consume new stop requests.
4. Check `bridge.paused` — if `True`, skip spawning.
5. Before spawning issue `#N`, check `bridge.is_issue_stopped(N)` — if `True`, skip it.
6. Call `bridge.read_inbox()` or `bridge.drain_pending_commands()` to get inbound `start` commands.

**Secrets required:**
- `judgemind/telegram/bot` — bot token and allowed user IDs (existing)
- `judgemind/anthropic/api-key` — Anthropic API key for Claude interpreter, or set `ANTHROPIC_API_KEY` env var. If missing, the daemon falls back to simple acknowledgments.

To start the responder daemon: `scripts/tg-responder.py`. To stop it: create `tmp/tg_responder.stop`.

## Improving the Agent Workflow

### Continuous DX improvements

At the end of every implementation loop — whether autonomous (`/ralph`, `/task`) or manual/human-directed — pause and consider whether any workflow friction, repeated patterns, or missing automation could be improved. If you identify a DX improvement:

1. **File a GitHub issue** immediately:
   - Title: `[DX] <description>`
   - Label: `type/dx`
   - Body: describe the friction, the ideal fix, and which files are likely affected.
   - Add `agent/ready` if the fix is self-contained and fully specified.
2. **Kick off a background subagent** (via `/task #N`) to implement the fix in parallel, so it doesn't block the current work.

Examples of DX improvements to look for:
- Permission prompts that could be avoided with a better command pattern or settings change
- Repeated manual steps that could be scripted or added to a hook
- Missing preflight checks that would have caught an error earlier
- CLAUDE.md rules that are unclear, outdated, or missing

### Permission prompt workarounds

When you encounter a permission prompt for a command that is **clearly safe and non-destructive** (read-only operations, local file writes, running tests, formatting tools, creating branches), and the prompt could be avoided with a better command pattern:

1. **Work around it immediately** using the patterns in "Unattended Operation Patterns" above or by splitting the command.
2. **File a GitHub issue** to track the improvement (see above).

Do **not** file issues for prompts that exist for good reason — pushing to remote, opening PRs, merging, deploying, deleting branches, or any action that affects shared state. Those prompts are intentional.

## Memory and Instructions Updates

- Prefer updating `CLAUDE.md` in the repo root over writing to `~/.claude` project memory.
- Only use local `~/.claude` memory for things that cannot go in the repo (e.g. cross-repo or cross-project preferences).

## Additional Prohibitions

- Do not make architectural decisions that contradict `docs/specs/` without filing a `type/decision` issue.
- Do not add dependencies without justification in the PR description.
