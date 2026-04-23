# Investigation: case_type gaps in Orange (80%) and Riverside (54%)

**Issue:** #577
**Date:** 2026-03-11
**Status:** Complete

## Summary

Investigated case_type completeness gaps in Orange County (80% complete, 20 missing) and Riverside County (64%, 32 missing). All other counties are at 99-100%.

**Root cause:** The LLM (Claude Haiku) inconsistently extracts `case_type` for PDF documents. The pipeline is working correctly — other LLM-extracted fields (outcome, motion_type, department) are populated — but the LLM sometimes returns null for `case_type` despite clear contextual signals (e.g., "CV" prefix in case numbers indicating civil cases).

There is no regex fallback for `case_type` in the ingestion pipeline. Unlike other fields (judge_name, outcome, motion_type, case_number, case_title, hearing_date), `case_type` has no deterministic fallback when the LLM returns null. This makes `case_type` uniquely vulnerable to LLM non-determinism.

## Data Breakdown

### Orange County (98 total cases, 20 missing case_type)

| Category | Count | Notes |
|----------|-------|-------|
| UNKNOWN-* cases, missing case_type | 18 | Multi-ruling PDFs where case number extraction failed |
| Valid case number, missing case_type | 2 | Same case (2024-01380242) appearing in 2 documents; LLM extracted other fields but returned null for case_type |
| UNKNOWN-* cases, HAS case_type | 37 | LLM successfully inferred case_type even without a case number |
| Valid case number, HAS case_type | 41 | Working correctly |

### Riverside County (90 total cases, 32 missing case_type)

| Category | Count | Notes |
|----------|-------|-------|
| UNKNOWN-* cases, missing case_type | 11 | Similar pattern to OC |
| Valid case number, missing case_type | 21 | All CV-prefix (clearly civil). Created 2026-03-09/10 by live scraper. LLM running (outcome/motion_type populated) but returning null for case_type |
| UNKNOWN-* cases, HAS case_type | 3 | LLM successfully inferred |
| Valid case number, HAS case_type | 55 | Working correctly |

### Current Completeness (at time of investigation)

| County | Total Cases | Has case_type | Missing | % Complete |
|--------|------------|---------------|---------|-----------|
| Riverside | 90 | 58 | 32 | 64.4% |
| Orange | 98 | 78 | 20 | 79.6% |
| Los Angeles | 529 | 528 | 1 | 99.8% |
| San Bernardino | 70 | 70 | 0 | 100.0% |
| San Francisco | 21 | 21 | 0 | 100.0% |
| Santa Clara | 2 | 2 | 0 | 100.0% |

Note: The issue originally reported OC at 80% (85 cases) and Riverside at 77.3% (44 cases). The database has grown since then and Riverside's gap has widened due to new cases from the live scraper being ingested without case_type.

## Key Findings

### 1. LLM extraction is inconsistent for case_type

The LLM successfully extracts outcome, motion_type, and department from the same documents where it fails to extract case_type. This is not a systemic failure (documents are being processed by the LLM), but rather a field-specific inconsistency.

For Riverside, all 21 missing cases have "CV" prefixes in their case numbers (`CVME`, `CVRI`, `CVSW`, `CVCO`). The LLM prompt includes the hint: "CV, CIV, STCV indicate civil." However, the LLM sometimes ignores this hint and returns null.

### 2. No regex fallback exists for case_type

The ingestion pipeline has regex fallback extractors for every field except `case_type`:

| Field | LLM? | Regex fallback? |
|-------|------|-----------------|
| judge_name | Yes | Yes |
| outcome | Yes | Yes |
| motion_type | Yes | Yes |
| case_number | Yes | Yes |
| case_title | Yes | Yes |
| hearing_date | Yes | Yes |
| case_type | Yes | **No** |
| parties | Yes | Yes (from caption) |

This gap means case_type is entirely dependent on the LLM returning a non-null value.

### 3. UNKNOWN-* case numbers correlate with missing case_type

Across both counties, cases with synthetic `UNKNOWN-*` case numbers are disproportionately missing case_type. These are from multi-ruling PDFs where the case number extraction failed entirely. Without a case number, the LLM has even less context for classifying the case type.

### 4. Riverside gap is growing

The Riverside gap is actively widening as new cases are ingested daily by the live scraper without case_type. Without a fix, the gap will continue to grow.

## Recommendations

### Fix 1: Add regex-based case_type fallback (highest impact)

Add a deterministic fallback in the ingestion worker that infers case_type from the case number prefix when the LLM returns null. California case numbers use standardized prefixes:

- `CV*` (CVRI, CVME, CVPS, CVSW, CVCO, etc.) -> `civil`
- `FL*`, `DV*` -> `family`
- `PR*`, `BP*` -> `probate`
- `SC*` -> `small_claims`
- `CR*`, `F*` (felony) -> `criminal`
- `JV*` -> `juvenile`
- `TR*` -> `traffic`
- OC format `YYYY-*` -> `civil` (OC tentative rulings are all civil)

This would close 100% of the Riverside valid-case-number gap and the 2 OC valid-case-number gaps immediately, both retroactively (via reingest) and for future ingestion.

### Fix 2: Targeted reingest for remaining gaps

After deploying the regex fallback, run a targeted reingest for Orange and Riverside to fill in the remaining nulls. This would address both the valid-case-number and UNKNOWN-* gaps (the UNKNOWN cases may get case_type from the regex fallback if the LLM or regex manages to extract a case number on the second pass).

### Fix 3: Improve LLM prompt for case_type

The current prompt's inference hints could be strengthened. Instead of just listing hints, add an explicit instruction like: "If the case number starts with 'CV', the case type is always 'civil'. Case number prefixes are authoritative for case type classification."

## Decisions Needed

None — all recommended fixes are implementable without human input.
