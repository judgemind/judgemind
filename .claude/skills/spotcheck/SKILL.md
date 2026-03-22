---
description: Periodic data quality spot-check — samples rulings across counties, runs automated DB queries for known issue patterns, takes screenshots for visual inspection, cross-references existing issues, and files new issues for findings. Run manually or on a schedule.
argument-hint: ""
maxTurns: 200
---

# /spotcheck skill

Perform a periodic data quality spot-check on the Judgemind rulings database. Samples rulings across all active counties, runs automated DB queries for known issue patterns, takes screenshots of case detail pages for visual inspection, cross-references existing open issues to avoid duplicates, and files new issues for genuinely new findings.

**Trigger:** Invoke manually with `/spotcheck`, or schedule via the dispatcher.

**Prerequisites:** Must be in a worktree. Requires database access via `scripts/dev-db-query.sh` and screenshot capability via `scripts/run-py.sh scripts/screenshot.py`.

Do not ask for confirmation. Work autonomously through every step.

---

## Step 0 — Setup

Create a working directory for spotcheck state:

```
mkdir -p {worktree}/tmp/spotcheck/screenshots
```

Fetch the current list of open data-quality-related issues to avoid filing duplicates:

```
gh issue list --repo judgemind/judgemind --state open \
    --label "type/bug" \
    --json number,title,body,labels --limit 200
```

Store the result in `{worktree}/tmp/spotcheck/open_issues.json` for reference throughout the spotcheck.

Also fetch open issues labeled `type/data-quality` or with "data quality" in the title:

```
gh issue list --repo judgemind/judgemind --state open \
    --search "data quality" \
    --json number,title,body,labels --limit 100
```

Combine both lists (deduplicating by issue number) into the open issues reference file.

---

## Step 1 — Sample rulings across counties

Query the database to get a representative sample of rulings across all active counties. The goal is coverage — not just recent records from the busiest counties.

### 1.1 — Get active counties and their ruling counts

```
scripts/dev-db-query.sh "SELECT c.name AS county, COUNT(r.id) AS ruling_count, MAX(r.created_at) AS latest_ruling FROM rulings r JOIN courts co ON r.court_id = co.id JOIN counties c ON co.county_id = c.id GROUP BY c.name ORDER BY c.name"
```

Record the output — this is the baseline for sampling.

### 1.2 — Sample recent rulings (last 7 days, spread across counties)

For each active county, pull 2-3 recent rulings:

```
scripts/dev-db-query.sh "SELECT r.id, r.case_id, r.case_title, c.name AS county, r.created_at FROM rulings r JOIN courts co ON r.court_id = co.id JOIN counties c ON co.county_id = c.id WHERE r.created_at > NOW() - INTERVAL '7 days' ORDER BY c.name, RANDOM() LIMIT 50"
```

### 1.3 — Sample older rulings (random from all time)

Pull a random sample of older rulings for comparison:

```
scripts/dev-db-query.sh "SELECT r.id, r.case_id, r.case_title, c.name AS county, r.created_at FROM rulings r JOIN courts co ON r.court_id = co.id JOIN counties c ON co.county_id = c.id ORDER BY RANDOM() LIMIT 20"
```

Save all sampled ruling IDs and case IDs to `{worktree}/tmp/spotcheck/sample.txt` for use in later steps.

---

## Step 2 — Database-level automated checks

Run each of the following queries and record findings. For each query that returns results, note the count, sample records, and severity.

### 2.1 — Garbled or overly long case titles

```
scripts/dev-db-query.sh "SELECT id, case_id, case_title, LENGTH(case_title) AS title_len FROM rulings WHERE LENGTH(case_title) > 100 ORDER BY LENGTH(case_title) DESC LIMIT 20"
```

### 2.2 — Case titles containing header/column-merge text

```
scripts/dev-db-query.sh "SELECT id, case_id, case_title FROM rulings WHERE case_title ILIKE '%Before the court%' OR case_title ILIKE '%Department %' OR case_title ILIKE '%Motion%' OR case_title ILIKE '%Petition%' LIMIT 20"
```

**Note:** Some of these may be legitimate (e.g. "Petition of Smith" is a valid case title). Filter for patterns that clearly indicate parsing errors — titles that contain motion text, department numbers, or judicial officer names are the real signal.

### 2.3 — ALL CAPS case titles

```
scripts/dev-db-query.sh "SELECT id, case_id, case_title FROM rulings WHERE case_title = UPPER(case_title) AND LENGTH(case_title) > 20 LIMIT 20"
```

### 2.4 — Duplicate rulings (same case_id + identical content hash)

```
scripts/dev-db-query.sh "SELECT case_id, content_hash, COUNT(*) AS dup_count FROM rulings WHERE content_hash IS NOT NULL GROUP BY case_id, content_hash HAVING COUNT(*) > 1 ORDER BY dup_count DESC LIMIT 20"
```

### 2.5 — Ruling text starting with redundant metadata

```
scripts/dev-db-query.sh "SELECT r.id, r.case_id, r.case_title, LEFT(r.ruling_text, 200) AS text_start FROM rulings r WHERE r.ruling_text IS NOT NULL AND (r.ruling_text ILIKE r.case_title || '%' OR r.ruling_text ~ '^(Department|Dept\.?\s+\d|Case No|Judicial Officer)') LIMIT 20"
```

### 2.6 — Very short ruling text (potential boilerplate-only)

```
scripts/dev-db-query.sh "SELECT id, case_id, case_title, LENGTH(ruling_text) AS text_len, LEFT(ruling_text, 200) AS text_preview FROM rulings WHERE ruling_text IS NOT NULL AND LENGTH(ruling_text) < 100 ORDER BY LENGTH(ruling_text) LIMIT 20"
```

### 2.7 — Very long ruling text (potential unsplit calendars)

```
scripts/dev-db-query.sh "SELECT id, case_id, case_title, LENGTH(ruling_text) AS text_len FROM rulings WHERE ruling_text IS NOT NULL AND LENGTH(ruling_text) > 20000 ORDER BY LENGTH(ruling_text) DESC LIMIT 20"
```

### 2.8 — Cases with no parties listed

```
scripts/dev-db-query.sh "SELECT r.id, r.case_id, r.case_title, c.name AS county FROM rulings r JOIN courts co ON r.court_id = co.id JOIN counties c ON co.county_id = c.id LEFT JOIN ruling_parties rp ON r.id = rp.ruling_id WHERE rp.id IS NULL LIMIT 20"
```

### 2.9 — Party names that look like motion text

```
scripts/dev-db-query.sh "SELECT rp.id, rp.name, r.case_id FROM ruling_parties rp JOIN rulings r ON rp.ruling_id = r.id WHERE rp.name ILIKE '%motion%' OR rp.name ILIKE '%petition%' OR rp.name ILIKE '%hearing%' OR rp.name ILIKE '%department%' OR LENGTH(rp.name) > 80 LIMIT 20"
```

### Recording findings

For each query:
1. Record the count of results.
2. If count > 0, save representative examples (up to 5) with case IDs, county, and the problematic field value.
3. Assess severity:
   - **p1** — Duplicate rulings, unsplit calendars, or garbled titles affecting > 10 cases across multiple counties.
   - **p2** — Isolated issues in a single county, or cosmetic problems (ALL CAPS, redundant metadata).

Write all findings to `{worktree}/tmp/spotcheck/db_findings.md`.

---

## Step 3 — Visual inspection via screenshots

Screenshot 10-15 case detail pages from the sample collected in Step 1. Mix recent and older rulings, and spread across counties.

For each case:

```
scripts/run-py.sh scripts/screenshot.py /cases/<case_id> --output {worktree}/tmp/spotcheck/screenshots/case-<short_id>.png --full-page
```

After taking the screenshot, read it with the Read tool and analyze for:

- **Garbled or truncated titles** — text that looks like parsing errors, column merges, or HTML artifacts.
- **Missing data fields** — judge name, hearing date, parties, or motion type showing as empty or "Unknown".
- **Layout problems** — overlapping text, broken formatting, missing sections.
- **Redundant text** — ruling text that starts with the case number, department header, or judge name (duplicating the page header).
- **Formatting errors** — raw HTML tags visible, broken lists, or misaligned text.
- **Very short or very long rulings** — boilerplate-only content or unsplit multi-case calendars.

Record visual findings in `{worktree}/tmp/spotcheck/visual_findings.md` with:
- Case ID and URL
- Screenshot filename
- Description of the issue
- Severity assessment

---

## Step 4 — Cross-reference existing issues

**Do not rely on the Step 0 snapshot.** The open issues list fetched at the start of the spotcheck may be stale — other agents may have closed or modified issues during Steps 1-3. Re-fetch the current state before cross-referencing.

### 4.1 — Re-fetch open issues

Re-run the same queries from Step 0 to get a fresh list of open issues:

```
gh issue list --repo judgemind/judgemind --state open \
    --label "type/bug" \
    --json number,title,body,labels --limit 200
```

```
gh issue list --repo judgemind/judgemind --state open \
    --search "data quality" \
    --json number,title,body,labels --limit 100
```

Combine and deduplicate as in Step 0, overwriting `{worktree}/tmp/spotcheck/open_issues.json` with the fresh data.

### 4.2 — Verify individual issue state before classifying as "known"

Before marking any finding as **Known (extends)** or **Known (duplicate)** based on a matching issue #N, verify that #N is still open:

```
gh issue view <N> --repo judgemind/judgemind --json state -q '.state'
```

If the issue has been closed since the list was fetched, treat the finding as **New** instead. Do not reference closed issues as "known" — the fix may have already shipped, or the issue may have been closed as stale.

### 4.3 — Classify findings

For each finding, search the refreshed open issues list for:
- Issues mentioning the same county + problem type.
- Issues mentioning the same case ID.
- Issues with similar titles (e.g., "garbled titles in [county]", "duplicate rulings in [county]").

Mark each finding as:
- **New** — no existing open issue covers this pattern or county.
- **Known (extends)** — an existing open issue covers the same pattern but this finding adds new affected counties or examples. Comment on the existing issue instead of filing a new one.
- **Known (duplicate)** — an existing open issue already covers this exact finding. Skip.

Write the cross-reference results to `{worktree}/tmp/spotcheck/crossref.md`.

---

## Step 5 — File issues for new findings

For each finding marked as **New** in Step 4:

1. Write the issue body to `{worktree}/tmp/spotcheck/issue_N.txt`.
2. Include in the body:
   - **Found by:** `/spotcheck` skill (periodic data quality check)
   - **Category:** which check found it (DB query, visual inspection, or both)
   - **Affected county/counties:** list all affected counties
   - **Examples:** concrete case IDs, DB query results, and/or screenshot references
   - **Suggested fix:** which scraper or parser likely needs updating
   - Acceptance criteria with `Verify:` lines containing the specific DB query or URL to check
3. Create the issue:

```
gh issue create --repo judgemind/judgemind \
    --title "fix(scraping): <description> in <county>" \
    --label "type/bug,priority/<p1|p2>,agent/ready,area/scraping" \
    --body-file {worktree}/tmp/spotcheck/issue_N.txt
```

For findings marked as **Known (extends)**, add a comment on the existing issue with the new examples:

```
gh issue comment <N> --repo judgemind/judgemind --body-file {worktree}/tmp/spotcheck/extend_N.txt
```

---

## Step 6 — Summary report

Write a summary report to `{worktree}/tmp/spotcheck/report.md`:

```markdown
# Spotcheck Report — YYYY-MM-DD

## Coverage
- Counties checked: N / N total active
- Recent rulings sampled: N (from last 7 days)
- Older rulings sampled: N (random historical)
- Case detail pages screenshotted: N

## Database Check Results

| Check | Count | Severity | Action |
|-------|-------|----------|--------|
| Long case titles (>100 chars) | N | p2 | Filed #N / Known #N / None |
| Header-merge titles | N | p1 | Filed #N / Known #N / None |
| ALL CAPS titles | N | p2 | Filed #N / Known #N / None |
| Duplicate rulings | N | p1 | Filed #N / Known #N / None |
| Redundant metadata in text | N | p2 | Filed #N / Known #N / None |
| Very short ruling text | N | p2 | Filed #N / Known #N / None |
| Very long ruling text (unsplit) | N | p1 | Filed #N / Known #N / None |
| No parties listed | N | p2 | Filed #N / Known #N / None |
| Party names = motion text | N | p2 | Filed #N / Known #N / None |

## Visual Inspection Results
- Pages inspected: N
- Issues found: N
- [List each visual issue with case ID and description]

## Issues Filed
- #N — title (severity)
- ...

## Issues Extended (commented on existing)
- #N — added N new examples from N counties

## Summary
- New issues filed: N
- Existing issues extended: N
- Duplicate findings skipped: N
- Overall data quality assessment: [Good / Needs attention / Critical issues found]
```

Print the report to stdout as well.

---

## Step 7 — Clean up

The spotcheck skill does not create a PR or modify code. Worktree cleanup is handled automatically by Claude Code when the agent exits (if spawned with `isolation: "worktree"`). For manual worktrees, run `scripts/end-worker.sh {worktree}`.

---

## What NOT to do

- **Do not fix data quality issues directly.** The spotcheck skill is diagnostic. Only file issues.
- **Do not re-file issues that already exist.** Always cross-reference Step 4 first.
- **Do not run production scraping.** Only read from the database and take screenshots of dev.
- **Do not set `priority/p0`.** That priority is reserved for humans.
- **Do not modify any source files, configs, or infrastructure.** Read and report only.
- **Do not screenshot production (judgemind.org).** Only `dev.judgemind.org` is allowed.

---

## Guardrails

- **Time budget:** Complete within a single agent session. If a step is taking too long (e.g., too many screenshots), reduce the sample size and note the reduced coverage in the report.
- **Signal over noise:** Fewer high-quality findings are better than many low-quality ones. Only file issues for patterns that affect data quality for users.
- **Err toward filing:** If unsure whether something is a real issue, file it with p2 severity. The maintainer can close it if not actionable.
- **Adapt queries to the actual schema.** The queries above are templates. If a table or column name differs in the actual schema, adjust accordingly. Run a quick `scripts/dev-db-query.sh "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"` if needed.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Always Read before Write** for existing files.
