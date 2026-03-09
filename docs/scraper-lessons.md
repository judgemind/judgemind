# Scraper Development — Lessons Learned

Common issues found during audits and fixes. Consult this when writing or reviewing scrapers.

## Regex Patterns

- **Always use `re.IGNORECASE`** for date, name, and keyword patterns. PDF text extraction produces inconsistent casing — "FEBRUARY", "February", "february" all appear. OC civil and family law scrapers shipped without IGNORECASE on hearing date patterns and missed ~37% of dates.
- **Use `re.MULTILINE`** when anchoring with `^` or `$` in multi-line PDF text.
- **Normalize whitespace before matching.** PDFs often have extra spaces, line breaks mid-word, or non-breaking spaces (`\xa0`). Use `" ".join(text.split())` on extracted groups.

## Field Extraction

- **The ingestion worker gates ruling rows on `hearing_date`.** If a scraper doesn't extract `hearing_date`, no ruling row is created — which means judge, outcome, motion_type are all lost too. Hearing date extraction is the single most important field.
- **Always implement fallback extraction.** The narrow scraper-specific pattern should be tried first, then fall back to `extract.py` centralized regex. Example: LA scraper's `_JUDGE_DIV_RE` is very narrow; adding a fallback to `extract_judge_name()` from `extract.py` improved coverage from 27% to much higher.
- **Extract all 6 required fields:** judge name, motion type, case title, hearing date, outcome, parties. Don't ship a scraper that leaves extractable fields empty — backfills are unreliable.

## PDF-Specific Issues

- **PDF text extraction order varies.** `pdfplumber` extracts text in visual order, which may not match reading order for multi-column layouts.
- **San Bernardino extracts hearing date from filename, not content.** This is fragile — if the filename format changes, all dates are lost. Prefer content-based extraction with filename as fallback.
- **Multi-ruling PDFs (Riverside):** When splitting a PDF into individual rulings, ensure metadata (hearing_date, judge_name) from the parent document propagates to all children.

## Ingestion Pipeline

- **`insert_document` and `insert_ruling` use upsert semantics** (ON CONFLICT DO UPDATE). Re-ingesting a document updates mutable fields (hearing_date, judge_id, outcome, etc.) while preserving immutable fields (s3_key, content_hash, captured_at).
- **`extract.py` provides centralized fallback extraction** for: case_number, judge_name, motion_type, outcome, case_title, hearing_date. The ingestion worker uses these when scrapers don't populate fields. When adding a new extraction pattern, add it to `extract.py` so all courts benefit.
- **The worker has fallback chains for critical fields.** If the scraper event doesn't include hearing_date, case_number, or case_title, the worker tries `extract_hearing_date()`, `extract_case_number()`, and `extract_case_title()` from ruling text before giving up. This is a safety net, not a substitute for proper scraper extraction.
- **Party extraction is the hardest field.** Only LA implements it (structured HTML with role labels). PDF-based courts don't have reliable party structure. This remains an open problem.

## Testing

- **Every scraper needs regression tests against real fixtures.** Save actual PDFs/HTML to `tests/fixtures/` and test field extraction against them.
- **Test edge cases explicitly:** empty PDFs, PDFs with no rulings, unusual date formats, missing fields.
- **Run the full test suite before pushing** — `695 tests` across all scrapers as of 2026-03-08.
