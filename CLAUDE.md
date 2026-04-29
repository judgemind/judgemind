# Judgemind — Agent Instructions

**STOP. Read this entire file before doing anything else.** Do not explore the codebase, do not read other files, do not respond substantively to the user's request until you have read this file. This file defines your mandatory workflow — deviating from it is a bug.

## Critical Rules — Read First

**A PreToolUse hook enforces the shell rules automatically.** See `docs/preflight-checklist.md` for the complete machine-readable checklist.

### NEVER — Shell Commands

Shell-interactive prompt-prevention rules (`$()`, heredocs, inline `python -c`, quoted + `&&`/`;`, `bash scripts/`, bare `stash pop`, `.claude/` writes) are hook-enforced on operator laptops and inert on Fargate — see `docs/agent/interactive-shell-rules.md` for the full list with rationale.

- Never use shell `&`, `nohup`, `disown`, or multicommand tricks to background a process. Use the Bash tool's `run_in_background: true` parameter instead.
- Never run bare `git stash pop` or `git stash apply` (#2749). Pop by explicit ref after `git stash list`, or use a throwaway commit instead.

### NEVER — Workflow
- Never use `run_in_background` in any subagent (`/task`, `/ralph`, or Agent-spawned workers). All commands inside subagents run synchronously.
- Never commit directly to `main` during autonomous task work.
- **You MAY merge your own PRs** after `/ralph` and CI are green: `gh pr merge <N> --repo judgemind/judgemind --squash --delete-branch`.
- Never exit or stop after `/ralph` completes without finishing the full `/task` workflow (steps A.3–A.9 in the task skill). Ralph completing means code is ready — not committed, not pushed, not merged. See #721.
- Never deploy to production. Production deploys are human-only.
- Never skip pre-PR checks. Run lint, format, AND tests locally before pushing.
- Never share venvs between worktrees. Each worktree gets its own `.venv`.
- Never create additional worktrees from inside a worktree via `git worktree add`. Fix bad state with `git checkout -- .` / `git clean -fd` rather than creating a new worktree.
- Never close a task or remove a worktree without posting a verification evidence comment on the issue. See §A.8 Step 3.
- Never merge user-visible affordances that haven't been exercised end-to-end. "The page renders" / "the service returns 200" is not evidence. Half-built behind a flag is fine; half-built and reachable by users is the bug.
- Never use `--force` (or `--force-with-lease`) to bypass a failing CI check or pre-push hook. `--force-with-lease` IS allowed after an intentional rebase of your own branch — see the `/task` skill A.4.
- Never run `gh auth switch` without an explicit user instruction.

### ALWAYS — Before Acting
- Read a file before Writing to it (the Write tool fails on existing files you haven't read).
- Pull latest code: `git fetch origin main` then `git rebase origin/main` as separate tool calls before modifying files.
- Use `{worktree}/tmp/` for temp files, never `/tmp/`.
- Use dedicated tools (Read, Glob, Grep) instead of Bash for file operations.
- Watch CI to completion (`gh run watch`) before doing anything else after pushing.
- Create a PR immediately after your first push to a branch.
- Re-fetch GitHub issue or PR state before acting on it if more than a few minutes have elapsed.
- Set `timeout: 1200000` (20 minutes) on Bash commands that may take longer than 2 minutes: `pytest`, `gh run watch`, `terraform apply`, `pip install`, `npm install`, `npm run build`, `ruff check` on large codebases, `scripts/ecs-run-task.sh`, `scripts/ecs-run.sh --script`, `scripts/rebuild_db.sh`, any data-processing script.

## Enforced Rules — Automated Checks

The PreToolUse hook (`.claude/hooks/preflight-bash.sh`) and `scripts/preflight.sh` enforce the Critical Rules above. See `docs/agent/interactive-shell-rules.md` for the full operator-laptop vs. Fargate-runtime scope split.

```bash
scripts/preflight/in-worktree.sh       # Fail if pwd is main repo, not a worktree
scripts/preflight/not-on-main.sh       # Fail if on main/master branch
scripts/preflight/branch-fresh.sh      # Fail if behind origin/main (pass --fetch to fetch first)
scripts/preflight/venv-local.sh        # Fail if .venv is missing or is a symlink
scripts/preflight/no-duplicate-pr.sh N # Check if open PR already exists for issue #N
scripts/preflight/rate-budget.sh       # Warn if GitHub API rate budget < 100 remaining
```

`scripts/preflight.sh` is the umbrella runner / sourceable function library; the per-check wrappers under `scripts/preflight/` (in-worktree.sh, not-on-main.sh, etc.) each invoke one library function for use under permission rules that block `source ... && fn`.

## Project Context

Judgemind is a free, open-source legal research platform replacing Trellis.law. Read the specs in `docs/specs/` for full context. The key things to know:

- **Capture to S3 is the most critical step.** Tentative rulings disappear from court websites within days. If the scraper is down when a ruling is posted, the raw is lost forever — scraper reliability is the top priority.
- **Data is tiered; the schema encodes the tiers.** S3 is the source of truth. PostgreSQL is split by schema namespace:
  - `derived.*` (documents, rulings, cases, judges, attorneys, parties, court_directory_snapshots, aliases) — fully rebuildable from S3 via `rebuild_db.py`. For cleanup or corrupted state, prefer rebuild over surgical deletion/patch scripts.
  - `public.*` (users, refresh_tokens, alert_subscriptions, alert_events) — authoritative accumulated state. Never drop without explicit justification; not rebuildable.
  - `staging.*` (captures, ruled_items) — transient pipeline state. Drain, don't rebuild.
  - `telemetry.*` (scraper_runs, validation_results, data_quality_metrics) — accumulated observability. Low-stakes but not rebuildable from S3.
  - OpenSearch is fully derivable from `derived.*`.
- **Two data pipelines.** Model A captures California tentative rulings (ephemeral at capture, high urgency). Model B extracts judge analytics from dockets/documents (all other states, persistent, NLP-dependent).
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
| `docs/agent/code-standards.md` | Python/TypeScript/Terraform standards, pre-PR check commands, coverage gates |
| `docs/agent/issue-authoring.md` | Writing acceptance criteria, sub-tasks, investigation follow-ups |
| `docs/agent/task-dependencies.md` | Blocking/unblocking issues, `Blocked by` mechanics |
| `docs/agent/infrastructure-reference.md` | ECS script execution, Terraform apply, secrets, Vercel, Reingest vs Rebuild |
| `docs/agent/local-dev.md` | Docker Compose, local DB rebuild, S3 cache, local env vars |
| `docs/agent/unattended-patterns.md` | Permission-prompt workarounds for git, curl, secrets, `.claude/` writes |
| `docs/agent/interactive-shell-rules.md` | Hook-enforced shell NEVERs, operator-laptop vs. Fargate scope |
| `docs/agent/github-api-access.md` | When to use the GitHub MCP server vs the `gh` CLI — MCP-first for reads, `gh` for writes and gaps |
| `docs/agent/gh-to-mcp-migration.md` | Full tool-by-tool `gh` → `mcp__github__*` mapping, including the known gaps |
| `docs/agent/aws-api-access.md` | When to use the AWS MCP servers vs the `aws` CLI vs `scripts/ecs-*.sh` — MCP-first for ECS/CloudWatch reads, scripts for launch-and-stream, CLI for writes/S3/secrets |
| `docs/agent/aws-to-mcp-migration.md` | Full tool-by-tool `aws` → `mcp__awslabs_*` mapping, including the known gaps |
| `docs/agent/spec-authoring.md` | Authoring or restructuring a spec/design doc — Today vs. Direction split rules |

## Starting a New Session

Wait for the user's instruction before deciding what to do. Sessions fall into two modes:

### Interactive sessions (human present)

Interactive sessions are **general-purpose** — the user decides what the session is for. To enable autonomous work queue management, invoke `/dispatcher`. This is opt-in.

### Autonomous sessions (subagent via `/task`)

Subagents do the implementation work: worktree setup, coding, testing, PR, and review. They follow the full PR Workflow below.

### Available Skills

The full skill catalog is injected at session start as the `available-skills` system-reminder; each skill's authoritative description lives in `.claude/skills/<name>/SKILL.md`. The two operator-facing entry points are `/task` (claim and work an issue) and `/dispatcher` (autonomous work queue manager).

### Dispatcher-internal phase skills

The dispatcher v2 daemon decomposes each task into per-phase subagents (`/task-v2-*`, `/dispatcher-*`, `/diagnose-failure`). Operators do not invoke them directly. Full spec: `docs/specs/dispatcher-v2-spec.md`.

### Worktree setup

**Automated (via dispatcher):** The dispatcher spawns `/task` agents with `isolation: "worktree"` on the Agent tool. Claude Code automatically creates a unique worktree at `.claude/worktrees/agent-<id>/`. No locking, no number contention, no races.

Each worktree gets its own `.venv` per package. Use `scripts/install-package-venv.sh` — it creates the venv and installs local sibling dependencies (e.g. `judgemind-config`) in the right order:

```
scripts/install-package-venv.sh <pkg>     # e.g. scraper-framework, nlp-pipeline
```

The raw equivalent: `python3.12 -m venv packages/<pkg>/.venv`, install `packages/judgemind-config` first if `<pkg>` depends on it, then `pip install -e "packages/<pkg>[dev]"`. Plain `pip install -e ".[dev]"` **fails** for `scraper-framework` and `nlp-pipeline` because `judgemind-config` is an unpublished local sibling (see #2491).

### Pick up a task

Use `/task` to claim and work on an issue: `/task`, `/task #42`, or `/task scrapers`.

### Issue author trust check (security gate)

**This is a public repository.** Before working on any issue, the dispatcher and `/task` skill verify the issue author is a trusted collaborator using `scripts/check-issue-author.sh <N>`. Issues filed by non-collaborators are rejected and moved to `status/triage`. Three layers enforce this:

1. **Issue template** — the Task template uses `status/triage`, so new issues require manual labeling.
2. **GitHub Action** (`issue-triage.yml`) — strips `agent/ready` from issues filed by non-collaborators.
3. **Runtime check** — dispatcher and `/task` call `scripts/check-issue-author.sh` before spawning work (fail-closed).

## Collaboration & Judgment

- **Wait for confirmation before acting on proposals.** End the turn after presenting options and wait. Don't file issues, don't start implementing, don't spawn agents until the user responds.
- **Evidence-Based Answers.** Verify before answering factual claims about code, PRs, deploy state, CI status. Use `gh issue view` / `gh pr view`, Read, Grep, `git log`. Soft hedges are fine in exploratory discussion but not as a substitute for verification.
- **Zoom out before planning: root cause vs. symptom.** Before generating a plan or filing issues, ask: do these share a root cause? Would a structural fix prevent the entire class of problem?
- **Root Cause Over Symptoms** (triage, spotcheck, investigation). Before filing a symptom-level ticket, look one level deeper. File one root-cause issue, not three symptom issues.
- **Investigations go to root cause.** When asked to investigate — or when you autonomously decide to — take it all the way down. Build a chain of verified evidence (code read, log line, query result, git history), not supposition. Hold multiple hypotheses in parallel and disprove them with evidence; don't anchor on the first plausible one. Launch multiple subagents in parallel to explore distinct hypotheses concurrently — do not speculate; require proof. Stop only when the chain bottoms out in something concrete.
- **Instrument before you guess.** When a failure's root cause isn't obvious, the first move is to make the failure self-diagnosing — add structured logging, capture the raw state the process actually saw, re-trigger — not to patch the top hypothesis. See docs/agent/investigation-patterns.md for the full pattern.

## PR Workflow

**The authoritative step-by-step lives in `.claude/skills/task/SKILL.md` §A.3–A.9.**

- **Claim interlock (`status/in-progress` label, label-only).** On claim, `/task` adds `status/in-progress` before removing `agent/ready`; removes it on terminal. Issue #2927 replaced the prior DB-row + label interlock (#2866) with this label-only flow.
- **Single-issue rule.** Each PR addresses exactly one issue.
- **All commits on the worktree branch.** Every change goes through a PR.
- **Scope completeness check before implementing.** Grep for all locations affected by the change.
- **Ralph for testable code only** (Python, TypeScript). Non-testable tasks implement directly, then run pre-PR checks and self-review the diff.
- **Check for duplicate PRs** — `scripts/check-duplicate-pr.sh <N>`.
- **CI watch is non-negotiable.** `gh run watch <id> --interval 60 --exit-status --compact`. Fix and re-push until CI is green.
- **Verify `mergeable: MERGEABLE` and `statusCheckRollup` all SUCCESS/SKIPPED before merging.**
- **Verification evidence comment is MANDATORY on every task completion.** For deployed services: curl / DB query / log lines / screenshot. For docs/CI/tooling: state the skip reason.
- **For new user-visible affordances, the evidence must be the affordance exercised** — not "the page loads." A stop button requires log lines showing the kill AND a DB/state snapshot. Rendering is not evidence.
- **Deploy before cleanup.** A task is done when the change is deployed, verified, AND evidence posted.
- **Never ask for user confirmation during the substeps.** Just execute.

## Tool Use Rules

- **Use dedicated tools for file operations** — never use Bash for `cat`, `ls`, `grep`, `find`. Use Read, Glob, and Grep instead.
- **Always Read before Write** — the Write tool requires this for existing files.
- **Prefer Edit over Write for existing files.** Edit sends only the diff. Write sends the entire file content — a 60KB file is ~20K output tokens. Reserve Write for new files or full rewrites.
- **Don't re-Read a file you already Read in full.**
- **Use Bash only for shell-only operations** — git, gh CLI, running tests, pip install, terraform, etc.
- **GitHub reads: MCP for single objects, `gh --json` for lists and wide rollups.** See `docs/agent/github-api-access.md`.
- **GitHub writes: `gh` CLI** (`gh issue create`, `gh pr create`, `gh pr merge --squash --delete-branch`, etc.). See `docs/agent/gh-to-mcp-migration.md`.
- **AWS reads: MCP-first for ECS + CloudWatch.** See `docs/agent/aws-api-access.md`. `scripts/ecs-*.sh` wrappers stay for launch-and-stream-logs. `aws` CLI for writes, S3, and interactive Exec. Never call `aws secretsmanager get-secret-value` directly — use `scripts/with-secret.sh`.
- **Deferred tools require schema loading.** GitHub MCP, AWS MCP, `TodoWrite`, `Monitor`, `EnterWorktree`/`ExitWorktree`, `WebFetch`/`WebSearch`, and `Cron*` appear by name only in `<system-reminder>` messages — call them directly and you'll hit `InputValidationError`. Load with `ToolSearch query="select:Name1,Name2"` before first invocation.
- **Parallelize independent Bash calls** to reduce wall-clock time.
- `sudo` and `rm` always prompt; split commands to avoid triggering prompts.

For detailed patterns to avoid permission prompts, see `docs/agent/unattended-patterns.md`.

### GitHub API Rate Limit Awareness

GitHub allows 5,000 API requests per hour. Always use `--interval 60` with `gh run watch`. Never retry 403 errors in a tight loop.

## Accounts & Infrastructure

**GitHub:** org `judgemind/judgemind`. **AWS:** account `155326049300`, region `us-west-2`.

For detailed infrastructure reference (Vercel, Terraform state, ECS, secrets), see `docs/agent/infrastructure-reference.md`.

## Code Standards & Pre-PR Checks

See **`docs/agent/code-standards.md`** for the full reference. Highlights:

- **Python:** 3.12+, `.venv` per package, ruff + pytest. Scripts in `scripts/` need a `# venv:` header plus exactly one of `# one-off: true` or `# permanent: true`. ECS oneshot scripts cannot import from other `scripts/*.py` files.
- **TypeScript:** strict mode, Node 20+. In `packages/web/`, use `@/` path aliases; any new GraphQL type without `id` needs a `keyFields` entry in `apollo-client.ts`.
- **General:** all code has tests. Never hardcode secrets. Grep every import site before removing/renaming exports.
- **Perf:** watch for sequential I/O, `LIMIT/OFFSET` pagination, unbatched DB writes, missing connection reuse.
- **Pre-PR (MANDATORY, `.githooks/pre-push` enforces):** from each touched package, run `ruff check`, `ruff format --check`, and `pytest` (Python) or `lint`/`typecheck`/`test` + `build` for `packages/web/` (TS). Diff coverage ≥ 90%; package floor ratchet in `coverage-baselines.json`.
- **Docs / Markdown:** run `scripts/check-markdown-links.sh` when any `.md` file changes.
- **CI workflow edits:** run `scripts/check-ci-job-skipped.sh` when `.github/workflows/ci.yml` changes.
- **Hygiene-check CI steps:** when wiring a `scripts/check-no-*.sh` / `check-forbidden-*.sh` / `check-deprecated-*.sh` guard into CI, do not quote the forbidden string in the step's `name:` field. See `docs/agent/code-standards.md` §Hygiene-check CI steps.

### Subagent Responsibilities

**Spawn `/task` agents with `isolation: "worktree"` on the Agent tool.** Claude Code creates a unique worktree at `.claude/worktrees/agent-<id>/` automatically. **Never run `git checkout` or `git switch` in the parent's working directory from a subagent.**

## Git Workflow

- Commit messages follow conventional commits: `feat(scraping): implement OC PDF link scraper (#42)`
- Always work on the worktree branch. Open a PR, wait for CI. You may merge your own PRs after ralph and CI are green.
- **A PR is not done until it has no conflicts and CI is green.**

## Task Dependencies, Sub-Tasks, Investigations

See **`docs/agent/task-dependencies.md`** and **`docs/agent/issue-authoring.md`** for the full mechanics. Core rules:

- **Blocking:** use `scripts/block-issue.sh <issue> <blocker>`. Both the `status/blocked` label AND a `Blocked by #N` line in the issue body are required. Label-only blocks never auto-unblock. `Parent: #N` is hierarchy, not a dependency.
- **Unblocking:** PR merges auto-unblock via `Closes #N`. For non-PR completions, run `scripts/unblock-dependents.sh <your-issue>`. For manual unblock of a single issue (rather than auto-unblock of all dependents of a closed issue), use `scripts/unblock-issue.sh <N>` — never bare `gh issue edit --remove-label status/blocked`.
- **Sub-tasks:** reference the parent as `Parent: #N`; each sub-task should be independently pickup-able.
- **Acceptance criteria:** concrete and machine-checkable. Each criterion has at least one `Verify:` line. **Data cleanup on `derived.*` defaults to `rebuild_db.py --county <name>`** — surgical scripts are a last resort.
- **Investigation tasks:** produce documentation and file follow-up issues for every actionable finding, then close.
- **Priority:** p1 = time-sensitive or workflow accelerators. p2 = most user-facing bugs, backfills, refactoring. p3 = large slow work. p0 is reserved for true emergencies; agents may set it only when explicitly told by a human.

## Ingestion Pipeline & Scraper Development

See **`docs/specs/architecture-spec-v1.md` §3.3** for the full ingestion pipeline (three-stage: Capture → Transcription → Enrichment) and §3.3.3 for scraper development constraints (archive-first, SHA-256 hashing, data correctness > completeness, required fields, regression tests). Key paths: framework in `packages/scraper-framework/src/framework/`, California courts in `packages/scraper-framework/src/courts/ca/`.

## Infrastructure & Data Scripts

### Terraform

- Terraform for all AWS resources. Every resource must be in a module.
- **Do NOT add `tags` blocks to individual resources** — the AWS provider's `default_tags` handles this.
- Never commit AWS credentials or state files.
- **Always run `terraform init` with `-lockfile=readonly`** for agent-side validation (#2582). See `docs/agent/code-standards.md` §Terraform.
- **Dev terraform apply is automated.** The `dev-apply` job in `.github/workflows/terraform.yml` runs `terraform apply -auto-approve` against `environments/dev` on every `push:main` that touches `infra/terraform/**`, once the plan job succeeds. Production applies remain human-only — there is no apply job for `environments/production/`. See `docs/agent/infrastructure-reference.md` §Terraform for the troubleshooting flow when the workflow fails.

### Running Data Scripts on Dev

The dev database is in a private VPC — **not reachable from localhost**. Use:

- `scripts/ecs-run-task.sh` for **all data scripts** (backfills, migrations, audits, one-offs).
- `scripts/dev-db-query.sh` for **quick SQL queries** (SELECT, EXPLAIN).
- `scripts/ecs-run.sh` for **interactive debugging only** — SSM sessions drop; never use for scripts.

See `docs/agent/infrastructure-reference.md` §ECS Script Execution for full patterns. For Reingest vs Rebuild guidance, see `docs/agent/infrastructure-reference.md` §Reingest vs Rebuild.

### Local Development

Docker Compose stack (Postgres, Redis, OpenSearch, MinIO) — see **`docs/agent/local-dev.md`**.

## Unattended Operation Patterns

For the full reference of permission-prompt workarounds (git, curl, secrets, `.claude/` writes, ECS, Telegram), see `docs/agent/unattended-patterns.md`.

## Session Triggers

### Handling system tags in user message turns

**Task notifications and system reminders are system events, not user responses.** Messages tagged with `<task-notification>` or `<system-reminder>` are injected by the platform — the user did not type them.

When one of these tags arrives and you have a **pending question**: do not treat it as the user's answer. Acknowledge in one line, continue waiting for the user's actual response.

### Telegram Integration (optional)

Telegram integration (optional, opt-in) is delivered via the `plugin:telegram` MCP plugin when installed. Agents never invoke `/telegram:access` or edit `.claude/telegram/`.

When the user asks to pick up work, invoke `/task` as a background subagent. To enable continuous autonomous work queue management, invoke `/dispatcher`.

## Improving the Agent Workflow

At the end of every implementation loop, consider workflow friction that could be improved. File a `type/dx` issue and kick off a background subagent to fix it. Look for: avoidable permission prompts, repeated manual steps, missing preflight checks, unclear CLAUDE.md rules.

When you encounter a prompt for a safe command, work around it immediately using the patterns in `docs/agent/unattended-patterns.md`. File a GitHub issue to track the improvement. Do **not** file issues for intentional prompts (push, PR, merge, deploy).

## Memory and Instructions Updates

- Prefer updating `CLAUDE.md` in the repo root over writing to `~/.claude` project memory.
- Only use local `~/.claude` memory for things that cannot go in the repo.
- For `.claude/` writes (skill SKILL.md files, hook configs, etc.) where the platform blocks the Write/Edit tools, use `scripts/write-claude-file.sh`. From inside a worktree the script also enforces worktree scope to prevent cross-worktree writes (#3345).

## Additional Prohibitions

- Do not make architectural decisions that contradict `docs/specs/` without filing a `type/decision` issue.
- Do not add dependencies without justification in the PR description.
