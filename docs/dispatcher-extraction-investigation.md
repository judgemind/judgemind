# Dispatcher Extraction Investigation

**Status:** Investigation only. No work scheduled.
**Date:** 2026-04-23

## Motivation

Dispatcher v2 PRs account for ~25% of recent commits to `judgemind/judgemind` (142 of 566 in the 30 days preceding 2026-04-23). The dispatcher is increasingly its own project — orchestration logic, agent-skill design, daemon reliability — distinct from the legal-research product work. This document captures the coupling analysis and proposed extraction architecture should we choose to split the dispatcher into its own repo (`judgemind-dispatcher`).

## Scope

Extraction would cover **dispatcher v2 only**. The legacy `/task` and `/dispatcher` (v1) skills stay in `judgemind/judgemind`, frozen — they remain useful as a backup but no longer receive active development. This dramatically reduces the migration surface area, since the v1 skills are where the deepest hardcoded judgemind coupling lives.

## Today: Current Coupling

### Python boundary

`scripts/dispatcher/` (63 Python files, ~61k LOC including helpers) has **zero imports from any `packages/*`**. The dispatcher engine is already a clean Python boundary.

### Hardcoded `judgemind` references in the daemon

`scripts/dispatcher/daemon.py` (19,294 lines) contains only **9 hardcoded `judgemind` references**, all in named constants or dict literals:

- `DEFAULT_GITHUB_REPO = "judgemind/judgemind"` (line 204)
- `BASELINE_CLONE_URL = "https://github.com/judgemind/judgemind.git"` (line 654)
- Deploy-workflow-to-ECS-service map (line ~12885): `{"Deploy API": "judgemind-api-dev", "Deploy Dispatcher": "judgemind-dispatcher-dev", "Deploy Scraper": "judgemind-scraper-dev"}`
- PR URL format string `https://github.com/judgemind/judgemind/pull/...`
- Service name pattern `judgemind-dispatcher-<env>` (the dispatcher's *own* ECS service)
- Comments and docstrings

The other v2 dispatcher Python modules (`phases.py`, `task_claim.py`, `phase_transitions.py`, `iteration_feedback.py`, `stream_forwarder.py`, `emit_failure.py`) have **zero hardcoded `judgemind` references**.

### Skill coupling

| Skill | Lines | judgemind-token mentions | Hardcoded `--repo judgemind/judgemind` | Verdict |
|---|---|---|---|---|
| `tdd` | 157 | 5 (mostly examples) | 0 | Already generic |
| `ralph` | 576 | 4 (one prose paragraph) | 0 | Already generic |
| `task-v2-fix-ci` | 178 | 0 | 0 | Already generic |
| `task-v2-retro` | 218 | 1 | 0 | Already generic |
| `task-v2-plan` | 189 | 8 | 0 | Mostly generic |
| `task-v2-summary` | 322 | 9 | 1 | Mostly generic |
| `task-v2-ralph` | 515 | 10 | 2 | Mostly generic |
| `task-v2-verify` | 242 | 10 | 0 | Generic logic, judgemind verification recipes |
| `diagnose-failure` | 443 | 11 | 4 | Mostly generic |
| `file-issue` | 126 | 14 | 7 | Conventions doc — repo-specific by design |
| `dispatcher-startup` | 193 | 8 | several | Mostly generic |
| `task` (v1, legacy) | 847 | 54 | **25** | Stays in judgemind |
| `dispatcher` (v1, legacy) | 1007 | 18 | 9 | Stays in judgemind |
| `audit` | 481 | 15 | 4 | Stays in judgemind |
| `spotcheck` | 296 | many | many | Stays in judgemind |
| `screenshot` | 89 | many | many | Stays in judgemind |

### Helper-script dependencies

V2 skills reference these consumer-repo helper scripts:

- `scripts/check-issue-author.sh` — invoked by `dispatcher-startup` (security gate)
- `scripts/install-package-venv.sh` — referenced by `task-v2-plan` and `ralph`
- `scripts/with-secret.sh` — referenced by `task-v2-verify` (advice text) and `task-v2-fix-ci` (error-message text)
- `scripts/preflight.sh` — referenced as guidance

The runtime architecture handles these naturally: the daemon clones `judgemind/judgemind` into `/var/lib/dispatcher/judgemind` at boot and creates per-task worktrees. Agents always run **inside the consumer's cloned worktree**, so `scripts/*` references resolve against the consumer repo, not the engine.

The single exception is `scripts/check-issue-author.sh`, which `dispatcher-startup` invokes before a worktree exists — but the baseline clone is the consumer repo, so the script is reachable from there.

### Infrastructure

- Two dedicated Terraform modules: `infra/terraform/modules/dispatcher-daemon/`, `infra/terraform/modules/dispatcher-agent-runner/`. Wired in `infra/terraform/environments/dev/main.tf`.
- Two Dockerfiles: `Dockerfile.dispatcher`, `Dockerfile.dispatcher-agent-runner`.
- Two deploy workflows: `.github/workflows/deploy-dispatcher.yml`, `.github/workflows/deploy-agent-runner.yml`.
- ECR repositories for the two images.

## Direction: Proposed Architecture

### Repository split

**`judgemind-dispatcher` (new repo) — owns:**

- `scripts/dispatcher/` (daemon, phases, helpers, tests)
- `.claude/skills/`: `task-v2-{plan,ralph,fix-ci,summary,verify,retro}`, `ralph`, `tdd`, `diagnose-failure`, `file-issue`, `dispatcher-startup`
- `infra/terraform/modules/dispatcher-daemon/`
- `infra/terraform/modules/dispatcher-agent-runner/`
- `Dockerfile.dispatcher`, `Dockerfile.dispatcher-agent-runner`
- `.github/workflows/deploy-dispatcher.yml`, `.github/workflows/deploy-agent-runner.yml`
- `dispatcher.sh`
- `docs/specs/dispatcher-v2-spec.md` and dispatcher-relevant `docs/agent/*` files

**`judgemind/judgemind` (stays) — keeps:**

- All `packages/`, all of `scripts/` except `scripts/dispatcher/`
- `.claude/skills/`: `task` (legacy), `dispatcher` (legacy), `audit`, `spotcheck`, `screenshot`
- `CLAUDE.md`, retaining its dispatcher-contract sections (those describe what the consumer agrees to, not what the engine does)
- The verification recipes that v2-verify consumes

### Config interface

Small consumer-side config file consumed by the daemon at boot from the baseline clone:

```yaml
# .dispatcher/config.yaml in the consumer repo
repo: judgemind/judgemind
clone_url: https://github.com/judgemind/judgemind.git
agent_account: drewthaler
deploy_workflow_service_map:
  "Deploy API": judgemind-api-dev
  "Deploy Scraper": judgemind-scraper-dev
  "Deploy Dispatcher": judgemind-dispatcher-dev  # optional — only if consumer also deploys the dispatcher
verification_recipes_path: docs/agent/verification-recipes.md
```

That is roughly the entire interface. The daemon's existing constants become config lookups.

### Runtime model (unchanged)

The dispatcher engine clones the consumer repo, creates per-task worktrees, and bakes the v2 skills into the agent-runner Docker image. The agent runs in the consumer worktree with engine-provided skills. This symmetry already exists today.

## Risks

1. **Self-deploy loop.** The dispatcher currently watches `Deploy Dispatcher` runs in `judgemind/judgemind`. After the split, dispatcher PRs land in `judgemind-dispatcher` and `deploy-dispatcher.yml` runs there. The daemon's deploy-watcher needs to watch *its own* repo for its deploy and the *consumer* repo for all other workflows. Small refactor of the watch loop.
2. **Agent-runner image symmetry.** The agent-runner image bundles the v2 skills. Today the build is in `judgemind/judgemind`; after the split, the build is in `judgemind-dispatcher` and the consumer repo is cloned at boot. The path coupling already works that way — should be straightforward.
3. **Cross-repo PR coordination for protocol changes.** When a v2 phase contract changes (e.g. plan output schema), both repos may need synchronized updates. Avoidable by keeping the contract narrow, but real.

## Effort Estimate

**3-5 days of focused work** for someone who knows both sides:

- Repo bootstrap, CI, Terraform pipeline (~1 day)
- File migration, config-interface implementation, daemon constant → config refactor (~1-2 days)
- End-to-end validation: dispatcher in new repo successfully runs a `/task-v2-*` cycle and lands a green PR in `judgemind/judgemind` (~1 day)
- Documentation, deploy cutover, cleanup (~1 day)

The biggest contributor to the low estimate: **legacy `/task` (847 lines, 25 hardcoded refs) and legacy `/dispatcher` (1007 lines, 9 refs) do not need to migrate.** They stay frozen in `judgemind/judgemind`. This eliminates the deepest coupling.

## Open Questions

- Should `judgemind-dispatcher` be public from the start (eventually usable for non-judgemind projects), or private until proven?
- How should the v2 phase contract evolve when both repos may need coordinated changes? RFC process? Versioned phase outputs?
- Does the verification-recipes registry belong in the consumer repo (per-project recipes) or as a generic format the engine consumes (e.g. `verify_command:` per workflow)?
