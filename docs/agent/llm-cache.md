# LLM Extraction Cache

Agent-facing reference for the S3-backed LLM extraction cache used by `LlmExtractor` in `packages/scraper-framework/src/framework/llm_extractor.py`. Covers what is stored, which filters re-run on cache hit, when to bust the cache, and when surgical DB cleanup is appropriate.

## What the LLM cache stores

The document-level cache entry stores **post-filter `ExtractedRuling[]` JSON** — not the raw model response and not per-page row lists. (The multimodal PDF path also keeps per-page raw responses; see §Per-page entries below.) The value written is what `_join_page_rows` + post-processing returns: fully joined, filtered `ExtractedRuling` objects.

**S3 key format:**

```
llm-cache/{provider}-{model}/prompt-{prompt_hash}/{content_hash}.json
```

- `{provider}-{model}` — e.g. `google-gemini-1.5-flash-8b` or `anthropic-claude-haiku-4-5-20251001`. Changes automatically when the configured model changes.
- `prompt-{prompt_hash}` — SHA-256 of the system prompt text. Any edit to the prompt text produces a new hash, making old cache entries unreachable without `--bust-llm-cache`.
- `{content_hash}` — SHA-256 of the raw document content (PDF bytes or text) **plus** any scraper-provided metadata (`judge_name`, `department`, `hearing_date`). Metadata is included because the LLM output may differ when metadata changes even for identical document content.

Local development reads are served from `S3_CACHE_DIR` via `CachedS3Client` (fast disk reads). On ECS, reads and writes go directly to S3. The cache is shared across environments.

### Per-page entries (multimodal PDF path, #4716)

`extract_from_pdf` sends one page image per LLM call. Alongside the document-level entry above, it caches each page that succeeded:

```
llm-cache/{provider}-{model}/prompt-{prompt_hash}/pages/{page_hash}.json   →   {"raw_text": "<LLM response>"}
```

- `{page_hash}` — SHA-256 of the rendered page PNG bytes plus the same scraper metadata as the document key. The per-page user message depends on that metadata.
- The **raw LLM response** is stored, not parsed rows. A page-cache hit re-runs `_parse_page_rows`, so parser fixes apply to page-cached pages without a bust. If the stored text no longer parses, the entry is treated as a miss.
- Page entries are read only when the document-level entry misses. `--bust-llm-cache` skips page reads too, but pages are still written.

### What gets cached and what does not (PDF path)

`_extract_single_page` returns a status for each page:

| Page status | Meaning | Page entry written? | Blocks document entry? |
|---|---|---|---|
| `ok` | API call succeeded and the response parsed as a JSON object/array. **Zero rows is still `ok`**: boilerplate/header-only pages are a valid result. | Yes | No |
| `parse_error` | Response did not parse, even after one retry with `PAGE_JSON_RETRY_NUDGE` appended to the user message. | No | Yes |
| `api_error` | API call failed after `max_retries`. | No | Yes |

The document-level entry is written only when **every** page is `ok` and the join produced at least one ruling. A document with a failed page is never cached as complete, because that would permanently serve a partial ruling set (#3517). Its `ok` pages are page-cached, so the next run re-sends only the failed pages. Failures are never cached as "empty".

Each PDF extraction logs `llm_extractor.pdf_pages_summary` with `total_pages`, `page_cache_hits`, `llm_pages` and `failed_pages`. Use it to see why a document missed the document-level cache. `llm_extractor.page_parse_retry` and `llm_extractor.page_parse_exhausted` record the JSON retry.

## What re-runs on cache hit and what does NOT

Since #2513, cache hits are **not a raw replay** of the stored value. Two helpers re-apply the post-processing filters that operate purely on the final `ExtractedRuling[]` list before returning to the caller:

- **PDF path** (`_apply_pdf_cache_hit_filters`): re-applies `_drop_calendar_listing_rulings`, `_drop_short_unsubstantive_rulings`, `_truncate_concatenated_case_titles`, `_deduplicate_ruling_texts`, `_filter_citation_artifacts`.
- **Text/HTML path** (`_apply_text_cache_hit_filters`): re-applies `_filter_citation_artifacts`, `_truncate_concatenated_case_titles`, `_deduplicate_ruling_texts`, and the county sanitizers `_sanitize_riverside_rulings` + `_sanitize_san_bernardino_rulings`.

These filters are **idempotent**: if the cached rulings already satisfy the current filter logic, re-applying them is a no-op. If the filter logic was updated after the cache entry was written, the new logic takes effect on cache read — without requiring an expensive reingest.

**Filters that are NOT re-applied on cache hit:**

- `_resolve_cross_references` — requires per-page row state (entry numbers) that is discarded once `ExtractedRuling` objects are produced.
- `_propagate_document_fields` — requires the pre-join intermediate rows; not available from the stored ruling list.
- Any filter that inspects per-page rows or entry numbers.

**Consequence:** changes to `_resolve_cross_references`, `_propagate_document_fields`, or any pre-join filter do NOT take effect from the cache. Existing cached entries will continue to produce the old behavior for those operations. `--bust-llm-cache` is required to see the corrected output for already-cached documents.

## When `--bust-llm-cache` is required

Pass `--bust-llm-cache` to `scripts/reingest_from_s3.py` (#2424) when any of the following change:

| Change | Why bust is required |
|---|---|
| Prompt text (`EXTRACTION_SYSTEM_PROMPT` or county-specific prompts) | The prompt hash changes, so old entries are unreachable — but only for new documents. Existing documents need bust to get new LLM output. |
| LLM provider or model | The `{provider}-{model}` prefix changes — same issue. |
| Logic inside `_extract_chunk_with_retry` or `_parse_page_rows` | These run before the document-level cache write, and that entry stores their output. (Per-page entries store the raw response and are re-parsed on hit, but they are only consulted when the document-level entry misses.) |
| Any pre-join filter (`_resolve_cross_references`, `_propagate_document_fields`, or other functions needing per-page row state) | These are not re-applied on cache hit (see above). |

**Cache writes always happen** even when `--bust-llm-cache` is set, so subsequent runs without the flag benefit from the fresh results immediately.

**Cost warning:** A full OC reingest with `--bust-llm-cache` costs approximately 30 hours of Gemini/Anthropic API time. Prefer the cache-hit filter path (§ above) when the change only affects post-join filters. Only bust the cache when the change cannot be applied at cache-hit time.

Example invocation:

```
scripts/ecs-run-task.sh scripts/reingest_from_s3.py -- --county orange --bust-llm-cache
```

**Partial-failure exit gate (#4624).** For `--bust-llm-cache` **prefix**
reingests, the script fails loud by default — it exits non-zero when more than
10% of keys error, so a run that silently lost data (the #3855 OOM failure
mode) surfaces as a non-zero exit instead of a false success. You do not need
to pass anything to get this; the gate is the default for the cache-bust prefix
path:

```
scripts/ecs-run-task.sh scripts/reingest_from_s3.py -- --prefix orange/ --bust-llm-cache
```

To override the threshold pass `--max-error-ratio 0.05` (your value always
wins). To opt out of the gate entirely for a run where partial failure is
expected, pass `--no-fail-on-errors`. Standard (DB-row) mode and non-cache-bust
prefix runs keep the pre-#4619 opt-in behavior — pass `--max-error-ratio`
explicitly there if you want the gate. See #4619 and #4624.

## When surgical DB cleanup is appropriate

The default cleanup path for `derived.*` data is always `rebuild_db.py --county <name>` per CLAUDE.md. Prefer a rebuild when the bad data exists in S3 (i.e., the raw document is fine and the ingestion pipeline will produce correct output on replay).

Surgical DB cleanup scripts are appropriate when:

1. **The raw S3 data itself contains the bad value** — the rebuild path would re-ingest the same bad result, so a targeted predicate-based correction in the DB is cheaper and correct.
2. **The rebuild is disproportionately expensive** for the scope of the fix — a targeted DELETE or UPDATE against a small, well-scoped predicate is cheaper than a full county rebuild affecting millions of rows.

**Templates:** see `scripts/archive/cleanup_*.py` — e.g. `scripts/archive/cleanup_oc_data.py` and `scripts/archive/cleanup_riverside_unknown.py`. These scripts mirror the filter predicates being fixed against `derived.rulings` and `derived.cases`, run via `scripts/ecs-run-task.sh`, and include a `--dry-run` mode. Model new cleanup scripts on this pattern.

> **Note:** Earlier issue bodies reference `scripts/delete_oc_calendar_listings.py` as a template. That file does not exist. Use the `scripts/archive/cleanup_*.py` scripts listed above instead.

Surgical scripts live in `scripts/archive/` and must carry a `# one-off: true` header marker. They are candidates for archival once their work is confirmed complete.
