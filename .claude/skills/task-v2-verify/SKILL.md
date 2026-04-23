---
description: Verify phase for the per-phase /task-v2 pipeline. Reads the merged PR plus deploy status plus acceptance criteria, produces a verification-evidence comment with per-criterion proof against dev.
argument-hint: "<agent-id>"
maxTurns: 50
model: haiku
---

# /task-v2-verify skill

Verify phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Invoked by the daemon after the deploy workflow finishes green. Runs functional verification against dev and produces the per-criterion evidence comment the daemon will post on the issue (mandatory per `CLAUDE.md` §PR Workflow — every task completion requires concrete evidence or an explicit skip reason).

**Prerequisites:** The dispatcher daemon has already (a) merged the PR, (b) watched the deploy workflow (when applicable) to SUCCESS, (c) written the input bundle to `{worktree}/tmp/dispatcher-input/verify.json`.

**Goal:** Produce `{worktree}/tmp/dispatcher-output/verify.json` with verdict=`VERIFIED`, `SKIPPED`, or `FAILED`, and the evidence markdown the daemon will post on the issue.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation.

**IMPORTANT — Heartbeat lines (issue #3017).** Emit two distinctive heartbeat lines to stdout so CloudWatch Log Insights can answer "what was the last thing the skill did before its stream went silent?" during a hang. Run the Bash tool with:

- `echo PHASE_START verify` immediately after reading this SKILL.md (before Step 1).
- `echo PHASE_DONE <verdict>` right before writing the output JSON (Step N — the last step), where `<verdict>` matches the verdict/go field the output JSON will carry.

These are plain `echo` statements — the dispatcher daemon's stream-forwarder (`scripts/dispatcher/stream_forwarder.py`) picks them up from subprocess stdout and tags them with `agent_id`, `issue_number`, `phase=verify`, `stream=stdout` in CloudWatch + a real-time JSONL mirror at `{worktree}/.dispatcher/verify-<agent_id>.jsonl`. The grep-friendly `PHASE_START` / `PHASE_DONE` tokens make it trivial to filter phase boundaries: `filter @message like /PHASE_START/`.

**IMPORTANT — Never deploy to production.** Verification runs against dev only. Production deploys are human-only per `CLAUDE.md`.

**IMPORTANT — Post the comment? No.** The daemon posts the comment after reading this skill's output. This skill does not call `gh issue comment` — it only produces the markdown.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/verify.json`. Required fields:

- `agent_id` (str).
- `issue_number` (int).
- `pr_number` (int).
- `acceptance_criteria` (list of str) — from plan output or extracted from issue body.
- `change_type` (str) — one of `api`, `scraper`, `ingestion`, `web`, `db_migration`, `dx_tooling`, `backfill_script`, `docs`, `agent_skill`, `no_deployed_component`.
- `touched_services` (list of str) — ECS service names, URL endpoints, or DB tables affected. Examples: `judgemind-api-dev`, `https://dev.api.judgemind.org/api/rulings`, `derived.documents`.
- `deploy_status` (object) — `{workflow_name, run_id, conclusion, duration_s}` for the deploy run, or `null` if no-deploy change.
- `merged_commit_sha` (str).
- `worktree_path` (str).
- `repo_root` (str).

Optional:

- `plan_text` (str) — from plan output; can clarify ambiguous criteria.
- `scope_check` (list) — for context on what's intentionally out of scope.
- `deferred_acs` (list) — carried forward from summary's output (`dispatcher.phase_outputs`). Shape: `[{"index": <int>, "reason": "marker" | "heuristic", "verify_instruction": "<Verify: line text>"}]`. When present, this skill runs the deferred ACs FIRST and labels each result as "deferred (marker|heuristic) → pass|fail"; the remaining ACs are labeled as "pre-merge validated, re-confirmed post-deploy". See [spec §6a `^summary-deferred-acs`](../../../docs/specs/dispatcher-v2-spec.md) and issue #3010. Absent or empty on pre-#3010 agents and on no-deploy/docs change types — the skill treats the verification universe as "every AC, no labeling" in that case.

If the file is missing or malformed, exit 0 with verdict=`FAILED, failure_reason="input JSON missing or malformed"`.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/verify.json`:

```
{
  "agent_id": "<echo>",
  "issue_number": <int>,
  "pr_number": <int>,
  "verdict": "VERIFIED" | "SKIPPED" | "FAILED",
  "change_type": "<echo>",
  "evidence_md": "<full markdown comment body the daemon will post>",
  "per_criterion_results": [
    {"criterion": "<text>", "verified": true|false, "evidence": "<1-3 lines of proof>"}
  ],
  "failure_reason": null | "<string>",
  "unblock_issues": [<int>, ...]
}
```

`unblock_issues` is a list of issue numbers this completion unblocks — the daemon passes them to `scripts/unblock-dependents.sh`. Typically empty unless the issue body contains `Unblocks: #N` references.

Exit 0 regardless. Verdict comes from JSON.

---

## Step 1 — Determine verification path

Use `change_type` to pick the verification strategy:

| change_type | Verification approach | Required evidence |
|---|---|---|
| `api` | `curl -fsS https://dev.api.judgemind.org/<endpoint>` — confirm expected response | status code + body snippet (first 500 chars) |
| `scraper` | Read ECS logs via `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` against `/ecs/judgemind-scraper-dev` | log lines showing successful capture of the relevant court |
| `ingestion` | Read ECS logs via MCP CloudWatch against `/ecs/judgemind-ingestion-worker-dev` | log lines showing successful document processing (plus sample downstream DB query confirming the row) |
| `web` | Fetch a rendered page from `https://dev.judgemind.org/<path>`, or screenshot via `scripts/run-py.sh scripts/screenshot.py <url>` | rendered HTML or screenshot filepath |
| `db_migration` | `scripts/dev-db-query.sh` to confirm the column/table/constraint exists and the schema matches | DB query output |
| `backfill_script` | The daemon or prior step ran the script via `scripts/ecs-run-task.sh`. Verify row counts / sample records via `scripts/dev-db-query.sh` | before/after row counts, or sample rows |
| `dx_tooling` | Run the tool in a representative scenario (e.g. invoke the new script on a known input) | command output demonstrating expected behavior |
| `docs` | No functional verification possible. `verdict=SKIPPED` with reason. Optional: confirm `scripts/check-markdown-links.sh` passed in CI | n/a |
| `agent_skill` | No runtime verification. Confirm the skill file is present on `main`, has production-ready frontmatter (not a stub marker), and (if feasible) a dry `claude -p /<skill> <fixture>` produces non-empty output | file-presence + content-shape check |
| `no_deployed_component` | `verdict=SKIPPED` with reason | n/a |

If `deploy_status` is `null` or `conclusion != 'success'` and `change_type` is deploy-requiring, set `verdict=FAILED` with `failure_reason="deploy did not reach SUCCESS: <conclusion>"`. Do not run functional checks against stale code.

## Step 2 — Per-criterion verification

Order matters: **run the `deferred_acs` first** (they are the ACs summary intentionally skipped pre-merge — verifying them is the point of this phase), then run the non-deferred ACs as a belt. Within each group, execute the verification action that the criterion's `Verify:` line names (per `docs/agent/issue-authoring.md`) and capture concrete evidence into `per_criterion_results[].evidence`.

### 2a — Run deferred ACs first (issue #3010)

If `deferred_acs` is non-empty in the input bundle:

1. For each entry `{index, reason, verify_instruction}`:
   - Look up the AC text in `acceptance_criteria[index - 1]` (1-based → 0-based index).
   - Execute the verification using `verify_instruction` as the primary source for the action (summary already extracted the exact `Verify:` line).
   - Record the result in `per_criterion_results` with a label that surfaces the deferred classification: set `evidence` to begin with `"deferred (marker) → pass:"` or `"deferred (heuristic) → pass:"` followed by the concrete proof. Use `"deferred (marker) → fail: <why>"` when the verification fails.
2. A `fail` on a deferred AC promotes the overall `verdict` to `FAILED` with a `failure_reason` that calls out the deferred AC by index (e.g. `"deferred AC #5 (post-deploy OpenSearch count) did not match expected after deploy"`).

### 2b — Run non-deferred ACs as a belt

For each AC NOT in `deferred_acs`, re-run the verification against the deployed environment. Summary already validated these against the pre-merge diff; the belt run confirms the deployed code still passes. Prefix each `evidence` with `"pre-merge validated, re-confirmed post-deploy:"` so operators reading the PR trail can see which verifications were time-shifted vs. redundantly re-run.

**Default: 100% coverage.** The post-deploy evidence comment covers every AC. If a non-deferred AC is genuinely impossible to re-verify post-deploy (rare — e.g. a static analysis gate that only runs in CI), record `evidence` as `"pre-merge validated by summary (CI-only check, not re-runnable post-deploy): <summary's original evidence>"` and keep `verified=true`.

### 2c — Per-AC verification rules (applies to 2a and 2b)

- **Frontend criteria** ("Verify: page renders without errors") — screenshot or page-fetch. Evidence = screenshot filepath or first 500 chars of rendered HTML.
- **Data criteria** ("Verify: SELECT count(*) FROM derived.documents WHERE …") — run the exact SQL via `scripts/dev-db-query.sh`. Evidence = the query + result.
- **API criteria** ("Verify: curl returns 200 with field `x`") — run the curl. Evidence = status code + relevant body fragment.
- **Behavior criteria** ("Verify: ingestion worker processes a sample fixture without errors") — trigger the scenario (or find a recent occurrence in logs) and capture result.
- **Log criteria** — MCP CloudWatch Logs Insights query; capture the matching log line(s). Limit to 5-10 lines per criterion to keep the evidence comment readable.

If any criterion fails to verify, record `verified=false` in that row, and set the overall `verdict=FAILED` with `failure_reason` summarizing which criteria failed (deferred or not).

## Step 3 — Build the evidence comment

For `verdict=VERIFIED`:

```
## Verification Evidence

**Change type:** <change_type>
**Environment:** dev
**PR:** #<PR-N> merged at <sha>
**Deploy workflow:** <workflow_name> run <run_id> — SUCCESS in <duration>

### Deployment health

<curl output / log lines / DB query output demonstrating the service is live after deploy>

### Acceptance criteria verification

| # | Criterion | Verified | Evidence |
|---|-----------|----------|----------|
| 1 | <criterion> | Yes | deferred (marker) → pass: curl dev.api.judgemind.org/... returned 200 |
| 2 | <criterion> | Yes | deferred (heuristic) → pass: OpenSearch index count matches expected 2,444 |
| 3 | <criterion> | Yes | pre-merge validated, re-confirmed post-deploy: pytest test_foo still green against merged sha |

**Post-deploy verification: PASSED**
```

Each row's `Evidence` column begins with one of three labels so a PR-trail reader can see which verifications were time-shifted from pre-merge (per spec §6a `^verify-deferred-acs`):

- `deferred (marker) → pass:` — AC was tagged `(post-deploy)` by its author; summary skipped it pre-merge; verify ran it now.
- `deferred (heuristic) → pass:` — AC matched the heuristic pattern; summary skipped it pre-merge; verify ran it now.
- `pre-merge validated, re-confirmed post-deploy:` — summary validated against the diff, verify re-ran against the deployed environment as a belt.

For `verdict=SKIPPED` (no deployed component):

```
## Verification Evidence

**Change type:** <change_type>
**PR:** #<PR-N> merged at <sha>

**Skip reason:** No deployed component — <docs / CI / tooling / agent config, specify which>

### Acceptance criteria

| # | Criterion | Status |
|---|-----------|--------|
| 1 | <criterion> | Merged to main — file present |
| 2 | <criterion> | Merged to main — <description of static verification> |

**Post-deploy verification: N/A (no deployed component)**
```

For `verdict=FAILED`:

```
## Verification Evidence — FAILED

**Change type:** <change_type>
**PR:** #<PR-N> merged at <sha>
**Deploy workflow:** <workflow_name> run <run_id> — <conclusion>

**Failure:** <failure_reason>

### Diagnostic output

```
<log lines / curl error / DB query output showing the failure>
```

### Acceptance criteria verification

| # | Criterion | Verified | Evidence |
|---|-----------|----------|----------|
| 1 | <criterion> | No | <why it failed> |

**Post-deploy verification: FAILED — daemon will file priority/p1 follow-up issue for rollback or hotfix.**
```

Keep the comment under ~8000 characters. For long log dumps, truncate to the most relevant 20-30 lines.

## Step 4 — Populate unblock_issues

Check the issue body for `Unblocks: #<N>` lines (rare but useful for the spec's dependency graph). Add each to `unblock_issues`. The daemon runs `scripts/unblock-dependents.sh <issue_number>` after this phase, which uses GitHub's `Closes #N` + `Blocked by` mechanics — `unblock_issues` is an explicit override for cases where the automatic path misses.

## Step 5 — Write the output JSON

Emit `{worktree}/tmp/dispatcher-output/verify.json`. Exit 0.

---

## What this skill does NOT do

- **Does not post the comment.** Daemon does that after reading this output.
- **Does not deploy anything.** Daemon ran the deploy watch; this skill only verifies the already-deployed code.
- **Does not close the issue.** The PR merge auto-closes via `Closes #N`. If verdict=`FAILED`, the issue is NOT reopened by this skill — the daemon decides whether to file a rollback/hotfix issue.
- **Does not run backfills or data rewrites.** If verification reveals data needs repair, this skill reports that; a human or a follow-up task does the repair.

## Verification quality notes

- **Correctness > completeness** per `CLAUDE.md`. If the deployed code returns a field, check it against the source document / fixture — do not just confirm the field is populated.
- **Use MCP first** for AWS reads: `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` is faster and cleaner than shelling out to `aws logs`. Load via `ToolSearch query="select:mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query,mcp__awslabs_cloudwatch-mcp-server__describe_log_groups"` before first use.
- **Evidence must be reproducible.** Someone reading the comment should be able to copy the curl/SQL/log query and get the same result.
- **Truncate, don't hide.** If a log dump is 200 lines long, show the 20 most relevant with `...` to indicate truncation. Never drop the evidence to "it worked".

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Never call `aws secretsmanager get-secret-value` directly — use `scripts/with-secret.sh`.
- Never run production scraping. Dev only.
- If you need to run a data query, use `scripts/dev-db-query.sh` (ECS Exec path) — direct DB connection from the worktree is blocked by VPC per `CLAUDE.md` §Infrastructure.
