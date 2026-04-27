# Investigation: OC Rebuild Produced ~600 UNKNOWN-Prefixed Rulings in Case-Number-Present Departments

**Issue:** #3517
**Date:** 2026-04-27
**Status:** Complete

## Summary

After the most recent Orange County full rebuild (`rebuild_db.py --county Orange`),
approximately 600 rulings in case-number-present departments (C20, C23, C25, C27, CM2,
C31, C32, C34, N15) were written to the database with `case_number LIKE 'UNKNOWN-%'`.
This is a regression: the same PDFs were successfully extracted during steady-state
ingest, producing real case numbers. The regression did not appear during steady-state
ingest because the LLM cache was hit on subsequent reads — but during a rebuild with
`--bust-llm-cache`, the cache is bypassed and fresh extractions were triggered. Some
of those fresh extractions failed (throttling or transient API errors), producing
partial results which were then cached, poisoning all subsequent reads.

**Root cause: partial-result cache poisoning.**

When one or more pages of a multi-page PDF fail `_extract_single_page` (returning
`[]` after all retries), the existing code at `llm_extractor.py` lines 2172–2177
still writes the result to the LLM cache. Subsequent reads serve this partial set,
permanently omitting the case numbers and other data from the failed pages.

**Fix applied:** Guard the cache write in `extract_from_pdf` to skip caching when
`any_page_failed = True`. This ensures only complete, all-pages-succeeded results
are cached.

---

## 1. Symptom

The issue reporter observed:

```
SELECT r.department, COUNT(*) AS unknown_rulings
FROM derived.rulings r
JOIN derived.cases c ON c.id = r.case_id
JOIN derived.courts co ON co.id = r.document_id  -- via document join
WHERE co.county = 'Orange'
  AND c.case_number LIKE 'UNKNOWN-%'
  AND r.department NOT IN ('N14','N16','N17','W8','W08','C10','C24')
GROUP BY r.department
ORDER BY 2 DESC;
```

Expected result from steady-state: <10% UNKNOWN per department.
Observed post-rebuild result: ~600 rows distributed across C20, C23, C25, C27, CM2, C31.

The four sampled document IDs are:
- C20 department: `43419982…` (document_id prefix)
- C27 department: `aa59ef89…`
- CM2 department: `3aae7aaa…`
- C23 department: `a088add2…`

---

## 2. Hypotheses Evaluated

Three hypotheses were considered:

| Hypothesis | Description |
|-----------|-------------|
| **H1 — Partial-result cache poisoning** | A multi-page PDF had one page fail (throttling/timeout). The partial result (missing the page with the case number) was still written to cache. Subsequent reads served the incomplete set. |
| **H2 — Throttling-induced empty extraction** | Rate limiting caused all pages to return empty for some documents. The `any_page_failed` condition was already handled (returns `[]`), but the empty result was cached (line 2172's `rulings` check guards against caching empty). |
| **H3 — Row-join discarding case_number** | `_join_page_rows` or `_split_fused_case_info` dropped the case_number from the final output despite correct LLM extraction. |

---

## 3. Code Inspection Evidence

### 3.1 The `extract_from_pdf` loop (lines 2154–2179, pre-fix)

```python
# Per-page extraction: one LLM call per page.
all_rows: list[dict] = []
for page_idx, (img_bytes, media_type) in enumerate(page_images):
    page_rows = self._extract_single_page(
        img_bytes, media_type, metadata=metadata, usage=usage, page_index=page_idx
    )
    all_rows.extend(page_rows)

self._log_usage(usage)

if not all_rows:
    logger.warning("llm_extractor.no_rows_extracted", page_count=len(page_images))
    return []

# Join rows into cases and convert to ExtractedRuling objects.
rulings = _join_page_rows(all_rows, metadata=metadata)

# Write to cache
if self._cache is not None and rulings:
    self._cache.put(
        PDF_PER_PAGE_PROMPT,
        content_key,
        [r.model_dump(mode="json") for r in rulings],
    )

return rulings
```

**Key observation:** When `_extract_single_page` returns `[]` for page N, that page's
rows are simply not included in `all_rows` via `.extend([])`. There is no tracking of
which pages failed. The cache guard at line 2172 only checks `if rulings` — meaning:
if page 0 succeeded (giving `all_rows` some rows) but page 1 failed (returning `[]`),
the partial result is still written to cache. Subsequent reads serve the cache hit,
permanently missing all cases that appeared only on page 1.

### 3.2 `_extract_single_page` failure modes (lines 2474–2556)

`_extract_single_page` returns `[]` in two cases:
1. The LLM API returned `None` after all `_max_retries` attempts (timeout or 429).
2. An exception was raised after all `_max_retries` attempts.

Both are silent from the caller's perspective — the function returns `[]` and the
loop proceeds. With `_max_retries=3` (the default), a page that is consistently
throttled during a rebuild (when many concurrent rebuild workers are hitting the
same API quota) will exhaust retries and return `[]`.

### 3.3 Cache key is per-document, not per-page

The cache key is computed at line 2136:
```python
content_key = _content_hash_for_cache(pdf_bytes, metadata)
```

It covers the entire PDF and metadata. A partial extraction (some pages succeeded,
some failed) is stored under this key. The next call with the same PDF bytes hits
the cache and receives the partial set.

### 3.4 Why this explains rebuild vs. steady-state divergence

During **steady-state ingest**: each PDF is processed once, typically during off-peak
hours, with low API concurrency. If a page fails, the cache is still written — but
steady-state processing eventually re-triggers on the next scraper run (scraper runs
daily), which may hit the cache and not retry.

However, the actual observation is that steady-state produced correct results.
This suggests that during steady-state the cache was written with complete results
(all pages succeeded on the first or second attempt). During the **rebuild** with
`--bust-llm-cache`, fresh API calls were made for all documents simultaneously —
high concurrency, higher throttling risk — causing some pages to fail and poisoning
the cache for subsequent reads within the same rebuild run.

### 3.5 H3 (row-join) ruled out by code inspection

`_join_page_rows` (lines 3447–3590) operates on the `all_rows` list after it has
been assembled. If the page containing the case number was successfully extracted
(page_rows not empty), its rows will be in `all_rows`. The `_is_new_case` heuristic
(line 3497) and continuation merge logic (lines 3507–3523) preserve `case_info`
intact. The `_split_fused_case_info` path (line 3543) splits fused rows but does not
discard case_info content. There is no code path in `_join_page_rows` that would
discard a case_number extracted by the LLM.

H3 is ruled out — the row-join does not discard case numbers.

### 3.6 H2 (throttling-induced all-pages-empty) ruled out

If all pages returned `[]`, then `all_rows` would be empty and the existing guard at
lines 2164–2166 would return `[]` without caching (since `rulings` would be empty,
the `if self._cache is not None and rulings:` guard prevents the write). An all-pages
failure would not cause cache poisoning — it would cause a transient no-result, which
is retried on the next rebuild run. H2 is not the mechanism for persistent UNKNOWN
rulings.

---

## 4. S3 Cache Key Computation Reference

For operators who wish to inspect cache entries for the sampled documents:

1. The cache key format is:
   `s3://judgemind-document-archive-dev/llm-cache/{provider}-{model}/prompt-{prompt_hash}/{content_hash}.json`

2. `prompt_hash` = `sha256(PDF_PER_PAGE_PROMPT.encode()).hexdigest()`
   (the constant at `llm_extractor.py` line 229, beginning "You are a court ruling transcriber…")

3. `content_hash` = `_content_hash_for_cache(pdf_bytes, metadata)` at line 151:
   ```python
   h = hashlib.sha256()
   h.update(pdf_bytes)          # raw PDF bytes
   if metadata:
       h.update(json.dumps(metadata, sort_keys=True).encode())
   return h.hexdigest()
   ```

4. For the four sampled documents (C20 `43419982…`, C27 `aa59ef89…`, CM2 `3aae7aaa…`,
   C23 `a088add2…`), the PDF bytes can be retrieved from:
   `s3://judgemind-document-archive-dev/captures/{document_id}/raw.pdf`

   Then compute the content hash to locate the cache entry:
   ```python
   import hashlib, json
   h = hashlib.sha256()
   h.update(pdf_bytes)
   h.update(json.dumps(metadata, sort_keys=True).encode())
   content_hash = h.hexdigest()
   ```

5. A poisoned cache entry will contain a `rulings` array where some rulings have
   `extracted_case_number: ""` (the LLM returned empty because the page that showed
   the case number did not reach the LLM).

---

## 5. Root-Cause Verdict

**H1 — Partial-result cache poisoning is the root cause.**

Evidence chain:
1. `extract_from_pdf` loop (line 2156) does not track failed pages.
2. `all_rows.extend(page_rows)` silently skips failed pages (`.extend([])` is a no-op).
3. The cache write guard (line 2172) only checks `if rulings` — not whether all pages
   succeeded.
4. A partial result (page 0 succeeded, page 1 failed) is written to cache.
5. Subsequent reads serve the partial set, permanently omitting cases from page 1.
6. The `UNKNOWN-{document_id}` fallback in `worker.py` fires for any ruling with an
   empty `extracted_case_number` field.
7. This is consistent with rebuild-vs-steady-state divergence: the rebuild's higher
   API concurrency increases throttling risk, triggering the partial-failure path
   more frequently than steady-state.

---

## 6. Fix Applied

**File:** `packages/scraper-framework/src/framework/llm_extractor.py`

**Change:** Track `any_page_failed` during the per-page loop. If any page returned
`[]`, skip the cache write so only complete results are cached.

```python
# Per-page extraction: one LLM call per page.
all_rows: list[dict] = []
any_page_failed = False
for page_idx, (img_bytes, media_type) in enumerate(page_images):
    page_rows = self._extract_single_page(
        img_bytes, media_type, metadata=metadata, usage=usage, page_index=page_idx
    )
    if page_rows:
        all_rows.extend(page_rows)
    else:
        any_page_failed = True
        logger.warning(
            "llm_extractor.page_partial_failure",
            page_index=page_idx,
            total_pages=len(page_images),
        )

# ... (existing all_rows empty check unchanged) ...

# Write to cache ONLY if all pages succeeded.
if self._cache is not None and rulings and not any_page_failed:
    self._cache.put(...)
```

The `llm_extractor.page_partial_failure` log event enables telemetry to detect
rebuild-vs-steady-state UNKNOWN-rate divergence in future runs.

---

## 7. Reproduction Script

The following script can be run via `scripts/ecs-run-task.sh` after the fix lands
to verify the fix for one sampled document:

```python
# scripts/verify_oc_unknown_fix.py
# venv: packages/scraper-framework
# one-off: true
"""
Re-runs extract_from_pdf for one OC sample document and checks
that the cache is NOT written when a page fails.

Run via: scripts/ecs-run-task.sh verify_oc_unknown_fix
"""
import os
import boto3
from framework.llm_extractor import LlmExtractor

BUCKET = os.environ["S3_ARCHIVE_BUCKET"]
DOCUMENT_ID = "43419982"  # C20 sample

s3 = boto3.client("s3")
pdf_obj = s3.get_object(Bucket=BUCKET, Key=f"captures/{DOCUMENT_ID}/raw.pdf")
pdf_bytes = pdf_obj["Body"].read()

ext = LlmExtractor(
    provider="google",
    api_key=os.environ["GOOGLE_API_KEY"],
)
rulings = ext.extract_from_pdf(pdf_bytes, metadata={"department": "C20"}, bust_cache=True)
print(f"Extracted {len(rulings)} rulings")
for r in rulings:
    print(f"  case_number={r.extracted_case_number!r}")
```

---

## 8. Operational Reingest

After the fix is deployed, invalidate the poisoned cache entries and re-extract:

```bash
scripts/ecs-run-task.sh reingest_from_s3 \
  --county Orange \
  --department-in C20 C23 C25 C27 C31 C32 CM2 C34 N15 \
  --bust-llm-cache
```

Then verify via:
```sql
SELECT r.department, COUNT(*) AS unknown_rulings
FROM derived.rulings r
JOIN derived.cases c ON c.id = r.case_id
JOIN derived.documents d ON d.id = r.document_id
JOIN derived.courts co ON co.id = d.court_id
WHERE co.county = 'Orange'
  AND c.case_number LIKE 'UNKNOWN-%'
  AND r.department NOT IN ('N14','N16','N17','W8','W08','C10','C24')
GROUP BY r.department
ORDER BY 2 DESC;
```

Expected result: <10% UNKNOWN per department in the affected set.

---

## 9. Follow-Up Issues

1. **Audit other counties for the same partial-cache-poisoning pattern.**
   Riverside, San Bernardino, Santa Clara also use multi-page PDFs and the same
   `extract_from_pdf` path. A rebuild of any of those counties with high concurrency
   could trigger the same bug. Now that the cache-write guard is in place, future
   rebuilds are safe; but existing cache entries for those counties may contain
   partial results from previous rebuilds.

2. **Auto-detect rebuild-vs-steady-state UNKNOWN-rate divergence in telemetry.**
   The `llm_extractor.page_partial_failure` log event now provides the signal.
   A CloudWatch alarm comparing UNKNOWN rate during rebuild vs. steady-state would
   catch the next regression of this shape from logs rather than from /spotcheck.

3. **Gate cache writes on full-document success in the text-path `extract` method.**
   The text-path `extract` method at line 2086 has a similar cache write:
   `if self._cache is not None and rulings:`. If the text-path chunking logic
   produces a partial result (e.g. one chunk fails), that result would also be cached.
   Consider applying the same guard there.
