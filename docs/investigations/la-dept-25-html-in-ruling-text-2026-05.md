# LA dept-25 "raw HTML in ruling_text" reingest failure — 2026-05

**Issue:** #4382
**Status:** Root-cause identified; hypothesis in original issue body falsified.
**Worktree:** `agent-ad2cacb6c1bcfd80c`
**Date:** 2026-05-08

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
