---
description: Periodic data quality spot-check — runs a one-shot bidirectional sample across all counties, downloads originals, compares against DB records, and files issues for findings.
argument-hint: ""
maxTurns: 200
---

# /spotcheck skill

## Purpose

The original court documents are authoritative. They are the source of truth — published by the court, ephemeral, and irreplaceable once they expire. Our entire value depends on accurately representing these documents in our database so that rulings can be found, searched, and associated with the correct cases, judges, motion types, and outcomes.

**The spotcheck's job is to verify that we are doing this correctly.** It draws a random sample of rulings and original documents across all active counties and verifies — as rigorously as possible — that the parsed data in our database is an accurate representation of what the court actually published.

**Correctness over completeness.** The primary question is: "Is each extracted field accurate compared to the source document?" A null field is acceptable — a wrong field is a bug. When you find a populated field, verify it matches the original. When you find a null field, check whether the information was present in the source — if it was, that's a completeness gap worth filing, but it's less urgent than a field that's present but wrong.

Completeness metrics (% of fields populated) are useful as a *signal* toward correctness problems — a sudden drop suggests a bug — but don't mistake high completeness for high quality. Every field matters: a ruling attributed to the wrong case, a motion type that doesn't match the document, a case title that's garbled — these are the critical failures that make the data unreliable for the lawyers and researchers who depend on it.

The original documents may contain typos, ambiguities, or unusual formatting. That's fine — they're court documents. But our extraction must faithfully represent what's there, not silently drop, mangle, or misattribute it.

## Process

Bidirectional spot-check across all active counties:
- **Rulings → Originals:** Sample 10 random rulings from the DB, download source PDFs, verify every field against the original.
- **Originals → Rulings:** Sample 10 random original documents, query derived rulings, verify all cases in the source are represented and correctly attributed.

**Do not ask for confirmation. Work autonomously through every step.**

> **MCP vs `gh`:** use `mcp__github__list_issues` for the open-issues lookup in Step 4.1 (no `--json` enumeration, no `-q` jq — returns full typed objects). Keep `gh issue create --body-file` for new-issue writes — the MCP write path (`mcp__github__create_issue`) exists but is currently auth-blocked on this machine. See `docs/agent/github-api-access.md` for the decision rule.


> **MCP vs `aws` CLI:** the spotcheck stays on `scripts/ecs-run-task.sh` for the oneshot Fargate launch (handles network config, log streaming, exit-code propagation — not MCP-replaceable) and on `aws s3 cp` for downloading the spotcheck JSON and PDF artifacts (no MCP S3 coverage in Phase A). For ad-hoc post-run health checks against the dev cluster (`DescribeServices` on the ingestion worker, Logs Insights queries against `/ecs/judgemind-ingestion-worker-dev`), prefer the `mcp__awslabs_ecs-mcp-server__*` and `mcp__awslabs_cloudwatch-mcp-server__*` tools — see `docs/agent/aws-api-access.md`.
---

## Step 0 — Run the one-shot spotcheck script

The spotcheck script (`scripts/spotcheck/run_spotcheck.py`) runs on ECS and does all DB work in one shot: samples rulings and originals per county, fetches all paired data, detects the all-same-case bug, and writes the full result JSON to S3.

```
scripts/ecs-run-task.sh scripts/spotcheck/run_spotcheck.py -- --n 10
```

Use `timeout: 1200000` — this queries every county.

The script prints a compact summary to stdout (visible in the task logs) and writes the full result to S3. The last line of stderr contains the S3 path.

### Download the result

After the ECS task completes, download the full JSON from S3:

```
aws s3 cp s3://judgemind-document-archive-dev/spotcheck/<timestamp>.json {worktree}/tmp/spotcheck/data.json
```

The S3 path is printed in the ECS task output. Parse it from the logs.

### Download PDFs for review

Collect all unique `s3_key` values from the result JSON (from both `rulings_by_county` and `originals_by_county`). Write a shell script to `{worktree}/tmp/spotcheck/download_pdfs.sh` that downloads each one:

```
aws s3 cp s3://judgemind-document-archive-dev/<s3_key> {worktree}/tmp/spotcheck/pdfs/<basename>
```

Run the download script. Skip keys that are null.

---

## Step 1 — Review rulings direction (DB → Originals)

**Review EVERY sampled ruling, not just flagged ones.** The summary stats (null judges, UNKNOWN case numbers, etc.) are triage hints for where to look harder, but you must check every ruling against its original PDF. Do not skip items because a field looks normal in the JSON — the whole point is to verify the JSON against the source.

For each county in `rulings_by_county`, review all sampled rulings.

For each ruling:

1. **Read the original PDF** — use `pages: "1-20"` (the Read tool's max per request). For PDFs over 20 pages, make a second read with `pages: "21-40"`. Read the entire document to catch cases on later pages.
2. **Compare against the DB record** from the JSON data

Check for:

| Check | What to look for |
|---|---|
| **Case attribution** | Does the ruling text preview match the case_title? Is this actually about a different case from the same multi-case PDF? |
| **Outcome accuracy** | Does DB outcome match the PDF? Watch for: OVERRULED→"denied" (correct for demurrers), MOOT→"granted" (wrong), SUSTAINED→"granted" (correct for demurrers) |
| **Motion type** | Does DB motion_type match the PDF? |
| **Case title** | Any contamination — case numbers, department names, boilerplate in the title? |
| **Judge name** | Does DB judge match the PDF header? Null when PDF clearly shows the judge? |
| **Department** | Does DB department match? Watch for years or fragments misextracted (e.g., "(1982)") |
| **UNKNOWN case numbers** | case_number starts with "UNKNOWN-" but PDF shows the real number? |
| **Truncation** | Compare `ruling_text_length` against the apparent length in the PDF. A multi-page ruling analysis reduced to a few hundred chars is a truncation bug. |

Record findings in `{worktree}/tmp/spotcheck/rulings_findings.md`.

**Systemic patterns:** If you see the same bug repeated across many rulings in one county (e.g., every ruling has wrong case attribution), note the pattern with 2-3 examples and the total count, then move on. But you still must open each PDF and verify — do not assume a ruling is clean based on the JSON alone.

---

## Step 2 — Review originals direction (S3 → DB)

**Review EVERY sampled original, not just flagged ones.** The `all_same_case` and `zero_derived` flags are triage hints, but you must open each PDF and verify all derived rulings against the source. A document with `all_same_case: false` and 7 distinct titles can still have wrong outcomes, wrong motion types, or truncated text.

For each county in `originals_by_county`, review all sampled originals.

For each selected original:

1. **Read the original PDF** — use `pages: "1-20"`. For PDFs over 20 pages, make a second read. Read the full document to count all cases.
2. **Compare against derived rulings** from the JSON data

Check for:

| Check | What to look for |
|---|---|
| **All-same-case bug** | `all_same_case: true` — all derived rulings have the same case_title despite the PDF having multiple distinct cases (#2195) |
| **Missing rulings** | PDF has N cases but `derived_count` is lower — cases dropped during splitting |
| **Zero derived** | Document exists in DB but produced no rulings at all |
| **Content fidelity** | Do ruling_text_previews match the actual PDF content? |
| **Ruling count** | For multi-case PDFs, does derived_count match the case count in the PDF? |
| **Truncation** | Compare `ruling_text_length` per ruling against the apparent length in the PDF. Multi-page analyses reduced to a few hundred chars is a truncation bug. |

Record findings in `{worktree}/tmp/spotcheck/originals_findings.md`.

### 2.5 — S3 orphan check (supplementary)

The originals sampled in Step 2 come from the documents table, so they always have DB rows. To also catch **orphaned S3 objects** (archived but never ingested), run the S3-based sampler for 2-3 counties:

> **Two-shape S3 key gotcha (see #2583):** S3 contains two key shapes — legacy date-partitioned keys (`raw/YYYY/MM/DD/<uuid>.ext`) and flat-hash keys (`raw/<sha256-hex>.<ext>`). The sampler pulls from all S3 objects under `ca/<county>/`, so date-partitioned keys (which the DB never references by that path) inflate the orphan rate to 60–90% regardless of actual bugs. Until #2629 lands a proper regression guard (`scripts/spotcheck/check_s3_orphan_rate.py`), apply the post-filter below to get a meaningful orphan count.

```
scripts/run-py.sh scripts/spotcheck/sample.py --from originals --county "<County>" --n 10 --output {worktree}/tmp/spotcheck/s3_sample_<county>.json
```

After sampling, filter to flat-hash keys only before computing the orphan rate (regex matches `scripts/archive/migrate_s3_keys.py`'s `NEW_KEY_PATTERN`):

```python
import json, re
flat = re.compile(r".*/raw/[0-9a-f]{64}\.[a-z]+$")
data = json.load(open("{worktree}/tmp/spotcheck/s3_sample_<county>.json"))
flat_keys = [k for k in data["s3_keys"] if flat.match(k)]
print(f"{len(flat_keys)} flat-hash keys out of {len(data['s3_keys'])} sampled")
```

Compare `flat_keys` against the `originals_by_county` data. Any flat-hash key not in the documents table is an orphan — note the count per county. Tracking the flat-hash orphan rate over time helps measure progress (see #2583).

---

## Step 3 — Screenshots (optional, 3-5 per county)

If time permits, take screenshots of a few ruling detail pages:

```
scripts/run-py.sh scripts/screenshot.py /rulings/<ruling_id> \
    --output {worktree}/tmp/spotcheck/screenshots/<ruling_id>.png --full-page
```

Check for garbled titles, missing fields, layout issues. Record in `{worktree}/tmp/spotcheck/visual_findings.md`.

---

## Step 4 — Cross-reference and file issues

### 4.1 — Fetch open issues

Use MCP — returns full typed objects (number, title, body, labels, assignees) in one call:

```
mcp__github__list_issues
  owner: "judgemind"
  repo: "judgemind"
  state: "open"
  labels: ["type/bug"]
  per_page: 200
```

### 4.2 — Classify findings

For each finding, classify as:
- **New** — no existing open issue covers this pattern
- **Known (extends)** — existing issue covers the pattern, new examples found
- **Known (duplicate)** — already fully covered

**File per-pattern, not per-case.** If 5 rulings across 3 counties show the same bug, that's one issue.

### 4.3 — File new issues

Write each issue body to `{worktree}/tmp/spotcheck/issue_N.txt`. Include:
- **Found by:** `/spotcheck` skill
- **Affected counties** with example ruling IDs
- **Acceptance criteria** with `Verify:` lines

**File spotcheck issues with `agent/ready` by default.** The dispatcher should be able to pick them up immediately — spotcheck findings are concrete, evidence-backed, and per-pattern, so they meet the bar. Only omit `agent/ready` when there's a specific reason:
- **Blocked by an open dependency** — use `scripts/block-issue.sh <new-issue> <blocker>` instead. This adds `status/blocked` and a `Blocked by #N` line that auto-unblocks when the blocker closes.
- **Acceptance criteria not yet concrete** — e.g. "investigate X" with no clear success condition. Tighten the AC before filing, or file with `status/triage` for human review.
- **Needs a maintainer decision** — scope/priority ambiguity that an agent can't resolve.

```
gh issue create --repo judgemind/judgemind \
    --title "fix(scraping): <description>" \
    --label "type/bug,priority/<p1|p2>,area/scraping,agent/ready" \
    --body-file {worktree}/tmp/spotcheck/issue_N.txt
```

---

## Step 5 — Summary report

Write to `{worktree}/tmp/spotcheck/report.md` and print to stdout:

```markdown
# Spotcheck Report — YYYY-MM-DD

## Coverage
- Counties checked: N / N active
- Rulings sampled (DB → Originals): N per county
- Originals sampled (DB docs → Derived): N per county

## Quick Stats (from spotcheck script)
| County | Rulings | Originals | Null Judges | UNKNOWN Cases | Same-Case Bug | Zero Derived |
|--------|---------|-----------|-------------|---------------|---------------|--------------|
| ...    | ...     | ...       | ...         | ...           | ...           | ...          |

## Findings by Direction

### Rulings → Originals
[Per-county findings summary with examples]

### Originals → Rulings
[Per-county findings summary with examples]

## Issues Filed / Extended
- #N — title (severity)

## Overall Assessment
[Good / Needs attention / Critical]
```

---

## What NOT to do

- **Do not fix issues directly.** Spotcheck is diagnostic only.
- **Do not re-file existing issues.** Cross-reference first.
- **Do not set `priority/p0`.** Human-only.
- **Do not modify source files.** Read and report only.
- **Do not screenshot production.** Only `dev.judgemind.org`.
- **Do not use `dev-db-query.sh` in scripts.** Use `ecs-run-task.sh` for all DB queries.
- **Large PDFs:** Read tool max is 20 pages per request. For PDFs over 20 pages, make multiple reads (`pages: "1-20"`, then `pages: "21-40"`).

---

## Reminders

- **No `$()` in Bash.** Separate tool calls for dynamic values.
- **No heredocs or `python -c`.** Write scripts to files first.
- **All temp files in `{worktree}/tmp/`**, not `/tmp/`.
- **`timeout: 1200000`** on `ecs-run-task.sh` commands.
- **Judge name: `judges.canonical_name`**, not `name`.
- **S3 bucket: `judgemind-document-archive-dev`**.
- **S3 prefix: lowercase with underscores** (`ca/orange/`, not `CA/Orange/`).
