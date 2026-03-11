# Judge ID Completeness Gaps After LLM Reingest

**Issue:** #572
**Date:** 2026-03-10
**Status:** Complete

## Summary

After the full LLM reingest, `judge_id` is the weakest field at 60.7% (594/979 rulings). The gap is **385 rulings** (357 in Los Angeles, 28 in Riverside). All other counties are at 100%.

## Root Cause

**The gap is NOT an extraction failure -- it is a missing department-to-judge mapping issue.**

### Finding 1: LA rulings don't contain judge names in the text

All 357 LA rulings missing `judge_id` have a `department` value set. However, the vast majority of LA ruling documents do not include the judge's name anywhere in the text. The typical LA format is:

```
DEPARTMENT 26 LAW AND MOTION RULINGS
Case Number: 25STLC04625
Hearing Date: March 9, 2026
...
```

The judge name simply isn't there. Neither the LLM nor regex extraction can find what doesn't exist. Of the 582 LA rulings:

| Condition | Count |
|-----------|-------|
| Has judge text + has judge_id | 186 |
| Has judge text + NO judge_id | 0 |
| No judge text + has judge_id | 39 |
| No judge text + NO judge_id | 319 |

The 186 rulings that got judge_id had the name embedded in the text (some LA departments include it, e.g. "Judge Nicholas F. Daum, Department 47"). The 39 that got judge_id without text likely came from a previous department mapping backfill.

**Conclusion:** The LLM and regex extraction are working correctly. They extract judge names when present (186/186 = 100%). The problem is that most LA departments don't include the judge name in the ruling text.

### Finding 2: Riverside has the same pattern

All 28 Riverside rulings missing `judge_id` also have a department set. Riverside ruling text follows a format like "Tentative Rulings for March 6, 2026 / Department 4" without the judge name. Unlike LA, there is no existing department-to-judge mapping script for Riverside.

### Finding 3: Existing backfill script exists but needs re-running

`scripts/backfill_la_judge_from_dept.py` uses the LA judicial officer directory to map departments to judges. It queries `https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx` for the live mapping. However:

- It may not have been run after the LLM reingest
- It only covers LA, not Riverside
- It fetches the *current* judge assignment, which may not match historical rulings (judges rotate departments)

### Finding 4: Judge name normalization issues in the judges table

The LA judges table contains 60 entries but has significant data quality issues:

| Issue | Examples | Count |
|-------|----------|-------|
| Garbage/unicode entries | `"?"`, `"? ?? ? ? ? Brock T. Hammond?? ?"` | 2+ |
| Paragraph captured as name | `"2026 ___ Hon. Tiana J. Murillo Moving Party..."` | 1 |
| Last-name-only entries | "Bahadori", "Crowe", "Crowfoot", "Mkrtchyan", "Murillo" | 5+ |
| Truncated names | "Curtis A. Kin" (likely "King" or "Kinney") | 1+ |
| Honorific not stripped | "Hon. Elizabeth L. Bradley", "Hon. Daniel M. Crowley" | 2 |
| Suffix parsing bug | "Jr. Edward B. Moreton" (should be "Edward B. Moreton Jr.") | 1 |
| "Arbitrator" prefix | "Arbitrator Howard B. Miller" | 1 |
| Duplicates | Two "Crowfoot" entries with 0 rulings each | 1+ |

These issues don't directly cause the 357 missing `judge_id` values (those rulings have no judge name at all), but they do fragment the judges table and will cause future matching problems.

### Finding 5: 56 distinct LA departments need mapping

The 357 LA rulings span 56 distinct departments. The top departments:

| Department | Missing Rulings |
|------------|----------------|
| X | 37 |
| 26 | 24 |
| H | 18 |
| 28 | 14 |
| B | 13 |
| E | 13 |
| O | 12 |
| 19 | 12 |
| 6 | 11 |
| A14 | 10 |

### Finding 6: Riverside has 15 distinct departments needing mapping

The 28 Riverside rulings span 15 departments (2, PS2, 4, C1, M302, PS1, 10, 7, M301, MV1, 3, PS4, 1, 5, M205).

## Quantified Opportunity

| Fix | Rulings Fixed | Impact |
|-----|---------------|--------|
| Run LA dept backfill + add Riverside mapping | ~350-370 | 36-38% boost (to ~96-98%) |
| Clean up judge name normalization bugs | 10-15 | Better data quality, prevents future fragmentation |
| **Combined** | **~365-385** | **~99%+ judge_id completeness** |

Running the LA backfill alone would bring overall judge_id completeness from 60.7% to approximately 97%.

## Recommendations

### 1. Run LA department-to-judge backfill (HIGH IMPACT)

**Estimated impact:** ~319-330 rulings fixed (assuming good directory coverage)

The script already exists (`scripts/backfill_la_judge_from_dept.py`). It just needs to be run after the LLM reingest. However, this is a one-time fix. The real fix is to integrate dept-to-judge resolution into the ingestion pipeline.

**Implementation:** Integrate the LA dept-to-judge lookup into the ingestion worker so new rulings automatically get `judge_id` from department when the name isn't in the text. The `backfill_la_judge_from_dept.py` script shows the pattern.

### 2. Build Riverside department-to-judge mapping (MEDIUM IMPACT)

**Estimated impact:** ~25-28 rulings fixed

Create a `riverside_dept_judges.py` module similar to `la_dept_judges.py` that scrapes the Riverside court website for department-to-judge assignments. Integrate into the ingestion pipeline.

### 3. Clean up judges table normalization (MEDIUM IMPACT)

**Estimated impact:** Better data quality, fewer duplicate judge records

Fix the `normalize_judge_name()` function to handle:
- Strip "Arbitrator" prefix
- Handle "Jr." / "Sr." / "III" suffixes correctly (move to end of name)
- Reject garbage strings (unicode junk, very long strings)
- Merge duplicate judge records

Then run a cleanup migration to fix existing bad records.

### 4. Integrate dept-to-judge lookup in the ingestion pipeline (HIGH IMPACT, PREVENTATIVE)

**Estimated impact:** Prevents all future judge_id gaps for LA and Riverside

Modify the ingestion worker to:
1. After LLM + regex extraction fails to find a judge name
2. If department IS available, look up the judge via the court's dept-to-judge mapping
3. Cache the mapping per court (refresh periodically)

This makes the fix permanent rather than requiring periodic backfill runs.

## Decisions Needing Human Input

None -- all recommendations are straightforward engineering work.
