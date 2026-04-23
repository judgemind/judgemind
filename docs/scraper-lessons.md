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
- **Orange County (OC) Central/West departments don't always have case numbers in PDFs (#2434).** Case number availability varies by department, not by courthouse. The original `oc_tentatives.py` docstring claimed Central (`C*`) and West (`W*`) PDFs contain `DD-DDDDDDDD` case numbers — this held for some historical PDFs but is not universal. Multiple Central departments (C10, C24) and West departments (W8, W08) now publish PDFs with only entry numbers and case titles, matching the North Justice Center pattern. UNKNOWN-prefixed case numbers for these departments are the expected fallback, not a bug. Departments observed with case numbers: C11, C20, C23, C25, C27, C28, C31, C32, C33, C34, C44, CX*, CM*. Departments observed without: W8/W08, N14, N16, N17, N18, C10, C24. When auditing OC completeness, group UNKNOWN counts by department rather than by courthouse prefix.

## Ingestion Pipeline

- **`insert_document` and `insert_ruling` use upsert semantics** (ON CONFLICT DO UPDATE). Re-ingesting a document updates mutable fields (hearing_date, judge_id, outcome, etc.) while preserving immutable fields (s3_key, content_hash, captured_at).
- **`extract.py` provides centralized fallback extraction** for: case_number, judge_name, motion_type, outcome, case_title, hearing_date. The ingestion worker uses these when scrapers don't populate fields. When adding a new extraction pattern, add it to `extract.py` so all courts benefit.
- **The worker has fallback chains for critical fields.** If the scraper event doesn't include hearing_date, case_number, or case_title, the worker tries `extract_hearing_date()`, `extract_case_number()`, and `extract_case_title()` from ruling text before giving up. This is a safety net, not a substitute for proper scraper extraction.
- **Party extraction is the hardest field.** Only LA implements it (structured HTML with role labels). PDF-based courts don't have reliable party structure. This remains an open problem.

## Defensive rollback pattern

When an exception handler inside a DB-writing function needs to recover the connection so subsequent writes can proceed, it calls `conn.rollback()`. The connection may itself be broken (closed, timed out, etc.), in which case `rollback()` also raises. To avoid cascading a second exception inside an already-failing handler, wrap the rollback in a nested `try/except` that swallows the error:

```python
try:
    insert_validation_result(conn, ...)
except Exception as exc:
    # Primary handler: log, flag, and continue.
    logger.warning("validation insert failed: %s", exc)

    # Defensive rollback: a closed connection can make rollback() itself raise.
    # We don't want to cascade a second exception inside an already-failing
    # handler, so swallow it. The next DB write will reopen the connection
    # via the worker's connection helper, so the subsequent path can proceed.
    try:
        conn.rollback()
    except Exception:
        pass
```

This pattern appears in `packages/scraper-framework/src/ingestion/worker.py` around every defensive insert (e.g. `insert_validation_result`, split-event writes) — see `#2385` for the original incident and `#2350` / `#2371` for follow-up applications of the same pattern.

### Why this matters for tests: the diff-coverage pitfall

Tests that use a plain `MagicMock()` for `conn` exercise only the "happy" branch of the nested `try/except` — `mock.rollback()` returns silently, so the inner `except Exception: pass` body is never executed. `diff-cover` counts those two lines (`except Exception:` and `pass`) as uncovered, which drops new-line diff coverage to ~50% and fails CI's 90% diff-coverage gate.

We hit this concretely in `#2385` / PR `#2389`: three new rollback handlers added three unexercised defensive branches (lines 1463-1464, 1496-1497, 1599-1600 in `worker.py`). The fix was not to remove the handlers or loosen the coverage gate — it was to add a dedicated test per handler that makes `rollback()` itself raise.

### Canonical test template

For every defensive rollback handler you add, add a paired test that exercises the inner branch:

```python
def test_defensive_rollback_does_not_crash(mock_conn, worker):
    """rollback() itself raising inside the defensive handler must not propagate."""
    # Make the primary DB write fail so we enter the handler at all.
    mock_conn.insert_validation_result.side_effect = Exception("insert failed")
    # Then make the defensive rollback fail too.
    mock_conn.rollback.side_effect = RuntimeError("connection closed")

    # The function under test must swallow both exceptions and not propagate.
    worker.process_event(event)

    # Optional: assert the rollback was attempted even though it failed.
    mock_conn.rollback.assert_called()
```

Two cautions when writing these tests:

- If the same mock connection is reused across a later path that calls `conn.rollback()` and *does* expect it to succeed, use a list for `side_effect` so only the first call raises: `mock_conn.rollback.side_effect = [RuntimeError("closed"), None]`.
- Don't assert on log output or re-raise — the whole point is that the defensive branch is silent. Assert on the post-handler behavior (e.g. function returns, OpenSearch not indexed, subsequent write still happens).

### Reference tests

Three existing tests cover this pattern and can be used as templates when adding a new rollback handler:

- `packages/scraper-framework/tests/test_deterministic_validation_integration.py`:
  - `test_det_validation_fail_rollback_error_does_not_crash`
  - `test_det_validation_flag_rollback_error_does_not_crash`
- `packages/scraper-framework/tests/test_ingestion_validation.py`:
  - `test_validation_fail_rollback_error_still_returns`

## `None` vs Empty Collection — The Falsy Sentinel Antipattern

When a function uses `None` as a sentinel to mean "operation failed" and an empty collection (`[]`, `{}`) to mean "operation succeeded but found nothing," testing the result with `if not result:` is a bug. Both `None` and `[]` are falsy in Python, so the check conflates failure with empty success.

### The rule

**Use `if result is None:` when `None` is a distinct sentinel value.** Use `if not result:` only when you genuinely want to treat `None` and empty the same way (e.g., early-return guards on input parameters).

### Case study: multimodal extraction fallback (#1590)

In the ingestion worker, multimodal PDF extraction returns `None` on failure and `[]` when extraction succeeds but the PDF contains no rulings (e.g., a boilerplate page). The fallback condition was:

```python
# Bug: treats [] (no rulings found) the same as None (extraction failed)
if not extracted_rulings:
    # Falls back to text-based extraction even when multimodal succeeded
```

The fix:

```python
# Correct: only fall back when extraction actually failed
if extracted_rulings is None:
    # Fall back to text-based extraction
```

Without this fix, a PDF with zero rulings (legitimate empty result from multimodal) would trigger unnecessary text-based fallback, wasting LLM tokens and potentially producing incorrect results from a less capable extraction path.

### Where this pattern appears in the codebase

Functions that return `T | None` where `None` means failure:

| Function | Returns | `None` means |
|---|---|---|
| `extract_fields_llm()` (`ingestion/llm_extract.py`) | `LLMExtractionResult \| None` | API call failed or text was empty — caller should fall back to regex |
| `_extract_chunk()` (`framework/llm_extractor.py`) | `ExtractionResult \| None` | Single chunk extraction failed — skip this chunk |
| `ruling_formatter` / `ruling_summarizer` LLM calls | `str \| None` | LLM call failed — skip formatting/summarizing |
| `CourtPortalClient.lookup_case()` (`framework/enrichment.py`) | `dict \| None` | Case not found or portal unavailable |

Callers of these functions must use `is None` checks, not truthiness checks.

### When `if not` is correct

`if not result:` is appropriate when:

- **Guarding input parameters** — `if not text:` to reject both `None` and `""` before processing.
- **The variable is always a collection** (never `None`) — e.g., a locally constructed list that is never assigned `None`.
- **You explicitly want to treat empty and absent the same** — e.g., `if not parties_data:` in `insert_parties()` where both `None` and `[]` mean "nothing to insert."

### Review checklist

When reviewing code that checks a return value with `if not`:

1. What is the return type of the function? If it's `T | None`, `if not` is likely wrong.
2. Does the caller need to distinguish "operation failed" from "operation succeeded with empty result"? If yes, use `is None`.
3. Is there a fallback or retry path? If `if not` triggers a fallback, an empty success will cause unnecessary (and potentially harmful) retries.

## Text Comparison

- **Always use `autojunk=False` with `difflib.SequenceMatcher` when comparing legal text.** The default `autojunk=True` marks frequently-repeated characters as "junk" to speed up matching on short strings. Legal documents are highly repetitive (standard phrases like "The motion for summary judgment is GRANTED." appear many times), causing the heuristic to treat common characters as junk. This produces wildly incorrect similarity scores — e.g. 0.19 instead of 0.99 for texts that differ only by whitespace. Discovered during #978 (ruling text formatter validation). Always pass `SequenceMatcher(None, a, b, autojunk=False)`.

## Node.js Charset Handling

Node.js `TextDecoder` only supports a limited set of charsets without full ICU data (`--with-intl=full-icu`). In CI and production, Node.js is typically built with the default "small ICU" configuration, which restricts `TextDecoder` to:

- **UTF-8** (and aliases like `utf8`)
- **UTF-16LE** / **UTF-16BE**
- **ISO-8859-1** (Latin-1)

Charsets like **Windows-1252** (CP1252) — the most common legacy charset in California court HTML documents — are **not available** in small-ICU builds. Code that works locally (where full ICU is often installed) will **silently fail or throw** in CI and production.

### The Windows-1252 problem

Windows-1252 and ISO-8859-1 (Latin-1) are identical for bytes 0x00-0x7F and 0xA0-0xFF, but differ in the 0x80-0x9F range. Latin-1 maps those bytes to C1 control characters (invisible/unprintable), while Windows-1252 maps them to commonly used printable characters:

- Smart quotes: `\u201c` `\u201d` `\u2018` `\u2019`
- Em dash: `\u2014`, en dash: `\u2013`
- Ellipsis: `\u2026`
- Euro sign: `\u20ac`
- Bullet: `\u2022`

Using `buffer.toString('latin1')` as a fallback is tempting but incorrect for Windows-1252 content — any bytes in the 0x80-0x9F range will be decoded to the wrong Unicode code points.

### Established pattern: manual byte mapping

The solution used in this codebase is a manual byte-by-byte mapping table for the 27 Windows-1252-specific characters. See `packages/api/src/rest/document-content.ts`:

- `WIN1252_MAP` — maps each 0x80-0x9F byte to its correct Unicode code point
- `transcodeToUtf8()` — uses the map for Windows-1252, falls back to `TextDecoder` for other charsets, then to `latin1` as a last resort
- `detectCharset()` — parses `<meta charset="...">` and `<meta http-equiv="Content-Type">` tags to discover the declared encoding

When adding charset transcoding in Node.js code, **always use this pattern** rather than `new TextDecoder('windows-1252')`. The TextDecoder approach will pass all local tests but break in CI/production.

### Python is not affected

Python's `codecs` module ships with Windows-1252 support by default (`'cp1252'` or `'windows-1252'`). The scraper framework's encoding module (`packages/scraper-framework/src/framework/encoding.py`) handles charset detection and decoding on the Python side without this limitation. This issue is **Node.js-specific**.

## LLM Extraction — Approach and Lessons

LLM-based extraction replaces fragile regex splitters for multi-case PDF transcription. The rollout plan is OC → Riverside → SB → LA → SF/SC/Ventura (#1467). Each county follows the same phased approach.

### The proven process

Each county goes through three phases, each in its own issue/PR:

1. **Eval phase** — Build a standalone eval script (`scripts/eval/eval_<county>_*.py`) that runs the LLM against all test fixtures and measures case count accuracy. Iterate on the prompt and join logic until **100% lenient accuracy** (exact or ±1 on every fixture). The eval IS the deliverable of this phase. Do not proceed until it passes.
2. **Integration phase** — Port the validated prompt and join logic from the eval script into the production `LlmExtractor`. Configure the county as `ExtractionMethod.LLM` in `extraction_config.py`. Remove the old regex splitter code.
3. **Backfill phase** — Re-ingest historical data through the improved pipeline and verify no regressions.

**Never skip the eval phase.** The iterative loop of "run eval → diagnose failures → adjust prompt → re-run" is what produces reliable prompts. A prompt that looks reasonable but hasn't been validated against every fixture will fail on edge cases.

### Prompt design: visual structure, not text heuristics

**Describe what a human would see, not what a regex would match.** Court PDFs have consistent visual structure — columns, ruled lines, entry numbers, indentation. A prompt that describes the visual layout is robust to the text variations that break regex.

**OC example (tabular PDFs with ruled lines):**

The OC prompt describes three columns by their visual position relative to vertical ruled lines, column widths, and row separators. It says nothing about case number formats, "continued" markers, or text patterns. Key elements:

- "THREE COLUMNS separated by TWO VERTICAL RULED LINES that run the full height of the page"
- Column 1 is "VERY NARROW... at the far left, LEFT of the first vertical line"
- Column 3 is "the WIDEST column, taking up most of the page width"
- "Most text on the page is in column 3. When in doubt about column membership, the text is in column 3."

**What NOT to do:**

- Don't tell the LLM to look for specific case number formats — formats vary by court division (probate uses standalone 8-digit numbers, North JC uses "vs" without case numbers)
- Don't use a "continued" flag for cross-page detection — Flash Lite can't reliably determine continuations. Use post-processing join logic instead
- Don't tell the LLM to extract structured fields (case_number, outcome, etc.) — that's the enrichment layer's job. The LLM's job is transcription and splitting

### Per-page processing

Send one page at a time to the LLM, not the entire PDF. This:

- Stays within output token limits (8192 for Flash Lite)
- Gives deterministic page-level results
- Makes failures diagnosable (you know exactly which page failed)
- Allows parallel page processing for latency

### Join logic: entry_number + case_info validation

The LLM returns per-page rows. Join logic merges them into complete rulings across pages. The key insight from OC: **a new case requires BOTH a valid entry number AND real case identification**.

```python
# A new case needs:
# 1. entry_number is a valid integer (column 1 has content)
# 2. case_info contains a case identifier (column 2 has content)
has_valid_entry = (
    bool(entry_num)
    and _is_valid_entry_number(entry_num)
    and looks_like_case
)
```

This two-signal approach filters out false positives where the LLM misattributes section headings (e.g., "2. Second Cause of Action") to column 1 — they have an entry number but no case_info.

**Case identification patterns** (for the `looks_like_case` check):

| Pattern | Regex | Example |
|---|---|---|
| Year-prefixed case number | `\d{2,4}-\d{5,8}` | `2024-01393434`, `30-2024-01420730` |
| Standalone case number (probate) | `\b\d{7,8}\b` | `01430606` |
| Adversarial case name | `\bvs?\.?\b` (case-insensitive) | `Smith vs Jones`, `Estate of Smith v. Jones` |

### Model selection

Use the cheapest model that achieves 100% accuracy. For OC, Gemini 2.5 Flash Lite ($0.075/M input, $0.30/M output) achieves 100% lenient accuracy — significantly cheaper than Claude Haiku or full Flash. Always eval at least two models and report cost per fixture.

### Eval script structure

Follow the pattern in `scripts/eval/eval_oc_multimodal.py`:

- **Ground truth from fixtures:** expected case counts in `tests/fixtures/expected/<fixture>.json`
- **Three accuracy buckets:** exact match, off-by-one (lenient), wrong (>1 off)
- **Per-fixture breakdown:** shows which fixtures fail and by how much
- **Cost tracking:** input/output tokens and estimated monthly cost per model
- **Retry logic:** 20s timeout per API call, 3 retries with exponential backoff for 503s
- **Multiple models:** eval the same fixtures across models to compare accuracy and cost

### Text-based vs multimodal extraction

Choose the extraction mode based on the PDF format:

| PDF format | Mode | LLM receives | Example counties |
|---|---|---|---|
| Tabular with ruled lines | Multimodal (page images) | PNG image per page | OC |
| Structured text (numbered entries) | Text-based | Extracted text via pdfplumber/pymupdf | Riverside |
| HTML (not PDF) | No LLM needed | BeautifulSoup parsing | LA |

For text-based PDFs, use `LlmExtractor.extract(text)`. For tabular PDFs, use `LlmExtractor.extract_from_pdf()` with page images. The eval script should match the production extraction mode.

### Common failure modes and fixes

| Symptom | Root cause | Fix |
|---|---|---|
| Over-counting cases | Section headings ("1. First Cause of Action") misattributed to column 1 | Require both entry_number AND case_info for new case detection |
| Under-counting cases | LLM not returning entry_numbers on many pages | Check if prompt clearly describes column 1; add case_info validation as second signal |
| Case number regex too narrow | Different divisions use different formats (probate: standalone 8-digit; North JC: "vs" only) | Add multiple case identifier patterns, don't assume one format |
| Wrong text assigned to cases | Regex splitter boundary calculation fails with duplicate case numbers | This is exactly why LLM extraction exists — replace the regex splitter |

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
