JUDGEMIND

Architecture Specification v1.0

March 2026

Companion to: Judgemind Product Specification v1.0

AI-implemented • Human-reviewed • Open source

# 1. Architecture Principles

The following principles govern all architectural decisions in Judgemind. They are listed in priority order; when principles conflict, higher-ranked principles prevail.

API-first. The web application is a client of the API, not the other way around. Every capability exposed in the UI is available programmatically. The public REST API and the internal GraphQL API share the same data access layer.

Cost-aware by default. Judgemind is self-funded and free to users. Every component must be designed with cost ceilings in mind. Prefer fixed-cost infrastructure over usage-based pricing where possible. Never assume unlimited budget.

Data capture is irreversible priority. Tentative rulings and other ephemeral court data disappear permanently if not captured. The ingestion pipeline is the single most critical system. Downtime in the web UI is tolerable; downtime in scraping is data loss.

Transparency over polish. Every AI output is labeled. Every analytic shows its sample size. Every data gap is disclosed. Trust is the product.

Open source. The codebase is open source. Architecture should favor open-source and commodity components where practical, but managed AWS services are the primary deployment target. If a proprietary service is clearly the best tool for a job, use it. Self-hosted deployment is not a design constraint.

# 2. System Overview

Judgemind consists of five major subsystems connected by event-driven messaging. Each subsystem can be scaled, deployed, and developed independently.

## 2.1 Event-Driven Architecture

The five subsystems communicate through an event bus implemented with Redis Streams. Redis is already in the stack for caching and rate limiting (see Section 2.2), so using Redis Streams for messaging avoids adding another service. The event volume (thousands of documents per day, not millions) is well within Redis Streams’ capabilities.

### 2.1.1 Event Flow

Data flows through the system in a pipeline pattern. Each stage produces events consumed by the next stage, with the event bus decoupling producers from consumers so each can scale and fail independently.

document.captured: Emitted by a scraper when it captures a new or updated document. Payload includes raw content, content hash, source URL, court/county/state metadata, and capture timestamp. Consumed by the ingestion worker, which handles transcription, enrichment, and indexing inline.

scraper.health: Emitted by each scraper after every run with operational metrics (success/failure, response time, records captured). Consumed by CloudWatch metric filters for alerting on scraper failures.

Note: The ingestion worker processes documents inline (transcription → enrichment → DB write → OpenSearch index) rather than through separate event-driven stages. This is simpler and sufficient at current scale. The event bus handles producer-consumer decoupling between scrapers and the ingestion worker, but downstream processing is synchronous within the worker. A planned validation step (§3.5.3) will add LLM-based quality checks between enrichment and DB write.

### 2.1.2 Consumer Groups & Failure Handling

Redis Streams consumer groups ensure that each event is processed exactly once by each consuming service, even if the consumer crashes and restarts. Events are acknowledged after successful processing; unacknowledged events are automatically retried. A dead-letter mechanism catches events that fail processing repeatedly so they can be investigated without blocking the pipeline.

The pipeline is designed to be resumable. If any stage goes down (the NLP pipeline has an outage, Elasticsearch is temporarily unavailable), events accumulate in the stream and are processed when the consumer comes back. This is particularly important for the ingestion layer: scrapers should never be blocked by a downstream processing delay, because the court data they are capturing may be ephemeral.

### 2.1.3 Event Schema

All events share a common envelope: event type, event ID (UUID), timestamp, producer ID, and a correlation ID that traces a document from capture through indexing. The payload is event-type-specific and serialized as JSON. Event schemas are versioned so consumers can handle schema evolution gracefully.

## 2.2 Caching Strategy

Court data has an excellent property for caching: once captured, it almost never changes. A tentative ruling captured yesterday will have the same text today. A docket entry from last month is identical. This means cache invalidation — normally the hard part of caching — is straightforward. The caching layer uses the same Redis instance as the event bus and rate limiter.

### 2.2.1 What Gets Cached

Pre-computed AI outputs (highest value): Document summaries, entity extraction results, and classification outputs are generated during ingestion (Tier 1) and cached permanently. When a user views a document summary, the system serves the cached result rather than making a live AI API call. This eliminates the most expensive per-request cost.

Judge analytics aggregations: Grant/deny rates, motion-specific statistics, and other analytics are computed from the underlying ruling data and cached. Cache is invalidated and recomputed when new rulings for that judge are indexed (triggered by the document.indexed event). Since new rulings arrive at most daily, this is infrequent.

Search results: Elasticsearch query results for common searches are cached with short TTLs (5–15 minutes). This smooths traffic spikes without serving stale data. The GraphQL layer can serve entire resolved queries from cache when the underlying data has not changed.

Judge and attorney profiles: Profile pages with biographical data, case history, and analytics are among the most frequently accessed pages and change infrequently. Cached with event-driven invalidation.

### 2.2.2 Invalidation

Cache invalidation is event-driven. When a document.indexed event fires, the system invalidates cached data that depends on the new document: the relevant judge’s analytics cache, attorney profile caches, case detail caches, and any search result caches that might be affected. Because court data changes infrequently (new data arrives in daily batches, not continuous streams), the invalidation volume is low and the window of stale data is short.

For user-facing AI features (Tier 2 and 3), results are cached per-document: if a user requests a case assessment for a case that another user already assessed, the cached result is served. This is safe because the AI output is grounded in the same source documents regardless of who requests it.

# 3. Data Ingestion Layer

The ingestion layer is the most operationally critical component of Judgemind. It is responsible for capturing court data before it disappears, particularly tentative rulings that may only be available for days.

## 3.1 Scraper Framework

Scrapers are organized in a four-level hierarchy reflecting how court systems are actually structured:

Each scraper is a self-contained module with the following contract:

Configuration: Target URL(s), polling frequency, authentication requirements (if any), rate limits, and time-of-day restrictions (some courts deploy CAPTCHAs during business hours only).

Execution: Fetch data, parse response (HTML, PDF, or DOCX), extract structured fields, compute content hash.

Field extraction completeness: A scraper is not considered complete until it correctly extracts 100% of the structured fields present in the source data obtained during development. Required fields: judge name, motion type, case title, hearing date, outcome, and parties. If a field is present in the source, the scraper must extract it — do not ship scrapers that leave extractable fields empty and rely on post-hoc backfills. Regression tests against real fixtures must cover every extracted field. "Unknown" or "Not classified" values are acceptable only when the source data genuinely does not contain the information.

Output: Emit standardized ingestion events to the message queue. Events include raw content, parsed content, content hash, source metadata, and capture timestamp. Scrapers populate as many structured fields as possible (judge name, case number, hearing date, etc.) from the court website's own structured data. Any fields the scraper cannot populate are filled downstream by the three-tier extraction pipeline (see Section 5.2).

Error handling: Retry with exponential backoff. Alert on repeated failures. Log all errors with enough context for debugging (URL, response status, partial content).

Health reporting: Each scraper reports its last successful run, last failure, and current status to a central registry.

### 3.1.1 Lessons from Prior Implementation

The following design decisions are informed by hard-won experience building the original state court scraping infrastructure in 2016:

Assume nothing about data consistency. Different clerks enter data differently within the same court. Holiday schedules create unexpected entries. Typo corrections appear as updates. The scraper must treat every assumption about format as provisional and handle deviations gracefully.

Version tracking with content hashing. Every captured document or ruling gets a SHA-256 content hash. When a scraper sees content with a matching hash, it skips the document. When the hash differs, the new version is stored via upsert.

Multiple tentatives per case. A single case may have multiple tentative rulings corresponding to different motions or hearings. The data model must associate each tentative with its specific motion/hearing, not just the case.

Time-of-day awareness. Some courts deploy anti-scraping measures (CAPTCHAs, rate limits) only during business hours. Scrapers must support scheduling windows. Example: Orange County historically deployed CAPTCHAs from 9 AM–5 PM Pacific only.

Court website performance. Some court websites are slow or unreliable. Scrapers must have generous timeouts, handle partial responses, and avoid hammering already-struggling servers. Be a good citizen: respect robots.txt, use reasonable request intervals.

Leverage shared CMS platforms. Many counties use the same court management software (Tyler Technologies Odyssey is common). When we identify a shared CMS, we can write one scraper template and parameterize it per county, dramatically reducing per-court development effort.

## 3.2 Document Processing Pipeline

Court documents arrive in three formats, each requiring a different processing path:

All documents, regardless of source format, pass through the NLP pipeline (Section 5) after text extraction for entity extraction, classification, and embedding generation.

## 3.3 Tentative Ruling Capture

Tentative rulings are the highest-priority data type. The capture pipeline has dedicated monitoring and alerting separate from general scraping.

Polling frequency: Daily for most courts. Hourly for high-volume courts that update frequently (configurable per endpoint). More frequent polling adds cost and load without proportional value for most courts.

Archival: Every captured tentative ruling is immediately archived to object storage (immutable). The system never overwrites a previously captured version.

Deduplication: Content hashing (SHA-256) distinguishes new captures from duplicates. When a hash matches, the document is skipped. When it differs, the new version is stored (upsert semantics update mutable fields while preserving immutable ones). LLM-based diff classification (substantive vs cosmetic) is not yet implemented.

Failure alerting: If a tentative ruling scraper fails for more than 24 hours, an alert fires. Tentative ruling capture failures are treated as high-severity incidents because the data may be permanently lost.

## 3.4 External Data Integration

Judgemind integrates with existing open legal data sources to avoid duplicating effort and to provide federal court coverage:

CourtListener (Free Law Project): Federal opinions, some state appellate data. Implemented as a scraper (`packages/scraper-framework/src/courts/federal/courtlistener.py`) that ingests via their API.

## 3.5 Scraper Development & Quality Assurance

Building a reliable scraper requires iteration. Court websites are messy, inconsistent, and full of edge cases that only surface over time.

### 3.5.1 Development Process

AI agents (via `/task`) write scrapers against real court websites. Each scraper ships with regression tests against archived fixture pages. The `/ralph` review loop (worker + reviewers) iterates until the code passes review and CI. Once merged, the scraper runs on its EventBridge schedule and the ingestion worker processes captured documents.

### 3.5.2 Current Quality Assurance

Today, scraped data flows directly to the production database without a validation gate. Quality is ensured through:
- **Regression tests:** Every scraper has tests against real fixtures covering typical pages, edge cases, and known formatting variations.
- **Hourly data quality checks:** GitHub Actions workflow runs `scripts/data-quality-check.py` to detect ingest rate drops, scraper staleness, field completeness regressions, and orphaned documents. Auto-creates issues on regressions.
- **Periodic spot checks:** The `/spotcheck` skill samples rulings across counties, runs DB queries for known issue patterns, takes screenshots for visual inspection, and files issues for findings.
- **Field completeness auditing:** `scripts/audit_field_completeness.py` reports per-county gaps in required fields (judge name, motion type, case title, hearing date, outcome, parties).

These mechanisms catch scraper *failures* well (crashes, staleness, missing fields). They are weaker at catching scrapers that *succeed but return wrong data* — e.g., text assigned to the wrong case (#1716), ruling content from one entry overwriting another. This class of silent data corruption motivates the validation agent described below.

### 3.5.3 Validation Agent (Planned)

A lightweight LLM-based validation step that reviews every ingested document before it reaches the production database. The goal is to catch data quality issues that regression tests and volume-based monitoring miss — particularly cases where the scraper runs successfully but produces incorrect output.

**How it works:**

The ingestion worker, after transcription and enrichment, passes each document's extracted fields through a validation LLM call before writing to the database. The validator checks:

- **Internal consistency:** Does the ruling text plausibly match the assigned case number and case title? Is a 6-page ruling assigned to a case that also has a one-line "See #1 Above" entry? (This would have caught #1716.)
- **Field plausibility:** Is the judge name actually a name (not a court division header)? Is the hearing date in a reasonable range? Is the motion type a recognized legal motion?
- **Cross-document consistency:** Within a multi-case PDF, are all entries accounted for? Do entry counts match expected patterns for this court?
- **Court-specific learned rules:** Expected volume ranges per department, typical case number formats, known formatting patterns. These rules accumulate over time as edge cases are discovered.

**Validation outcomes:**

| Result | Action |
|---|---|
| **Pass** | Write to production database normally |
| **Flag** | Write to production but tag for async review; create a review item in the admin dashboard |
| **Fail** | Do not write to production; log the failure with full context; create a high-priority review item |

There is no manual approval gate — at hundreds of documents per day, all validation is automated. Flagged items are reviewed asynchronously and feed back into scraper improvements.

**Cost:** Uses a cheap model (Haiku-class or Flash Lite). The validation prompt is short (extracted fields + ruling text excerpt, not full text). At ~1,000 documents/day, the incremental LLM cost should be modest relative to existing enrichment costs.

**Implementation approach:** Add validation as a step in the ingestion worker between enrichment and database write. No separate service or staging schema needed — the worker already has all the context. Validation results are logged to a `validation_results` table for monitoring and the admin dashboard.

### 3.5.4 Scraper Health Model

The scraper health model tracks operational status via `scraper_runs` records (success/failure, response time, records captured) and output quality via hourly data quality checks. CloudWatch alarms fire if no successful scraper run occurs within 24 hours. The admin data quality dashboard shows per-county health tiles (green/yellow/red) based on ruling count, field completeness, and scraper freshness. Once the validation agent is implemented, validation pass/flag/fail rates will be added to the health model.

# 4. Data Store

Judgemind uses four complementary storage systems, each optimized for a specific access pattern.

## 4.1 PostgreSQL — Structured Data

PostgreSQL is the primary database for all structured, relational data. It stores the core entities and their relationships.

### 4.1.1 Core Entity Model

The data model centers on six primary entities. The entity-relationship design must accommodate the messiness of court data, particularly around identity resolution (see Section 4.1.2).

### 4.1.2 Entity Resolution

Court data is entered by humans with no enforced consistency. The same judge may appear as "Johnson, Robert M.", "Robert Johnson", "Hon. R.M. Johnson", or "Judge Johnson" across different courts, clerks, and document types.

The schema supports canonical records with aliases (`judges`/`judge_aliases`, `attorneys`/`attorney_aliases`). Currently, entity resolution is handled by the enrichment tier's regex extraction — normalized name matching links rulings to canonical judge records. Fuzzy matching and embedding-based resolution are not yet implemented.

## 4.2 OpenSearch — Full-Text Search

OpenSearch (AWS-managed) indexes ruling text for full-text search with relevance ranking. It powers the ruling search page with faceted filtering.

Faceted search: Aggregations for filtering by court, county, judge, hearing date range, case number prefix, motion type, and outcome.

Sync: OpenSearch is populated by the ingestion worker when it writes rulings to PostgreSQL. PostgreSQL is the source of truth; OpenSearch is a derived read-optimized view.

## 4.3 Qdrant — Vector Search (Not Yet Active)

Qdrant is included in the docker-compose stack and declared as a dependency, but embedding generation is not yet implemented. No embeddings are being produced or stored. When semantic search or RAG features are built, Qdrant will store document embeddings for similarity search with metadata filtering.

## 4.4 Object Storage — Documents & Archival

AWS S3 stores all original documents and archival copies (MinIO in docker-compose for local development).

Immutable archival: Original captured documents are never modified or deleted. Object versioning enabled for an additional safety net.

Tiered storage: Hot storage for documents less than 90 days old or frequently accessed. Cold/archive storage for older documents. Lifecycle policies automate transitions.

Path convention: /{state}/{county}/{court}/raw/{content_hash}.{ext} — content-addressed keys make S3 PutObject idempotent (same content = same key, no orphaned duplicates).

# 5. AI/ML Layer

The AI layer handles all natural language processing, from ingestion-time entity extraction to user-facing generative features. It is designed around three processing tiers with different cost, quality, and volume characteristics.

## 5.1 Processing Tiers

### 5.1.1 Cost Management Strategy

The guiding principle is: start simple, measure, then optimize. Do not prematurely invest in GPU infrastructure before understanding actual usage patterns.

Phase 1 (Months 1–6): All hosted APIs. Use small/cheap models for Tier 1 (Haiku-class). Monitor per-document ingestion cost carefully. Cache all Tier 1 outputs so they never need to be recomputed.

Phase 2 (Months 6–12): If Tier 1 costs exceed ~$3,000/month (indicating ~10,000+ documents/day), evaluate self-hosted GPU. A single A100 instance at ~$1,500/month running Llama or Mistral can handle Tier 1 at any realistic volume.

Ongoing: Tier 2 and Tier 3 remain on hosted commercial APIs indefinitely. Their per-call costs scale with users, not data volume, and quality requirements justify premium models. Rate limiting on AI features prevents runaway costs.

## 5.2 Ingestion Pipeline

The ingestion pipeline converts raw captured content into structured ruling records. It has three stages, each with a clear responsibility. **No stage should do the work of another stage** — scrapers capture, transcription converts format, enrichment populates fields.

### 5.2.1 Three-Stage Pipeline

| Stage | Responsibility | Inputs | Outputs |
|---|---|---|---|
| **Capture** (scraper) | Fetch raw content, extract metadata from website structure, archive to S3 | Court website HTML/PDF | Raw content bytes, structural metadata (judge_name, department from link text/HTML headers), source URL, content hash |
| **Transcription** | Convert raw content to clean text, split multi-case documents, mark cross-page continuations | Raw PDF bytes or HTML | Ruling text per case, case boundaries, continuation markers |
| **Enrichment** | Extract structured fields from text | Ruling text + scraper metadata | case_number, case_title, hearing_date, motion_type, outcome, parties, case_type |

**Capture** is format-agnostic. The scraper's job is to reliably fetch and archive raw content, plus extract whatever metadata the website's *own structure* provides (e.g., a judge name in link text, a department in a URL parameter). Scrapers do NOT parse PDF content or extract fields from unstructured text — that's enrichment's job.

**Transcription** varies by content format:
- **HTML documents** (e.g., LA County): text is extracted directly from HTML markup. No LLM needed — BeautifulSoup parsing is sufficient. Case splitting uses HTML structure (dividers, headings).
- **Tabular PDF documents** (e.g., OC): pages are rendered as images and sent to a multimodal LLM (one page per call). The prompt describes the visual structure of the page — column positions relative to ruled lines, column widths, row separators — so the LLM reads the table like a human would. The LLM returns structured JSON per table row with `entry_number`, `case_info`, and `ruling_text` fields. A post-processing join step merges rows across pages: a new case is detected when a row has both a valid integer entry number AND case identification (case number or adversarial case name) in the case_info column. Rows without both signals are merged as continuations of the previous case.
- **Text-based PDF documents** (e.g., Riverside): pdfplumber/pymupdf text extraction is reliable (no column layout issues), so extracted text is sent to the LLM rather than page images. The LLM's job is splitting numbered entries and extracting ruling text per case.

The transcription LLM prompt describes **visual structure, not text heuristics**. For tabular PDFs, it describes column positions relative to vertical ruled lines and row separators. For text-based PDFs, it describes the numbered entry format. The prompt never references specific case number formats, date patterns, or other fragile text patterns — those vary by court division and break when formats change. See `docs/scraper-lessons.md` §LLM Extraction for the full approach and lessons learned.

Each county's prompt is validated through an iterative eval loop before production integration: build an eval script against test fixtures, iterate the prompt until 100% lenient case count accuracy, then integrate. The rollout plan (#1467) is: OC → Riverside → SB → LA → SF/SC/Ventura.

The transcription LLM does NOT extract case numbers, titles, outcomes, or other structured fields. That is enrichment's job.

**Enrichment** applies a three-tier extraction strategy to each ruling's text:

**Tier 1 — Scraper-provided fields (highest priority).** Values the scraper extracted from website structure (not from document content). These are authoritative — e.g., a judge name from link text, a department from a URL parameter. Used as-is, never overwritten by later tiers.

**Tier 2 — LLM extraction.** For fields not provided by the scraper, the ingestion worker calls a configurable LLM to extract them from the ruling text. The LLM receives a structured JSON extraction prompt with the text and any Tier 1 metadata as context. Returns structured JSON with all extractable fields, applied only to fields still missing after Tier 1. On API failure, falls through to Tier 3.

Key implementation details:
- **Provider-agnostic adapter** (`packages/scraper-framework/src/ingestion/llm_providers.py`): supports Anthropic and Google GenAI via `LLM_PROVIDER` and `LLM_MODEL` environment variables. Default: Claude Haiku (defined centrally in `packages/judgemind-config/src/judgemind_config/models.py`, overridable via `HAIKU_MODEL` env var).
- **Connection reuse:** the worker creates a single LLM client at startup and reuses it across all documents in the session, amortizing connection overhead.
- **Rate-limit retry:** each provider adapter retries once on rate-limit errors (HTTP 429 / ResourceExhausted) with a 1-second backoff.
- **Cost:** approximately $47/month on Anthropic Haiku at current ingestion volume (~1,000 documents/day).

**Tier 3 — Regex fallback.** For fields still missing after LLM extraction (or when the LLM API is unavailable), the worker applies court-specific regex patterns (`packages/scraper-framework/src/ingestion/extract.py`). These cover outcome classification, motion type identification, case number extraction, case title parsing, judge name extraction, hearing date extraction, and party extraction from case captions. The regex patterns are drawn from real California court formatting and are ordered by specificity to minimize false positives.

### 5.2.2 Enrichment Logging

The worker tracks which tier populated each field in an `extraction_methods` dict (values: `"scraper"`, `"llm"`, `"regex"`) and logs a summary for every document. This enables monitoring of extraction quality and identifying courts where scrapers should be improved to reduce LLM dependency.

### 5.2.3 Reingestion

Historical documents already in S3 can be reprocessed through the full three-tier pipeline using `scripts/reingest_from_s3.py`. This script reads archived documents from the S3 bucket, reconstructs ingestion events, and pushes them through the same extraction pipeline. This is used to backfill fields for documents that were ingested before LLM extraction was available, or after extraction logic improvements.

### 5.2.4 Additional Tier 1 Capabilities

- **Summarization:** Partially implemented. Gated behind `ENABLE_RULING_SUMMARIZATION` env var. Uses Claude Haiku to generate one-paragraph summaries at ingestion time (`packages/scraper-framework/src/ingestion/ruling_summarizer.py`).
- **Embedding generation:** Not yet implemented. Qdrant dependency declared but no embeddings are being produced.
- **Version classification:** Not yet implemented. No LLM-based diffing for content hash mismatches.

## 5.3 RAG Pipeline (Not Yet Implemented)

User-facing generative AI features (document summarization, case assessment, etc.) will use retrieval-augmented generation grounded in actual court documents. This requires embedding generation (§5.2.4) to be implemented first. The pipeline design: retrieve relevant documents from Qdrant via semantic similarity → assemble context with source attribution → generate with citation requirements → verify citations against source material.

# 6. Application Layer

## 6.1 API Architecture — Dual API Pattern

Judgemind exposes two API surfaces, both backed by the same data access layer. This ensures consistency while optimizing each API for its audience.

### 6.1.1 GraphQL API (Internal, Frontend)

The Next.js frontend communicates exclusively with the GraphQL API. GraphQL is the right fit for Judgemind’s data model because:

Different views need different data slices. A case detail page, a judge profile page, and a search results page all query the same underlying entities but need different fields and relationships. GraphQL lets the frontend request exactly what it needs in one round trip.

Legal data is deeply relational. A single case involves a judge, multiple attorneys, multiple parties, docket entries, documents, rulings, and motions. GraphQL’s nested query structure maps naturally to this.

Performance: Eliminates the "chatty API" problem. A case detail page that would require 5–7 REST calls can be served in a single GraphQL query. This directly supports the requirement for a fast, responsive UI.

### 6.1.2 REST API

Currently minimal — two endpoints for document access:
- `/rest/document-download` — download original PDF/HTML from S3
- `/rest/document-content` — retrieve document text content with charset handling

A comprehensive public REST API (resource endpoints, OpenAPI docs, versioning, webhooks) is a future consideration if third-party integration demand materializes. For now, GraphQL serves all needs.

### 6.1.3 Shared Data Access Layer

Both APIs sit on top of a shared data access layer that handles database queries, caching, authorization, and business logic. This ensures that a case retrieved via GraphQL and the same case retrieved via REST are always identical and subject to the same access controls.

## 6.2 Web Application — Next.js

The web application is built with Next.js, providing server-side rendering for SEO and fast initial page loads, with client-side navigation for a responsive single-page application experience after first load.

### 6.2.1 Why Next.js

SEO: Court data pages (judge profiles, case summaries, ruling text) should be indexable by search engines. Server-side rendering ensures search engines see full content. This is particularly important for an open-source project that benefits from organic discovery.

Performance: Server-side rendering means users see content on first paint without waiting for client-side JavaScript to fetch data. Combined with GraphQL, this produces a fast, responsive experience.

Developer experience: Next.js is the most widely adopted React framework with a large ecosystem. This matters for an open-source project that needs community contributors.

## 6.3 Authentication & Authorization

JWT-based authentication with refresh tokens. Two login methods: email/password (with email verification) and Google OAuth. Rate limiting on login attempts. No role-based access control beyond a simple admin flag on user records.

## 6.5 Cost Protection & Rate Limiting

Judgemind is free, but the hosted instance is self-funded. The platform must protect against both intentional abuse and unintentional cost spikes without degrading the experience for normal users. Cost protection is built into every layer that has variable cost exposure.

### 6.5.1 API Rate Limiting

Rate limits apply to both the GraphQL and REST APIs. Limits are generous for normal use and aggressive against abuse.

Per-user limits: Authenticated users get a baseline request budget (e.g., 1,000 API calls/hour for search and data retrieval). Unauthenticated access is heavily restricted or disabled for expensive endpoints.

Per-API-key limits: Third-party API keys have configurable rate limits. Default limits are generous for research and integration use. Keys that consistently hit limits can request increases (manual review).

Anti-scraping: Patterns consistent with bulk scraping (sequential enumeration, high-volume document downloads, systematic crawling) are detected and throttled. The irony of an open-source scraping platform blocking scrapers is acknowledged, but the hosted instance has finite resources. Anyone who wants bulk access can self-host.

### 6.5.2 AI Feature Cost Caps

AI-powered features (Tiers 2 and 3) are the most expensive per-request operations. They require dedicated cost controls:

Per-user daily AI budget: Each user gets a daily allocation of AI-powered operations (e.g., 20 document summaries, 5 case assessments, 2 motion drafts per day). Limits are set based on actual cost per operation and total AI budget. Users who hit their daily cap see a clear message explaining the limit and when it resets.

Global AI spend ceiling: A platform-wide daily and monthly ceiling on total AI API spend. If the ceiling is approached, the system automatically reduces per-user AI budgets or temporarily queues non-urgent AI requests. This prevents a sudden influx of users from creating an unbounded cost spike.

Tiered degradation: If cost pressure requires it, the system can downgrade AI operations to cheaper models (e.g., Haiku instead of Sonnet for summarization) rather than disabling features entirely. This is preferable to hard cutoffs from the user’s perspective.

Abuse detection: Automated detection of patterns that suggest non-human or abusive use of AI features (e.g., rapid-fire summarization of hundreds of documents, which suggests automated consumption rather than a human researcher). Flagged accounts are throttled pending review.

### 6.5.3 Admin Controls

Platform administrators have the ability to:

Adjust per-user and global rate limits and AI budgets without a code deploy (configuration-driven).

Throttle or suspend specific users or API keys that are generating disproportionate cost.

Temporarily disable specific AI features platform-wide if costs spike unexpectedly (emergency lever).

View a real-time cost dashboard showing per-user, per-feature, and per-model spend with projections based on current usage trajectory.

# 7. Infrastructure & Deployment

## 7.1 Hosted Instance

The primary Judgemind instance runs on AWS (us-west-2) using ECS Fargate for all compute. Fargate was chosen over EC2 or Kubernetes because it provides per-second billing with zero cluster management overhead — no nodes to patch, no autoscaler to tune, no idle capacity to pay for. At current scale this is significantly cheaper than maintaining a Kubernetes cluster.

**Compute services (ECS Fargate):**

| Service | Task | Schedule |
|---|---|---|
| API | `judgemind-api-{env}` | Always-on (ECS Service, ALB health-checked) |
| Ingestion worker | `judgemind-ingestion-worker-{env}` | Always-on (ECS Service, consumes Redis Streams) |
| Scrapers | `judgemind-scraper-{env}` | Scheduled (EventBridge cron rules per court) |
| One-off scripts | `judgemind-exec-{env}` | On-demand via `scripts/ecs-run-task.sh` |

**Other AWS services:** RDS PostgreSQL, ElastiCache Redis, OpenSearch, S3 (document archive), CloudWatch (logs + alarms), SNS (alert delivery), Secrets Manager, ACM (TLS certs), Route 53 (DNS).

**Frontend:** Vercel (Next.js), deployed automatically on push to main.

All infrastructure is managed via Terraform (`infra/terraform/`).

## 7.1.1 Domain Naming Convention

All Judgemind services follow a consistent domain naming pattern. Production services use bare subdomains under `judgemind.org`. Non-production environments prefix the environment name to the service subdomain.

| Service | Production | Dev |
|---------|-----------|-----|
| Web app | `judgemind.org` | `dev.judgemind.org` |
| API     | `api.judgemind.org` | `dev.api.judgemind.org` |

The pattern is `{env}.{service}.judgemind.org` for non-production and `{service}.judgemind.org` for production (web app uses the bare domain). This keeps the environment as a prefix, avoiding DNS conflicts where a parent subdomain's CNAME (e.g. `dev.` pointing to Vercel) would affect child subdomains (e.g. CAA record inheritance).

## 7.2 Local Development

`docker-compose.yml` provides the full dependency stack for local development: PostgreSQL 16, Redis 7, OpenSearch 2.12, Qdrant, and MinIO (S3-compatible). Self-hosted production deployment is not a design goal.

## 7.3 Monitoring & Observability

Monitoring uses CloudWatch for infrastructure metrics and alarms, GitHub Actions for automated data quality checks, and PostgreSQL for metrics storage. No Prometheus or Grafana — CloudWatch is the natural fit for ECS Fargate, and GitHub Actions workflows provide higher-level checks that integrate directly with the issue tracker.

### 7.3.1 CloudWatch Alarms

CloudWatch metric filters extract key signals from ECS task logs and ALB metrics:

| Alarm | Trigger | Severity |
|---|---|---|
| Scraper no success 24h | No `scraper_run_complete` log in 24h | Critical (data loss risk) |
| Ingestion worker crash loop | 3+ crashes in 15 min | Critical |
| API 5xx errors | ≥10 errors in 5 min (prod) | High |
| API P99 latency | ≥5s P99 response time | Medium |
| Data quality check stale | Hourly DQ check hasn't run in 2h | Medium |
| API unhealthy hosts | ALB target health check failures | High |

Alarms publish to SNS topics (`judgemind-scraper-alerts-{env}`) which deliver to email.

### 7.3.2 Automated Data Quality Checks

Two GitHub Actions workflows run hourly and provide application-level monitoring:

**Data quality check** (`.github/workflows/data-quality-check.yml`, every hour at :15):
- Runs `scripts/data-quality-check.py` on dev via ECS
- Checks ingest rate drops, scraper staleness, zero rulings, field completeness regressions, orphaned documents
- Auto-creates GitHub issues labeled `data-quality-failure` on regressions
- Auto-closes issues when checks pass
- Sends Telegram notifications to ops channel on failures

**Site quality check** (`.github/workflows/site-quality-check.yml`, every hour at :30):
- Validates `dev.judgemind.org` pages load with expected content
- Validates API GraphQL endpoint responds with expected shapes
- Auto-creates/closes `site-quality-failure` issues

### 7.3.3 Data Quality Dashboard

An admin dashboard at `/admin/data-quality` shows per-county health status:
- **OverviewGrid**: county health tiles (green/yellow/red) based on ruling count, field completeness, and scraper freshness
- **MetricsChart**: 7-day time-series of key metrics from `data_quality_metrics` table
- **CountyDetail**: drill-down with full metric history

Health thresholds: green (rulings > 0, completeness ≥ 90%, scraper < 6h old), yellow (completeness 70–90% or scraper 6–24h old), red (scraper > 24h or completeness < 70%).

### 7.3.4 ECS Container Insights

Container Insights is enabled on all ECS clusters, providing automatic CPU, memory, network, and task health metrics without custom instrumentation.

### 7.3.5 Log Retention

Dev: 14 days. Production: 30 days. All ECS tasks write structured logs to CloudWatch log groups (`/ecs/judgemind-{service}-{env}`).

## 7.4 Testing Strategy

Testing a system that depends on live, external court websites presents a unique challenge. The court websites change without notice, and scraper correctness can only be verified against real court data. Judgemind’s testing strategy uses the archived court pages it captures during normal operation as a regression test corpus.

### 7.4.1 Scraper Testing

Since the ingestion pipeline already archives every page it captures (raw HTML, PDFs, and DOCX files in object storage), these archived pages form a natural regression test corpus. The testing approach works in three layers:

Baseline snapshot corpus: For each court, maintain a representative sample of archived pages (50–100 pages covering typical rulings, edge cases, holidays, multi-tentative cases, and clerk formatting variations). This sample is curated during the burn-in phase as interesting edge cases are discovered. Each snapshot is paired with the expected extraction output (the “golden” output that was validated during burn-in).

Regression testing on scraper changes: When a scraper is modified (to handle a new edge case, adapt to a site redesign, or fix a bug), the modified scraper runs against the full snapshot corpus for that court. Its output is compared to the golden output. Any discrepancies are reviewed: if the scraper correctly handles a previously failing case, the golden output is updated. If it breaks a previously passing case, the change is rejected or revised.

Edge case fixtures: Particularly interesting or tricky pages are tagged as permanent test fixtures. These include: pages with multiple tentative rulings for the same case, holiday schedule entries, typo corrections (paired with the original to test version deduplication), unusual clerk formatting, CAPTCHAs or access-denied pages (to test error handling), and pages from court website redesigns (to test detection of structural changes).

### 7.4.2 Application Testing

Standard application testing applies to the API and web layers:

Unit tests: Data access layer, entity resolution logic, rate limiting, and authentication. These are conventional and can use standard mocking.

Integration tests: End-to-end tests that push a captured document through the ingestion pipeline (transcription, enrichment, indexing) and verify the final state in the database.

API tests: The GraphQL API is tested against its schema. TypeScript type checking (`tsc --noEmit`) ensures type safety across the API and frontend.

CI/CD: GitHub Actions runs the full test suite on every pull request. Scraper regression tests run as part of CI when scraper code is modified. The test suite must pass before merge.

## 7.5 Backup & Disaster Recovery

Judgemind has an unusual backup requirement: most of its data is public court records that could theoretically be re-scraped, but tentative rulings and other ephemeral data cannot be re-acquired once the court takes them down. Losing the tentative ruling archive means losing data that is irreplaceable. The backup strategy reflects this asymmetry.

### 7.5.1 Document Archive (Critical)

The S3-compatible object store containing original captured documents (especially tentative rulings) is the most critical data to protect:

Object versioning: Enabled on the document bucket. Protects against accidental deletion or overwrite.

Cross-region replication: The document archive is replicated to a second region. If the primary region suffers a catastrophic failure, the archive survives. This is the one area where the cost of redundancy is justified regardless of budget pressure.

Versioning: The archive bucket has versioning enabled to protect against accidental deletion or overwrite. Object lock (WORM) is not currently enabled.

### 7.5.2 PostgreSQL (Important)

The PostgreSQL database contains all structured data (cases, judges, attorneys, docket entries, user accounts). While most of this data could be re-derived from the document archive by re-running the ingestion pipeline, that would be extremely expensive and time-consuming. Standard database backup practices apply:

Automated daily snapshots with point-in-time recovery (PITR) enabled for continuous WAL archiving.

Backup retention: 30 days of daily snapshots, 12 months of weekly snapshots.

Regular backup restoration tests to verify recoverability.

### 7.5.3 OpenSearch & Qdrant (Rebuildable)

Both OpenSearch indices and Qdrant vector collections are derived from PostgreSQL and the document archive. They can be fully rebuilt from source if lost, though rebuilding takes time. AWS-managed OpenSearch has automated snapshots. Neither requires the same level of protection as the document archive or PostgreSQL, since they are derived data stores.

# 8. Security & Trust

Judgemind handles public court data, but user accounts require standard security practices. Secrets are stored in AWS Secrets Manager. All traffic is TLS-terminated. JWT tokens have short expiry with refresh token rotation.

# 9. Technology Stack Summary

# 10. Open Questions & Decisions Deferred

Tier 1 self-hosting threshold. The crossover point where self-hosted GPU becomes cheaper than hosted API calls depends on actual document volume and per-document token counts. Currently using Google GenAI (Flash Lite) for transcription and Claude Haiku for enrichment — both are cheap enough that self-hosting is not justified at current scale.

Embedding generation and semantic search. When to build this depends on whether full-text search (OpenSearch) is sufficient for user needs or whether semantic/RAG features are demanded.

Predictive model architecture. Deferred until 18+ months of data accumulation. The prediction system will require its own architecture review when the time comes.

End of Document