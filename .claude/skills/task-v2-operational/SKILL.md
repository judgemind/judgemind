---
description: Operational handler for the per-phase /task-v2 pipeline. Executes non-coding tasks (script runs, DB queries, gh actions, data rebuilds) without creating a PR. Emits a structured verdict for the dispatcher to advance the agent pipeline.
argument-hint: "<agent-id>"
model: opus
maxTurns: 200
---

# /task-v2-operational skill

Operational handler for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Executes tasks that require only running scripts, querying the database, firing gh actions, or updating labels/issue state — without writing code or opening a PR.

**Prerequisites:** The dispatcher daemon has already (a) claimed the issue, (b) created the worktree at `{worktree}`, (c) run the plan phase and determined `task_type=operational`, (d) written the input bundle to `{worktree}/tmp/dispatcher-input/operational.json`.

**Goal:** Complete the operational task described in the issue, post verification evidence as an issue comment, close the issue (if done), and write a structured output JSON to `{worktree}/tmp/dispatcher-output/operational.json`. No PR. No code commits.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. This subprocess is already a dispatcher-spawned background task.

**IMPORTANT — Heartbeat lines.** Run the Bash tool with:

- `echo PHASE_START operational` immediately after reading this SKILL.md (before Step 1).
- `echo PHASE_DONE <verdict>` right before writing the output JSON.

---

## Capabilities — same as a peer /task agent

You have full authority to execute operational work:

- **Bash** — `scripts/ecs-run-task.sh` for ECS oneshot script runs, `scripts/dev-db-query.sh` for SQL queries, `gh` for issue/label/comment operations, `aws s3` for artifact downloads, `psql` via the scripts for DB validation. Set `timeout: 1200000` on long-running commands.
- **Read / Glob / Grep** — full filesystem access for reading issue context, scripts, and docs.
- **MCP servers** — `mcp__github__*` for GitHub reads, `mcp__awslabs_cloudwatch-mcp-server__*` and `mcp__awslabs_ecs-mcp-server__*` for ECS + CloudWatch reads.
- **gh CLI** — `gh issue comment`, `gh issue close`, `gh issue edit`, `gh issue view`.

> **MCP vs `gh`:** `mcp__github__get_issue` for reads. `gh issue comment --body-file`, `gh issue close`, `gh issue edit --add-label` for writes. See `docs/agent/github-api-access.md`.

> **MCP vs `aws` CLI:** `scripts/ecs-run-task.sh` for ECS oneshot task launch (handles network config, log streaming, exit-code propagation). `mcp__awslabs_ecs-mcp-server__*` for cluster/task reads. See `docs/agent/aws-api-access.md`.

## Bright lines — irreducible

1. **No production deploy.** `terraform apply` against `environments/production/`, ECS service writes against `*-production` clusters. Human-only per `CLAUDE.md`. Use dev-tier scripts only.
2. **No `gh auth switch`.** Operator-only.
3. **No force-push to main, no amending merged commits.**
4. **No code commits to the worktree branch.** Operational tasks produce zero commits — no PR is ever opened. All work is done through `scripts/ecs-run-task.sh`, `scripts/dev-db-query.sh`, or `gh` commands.

If a bright line is triggered, emit `verdict=blocked` with a clear `block_reason` explaining which line was hit and what operator action is required.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/operational.json`. Required fields (same shape as `plan.json` input plus the plan output):

- `agent_id` (str) — your UUID for correlation.
- `issue_number` (int) — the issue being worked.
- `issue_title` (str) — title text.
- `issue_body` (str) — raw markdown issue body.
- `issue_comments` (list) — filtered to non-bot authors.
- `plan_text` (str) — the plan phase output, describing exactly what to execute.
- `acceptance_criteria` (list of str) — the acceptance criteria to verify.
- `worktree_path` (str) — absolute path to your worktree root.
- `repo_root` (str) — same as `worktree_path`.

If the file is missing or malformed, write `verdict=blocked, block_reason="input JSON missing or malformed"` and exit 0. Never read from GitHub to reconstruct — the daemon owns the handoff.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/operational.json` with these fields, then exit 0:

```
{
  "agent_id": "<echo from input>",
  "issue_number": <int>,
  "verdict": "succeeded" | "blocked" | "failed",
  "action_taken": "<one-line description of what was executed>",
  "evidence_md": "<markdown evidence block — DB row counts, curl output, log lines>",
  "block_reason": null | "<string — only when verdict=blocked>"
}
```

Exit 0 regardless of verdict. The dispatcher reads the verdict from the JSON, not the exit code.

**Verdict rules:**

- `succeeded` — task completed, evidence posted, issue closed (if the AC says to close it). The agent advances to `operational_done` with `status=succeeded`.
- `blocked` — a bright line was hit, a required secret is missing, the environment is not ready, or any other condition that needs operator action before the task can proceed. Advances to `operational_failed` with `status=needs_review`.
- `failed` — a genuine execution failure (script errored, DB unreachable, ECS task timed out, unexpected output). Routes to the diagnoser for retry/escalation.

---

## Step 0 — Post-compaction recovery (READ FIRST after any context reset)

If your context was just autocompacted (the summary references "previous conversation"), check the output file:

```
ls {worktree}/tmp/dispatcher-output/operational.json 2>/dev/null && echo DONE || echo RESUME
```

- **`DONE`**: the output file exists. Re-read it to confirm `verdict` is populated, then exit without re-running the task.
- **`RESUME`**: output file missing. Continue from Step 1.

---

## Step 1 — Read the input bundle and understand the task

Read `{worktree}/tmp/dispatcher-input/operational.json`. Extract:

- The `plan_text` — the exact operational steps the plan phase prescribed.
- The `acceptance_criteria` — what you must verify to emit `verdict=succeeded`.
- The `issue_body` — the original issue for full context.

## Step 2 — State-awareness before action (MANDATORY)

Before executing anything, check what's already in flight. Duplicate ECS runs, duplicate DB rebuilds, and duplicate issue comments all have real costs.

1. Run `gh issue view <issue_number> --repo judgemind/judgemind --json state,labels,comments` to confirm the issue is still open and not already completed by a prior agent.
2. If the issue is already closed, emit `verdict=succeeded, action_taken="issue already closed by prior agent", evidence_md="Issue was already in closed state"` and exit.
3. Check whether the operational action was already run by looking at issue comments for evidence markers from a prior agent.

## Step 3 — Plan + execute

Execute the operational steps described in `plan_text`. Common patterns:

**ECS oneshot (data script / rebuild):**

```
scripts/ecs-run-task.sh scripts/<script>.py -- <args>
```

Use `timeout: 1200000`. The last line of stderr contains the CloudWatch log URL. Capture stdout/stderr for the evidence block.

**DB query (validation / row count):**

```
scripts/dev-db-query.sh "SELECT count(*) FROM derived.rulings WHERE county = 'Santa Clara'"
```

**gh label / close:**

```
gh issue edit <N> --repo judgemind/judgemind --add-label status/done --remove-label agent/ready
gh issue close <N> --repo judgemind/judgemind --reason completed --comment "..."
```

After executing, verify the result against each acceptance criterion. Record the verification evidence (row counts, curl responses, log lines, gh output).

## Step 4 — Validate

For each acceptance criterion, run the verification check described in `plan_text` or implied by the criterion text. Concrete verification patterns:

- **Row count:** `scripts/dev-db-query.sh "SELECT count(*) FROM ..."` — assert count is non-zero and within expected range.
- **Data spot-check:** `scripts/dev-db-query.sh "SELECT ... LIMIT 5"` — assert expected fields are populated.
- **S3 presence:** `aws s3 ls s3://judgemind-document-archive-dev/<path>` — assert objects exist.
- **ECS task exit code:** captured by `scripts/ecs-run-task.sh` — non-zero means `verdict=failed`.

If any verification fails: capture the failure output and emit `verdict=failed` with the failure evidence in `evidence_md`.

## Step 4.5 — Time-budget discipline (#3524)

The supervisor flags operational agents as stuck after 3600s (60 min). Respect
that budget in your own retry loop: do not exhaust the entire window on
repetitive validation attempts against the same data.

**Rule:** If you have run the operation and validate more than 3 times against
the same data without the success criterion being met, do NOT keep retrying.
Emit `verdict=blocked` with `block_reason` describing the non-converging
condition and `evidence_md` containing the last validation output. Looping is
the bug.

**Why:** Repeating the same ECS run or DB query when the underlying data has
not changed cannot produce a different result. A BLOCKED verdict routes to the
operator / diagnoser so the root cause (data not populated, wrong script args,
missing dependency) can be fixed — rather than burning the 60-min budget and
having the supervisor catch the agent as stuck anyway.

## Step 5 — Post evidence comment and close issue

Write the evidence markdown to a temp file then post:

```
# Do NOT use heredoc — write evidence to a file first, then use --body-file
```

Write `{worktree}/tmp/dispatcher-operational/evidence.md` with a `## Verification Evidence` section, then:

```
gh issue comment <issue_number> --repo judgemind/judgemind --body-file {worktree}/tmp/dispatcher-operational/evidence.md
```

If the task is complete and the AC say to close the issue:

```
gh issue close <issue_number> --repo judgemind/judgemind --reason completed
```

## Step 6 — Write the output JSON

Write `{worktree}/tmp/dispatcher-output/operational.json` with verdict + evidence. Exit 0.

---

## What this skill does NOT do

- **Does not write code.** No edits to source files, no commits, no PR.
- **Does not deploy to production.** ECS writes are always against dev-tier clusters.
- **Does not spawn recursive operational agents.** If the task requires a code change, emit `verdict=blocked` with `block_reason` explaining the gap so an operator can file a coding task.

## Worked example — Santa Clara county data restore

Issue: "ops: restore Santa Clara county rulings after SC outage (#2419)"

Plan text: "Run `rebuild_db.py --county 'Santa Clara'` via ECS oneshot. Validate row count. Post evidence. Close issue."

**Step 3 execution:**

```
scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county "Santa Clara"
```

**Step 4 validation:**

```
scripts/dev-db-query.sh "SELECT count(*) FROM derived.rulings WHERE county = 'Santa Clara'"
```

Expected: count > 0 and approximately matches pre-outage snapshot.

**Step 5 evidence:**

```
## Verification Evidence

- ECS task `rebuild_db.py --county 'Santa Clara'` completed with exit 0.
- `SELECT count(*) FROM derived.rulings WHERE county = 'Santa Clara'` → 4,821 rows.
- Spot-check: `SELECT case_number, ruling_date FROM derived.rulings WHERE county = 'Santa Clara' LIMIT 5` shows data from 2026-04-15 through 2026-04-26.

AC1 ✓ — Santa Clara county rulings are queryable in the DB after rebuild.
AC2 ✓ — Row count 4,821 is within expected range of pre-outage snapshot (~4,800–4,900).
```

**Output:**

```json
{
  "agent_id": "...",
  "issue_number": 2419,
  "verdict": "succeeded",
  "action_taken": "ran rebuild_db.py --county 'Santa Clara' via ECS oneshot; 4,821 rows restored",
  "evidence_md": "## Verification Evidence\n\n- ECS task completed exit 0.\n- 4,821 rows in derived.rulings for Santa Clara.\n",
  "block_reason": null
}
```

---

## Reminders

- No `$()`, no heredocs, no `python -c`. See the repo root `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/dispatcher-operational/`, never `/tmp/`.
- Use Grep, Glob, and Read tools — never `find`, `cat`, `head`, `tail` from Bash.
- Set `timeout: 1200000` on any long-running Bash command.
- `evidence_md` is mandatory even for `verdict=blocked` or `verdict=failed` — include the failure output so the diagnoser can diagnose without re-running.
- Write evidence to a file before posting to gh — never construct multi-line `--body` on the command line.
- Post the evidence comment BEFORE closing the issue (so the comment is visible on the closed issue).
