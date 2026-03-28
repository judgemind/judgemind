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
- **NEVER** use shell `&`, `nohup`, `disown`, or multicommand tricks to background a process. Use the Bash tool's `run_in_background: true` parameter instead. Shell backgrounding requires compound commands that cannot be allowlisted and always trigger permission prompts.
- **NEVER** use Edit or Write tools on files inside `.claude/`. The CLI blocks these operations. Write content to `{worktree}/tmp/` first, then copy it into place with `scripts/write-claude-file.sh {worktree}/tmp/file.md {worktree}/.claude/target/file.md`.

### NEVER — Workflow
- **NEVER** use `run_in_background` in any subagent (`/task`, `/ralph`, `/tdd`, or any Agent-spawned worker/reviewer). Subagents are already running as background tasks — further backgrounding causes completion notifications to surface in the wrong context (the parent agent), leading to confusion and lost results. All commands inside subagents run synchronously.
- **NEVER** commit directly to `main` during autonomous task work. All `/task` work happens on worktree branches via PRs. (The user may direct you to commit to `main` during interactive sessions — that's fine.)
- **You MAY merge your own PRs** if the PR has passed the `/ralph` review loop (§4.3) and CI is green. Use `gh pr merge <N> --repo judgemind/judgemind --squash --delete-branch`.
- **NEVER** exit or stop after `/ralph` completes without finishing the full `/task` workflow (steps 4.4 through 4.11 in this document; A.3 through A.9 in the task skill). Ralph completing means the code is ready — but uncommitted, unpushed, and unmerged. The task is only halfway done. See #721.
- **NEVER** deploy to production. Production deploys are human-only.
- **NEVER** set `priority/p0` on issues unless explicitly told to by a human. `p0` is human-only.
- **NEVER** skip pre-PR checks. Run lint, format, AND tests locally before pushing.
- **NEVER** share venvs between worktrees. Each worktree gets its own `.venv`.
- **NEVER** create additional worktrees from inside a worktree via `git worktree add`. Subagents must work in their assigned worktree only. If the worktree gets into a bad state, fix it (e.g., `git checkout -- .`, `git clean -fd`) rather than creating a new one. Child worktrees become orphaned — `cleanup_worktree.sh` cannot track them, and the dispatcher does not know about them.
- **NEVER** close a task or remove a worktree without posting a verification evidence comment on the issue. Every task completion requires concrete evidence that the change works (deployed services) or an explicit skip reason (docs/CI/tooling). See §4.10 Step 3.

### ALWAYS — Before Acting
- **ALWAYS** Read a file before Writing to it (the Write tool fails on existing files you haven't read).
- **ALWAYS** pull latest code (run `git fetch origin main` then `git rebase origin/main` as separate tool calls) before analyzing or modifying files.
- **ALWAYS** use `{worktree}/tmp/` for temp files, never `/tmp/`.
- **ALWAYS** use dedicated tools (Read, Glob, Grep) instead of Bash for file operations.
- **ALWAYS** watch CI to completion (`gh run watch`) before doing anything else after pushing.
- **ALWAYS** create a PR immediately after your first push to a branch.
- **ALWAYS** re-fetch GitHub issue or PR state before acting on it if more than a few minutes have elapsed since you last fetched it. Other agents may have closed, merged, or modified issues in the interim — acting on stale state causes incorrect cross-references, duplicate filings, and wasted work.
- **ALWAYS** set `timeout: 1200000` (20 minutes) on Bash commands that may take longer than 2 minutes. The default 2-minute timeout causes the platform to auto-background the command, which violates the no-background rule for subagents and causes lost results. Commands that need this: `pytest`, `gh run watch`, `terraform apply`, `pip install`, `npm install`, `npm run build`, `ruff check` on large codebases, and any script that processes data.

## Enforced Rules — Automated Checks

The PreToolUse hook (`.claude/hooks/preflight-bash.sh`) and `scripts/preflight.sh` automatically enforce the Critical Rules above. The hook blocks `$(`, `<<EOF`, `python -c`, `git push` to main, and `git worktree add` inside worktrees. A separate PreToolUse hook (`.claude/hooks/agent-worktree-guard.sh`) blocks Agent tool calls with `isolation: "worktree"` when cwd has drifted into an existing worktree, preventing nested worktree creation. Runtime checks:

```bash
source scripts/preflight.sh
preflight_in_worktree       # Fail if pwd is main repo, not a worktree
preflight_not_on_main       # Fail if on main/master branch
preflight_branch_fresh      # Fail if behind origin/main (add --fetch to fetch first)
preflight_venv_local        # Fail if .venv is missing or is a symlink
preflight_no_duplicate_pr N # Check if open PR already exists for issue #N
preflight_rate_budget       # Warn if GitHub API rate budget < 100 remaining
```

## Project Context

Judgemind is a free, open-source legal research platform replacing Trellis.law. Read the specs in `docs/specs/` for full context. The key things to know:

- **Tentative rulings are ephemeral.** If a scraper is down, data is permanently lost. Scraper reliability is the highest priority in the system.
- **Two data pipelines.** Model A captures California tentative rulings (ephemeral, high urgency). Model B extracts judge analytics from dockets/documents (all other states, persistent data, NLP-dependent).
- **Self-funded and free.** Every architecture decision must consider cost. Prefer fixed-cost over usage-based. Never assume unlimited budget.
- **API-first.** The web app is a client of the API. Every UI feature has an API endpoint.

### Key Documentation

Consult these docs before making changes in their domain:

| Document | When to consult |
|----------|-----------------|
| `docs/specs/product-spec-v3.md` | Product requirements, feature priorities, scope decisions |
| `docs/specs/user-journeys.md` | UI/UX decisions, feature design, evaluating what to build |
| `docs/specs/architecture-spec-v1.md` | Ingestion pipeline, data model, infrastructure decisions |
| `docs/web-patterns.md` | **All frontend work** — page layout patterns, component usage, consistency rules |
| `docs/BRAND.md` | Colors, typography, design principles for all visual work |
| `docs/web-lessons.md` | Frontend incident lessons, server component error handling |

## Starting a New Session

Wait for the user's instruction before deciding what to do. Sessions fall into two modes:

### Interactive sessions (human present)

Interactive sessions are **general-purpose** — the user decides what the session is for. To enable autonomous work queue management, invoke `/dispatcher`. This is opt-in.

### Autonomous sessions (subagent via `/task`)

Subagents do the implementation work: worktree setup, coding, testing, PR, and review. They follow the full PR Workflow below.

### Available Skills

- **`/task`** — Full autonomous pipeline: worktree, issue claim, implementation, PR, review. Accepts `#N`, natural language, or no argument (picks highest priority).
- **`/ralph`** — Iterative work-review loop. Spawns worker (TDD) and reviewer subagents. Called by `/task` automatically for testable code tasks.
- **`/tdd`** — Test-driven implementation for code tasks (Python, TypeScript). Called by `/ralph` internally. **Not for** Terraform, DB migrations, CI/CD, docs, or investigation tasks.
- **`/dispatcher`** — Opt-in autonomous work queue manager. See `.claude/skills/dispatcher/SKILL.md`.
- **`/audit`** — Periodic codebase health audit. Reviews recent PRs, checks for dead code, test gaps, performance issues, security concerns, and dependency health. Files issues for findings. Triggered by the dispatcher every 20 merged PRs, or manually.
- **`/spotcheck`** — Periodic data quality spot-check. Samples rulings across counties, runs automated DB queries for known issue patterns, screenshots case detail pages for visual inspection, cross-references existing issues, and files new issues for findings.

### Worktree setup

**Automated (via dispatcher):** The dispatcher spawns `/task` agents with `isolation: "worktree"` on the Agent tool. Claude Code automatically creates a unique worktree at `.claude/worktrees/agent-<id>/`. No locking, no number contention, no races.

**Manual (interactive sessions):** Use `scripts/start-worker.sh` for manual worktree setup when not using the dispatcher. This claims a worker number, creates the worktree, configures git hooks, and creates `tmp/`.

Each worktree gets its own `.venv` per package:

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
- **Scope completeness check:** Before implementing, search the codebase for all locations affected by the change. If the issue mentions fixing or changing X in one file, grep for X across the entire codebase. List all locations that use, render, or implement the same pattern. If the issue's scope doesn't cover all of them, either expand scope to include them or file follow-up issues for the missed locations so they are tracked. Document the scope check results (what you searched for, what you found) in your implementation notes.
- If you need a decision from the maintainer, comment on the issue, block it with `scripts/block-issue.sh <issue> <blocker>` (or just add `status/blocked` if there is no specific blocking issue), and pick up a different task.

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
gh run watch <run-id> --repo judgemind/judgemind --interval 60 --exit-status --compact
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

Fetch the PR body, check off **Automated checks** items that passed in CI. Do NOT check off **Post-deploy verification** items yet — those are checked after merge and deploy in step 4.10. Write updated body to `{worktree}/tmp/pr_body.txt` and update with `gh pr edit --body-file`.

PR test plans must be structured with two sections:
- **Automated checks** (lint, format, tests, CI) — checked when CI passes
- **Post-deploy verification** (feature works on dev) — checked after deploy with concrete evidence

#### 4.9 — Link the issue and request review

Comment on the issue linking the PR. Add the `status/review` label.

#### 4.10 — Verify deployment and post evidence (after merge, deployed services only)

**A task is NOT done when the PR merges. A task is done when the change is deployed, verified working, AND verification evidence is posted.** The worktree stays alive until verification passes.

Skip deploy watching only for pure library, tooling, docs, or CI-only changes that have no deployed component — but you must STILL post a verification evidence comment (see Step 3 below).

**Step 1 — Watch the deploy workflow:**

1. Watch the deploy workflow triggered by the merge to `main` (`gh run watch`).
2. If deploy **fails**: file a `priority/p1` issue, reference the merged PR, add `agent/ready`. Do NOT consider the task complete.
3. If deploy **succeeds**: continue to Step 2 (functional verification).

For **web frontend** changes: Vercel deploys automatically — see `docs/agent/infrastructure-reference.md` for details.

**Step 2 — Functional verification (required for deployed services):**

A successful deploy only means the new image is running — not that the service works. Verify the feature is actually functional based on the change type:

| Change type | Verification | Required evidence |
|---|---|---|
| **DB migration + code** | Confirm migration applied (column/table exists via `scripts/dev-db-query.sh`) AND service processes a request without errors | DB query output + successful request/response |
| **API endpoint** | Hit the endpoint on dev (`curl https://dev.api.judgemind.org/graphql`), confirm expected response shape and no errors | Curl response (status code + relevant body snippet) |
| **Ingestion pipeline** | Confirm the worker processes at least one message successfully (check ECS logs via `scripts/ecs-logs.sh /ecs/judgemind-ingestion-worker-dev --lines 50` for recent successful processing) | Log lines showing successful processing |
| **Scraper** | Check ECS logs for the next scheduled run, confirm documents are captured without errors | Log lines showing successful capture |
| **Frontend** | Confirm the affected page loads on `dev.judgemind.org` and renders the expected content | Screenshot or page content showing the feature |
| **DX/tooling** | Run the tool in a representative scenario and confirm expected output | Command output showing correct behavior |
| **Backfill / data migration script** | Execute the script against dev via `scripts/ecs-run-task.sh` (or locally if appropriate). Confirm the expected data changes applied — e.g., query dev DB via `scripts/dev-db-query.sh` to check row counts, null rates, or sample records. | DB query results showing the data changed |

**Script-producing tasks:** If a task produces a backfill, migration, or one-off fixup script that is meant to be run, executing it on dev and verifying results is part of the definition of done. When filing issues that include "create a backfill script" or similar, always include "backfill executed on dev and results verified" in the acceptance criteria.

If functional verification fails: diagnose the issue. If it's a simple fix, fix it in a follow-up PR. If it's complex, file a `priority/p1` issue with details of what's broken, reference the merged PR, and add `agent/ready`.

**Step 3 — Post verification evidence comment (MANDATORY):**

After verification succeeds (or after determining there is no deployed component), post a verification evidence comment on the issue. This is a hard gate — the task cannot proceed to 4.11 without this comment. Write the comment to `{worktree}/tmp/verification_evidence.txt`, then post it with `gh issue comment`.

For deployed changes, include concrete evidence (curl output, DB query results, log lines, screenshots). For non-deployed changes, state the skip reason explicitly (e.g., "No deployed component — docs/CI only"). See `.claude/skills/task/SKILL.md` A.8 Step 3 for the full evidence format.

After posting, update the PR test plan to check off the **Post-deploy verification** items.

#### 4.11 — Remove your worktree

**Only remove the worktree after deployment verification passes** (or after confirming the change has no deployed component). Never clean up immediately after merge — the worktree is needed for debugging if verification fails.

For agents spawned with `isolation: "worktree"`, Claude Code handles cleanup automatically when the agent exits. For manual worktrees, run:

```
scripts/end-worker.sh {worktree}
```

## Tool Use Rules

- **Use dedicated tools for file operations** — never use Bash for `cat`, `ls`, `grep`, `find`. Use Read, Glob, and Grep instead.
- **Always Read before Write** — the Write tool requires this for existing files.
- **Use Bash only for shell-only operations** — git, gh CLI, running tests, pip install, terraform, etc.
- `sudo` and `rm` always prompt; split commands to avoid triggering prompts.

For detailed patterns to avoid permission prompts, see `docs/agent/unattended-patterns.md`.

### GitHub API Rate Limit Awareness

GitHub allows 5,000 API requests per hour. With multiple concurrent agents, this budget is shared and can be exhausted quickly.

- **Always use `--interval 60` with `gh run watch`.** The default poll interval is 3 seconds, which burns through API budget fast. Use `gh run watch <id> --repo judgemind/judgemind --interval 60 --exit-status --compact` as the standard CI/deploy watch command.
- **Never retry 403 errors in a tight loop.** Always check the rate limit reset time and wait for it.

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

Scripts in `scripts/` that import non-stdlib modules must declare their venv with a `# venv:` header comment in the first 10 lines:

```python
#!/usr/bin/env python3
"""Script docstring."""
# venv: scraper-framework
from __future__ import annotations
```

Run scripts via `scripts/run-py.sh scripts/<name>.py` — it reads the header and activates the correct venv automatically.

- **One-off scripts** (backfills, cleanups, fixups) must include a `# one-off: true` header comment in the first 10 lines. This marks them for automatic detection by the `/audit` skill so they can be archived when no longer needed. Example:

```python
#!/usr/bin/env python3
"""Backfill missing party names for Santa Barbara rulings."""
# venv: scraper-framework
# one-off: true
from __future__ import annotations
```

- Eval scripts (`scripts/eval/`) are excluded from this convention.
- **ECS oneshot constraint:** Scripts run via `ecs-run-task.sh` are uploaded as single files — they **cannot import other `.py` files from `scripts/`**. Only stdlib and installed packages are available. If you need shared code, either inline it, use a lazy import inside a function (for optional features), or move the shared code into an installed package. CI enforces this via `scripts/check-oneshot-imports.sh`. Scripts that are never run as ECS oneshots can be added to the `LOCAL_ONLY` list in that script.

### TypeScript (API, frontend)

- Strict mode always
- Node.js 20+ for API; activate with `source ~/.nvm/nvm.sh && nvm install 20 --no-progress`
- Next.js 14+ for frontend
- ESLint + Prettier
- In `packages/web/src/app/`, use `@/` path aliases instead of deep relative imports (`../../` or deeper). The `local/prefer-path-alias` ESLint rule enforces this. Deep relative imports break when route groups are reorganised.
- **Follow `docs/web-patterns.md`** for all page layout, component usage, and consistency decisions. This is mandatory for frontend work.
- Jest or Vitest for testing

### General

- All code must have tests. Scrapers must have regression tests against archived pages in `tests/fixtures/`.
- Never hardcode secrets, API keys, credentials, or URLs to live court sites in source code. Use environment variables.
- Never commit large binary files. Use `.gitignore`.
- Write clear docstrings/comments for non-obvious logic.
- **When removing or renaming module-level exports** (functions, classes, constants), grep for all import sites across the entire codebase (`src/` and `tests/`) before committing. Update or remove every import — not just the test file matching the modified module. Broken imports in unrelated test files will not surface until CI runs the full suite, and may not surface at all if the tests are skipped or filtered.

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
.venv/bin/ruff check src/ tests/           # Lint (rules: E, F, I, N, UP, ANN, DTZ)
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

**Spawn `/task` agents with `isolation: "worktree"` on the Agent tool.** Claude Code creates a unique worktree at `.claude/worktrees/agent-<id>/` automatically — no locking, no number contention, no races. The `/task` agent detects it is already in a worktree and skips manual worktree setup.

For non-`/task` subagents needing branch isolation (rare), create a worktree manually. **Never run `git checkout` or `git switch` in the parent's working directory from a subagent.**

#### Pre-PR Checks

Subagents MUST install dependencies, run ALL lint/format/test commands for every package touched, fix failures before committing, and only push after all local checks pass.

## Git Workflow

- Commit messages follow conventional commits: `feat(scraping): implement OC PDF link scraper (#42)`
- Always work on the worktree branch. Open a PR, wait for CI. You may merge your own PRs after ralph and CI are green.
- **A PR is not done until it has no conflicts and CI is green.**

## Task Dependencies

- Blocked issues carry `status/blocked` and do **not** have `agent/ready`. Agents skip them.
- Dependencies are listed as `Blocked by #N` under a `## Dependencies` heading.

### Marking an issue as blocked

**Both pieces are required** — the `status/blocked` label AND a `Blocked by #N` line in the issue body. The `unblock-issues` CI workflow searches for `Blocked by #N` in the body to find issues to unblock when a PR merges. If the label is present but the body text is missing, the workflow never fires and the issue stays stuck forever.

**Always use the helper script** to block an issue:

```
scripts/block-issue.sh <issue> <blocker>
```

This atomically: (1) adds `Blocked by #<blocker>` to the issue body's `## Dependencies` section (creating it if absent), (2) adds the `status/blocked` label, and (3) removes the `agent/ready` label.

**Do NOT** block issues by only adding the `status/blocked` label, only posting a comment, or using `Parent: #N` without a `Blocked by` line. These patterns break auto-unblocking:

| Pattern | Auto-unblock? | Correct? |
|---|---|---|
| `status/blocked` label + `Blocked by #N` in body | Yes | Yes |
| `status/blocked` label + comment "Blocked by #N" (no body text) | **No** | **No** |
| `Parent: #N` without `Blocked by #N` | **No** | **No** — `Parent:` is hierarchy, not dependency |
| `status/blocked` label only (no body reference at all) | **No** | **No** |

**Note:** `Parent: #N` expresses hierarchy (this is a sub-task of #N), not dependency. A sub-task can be worked on independently even while the parent is open. Only use `Blocked by #N` when the issue genuinely cannot proceed until #N is resolved.

### When you finish a task

**Implementation tasks (PRs):** dependent issues are unblocked automatically by the `unblock-issues` CI workflow when the PR merges. The PR body must include `Closes #N`.

**Non-PR completions:** unblock dependent issues by running `scripts/unblock-dependents.sh <your-issue>`. The script searches for open issues with `Blocked by #<your-issue>`, checks if all blockers are closed, and if so removes `status/blocked`, adds `agent/ready`, and cleans the `Blocked by` lines from the body. Use `--dry-run` to preview changes first.

## Creating Sub-Tasks

If a task naturally breaks into 2+ independent pieces of work, create child issues:

- Reference the parent: "Parent: #42" in the issue body.
- Sub-tasks should be self-contained — another agent should be able to pick one up independently.
- Label child issues appropriately and add `agent/ready` if fully specified.

## Writing Acceptance Criteria

When filing issues, acceptance criteria must be concrete and machine-checkable wherever possible. Vague criteria like "page looks correct" allow agents to hand-wave past verification. Instead, include specific verification commands and expected results.

**Guidelines:**
- **Data changes**: include the SQL query and expected result.
- **Frontend changes**: include the URL and what should be visible (element, text, layout).
- **API changes**: include the endpoint, request, and expected response shape.
- **Behavior changes**: include the specific trigger and expected outcome.

**Example — vague (bad):**
```
- [ ] Zavala v Becker shows only its ruling text
```

**Example — machine-checkable (good):**
```
- [ ] Zavala v Becker shows only its ruling text
  Verify: `SELECT length(ruling_text) FROM rulings WHERE case_id = 'f51849ca-...'` returns values < 5000
  Verify: Screenshot of /cases/f51849ca-... shows single-case content
```

Each criterion should have at least one `Verify:` line that an agent can execute to confirm the criterion is met. This applies to issues filed by both humans and agents. If a verification command is not possible (e.g., requires subjective judgment), note that explicitly so reviewers know it requires manual verification.

## Investigation Tasks

Investigation tasks produce documentation, not code:

- Write findings in the issue body or `docs/investigations/`.
- **Always file follow-up issues** for every actionable finding. Label them `agent/ready` if fully specified. Reference the investigation issue as the parent.
- After documenting findings and filing follow-ups, close the investigation issue unless human judgment is genuinely needed.

## Ingestion Pipeline — Separation of Concerns

The ingestion pipeline has three stages. **Each stage does one job. Do not mix responsibilities.**

| Stage | Responsibility | Does NOT do |
|---|---|---|
| **Capture** (scraper) | Fetch raw content, extract metadata from website structure (link text, HTML headers, URL params), archive to S3 | Parse PDF content, extract fields from unstructured text |
| **Transcription** | Convert raw content to clean text per case, split multi-case documents, mark cross-page continuations | Extract structured fields (case_number, outcome, etc.) |
| **Enrichment** | Extract structured fields from text using three-tier strategy (scraper metadata > LLM > regex) | Fetch content, parse PDFs |

**Transcription** varies by format:
- **HTML** (e.g., LA): BeautifulSoup parsing, no LLM needed.
- **PDF** (e.g., OC, Riverside): multimodal LLM sees page images, returns `ruling_text` per case + `continued` markers. One page per LLM call, join results across pages. Does NOT extract case_number, case_title, outcome, etc. — that's enrichment.

See `docs/specs/architecture-spec-v1.md` Section 5.2 for the full specification.

## Scraper Development Rules

Key paths: framework in `packages/scraper-framework/src/framework/`, California courts in `packages/scraper-framework/src/courts/ca/`.

- **Never run production scraping from dev.** Fetching pages to create real test fixtures is fine.
- Every scraper must implement the base `Scraper` class, report health metrics, and use SHA-256 content hashing.
- Raw content is always archived to object storage before processing.
- Scraper configurations (URLs, selectors, schedules) are separate from scraper logic.
- **Scrapers extract metadata from website structure only** — judge name from link text, department from URL parameters, etc. They do NOT parse PDF content or extract fields from unstructured text. Field extraction from document content happens downstream in enrichment.
- **Field extraction completeness is a hard requirement.** Required fields: **judge name, motion type, case title, hearing date, outcome, parties**. These must be populated by the end of the enrichment pipeline (not necessarily by the scraper). Write regression tests against real fixtures for every field.

## Infrastructure Code

- Terraform for all AWS resources. Every resource must be in a module.
- **Do NOT add `tags` blocks to individual resources** — the AWS provider's `default_tags` handles this.
- Never commit AWS credentials or state files.
- **Dev terraform apply is automated.** After a PR that touches `infra/terraform/` merges to main, the dispatcher automatically runs `terraform apply` for the dev environment. Production applies remain human-only. See `.claude/skills/dispatcher/SKILL.md` for the full apply procedure.

For Terraform apply/deploy details, see `docs/agent/infrastructure-reference.md`.

### Running Data Scripts on Dev

The dev database is in a private VPC — it is **not reachable from localhost**. Never use `scripts/with-secret.sh` with `DATABASE_URL` to run data scripts locally; the connection will fail.

| Tool | Use for | Reliability |
|---|---|---|
| `scripts/ecs-run-task.sh` | **All data scripts** (backfills, migrations, audits, one-off fixups) | Reliable — launches a standalone Fargate task with full VPC access and CloudWatch log streaming |
| `scripts/dev-db-query.sh` | **Quick SQL queries** (SELECT, EXPLAIN) | Good for short queries — uses ECS Exec internally |
| `scripts/ecs-run.sh` | **Interactive debugging only** (e.g., `ecs-run.sh bash`) | Unreliable — SSM sessions drop after seconds, losing all output. Never use for scripts that take more than a few seconds |

**Standard patterns:**

```
# Run a data script on dev (preferred)
scripts/ecs-run-task.sh scripts/backfill_example.py -- --dry-run

# Quick SQL query
scripts/dev-db-query.sh "SELECT COUNT(*) FROM rulings"

# Long-running script: launch and check later
scripts/ecs-run-task.sh --detach scripts/reingest_from_s3.py -- --all
scripts/ecs-run-task.sh --logs <task-arn>
```

For full details and options, see `docs/agent/infrastructure-reference.md` §ECS Script Execution.

## Unattended Operation Patterns

The Critical Rules above cover the most common patterns. For the full reference of permission-prompt workarounds (git, curl, secrets, `.claude/` writes, ECS, Telegram), see `docs/agent/unattended-patterns.md`.

## Session Triggers

### Handling system tags in user message turns

**Task notifications and system reminders are system events, not user responses.** Messages tagged with `<task-notification>` or `<system-reminder>` are injected by the platform — the user did not type them.

When one of these tags arrives and you have a **pending question**: do not treat it as the user's answer. Acknowledge in one line, continue waiting for the user's actual response.

### Telegram Integration (optional)

Telegram integration is opt-in. For full details, see `docs/agent/telegram-reference.md`.

When the user asks to pick up work, invoke `/task` as a background subagent. To enable continuous autonomous work queue management, invoke `/dispatcher`.

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
