# Judgemind — Agent Instructions

**STOP. Read this entire file before doing anything else.** Do not explore the codebase, do not read other files, do not respond substantively to the user's request until you have read this file. This file defines your mandatory workflow — deviating from it is a bug.

## Critical Rules — Read First

These are the most frequently violated rules. **A PreToolUse hook enforces the shell rules automatically**, but you must internalize all of them. See `docs/preflight-checklist.md` for the complete machine-readable checklist.

### NEVER — Shell Commands
- **NEVER** use `$()` command substitution in any Bash command. Run the inner command as a separate tool call and use the literal result. For secrets, use `scripts/with-secret.sh`.
- **NEVER** use heredocs (`<<EOF`) in Bash commands. Write content to a file with the Write tool, then pass via `--body-file` or `-F`.
- **NEVER** use `python3 -c "..."` for inline scripts. Write to `{worktree}/tmp/script.py` and run the file.
- **NEVER** combine quoted strings with `&&` or `;`. Split into separate tool calls.
- **NEVER** prefix scripts with `bash` — run `scripts/start-worker.sh`, not `bash scripts/start-worker.sh`.
- **NEVER** use Edit or Write tools on files inside `.claude/`. The CLI blocks these operations. Write content to `{worktree}/tmp/` first, then copy it into place with `scripts/write-claude-file.sh {worktree}/tmp/file.md {worktree}/.claude/target/file.md`.

### NEVER — Workflow
- **NEVER** commit directly to `main` during autonomous task work. All `/task` work happens on worktree branches via PRs. (The user may direct you to commit to `main` during interactive sessions — that's fine.)
- **You MAY merge your own PRs** if the PR has passed the `/ralph` review loop (A.2) and CI is green. Use `gh pr merge <N> --repo judgemind/judgemind --squash --delete-branch`.
- **NEVER** exit or stop after `/ralph` completes without finishing the full `/task` workflow (A.3 through A.9). Ralph completing means the code is ready — but uncommitted, unpushed, and unmerged. The task is only halfway done. See #721.
- **NEVER** deploy to production. Production deploys are human-only.
- **NEVER** set `priority/p0` on issues unless explicitly told to by a human. `p0` is human-only.
- **NEVER** skip pre-PR checks. Run lint, format, AND tests locally before pushing.
- **NEVER** share venvs between worktrees. Each worktree gets its own `.venv`.

### NEVER — Interactive Sessions
- **NEVER file issues, create PRs, or act on a proposed approach without explicit user confirmation.** Before filing any issue or taking action on a proposal during an interactive session, check: did the user explicitly say "yes", "file it", "go ahead", or similar in their most recent actual message (not a `<system-reminder>` or `<task-notification>`)? If not, do not act. Proposals and designs require explicit approval before action.
- This rule does **not** apply to autonomous `/task` agents working from already-filed issues — they should continue to act independently (filing sub-tasks, DX issues, etc.) without asking.

### ALWAYS — Before Acting
- **ALWAYS** Read a file before Writing to it (the Write tool fails on existing files you haven't read).
- **ALWAYS** pull latest code (run `git fetch origin main` then `git rebase origin/main` as separate tool calls) before analyzing or modifying files.
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
preflight_no_duplicate_pr N # Check if open PR already exists for issue #N
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

Interactive sessions are **general-purpose** — the user decides what the session is for. To enable autonomous work queue management, invoke `/orchestrator`. This is opt-in.

### Autonomous sessions (subagent via `/task`)

Subagents do the implementation work: worktree setup, coding, testing, PR, and review. They follow the full PR Workflow below.

### Available Skills

- **`/task`** — Full autonomous pipeline: worktree, issue claim, implementation, PR, review. Accepts `#N`, natural language, or no argument (picks highest priority).
- **`/ralph`** — Iterative work-review loop. Spawns worker (TDD) and reviewer subagents. Called by `/task` automatically for testable code tasks.
- **`/tdd`** — Test-driven implementation for code tasks (Python, TypeScript). Called by `/ralph` internally. **Not for** Terraform, DB migrations, CI/CD, docs, or investigation tasks.
- **`/orchestrator`** — Opt-in autonomous work queue manager. See `.claude/skills/orchestrator/SKILL.md`.
- **`/audit`** — Periodic codebase health audit. Reviews recent PRs, checks for dead code, test gaps, performance issues, security concerns, and dependency health. Files issues for findings. Triggered by the orchestrator every 20 merged PRs, or manually.

### Worktree setup (manual)

```
scripts/start-worker.sh
```

Claims a worker number, creates the worktree from latest `origin/main`, configures git hooks, and creates `tmp/`. All work happens inside `{worktree}`. Each worktree gets its own `.venv` per package:

```
python3.12 -m venv {worktree}/packages/<pkg>/.venv
cd {worktree}/packages/<pkg> && .venv/bin/pip install -e ".[dev]" --quiet
```

### Step 3 — Pick up a task

Use `/task` to claim and work on an issue: `/task`, `/task #42`, or `/task scrapers`.

## PR Workflow (authoritative — applies to all task work)

**Single-issue rule:** each PR addresses exactly one issue. Do not combine unrelated changes in a single PR. If an issue is large or ambiguous, break it into sub-tasks first (see **Creating Sub-Tasks**), label them `agent/ready`, then pick up the first sub-task.

**All commits must be made on the worktree branch created in Step 2, never directly on `main`.** Every change goes through a PR — no direct pushes to `main`, ever.

Complete every substep in order. A task is not done until substep 4.11 is finished. Do not ask the user for confirmation during any of these steps.

#### 4.1 — Sync the worktree to latest main

```
git -C {worktree} fetch origin main
git -C {worktree} rebase origin/main
```

#### 4.2 — Understand the problem

- Read the issue thoroughly, including linked issues.
- Check `docs/specs/` for relevant guidance.
- Look at existing code for patterns. Be consistent with what's already there.
- If you need a decision from the maintainer, comment on the issue, label it `status/blocked`, and pick up a different task.

#### 4.3 — Implement and verify locally

- **For testable code tasks** (Python, TypeScript): use the `/ralph` loop. See `.claude/skills/ralph/SKILL.md`.
- **For non-testable tasks** (Terraform, DB migrations, CI/CD, docs): implement directly, then run all applicable pre-PR checks and review your own diff.
- Fix any failures before proceeding.

#### 4.4 — Push, open a PR, and immediately watch CI

```
git push -u origin <branch>
```

Before creating a PR, check for duplicates using `preflight_no_duplicate_pr` from `scripts/preflight.sh`. If a duplicate is found (return code 0), adopt the existing PR instead of creating a new one. If no duplicate (return code 1) or on error (return code 2), proceed normally:

```
gh pr create --repo judgemind/judgemind ...
gh run list --repo judgemind/judgemind --branch <branch> --limit 1 --json databaseId -q '.[0].databaseId'
gh run watch <run-id> --repo judgemind/judgemind --exit-status --compact
```

**Never leave the CI watch step unfinished.** If CI is green, continue to 4.5. If CI fails, go to 4.7.

#### 4.5 — Verify no merge conflicts

```
gh pr view <N> --repo judgemind/judgemind --json mergeable,mergeStateStatus
```
If `mergeable` is `CONFLICTING`, rebase onto main, resolve, push with `--force-with-lease`, return to 4.4.

#### 4.6 — CI is green — confirm before proceeding

```
gh pr view <N> --repo judgemind/judgemind --json statusCheckRollup
```
All checks must show `SUCCESS` or `SKIPPED`. Any `FAILURE` goes to 4.7.

#### 4.7 — Fix CI failures (repeat until green)

Diagnose, fix locally, push again, return to 4.4. Repeat until CI is green.

#### 4.8 — Update the PR test plan

Fetch the PR body, check off automated steps that passed in CI. Write updated body to `{worktree}/tmp/pr_body.txt` and update with `gh pr edit --body-file`.

#### 4.9 — Link the issue and request review

Comment on the issue linking the PR. Add the `status/review` label.

#### 4.10 — Verify deployment (after merge, deployed services only)

Skip for library, tooling, docs, or CI-only changes. For deployed code (API, scrapers, infra):

1. Watch the deploy workflow triggered by the merge to `main` (`gh run watch`).
2. If deploy **fails**: file a `priority/p1` issue, reference the merged PR, add `agent/ready`.
3. If deploy **succeeds**: smoke-test the deployed environment where feasible.

For **web frontend** changes: Vercel deploys automatically — see `docs/agent/infrastructure-reference.md` for details.

#### 4.11 — Remove your worktree

```
scripts/end-worker.sh {worktree}
```

## Tool Use Rules

- **Use dedicated tools for file operations** — never use Bash for `cat`, `ls`, `grep`, `find`. Use Read, Glob, and Grep instead.
- **Always Read before Write** — the Write tool requires this for existing files.
- **Use Bash only for shell-only operations** — git, gh CLI, running tests, pip install, terraform, etc.
- `sudo` and `rm` always prompt; split commands to avoid triggering prompts.

For detailed patterns to avoid permission prompts, see `docs/agent/unattended-patterns.md`.

## Accounts & Infrastructure

**GitHub:** org `judgemind/judgemind`. **AWS:** account `155326049300`, region `us-west-2`.

For detailed infrastructure reference (Vercel, Terraform state, ECS, secrets), see `docs/agent/infrastructure-reference.md`.

## Code Standards

### Python (scrapers, NLP pipeline)

- Python 3.12+, using `.venv` in each package directory
- Run tests: `.venv/bin/pytest tests/ -v`
- Install deps: `.venv/bin/pip install -e ".[dev]"`
- Type hints on all function signatures
- pytest for testing; ruff for linting and formatting
- Dependencies managed via pyproject.toml
- Async where appropriate (httpx for HTTP, playwright for browser automation)

### Python scripts (`scripts/*.py`)

Scripts in `scripts/` that import non-stdlib modules must use `_venv_helper`:

```python
from _venv_helper import ensure_venv
ensure_venv("scraper-framework")  # or "telegram-bridge", etc.
```

- Set `_VENV_HELPER_SKIP=1` in tests or containers where deps are already available.
- Eval scripts (`scripts/eval/`) are excluded from this convention.
- **ECS oneshot constraint:** Scripts run via `ecs-run-task.sh` are uploaded as single files — they **cannot import other `.py` files from `scripts/`**. Only stdlib, installed packages, and `_venv_helper` (stubbed in-container) are available. If you need shared code, either inline it, use a lazy import inside a function (for optional features), or move the shared code into an installed package. CI enforces this via `scripts/check-oneshot-imports.sh`. Scripts that are never run as ECS oneshots can be added to the `LOCAL_ONLY` list in that script.

### TypeScript (API, frontend)

- Strict mode always
- Node.js 20+ for API; activate with `source ~/.nvm/nvm.sh && nvm install 20 --no-progress`
- Next.js 14+ for frontend
- ESLint + Prettier
- Jest or Vitest for testing

### General

- All code must have tests. Scrapers must have regression tests against archived pages in `tests/fixtures/`.
- Never hardcode secrets, API keys, credentials, or URLs to live court sites in source code. Use environment variables.
- Never commit large binary files. Use `.gitignore`.
- Write clear docstrings/comments for non-obvious logic.

### Performance awareness

Every diff review must check for these common bottlenecks:

- **Sequential I/O over collections.** Use concurrency (`ThreadPoolExecutor`, `asyncio.gather`, `pipeline()`) or batching instead of per-item network calls.
- **O(n^2) pagination.** Use keyset (cursor-based) pagination, never `LIMIT/OFFSET` for large datasets.
- **Unbatched DB writes.** Use `executemany`, `COPY`, or psycopg3 `pipeline()` mode.
- **Missing connection reuse.** Reuse HTTP clients, DB connections, and S3 clients across calls.

If unsure whether a perf pattern matters at current scale, add a `# TODO(perf):` comment.

## Pre-PR Checks (MANDATORY — No Exceptions)

**Every agent MUST run ALL applicable checks locally BEFORE pushing.** The `.githooks/pre-push` hook also runs them automatically.

**Python packages** (from the package directory):

```
.venv/bin/ruff check src/ tests/           # Lint (rules: E, F, I, N, UP, ANN)
.venv/bin/ruff format --check src/ tests/   # Format check
.venv/bin/pytest tests/ -v --tb=short       # Tests with coverage
```

If lint fails: `.venv/bin/ruff check --fix src/ tests/` then `.venv/bin/ruff format src/ tests/`.

Common ruff pitfalls: **I001** (unsorted imports, `--fix` resolves), **F401** (unused imports, remove them), **UP017** (use `datetime.now(datetime.UTC)`). Format and lint are **separate commands** — run BOTH.

**Coverage gates (enforced in CI):**
- **Diff coverage:** new/changed lines must have >= 90% test coverage. CI runs `diff-cover` against `coverage.xml` (Python) or `lcov.info` (TypeScript).
- **Coverage floor ratchet:** overall package coverage must not decrease below the baseline in `coverage-baselines.json`. The floor only goes up — when coverage increases, update the baselines with `scripts/update-coverage-baselines.py`.

**TypeScript packages:**

```
npm run lint                                # ESLint
npm run typecheck                           # tsc --noEmit
npm test                                    # Vitest
```

For `packages/web/`, also run `npm run build`. The same diff coverage and floor ratchet gates apply to TypeScript packages (CI reads `lcov.info`).

**Terraform** (from `infra/terraform/`):

```
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

### Subagent Responsibilities

#### Worktree Isolation

**Do NOT use `isolation: "worktree"` on the Agent tool for `/task` subagents.** The `/task` skill creates its own worktree internally. The Agent tool's worktree isolation breaks project permissions.

Spawn `/task` agents **without** `isolation: "worktree"`. For non-`/task` subagents needing branch isolation (rare), create a worktree manually. **Never run `git checkout` or `git switch` in the parent's working directory from a subagent.**

#### Pre-PR Checks

Subagents MUST install dependencies, run ALL lint/format/test commands for every package touched, fix failures before committing, and only push after all local checks pass.

## Git Workflow

- Commit messages follow conventional commits: `feat(scraping): implement OC PDF link scraper (#42)`
- Always work on the worktree branch. Open a PR, wait for CI. You may merge your own PRs after ralph and CI are green.
- **A PR is not done until it has no conflicts and CI is green.**

## Task Dependencies

- Blocked issues carry `status/blocked` and do **not** have `agent/ready`. Agents skip them.
- Dependencies are listed as `Blocked by #N` under a `## Dependencies` heading.

### When you finish a task

**Implementation tasks (PRs):** dependent issues are unblocked automatically by the `unblock-issues` CI workflow when the PR merges. The PR body must include `Closes #N`.

**Non-PR completions:** manually unblock dependent issues. Search for open issues with `Blocked by #<your-issue>`. For each, if all blockers are closed: remove `status/blocked`, add `agent/ready`, remove the `Blocked by` lines from the body.

## Creating Sub-Tasks

If a task naturally breaks into 2+ independent pieces of work, create child issues:

- Reference the parent: "Parent: #42" in the issue body.
- Sub-tasks should be self-contained — another agent should be able to pick one up independently.
- Label child issues appropriately and add `agent/ready` if fully specified.

## Investigation Tasks

Investigation tasks produce documentation, not code:

- Write findings in the issue body or `docs/investigations/`.
- **Always file follow-up issues** for every actionable finding. Label them `agent/ready` if fully specified. Reference the investigation issue as the parent.

## Scraper Development Rules

Key paths: framework in `packages/scraper-framework/src/framework/`, California courts in `packages/scraper-framework/src/courts/ca/`.

- **Never run production scraping from dev.** Fetching pages to create real test fixtures is fine.
- Every scraper must implement the base `Scraper` class, report health metrics, and use SHA-256 content hashing.
- Raw content is always archived to object storage before processing.
- Scraper configurations (URLs, selectors, schedules) are separate from scraper logic.
- **Field extraction completeness is a hard requirement.** Required fields: **judge name, motion type, case title, hearing date, outcome, parties**. Write regression tests against real fixtures for every field.

## Infrastructure Code

- Terraform for all AWS resources. Every resource must be in a module.
- **Do NOT add `tags` blocks to individual resources** — the AWS provider's `default_tags` handles this.
- Never commit AWS credentials or state files.
- **Dev terraform apply is automated.** After a PR that touches `infra/terraform/` merges to main, the orchestrator automatically runs `terraform apply` for the dev environment. Production applies remain human-only. See `.claude/skills/orchestrator/SKILL.md` for the full apply procedure.

For Terraform apply/deploy details, see `docs/agent/infrastructure-reference.md`.

## Unattended Operation Patterns

The Critical Rules above cover the most common patterns. For the full reference of permission-prompt workarounds (git, curl, secrets, `.claude/` writes, ECS, Telegram), see `docs/agent/unattended-patterns.md`.

## Session Triggers

### Handling system tags in user message turns

**Task notifications and system reminders are system events, not user responses.** Messages tagged with `<task-notification>` or `<system-reminder>` are injected by the platform — the user did not type them.

When one of these tags arrives and you have a **pending question**: do not treat it as the user's answer. Acknowledge in one line, continue waiting for the user's actual response. See also the **NEVER — Interactive Sessions** rule in Critical Rules, which provides a concrete point-of-action check: before filing any issue or acting on a proposal, verify that the user's most recent actual message contains explicit confirmation.

### Telegram Integration (optional)

Telegram integration is opt-in. For full details, see `docs/agent/telegram-reference.md`.

When the user asks to pick up work, invoke `/task` as a background subagent. To enable continuous autonomous work queue management, invoke `/orchestrator`.

## Improving the Agent Workflow

### Continuous DX improvements

At the end of every implementation loop, consider workflow friction that could be improved. File a `type/dx` issue and kick off a background subagent to fix it. Look for: avoidable permission prompts, repeated manual steps, missing preflight checks, unclear CLAUDE.md rules.

### Permission prompt workarounds

When you encounter a prompt for a safe command, work around it immediately using the patterns in `docs/agent/unattended-patterns.md`. File a GitHub issue to track the improvement. Do **not** file issues for intentional prompts (push, PR, merge, deploy).

## Memory and Instructions Updates

- Prefer updating `CLAUDE.md` in the repo root over writing to `~/.claude` project memory.
- Only use local `~/.claude` memory for things that cannot go in the repo.

## Additional Prohibitions

- Do not make architectural decisions that contradict `docs/specs/` without filing a `type/decision` issue.
- Do not add dependencies without justification in the PR description.
