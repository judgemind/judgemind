---
description: Periodic data quality spot-check — samples rulings across counties, runs automated DB queries for known issue patterns, compares source documents against DB records, takes screenshots for visual inspection, cross-references existing issues, and files new issues for findings. Run manually or on a schedule.
argument-hint: ""
maxTurns: 200
---

# /spotcheck skill

Perform a periodic data quality spot-check on the Judgemind rulings database. Samples rulings across all active counties, runs automated DB queries for known issue patterns, compares original source documents (PDF, HTML, etc.) against extracted DB records, takes screenshots of case detail pages for visual inspection, cross-references existing open issues to avoid duplicates, and files new issues for genuinely new findings.

**Trigger:** Invoke manually with `/spotcheck`, or schedule via the dispatcher.

**Prerequisites:** Must be in a worktree. Requires database access via `scripts/dev-db-query.sh`, screenshot capability via `scripts/run-py.sh scripts/screenshot.py`, and S3 access via `aws s3 cp`.

Do not ask for confirmation. Work autonomously through every step.

---

## Step 0 — Setup

Create a working directory for spotcheck state:

```
mkdir -p {worktree}/tmp/spotcheck/screenshots
mkdir -p {worktree}/tmp/spotcheck/source-docs
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

**Important:** The schema has `courts.county` (text field on the courts table), NOT a separate `counties` table. Parties are in `parties` + `case_parties` (joined by `case_id`), NOT `ruling_parties`. Case title is on `cases.case_title`, NOT on rulings. Content hash is `rulings.ruling_text_hash`, NOT `content_hash`.

### 1.1 — Get active counties and their ruling counts

```
scripts/dev-db-query.sh "SELECT co.county, COUNT(r.id) AS ruling_count, MAX(r.created_at) AS latest_ruling FROM rulings r JOIN courts co ON r.court_id = co.id GROUP BY co.county ORDER BY co.county"
```

Record the output — this is the baseline for sampling.

### 1.2 — Sample recent rulings (last 7 days, spread across counties)

```
scripts/dev-db-query.sh "SELECT r.id, ca.id AS case_id, ca.case_number, ca.case_title, co.county, r.created_at FROM rulings r JOIN cases ca ON r.case_id = ca.id JOIN courts co ON r.court_id = co.id WHERE r.created_at > NOW() - INTERVAL '7 days' ORDER BY co.county, RANDOM() LIMIT 50"
```

### 1.3 — Sample older rulings (random from all time)

Pull a random sample of older rulings for comparison:

```
scripts/dev-db-query.sh "SELECT r.id, ca.id AS case_id, ca.case_number, ca.case_title, co.county, r.created_at FROM rulings r JOIN cases ca ON r.case_id = ca.id JOIN courts co ON r.court_id = co.id ORDER BY RANDOM() LIMIT 20"
```

Save all sampled ruling IDs and case IDs to `{worktree}/tmp/spotcheck/sample.txt` for use in later steps.

---

## Step 2 — Database-level automated checks

Run each of the following queries and record findings. For each query that returns results, note the count, sample records, and severity.

### 2.1 — Garbled or overly long case titles

```
scripts/dev-db-query.sh "SELECT ca.id AS case_id, ca.case_number, ca.case_title, LENGTH(ca.case_title) AS title_len, co.county FROM cases ca JOIN courts co ON ca.court_id = co.id WHERE LENGTH(ca.case_title) > 100 ORDER BY LENGTH(ca.case_title) DESC LIMIT 20"
```

### 2.2 — Case titles containing header/column-merge text

```
scripts/dev-db-query.sh "SELECT ca.id AS case_id, ca.case_number, ca.case_title, co.county FROM cases ca JOIN courts co ON ca.court_id = co.id WHERE ca.case_title ILIKE '%Before the court%' OR ca.case_title ILIKE '%Judicial Officer%' OR (ca.case_title ILIKE '%Motion%' AND ca.case_title NOT ILIKE '%v.%' AND ca.case_title NOT ILIKE '%vs%') LIMIT 20"
```

**Note:** Filter for patterns that clearly indicate parsing errors — titles that contain ruling text, department numbers, or judicial officer names are the real signal. Titles containing "Motion" but also "v." are likely legitimate (e.g., "Smith v. Jones" where the case involves a motion).

### 2.3 — UNKNOWN case numbers

```
scripts/dev-db-query.sh "SELECT co.county, COUNT(*) FROM cases ca JOIN courts co ON ca.court_id = co.id WHERE ca.case_number LIKE 'UNKNOWN-%' GROUP BY co.county"
```

### 2.4 — Null case titles

```
scripts/dev-db-query.sh "SELECT co.county, COUNT(*) FROM cases ca JOIN courts co ON ca.court_id = co.id WHERE ca.case_title IS NULL GROUP BY co.county"
```

### 2.5 — Duplicate rulings (same case_id + identical ruling text hash)

```
scripts/dev-db-query.sh "SELECT r.case_id, r.ruling_text_hash, COUNT(*) AS dup_count FROM rulings r WHERE r.ruling_text_hash IS NOT NULL GROUP BY r.case_id, r.ruling_text_hash HAVING COUNT(*) > 1 ORDER BY dup_count DESC LIMIT 20"
```

### 2.6 — Very short ruling text (potential boilerplate-only)

```
scripts/dev-db-query.sh "SELECT r.id, ca.case_number, ca.case_title, co.county, LENGTH(r.ruling_text) AS text_len, LEFT(r.ruling_text, 200) AS text_preview FROM rulings r JOIN cases ca ON r.case_id = ca.id JOIN courts co ON r.court_id = co.id WHERE r.ruling_text IS NOT NULL AND LENGTH(r.ruling_text) < 100 ORDER BY LENGTH(r.ruling_text) LIMIT 20"
```

### 2.7 — Very long ruling text (potential unsplit calendars)

```
scripts/dev-db-query.sh "SELECT r.id, ca.case_number, ca.case_title, co.county, LENGTH(r.ruling_text) AS text_len FROM rulings r JOIN cases ca ON r.case_id = ca.id JOIN courts co ON r.court_id = co.id WHERE r.ruling_text IS NOT NULL AND LENGTH(r.ruling_text) > 20000 ORDER BY LENGTH(r.ruling_text) DESC LIMIT 20"
```

### 2.8 — Cases with no parties listed

```
scripts/dev-db-query.sh "SELECT r.id, ca.case_number, ca.case_title, co.county FROM rulings r JOIN cases ca ON r.case_id = ca.id JOIN courts co ON r.court_id = co.id LEFT JOIN case_parties cp ON ca.id = cp.case_id WHERE cp.id IS NULL LIMIT 20"
```

### 2.9 — Party names that look like ruling text or court headers

```
scripts/dev-db-query.sh "SELECT p.id, p.canonical_name, ca.case_number FROM parties p JOIN case_parties cp ON p.id = cp.party_id JOIN cases ca ON cp.case_id = ca.id WHERE p.canonical_name ILIKE '%Before the Court%' OR p.canonical_name ILIKE '%Law And Motion Rulings%' OR p.canonical_name ILIKE '%Hearing Date%' OR p.canonical_name ILIKE '%Motion For%' OR LENGTH(p.canonical_name) > 120 LIMIT 20"
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

## Step 3 — Source document comparison

This is the highest-signal check. Download original source documents from S3 and compare them against what was extracted into the database. This catches truncation, cross-contamination, and field extraction errors that DB-only checks cannot detect.

### 3.1 — Sample documents for comparison

Pull a **random sample** of 10-15 documents across all counties and formats. Do not bias by motion type, county, or format — random sampling over repeated spotchecks gives the best coverage over time.

```
scripts/dev-db-query.sh "SELECT d.id, d.s3_key, d.s3_bucket, d.format, d.hearing_date, co.county FROM documents d JOIN courts co ON d.court_id = co.id WHERE d.s3_key IS NOT NULL ORDER BY RANDOM() LIMIT 15"
```

### 3.2 — For each sampled document

**a) Download the source document from S3:**

```
aws s3 cp s3://<bucket>/<s3_key> {worktree}/tmp/spotcheck/source-docs/<filename>
```

**b) Read the source document** using the Read tool. The Read tool handles PDFs (renders pages visually), HTML files, and other text formats natively. For large PDFs (>10 pages), read the first 10 pages.

**c) Query the database** for all rulings linked to this document:

```
scripts/dev-db-query.sh "SELECT r.id, ca.case_number, ca.case_title, r.motion_type, r.outcome, LENGTH(r.ruling_text) AS text_len, LEFT(r.ruling_text, 300) AS text_start, r.ruling_text AS full_text FROM rulings r JOIN cases ca ON r.case_id = ca.id WHERE r.document_id = '<doc_id>'"
```

**d) Compare source vs DB** and check for:

1. **Completeness — are all cases from the source represented in the DB?**
   - Count the distinct cases/entries in the source document
   - Count the rulings in the DB linked to this document
   - If the source has more cases than the DB has rulings, cases were dropped during splitting

2. **Ruling text fidelity — does the DB capture the full content?**
   - Does the ruling_text in the DB include the judge's full analysis/reasoning?
   - Or does it only contain the disposition summary (e.g., "Motion granted", "DENY Defendant's Motion")?
   - A multi-page analysis reduced to one line is a **p1 truncation bug**

3. **Correct case assignment — is each ruling linked to the right case?**
   - Does the case_number in the DB match the case number shown in the source for that ruling?
   - Does the ruling_text content match the source content for that case?
   - Cross-contamination (ruling from case A stored under case B) is a **p1 bug**

4. **Field accuracy — do extracted fields match the source?**
   - case_title: does it match the parties shown in the source?
   - motion_type: does it match the motion described in the source?
   - outcome: does it match the ruling in the source?
   - judge_name: does it match the judge shown in the source (if visible)?

### 3.3 — Recording source comparison findings

For each document comparison, record in `{worktree}/tmp/spotcheck/source_findings.md`:

- Document ID, S3 key, county, format, hearing date
- Number of cases in source vs number of rulings in DB
- For each discrepancy found:
  - Which check failed (completeness, fidelity, assignment, accuracy)
  - Specific details (e.g., "MSJ ruling for CVRI2501693 has 3 pages of analysis in PDF but only 121 chars in DB")
  - Severity: p1 for truncation/cross-contamination, p2 for minor field inaccuracies
- If no issues found, note "Clean — source and DB match"

**Prioritize depth over breadth.** If a document reveals a systemic issue (e.g., all rulings from a county are truncated), note the pattern and move on — don't need to verify every document from that county.

---

## Step 4 — Visual inspection via screenshots

Screenshot 5-10 case detail pages from the sample collected in Step 1. Mix recent and older rulings, and spread across counties. Include any cases flagged in Steps 2-3.

For each case:

```
scripts/run-py.sh scripts/screenshot.py /cases/<case_id> --output {worktree}/tmp/spotcheck/screenshots/case-<short_id>.png --full-page
```

After taking the screenshot, read it with the Read tool and analyze for:

- **Garbled or truncated titles** — text that looks like parsing errors, column merges, or HTML artifacts.
- **Missing data fields** — judge name, hearing date, parties, or motion type showing as empty or "Unknown".
- **Layout problems** — overlapping text, broken formatting, missing sections.
- **Suspiciously short ruling text** — especially for substantive motions (MSJ, demurrer) where you'd expect detailed analysis.
- **Formatting errors** — raw HTML tags visible, broken lists, or misaligned text.

Record visual findings in `{worktree}/tmp/spotcheck/visual_findings.md` with:
- Case ID and URL
- Screenshot filename
- Description of the issue
- Severity assessment

---

## Step 5 — Cross-reference existing issues

**Do not rely on the Step 0 snapshot.** The open issues list fetched at the start of the spotcheck may be stale — other agents may have closed or modified issues during Steps 1-4. Re-fetch the current state before cross-referencing.

### 5.1 — Re-fetch open issues

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

### 5.2 — Verify individual issue state before classifying as "known"

Before marking any finding as **Known (extends)** or **Known (duplicate)** based on a matching issue #N, verify that #N is still open:

```
gh issue view <N> --repo judgemind/judgemind --json state -q '.state'
```

If the issue has been closed since the list was fetched, treat the finding as **New** instead. Do not reference closed issues as "known" — the fix may have already shipped, or the issue may have been closed as stale.

### 5.3 — Classify findings

For each finding from Steps 2, 3, and 4, search the refreshed open issues list for:
- Issues mentioning the same county + problem type.
- Issues mentioning the same case ID.
- Issues with similar titles (e.g., "truncated rulings in Riverside", "UNKNOWN case numbers in OC").

Mark each finding as:
- **New** — no existing open issue covers this pattern or county.
- **Known (extends)** — an existing open issue covers the same pattern but this finding adds new affected counties or examples. Comment on the existing issue instead of filing a new one.
- **Known (duplicate)** — an existing open issue already covers this exact finding. Skip.

Write the cross-reference results to `{worktree}/tmp/spotcheck/crossref.md`.

---

## Step 6 — File issues for new findings

For each finding marked as **New** in Step 5:

1. Write the issue body to `{worktree}/tmp/spotcheck/issue_N.txt`.
2. Include in the body:
   - **Found by:** `/spotcheck` skill (periodic data quality check)
   - **Category:** which check found it (DB query, source comparison, visual inspection, or combination)
   - **Affected county/counties:** list all affected counties
   - **Examples:** concrete case IDs, DB query results, source document S3 keys, and/or screenshot references
   - **Suggested fix:** which scraper or extraction step likely needs updating
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

## Step 7 — Summary report

Write a summary report to `{worktree}/tmp/spotcheck/report.md`:

```markdown
# Spotcheck Report — YYYY-MM-DD

## Coverage
- Counties checked: N / N total active
- Recent rulings sampled: N (from last 7 days)
- Older rulings sampled: N (random historical)
- Source documents compared: N (across N counties)
- Case detail pages screenshotted: N

## Database Check Results

| Check | Count | Severity | Action |
|-------|-------|----------|--------|
| Long case titles (>100 chars) | N | p2 | Filed #N / Known #N / None |
| Header-merge titles | N | p1 | Filed #N / Known #N / None |
| UNKNOWN case numbers | N | p1 | Filed #N / Known #N / None |
| Null case titles | N | p2 | Filed #N / Known #N / None |
| Duplicate rulings | N | p1 | Filed #N / Known #N / None |
| Very short ruling text | N | p2 | Filed #N / Known #N / None |
| Very long ruling text (unsplit) | N | p1 | Filed #N / Known #N / None |
| No parties listed | N | p2 | Filed #N / Known #N / None |
| Party names = ruling text | N | p2 | Filed #N / Known #N / None |

## Source Document Comparison Results
- Documents compared: N
- Clean (source matches DB): N
- Discrepancies found: N
- [List each discrepancy with document ID, county, and description]

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

## Step 8 — Clean up

The spotcheck skill does not create a PR or modify code. Worktree cleanup is handled automatically by Claude Code when the agent exits.

---

## What NOT to do

- **Do not fix data quality issues directly.** The spotcheck skill is diagnostic. Only file issues.
- **Do not re-file issues that already exist.** Always cross-reference Step 5 first.
- **Do not run production scraping.** Only read from the database, S3, and take screenshots of dev.
- **Do not set `priority/p0`.** That priority is reserved for humans.
- **Do not modify any source files, configs, or infrastructure.** Read and report only.
- **Do not screenshot production (judgemind.org).** Only `dev.judgemind.org` is allowed.
- **Do not bias document sampling.** Use random samples — repeated spotchecks over time give full coverage.

---

## Guardrails

- **Time budget:** Complete within a single agent session. If a step is taking too long (e.g., too many documents to compare), reduce the sample size and note the reduced coverage in the report.
- **Signal over noise:** Fewer high-quality findings are better than many low-quality ones. Only file issues for patterns that affect data quality for users.
- **Err toward filing:** If unsure whether something is a real issue, file it with p2 severity. The maintainer can close it if not actionable.
- **Adapt queries to the actual schema.** The queries above are templates. If a table or column name differs in the actual schema, adjust accordingly. Run a quick `scripts/dev-db-query.sh "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"` if needed.
- **Large PDFs:** For PDFs with more than 10 pages, read the first 10 pages only (the Read tool requires a `pages` parameter for large PDFs). Note the truncated read in findings.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Always Read before Write** for existing files.
