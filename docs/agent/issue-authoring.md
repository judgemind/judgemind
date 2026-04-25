# Issue Authoring

How to file issues that agents can act on correctly — acceptance criteria, sub-tasks, and investigation follow-ups. CLAUDE.md contains a short summary; this doc has the detail.

## Writing Acceptance Criteria

Acceptance criteria must be concrete and machine-checkable wherever possible. Vague criteria like "page looks correct" allow agents to hand-wave past verification. Include specific verification commands and expected results.

### Guidelines

- **Data changes**: include the SQL query and expected result.
- **Frontend changes**: include the URL and what should be visible (element, text, layout).
- **API changes**: include the endpoint, request, and expected response shape.
- **Behavior changes**: include the specific trigger and expected outcome.
- **External-integration changes** (issues proposing to query a third-party website or API — court case-search endpoints, public records APIs, etc.): include a one-line HTTP feasibility note confirming the endpoint is actually usable before labeling the issue `agent/ready`. Example: `Feasibility: curl https://example.court.gov/api/search?case=123 returns JSON, no reCAPTCHA/WAF, anonymous access works`. At minimum, verify: (1) the endpoint responds to an anonymous request, (2) there is no reCAPTCHA / Cloudflare challenge / login wall on the query path, (3) the expected data is actually returned for a realistic sample. Issues without a feasibility note risk premising acceptance criteria on integrations that cannot work — see #1979 for a case where ~a day of agent time was spent on an infeasible premise.
- **Data cleanup tasks on `derived.*` tables**: default the plan to `rebuild_db.py --county <name>` rather than a surgical one-off delete/patch script. Surgical scripts tend to ship with their own bugs and only patch existing rows — they do not validate the ingestion/enrichment pipeline, so the same root cause can keep affecting inbound data. Only write a surgical script if (1) rebuild cost is prohibitive at the affected scale, or (2) the deletion is scoped to a subset rebuild can't express. Include a one-line justification in the issue body if going surgical.

### Example — vague (bad)

```
- [ ] Zavala v Becker shows only its ruling text
```

### Example — machine-checkable (good)

```
- [ ] Zavala v Becker shows only its ruling text
  Verify: `SELECT length(ruling_text) FROM rulings WHERE case_id = 'f51849ca-...'` returns values < 5000
  Verify: Screenshot of /cases/f51849ca-... shows single-case content
```

Each criterion should have at least one `Verify:` line that an agent can execute to confirm the criterion is met. This applies to issues filed by both humans and agents. If a verification command is not possible (e.g., requires subjective judgment), note that explicitly so reviewers know it requires manual verification.

## Verify the gap exists before filing

Before filing a "does X" / "enable X" / "add X" issue, run a single command that verifies X is not already the case. This rule exists because an agent-runner cycle spent re-creating state that already exists is pure waste — the canonical example is #3146, where an issue was filed to "enable Container Insights on the ECS cluster" after the Terraform attribute `enable_container_insights = true` had already shipped in a prior PR. The agent spent a full cycle discovering, through probing, that there was nothing to do. A thirty-second check before filing would have surfaced that immediately. This is a sibling of the external-integration feasibility note in §Writing Acceptance Criteria (line 15): both rules say "verify the precondition before you ask an agent to build on top of it."

**Probe patterns by verb:**

- **"Enable AWS setting Y"** → check the live state and the Terraform source before filing.
  ```
  # Live state (example: ECS Container Insights)
  aws ecs describe-clusters --clusters <cluster-name> --include SETTINGS \
    --query 'clusters[0].settings'
  # Terraform source
  grep -r "enable_container_insights\|container_insights" infra/terraform/
  ```
  Already present if: live state shows `"value": "enabled"` OR Terraform already sets the attribute to `true`.

- **"Add metric / alarm for X"** → check whether the metric namespace is already populated and the alarm already exists.
  ```
  aws cloudwatch list-metrics --namespace <namespace> --metric-name <MetricName>
  aws cloudwatch describe-alarms --alarm-name-prefix <prefix>
  ```
  Already present if: `list-metrics` returns a non-empty `Metrics` array, or `describe-alarms` returns an existing alarm with the expected name prefix.

- **"Add documentation for X"** → grep the docs tree before filing.
  ```
  grep -r "X" docs/
  # or for agent-skill docs:
  grep -r "X" .claude/skills/ CLAUDE.md
  ```
  Already present if: the concept is already explained at the relevant level of detail in an existing doc.

- **"Fix code bug X" / "X throws an error"** → grep for the function name or error-message string; the bug may already be fixed in a prior PR the filer didn't see.
  ```
  grep -r "error_message_or_function_name" packages/
  # Also check recent git log for the relevant area:
  git log --oneline --all -- packages/<area>/
  ```
  Already fixed if: the offending code path no longer exists, or a recent commit message references the fix.

**Decision after probing:** If the gap is genuinely absent (the feature/setting/doc does not yet exist), file the issue normally. If the gap is already satisfied, either close the draft or pivot scope to a doc-only update that confirms the current state (e.g., "document that Container Insights is enabled and cite the Terraform attribute"). If the probe is ambiguous, treat as real and file normally — agents can do their own probe at pickup time per Step 4b in `.claude/skills/task/SKILL.md`.

## Priority Framework

Assign priority by urgency and workflow impact, not by user-visibility.

| Priority | Principle | Categories |
|---|---|---|
| **p0** | Human-only; agents set only when explicitly told | Production outages, data loss |
| **p1** | Time-sensitive or workflow accelerators | Scraper failures, data quality bugs, DX improvements, process fixes |
| **p2** | Everything else | User-facing bugs, backfills, refactoring, docs, data prevention |
| **p3** | Large slower units of work | Missing features, UI/UX redesign |

**Rationale:** Scraper data is ephemeral (lost forever if not captured). DX issues directly affect agent throughput — a broken workflow wastes entire agent sessions. Process improvements prevent repeated failures. These justify p1 even though they are not user-facing. Large feature work and redesigns are important but not urgent — p3.

Default DX to p1, not p3. Default features to p3, not p2. Reserve p0 for human-assigned true emergencies.

## Creating Sub-Tasks

If a task naturally breaks into 2+ independent pieces of work, create child issues:

- Reference the parent: `Parent: #42` in the issue body.
- Sub-tasks should be self-contained — another agent should be able to pick one up independently.
- Label child issues appropriately and add `agent/ready` if fully specified.

## Backfill Migrations: Row-Class Coverage

Why: Issues #2961 and #2960 revealed that a backfill migration silently skipped entire row classes — specifically, `failed` and `plan_blocked` rows were left un-updated because the SQL filter only matched the modal (`crashed`) case. The checklist below makes row-class coverage an explicit authoring and implementation requirement so the same oversight cannot recur. See #2961 (checklist formalized) and #2960 (prior incident).

When filing or implementing an issue that includes a backfill migration (any PR touching `packages/api/migrations/*.sql` with an `UPDATE`, `DELETE`, or `INSERT` that writes against existing rows), follow this checklist:

- **Enumerate every row class in the issue body.** If the affected table has a status/state/type column, list all distinct values that the migration must handle — not just the most common one. Present them as a table or bulleted list with the expected row count for each class (even if that count is 0).
- **Cross-reference the SQL filter against each row class.** For each `(status, failure_summary, …)` tuple in your row-class table, verify the SQL `WHERE` clause matches or explicitly excludes that row. Distinct status values (`failed` / `plan_blocked` / `crashed`) require distinct `OR` branches or a generalized filter — a single-value filter is almost always a bug.
- **The acceptance criteria must include a post-backfill invariant `SELECT`.** State the exact query and its expected result (typically `0` rows matching the pre-migration condition). Example:
  ```
  Verify: SELECT COUNT(*) FROM staging.captures WHERE … returns 0
  ```
- **For large tables, include a before/after row-count breakdown by status.** Confirm the counts add up correctly — total updated should equal the sum of per-class counts.

## Investigation Tasks

Investigation tasks produce documentation, not code:

- Write findings in the issue body or `docs/investigations/`.
- **Always file follow-up issues** for every actionable finding. Label them `agent/ready` if fully specified. Reference the investigation issue as the parent.
- After documenting findings and filing follow-ups, close the investigation issue unless human judgment is genuinely needed.
