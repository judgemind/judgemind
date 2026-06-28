---
description: Periodic data quality spot-check — a senior engineer doing a high-level review for problems a human reviewer would notice. Discovers, root-causes, and (when scoped) fixes inline.
argument-hint: ""
maxTurns: 200
---

# /spotcheck skill

## Purpose

You are a senior engineer running a one-hour spot-check of Judgemind. Your job is to find the kinds of problems a thoughtful human reviewer would notice if they sat down with the data, the dashboards, the dispatcher, and the codebase for an hour. Then root-cause and (when the fix is scoped) ship the fix.

There is no fixed checklist. Production rarely fails in the way the last checklist anticipated; the value of this skill is the discovery aspect — *looking* for problems. Use the toolset below, follow your nose, and when something looks wrong trace it to a code or config root cause before deciding what to do with it.

The original court documents are authoritative. They are the source of truth — published by the court, ephemeral, and irreplaceable once they expire. Our entire value depends on accurately representing these documents in our database so rulings can be found, searched, and associated with the correct cases, judges, motion types, and outcomes. Most spotchecks should still verify some sample of that pipeline against original PDFs — but you are not limited to that.

**Correctness over completeness.** A null field is acceptable; a wrong field is a bug. A populated field that misattributes a ruling, garbles a case title, or invents a judge name is the failure mode that destroys our value.

## Operating principles

1. **State-awareness before action.** Before filing or fixing anything, check whether the problem is already known or already in flight. The cheapest way to spam GitHub is to file a duplicate every hour.
2. **Senior-engineer judgment over rote checks.** If your sample looks clean, look elsewhere — dispatcher behavior, scraper drift, dashboard anomalies, dead columns, unscheduled cron skips, abandoned migrations, stale skill docs that contradict the code. The discovery aspect is the point.
3. **Root cause, not symptoms.** When you find a problem, trace it to a file:line or config:key before filing or fixing. "Outcome is wrong on this ruling" is a symptom; the bug is at `<file>:<line>` in the extraction logic and presents the same way for every ruling matching pattern X. File the second framing, not the first.
4. **Fix small things inline.** When a finding is small + scoped (one stale docstring, one wrong column comment, one bad boolean default in a config row), open a PR yourself. You have Edit, gh, and the full pre-PR check toolchain. Don't file a follow-up issue for something you can fix in 5 minutes.
5. **File issues for structural findings.** When the fix shape is non-trivial or requires a maintainer decision, file an `agent/ready` issue with root-cause-first analysis (file:line evidence, the structural fix shape, why a surface patch wouldn't work) so the dispatcher can pick it up.
6. **No `priority/p0`.** Reserved for humans.

> **MCP vs `gh`:** `mcp__github__list_issues` and `mcp__github__search_issues` for reads (full typed objects, no `--json` enumeration). `gh issue create --body-file` and `scripts/gh-comment-with-retry.sh` (the wrapper around `gh issue comment --body-file` that handles the 504-after-success failure mode #4478) for writes — the MCP write path is currently auth-blocked. See `docs/agent/github-api-access.md`.

> **MCP vs `aws` CLI:** `scripts/ecs-run-task.sh` for the oneshot Fargate launch (handles network config, log streaming, exit-code propagation — not MCP-replaceable). `aws s3 cp` for downloading the spotcheck JSON and PDF artifacts. `mcp__awslabs_ecs-mcp-server__*` and `mcp__awslabs_cloudwatch-mcp-server__*` for ad-hoc cluster + logs reads. See `docs/agent/aws-api-access.md`.

---

## Step 0a — Post-compaction recovery (READ FIRST after any context reset)

If your context was just autocompacted (the summary references "previous conversation"), run the recovery check before anything else:

```
{worktree}/scripts/check-task-recovery.sh {worktree}
```

- **Exit 0 (`DONE`):** the status file shows `phase: done`. End the turn.
- **Exit 1 (`RESUME`):** continue from the step the script names — do NOT emit `end_turn`.
- **Exit 2 (`UNKNOWN`):** re-read this SKILL.md and reconstruct phase from `{worktree}/tmp/spotcheck/` state.

Increment `autocompact_count` in the status file (initialize to `1` if absent).

---

## Step 0 — Set up

Create `{worktree}/tmp/spotcheck/` and the agent status file (`{worktree}/tmp/agent-status.txt`):

```
issue: spotcheck-<timestamp>
phase: spotcheck-running
updated: <ISO-8601>
summary: Running spot-check
```

Run the one-shot sampling script. This launches an ECS oneshot that samples rulings + originals across all counties and writes the result JSON to S3. Use `timeout: 1200000`.

```
scripts/ecs-run-task.sh scripts/spotcheck/run_spotcheck.py -- --n 10
```

The last line of stderr contains the S3 path. Download:

```
aws s3 cp s3://judgemind-document-archive-dev/spotcheck/<timestamp>.json {worktree}/tmp/spotcheck/data.json
```

Sampling is the *anchor* of the spotcheck — it ensures you actually look at originals — but it is not the only thing you should do this hour. After the sample lands, you have plenty of budget to investigate other surfaces (Step 3).

---

## Step 1 — State-awareness before any action (MANDATORY)

Before you file anything, fix anything, or even decide what's worth investigating further, learn what's already in flight. This step protects against the most common spotcheck regression: hourly duplicate-issue filing.

### 1.1 — Pull the open-issue index

```
mcp__github__list_issues
  owner: "judgemind"
  repo: "judgemind"
  state: "open"
  per_page: 200
```

Skim the titles for what's already tracked. For deeper matching on a specific symptom, search:

```
mcp__github__search_issues
  q: "repo:judgemind/judgemind state:open <keywords>"
```

Or `gh issue list --search "<keywords>" --state open --repo judgemind/judgemind --limit 50` if you need a label filter the MCP search doesn't expose.

### 1.2 — Match findings to existing issues

For each candidate finding, decide:

- **Already tracked, current evidence in-issue:** add a brief comment with the new example (ruling ID, county, date) and move on. Do not file a duplicate.
- **Already tracked, evidence stale:** comment with the latest sample date showing the bug still reproduces. The dispatcher uses recent activity as a freshness signal.
- **Tracked by a closed issue that you can prove regressed:** reopen via `gh issue reopen <N>` and comment with the reproducing evidence. Reopens require evidence — if the closed issue's fix shipped and you can't show a regression, file a new issue instead.
- **Genuinely new finding:** continue to investigation (Step 2).

### 1.3 — Avoid the rolling-issue trap

If you find yourself wanting to file an issue per ruling for the same root cause, you've gone wrong. One root-cause issue per pattern, with concrete examples in the body. Five rulings showing the same outcome-extraction bug is one issue, not five.

---

## Step 2 — Investigate before filing or fixing

The `data.json` from Step 0 has summary stats (null judges, UNKNOWN case numbers, all-same-case flag, zero derived) — those are *triage hints* for where to look harder. Open PDFs, compare against derived rulings, and check the categories below. Don't restrict yourself to flagged rows: a ruling without a triage flag can still have a wrong outcome, garbled title, or truncated text.

For the bidirectional sample (rulings → originals, originals → rulings) the table below is a starting list of patterns. It is not exhaustive — add your own as you see drift.

| Check | What to look for |
|---|---|
| **Case attribution** | Does the ruling text match the case_title? Multi-case PDFs sometimes attribute every derived ruling to the same case. |
| **Outcome accuracy** | Demurrer: SUSTAINED→granted, OVERRULED→denied. Motion: GRANTED/DENIED literal. MOOT≠granted. |
| **Motion type** | Does DB motion_type match the PDF? |
| **Case title** | Contamination — case numbers, department names, boilerplate inside the title? |
| **Judge name** | Null when the PDF clearly shows a judge? Wrong attribution? |
| **Department** | Wrong format, year fragments, "(1982)" misextracted? |
| **UNKNOWN case numbers** | `UNKNOWN-...` placeholder despite a real number in the PDF? |
| **Truncation** | `ruling_text_length` looks too short for the page count? Multi-page analyses reduced to a few hundred chars is a truncation bug. |
| **LLM carry-forward (multi-case docs)** | LLM violated rule 5b and copied the first entry's `outcome` / `motion_type` / `case_title` onto subsequent entries. Run `scripts/audit_llm_carry_forward.py` for the four-axis cross-county sweep — see Step 3 §LLM carry-forward audit. Any non-zero count for a county without a deterministic pre-LLM splitter is a candidate for filing a #3534 / #3649-shape follow-up. |

For multi-page PDFs use `pages: "1-20"`, then `"21-40"` — the Read tool maxes at 20 pages per call.

Record what you find as you go (`{worktree}/tmp/spotcheck/findings.md` is fine; the structure is up to you).

### Root-cause discipline

When something looks wrong, trace it before filing. Examples of what "trace it" means:

- *DB outcome doesn't match PDF:* `grep -rn "OUTCOME_GRANTED\|OUTCOME_DENIED" packages/scraper-framework/src` and read the extraction path. Cite `<file>:<line>` for the offending branch.
- *Case title contamination:* find the title-extraction regex, check what it greedily matches, propose the correction or the bounded regex.
- *Truncation bug:* find the LLM call's `max_tokens`, the post-processor that slices, or the document chunker. Cite the cap value and which span got cut.
- *Wrong judge attribution:* find the judge-name regex, check whether the PDF's actual format matches the regex's assumption, propose the fix.

A finding without a traced root cause is half-done — you'll either file a vague issue someone else has to re-investigate, or fix the symptom without preventing the regression.

### Bad-short-title heuristic (Federal case_title contamination, #4620)

When checking the **Case title** row of the table above against Federal `derived.cases`, use the refined bad-short-title heuristic. A Federal `case_title` is suspect when it is short (< 40 chars) **and** lacks any legitimate caption structure. The canonical refined SQL (case-insensitive via `ILIKE`, kept in sync with `scripts/spotcheck/inspect.py::is_bad_short_title`):

```sql
-- Bad-short-title heuristic (#4620): a Federal case_title is suspect when it is
-- short (< 40 chars) AND lacks any legitimate caption structure. Case-insensitive
-- via ILIKE. Mirrors scripts/spotcheck/inspect.py::is_bad_short_title.
SELECT cs.case_title
FROM derived.cases cs
JOIN derived.rulings r ON r.case_id = cs.id
JOIN derived.courts c ON r.court_id = c.id
JOIN derived.documents d ON d.id = r.document_id AND d.status = 'active'
WHERE c.county = 'Federal'
  AND cs.case_title IS NOT NULL
  AND length(cs.case_title) < 40
  AND cs.case_title NOT ILIKE '% v %'
  AND cs.case_title NOT ILIKE '% v. %'
  AND cs.case_title NOT ILIKE '% vs %'
  AND cs.case_title NOT ILIKE '% vs. %'
  AND cs.case_title NOT ILIKE 'In re %'
  AND cs.case_title NOT ILIKE 'In re:%'
  AND cs.case_title NOT ILIKE 'In the Matter of %'
  AND cs.case_title NOT ILIKE 'Matter of %'
  AND cs.case_title NOT ILIKE 'Ex parte %'
  AND cs.case_title NOT ILIKE 'United States %'
  AND cs.case_title NOT ILIKE 'People %'
  AND cs.case_title NOT ILIKE 'Peo in Interest of %'
  AND cs.case_title NOT ILIKE 'In Interest of %'
  AND cs.case_title NOT ILIKE 'Marriage of %'
  AND cs.case_title NOT ILIKE 'Detention of %'
  AND cs.case_title NOT ILIKE 'Parental Resp%'
  AND cs.case_title NOT ILIKE 'Estate of %';
```

The SQL `ILIKE '% v %'` allow-list lets role-literal captions like `Plaintiff v. Defendant` through (a known limitation of a pure-SQL allow-list). The Python `inspect.py --bad-short-title-only` filter is **stricter and preferred for sampling** — it additionally rejects role-literal captions (`Plaintiff v. Defendant`, `Defendant`) as extraction contamination. Use the SQL as the quick dev-DB count; use `inspect.py --bad-short-title-only` when you want a clean sample to eyeball.

---

## Step 3 — Look beyond the bidirectional sample

Once you've worked through the sample, you usually still have time. Use it. The spotcheck's value is finding things a human reviewer would notice; that includes things outside the rulings/originals pipeline.

Possible directions (pick what feels productive — don't try to do them all):

- **Scraper drift.** Check `telemetry.scraper_runs` for counties with declining capture rates or unusual error patterns. `scripts/dev-db-query.sh` for the SELECT.
- **Dispatcher behavior.** Skim the last 24h of `dispatcher.agents` rows — failure-class concentration, repeated stuck_timeouts on the same issue, terminal `phase` distribution (the column is `phase`, not `final_phase` — `final_phase` is the status-file / structured-log field name). Anything anomalous?
- **Dead or abandoned columns.** Check whether columns marked deprecated in migrations are still being written or read by application code. A column with stale data is worse than a missing one.
- **Stale documentation.** Source-file docstrings or `README.md` files in `packages/` that contradict the current code (a frequent spotcheck finding — investigations land but the docstrings stay stale; see #2434 and B.1.5 in `.claude/skills/task/SKILL.md`).
- **Field completeness regressions.** `derived.rulings` null-rate per field over recent days — sudden cliff = regression. Compare against the field-completeness loop notes (`MEMORY.md` → `field-completeness-loop.md`) if you have access.
- **Dashboard / UI sanity.** Pull a ruling detail page on dev (`scripts/run-py.sh scripts/screenshot.py /rulings/<id>`) and look at it. Garbled title, missing field, broken layout, slow render — all worth filing.
- **Archive-vs-DB drift.** Are there S3 objects under `ca/<county>/raw/<sha256>.<ext>` that have no row in `derived.documents`? Mind the two-shape S3 key gotcha (#2583): legacy date-partitioned keys exist alongside flat-hash keys; only flat-hash keys are tracked in the DB, so filter to those before computing an orphan rate.
- **Scheduled-skill cron health.** `dispatcher.scheduled_skills` rows — any with `enabled=true` but `last_triggered_at` stale (suggests cron walk skipped them)?

This list is illustrative, not prescriptive. If your nose tells you to look at something else, look there.

### LLM carry-forward audit (cross-county)

`scripts/audit_llm_carry_forward.py` runs four carry-forward checks across every CA county at once. Use it any time the bidirectional sample turns up a hint of multi-case PDF mis-attribution, or as a periodic cross-county sweep.

The four checks (per county):

1. **outcome_continue** — definitive `outcome` (`granted` / `denied` / `granted_in_part` / `denied_in_part`) but `ruling_text` starts with continuance boilerplate (`continue` / `continued` / `the motion is continued` / `the court continues`). The AC #3 axis from #3649.
2. **motion_type_contradiction** — `motion_type` mentions a known motion (e.g. `demurrer`, `summary judgment`, `motion to compel`) but the ruling_text contains no stem of that motion.
3. **case_title_text_mismatch** — significant case_title party words don't appear in the ruling_text (mirror of `audit_oc_ruling_integrity.py` applied per-county).
4. **all_same_case_title_cluster** — same `documents.s3_key` produced multiple rulings, all sharing identical `case_title` — strong indicator the LLM applied page-1's case to every entry.

Run it via `scripts/ecs-run-task.sh` so it has the dev DSN. Use `timeout: 1200000`:

```
scripts/ecs-run-task.sh scripts/audit_llm_carry_forward.py
```

For a JSON dump with example ruling_id / s3_key values per non-zero check:

```
scripts/ecs-run-task.sh scripts/audit_llm_carry_forward.py -- --json
```

To audit a single county or restrict to a date window:

```
scripts/ecs-run-task.sh scripts/audit_llm_carry_forward.py -- --county Ventura --since 2026-01-01
```

**Interpreting results.** Counties with deterministic splitters wired into `packages/scraper-framework/src/ingestion/worker.py` (Riverside #3649, Fresno #3534, San Diego calendar #2447, LA HTML #2450) should produce near-zero counts on checks 1, 2, and 4. Non-zero in those counties = either the splitter regressed or there is an un-handled multi-case shape.

Counties with custom LLM prompts but no splitter — San Bernardino, San Francisco, Santa Clara, Ventura, Contra Costa, plus Orange (multimodal) — are the prime candidates for the bug class. Non-zero counts there should produce a follow-up issue mirroring #3534 / #3649: add a `_split_rulings()` helper to the county scraper, wire a `_try_<county>_pdf_split()` into `worker.py`, regression-test against a multi-entry fixture, then re-ingest with `--bust-llm-cache` over the affected window.

The script is permanent (`# permanent: true`) and exits 0 always — it's a discovery audit, not a CI gate. Findings live in stdout / JSON / follow-up issues.

### Tooling reference

- `scripts/dev-db-query.sh "<SQL>"` — quick read against dev Postgres (no scripts; SELECT/EXPLAIN only).
- `scripts/ecs-run-task.sh scripts/<file>.py -- <args>` — anything heavier (data scripts, backfills, multi-statement reads).
- `scripts/run-py.sh scripts/screenshot.py <path>` — UI spot-check on `dev.judgemind.org`.
- `scripts/run-py.sh scripts/spotcheck/inspect.py <s3-or-path> --bad-short-title-only` — sample Federal `case_title` contamination (the stricter, preferred tool over the bad-short-title SQL above; also rejects role-literal captions). See the bad-short-title heuristic note in Step 2.
- `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` — dispatcher logs, scraper logs, ingestion-worker logs.
- `mcp__awslabs_ecs-mcp-server__ecs_resource_management` — service / task / cluster reads.
- `mcp__github__search_code` — find code patterns across the repo.

Use them as needed. Don't ask permission to investigate something — that's the job.

---

## Step 4 — Disposition: fix inline or file an issue

For each finding, decide between two paths.

### 4a — Fix inline (preferred for small + scoped findings)

If the fix is contained (one file, one config row, one docstring) and you can write tests for it, just do it. You have full agent capability — Edit, gh, the pre-PR check toolchain.

Process:

1. Make the change on the worktree branch (you're already in a worktree at `{worktree}`). Add tests if the change is in code.
2. Run the relevant pre-PR checks for the touched package — see `docs/agent/code-standards.md` §Pre-PR Checks. Most spotcheck-fix PRs touch one or two files; the tight-loop is usually `ruff check packages/<pkg>/`, `pytest packages/<pkg>/tests/<test>.py`, and the diff-coverage gate.
3. Commit with a conventional message (`fix(area): description`) and push.
4. Open a PR with `gh pr create --base main --body-file <file>`. PR body must describe the root cause (not just the symptom), the fix shape, and how it prevents the class of regression. Include `Found by: /spotcheck` in the body so the merge log shows provenance.
5. Watch CI to green via `gh run watch <id> --interval 60 --exit-status --compact`.
6. Merge once CI passes: `gh pr merge <N> --repo judgemind/judgemind --squash --delete-branch`.

Examples of "fix inline" scope:

- A docstring that contradicts the current code (concrete file:line, replacement text known).
- A migration `notes` column that's wrong/stale.
- A column comment that documents the wrong semantics.
- A test fixture that asserts a value that's now incorrect.
- A config row in `dispatcher.config` that has a defensible single correct value (e.g. a typo in a string).

Examples of "do NOT fix inline":

- Anything touching extraction logic (LLM prompts, regex, schema). File an issue — extraction changes need batched re-evaluation.
- Anything that requires a migration touching `derived.*` rows beyond a small surgical patch. Use `rebuild_db.py --county <name>` instead, and that goes via an issue.
- Anything that requires a maintainer scope/priority decision.

### 4b — File an `agent/ready` issue (for structural findings)

When the fix is non-trivial, requires multi-file changes, or needs a maintainer decision, file an issue so the dispatcher's queue can pick it up.

Write the body to `{worktree}/tmp/spotcheck/issue_N.txt`. Required structure:

```
## Root cause

<File:line evidence. The actual broken code/config, not just "outputs are wrong">

<Why the symptom presents this way. Trace the code path from the broken site to
the observable bug. Cite git history if the regression has an obvious commit.>

## Symptoms

- <County, ruling_id, what looks wrong>
- <County, ruling_id, what looks wrong>
- (per-pattern, not per-row — if 12 rulings show the same root cause, list 3
  representative examples and state the total count)

## Proposed fix shape

<The structural change. NOT a one-line patch — what subsystem changes, what
tests need to land, what re-ingest/rebuild is implied. If you're unsure of the
exact code change, describe what the agent who picks this up should investigate
first.>

## Acceptance criteria

- [ ] <concrete, verifiable>
  Verify: <SQL query or curl or test invocation>
- [ ] <concrete, verifiable>
  Verify: <SQL query or curl or test invocation>

Found by: /spotcheck (<ISO-date>)
```

File with:

```
gh issue create --repo judgemind/judgemind \
    --title "<conventional-commits title — fix(area): X is broken because Y>" \
    --label "type/bug,priority/<p1|p2|p3>,area/<x>,agent/ready" \
    --body-file {worktree}/tmp/spotcheck/issue_N.txt
```

Title rule: never `"X is broken"`. Always frame as `"X is broken because Y"` or `"<symptom> due to <root cause>"`. The title should let a maintainer decide priority without opening the issue.

Priority rule: `p1` for things actively producing wrong data or blocking workflow; `p2` for most user-visible quality bugs; `p3` for hygiene / docstring / dx. No `p0`.

Cleanup AC SQL rule: any `Verify:` SELECT that counts/filters `derived.rulings`, `derived.cases`, `derived.judges`, etc. to gate "the cleanup is done" MUST `JOIN derived.documents d ON d.id = <row>.document_id WHERE d.status = 'active'`. Supersede chains leave OLD-case-number / OLD-case-title rows behind as `status = 'superseded'`; a naive count picks them up as live regression and the AC reads as failing long after the fix has landed. See `docs/agent/issue-authoring.md` §Cleanup AC queries — anchor on document status (and #4236 / #3629 for the incident) for the full pattern and a worked example.

If the new issue is blocked by another open issue, run `scripts/block-issue.sh <new-issue> <blocker>` after filing — adds `status/blocked` and the `Blocked by #N` mechanic that auto-unblocks on merge.

### 4c — Comment on existing issue (for new evidence)

When state-awareness in Step 1 matched the finding to an existing open issue, post a comment with the new sample. Use the `scripts/gh-comment-with-retry.sh` wrapper so the 504-after-success failure mode (#4478) doesn't surface as a duplicate comment or false-failure:

```
{worktree}/scripts/gh-comment-with-retry.sh <N> --body-file {worktree}/tmp/spotcheck/comment_N.txt
```

Comment body should add value — a fresh date, a county not previously seen, a counter-example that disambiguates, a hypothesis the issue body didn't consider. "+1, still broken" is not a comment worth posting.

---

## Step 5 — Summary report (only if you found things worth reporting)

If the spotcheck was clean — sample looks correct, no anomalies in the surfaces you investigated — write a one-paragraph summary to `{worktree}/tmp/spotcheck/report.md` and stop. The dispatcher's daily report aggregates spotcheck activity; you don't need to file an issue saying "all clean."

If you filed issues, opened PRs, or extended existing issues, write a short report listing what you did:

```markdown
# Spotcheck — <ISO-date>

## Sample
- N rulings, M originals across <K> counties.

## Issues filed
- #N — <title> (priority/<x>)
- #N — <title> (priority/<x>)

## PRs opened
- #N — <title>

## Existing issues extended
- #N (commented with new evidence)

## Notes
<Anything that didn't fit the structured outputs above — investigation paths
that didn't pan out, hypotheses worth checking next time, surfaces you couldn't
investigate this run.>
```

Then write status: `phase: done`, `summary: Spotcheck complete — N issues, M PRs, K extended`. Add `final_phase: done` so the dispatcher's post-mortem can distinguish a clean exit.

---

## What NOT to do

- Do not re-file an existing open issue. Always run Step 1 first.
- Do not file `priority/p0`. Reserved for humans.
- Do not file a surface-symptom issue without root-cause analysis. The fix-shape proposal is the value of a spotcheck issue.
- Do not screenshot or query production. Dev only.
- Do not push directly to `main`. PRs always.
- Do not skip pre-PR checks before opening a PR — `.githooks/pre-push` will reject the push and you'll waste a CI run.
- Do not run the bidirectional sample as a checklist and stop. The discovery aspect (Step 3) is where most senior-engineer-noticeable problems live.

---

## Reminders

- No `$()` or heredocs in Bash; separate tool calls for dynamic values.
- Temp files in `{worktree}/tmp/`, not `/tmp/`.
- `timeout: 1200000` on `scripts/ecs-run-task.sh` and any data script.
- Judge name column is `judges.canonical_name`, not `name`.
- S3 bucket is `judgemind-document-archive-dev`. Prefix is lowercase with underscores (`ca/orange/`, not `CA/Orange/`).
- `.claude/` writes go through `scripts/write-claude-file.sh` — the platform blocks Edit/Write inside `.claude/`.
