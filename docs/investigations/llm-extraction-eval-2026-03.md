# LLM-Based Field Extraction Evaluation

**Issue:** #418
**Date:** 2026-03-09
**Status:** Complete

## Summary

Evaluated replacing per-court regex parsers with LLM-based field extraction using Claude Haiku, Sonnet, and Opus. Ran 18 test fixtures (4 LA HTML, 10 OC PDF, 1 OC complex, 3 Riverside PDF) through all three models with identical prompts.

**Bottom line:** All three models perform nearly identically (78–79% overall accuracy). The errors are systematic — caused by prompt design and ambiguous test expectations, not model capability. Tiered escalation (Haiku → Sonnet → Opus) adds cost without improving accuracy. Haiku is the clear winner at $31/mo vs $588/mo for Opus.

## Accuracy Results

| Model | Overall | judge_name | hearing_date | department | case_number | case_title | outcome | motion_type | case_count |
|-------|---------|-----------|-------------|-----------|------------|-----------|---------|------------|-----------|
| Haiku | 78.4% | 77.8% | 94.4% | 88.9% | 94.4% | 75.0% | 25.0% | 50.0% | 55.6% |
| Sonnet | 78.4% | 77.8% | 94.4% | 88.9% | 94.4% | 75.0% | 25.0% | 50.0% | 55.6% |
| Opus | 79.4% | 77.8% | 100.0% | 88.9% | 94.4% | 75.0% | 25.0% | 50.0% | 55.6% |

Opus's only advantage: 100% on hearing_date (vs 94.4%).

## Cost Projections (200 rulings/day, 6,000/month)

| Scenario | Monthly Cost |
|----------|-------------|
| All Haiku | $31/mo |
| 80% Haiku + 20% Sonnet | $48/mo |
| 70% Haiku + 25% Sonnet + 5% Opus | $80/mo |
| All Sonnet | $116/mo |
| All Opus | $588/mo |

**Note:** These are 3× higher than the original issue estimates because actual token counts (~5,400 input tokens/ruling) are much higher than the estimated ~1,200. PDF fixtures and HTML rulings contain substantial boilerplate.

## Latency

| Model | Avg Latency |
|-------|-------------|
| Haiku | 2.2s |
| Sonnet | 5.5s |
| Opus | 5.1s |

All models are fast enough for batch processing. At 200 rulings/day, even sequential Haiku processing takes <8 minutes.

## Determinism

Haiku produced **identical** outputs across 5 runs on all 5 test fixtures. Non-determinism is not a practical concern with temperature=0.

## Confidence Calibration

**Self-reported confidence is NOT a reliable escalation signal.**

- High-confidence `outcome` accuracy: 25–33% (models report high confidence on wrong answers)
- High-confidence `case_count` accuracy: 59–62%
- Overall high-confidence accuracy: ~85% (should be >95% to be useful for escalation)

This means the tiered escalation strategy based on confidence thresholds would not work as designed. A low-confidence flag would miss most actual errors.

## Error Analysis

### Errors shared by ALL three models (systematic, prompt-related):

| Fixture | Field | Expected | Got | Root Cause |
|---------|-------|----------|-----|------------|
| oc_north_n.pdf | judge_name | Julianne S. Bancroft | Richard J. Oberholzer | Multi-department PDF — model picked first judge, not dept header judge |
| oc_north_n.pdf | department | N6 | N14 | Same — model picked wrong department from multi-dept document |
| riv_moreno_valley.pdf | judge_name | David E. Gregory | None | Judge name only in PDF metadata/header, not in ruling body text |
| oc_apkarian_c25.pdf | case_count | 14 | 2 | 36-page PDF — model only counted cases visible in first portion |
| oc_west_w.pdf | case_count | 10 | 1 | Same — multi-case PDF, model undercounted |
| oc_costa_mesa_cm.pdf | case_count | 5 | 2 | Same pattern |
| oc_family_law_claustro_c22.pdf | outcome | OFF CALENDAR | continued | Outcome classification mismatch |
| oc_family_law_kohler_l69.pdf | outcome | OFF CALENDAR | None | Boilerplate PDF with no actual rulings |
| oc_probate_cm3.pdf | outcome | DENIED | granted_in_part | Complex ruling with multiple outcomes |

### Normalization issues (eval script, not real errors):

| Fixture | Field | Expected | Got | Fix Needed |
|---------|-------|----------|-----|------------|
| oc_costa_mesa_cm.pdf | department | CM02 | CM2 | Leading zero normalization |
| oc_probate_cm5.pdf | case_title | Collins - Trust | Collins – Trust | Dash vs en-dash |
| oc_central_c34.pdf | case_number | 2024-01393434 | 30-2024-01393434 | County prefix handling |
| oc_probate_cm3.pdf | motion_type | MOTIONS FOR JUDGMENT ON THE PLEADINGS | motion_for_judgment_on_the_pleadings | Case normalization |

### Genuine ambiguities:

- **Multi-department PDFs:** The prompt asks for "the first ruling" but some PDFs contain multiple departments. The model doesn't know which department the user cares about.
- **Case count on long PDFs:** Models consistently undercount cases in PDFs with 10+ cases. Likely a PDF text extraction issue — the full text may not be making it into the prompt, or the model struggles to count systematically through long documents.
- **Outcome classification:** "OFF CALENDAR" vs "continued" and "DENIED" vs "granted_in_part" suggest the outcome taxonomy in the prompt needs clearer definitions.

## Key Findings

1. **Model tier doesn't matter.** Haiku, Sonnet, and Opus make the same mistakes. The bottleneck is prompt design and input preprocessing, not model intelligence.

2. **Confidence-based escalation is not viable.** Models report high confidence on wrong answers. An alternative escalation signal would be needed (e.g., output validation rules, or spot-check sampling without relying on self-assessment).

3. **The prompt needs iteration.** Most errors fall into fixable categories:
   - Better handling of multi-department/multi-case documents
   - Clearer outcome taxonomy with examples
   - Explicit instructions for case counting methodology
   - Better PDF preprocessing to ensure all content is in the prompt

4. **Some test expectations need updating.** At least 4 mismatches are normalization issues in the eval script, not real extraction errors.

5. **Cost is manageable but higher than estimated.** $31/mo for Haiku is acceptable but 3× the original $10 estimate due to higher actual token counts.

6. **Determinism is a non-issue.** Perfect consistency across 5 runs.

## Recommendation

**Do NOT proceed with tiered model escalation.** The data shows it adds cost without improving accuracy.

**Recommended next steps:**

1. **Improve the extraction prompt** — fix the systematic errors identified above. A second round of eval with an improved prompt should push accuracy above 90%.

2. **Fix PDF preprocessing** — ensure all pages of multi-case PDFs are included in the prompt. The current approach may be truncating long documents.

3. **Start with hybrid approach** — use LLM extraction for new courts only (where we'd otherwise need to write new regex parsers). Keep existing regex parsers for courts where they work well. This gives us the main benefit (no new parser code) at minimal risk.

4. **Fix eval normalization** — update the comparison logic to handle leading zeros, dashes/endashes, case prefixes, and case-insensitive motion types.

5. **Consider structured output validation** — instead of confidence-based escalation, validate extracted fields against known patterns (e.g., case number format regex, date range sanity checks) and flag anomalies.

## Decisions Needed (Human Input)

1. **Budget threshold:** Is $31/mo for Haiku acceptable for LLM extraction? (Original estimate was $10/mo.)
2. **Hybrid vs full migration:** Should we use LLM for new courts only, or also migrate existing courts?
3. **Accuracy threshold:** What per-field accuracy is required before we ship LLM extraction to production? (Current: ~79% overall, but many "errors" are normalization issues.)

## Raw Data

Full results JSON: `worktrees/worker-2/tmp/llm_eval_results.json`
Eval script: `worktrees/worker-2/tmp/llm_extraction_eval.py`
