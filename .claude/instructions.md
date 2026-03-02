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

### Step 1 — Claim your worker number

Run:
```
git -C /Users/drewthaler/judgemind/judgemind-bootstrap worktree list
```
Examine the output. Worker paths follow the pattern `judgemind-worker-N`. Pick the **lowest integer N ≥ 1 not already present** in the list. That is your worker number for this session.

Example: if the list shows `judgemind-worker-1` and `judgemind-worker-3`, claim **worker-2**.

### Step 2 — Create your worktree and tmp directory (always, no exceptions)

Every agent session must work in an isolated git worktree, never directly in `judgemind-bootstrap`. Run these sequentially (split to avoid `$()` prompts):
```
date +%Y%m%d
git -C /Users/drewthaler/judgemind/judgemind-bootstrap worktree add \
    /Users/drewthaler/judgemind/judgemind-worker-N -b worker-N/session-YYYYMMDD
mkdir -p /tmp/judgemind-worker-N
```
All subsequent work happens inside `/Users/drewthaler/judgemind/judgemind-worker-N`.
Use `/tmp/judgemind-worker-N/` for **all** temporary files (scripts, PR bodies, etc.) — never write to `/tmp/` directly, as other workers may be using the same filenames.

When the session is done, remove the worktree and tmp dir:
```
git -C /Users/drewthaler/judgemind/judgemind-bootstrap worktree remove /Users/drewthaler/judgemind/judgemind-worker-N
rm -rf /tmp/judgemind-worker-N
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

## Code Standards

### Python (scrapers, NLP pipeline)
- Python 3.12+
- Type hints on all function signatures
- pytest for testing
- ruff for linting and formatting
- Dependencies managed via pyproject.toml
- Async where appropriate (httpx for HTTP, playwright for browser automation)

### TypeScript (API, frontend)
- Strict mode always
- Node.js 20+ for API
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
- During initial bringup (before CI exists): push directly to `main` is fine.
- Once CI is established: branch from `main` (`feat/issue-{N}-short-description`), open a PR, wait for CI to pass, then request human review. Never merge your own PRs.
- **A PR is not done until CI is green.** After pushing, watch the run:
  ```
  gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact
  ```
  If CI fails, diagnose the failure, fix it, push again, and repeat until green. Only then comment on the issue linking the PR and add the `status/review` label.

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

## Unattended Operation Patterns

These patterns avoid permission prompts and allow the agent to run without interruption:

- **Git outside the working directory:** use `git -C /absolute/path <subcommand>` instead of `cd /path && git <subcommand>`. Compound commands with `cd` trigger a safety prompt.
- **Multi-line content for `gh` commands:** write to a temp file and use `--body-file /tmp/judgemind-worker-N/file.txt`. Never use backticks or command substitution inside quoted strings passed to `gh`.
- **Multi-line Python scripts:** write to `/tmp/judgemind-worker-N/script.py`, then run with `.venv/bin/python3 /tmp/judgemind-worker-N/script.py`. Embedding multi-line code in `-c "..."` breaks pattern matching and triggers a prompt.
- **Tmp directory isolation:** always use `/tmp/judgemind-worker-N/` (your worker's subdirectory) for all temp files, never `/tmp/` directly. Multiple workers share the same `/tmp` and will collide on common filenames like `script.py` or `pr_body.txt`.
- **Dynamic values in shell commands:** never embed `$(...)` command substitution inside a command that needs approval. Run the inner command first to get the value, then use the literal value in the next command. Example: run `date +%Y%m%d` first, then use the printed date string in the subsequent command.

## Improving the Agent Workflow

When you encounter a permission prompt for a command that is **clearly safe and non-destructive** (read-only operations, local file writes, running tests, formatting tools, creating branches), and the prompt could be avoided with a better command pattern:

1. **Work around it immediately** using the patterns above or by splitting the command.
2. **File a GitHub issue** to track the improvement:
   - Title: `[DX] Agent workflow: avoid prompt for <description>`
   - Label: `type/dx` (create it if it doesn't exist)
   - Body: describe what triggered the prompt, the workaround used, and the specific line to add to the "Unattended Operation Patterns" section of `.claude/instructions.md`.

Do **not** file issues for prompts that exist for good reason — pushing to remote, opening PRs, merging, deploying, deleting branches, or any action that affects shared state. Those prompts are intentional.

## Things You Must Not Do

- Do not merge PRs.
- Do not deploy to production.
- Do not make architectural decisions that contradict the specs without flagging them as `type/decision` issues.
- Do not scrape live court websites during development.
- Do not store secrets in code, config files, or commit history.
- Do not add dependencies without justification in the PR description.
