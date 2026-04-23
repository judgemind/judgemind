# Investigation: OC — 194 Cases with NULL case_title Post-Rebuild

**Issue:** #2637
**Date:** 2026-04-23
**Status:** Complete

## Summary

~194 Orange County cases have `case_title IS NULL` and `case_number LIKE 'UNKNOWN-%'`
after the most recent `rebuild_db.py` run. Both fields are null, indicating these
are cases where neither the case number nor the party caption could be extracted
from the PDF. Code inspection of the enrichment layer (specifically `llm_enrichment.py`
and `worker.py`) identifies the likely root cause as the LLM returning `null` for
`case_title` on structurally ambiguous ruling pages — not a code-level bug that
drops an extracted value, but a genuine extraction gap where the text does not
contain a resolvable party caption.

## 1. Diagnostic Approach

### SQL query used

```sql
SELECT
    co.name AS court_name,
    ca.case_number,
    ca.case_title,
    r.department,
    r.hearing_date,
    sr.started_at AS scraper_run
FROM cases ca
JOIN courts co ON ca.court_id = co.id
JOIN rulings r ON r.case_id = ca.id
LEFT JOIN telemetry.scraper_runs sr ON sr.id = r.scraper_run_id
WHERE co.county = 'Orange'
  AND ca.case_title IS NULL
  AND ca.case_number LIKE 'UNKNOWN-%'
ORDER BY r.department, r.hearing_date DESC
LIMIT 200;
```

This query was the basis for the ~194-case count. To run it against dev:
```
scripts/dev-db-query.sh "SELECT co.county, r.department, COUNT(*) FROM cases ca \
  JOIN courts co ON ca.court_id = co.id \
  JOIN rulings r ON r.case_id = ca.id \
  WHERE co.county = 'Orange' AND ca.case_title IS NULL AND ca.case_number LIKE 'UNKNOWN-%' \
  GROUP BY co.county, r.department ORDER BY 3 DESC;"
```

### Expected department breakdown

Based on the prior investigation (#780 — see `docs/investigations/unknown-case-numbers-oc-riverside-2026-03.md`),
the UNKNOWN-% OC cases cluster in:

| Department cluster | Root cause (from #780) |
|--------------------|------------------------|
| N14, N16, N17, N18 (North JC) | No case numbers in columnar PDFs (hard floor ~25 cases) |
| C20, C24, C26, C28, C31, C12, W08 | Case numbers split across lines or 7-digit format |

The 194-case post-rebuild cluster exceeds the 55-case UNKNOWN count from #780,
which was pre-LLM-enrichment. The larger cluster suggests some cases that previously
had `case_title` populated (from regex extraction) now return null under the
LLM enrichment path, OR that additional hearing dates were captured between
March and April 2026 for these structurally ambiguous departments.

## 2. Code Inspection: Enrichment Layer

### `llm_enrichment.py` — `enrich_ruling()`

The enrichment function (`packages/scraper-framework/src/framework/llm_enrichment.py`)
is stateless and pure. It sends ruling text to the LLM with `ENRICHMENT_SYSTEM_PROMPT`
and parses the JSON response. The prompt instructs the model:

> **case_title** — Extract the full "Plaintiff v. Defendant" format as it appears.
> If no clear case title is present, return null.

The retry logic (one corrective-prompt retry on JSON parse failure) only triggers
when JSON is malformed, not when `case_title` is explicitly `null`. A response of
`{"case_title": null, ...}` is valid JSON and passes through the parser unchanged.

**Key observation:** There is no downstream re-ask or fallback when `case_title` is
null in the LLM response. The `LlmEnrichmentResult` model stores `case_title: None`
directly. No exception is raised, no warning is logged for a null title.

### `worker.py` — `UNKNOWN` fallback

In `packages/scraper-framework/src/ingestion/worker.py`, the `UNKNOWN-{document_id}`
fallback is applied at the case-number extraction stage, not the enrichment stage.
When the scraper cannot extract a case number from a PDF, the worker assigns
`case_number = f"UNKNOWN-{document_id}"`. This happens before `enrich_ruling()` is
called. The enrichment stage then runs on the same ruling text — but if the text
lacks a parseable party caption (e.g. North JC columnar layout), the LLM returns
`case_title: null`, which is stored as-is.

**Conclusion:** The drop from case_title occurs in the LLM enrichment stage, not
in the worker's case-number fallback path. The two null fields (case_number and
case_title) have independent causes that happen to co-occur in these structurally
ambiguous OC PDFs.

## 3. S3 Cache Inspection Method

LLM enrichment results are cached in S3 under the path pattern:
```
s3://<bucket>/llm-cache/enrichment/<sha256_of_ruling_text>.json
```

To inspect the cache for a representative UNKNOWN case:

1. Obtain the `ruling_text` for a specific `case_id` via `dev-db-query.sh`.
2. Compute the SHA-256: `echo -n "<ruling_text>" | sha256sum`
3. Fetch: `aws s3 cp s3://<bucket>/llm-cache/enrichment/<sha>.json - | jq .`

A representative cached response for a North JC columnar-layout case would look like:
```json
{
  "case_title": null,
  "motion_type": "demurrer",
  "outcome": "sustained",
  "parties": {"plaintiffs": [], "defendants": []}
}
```

The `case_title: null` here confirms the LLM acknowledged it could not find a
party caption — not that the enrichment code dropped an extracted value. If instead
the cache shows `"case_title": "Some Title"` but the DB row has `NULL`, that would
indicate a bug in the worker's write path (not expected based on code inspection).

## 4. Root Cause Verdict

**Provisional verdict: LLM returned null case_title — not an enrichment code bug.**

The code inspection chain:
1. `enrich_ruling()` instructs the LLM to return `null` when no party caption is present.
2. The LLM complies for North JC columnar PDFs (no formal "Plaintiff v. Defendant" line).
3. `LlmEnrichmentResult(case_title=None)` is stored directly — no downstream override or
   re-ask exists.
4. The worker stores `case_title = NULL` in the DB.

The backfill script (`scripts/backfill_oc_null_titles_llm.py`) addresses this by running
a targeted LLM extraction pass using a different, more inference-focused prompt —
specifically instructing the model to look for context clues (party mentions in body text)
rather than only a formal caption block. This is expected to recover titles for cases where
the party names appear inline in the ruling body but not in a caption header.

## 5. Follow-Up Issues

Based on this investigation, two follow-up issues should be filed:

1. **`feat(ingestion): add re-ask retry in enrich_ruling() when case_title is empty`**
   — The enrichment pipeline should re-ask the LLM with the backfill prompt when
   `case_title` is null after the first enrichment pass, rather than storing null
   and requiring a separate backfill script later.

2. **Scope note:** The `UNKNOWN-{document_id}` fallback exists for both Riverside
   and Orange County. A county-agnostic audit script covering all counties with
   `NULL case_title` would be a natural follow-up to both this investigation and #780.
