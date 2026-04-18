---
description: (WIP — dispatcher v2 spike 0.3 stub) Verify phase for the per-phase /task-v2 pipeline. Reads merged PR + deploy status + AC list, produces a verification-evidence comment with per-criterion proof.
argument-hint: ""
maxTurns: 40
model: opus
---

# /task-v2-verify skill (WIP stub)

**Status:** WIP — extracted from `.claude/skills/task/SKILL.md` A.8 (deploy verification + evidence comment) for dispatcher v2 spike 0.3.

**Goal:** After the daemon watches the deploy workflow to SUCCESS, this skill runs functional verification against dev and produces the per-criterion evidence comment the daemon will post on the issue.

**Input:** `{worktree}/tmp/dispatcher-input/verify.json`:
- `issue_number` (int)
- `pr_number` (int)
- `acceptance_criteria` (list of str) — from the plan
- `change_type` (str) — `api`, `scraper`, `ingestion`, `web`, `db_migration`, `dx_tooling`, `backfill_script`, `no_deployed_component`
- `touched_services` (list of str) — ECS service names / URL endpoints / DB tables affected
- `worktree_path` (str)
- `repo_root` (str)

**Output:** `{worktree}/tmp/dispatcher-output/verify.json`:
- `verdict` (str) — `VERIFIED`, `FAILED`, `SKIPPED`
- `evidence_md` (str) — the markdown comment to post on the issue
- `per_criterion_results` (list of `{criterion, verified, evidence}`)
- `failure_reason` (str or null) — if verdict=FAILED, what was broken

---

## Step 1 — Determine verification path

Use the change_type to pick the verification strategy:

| change_type | Verification approach | Required evidence |
|---|---|---|
| `api` | `curl` against `dev.api.judgemind.org`, confirm expected response | status code + body snippet |
| `scraper` | Read ECS logs via `scripts/ecs-logs.sh /ecs/judgemind-scraper-dev --lines 50` | log lines showing successful capture |
| `ingestion` | Read ECS logs `/ecs/judgemind-ingestion-worker-dev` | log lines showing successful processing |
| `web` | Fetch page content from `dev.judgemind.org` | rendered HTML / screenshot |
| `db_migration` | `scripts/dev-db-query.sh` to confirm column/table exists | DB query output |
| `backfill_script` | Run script via `scripts/ecs-run-task.sh`, verify data changes | row counts / sample records |
| `dx_tooling` | Run the tool in a representative scenario | command output |
| `no_deployed_component` | Skip functional verification — go directly to Step 3 with verdict=SKIPPED | n/a |

## Step 2 — Per-criterion verification

For each acceptance criterion, pick the right verification action and capture concrete evidence:
- **Frontend criteria**: screenshot via `scripts/run-py.sh scripts/screenshot.py <url>`, or fetch page content.
- **Data criteria**: run the specific SQL query (prefer `scripts/dev-db-query.sh`).
- **API criteria**: hit the specific endpoint and confirm response.
- **Behavior criteria**: trigger the scenario and capture result.

If any criterion fails to verify, set `verdict=FAILED` and include the diagnostic output in `failure_reason`.

## Step 3 — Build the evidence comment

For verdict=VERIFIED:

```
## Verification Evidence

**Change type:** <change_type>
**Environment:** dev

**Deployment health:**
<curl/log/DB query output>

**Acceptance criteria verification:**

| # | Criterion | Verified | Evidence |
|---|-----------|----------|----------|
| 1 | <criterion> | Yes | <proof> |
| 2 | <criterion> | Yes | <proof> |

Post-deploy verification: PASSED
```

For verdict=SKIPPED (no deployed component):

```
## Verification Evidence

**Change type:** <change_type>
**Skip reason:** No deployed component — <docs/CI/tooling only, etc.>

Post-deploy verification: N/A (no deployed component)
```

For verdict=FAILED:

```
## Verification Evidence — FAILED

**Change type:** <change_type>
**Failure:** <failure_reason>

<diagnostic output>

Post-deploy verification: FAILED — daemon will file priority/p1 follow-up issue.
```

## Reminders

- Do not post the comment — the daemon does that after reading this skill's output.
- Prefer MCP for AWS reads (`mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query`) over shelling out to `aws logs`.
- No `$()`, no heredocs. All outputs go in the JSON file.
