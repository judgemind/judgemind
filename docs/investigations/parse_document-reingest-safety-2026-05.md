# Audit: `parse_document` reingest safety across all scrapers

**Date:** 2026-05-04
**Issue:** [#4046](https://github.com/judgemind/judgemind/issues/4046)
**Trigger:** Retrospective on [#3986](https://github.com/judgemind/judgemind/issues/3986),
where `CourtListener.parse_document` returned a fresh `CapturedDocument`
unchanged on the reingest path. The JSON envelope was decoded as text and
stored as `ruling_text`, hitting the 50000-char truncation cap that
`packages/scraper-framework/src/validation/deterministic.py:40`
(`_TRUNCATION_SENTINEL_LENGTH`) was added to detect.

The validation layer caught the bug *after* it shipped — by chance, on a
spotcheck. A reusable preventative check is to **find any scraper whose
`parse_document` does not populate fields from `raw_content`** — those are
all candidates for the same bug if/when the reingest path ever feeds them
a fresh `CapturedDocument` carrying only `raw_content`.

## How `_reparse_document` actually consumes `parse_document`

Scope of the audit hinges on what the reingest path does with
`parse_document` output, so the contract is restated here.
`scripts/reingest_from_s3.py::_reparse_document` (lines 851–977):

1. Loads `raw_content` bytes from S3 and runs
   `_extract_text_from_content(raw_content, doc_format)` — pdfplumber
   subprocess for PDFs (with OCR fallback), UTF-8 decode for HTML/text.
2. Seeds `extracted` from `doc_meta` (the DB row) for `case_number`,
   `case_title`, `case_type`, `hearing_date`, plus `text` for
   `ruling_text`. `judge_name`, `outcome`, `motion_type`, `department`,
   `parties` start as `None` / `[]`.
3. Builds a fresh `CapturedDocument` with only `raw_content` and
   identifiers populated (notably `case_number` from `doc_meta` is set
   before `parse_document` so multi-case calendar pages can narrow —
   #2311). Other structured fields are *not* seeded onto `cap_doc`.
4. Calls `scraper.parse_document(cap_doc)`.
5. Merges `parsed` into `extracted`:
   - `ruling_text`: `parsed.ruling_text or text` (so `text` is the safety net).
   - `case_number`, `case_title`: `parsed.X or extracted["X"]` (DB-seeded fallback).
   - `judge_name`, `department`, `parties`: **unconditional overwrite**
     from `parsed.X` (so a no-op `parse_document` clobbers any potential
     downstream-LLM result with `None`/`[]`).
   - `outcome`, `motion_type`: normalized from `parsed.X` if non-None,
     else `None`.
   - `hearing_date`: `parsed.hearing_date` if set, else DB seed.
6. After the scraper call, `_apply_regex_fallbacks` and (if enabled) LLM
   extraction can repopulate fields that are still `None`.

The asymmetry matters: a Live-only `parse_document` that returns `doc`
unchanged unconditionally clears `judge_name`, `department`, and
`parties` even when those fields are present in `doc_meta`. The
downstream LLM extraction rescues this for most CA scrapers, but
**reingest with LLM disabled (or LLM fail) is unsafe** for any Live-only
scraper.

## Classification axes

| Class | Behavior | Reingest safety |
|---|---|---|
| **Live-only** | `parse_document` is a no-op (returns `doc` unchanged) or only refines a field already set by `_map_to_document`. | UNSAFE for reingest — `parse_document` does not re-derive structured fields from `raw_content`. |
| **Reingest-aware** | Reads `doc.raw_content` (HTML / PDF / JSON) and populates fields. | Safe — `parse_document` is the single source of truth. |
| **Mixed** | Branches on a discriminant such as `doc.extra["pre_split"]` or `doc.extra["_llm_extracted"]`. | Safe in the regex/parse branch (matches Reingest-aware); the pre-split branch is only reachable via the live-capture path so it never runs on reingest (the reingest cap_doc has `extra={}`). |

A note on **20 vs 21**: the issue body said "21 `parse_document`
implementations." The actual count is 20 — `grep -rn 'def parse_document'
packages/scraper-framework/src/courts/` returns 20 lines (la_tentatives.py
has two: civil and appellate; the rest are one-per-file). One of the 20
lives in `pdf_link_scraper.py`, which is a shared base class consumed by
4 OC/SF/SB scrapers. This audit lists all 20.

## Per-implementation classification

| File | Class | `raw_content` shape | Reingest exposure | Notes |
|---|---|---|---|---|
| `ca/cc_tentatives_portal.py:549` | **Live-only (no-op)** | PDF bytes | Yes (via `_reparse_document`) | Returns `doc` unchanged. The PDF text would survive `_extract_text_from_content`, but `case_number`, `case_title`, `hearing_date`, `motion_type`, `judge_name`, `department`, `courthouse`, and `ruling_text_html` are populated only in `_fetch_single_ruling` — none come from `raw_content`. **High-risk: same shape as #3986.** Existing docstring already names the pattern but doesn't call out the reingest hazard. |
| `ca/cc_tentatives.py:725` | **Mixed** | PDF bytes | Yes — Reingest-aware path | `doc.extra["pre_split"]/["_llm_extracted"]` short-circuit is unreachable on reingest (cap_doc has `extra={}`). The else branch calls `_extract_pdf_text(doc.raw_content)`, populates `ruling_text`, `judge_name`, `case_number`, `case_title`, `motion_type`, `outcome` from PDF text. Safe. |
| `ca/fresno_tentatives.py:614` | **Mixed** | PDF bytes | Yes — Reingest-aware path | `doc.extra["pre_split"]` short-circuit unreachable on reingest. Else branch calls `super().parse_document(doc)` (PdfLinkScraper, reingest-aware) plus Fresno-specific fallbacks. Safe. |
| `ca/governor_appointments.py:418` | **Reingest-aware** | HTML bytes | Yes | `doc.raw_content.decode("utf-8")` → `parse_appointees(html)`. Safe. |
| `ca/la_tentatives.py:1131` (civil) | **Mixed** | HTML bytes | Yes — Reingest-aware path | `_llm_extracted` short-circuit unreachable on reingest. Else branch parses `BeautifulSoup(doc.raw_content, "lxml")` → `_extract_ruling_fields`. Plus department-to-judge fallback. Safe. |
| `ca/la_tentatives.py:1269` (appellate) | **Mixed** | HTML bytes | Yes — Reingest-aware path | Same shape as civil. Safe. |
| `ca/oc_family_law_tentatives.py:208` | **Reingest-aware** (via super) | PDF bytes | Yes | Calls `super().parse_document(doc)` (PdfLinkScraper) then OC FL specifics from extracted text. Safe. |
| `ca/oc_probate_tentatives.py:186` | **Reingest-aware** (via super) | PDF bytes | Yes | Calls `super().parse_document(doc)` then probate specifics from extracted text. Safe. |
| `ca/oc_tentatives.py:162` | **Live-only (no-op, by-design)** | PDF bytes | Yes | Explicit no-op — docstring states "field extraction is handled by the multimodal LLM pipeline." `_extract_text_from_content` recovers PDF text into `extracted["ruling_text"]`; `judge_name`/`department` come from `_fetch_one_pdf` link-text parsing at capture and are NOT recoverable on reingest. Downstream LLM is the only thing that fills these. **Documented but reingest-fragile.** |
| `ca/pdf_link_scraper.py:213` (base) | **Reingest-aware** | PDF bytes | Yes (via subclasses) | Base `parse_document` calls `_extract_pdf_text(doc.raw_content)`, populates `ruling_text` and `case_number` (via `case_number_re`). Subclasses extend via `super().parse_document(doc)`. Safe at base layer. |
| `ca/riverside_tentatives.py:240` | **Reingest-aware** (via super) | PDF bytes | Yes | `super().parse_document(doc)` + hearing-date / judge-name fallbacks. Safe. |
| `ca/sb_tentatives.py:144` | **Reingest-aware** (via super) | PDF bytes | Yes | `super().parse_document(doc)` + judge / hearing date fallbacks (with explicit "covers reingest where link_text is unavailable" comment at line 169). Safe — and already documented for the reingest case. |
| `ca/sc_tentatives.py:587` | **Reingest-aware** | PDF bytes | Yes | `extract_pdf_text(doc.raw_content)`, populates `ruling_text`, `case_number`, `hearing_date`, `judge_name`, `department`, `case_title`, `motion_type`, `outcome` from PDF text. Safe. |
| `ca/sd_calendar.py:578` | **Reingest-aware** | HTML bytes | Yes — primary path | Docstring explicitly names "during reingest" as the main use case. `doc.raw_content.decode("utf-8")` → `parse_calendar_page(html)` matched by `doc.case_number`. Safe and explicitly documented. |
| `ca/sd_pipeline.py:188` | **Reingest-aware** (delegating) | HTML bytes | Yes | Delegates to `SDTentativeRulingsScraper.parse_document` (sd_tentatives.py). Safe. |
| `ca/sd_tentatives.py:843` | **Reingest-aware** | HTML bytes | Yes | `doc.raw_content.decode("utf-8")` → `parse_roa_page(html)`. LLM supplements outcome/motion_type when enabled. Safe. |
| `ca/sf_civil_tentatives.py:1153` | **Live-only (no-op)** | HTML bytes (per-ruling AJAX response text) | Yes (via `_reparse_document`) | Returns `doc` unchanged. `raw_content` is the per-ruling HTML text from the AJAX API, so `_extract_text_from_content` UTF-8-decodes it into `extracted["ruling_text"]` (cf. CourtListener: there `raw_content` was a JSON envelope, so the decode produced a JSON string — that was the #3986 bug). **No truncation hazard like #3986** because the body is HTML text rather than a JSON envelope. **But:** `case_number`, `case_title`, `hearing_date`, `motion_type`, `judge_name`, `department`, `parties`, `ruling_text_html`, `outcome` all populated only in `_ruling_to_document` from the AJAX response's structured fields. Reingest can recover `case_number/case_title/hearing_date` from `doc_meta`; `judge_name`/`department`/`parties` are unconditionally cleared (see "How `_reparse_document` consumes" §5 above). Existing docstring is misleading — says fields are "pre-populated during fetch" but doesn't mention reingest. **Same shape as #3986 minus the JSON-envelope-as-ruling_text symptom.** |
| `ca/sf_tentatives.py:214` | **Reingest-aware** (via super) | PDF bytes | Yes | `super().parse_document(doc)` + judge / hearing-date / case-title from PDF text. Safe. |
| `ca/ventura_tentatives.py:518` | **Reingest-aware** | PDF or HTML bytes | Yes | Branches on `doc.content_format`; both branches call `_extract_pdf_text`/`_extract_html_text` and populate fields. Safe. |
| `federal/courtlistener.py:513` | **Reingest-aware** (post-#3986) | JSON envelope bytes | Yes — primary path | Post-fix: `parse_document` calls `_populate_from_envelope` for both live-capture and reingest. Single source of truth. Safe. |

## Summary by class

- **Reingest-aware (or Mixed → Reingest-aware in the reingest branch):** 17 implementations. All safe.
- **Live-only (no-op):** 3 implementations:
  1. `ca/cc_tentatives_portal.py:549`
  2. `ca/oc_tentatives.py:162` (intentional — LLM-handled, already documented)
  3. `ca/sf_civil_tentatives.py:1153`

## Live-only triage

### `ca/cc_tentatives_portal.py:549` — UNSAFE for reingest

- `raw_content` is the PDF bytes — text would round-trip via pdfplumber.
- But `case_number/case_title/hearing_date/motion_type/judge_name/department/courthouse/ruling_text_html` come exclusively from `_fetch_single_ruling`'s parsing of the Drupal listing-row HTML and the detail-page HTML.
- `ruling_text_html` (the styled HTML) is in `doc.extra["detail_html"]` at capture time but is NOT in `doc.raw_content`; reingest cannot recover it.
- Reingest exposure: scraper is in production. There is currently no
  blocker preventing `_reparse_document` from running it. If/when this
  scraper's documents are reingested in a batch, the result would be
  rulings with PDF-extracted text but missing every other structured
  field (modulo `doc_meta` seeds for `case_number/case_title/hearing_date`).
- **Action:** filed follow-up issue (see below) to refactor along the #3986 shape — extract a `_populate_from_pdf_or_listing_envelope` helper so `parse_document` can populate fields from PDF text at minimum, and document that `ruling_text_html` is unrecoverable on reingest (or change the archive shape to include detail HTML).

### `ca/sf_civil_tentatives.py:1153` — UNSAFE for reingest (no-op clobber)

- `raw_content` is the per-ruling HTML text — `ruling_text` would round-trip via UTF-8 decode.
- Structured fields (`case_number/case_title/hearing_date/motion_type/judge_name/department/parties/ruling_text_html/outcome`) come from the AJAX API's structured response, parsed in `_ruling_to_document`. None of those structured fields survive in `raw_content`.
- The no-op `parse_document` returns `doc` unchanged, so `_reparse_document` clears `judge_name/department/parties` to `None`/`[]` (see §5 above). DB-seeded `case_number/case_title/hearing_date` survive; everything else is gone.
- **No truncation-cap hazard** like #3986 because the body is HTML rather than a JSON envelope, but the structural-field loss is the same shape.
- **Action:** filed follow-up issue (see below) to either (a) refactor `_ruling_to_document` so that the AJAX response is archived as a JSON envelope into `raw_content` and `parse_document` re-populates from it (mirrors CourtListener's #3986 fix), or (b) explicitly document that this scraper is not reingest-safe and add a runtime guard that refuses to reingest documents from `scraper_id="ca-sf-tentatives-civil"` until the refactor lands.

### `ca/oc_tentatives.py:162` — Documented as LLM-handled

- Explicit no-op by-design — docstring already states "field extraction is handled by the multimodal LLM pipeline."
- Reingest path: pdfplumber recovers `ruling_text` from `raw_content`, then downstream LLM extraction populates the rest.
- **No code change needed**, but the docstring should also call out
  the implication: "this method is a no-op on the reingest path; reingest
  relies on downstream LLM enrichment to populate `case_number`,
  `judge_name`, etc. — running reingest with LLM disabled will leave
  those fields empty."

## Stale docstrings (B.1.5 sweep) — UPDATED in this PR

Source-file docstrings that contradicted the reality this audit
established. **All three were updated in the same PR as this audit doc**
(satisfies AC#2: "Each Live-only scraper's `parse_document` docstring is
updated with a note saying ... reingest of S3-archived bytes for this
scraper is NOT supported and would result in empty/raw-text fields"):

1. **`ca/cc_tentatives_portal.py::parse_document`** — was "No-op — all
   fields are populated during fetch_documents." Now explicitly names
   the reingest hazard and links to follow-up #4133.

2. **`ca/sf_civil_tentatives.py::parse_document`** — was "most fields
   are already populated in fetch_documents..." Now explicitly names
   the reingest hazard and links to follow-up #4134.

3. **`ca/oc_tentatives.py::parse_document`** — already correctly named
   the LLM design intent. Updated to also mention the LLM-disabled
   reingest fallback behavior; links to follow-up #4135 (the broader
   docstring-clarification ticket).

The structural refactors for #1 and #2 (extracting
`_populate_from_envelope` helpers along the #3986 shape) are tracked in
follow-ups #4133 / #4134 — they require an archive-shape decision and
are a larger change than this audit's scope.

## Follow-up issues filed

- [#4133](https://github.com/judgemind/judgemind/issues/4133) — Refactor `cc_tentatives_portal.py::parse_document` along the #3986 shape, plus update the docstring. (priority/p2, type/bug)
- [#4134](https://github.com/judgemind/judgemind/issues/4134) — Refactor `sf_civil_tentatives.py::parse_document` along the #3986 shape, plus update the docstring. (priority/p2, type/bug)
- [#4135](https://github.com/judgemind/judgemind/issues/4135) — One-line docstring tweak in `oc_tentatives.py::parse_document` to explicitly call out the LLM-disabled reingest behavior. (priority/p3, type/chore)

## Out of scope (explicit)

- The `_TRUNCATION_SENTINEL_LENGTH` validator stays in place — this audit
  doesn't replace runtime validation, it complements it.
- New scrapers (`packages/scraper-framework/src/courts/{ca,federal,...}/<new>.py`)
  are covered by their own implementation review; this issue is one-time
  backfill of existing scrapers.
- The `tx/` subpackage currently contains only an empty `__init__.py` —
  no scrapers, nothing to audit.
