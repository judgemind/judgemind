JUDGEMIND

Architecture Specification v1.0

Last updated: April 2026

Companion to: Judgemind Product Specification

AI-implemented • Human-reviewed • Open source

---

This document is split into two parts. **§1–§2** cover principles and system overview. **§3 Today** describes what is implemented and running in production. **§4 Direction** describes what is planned but not yet built. A component belongs in exactly one of those sections. When a feature is partially built, name the shipped part in Today and the unbuilt part in Direction — no "partially implemented" hedge prose.

# 1. Architecture Principles

The following principles govern all architectural decisions in Judgemind. They are listed in priority order; when principles conflict, higher-ranked principles prevail.

**API-first.** The web application is a client of the API, not the other way around. Every capability exposed in the UI is available programmatically. The public REST API and the internal GraphQL API share the same data access layer.

**Cost-aware by default.** Judgemind is self-funded and free to users. Every component must be designed with cost ceilings in mind. Prefer fixed-cost infrastructure over usage-based pricing where possible. Never assume unlimited budget.

**Data capture is irreversible priority.** Tentative rulings and other ephemeral court data disappear permanently if not captured. The ingestion pipeline is the single most critical system. Downtime in the web UI is tolerable; downtime in scraping is data loss.

**Transparency over polish.** Every AI output is labeled. Every analytic shows its sample size. Every data gap is disclosed. Trust is the product.

**Open source.** The codebase is open source. Architecture should favor open-source and commodity components where practical, but managed AWS services are the primary deployment target. If a proprietary service is clearly the best tool for a job, use it. Self-hosted deployment is not a design constraint.

**No unreachable affordances.** Buttons, endpoints, config flags, and schema fields only land in `main` when they've been exercised end-to-end. Half-built behind a feature flag is fine; half-built and reachable by users is a bug.

# 2. System Overview

Judgemind consists of five major subsystems connected by event-driven messaging. Each subsystem can be scaled, deployed, and developed independently.

## 2.1 Event-Driven Architecture

The subsystems communicate through an event bus implemented with Redis Streams. Redis is already in the stack for caching and rate limiting, so using Redis Streams for messaging avoids adding another service. The event volume (thousands of documents per day, not millions) is well within Redis Streams' capabilities.

### 2.1.1 Event Flow

Data flows through the system in a pipeline pattern. Each stage produces events consumed by the next stage, with the event bus decoupling producers from consumers so each can scale and fail independently.

- **document.captured**: Emitted by a scraper when it captures a new or updated document. Payload includes raw content, content hash, source URL, court/county/state metadata, and capture timestamp. Consumed by the ingestion worker, which handles transcription, enrichment, and indexing inline.
- **scraper.health**: Emitted by each scraper after every run with operational metrics (success/failure, response time, records captured). Consumed by CloudWatch metric filters for alerting on scraper failures.

The ingestion worker processes documents inline (transcription → enrichment → DB write → OpenSearch index) rather than through separate event-driven stages. This is simpler and sufficient at current scale. The event bus handles producer-consumer decoupling between scrapers and the ingestion worker; downstream processing is synchronous within the worker.

### 2.1.2 Consumer Groups & Failure Handling

Redis Streams consumer groups ensure that each event is processed exactly once by each consuming service, even if the consumer crashes and restarts. Events are acknowledged after successful processing; unacknowledged events are automatically retried. A dead-letter mechanism catches events that fail processing repeatedly so they can be investigated without blocking the pipeline.

The pipeline is designed to be resumable. If any consumer goes down, events accumulate in the stream and are processed when the consumer comes back. This is particularly important for the ingestion layer: scrapers should never be blocked by a downstream processing delay, because the court data they are capturing may be ephemeral.

### 2.1.3 Event Schema

All events share a common envelope: event type, event ID (UUID), timestamp, producer ID, and a correlation ID that traces a document from capture through indexing. The payload is event-type-specific and serialized as JSON. Event schemas are versioned so consumers can handle schema evolution gracefully.

## 2.2 Caching Strategy

Court data has an excellent property for caching: once captured, it almost never changes. A tentative ruling captured yesterday will have the same text today. A docket entry from last month is identical. This means cache invalidation — normally the hard part of caching — is straightforward. The caching layer uses the same Redis instance as the event bus and rate limiter.

**What gets cached today:**

- **Judge analytics aggregations:** Grant/deny rates, motion-specific statistics, and other analytics computed from ruling data. Invalidated when new rulings for that judge are indexed.
- **Search results:** OpenSearch query results for common searches, with short TTLs (5–15 minutes) to smooth traffic spikes.
- **Judge and attorney profiles:** Profile pages with biographical data, case history, and analytics. Cached with event-driven invalidation.

**Invalidation.** Event-driven. When a document is indexed, the system invalidates caches depending on that document: the relevant judge's analytics, attorney profile, case detail, and any affected search results. Because court data arrives in daily batches rather than continuous streams, invalidation volume is low and stale windows are short.

# 3. Today — Implemented and Running

This section describes what currently exists in production. Anything not described here is either out of scope or described in §4 Direction.

## 3.1 Data Ingestion

The ingestion layer is the most operationally critical component of Judgemind. It captures court data before it disappears — particularly tentative rulings that may only be available for days.

### 3.1.1 Scraper Framework

Scrapers are organized in a four-level hierarchy reflecting how court systems are actually structured. Each scraper is a self-contained module with the following contract:

- **Configuration:** Target URL(s), polling frequency, authentication requirements (if any), rate limits, and time-of-day restrictions (some courts deploy CAPTCHAs during business hours only).
- **Execution:** Fetch data, parse response (HTML, PDF, or DOCX), extract structured fields, compute content hash.
- **Field extraction completeness:** A scraper is not considered complete until it correctly extracts 100% of the structured fields present in the source data obtained during development. Required fields: judge name, motion type, case title, hearing date, outcome, and parties. If a field is present in the source, the scraper must extract it — do not ship scrapers that leave extractable fields empty and rely on post-hoc backfills. Regression tests against real fixtures must cover every extracted field. "Unknown" or "Not classified" values are acceptable only when the source data genuinely does not contain the information.
- **Output:** Emit standardized ingestion events to the message queue. Events include raw content, parsed content, content hash, source metadata, and capture timestamp. Scrapers populate as many structured fields as possible (judge name, case number, hearing date, etc.) from the court website's own structured data. Any fields the scraper cannot populate are filled downstream by the LLM enrichment pipeline (§3.3.2).
- **Error handling:** Retry with exponential backoff. Alert on repeated failures. Log all errors with enough context for debugging.
- **Health reporting:** Each scraper reports its last successful run, last failure, and current status to a central registry.

**Lessons baked into the framework:**

- **Assume nothing about data consistency.** Different clerks enter data differently within the same court. Holiday schedules create unexpected entries. Typo corrections appear as updates. The scraper treats every assumption about format as provisional.
- **Version tracking with content hashing.** Every captured document or ruling gets a SHA-256 content hash. When a scraper sees matching content, it skips the document. Differing hashes trigger an upsert.
- **Multiple tentatives per case.** A single case may have multiple tentative rulings corresponding to different motions or hearings. The data model associates each tentative with its specific motion/hearing, not just the case.
- **Time-of-day awareness.** Some courts deploy anti-scraping measures (CAPTCHAs, rate limits) only during business hours. Scrapers support scheduling windows.
- **Court website performance.** Some court websites are slow or unreliable. Scrapers have generous timeouts, handle partial responses, and avoid hammering struggling servers.
- **Leverage shared CMS platforms.** Many counties use the same court management software (Tyler Technologies Odyssey is common). One scraper template parameterized per county can cover many courts.

### 3.1.2 Document Processing Pipeline

Court documents arrive in three formats (HTML, PDF, DOCX), each requiring a different processing path. All documents pass through the AI/ML pipeline (§3.3) after text extraction.

### 3.1.3 Tentative Ruling Capture

Tentative rulings are the highest-priority data type. The capture pipeline has dedicated monitoring and alerting separate from general scraping.

- **Polling frequency:** Daily for most courts. Hourly for high-volume courts that update frequently (configurable per endpoint).
- **Archival:** Every captured tentative ruling is immediately archived to object storage (immutable). The system never overwrites a previously captured version.
- **Deduplication:** Content hashing (SHA-256) distinguishes new captures from duplicates. When a hash matches, the document is skipped. When it differs, the new version is stored (upsert semantics update mutable fields while preserving immutable ones).
- **Failure alerting:** If a tentative ruling scraper fails for more than 24 hours, an alert fires. Tentative ruling capture failures are treated as high-severity incidents because the data may be permanently lost.

### 3.1.4 External Data Integration

Judgemind integrates with existing open legal data sources to avoid duplicating effort:

- **CourtListener (Free Law Project):** Federal opinions and some state appellate data. Implemented as a scraper (`packages/scraper-framework/src/courts/federal/courtlistener.py`) that ingests via their API, with regression tests against recorded responses.

### 3.1.5 Scraper Development & Quality Assurance

Building a reliable scraper requires iteration. Court websites are messy, inconsistent, and full of edge cases that only surface over time.

**Development process.** AI agents (via `/task`) write scrapers against real court websites. Each scraper ships with regression tests against archived fixture pages. The `/ralph` review loop (worker + reviewers) iterates until the code passes review and CI. Once merged, the scraper runs on its EventBridge schedule and the ingestion worker processes captured documents.

**Quality assurance mechanisms:**

- **Regression tests.** Every scraper has tests against real fixtures covering typical pages, edge cases, and known formatting variations.
- **Hourly data quality checks.** GitHub Actions workflow runs `scripts/data-quality-check.py` to detect ingest rate drops, scraper staleness, field completeness regressions, and orphaned documents. Persists per-county metrics to the `data_quality_metrics` DB table and sends P1 alerts via Telegram.
- **Periodic spot checks.** The `/spotcheck` skill samples rulings across counties, runs DB queries for known issue patterns, takes screenshots for visual inspection, and files issues for findings.
- **Field completeness auditing.** `scripts/audit_field_completeness.py` reports per-county gaps in required fields.

These mechanisms catch scraper *failures* well (crashes, staleness, missing fields). They are weaker at catching scrapers that *succeed but return wrong data* — e.g., text assigned to the wrong case, ruling content from one entry overwriting another. That class of silent data corruption motivates the validation agent in §4.1.

**Scraper health model.** Operational status is tracked via `scraper_runs` records (success/failure, response time, records captured). Output quality is tracked via hourly data quality checks. CloudWatch alarms fire if no successful scraper run occurs within 24 hours. The admin data quality dashboard shows per-county health tiles (green/yellow/red) based on ruling count, field completeness, and scraper freshness.

## 3.2 Data Store

Judgemind uses three complementary storage systems, each optimized for a specific access pattern.

### 3.2.1 PostgreSQL — Structured Data

PostgreSQL is the primary database for all structured, relational data. The data model centers on six primary entities — courts, judges, cases, attorneys, parties, and rulings — with supporting tables for documents and alerts.

**Entity resolution.** Court data is entered by humans with no enforced consistency. The same judge may appear as "Johnson, Robert M.", "Robert Johnson", "Hon. R.M. Johnson", or "Judge Johnson" across different courts, clerks, and document types. The schema supports canonical records with aliases (`judges`/`judge_aliases`, `attorneys`/`attorney_aliases`). Entity resolution is handled by the enrichment tier: normalized name matching links rulings to canonical judge records.

**Schema namespaces** make data tier explicit:

- **`derived.*`** (courts, judges, cases, attorneys, parties, documents, rulings, court_directory_snapshots, and `*_aliases`): rebuildable from the S3 archive by re-running ingestion (`scripts/rebuild_db.py`). Cacheable, disposable state.
- **`public.*`** (users, refresh_tokens, alert_subscriptions, alert_events): authoritative accumulated state. Not derivable from S3.
- **`staging.*`** (captures, ruled_items): transient pipeline buffers between ingestion stages.
- **`telemetry.*`** (scraper_runs, validation_results, data_quality_metrics): accumulated observability data. Not derivable from S3.

### 3.2.2 OpenSearch — Full-Text Search

OpenSearch (AWS-managed) indexes ruling text for full-text search with relevance ranking. It powers the ruling search page with faceted filtering (court, county, judge, hearing date range, case number prefix, motion type, outcome).

Populated by the ingestion worker when it writes rulings to PostgreSQL. PostgreSQL is the source of truth; OpenSearch is a derived read-optimized view.

### 3.2.3 Object Storage — Documents & Archival

AWS S3 stores all original documents and archival copies (MinIO in docker-compose for local development).

**Archive-first principle.** The S3 archive is the authoritative source of truth for all captured data. The PostgreSQL database is a derived, rebuildable index — every fact in the database traces back to an archived source file in S3. Three implications:

1. **Archive before process.** Every external data source (court websites, court directories) archives raw fetched content to S3 before any extraction or transformation. If a downstream processing step fails, the raw content is preserved and can be reprocessed.
2. **The database is rebuildable.** All extraction (LLM, regex, HTML parsing) can be re-run on archived content. `scripts/rebuild_db.py` discovers courts from S3 key prefixes, seeds the database, fetches court directory rosters, then processes every archived document through the full ingestion pipeline. The result is a complete database rebuilt from S3 alone.
3. **Immutable archival.** Original captured documents are never modified or deleted. Object versioning is enabled as an additional safety net.

**Content-addressed key scheme.** S3 keys for captured documents include a SHA-256 hash of the content. This makes writes idempotent (the same content maps to the same key, so duplicate uploads are no-ops), gives deduplication by construction, and makes the local cache (`S3_CACHE_DIR`) cache-friendly — cached files are valid forever with no invalidation. The archiver implementation is in `packages/scraper-framework/src/framework/storage.py`.

**S3 key prefixes** in the `judgemind-document-archive-{env}` bucket:

| Prefix pattern | Purpose | Key scheme |
|---|---|---|
| `{state}/{county}/{court}/raw/{content_hash}.{ext}` | Captured tentative rulings and court documents | Content-addressed |
| `directories/{court_id}/{timestamp}.{ext}` | Department-to-judge directory snapshots | Timestamped |
| `llm-cache/{provider}-{model}/prompt-{prompt_hash}/{content_key}.json` | Cached LLM extraction results | Content-addressed (prompt + document) |
| `data-quality/{YYYY-MM-DD}/{HH}.json` | Hourly data quality check snapshots | Timestamped |

Directory snapshots use timestamped (not content-addressed) keys because the same directory page changes over time and we want to preserve history. LLM cache stores extraction results keyed by prompt template hash and document content hash — changing the prompt invalidates the cache (new results are computed); unchanged prompts reuse cached results. The cache is shared across local and ECS environments via S3.

**Storage tiers.** Hot storage for documents less than 90 days old or frequently accessed. Cold/archive storage for older documents. Lifecycle policies automate transitions.

## 3.3 AI/ML Layer

The AI layer handles all natural language processing at ingestion time. User-facing generative features (RAG, summarization) are not yet built — see §4.2.

### 3.3.1 Processing Tiers

Judgemind uses three processing tiers with different cost, quality, and volume characteristics. The guiding principle is: start simple, measure, then optimize. Do not prematurely invest in GPU infrastructure before understanding actual usage patterns.

**Tier 1 — per-document ingestion.** Runs on every document at capture time (transcription, enrichment). Uses small/cheap models. All outputs are cached so they never need to be recomputed. Currently ~$47/month on Claude Haiku at ~1,000 documents/day ingestion volume.

**Tier 2 — per-user interactive.** Reserved for future user-facing AI features. Hosted commercial APIs on a per-call basis. Rate limiting prevents runaway costs. No Tier 2 features are live today.

**Tier 3 — per-query generative.** Reserved for future RAG-grounded generation. Hosted commercial APIs. Not live today.

### 3.3.2 Three-Stage Ingestion Pipeline

The ingestion pipeline converts raw captured content into structured ruling records. It has three stages, each with a clear responsibility. **No stage should do the work of another stage** — scrapers capture, transcription converts format, enrichment populates fields.

| Stage | Responsibility | Inputs | Outputs |
|---|---|---|---|
| **Capture** (scraper) | Fetch raw content, extract metadata from website structure, archive to S3 | Court website HTML/PDF | Raw content bytes, structural metadata (judge_name, department from link text/HTML headers), source URL, content hash |
| **Transcription** | Convert raw content to clean text, split multi-case documents, mark cross-page continuations | Raw PDF bytes or HTML | Ruling text per case, case boundaries, continuation markers |
| **Enrichment** | Extract structured fields from text | Ruling text + scraper metadata | case_number, case_title, hearing_date, motion_type, outcome, parties, case_type |

**Capture** is format-agnostic. The scraper reliably fetches and archives raw content plus extracts whatever metadata the website's *own structure* provides (e.g., a judge name in link text, a department in a URL parameter). Scrapers do NOT parse PDF content or extract fields from unstructured text — that's enrichment's job.

**Transcription** varies by content format:

- **HTML documents** (e.g., LA County): text is extracted directly from HTML markup. No LLM needed — BeautifulSoup parsing is sufficient. Case splitting uses HTML structure (dividers, headings).
- **Tabular PDF documents** (e.g., OC): pages are rendered as images and sent to a multimodal LLM (one page per call). The prompt describes the visual structure of the page — column positions relative to ruled lines, column widths, row separators — so the LLM reads the table like a human would. The LLM returns structured JSON per table row; a post-processing join step merges rows across pages based on whether a row has both a valid integer entry number AND case identification.
- **Text-based PDF documents** (e.g., Riverside): pdfplumber/pymupdf text extraction is reliable (no column layout issues), so extracted text is sent to the LLM rather than page images. The LLM's job is splitting numbered entries and extracting ruling text per case.

The transcription LLM prompt describes **visual structure, not text heuristics**. Each county's prompt is validated through an iterative eval loop before production integration: build an eval script against test fixtures, iterate the prompt until 100% lenient case count accuracy, then integrate. The transcription LLM does NOT extract case numbers, titles, outcomes, or other structured fields — that is enrichment's job.

**Enrichment** applies a two-tier extraction strategy to each ruling's text:

- **Tier 1 — Scraper-provided fields (highest priority).** Values the scraper extracted from website structure (not from document content). Authoritative — used as-is, never overwritten by later tiers.
- **Tier 2 — LLM extraction.** For fields not provided by the scraper. Two paths: per-field LLM extraction (`packages/scraper-framework/src/framework/llm_extractor.py`) for transcription-level fields on counties with custom configs, and universal LLM enrichment (`packages/scraper-framework/src/framework/llm_enrichment.py`) which extracts motion_type, outcome, case_title, and parties from ruling text in a single, taxonomy-constrained, stateless LLM call.

**Regex utilities** in `packages/scraper-framework/src/ingestion/extract.py` cover fields not handled by LLM enrichment: case number extraction, hearing date extraction, judge name extraction, and case type inference from case number prefixes. These are lightweight fallbacks that supplement LLM extraction.

**Implementation details:**

- **Provider-agnostic adapter** (`packages/scraper-framework/src/ingestion/llm_providers.py`): supports Anthropic and Google GenAI via `LLM_PROVIDER` and `LLM_MODEL` environment variables. Default: Claude Haiku (centrally defined in `packages/judgemind-config/src/judgemind_config/models.py`, overridable via `HAIKU_MODEL`).
- **Connection reuse:** the worker creates a single LLM client at startup and reuses it across all documents.
- **Rate-limit retry:** each provider adapter retries once on 429 / ResourceExhausted with a 1-second backoff.
- **Enrichment logging:** the worker tracks which tier populated each field in an `extraction_methods` dict (`"scraper"`, `"llm"`, `"llm_enrichment"`, `"regex"`) and logs a summary for every document. Enables monitoring of extraction quality per court.

**Reingestion.** Historical documents already in S3 can be reprocessed through the full pipeline using `scripts/reingest_from_s3.py`. Operates on **existing database records only** — it queries `documents` to find S3 keys to reprocess. For initial population of a county that has S3 data but no DB records, use `scripts/rebuild_db.py --county <name>`, which discovers documents directly from S3 keys.

## 3.4 Application Layer

### 3.4.1 Dual API — GraphQL + Minimal REST

Judgemind exposes two API surfaces, both backed by the same data access layer.

**GraphQL API (internal, frontend).** The Next.js frontend communicates exclusively with GraphQL. Legal data is deeply relational — a single case involves a judge, multiple attorneys, parties, docket entries, documents, rulings, and motions — and GraphQL's nested query structure maps naturally to it. A case detail page that would require 5–7 REST calls can be served in one GraphQL query.

**REST API (minimal).** Two endpoints today, both for document access:
- `/rest/document-download` — download original PDF/HTML from S3
- `/rest/document-content` — retrieve document text content with charset handling

Both APIs sit on a shared data access layer handling queries, caching, authorization, and business logic. This guarantees that a case retrieved via GraphQL and via REST is identical and subject to the same access controls.

### 3.4.2 Web Application — Next.js

Next.js provides server-side rendering for SEO and fast initial page loads, with client-side navigation for responsive navigation after first load. SSR matters because court data pages (judge profiles, case summaries, ruling text) should be indexable by search engines — particularly important for an open-source project that benefits from organic discovery.

### 3.4.3 Authentication & Authorization

JWT-based authentication with refresh tokens. Two login methods: email/password (with email verification) and Google OAuth. Rate limiting on login attempts. Authorization is a simple admin flag on user records — no role-based access control beyond that.

### 3.4.4 Cost Protection & Rate Limiting

Judgemind is free, but the hosted instance is self-funded. Cost protection is built into every layer with variable cost exposure.

**API rate limiting.** Generous for normal use, aggressive against abuse. Per-user limits (baseline request budget for authenticated users; unauthenticated access heavily restricted for expensive endpoints), per-API-key limits, and anti-scraping patterns (sequential enumeration, high-volume document downloads, systematic crawling are detected and throttled). The irony of an open-source scraping platform blocking scrapers is acknowledged — anyone who wants bulk access can self-host.

**AI feature cost caps** (applied when AI features ship). Per-user daily AI budget, global AI spend ceiling with automatic per-user budget reduction if approached, tiered degradation to cheaper models under cost pressure rather than hard cutoffs, and abuse detection for non-human consumption patterns.

**Admin controls.** Platform administrators can adjust per-user and global rate limits without a code deploy (configuration-driven), throttle or suspend specific users generating disproportionate cost, temporarily disable specific AI features platform-wide, and view a real-time cost dashboard.

## 3.5 Infrastructure & Operations

### 3.5.1 Hosted Instance

The primary Judgemind instance runs on AWS (us-west-2) using ECS Fargate for all compute. Fargate was chosen over EC2 or Kubernetes because it provides per-second billing with zero cluster management overhead.

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

**Domain naming convention.** Production services use bare subdomains under `judgemind.org`. Non-production environments prefix the environment name.

| Service | Production | Dev |
|---------|-----------|-----|
| Web app | `judgemind.org` | `dev.judgemind.org` |
| API     | `api.judgemind.org` | `dev.api.judgemind.org` |

The pattern is `{env}.{service}.judgemind.org` for non-production and `{service}.judgemind.org` for production. This keeps the environment as a prefix, avoiding DNS conflicts where a parent subdomain's CNAME (e.g. `dev.` pointing to Vercel) would affect child subdomains (e.g. CAA record inheritance).

### 3.5.2 Local Development

`docker-compose.yml` provides the full dependency stack for local development: PostgreSQL 16, Redis 7, OpenSearch 2.12, and MinIO (S3-compatible). Self-hosted production deployment is not a design goal.

### 3.5.3 Monitoring & Observability

Monitoring uses CloudWatch for infrastructure metrics and alarms, GitHub Actions for automated data quality checks, and PostgreSQL for metrics storage. No Prometheus or Grafana — CloudWatch is the natural fit for ECS Fargate, and GitHub Actions workflows provide higher-level checks that integrate directly with the issue tracker.

**CloudWatch alarms.** Metric filters extract key signals from ECS task logs and ALB metrics:

| Alarm | Trigger | Severity |
|---|---|---|
| Scraper no success 24h | No `scraper_run_complete` log in 24h | Critical (data loss risk) |
| Ingestion worker crash loop | 3+ crashes in 15 min | Critical |
| API 5xx errors | ≥10 errors in 5 min (prod) | High |
| API P99 latency | ≥5s P99 response time | Medium |
| Data quality check stale | Hourly DQ check hasn't run in 2h | Medium |
| API unhealthy hosts | ALB target health check failures | High |

Alarms publish to SNS topics (`judgemind-scraper-alerts-{env}`) which deliver to email.

**Automated data quality checks.** Two GitHub Actions workflows run hourly:

- **Data quality check** (`.github/workflows/data-quality-check.yml`, every hour at :15): runs `scripts/data-quality-check.py` on dev via ECS. Checks ingest rate drops, scraper staleness, zero rulings, field completeness regressions, orphaned documents. Persists per-county metrics to the `data_quality_metrics` DB table for dashboard display. Sends Telegram notifications for P1 alerts only.
- **Site quality check** (`.github/workflows/site-quality-check.yml`, every hour at :30): validates `dev.judgemind.org` pages load with expected content and API GraphQL endpoint responds with expected shapes. Auto-creates/closes `site-quality-failure` issues.

**Data quality dashboard.** An admin dashboard at `/admin/data-quality` shows per-county health status: county health tiles (green/yellow/red based on ruling count, field completeness, scraper freshness), 7-day time-series of key metrics, and drill-down with full metric history.

**ECS Container Insights** is enabled on all ECS clusters, providing automatic CPU, memory, network, and task health metrics.

**Log retention.** Dev: 14 days. Production: 30 days. All ECS tasks write structured logs to CloudWatch log groups (`/ecs/judgemind-{service}-{env}`).

### 3.5.4 Testing Strategy

Testing a system that depends on live, external court websites presents a unique challenge. Court websites change without notice, and scraper correctness can only be verified against real court data. Judgemind's testing strategy uses the archived court pages it captures during normal operation as a regression test corpus.

**Scraper testing.** Since the ingestion pipeline archives every page it captures, these archived pages form a natural regression test corpus. Three layers:

- **Baseline snapshot corpus.** For each court, a representative sample of archived pages (50–100 pages covering typical rulings, edge cases, holidays, multi-tentative cases, and clerk formatting variations). Each snapshot is paired with its "golden" extraction output.
- **Regression testing on scraper changes.** When a scraper is modified, it runs against the full snapshot corpus for its court. Output is compared to golden. Discrepancies are reviewed: correct handling of a previously failing case → update golden; break on a previously passing case → reject or revise the change.
- **Edge case fixtures.** Particularly tricky pages are tagged as permanent fixtures: multiple tentatives per case, holiday schedules, typo corrections, unusual clerk formatting, CAPTCHAs, pages from site redesigns.

**Application testing.** Unit tests for the data access layer, entity resolution, rate limiting, and auth. Integration tests that push a captured document through the ingestion pipeline and verify the final database state. GraphQL schema testing and TypeScript type checking (`tsc --noEmit`).

**CI/CD.** GitHub Actions runs the full test suite on every pull request. Scraper regression tests run when scraper code is modified. The test suite must pass before merge.

### 3.5.5 Backup & Disaster Recovery

Judgemind has an unusual backup requirement: most of its data is public court records that could theoretically be re-scraped, but tentative rulings and other ephemeral data cannot be re-acquired once the court takes them down. Losing the tentative ruling archive means losing data that is irreplaceable.

**Document archive (critical).** The S3 document bucket is the most critical data to protect. Object versioning protects against accidental deletion or overwrite. Cross-region replication to a second region — if the primary region suffers a catastrophic failure, the archive survives. This is the one area where the cost of redundancy is justified regardless of budget pressure.

**PostgreSQL (mixed).** Daily snapshots with PITR, 30 days daily / 12 months weekly retention, regular restoration tests. Impact varies by namespace: loss of `public.*` is user-facing; loss of `derived.*` is a cost event (re-running ingestion); loss of `telemetry.*` is a monitoring gap. For `derived.*` drift or corruption, the preferred remediation is a county-scoped rebuild (`scripts/rebuild_db.py --county <name>`) rather than surgical mutation — rebuild exercises the real ingestion and enrichment pipeline, so it simultaneously validates any upstream fix and backfills existing rows.

**OpenSearch (rebuildable).** OpenSearch indices are derived from PostgreSQL and the document archive. They can be fully rebuilt from source. AWS-managed OpenSearch has automated snapshots.

# 4. Direction — Planned, Not Yet Built

This section describes work that is planned or aspirational. Nothing here is implemented, running, or exercised by users today. When any of these ships and is verified end-to-end in a deployed environment, move it to §3.

## 4.1 Validation Agent

A lightweight LLM-based validation step between enrichment and database write. The goal is to catch data quality issues that regression tests and volume-based monitoring miss — particularly scrapers that run successfully but produce incorrect output (e.g., text assigned to the wrong case; ruling content from one entry overwriting another).

**Design.** The ingestion worker, after transcription and enrichment, would pass each document's extracted fields through a validation LLM call before writing to the database. The validator checks internal consistency (does the ruling text plausibly match the assigned case number and title?), field plausibility (is the judge name actually a name, not a court division header? Is the motion type a recognized legal motion?), cross-document consistency within multi-case PDFs (are all entries accounted for? Do entry counts match expected patterns for this court?), and court-specific learned rules (expected volume ranges per department, typical case number formats).

**Outcomes.** Pass → write to production normally. Flag → write to production but tag for async review in the admin dashboard. Fail → do not write; log the failure with full context; create a high-priority review item. No manual approval gate — at hundreds of documents per day, all validation is automated. Flagged items are reviewed asynchronously and feed back into scraper improvements.

**Cost target.** A cheap model (Haiku-class or Flash Lite), short prompt (extracted fields + ruling text excerpt, not full text). At ~1,000 documents/day, incremental LLM cost should be modest relative to existing enrichment costs.

**Implementation approach.** Add validation as a step in the ingestion worker between enrichment and database write. No separate service or staging schema needed — the worker already has all the context. Validation results logged to a `validation_results` table (already exists in the `telemetry.*` namespace) for monitoring and the admin dashboard.

## 4.2 Vector Search and Semantic Retrieval

User-facing AI features (semantic search, RAG-grounded document summarization, case assessment) require a semantic retrieval layer that does not exist today.

**Components planned:**

- **Embedding generation.** A Tier 1 enrichment step that generates vector embeddings for each document and ruling. Cached permanently (embeddings never change for a fixed document).
- **Vector store (Qdrant).** Stores document embeddings for similarity search with metadata filtering. Qdrant is already in `docker-compose.yml` from earlier aspirational work, but nothing in production writes to or reads from it — it is not deployed in AWS. When semantic search ships, Qdrant (or whichever vector DB is chosen at that point) will be deployed and populated by the ingestion worker.
- **RAG pipeline.** The pipeline design: retrieve relevant documents via semantic similarity → assemble context with source attribution → generate with citation requirements → verify citations against source material. This is a prerequisite for user-facing generative AI features (document summarization, case assessment, etc.).
- **Embedding-based entity resolution.** Current entity resolution (§3.2.1) uses normalized name matching only. Embedding-based resolution would improve judge/attorney disambiguation across formatting variants.

**Sequencing.** Embedding generation must ship first. Vector store deployment and RAG pipeline follow.

## 4.3 Ingestion Enhancements

### 4.3.1 Version Classification

LLM-based classification of content-hash mismatches as substantive (content changed meaningfully) vs cosmetic (whitespace, typo fix). Would let the admin dashboard surface only substantive changes and avoid noise from minor reformatting.

### 4.3.2 Ruling Summarization

One-paragraph summaries generated at ingestion time using Claude Haiku. The code exists (`packages/scraper-framework/src/ingestion/ruling_summarizer.py`) and is gated behind `ENABLE_RULING_SUMMARIZATION`, currently off in production pending cost and quality evaluation before rollout. Summaries would be cached in `rulings.summary`.

### 4.3.3 Archive-First for External Data Sources

Two data sources are currently integrated but do not archive raw content to S3 before processing:

- **Ballotpedia** (`packages/scraper-framework/src/courts/ca/ballotpedia.py`): fetches judge biographical data. A future `external/ballotpedia/{content_hash}.html` prefix is planned.
- **State bar associations**: not yet integrated. When added, raw responses should be archived under `external/{source}/{content_hash}.html`.

The planned `external/{source}/{content_hash}.html` prefix will follow the same content-addressed scheme as ruling archives.

## 4.4 Public REST API

The current REST surface is minimal (two document-access endpoints — see §3.4.1). A comprehensive public REST API (resource endpoints, OpenAPI docs, versioning, webhooks) is a future consideration if third-party integration demand materializes. For now, GraphQL serves all needs.

## 4.5 Predictive Models

Motion-outcome prediction based on case features, judge, and precedent. Deferred until 18+ months of clean data accumulation. The prediction system will require its own architecture review when the time comes.

# 5. Security & Trust

Cross-cutting; applies to the Today implementation and any Direction work that ships.

Judgemind handles public court data, but user accounts require standard security practices. Secrets are stored in AWS Secrets Manager. All traffic is TLS-terminated. JWT tokens have short expiry with refresh token rotation.

# 6. Open Questions

**Tier 1 self-hosting threshold.** The crossover point where self-hosted GPU becomes cheaper than hosted API calls depends on actual document volume and per-document token counts. Currently using Google GenAI (Flash Lite) for transcription and Claude Haiku for enrichment — both cheap enough that self-hosting is not justified at current scale. Phase 2 evaluation: if Tier 1 costs exceed ~$3,000/month (indicating ~10,000+ documents/day), consider a single A100 instance (~$1,500/month) running Llama or Mistral.

**When to ship semantic search.** Depends on whether full-text search (OpenSearch) proves sufficient for user needs or whether users demand semantic/RAG features strongly enough to justify the embedding generation + vector DB cost and complexity.

**Predictive model architecture.** Deferred until 18+ months of data accumulation. Architecture review happens when the time comes.

End of document.
