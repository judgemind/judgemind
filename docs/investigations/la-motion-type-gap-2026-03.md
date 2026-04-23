# Investigation: LA motion_type Extraction Gap (#420)

**Date:** 2026-03-09
**Issue:** #420
**Status:** Complete

## Summary

88 of 428 LA County rulings (20.6%) have NULL `motion_type`. The root causes are:

1. **Department header boilerplate ingested as rulings (25 docs, 28%)** — Pages like "DEPARTMENT 15 LAW AND MOTION RULINGS" contain no actual ruling text but are stored as ruling rows. These should be filtered out during ingestion.

2. **Missing motion type patterns in `extract.py` (47 docs, 53%)** — The centralized `extract_motion_type()` function lacks patterns for many common LA motion types. Breakdown by type:
   - Default judgment: 9
   - Motion to be relieved as counsel: 7
   - Motion for attorney fees (curly apostrophe bug): 5
   - Motion for leave to amend: 5
   - Motion to compel (plural "motions" bug): 4
   - Class action settlement / preliminary approval: 4
   - Motion for relief from waiver: 3
   - Motion for sanctions: 2
   - Ex parte (without "application"/"motion" suffix): 2
   - Various (1 each): pro hac vice, substitute, MILs abbreviation, tax costs, writ of possession, new trial

3. **Genuinely unclassifiable proceedings (16 docs, 18%)** — Case management conferences, venue transfer motions, tentative decisions without clear motion types, and discovery motions described in non-standard ways. Some of these could be captured with broader patterns; others genuinely lack a standard motion type.

## Root Cause Details

### Bug 1: Curly Apostrophe in Attorney Fees Pattern

The existing pattern in `extract.py` line 117:
```python
re.compile(r"\bmotion\s+for\s+attorney.?s?\s+fees\b", re.IGNORECASE)
```

This fails on "Attorneys\u2019 Fees" (with Unicode right single quote U+2019). The `.?` quantifier matches the trailing "s" of "attorneys", leaving the curly quote `\u2019` unmatched before `\s+`. The fix is to match the possessive marker more broadly:
```python
re.compile(r"\bmotion\s+for\s+attorneys?['\u2018\u2019]?\s*fees\b", re.IGNORECASE)
```

This also affects 5 docs where the standalone text (not in a "Motion for..." header) says "attorneys\u2019 fees" in the ruling body.

### Bug 2: Singular-Only "motion to compel"

The pattern `\bmotion\s+to\s+compel\b` does not match "motions to compel" (plural). LA rulings frequently use "Motions to Compel Discovery" when multiple discovery motions are heard together. Fix: change to `\bmotions?\s+to\s+compel\b`.

### Missing Pattern: Department Header Filtering

The LA scraper's `_split_cases_html()` splits on `<HR>` + `Case Number:` boundaries. Department header pages that contain no `Case Number:` should be filtered out entirely. Currently they pass through as a single-element list from the fallback path in `_split_cases_html()`.

## Affected Files

- `packages/scraper-framework/src/ingestion/extract.py` — Add new patterns, fix existing bugs
- `packages/scraper-framework/src/courts/ca/la_tentatives.py` — Filter department header boilerplate
- `packages/scraper-framework/tests/test_extract.py` — Add regression tests for new patterns

## Follow-Up Issues

1. **Fix attorney fees curly apostrophe bug and add missing motion type patterns** — Implementation task
2. **Filter LA department header boilerplate from ingestion** — Implementation task
3. **Backfill motion_type for existing LA rulings after pattern fixes** — Migration task
