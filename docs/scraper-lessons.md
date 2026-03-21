# Scraper Development — Lessons Learned

Common issues found during audits and fixes. Consult this when writing or reviewing scrapers.

## Regex Patterns

- **Always use `re.IGNORECASE`** for date, name, and keyword patterns. PDF text extraction produces inconsistent casing — "FEBRUARY", "February", "february" all appear. OC civil and family law scrapers shipped without IGNORECASE on hearing date patterns and missed ~37% of dates.
- **Use `re.MULTILINE`** when anchoring with `^` or `$` in multi-line PDF text.
- **Normalize whitespace before matching.** PDFs often have extra spaces, line breaks mid-word, or non-breaking spaces (`\xa0`). Use `" ".join(text.split())` on extracted groups.

## Regex Patterns for Natural Language

When writing regex for legal text, natural language variants are easy to miss. Use this checklist before shipping any regex that matches human-written text:

### Checklist

1. **Singular/plural.** Always use optional suffixes: `rulings?`, `orders?`, `motions?`, `departments?`. Never assume the text uses only one form.
2. **Abbreviations and alternate spellings.** Courts use abbreviations inconsistently: `dept` vs `department`, `no.` vs `number`, `vs.` vs `v.`, `assn` vs `association`. Use alternation groups: `(?:dept|department)`.
3. **Case variations.** Court documents appear in ALL CAPS, Title Case, and lowercase — often within the same page. Always use `re.IGNORECASE` (or the `(?i)` inline flag) for any pattern matching natural language. Use `\b` word boundaries to prevent false matches.
4. **Optional punctuation and whitespace.** Periods, colons, and hyphens are inconsistent: `Case No.`, `Case No`, `Case Number`. Use optional markers: `no\.?`, `\s*`, `[-\s]?`.
5. **Word boundaries.** Use `\b` to avoid matching substrings — `\bruling\b` prevents matching "overruling".

### Case study: "Tentative Ruling" vs "Tentative Rulings"

In #1247, a header-stripping regex used `tentative\s+ruling\b` to remove page headers from ruling text. This matched "Tentative Ruling" but not "Tentative Rulings" — the plural form that some courts use. The bug was not caught until post-deploy verification, requiring a follow-up PR (#1297) to fix.

The fix was simple — `tentative\s+rulings?\b` — but the missed variant caused incorrect ruling text to be served to users until the fix deployed.

**Lesson:** When writing regex for any term that appears in court documents, always grep existing fixtures and production data for variants before committing to a pattern. A 30-second search for `tentative ruling` across fixtures would have revealed both forms immediately.

## Field Extraction

- **The ingestion worker gates ruling rows on `hearing_date`.** If a scraper doesn't extract `hearing_date`, no ruling row is created — which means judge, outcome, motion_type are all lost too. Hearing date extraction is the single most important field.
- **Always implement fallback extraction.** The narrow scraper-specific pattern should be tried first, then fall back to `extract.py` centralized regex. Example: LA scraper's `_JUDGE_DIV_RE` is very narrow; adding a fallback to `extract_judge_name()` from `extract.py` improved coverage from 27% to much higher.
- **Extract all 6 required fields:** judge name, motion type, case title, hearing date, outcome, parties. Don't ship a scraper that leaves extractable fields empty — backfills are unreliable.

## PDF-Specific Issues

- **PDF text extraction order varies.** `pdfplumber` extracts text in visual order, which may not match reading order for multi-column layouts.
- **San Bernardino extracts hearing date from filename, not content.** This is fragile — if the filename format changes, all dates are lost. Prefer content-based extraction with filename as fallback.
- **Multi-ruling PDFs (Riverside):** When splitting a PDF into individual rulings, ensure metadata (hearing_date, judge_name) from the parent document propagates to all children.
- **Boilerplate PDF detection (#322):** Courts publish placeholder PDFs for departments with no rulings. These contain only instructional text (e.g. "No Tentative Rulings") or an empty case table. The `PdfLinkScraper` base class has a `_is_boilerplate(text)` hook that subclasses can override to detect court-specific patterns. The base implementation catches "No Tentative Rulings" text. OC scrapers additionally check for empty "TENTATIVE RULINGS / Date:" headers with no case numbers. Always override `_is_boilerplate()` when adding a new PDF-based scraper if the court publishes placeholder pages.

## Ingestion Pipeline

- **`insert_document` and `insert_ruling` use upsert semantics** (ON CONFLICT DO UPDATE). Re-ingesting a document updates mutable fields (hearing_date, judge_id, outcome, etc.) while preserving immutable fields (s3_key, content_hash, captured_at).
- **`extract.py` provides centralized fallback extraction** for: case_number, judge_name, motion_type, outcome, case_title, hearing_date. The ingestion worker uses these when scrapers don't populate fields. When adding a new extraction pattern, add it to `extract.py` so all courts benefit.
- **The worker has fallback chains for critical fields.** If the scraper event doesn't include hearing_date, case_number, or case_title, the worker tries `extract_hearing_date()`, `extract_case_number()`, and `extract_case_title()` from ruling text before giving up. This is a safety net, not a substitute for proper scraper extraction.
- **Party extraction is the hardest field.** Only LA implements it (structured HTML with role labels). PDF-based courts don't have reliable party structure. This remains an open problem.

## Text Comparison

- **Always use `autojunk=False` with `difflib.SequenceMatcher` when comparing legal text.** The default `autojunk=True` marks frequently-repeated characters as "junk" to speed up matching on short strings. Legal documents are highly repetitive (standard phrases like "The motion for summary judgment is GRANTED." appear many times), causing the heuristic to treat common characters as junk. This produces wildly incorrect similarity scores — e.g. 0.19 instead of 0.99 for texts that differ only by whitespace. Discovered during #978 (ruling text formatter validation). Always pass `SequenceMatcher(None, a, b, autojunk=False)`.

## Testing

- **Every scraper needs regression tests against real fixtures.** Save actual PDFs/HTML to `tests/fixtures/` and test field extraction against them.
- **Test edge cases explicitly:** empty PDFs, PDFs with no rulings, unusual date formats, missing fields.
- **Run the full test suite before pushing** — `695 tests` across all scrapers as of 2026-03-08.

## Composed / Pipeline Scrapers

Some courts require a multi-phase scraping pipeline — for example, one phase discovers case numbers from a calendar and a second phase fetches tentative rulings for each case. The canonical example is `SDPipelineScraper` (`packages/scraper-framework/src/courts/ca/sd_pipeline.py`), which chains `SDCalendarScraper` (Phase 1) with `SDTentativeRulingsScraper` (Phase 2).

### When to use this pattern

Use a pipeline scraper when:
- Data collection naturally splits into discovery (Phase 1) and detail-fetching (Phase 2).
- Each phase has fundamentally different scraping mechanics (e.g., plain HTTP vs. Playwright browser automation).
- The phases need different request delays, timeouts, or retry settings.

### Factory methods for child scrapers

Create each child scraper through a dedicated factory method (`_create_phase1_scraper`, `_create_phase2_scraper`) rather than constructing them inline. This has two benefits:

1. **Testability.** Tests can mock the factory methods to inject stub scrapers that return canned fixtures instead of hitting real court sites.
2. **Lazy imports.** Importing child scraper modules inside the factory avoids circular imports and keeps the pipeline module lightweight.

```python
def _create_phase1_scraper(
    self,
    archiver: S3Archiver | None,
    event_bus: EventBus | None,
) -> BaseScraper:
    from courts.ca.sd_calendar import SDCalendarScraper
    from courts.ca.sd_calendar import default_config as calendar_config

    config = calendar_config(s3_bucket=self.config.s3_bucket)
    return SDCalendarScraper(config=config, archiver=archiver, event_bus=event_bus)
```

### Config independence between parent and child

Each child scraper should use **its own default config**, not inherit the pipeline's config. Phase 1 (plain HTTP, 1s delay, 3 retries) and Phase 2 (Playwright browser, 3s delay, 2 retries) have fundamentally different optimal settings. Only environment-level config like `s3_bucket` should be propagated from the parent.

```python
# Good: each phase gets its own tuned config
config = calendar_config(s3_bucket=self.config.s3_bucket)

# Bad: inheriting the pipeline's config forces both phases to share settings
return SDCalendarScraper(config=self.config, ...)
```

### Delegating `parse_document` to a child scraper

The pipeline scraper's `fetch_documents` returns Phase 2 documents, so `parse_document` must delegate to the Phase 2 scraper. This keeps parsing logic in one place rather than duplicating it in the pipeline orchestrator.

```python
def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
    parser = self._get_phase2_parser()
    return parser.parse_document(doc)
```

### Lazy caching for parser instances

The `parse_document` method is called once per document. Creating a new Phase 2 scraper for each call is wasteful. Use a lazy-cached instance:

```python
def __init__(self, ...) -> None:
    ...
    self._phase2_parser: BaseScraper | None = None

def _get_phase2_parser(self) -> BaseScraper:
    if self._phase2_parser is None:
        self._phase2_parser = self._create_phase2_scraper(
            case_numbers=[],
            archiver=self._archiver,
            event_bus=self._event_bus,
        )
    return self._phase2_parser
```

Note: pass an empty `case_numbers=[]` (or equivalent no-op arguments) since the parser instance is only used for `parse_document`, not `fetch_documents`.

### Deduplication of intermediate results

Phase 1 may return multiple calendar entries for the same case number. Deduplicate before passing to Phase 2 to avoid redundant work:

```python
seen: set[str] = set()
case_numbers: list[str] = []
for doc in phase1_docs:
    cn = doc.case_number
    if cn and cn not in seen:
        seen.add(cn)
        case_numbers.append(cn)
```

Use a `set` for O(1) membership checks but maintain a `list` for deterministic ordering.

### Testing pipeline scrapers

- **Mock the factory methods**, not the child scrapers' internals. This lets you test the pipeline's orchestration logic (deduplication, Phase 1 -> Phase 2 handoff, empty results) in isolation.
- **Test the empty-results path.** If Phase 1 returns no case numbers, Phase 2 should be skipped entirely.
- **Test deduplication.** Provide Phase 1 fixtures with duplicate case numbers and verify Phase 2 receives the deduplicated list.
