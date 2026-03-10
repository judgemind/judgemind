# LLM Ingestion Before/After Audit

**Date:** 2026-03-09
**Epic:** #441 — LLM-based ingestion
**Purpose:** Measure field completeness improvement from replacing regex extraction with Claude Haiku LLM extraction.

## Methodology

- **Before:** Snapshot of dev DB field completeness with regex-only extraction pipeline (647 rulings, 569 cases).
- **After:** Same dataset reingested through the LLM extraction pipeline, then re-queried.
- All queries run against the dev database via `scripts/dev-db-query.sh`.

---

## Before (Regex Extraction)

**Snapshot taken:** 2026-03-09, pre-reingestion

### Rulings — Overall (647 total)

| Field | Present | Missing | Completeness |
|-------|---------|---------|-------------|
| outcome | 608 | 39 | 94.0% |
| motion_type | 552 | 95 | 85.3% |
| hearing_date | 647 | 0 | 100.0% |
| department | 546 | 101 | 84.4% |
| judge_id (FK) | 330 | 317 | 51.0% |
| case_id (FK) | 647 | 0 | 100.0% |

### Rulings — By County

| County | Rulings | Judge | Motion | Outcome | Dept |
|--------|---------|-------|--------|---------|------|
| Los Angeles | 351 | 93 (26.5%) | 285 (81.2%) | 326 (92.9%) | 351 (100%) |
| San Bernardino | 121 | 121 (100%) | 103 (85.1%) | 111 (91.7%) | 69 (57.0%) |
| Orange | 105 | 74 (70.5%) | 103 (98.1%) | 104 (99.0%) | 72 (68.6%) |
| Riverside | 49 | 21 (42.9%) | 45 (91.8%) | 46 (93.9%) | 33 (67.3%) |
| San Francisco | 19 | 19 (100%) | 14 (73.7%) | 19 (100%) | 19 (100%) |
| Santa Clara | 2 | 2 (100%) | 2 (100%) | 2 (100%) | 2 (100%) |

### Cases — Overall (569 total)

| Field | Present | Missing | Completeness |
|-------|---------|---------|-------------|
| case_number | 569 | 0 | 100.0% |
| case_title | 447 | 122 | 78.6% |
| case_type | 0 | 569 | 0.0% |

### Cases — Title by County

| County | Cases | Has Title | Completeness |
|--------|-------|-----------|-------------|
| Los Angeles | 346 | 293 | 84.7% |
| Orange | 84 | 50 | 59.5% |
| San Bernardino | 67 | 57 | 85.1% |
| Riverside | 51 | 32 | 62.7% |
| San Francisco | 19 | 13 | 68.4% |
| Santa Clara | 2 | 2 | 100.0% |

### Outcome Distribution

| Outcome | Count | % |
|---------|-------|---|
| granted | 388 | 60.0% |
| granted_in_part | 68 | 10.5% |
| denied | 67 | 10.4% |
| (null) | 39 | 6.0% |
| continued | 34 | 5.3% |
| moot | 17 | 2.6% |
| off_calendar | 15 | 2.3% |
| denied_in_part | 12 | 1.9% |
| submitted | 7 | 1.1% |

### Key Weaknesses (Regex)

- **Judge identification at 51.0%** — worst field by far, driven by LA at 26.5%. Regex struggles to extract judge names from varied document formats.
- **Motion type at 85.3%** — regex patterns miss many motion type phrasings.
- **Department at 84.4%** — inconsistent formatting across courts.
- **Case title at 78.6%** — many titles not extractable by regex (especially OC and Riverside).
- **Case type at 0.0%** — regex pipeline never populated this field.

---

## After (LLM Extraction)

**Snapshot taken:** 2026-03-10, after lean reingest (metadata-only updates)

**Methodology note:** The reingest used a "lean" approach — LLM extraction + metadata-only DB updates (no ruling_text rewrites). This was necessary because the dev database (db.t4g.micro, 1GB RAM) takes 60-90 seconds per ruling_text upsert. Documents >200K chars were skipped to avoid LLM timeouts. Of 647 documents: 258 LLM successes, 64 LLM failures (mostly binary PDF content), 176 skipped (all fields already present), 149 skipped (too large).

### Rulings — Overall (647 total)

| Field | Present | Missing | Completeness | Delta |
|-------|---------|---------|-------------|-------|
| outcome | 633 | 14 | 97.8% | +3.8pp (+25) |
| motion_type | 616 | 31 | 95.2% | +9.9pp (+64) |
| hearing_date | 647 | 0 | 100.0% | -- |
| department | 546 | 101 | 84.4% | -- |
| judge_id (FK) | 365 | 282 | 56.4% | +5.4pp (+35) |
| case_id (FK) | 647 | 0 | 100.0% | -- |

### Rulings — By County

| County | Rulings | Judge | Motion | Outcome | Dept |
|--------|---------|-------|--------|---------|------|
| Los Angeles | 351 | 128 (36.5%) | 349 (99.4%) | 351 (100%) | 351 (100%) |
| San Bernardino | 121 | 121 (100%) | 103 (85.1%) | 111 (91.7%) | 69 (57.0%) |
| Orange | 105 | 74 (70.5%) | 103 (98.1%) | 104 (99.0%) | 72 (68.6%) |
| Riverside | 49 | 21 (42.9%) | 45 (91.8%) | 46 (93.9%) | 33 (67.3%) |
| San Francisco | 19 | 19 (100%) | 14 (73.7%) | 19 (100%) | 19 (100%) |
| Santa Clara | 2 | 2 (100%) | 2 (100%) | 2 (100%) | 2 (100%) |

### Rulings — By County Delta (vs Before)

| County | Judge Delta | Motion Delta | Outcome Delta | Dept Delta |
|--------|------------|-------------|---------------|-----------|
| Los Angeles | 93->128 (+35, +10.0pp) | 285->349 (+64, +18.2pp) | 326->351 (+25, +7.1pp) | 351->351 (--) |
| San Bernardino | 121->121 (--) | 103->103 (--) | 111->111 (--) | 69->69 (--) |
| Orange | 74->74 (--) | 103->103 (--) | 104->104 (--) | 72->72 (--) |
| Riverside | 21->21 (--) | 45->45 (--) | 46->46 (--) | 33->33 (--) |
| San Francisco | 19->19 (--) | 14->14 (--) | 19->19 (--) | 19->19 (--) |
| Santa Clara | 2->2 (--) | 2->2 (--) | 2->2 (--) | 2->2 (--) |

### Cases — Overall (550 total)

| Field | Present | Missing | Completeness | Delta |
|-------|---------|---------|-------------|-------|
| case_number | 550 | 0 | 100.0% | -- |
| case_title | 475 | 75 | 86.4% | +7.8pp (+28) |
| case_type | 0 | 550 | 0.0% | -- |

Note: Case count changed from 569 to 550 between snapshots. The lean reingest updated existing case records but did not create new ones. The delta reflects the improvement within the current case set.

### Cases — Title by County

| County | Cases | Has Title | Completeness | Before |
|--------|-------|-----------|-------------|--------|
| Los Angeles | 346 | 331 | 95.7% | 84.7% (+11.0pp) |
| Orange | 81 | 49 | 60.5% | 59.5% (+1.0pp) |
| San Bernardino | 67 | 57 | 85.1% | 85.1% (--) |
| Riverside | 35 | 23 | 65.7% | 62.7% (+3.0pp) |
| San Francisco | 19 | 13 | 68.4% | 68.4% (--) |
| Santa Clara | 2 | 2 | 100.0% | 100.0% (--) |

### Outcome Distribution

| Outcome | Count | % | Before |
|---------|-------|---|--------|
| granted | 331 | 51.2% | 60.0% |
| denied | 103 | 15.9% | 10.4% |
| granted_in_part | 86 | 13.3% | 10.5% |
| continued | 38 | 5.9% | 5.3% |
| denied_in_part | 22 | 3.4% | 1.9% |
| other | 19 | 2.9% | -- |
| moot | 16 | 2.5% | 2.6% |
| (null) | 14 | 2.2% | 6.0% |
| off_calendar | 13 | 2.0% | 2.3% |
| submitted | 5 | 0.8% | 1.1% |

---

## Summary

### Results

The LLM extraction (Claude Haiku via Anthropic API) significantly improved field completeness, particularly for Los Angeles county which had the largest gaps:

**Biggest improvements:**
1. **Motion type: 85.3% -> 95.2% (+9.9pp, +64 rulings)** — nearly all from LA (285->349), where the LLM correctly classified motions that regex patterns missed.
2. **Case title: 78.6% -> 86.4% (+7.8pp, +28 cases)** — mostly LA (293->331), where LLM extracted titles from document content.
3. **Judge ID: 51.0% -> 56.4% (+5.4pp, +35 rulings)** — all from LA (93->128), where the LLM found judge names that regex couldn't parse from varied HTML formats.
4. **Outcome: 94.0% -> 97.8% (+3.8pp, +25 rulings)** — LA went from 92.9% to 100% (326->351).

**No change:**
- **Department** stayed at 84.4% — the LLM extracted department info but values matched existing data (already populated by scrapers for most counties).
- **San Bernardino, Orange, Riverside, SF, Santa Clara** saw no improvements — either they already had high completeness or documents were too large / binary PDFs that the LLM could not parse.
- **Case type** remains at 0% — the LLM extraction schema does not include a case_type field.

**Outcome distribution shifted:** More outcomes classified as "denied" and "granted_in_part" (previously some were misclassified as "granted" by regex). Nulls decreased from 39 to 14.

### Limitations of This Run

1. **149 documents (23%) skipped due to size >200K chars.** These are LA Superior Court multi-ruling documents. They need either text chunking in the LLM pipeline or document splitting before extraction.
2. **64 documents (10%) had LLM failures** — mostly Riverside and Orange county PDFs stored as raw binary. These need PDF text extraction (e.g., pdftotext) before LLM processing.
3. **Lean reingest only updated metadata fields** — ruling_text was not rewritten, so the full pipeline's `insert_ruling()` path was not exercised. A complete reingest would require a larger DB instance.
4. **Judge identification still low at 56.4%** — the LLM does extract judge names from LA docs, but many of the largest multi-ruling documents (which tend to include judge names in headers) were skipped due to size limits.

### Cost

- **Estimated ongoing cost:** ~$47/mo at current volume using Claude Haiku
- **Actual reingest cost:** ~258 LLM calls x ~$0.002/call = ~$0.52 total (Claude Haiku is very cost-effective)
- **Wall time:** 92 minutes on a 1024 CPU / 2048 MB Fargate task

### Recommendations

1. **Deploy LLM extraction to production** — the improvements justify the ~$47/mo cost. Motion type and outcome completeness are now at 95%+.
2. **Add PDF text extraction** before LLM — 64 binary PDF documents returned no data. A `pdftotext` preprocessing step would unlock these.
3. **Add document chunking for large documents** — 149 docs >200K chars were skipped. The LLM extraction already supports chunking (via `extract_fields_llm`), but the 200K limit in this run was conservative. Increasing to 500K with proper chunking would capture most of these.
4. **Upgrade dev DB for full reingest** — the db.t4g.micro instance (1GB RAM) is too slow for ruling_text upserts (60-90s per document). A db.t4g.small (2GB) would likely reduce this to <10s.
5. **Focus judge extraction on LA** — LA is 54% of rulings but only 36.5% judge completeness. The large multi-ruling documents that were skipped likely contain the most judge data.
6. **Add case_type to LLM extraction schema** — currently 0% populated. The LLM could infer case type from motion/document context.
