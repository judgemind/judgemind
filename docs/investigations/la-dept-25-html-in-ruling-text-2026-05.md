# LA dept-25 "raw HTML in ruling_text" reingest failure — 2026-05

**Issue:** #4382 (parent), #4386 (registry-loader fix), #4397 (residual-NULL follow-up)
**Status:** Closed end-to-end. #4386 fix landed in PR #4394 (119 → 62 NULL); #4397 follow-up identified the residual 62 as a chronological resolver-chain race; a second `--no-llm` reingest after the in-band judge auto-create dropped the count to **0/120**. See §"Residual NULLs after #4386 (2026-05-09)" below.
**Worktree:** `agent-ad2cacb6c1bcfd80c` (#4382), `agent-aa2d6e145af885f85` (#4397)
**Date:** 2026-05-08 (initial); 2026-05-09 (#4397 update)

## TL;DR

The original issue hypothesised that LA dept-25 documents in S3 contain raw HTML in `ruling_text` because **the transcription step was skipped at capture time**. That is **falsified**. The DB rows for these 126 documents already contain clean plain-text `ruling_text` (written by `rebuild_db.py` via the live LA scraper code path). The validation failure surfaces only on **reingest**, not on the stored data.

The actual root cause is a **gap in `scripts/reingest_from_s3.py::_reparse_document`**: when `documents.scraper_id == "rebuild-ca-los_angeles"` (the synthetic id emitted by `rebuild_db.py`), the function looks up `_SCRAPER_REGISTRY.get("rebuild-ca-los_angeles")` and gets `None` because aliases are NOT registered in `_SCRAPER_REGISTRY` — only in `_SPLIT_REGISTRY` and `_LLM_SPLIT_REGISTRY` (registry registration code at `scripts/reingest_from_s3.py:380-412`). Without a registered scraper class, `parse_document()` is never called, **the LA-specific narrowing path is never invoked**, and `extracted["ruling_text"]` stays as the raw decoded HTML string returned by `_extract_text_from_content`. The `check_no_html_in_ruling_text` validation correctly rejects this (validation rule at `packages/scraper-framework/src/validation/deterministic.py:161-181`), the DB write is skipped (`scripts/reingest_from_s3.py:3004-3014`), and the judge resolver call at line 3074 is unreachable.

San Diego has a hand-coded narrowing fallback for the same scenario (`scripts/reingest_from_s3.py:1225-1252`, comment explicitly cites `rebuild-ca-san_diego`). LA does not. This is a missing-fallback bug, not a transcription bug.

## Evidence chain

### 1. The stored DB rows are NOT raw HTML

Query against dev (2026-05-08):

```sql
SELECT count(*) AS total,
       count(*) FILTER (WHERE judge_id IS NULL) AS null_judge,
       count(*) FILTER (WHERE lower(ltrim(ruling_text)) LIKE '<html%'
                              OR lower(ltrim(ruling_text)) LIKE '<!doctype%'
                              OR lower(ltrim(ruling_text)) LIKE '<div%') AS starts_with_html,
       count(*) FILTER (WHERE ruling_text IS NOT NULL
                              AND lower(ltrim(ruling_text)) NOT LIKE '<%') AS starts_with_plain
FROM derived.rulings r
JOIN derived.documents d ON d.id = r.document_id
JOIN derived.courts c ON c.id = d.court_id
WHERE c.county ILIKE 'Los Angeles%' AND r.department = '25';
```

**Result:**
```json
{"total":126,"null_judge":119,"starts_with_html":0,"starts_with_plain":126}
```

Every single one of the 126 rows starts with plain text. Sample head from
`d.id = '94045693-0d6d-52df-ba43-932de609c4e2'`:

```
Case Number:
22STLC01488
Hearing Date:
March 12, 2026
Dept:
HEARING  DATE :
Thurs., March 12, 2026
JUDGE/DEPT :
Mkrtchyan/25
CASE NAME :
Aspire General Ins. Co. v. Garcia
…
```

### 2. The scraper_id for these documents is `rebuild-ca-los_angeles`

```sql
SELECT d.scraper_id, count(*)
FROM derived.rulings r
JOIN derived.documents d ON d.id = r.document_id
JOIN derived.courts c ON c.id = d.court_id
WHERE c.county ILIKE 'Los Angeles%' AND r.department = '25'
GROUP BY d.scraper_id;
```

Result: `[{"scraper_id":"rebuild-ca-los_angeles","n":126}]`

All 126 are rebuild-path documents. The `created_at` timestamps (2026-05-06 23:07–23:08) line up with a recent `rebuild_db.py --county "Los Angeles"` run.

### 3. `rebuild_db.py` correctly produces plain-text `ruling_text`

`scripts/rebuild_db.py:282-284` constructs the ingestion event with `event["ruling_text"] = text` where `text` is the decoded HTML string. The `IngestionWorker` then routes through the **live** LA scraper class (`ca-la-tentatives-civil`) which IS in `_SCRAPER_REGISTRY` — so live ingestion narrows correctly via `LATentativeRulingsScraper.parse_document` → `_split_rulings` → BeautifulSoup `get_text(separator="\n")`.

This is why the stored `ruling_text` is plain text. Capture / transcription is fine.

### 4. The reingest path lacks the narrowing fallback

`scripts/reingest_from_s3.py:1106-1196` (`_reparse_document` HTML branch):

```python
text = _extract_text_from_content(raw_content, doc_format, ...)  # returns raw HTML for HTML format
extracted: dict = {
    "ruling_text": text,
    ...
    "department": None,
    ...
}
scraper_cls = _SCRAPER_REGISTRY.get(scraper_id)   # None for "rebuild-ca-los_angeles"
if scraper_cls:
    # ... parse_document path that overwrites extracted["ruling_text"] with narrowed text
    # ... AND the DB-seed merge for extracted["judge_name"]/extracted["department"]
    ...
# When scraper_cls is None, NEITHER narrowing NOR dept-seed runs.
```

The San Diego narrowing block (`scripts/reingest_from_s3.py:1225-1252`) is the closest cousin and explicitly notes the same pattern in its comment:

> When a document has no registered scraper class (e.g. scraper_id `rebuild-ca-san_diego` from rebuild_db.py), `parse_document` is never called and `extracted["ruling_text"]` remains the full raw HTML page (~50KB).

LA hits the same case but has no equivalent fallback.

### 5. Registry registration confirms the alias gap

`packages/scraper-framework/src/courts/ca/la_tentatives.py:2298`:

```python
_SPLIT_REGISTRY_ALIASES: list[str] = ["rebuild-ca-los_angeles"]
```

`scripts/reingest_from_s3.py:380-412` (registry loader):

```python
_SCRAPER_REGISTRY[scraper_id] = scraper_cls            # canonical id only
...
split_aliases: list[str] = getattr(mod, "_SPLIT_REGISTRY_ALIASES", []) or []
if split_fn is not None and callable(split_fn):
    _SPLIT_REGISTRY[scraper_id] = split_fn
    for alias in split_aliases:
        _SPLIT_REGISTRY[alias] = split_fn              # alias registered here
if llm_split_fn is not None and callable(llm_split_fn):
    _LLM_SPLIT_REGISTRY[scraper_id] = llm_split_fn
    for alias in split_aliases:
        _LLM_SPLIT_REGISTRY[alias] = llm_split_fn       # and here
```

`_SCRAPER_REGISTRY` only ever holds the canonical scraper_id; aliases are split-registry-only. So `_SCRAPER_REGISTRY.get("rebuild-ca-los_angeles")` → `None`.

### 6. The validation log lines come from reingest, not from capture

The issue body quotes:
- `event: "Deterministic validation result (reingest)"` — matches `scripts/reingest_from_s3.py:2964`.
- `event: "Deterministic validation FAIL — skipping DB write"` — matches `scripts/reingest_from_s3.py:3005-3006`.

The reingest validation runs on `extracted["ruling_text"]` (line 2951). Since `extracted["ruling_text"]` is the raw HTML for these LA rebuild rows, validation correctly rejects it, then `continue` at line 3014 skips the DB write — including the resolver call at line 3074 which would have populated `judge_id`.

### 7. The resolver / judge data is fine

Cross-check: `Jonathan H. Eisenman` exists in `derived.judges` for the LA court (id `7e1cfebc-0b4b-4c37-9dac-c927f22c6b42`, court_id matches LA), so PR #4377's seed succeeded. `Karine Mkrtchyan` would similarly resolve via the directory snapshot (the `JUDGE/DEPT: Mkrtchyan/25` line in the ruling text shows up clearly on the dev DB; resolver path is `resolve_judge_from_department` at `packages/scraper-framework/src/ingestion/db.py:1085-1167` which works off `court_directory_snapshots.mapping`, not off `derived.judges.department`).

If the validation failure didn't skip the DB write, the resolver chain would fire correctly on these rulings.

**Note:** all 378 LA judges in `derived.judges` have `department = NULL` after PR #4377 — by design (line 234-236 of `judge_seed.py` only INSERTs `(canonical_name, court_id)`). This is fine; resolution goes through the snapshot mapping, not through `derived.judges.department`.

## Sample affected documents

(All confirmed to have plain-text `ruling_text`, NULL `judge_id`, `scraper_id = rebuild-ca-los_angeles`, dept `25`.)

| document_id | ruling_text head |
|---|---|
| `94045693-0d6d-52df-ba43-932de609c4e2` | `Case Number:\n22STLC01488\n...JUDGE/DEPT:\nMkrtchyan/25` |
| `c457225f-a0da-507e-8d51-80e8977f0bcd` | `Case Number:\n25STLC04013\nHearing Date:\n\nMarch 18, 2026\nDept:` |
| `ec17ae67-6cb1-5ac4-addf-e208e3290ea4` | `Case Number:\n25STLC00058\nHearing Date:\n\nMarch 24, 2026\nDept:****** UPDATED TENT` |

## What the original issue got right and what it missed

Right:
- The reingest is failing on every dept-25 doc.
- The validation rule fires correctly.
- This is not a regression from #4370.

Missed:
- The "raw HTML in ruling_text" only exists transiently inside the reingest function, NOT in S3 and NOT in the DB.
- The fix is in the reingest path, not in the scraper or the transcription step.
- A one-off "re-transcribe" script would not help — there is nothing wrong with the stored `ruling_text`. What's needed is an LA-specific narrowing fallback in `_reparse_document` (mirroring the existing SD block), or registering the scraper class under the alias too.

## Decision: which fix?

There are two viable mechanical fixes; both close the gap. The cheaper, more surgical option is preferred.

### Option A (preferred) — register `_SCRAPER_REGISTRY` under the alias

Change the registry loader at `scripts/reingest_from_s3.py:380` to also register the scraper class under `_SPLIT_REGISTRY_ALIASES`:

```python
_SCRAPER_REGISTRY[scraper_id] = scraper_cls
for alias in split_aliases:
    _SCRAPER_REGISTRY[alias] = scraper_cls
```

This makes `rebuild-ca-{state}-{county}` pseudoscraper ids transparently route through the live scraper's `parse_document`. Side-effects: all rebuild-path documents would now go through `LATentativeRulingsScraper.parse_document` during reingest, which will narrow text AND apply the DB-seed merge for `judge_name`/`department`. This is the same behaviour live ingestion already gets.

Risk: any other county that defines `_SPLIT_REGISTRY_ALIASES` would get the same upgrade. Today only LA uses this aliasing pattern (verified — `git grep _SPLIT_REGISTRY_ALIASES`). SC's format-B path uses a different mechanism. SD uses the hand-coded fallback at line 1235.

### Option B — copy the SD-style fallback for LA

Add a parallel narrowing block under the SD one (`scripts/reingest_from_s3.py:1235-1252`). Wider footprint, more code, easier to forget for the next county.

Recommendation: **Option A**. One-line fix in the registry loader. The followup issue (filed below) prescribes this.

### Side-effect: `extracted["department"]` initialization

Even after Option A lands, the dept-seed merge inside `if scraper_cls:` is now guaranteed to run for LA rebuild rows, so `extracted["department"]` will get populated from `doc_meta["department"]` via `_extract_doc_level_judge_department`. The judge resolver at line 3074 will then fire and populate `judge_id`. Closing the loop end-to-end.

## Acceptance criteria mapping (Issue #4382)

| AC | Status | Evidence |
|----|--------|----------|
| 1. Investigation document identifies whether issue is (a) scraper output bug, (b) skipped transcription, or (c) something else | Met | This document — root cause is **(c) reingest narrowing-fallback gap**, not (a) or (b). File:line evidence: `scripts/reingest_from_s3.py:1106-1196` (gap), `scripts/reingest_from_s3.py:1225-1252` (SD analog), `scripts/reingest_from_s3.py:380-412` (alias registration). |
| 2. If fix is straightforward, produce a one-off re-transcription script | N/A | The hypothesis is falsified — no re-transcription is needed. Fix lives in the reingest registry-load loop. Tracked as a separate follow-up issue (see below). |
| 3. After the transcription fix lands, re-run dept-25 reingest and confirm NULL judge count drops to ≤ ~5 | Deferred to follow-up | The follow-up issue listed below carries the verification-after-fix step. |

## Follow-up issues to file

1. **`fix(reingest): register _SCRAPER_REGISTRY under _SPLIT_REGISTRY_ALIASES so rebuild-ca-los_angeles narrows correctly`** (priority/p2, area/ingestion). One-line fix at `scripts/reingest_from_s3.py:380-412`. Verification: re-run #4297's dept-25 reingest on dev, confirm `det_failed_rules` no longer contains `no_html_in_ruling_text` for these docs and dept-25 NULL judge count drops accordingly.

2. **`investigate: confirm no other rebuild-* scraper_ids hit the same reingest narrowing gap`** (priority/p3, area/ingestion). Audit other counties that define `_SPLIT_REGISTRY_ALIASES` (today only LA, but the same gap could open up for any future addition). Could be folded into the fix's regression test.

## Source-file docstring updates triggered by this investigation (B.1.5)

After re-reading the affected source files, the docstrings below contain claims that **remain accurate** after this investigation — no in-place edits are required:

- `scripts/reingest_from_s3.py:1228-1234` (the SD-narrowing block comment) — accurate; explicitly references `rebuild-ca-san_diego` as the missing-scraper-class case. The fix-issue body recommends extending this comment when the LA fix lands to mention the registry-loader change closing the same gap for LA.
- `packages/scraper-framework/src/courts/ca/la_tentatives.py:2291-2298` (the `_SPLIT_REGISTRY_ALIASES` comment) — accurate; correctly explains that the alias is needed so `audit / drain scripts that key on documents.scraper_id` resolve the splitter on rebuild-path rows. The investigation extends this scenario to reingest as well; if the registry-loader fix in Option A lands, this comment should be expanded to also cite `_SCRAPER_REGISTRY` aliasing. That edit lives in the fix PR, not here.
- `packages/scraper-framework/src/validation/deterministic.py:161-181` (the `check_no_html_in_ruling_text` docstring) — accurate; the rule fires correctly on the input it sees. The bug is upstream (raw HTML being fed to it), not in the rule.

No follow-up issue is needed for docstring updates — the relevant edits are all part of the fix PR's natural scope and tracked under follow-up #1 above.

---

## Residual NULLs after #4386 (2026-05-09) — investigation #4397

After PR #4394 landed and the dept-25 reingest re-ran, NULL `judge_id` count for LA dept-25 dropped from **119 → 62**. Issue #4397 was filed to investigate the residual 62. The four hypothesis classes in #4397's body (alias-map gap, alternate-format regex, missing judge_directory rows, LLM-only recoverable) all turned out to be **falsified** — the actual root cause is a different, fifth class.

### Root cause: chronological resolver-chain race during reingest

Every one of the 62 residual NULLs contains "Mkrtchyan" in `ruling_text`, and 57 of 62 carry the per-case `JUDGE/DEPT: Mkrtchyan/25` form-layout header that PR #4394 wired through. The expansion path at `scripts/reingest_from_s3.py:3138` (`_expand_single_word_judge_surname`) and the resolver at `packages/scraper-framework/src/ingestion/db.py:1212-1346` are wired correctly. The bug is that `_expand_single_word_judge_surname` Step 4 (suffix search at `db.py:1296-1311`) can only resolve "Mkrtchyan" when **`derived.judges` already contains a row whose `canonical_name` ends in "Mkrtchyan"** — and during the post-#4394 reingest, that row didn't exist yet for the first 62 documents in cursor order.

#### Evidence chain

1. **All 62 NULL rows mention Mkrtchyan** (per `tmp/null_classification.sql` — 62/62 contain the substring; 57/62 carry the JUDGE/DEPT header; 0 contain "Eisenman", the directory-snapshot-mapped judge for dept 25).
2. **`Karine Mkrtchyan` was created in `derived.judges` at `2026-05-08 23:43:42.179284`.** No earlier row exists.
   ```
   id: cc47f872-2c55-43fb-8e72-301e8bf69f88, canonical_name: 'Karine Mkrtchyan',
   created_at: 2026-05-08 23:43:42.179284+00:00, court county: 'Los Angeles'
   ```
3. **The 62 NULL rows all committed BEFORE the judge was created.** updated_at range: `23:43:32.079 → 23:43:42.161` — all before `23:43:42.179`.
4. **The 45 successfully-resolved Mkrtchyan rows all committed AFTER.** updated_at range: `23:43:42.179 → 23:43:48.838`. The first resolved row's updated_at exactly matches the judge's `created_at` (sub-second).
5. **The judge was auto-created by `resolve_judge` Step 4** (`db.py:1685-1700`) when processing the FIRST document whose `parse_document` strategy 3 (`extract_judge_name` → LA ALL-CAPS regex `_JUDGE_NAME_PATTERNS[7]` at `extract.py:927-937`) extracted the full name "KARINE MKRTCHYAN" from the boilerplate page text `*** The Judicial Officer Presiding in Department 25 is JUDGE KARINE MKRTCHYAN ***`. That page (document_id `4443de74-c6b8-58e5-bef3-b1292c4d2d01`, `ruling_id abfb96e8-f6e7-47b1-b047-cb4e37e9e4fa`, ruling_text length 1629 — pure boilerplate) is the only doc in the corpus whose `parse_document` returned the full name as the doc-level `judge_name`. All other dept-25 docs match strategy 1 (`_JUDGE_DEPT_RE`) first and produce the surname-only "Mkrtchyan".
6. **The full-name-bearing boilerplate doc happens to fall LATER in the FETCH cursor order** (`(captured_at, id) ASC`) than the 62 surname-only docs. So the 62 hit the resolver before "Karine Mkrtchyan" was a row in `derived.judges`; their `_expand_single_word_judge_surname` Step 4 SQL `LOWER(canonical_name) LIKE '% mkrtchyan'` returned 0 rows; the function returned `None`; `judge_name` stayed "Mkrtchyan"; `resolve_judge` rejected the single-word name (`db.py:1457-1463`); `judge_id` stayed NULL.
7. **A second `--no-llm` reingest after the judge was auto-created in pass 1 dropped the residual NULLs from 62 to 0.** Verified 2026-05-09: `scripts/ecs-run-task.sh scripts/reingest_from_s3.py -- --county "Los Angeles" --department-in 25 --no-llm` processed 120 docs in 15.5 s with `total_failed: 0`. Post-run distribution:
   ```json
   [{"total":120,"null_judge":0,"resolved_judge":120,"starts_with_html":0,"starts_with_plain":120}]
   [{"canonical_name":"Karine Mkrtchyan","n_rulings":107},
    {"canonical_name":"Jonathan H. Eisenman","n_rulings":7},
    {"canonical_name":"Latrice A. G. Byrdsong","n_rulings":6}]
   ```
   This is the closure of the original #4297 → #4370 → #4382 → #4386 → #4397 chain. End-to-end: 119 NULL → 62 NULL (PR #4394 landed) → 0 NULL (second reingest after in-band judge auto-create).

#### Why the four hypotheses in #4397's body were all wrong

| #4397 hypothesis | Why falsified |
|---|---|
| 1. Single-word surname with no LA alias entry — "`_LA_SURNAME_TO_FULL` only has aliases for surnames the LA county directory already knows" | There is no `_LA_SURNAME_TO_FULL` map in the codebase. The expansion is purely DB-driven via `_expand_single_word_judge_surname` (`db.py:1212`) which queries `derived.judges` directly. The surname IS resolvable once "Karine Mkrtchyan" is in `derived.judges`. |
| 2. Alternate-format pages — "longer header `*** The Judicial Officer Presiding in Department 25 is JUDGE KARINE MKRTCHYAN ***` may not match the regex" | The LA ALL-CAPS regex (`_JUDGE_NAME_PATTERNS[7]` at `extract.py:927-937`) DOES match this header — verified with the worktree's `tmp/test_la_allcaps_regex.py`. In fact, this is precisely the regex that auto-created the judge during pass 1. |
| 3. Missing judge_directory rows — "the directory snapshot for LA dept 25 hasn't been refreshed" | The latest LA snapshot (id 429, captured 2026-05-08 13:30) maps dept 25 → "Jonathan H. Eisenman". This is consistent with LA's public directory and is correct from the directory's perspective; the snapshot is not stale. The disagreement between the snapshot (Eisenman) and the actual day-of-bench rulings (Mkrtchyan) is by design — `_expand_single_word_judge_surname` Step 4 explicitly handles this case via the suffix search. |
| 4. Reingest scope — "`--no-llm` was used; LLM extraction would recover them" | Falsified by hypothesis 7's evidence: a second `--no-llm` reingest closed all 62. LLM extraction is not required. |

### Actionable follow-ups

The chronological-race class of bug is structural: **any new judge whose first per-document appearance is in surname-only docs that fall earlier in the FETCH cursor than the doc that triggers the auto-create is at risk of staying NULL on the first reingest pass.** It will recur whenever a county adds a new judge whose name matches this pattern (single-word surname header + a separate full-name boilerplate page).

Two follow-ups close the loop:

1. **`feat(reingest): two-pass resolver to remove the chronological-race class`** (priority/p2, area/ingestion). Add a pre-pass to `scripts/reingest_from_s3.py` that walks the cursor once to discover all candidate full judge names (via `parse_document` + `extract_judge_name`) without writing rulings, upserts them into `derived.judges`, then runs the existing pass to write rulings. Eliminates the ordering dependency entirely. The same problem exists for any single-word LA pattern (other surnames will hit it the next time a new dept gets a new judge), so a structural fix is preferred over per-judge mitigations.
2. **`fix(reingest): document the second-reingest-pass closure pattern in the surname-expansion docstring`** (priority/p3, area/docs). Update the docstring at `db.py:1212-1267` to note that Step 4 only resolves surnames once a judge with the matching canonical_name has been created — so a single reingest pass can leave residual NULLs if the cursor order processes surname-only docs before the full-name doc that triggers the auto-create. Until follow-up #1 lands, a second reingest pass closes the gap.

The same "second reingest" pattern works for any LA dept whose new presiding judge first appears with a single-word JUDGE/DEPT header and is later seeded by a full-name boilerplate. No surgical SQL is needed; the rebuild path (`scripts/rebuild_db.py --county "Los Angeles"`) followed by two reingest passes always converges.

