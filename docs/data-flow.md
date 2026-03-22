# Judgemind Data Flow

End-to-end pipeline from court website to user query. For the full architecture specification, see `docs/specs/architecture-spec-v1.md`.

## Pipeline Overview

```
Court Website
    |
    v
[Scraper] ---> S3 (raw archive, immutable)
    |
    v
Redis Stream: "document.captured"
    |
    v
[Ingestion Worker] ---> PostgreSQL (structured data)
    |                |-> OpenSearch (full-text index)
    |
    v
[API Server] <--- GraphQL <--- [Web Frontend]
```

## Stage 1: Scraping (scraper-framework)

Court-specific scrapers (`packages/scraper-framework/src/courts/`) fetch pages from court websites, extract structured fields, and emit events.

**What happens:**
1. Scraper fetches page (HTTP/Playwright) from the court website.
2. `parse_document()` extracts structured fields from raw HTML/PDF: case number, judge name, hearing date, ruling text, outcome, motion type, parties.
3. Raw content is hashed (SHA-256) for deduplication and archived to S3.
4. A `DocumentCapturedEvent` is emitted to the `document.captured` Redis Stream.
5. A `ScraperHealthEvent` is emitted to the `scraper.health` Redis Stream.

**Redis stream:** `document.captured`

**Event payload (key fields):**
- `document_id`, `scraper_id`, `correlation_id`
- `state`, `county`, `court`, `source_url`
- `content_format` (html, pdf, docx, text), `content_hash`
- `s3_key`, `s3_bucket` (archive location)
- Scraper-extracted fields: `case_number`, `case_title`, `judge_name`, `hearing_date`, `ruling_text`, `outcome`, `motion_type`, `parties`, `department`, `courthouse`

## Stage 2: Ingestion (scraper-framework ingestion worker)

The ingestion worker (`packages/scraper-framework/src/ingestion/worker.py`) is a long-lived Redis Streams consumer running on ECS Fargate. It reads `document.captured` events and writes structured data to PostgreSQL and OpenSearch.

**Three-tier field extraction:**

Each field is populated using the first tier that provides a value:

| Tier | Source | Priority | Implementation |
|------|--------|----------|----------------|
| 1 | Scraper-provided fields | Highest | Fields from the `DocumentCapturedEvent` payload |
| 2 | LLM extraction | Medium | `ingestion/llm_extract.py` -- sends document text to Claude/Gemini |
| 3 | Regex fallback | Lowest | `ingestion/extract.py` -- court-specific regex patterns |

**Required fields at the ruling level:**
- `hearing_date` (gates ruling row creation -- no date means no ruling is stored)
- `case_number` (used for case upsert and deduplication)
- `judge_name` (resolved to canonical `judges` record via entity resolution)
- `outcome` (classified into `ruling_outcome` enum)
- `motion_type` (free-text, used for judge analytics)
- `case_title` (e.g., "Smith v. Jones")
- `parties` (extracted names and roles, written to `case_parties`)

**Multi-ruling documents:** Documents containing multiple cases (e.g. calendar PDFs) are split into individual ruling events by the framework-level `LlmExtractor` before per-field extraction. Each split ruling is re-injected with all fields pre-populated, skipping Tiers 2 and 3. Deterministic UUID5 IDs (`ingestion/split_ids.py`) ensure idempotent re-processing.

**Extraction logging:** The worker tracks which tier populated each field in an `extraction_methods` dict (`"scraper"`, `"llm"`, `"regex"`), enabling monitoring of extraction quality per court.

**Database writes (PostgreSQL):**

| Table | What's written | Key columns |
|-------|---------------|-------------|
| `courts` | Upserted from event metadata | `state`, `county`, `court_name`, `court_code` |
| `judges` | Resolved via alias matching | `canonical_name`, `court_id`, `department` |
| `cases` | Upserted by `(court_id, case_number)` | `case_number`, `case_title`, `case_type` |
| `documents` | Upserted by `content_hash` | `s3_key`, `format`, `scraper_id`, `hearing_date` |
| `rulings` | One row per ruling per hearing date | `ruling_text`, `outcome`, `motion_type`, `judge_id` |
| `case_parties` | Batch upserted from parsed parties | `party_id`, `role` |
| `case_judges` | Links judge to case | `judge_id`, `case_id` |

**OpenSearch index:** `tentative_rulings` -- full-text indexed for search. Fields include ruling text, case number, judge name, hearing date, outcome, motion type, county, and court.

## Stage 3: API (api)

The API server (`packages/api/`) queries PostgreSQL and OpenSearch to serve data to the frontend.

**GraphQL API** (`/graphql`):
- **Queries:** courts, judges (with analytics -- grant/deny rates by motion type), cases, rulings, search (full-text via OpenSearch), data quality metrics, alerts
- **Mutations:** auth (register, login, token refresh), alert subscriptions (create, update, delete)
- **DataLoader:** batched loading for judges, cases, courts to avoid N+1 queries

**REST API:**
- `GET /api/documents/:id/download` -- pre-signed S3 URL for original document download

**Data sources:**
- PostgreSQL for all structured queries (rulings, judges, cases, analytics aggregations)
- OpenSearch for full-text ruling search with faceted filtering
- S3 for document download (pre-signed URLs)
- Redis for caching

## Stage 4: Frontend (web)

The Next.js app (`packages/web/`) is a GraphQL client. It server-renders pages for SEO and provides client-side navigation.

**Key pages:**
- Rulings feed -- browse and filter tentative rulings by county, judge, date, outcome
- Judge profiles -- analytics dashboard with grant/deny rates, motion breakdowns
- Case detail -- case info, associated rulings, parties, documents
- Search -- full-text search across rulings with filters
- Admin -- data quality dashboard, scraper health overview

## NLP Pipeline (nlp-pipeline) -- Current State

The `nlp-pipeline` package (`packages/nlp-pipeline/`) contains modules for classification, entity extraction, summarization, embedding generation, and version diffing. Currently:

- **Integrated:** Document classification and entity extraction logic are defined but the production field extraction runs inline in the `scraper-framework` ingestion worker (Tier 2 LLM extraction).
- **Planned:** The pipeline will consume `document.validated` events from Redis Streams and run as independent processing stages. This will add:
  - Document summarization (cached in `rulings.summary`)
  - Vector embedding generation (stored in Qdrant for semantic search)
  - Version classification for revised documents (substantive vs. cosmetic)

## Telegram Bridge (telegram-bridge) -- Operational Tooling

The `telegram-bridge` package is not part of the data pipeline. It provides bidirectional communication between dispatcher agents and the maintainer via Telegram for operational notifications and commands. Fully opt-in.

## Reingestion

Historical documents can be reprocessed through the full three-tier extraction pipeline using `scripts/reingest_from_s3.py`. This reads archived documents from S3, reconstructs ingestion events, and pushes them through the same extraction pipeline. Used after extraction logic improvements or to backfill fields for documents ingested before LLM extraction was available.
