# Investigation: UNKNOWN Case Numbers in Orange and Riverside Counties

**Issue:** #780
**Date:** 2026-03-11

## Summary

Orange County has 55 UNKNOWN cases (56% of 98 total) and Riverside has 14 UNKNOWN cases (15% of 92 total). Three distinct root causes were identified, two of which are fixable by improving scraper regex patterns and PDF text extraction. The third (North Justice Center structural gap) is a permanent limitation of the source data.

## Root Causes

### Orange County

#### North Justice Center (N14, N16, N17, N18) — 25 cases — NOT FIXABLE

North JC PDFs use a columnar layout with only line numbers and case names — no formal case numbers exist in the source documents. The scraper already handles this correctly by extracting case titles and motion types. These UNKNOWN records represent a hard floor for OC field completeness.

#### Central/West Justice Center (C12, C20, C24, C26, C28, C31, W08) — 30 cases — FIXABLE

Two sub-problems:

1. **Case numbers split across lines by pdfplumber.** The columnar PDF layout causes pdfplumber to extract case numbers on separate lines from the entry header. Example from Dept C28:
   ```
   53. Morton v.     Plaintiff Robert Morton's ...
   OC Sheriff        DENIED.
   2024-             <-- case number split here
   01428785          <-- continuation on next line
   ```
   The regex `\b\d{2,4}-\d{8}\b` cannot match across line boundaries.

2. **Seven-digit case numbers in Dept C20.** Case numbers like `24-1377364` have only 7 digits after the dash. The regex requires exactly 8 (`\d{8}`).

Some departments (C26) use a three-part format like `30-2024-01420730` where the prefix `30` is a location code.

**Fix:** Pre-process text to rejoin split case numbers; relax regex to `\d{7,8}`.

### Riverside County

#### Non-CV case number prefix (Dept 2) — 3 cases — FIXABLE

The case number regex `\bCV[A-Z]{2,4}\d{6,8}\b` only matches CV-prefixed numbers. Dept 2 uses `RIC` prefix (e.g. `RIC1904113` for "FOUR STAR MIDWEST VS CITY OF JURUPA").

**Fix:** Expand regex to match RIC and other Riverside court prefixes.

#### Boilerplate PDFs ingested before filter existed (Dept 5, M205) — 2 cases

"No Tentative Rulings" placeholder PDFs were ingested before `_is_boilerplate()` was added.

**Fix:** DB cleanup migration to remove these non-ruling records.

#### Orphaned records with null S3 keys — 9 cases

Records with no associated documents, likely artifacts from reingest (#707).

**Fix:** Investigate and clean up via DB migration.

## Updated Baselines

| County | Total | Current UNKNOWN | Fixable | Hard Floor |
|--------|-------|----------------|---------|------------|
| Orange | 98 | 55 (56%) | 30 | 25 (25.5%) |
| Riverside | 92 | 14 (15%) | 5-14 | 0% (after cleanup) |

## Departments Affected

### Orange County UNKNOWN by department
| Department | Count | Root Cause |
|-----------|-------|-----------|
| N14 | 6 | Structural gap (no case numbers in North JC PDFs) |
| N16 | 7 | Structural gap |
| N17 | 6 | Structural gap |
| N18 | 6 | Structural gap |
| C20 | 6 | 7-digit case numbers not matched by regex |
| C26 | 6 | Case numbers split across lines |
| C28 | 6 | Case numbers split across lines |
| C31 | 6 | Case numbers split across lines |
| C24 | 2 | Case numbers split across lines |
| C12 | 1 | Case numbers split across lines |
| W08 | 1 | Case numbers split across lines |
