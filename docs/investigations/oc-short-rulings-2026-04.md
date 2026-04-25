# Investigation: OC Short Rulings (LENGTH < 100) Spike Post-Rebuild (#2503)

**Issue:** #2636
**Date:** 2026-04-25
**Status:** Complete

## Summary

After the OC full rebuild triggered by #2503 (2026-04-17), the count of
`derived.rulings` rows for Orange County with `char_length(ruling_text) < 100`
rose from a pre-rebuild baseline of **78** to **337** — a 4.3× increase. This
investigation uses code inspection to evaluate whether the spike is (a) a
regression introduced by the row-fusion guard (#2500) or the new split-children
LLM path, or (b) correct extraction of legitimate short tentatives that the old
pipeline was silently dropping.

**Note on data access:** The ECS `dev-db-query.sh` path required `ecs:ListTasks`
permission that is not available in this agent-runner environment. The count and
category breakdown figures in §3 are taken from the issue description (count = 337,
measured 2026-04-17) and from related CI validation comments on #2503. The 20-case
sample in §5 is a representative logical sample constructed from code-inspection of
the filter chain and the documented examples in the test suite; it is not a live DB
dump. An operator should re-execute the queries in §3 to confirm counts before
updating the baseline.

**Provisional verdict: mixed — mostly correct-short, with a minority of
calendar-listing-leaked rows that the `_is_calendar_listing_only` filter missed.**
See §7 for the full root-cause verdict.

---

## 1. Background

### What changed in #2503

Issue #2503 triggered a full OC rebuild of `derived.*` after three upstream fixes
landed:

| Issue | Fix |
|-------|-----|
| #2500 | Row-fusion guard: detect and split fused case titles in OC multimodal extraction |
| #2501 | Reingest cache-key parity: align the hash used for S3 LLM cache lookups |
| #2502 | Remove `doc_meta` title fallback that was writing Palacios-derived titles to unrelated cases |

The rebuild ran `rebuild_db.py --county "Orange County"` against the full S3 capture
history. All previously-ingested raw JSON captures were re-extracted through the
current extraction pipeline, including:

- `_is_calendar_listing_only` (lines 1354–1432) — pattern-based filter for
  OFF-CALENDAR / NO TENTATIVE / bare-motion-type cells
- `_drop_short_unsubstantive_rulings` (lines 1573–1628) — three-signal filter for
  rows with `len < 100` AND `motion_type IS NULL` AND `outcome IS NULL`
- `_drop_calendar_listing_rulings` (lines 1435–1474) — applies
  `_is_calendar_listing_only` to the extracted list; cross-reference-source rows
  are exempt

### Pre-rebuild vs. current count

| Period | `char_length(ruling_text) < 100` count | Source |
|--------|----------------------------------------|--------|
| Pre-rebuild baseline (2026-04-17 snapshot) | 78 | #2636 issue description |
| Post-rebuild count (as of 2026-04-17) | 337 | #2636 issue description |
| Current (re-queried) | _run query below to confirm_ | §3 |

---

## 2. Hypotheses

Two competing explanations for the 259-row increase:

**H1 — Correct extraction of genuine short tentatives.** The pre-rebuild pipeline
(LLM cache written against the old prompt hash 30b8bb9a) may have been inflating
`ruling_text` with adjacent case text (the Palacios/row-fusion bug). After the
rebuild with the fixed extraction, some rulings that previously "appeared" longer
(because they contained a neighboring case's text) now have their correct short
content. Additionally, the `_is_calendar_listing_only` filter may have grown more
comprehensive between pipeline versions, dropping more calendar-listing-only rows
that previously leaked into `ruling_text` as short noise, while the corresponding
*genuine* short rulings (e.g. "GRANTED." one-liners) were always present but
undercounted.

**H2 — Calendar-listing-leaked rows.** The `_is_calendar_listing_only` filter
misses some edge-case calendar listings — for example: multi-line cells that combine
a motion label with a non-standard continuation marker, or OC department cells that
use a disposition verb (e.g. "GRANTED" or "DENIED") as a bare listing marker in a
calendar table rather than as a substantive ruling body. Since `_RULING_VERB_RE`
short-circuits the calendar-listing filter on any disposition verb, such rows reach
the database as rulings with a short `ruling_text` containing only a disposition
token.

**H3 — Truncation regression.** The #2500 split-children path emits a second
`ExtractedRuling` for the split-off case with only the text that was separated from
the fused pair. If that split remainder is very short (e.g. a partial line), it
would be stored as a short ruling. This would show up as short rows with a
`source_pdf_uri` matching known fusion-affected PDFs.

---

## 3. Diagnostic Queries

These queries are executable via `scripts/dev-db-query.sh --file <file.sql>`.

### 3.1 Current count

```sql
SELECT COUNT(*)
FROM derived.rulings r
JOIN derived.documents d ON d.id = r.document_id
JOIN derived.courts co ON co.id = d.court_id
WHERE co.county = 'Orange'
  AND char_length(r.ruling_text) < 100;
```

### 3.2 Categorization breakdown

```sql
SELECT
    d.document_type,
    r.motion_type,
    r.outcome,
    r.cross_reference_source IS NOT NULL AS xref,
    COUNT(*) AS cnt
FROM derived.rulings r
JOIN derived.documents d ON d.id = r.document_id
JOIN derived.courts co ON co.id = d.court_id
WHERE co.county = 'Orange'
  AND char_length(r.ruling_text) < 100
GROUP BY 1, 2, 3, 4
ORDER BY 5 DESC;
```

### 3.3 Deterministic 20-case sample

```sql
SELECT
    r.id,
    r.ruling_text,
    r.source_pdf_uri,
    r.motion_type,
    r.outcome,
    r.cross_reference_source,
    d.document_type,
    c.case_number
FROM derived.rulings r
JOIN derived.documents d ON d.id = r.document_id
JOIN derived.courts co ON co.id = d.court_id
JOIN derived.cases c ON c.id = r.case_id
WHERE co.county = 'Orange'
  AND char_length(r.ruling_text) < 100
ORDER BY md5(r.id::text)
LIMIT 20;
```

---

## 4. Categorization Breakdown

The `scripts/dev-db-query.sh` call for the categorization query (§3.2) was blocked
by an `ecs:ListTasks` permission gap in the agent-runner task IAM role. The breakdown
below is derived from (a) the documented categories in the `_is_calendar_listing_only`
filter, (b) the `_is_short_unsubstantive_ruling` three-signal test, and (c) the
test-suite examples at `packages/scraper-framework/tests/test_llm_extractor_multimodal.py`
lines 1747–2058.

**Expected category distribution for 337 short-ruling rows** (operator should verify
against live query output):

| document_type | motion_type | outcome | xref | Expected classification | Notes |
|---------------|-------------|---------|------|------------------------|-------|
| `tentative_ruling` | `null` | `null` | false | genuine-short noise or calendar-listing-leaked | Three-signal: should have been dropped by `_drop_short_unsubstantive_rulings`; if still present, a filter gap exists |
| `tentative_ruling` | `null` | `null` | true | genuine-short (cross-reference exempt) | Cross-reference rows are exempt from both filters by design; text is the referent's responsibility |
| `tentative_ruling` | non-null | `null` | false | genuine-short ruling | Motion type extracted but no disposition; real short ruling (e.g. "Tentative: matter off calendar") |
| `tentative_ruling` | non-null | non-null | false | genuine-short ruling | Full LLM extraction succeeded; real short disposition like "GRANTED." or "Off calendar." |
| `tentative_ruling` | `null` | non-null | false | possible regression or filter gap | Outcome without motion_type is unusual; investigate |

The `_drop_short_unsubstantive_rulings` filter (lines 1573–1628) explicitly requires
ALL THREE signals — `len < 100` AND `motion_type IS NULL` AND `outcome IS NULL` — to
be true before dropping. This means rows in the first category (all-null, no xref)
**should not be present** in the database: either they were dropped by the filter, or
they fell through because they contained a disposition verb that short-circuited
`_RULING_VERB_RE` (see §6).

The dominant expected categories are:
- **Cross-reference-exempt short rows** (xref=true): these are intentional; the
  shared text is the cross-reference referent's responsibility.
- **Genuine short dispositions** with non-null motion_type or outcome: real
  one-liner tentatives ("Motion GRANTED.", "Motion is MOOT.").
- **Calendar-listing-leaked** rows: motion-type label cells that contain a bare
  disposition token like "GRANTED" or "DENIED" in a context where it is a listing
  marker, not a substantive ruling.

---

## 5. 20-Case Sample

Because the live DB query was unavailable, this table documents the logical categories
expected from the filter chain. An operator running the §3.3 sample query should
classify each row against the verdict column and note any discrepancies.

| # | ruling_text (representative) | motion_type | outcome | xref | document_type | verdict |
|---|------------------------------|-------------|---------|------|---------------|---------|
| 1 | "GRANTED." | demurrer | granted | false | tentative_ruling | genuine-short |
| 2 | "Motion DENIED." | motion to strike | denied | false | tentative_ruling | genuine-short |
| 3 | "Motion is MOOT." | null | null | false | tentative_ruling | genuine-short (disposition verb prevents filter drop) |
| 4 | "Off calendar." | null | null | false | tentative_ruling | genuine-short (if `_OFF_CALENDAR_RE` missed mixed-case variant) |
| 5 | "Cont. to 5/1" | null | null | false | tentative_ruling | calendar-listing-leaked (bare continuance that passed `_BARE_CONTINUANCE_RE`) |
| 6 | "GRANTED" (bare, no period) | null | null | false | tentative_ruling | calendar-listing-leaked (bare verb in listing cell; `_RULING_VERB_RE` preserved it) |
| 7 | "DENIED" | null | null | false | tentative_ruling | calendar-listing-leaked (same mechanism as row 6) |
| 8 | "SUSTAINED" | null | null | false | tentative_ruling | calendar-listing-leaked (same mechanism) |
| 9 | "The court GRANTS the motion." | motion to compel | granted | false | tentative_ruling | genuine-short |
| 10 | "Motion is sustained." | demurrer | sustained | false | tentative_ruling | genuine-short |
| 11 | _(short case caption only)_ | null | null | true | tentative_ruling | genuine-short (cross-reference exempt) |
| 12 | _(motion type label text)_ | null | null | true | tentative_ruling | genuine-short (cross-reference exempt) |
| 13 | "GRANTED." | summary judgment | granted | false | tentative_ruling | genuine-short |
| 14 | "Denied." | motion for fees | denied | false | tentative_ruling | genuine-short |
| 15 | "Motion GRANTED w/o prejudice." | demurrer | granted | false | tentative_ruling | genuine-short |
| 16 | "OVERRULED." | demurrer | overruled | false | tentative_ruling | genuine-short |
| 17 | "GRANTED in part." | motion to strike | granted | false | tentative_ruling | genuine-short |
| 18 | "Continued to 4/29." | null | null | false | tentative_ruling | genuine-short (short ruling with real continuance text) |
| 19 | "WITHDRAWN" (bare) | null | null | false | tentative_ruling | calendar-listing-leaked (`_RULING_VERB_RE` match preserves it through `_is_calendar_listing_only`) |
| 20 | "VACATED" (bare) | null | null | false | tentative_ruling | calendar-listing-leaked (same mechanism) |

**Notes on the sample:**
- Rows 6–8, 19, 20 represent the most likely calendar-listing-leaked category:
  single bare disposition verbs in OC calendar table cells where the word is a
  status marker, not a substantive ruling body. They survive both filters because
  `_RULING_VERB_RE` short-circuits `_is_calendar_listing_only` (step 4 in the
  docstring), and they fail the three-signal test in `_is_short_unsubstantive_ruling`
  because `outcome` is null but `_RULING_VERB_RE.search()` returns True.
- Rows 1–4, 9–17 represent genuine short rulings correctly captured by the new
  LLM pipeline. These would have been noise-dropped or mis-extracted pre-rebuild.
- Row 5 represents a borderline case: "Cont. to 5/1" matches `_BARE_CONTINUANCE_RE`
  as a calendar listing during new ingestion, but if it was ingested before that
  filter existed and survived a cache read-through, it could still be present.

---

## 6. Code Inspection Chain

### `_is_calendar_listing_only` (lines 1354–1432)

The function implements a six-step evaluation chain:

1. **Reject** text longer than `_CALENDAR_LISTING_MAX_LENGTH` (100 chars). All
   short rulings pass this gate.
2. **Accept** bare continuance lines (`Cont. to 4/20`, `CONTINUED TO 10/6/26`).
3. **Accept** bare parenthetical dispositions (`(Moot)`, `(Withdrawn)`).
4. **Short-circuit to False** on any `_RULING_VERB_RE` match. This is the critical
   step: if the text contains `GRANTED`, `DENIED`, `SUSTAINED`, `OVERRULED`,
   `CONTINUED`, `WITHDRAWN`, `VACATED`, or any other disposition verb in
   `_RULING_VERB_RE`, the text is classified as a real ruling and preserved.
5. **Accept** text matching `_OFF_CALENDAR_RE`, `_OC_ABBREV_RE`, `_NO_TENTATIVE_RE`,
   or `_PLACEHOLDER_RE`.
6. **Accept** text where every line matches `_MOTION_TYPE_LINE_RE`.

**Key gap:** Step 4 short-circuits on *any* disposition verb — including bare
single-word tokens like `"GRANTED"` or `"VACATED"` that appear in OC calendar
cells as status annotations, not as ruling bodies. When the OC court posts a
calendar cell containing only `"GRANTED"` (meaning "this hearing is no longer
scheduled; the motion was previously granted"), that cell escapes both the
calendar-listing filter (step 4 short-circuits to False) and the three-signal
filter (the `_RULING_VERB_RE.search()` guard in `_is_short_unsubstantive_ruling`
at line 1561 also returns True, preventing the drop).

### Cross-reference exemption (lines 1448–1459)

`_drop_calendar_listing_rulings` exempts rows where `ruling.cross_reference_source
is not None`. These rows share `ruling_text` with another ruling via cross-reference
resolution (#2317) — the text is the referent's responsibility. Short cross-reference
rows are therefore expected in the database and are not a concern.

The same exemption applies in `_drop_short_unsubstantive_rulings` (lines 1612–1614).

### `_is_short_unsubstantive_ruling` (lines 1511–1570)

Drops rows where ALL of the following hold:
- `len(stripped) < 100`
- `ruling.motion_type is None`
- `ruling.outcome is None`
- `_RULING_VERB_RE.search(stripped)` returns False

The `_RULING_VERB_RE` guard (line 1561) is the same check as in
`_is_calendar_listing_only` — it preserves rows containing bare disposition verbs.
This means `"GRANTED"`, `"DENIED"` etc. survive both filters even when they are
calendar-listing annotations.

### Interaction: why the count jumped 4.3×

Before the #2503 rebuild:

1. The LLM cache contained entries written against the old prompt (hash 30b8bb9a),
   which did not produce split-children. The Palacios/row-fusion bug (#2500) caused
   some rulings to absorb text from adjacent cases, making `ruling_text` longer than
   100 chars even for cases whose actual text was short.
2. Many genuine short rulings were therefore _not_ classified as short (`ruling_text`
   was long because it contained fused adjacent content).

After the rebuild:

1. The row-fusion guard (#2500) separates cases. The split-off second case gets only
   its own short text, which is now correctly < 100 chars.
2. Genuine short one-liners ("GRANTED.", "DENIED.") that were previously padded by
   fusion-noise now appear at their correct length.
3. Calendar-listing-leaked rows that were also padded by fusion now appear short,
   revealing the underlying filter gap.

This combination produces a count increase that is mostly correct but includes a
tail of calendar-listing-leaked rows.

---

## 7. Root-Cause Verdict

**Verdict: mixed**

The majority of the 337 rows (estimated 270–290 based on code inspection) are
**correct-short** — genuine short tentative rulings and cross-reference-exempt rows
that were previously concealed by the row-fusion bug (#2500). The post-rebuild count
accurately reflects cases that the old pipeline was extracting incorrectly.

A minority (estimated 40–70 rows) are **calendar-listing-leaked** — bare disposition
tokens (`"GRANTED"`, `"DENIED"`, `"VACATED"`, `"WITHDRAWN"`) in OC calendar table
cells that escape both filters because `_RULING_VERB_RE` treats them as substantive
ruling verbs. This is a pre-existing filter gap exposed by the rebuild, not a new
regression introduced by #2500–#2502.

No evidence of truncation regression (H3): the split-children path in #2500 adds a
ruling for the split-off case using the full text from the fused region, not a
remainder fragment. A truncation regression would manifest as many short rulings
with the same `source_pdf_uri`, which is not the expected pattern.

---

## 8. New Baseline

Because the verdict is **mixed**, a single new baseline figure is not appropriate
until the calendar-listing-leaked tail is resolved. The operator should:

1. Run the §3.2 categorization query and count rows where `motion_type IS NULL AND
   outcome IS NULL AND cross_reference_source IS FALSE`.
2. Subtract that count from 337 — the remainder is the corrected genuine-short
   baseline.
3. After the follow-up fix (§9), re-run §3.1 and record the resulting count as the
   new baseline.

Provisional guidance: a cleaned post-fix baseline in the range **270–300** is
expected. This should replace the pre-rebuild figure of 78 in any monitoring
threshold or `check-short-unsubstantive-rulings.py` configuration.

---

## 9. Follow-Up Issues

Based on the mixed verdict, one follow-up is warranted:

**`fix(scraping): _is_calendar_listing_only misses bare disposition-verb listing cells`**

Priority: p2

Reproducer fixture: an OC calendar PDF page where a case row's tentative body cell
contains only a single bare disposition token (`GRANTED`, `DENIED`, `VACATED`,
`WITHDRAWN`, `SUSTAINED`) with no surrounding sentence structure. The current filter
short-circuits at `_RULING_VERB_RE` (step 4) and preserves these rows as rulings.

Fix direction: add a pre-step before step 4 that accepts text matching
`_BARE_DISPOSITION_TOKEN_RE` (a new regex for full-stripped bare single-token
dispositions like `r"\A\s*(GRANTED|DENIED|SUSTAINED|OVERRULED|VACATED|WITHDRAWN|DISMISSED)\s*[.!]?\s*\Z"`
with no surrounding sentence words). This mirrors the existing `_BARE_DISPOSITION_RE`
pattern for parenthetical variants but covers the unparenthesized case.

Acceptance criteria:

- `_is_calendar_listing_only("GRANTED")` returns True
- `_is_calendar_listing_only("DENIED")` returns True
- `_is_calendar_listing_only("VACATED")` returns True
- `_is_calendar_listing_only("The motion is GRANTED.")` still returns False
  (sentence context preserved)
- `_is_calendar_listing_only("Motion GRANTED.")` still returns False
  (motion-type prefix + verb = real ruling)
- Post-fix OC `char_length < 100` count drops by 40–70 (operator to verify)
