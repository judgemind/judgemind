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

Open source and self-hostable. Architecture should favor open-source and commodity components where practical. Managed cloud services are acceptable for the hosted instance. The open-source codebase should be deployable by a third party, but this is an aspiration that should not drive significant additional complexity. If a proprietary service is clearly the best tool for a job, use it and document the self-hosted alternative without over-engineering an abstraction layer.

# 2. System Overview

Judgemind consists of five major subsystems connected by event-driven messaging. Each subsystem can be scaled, deployed, and developed independently.

## 2.1 Event-Driven Architecture

The five subsystems communicate through an event bus implemented with Redis Streams. Redis is already in the stack for caching and rate limiting (see Section 2.2), so using Redis Streams for messaging avoids adding another service. The event volume (thousands of documents per day, not millions) is well within Redis Streams’ capabilities.

### 2.1.1 Event Flow

Data flows through the system in a pipeline pattern. Each stage produces events consumed by the next stage, with the event bus decoupling producers from consumers so each can scale and fail independently.

document.captured: Emitted by a scraper when it captures a new or updated document. Payload includes raw content, content hash, source URL, court/county/state metadata, and capture timestamp. Consumed by the validation agent (Section 3.5) and the document processing pipeline.

document.validated: Emitted by the validation agent after reviewing a captured document. Payload includes the validation result (passed, failed, flagged) and any validation notes. Passed documents are consumed by the NLP pipeline. Failed documents enter the review queue.

document.processed: Emitted by the NLP pipeline after entity extraction, classification, summarization, and embedding generation are complete. Payload includes all extracted structured data. Consumed by the indexer, which writes to PostgreSQL, Elasticsearch, and Qdrant.

document.indexed: Emitted after the document and its structured data are written to all production data stores. Consumed by the alert evaluation service, which checks the new data against all active alert subscriptions.

alert.triggered: Emitted when a new document matches an active alert subscription. Payload includes the alert ID, user ID, and the matching document reference. Consumed by the alert aggregation service, which collects these events and produces the daily digest email.

scraper.health: Emitted by each scraper after every run with operational metrics (success/failure, response time, records captured). Consumed by the health monitoring dashboard and the alerting system for scraper failures.

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

Output: Emit standardized ingestion events to the message queue. Events include raw content, parsed content, content hash, source metadata, and capture timestamp.

Error handling: Retry with exponential backoff. Alert on repeated failures. Log all errors with enough context for debugging (URL, response status, partial content).

Health reporting: Each scraper reports its last successful run, last failure, and current status to a central registry.

### 3.1.1 Lessons from Prior Implementation

The following design decisions are informed by hard-won experience building the original state court scraping infrastructure in 2016:

Assume nothing about data consistency. Different clerks enter data differently within the same court. Holiday schedules create unexpected entries. Typo corrections appear as updates. The scraper must treat every assumption about format as provisional and handle deviations gracefully.

Version tracking with content hashing. Every captured document or ruling gets a SHA-256 content hash. When a scraper sees an update, it compares hashes. If the hash differs, both versions are stored. A downstream classifier determines whether the change is substantive (revised ruling) or cosmetic (typo fix). This was a significant pain point in the original system and is now solvable with LLM-based diffing.

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

Deduplication: Content hashing distinguishes genuinely revised rulings from cosmetic changes. When a hash mismatch is detected, an LLM-based diff classifier categorizes the change as substantive or cosmetic. Both versions are retained; cosmetic updates are linked to their parent but not surfaced as separate rulings in search results.

Failure alerting: If a tentative ruling scraper fails for more than 24 hours, an alert fires. Tentative ruling capture failures are treated as high-severity incidents because the data may be permanently lost.

## 3.4 External Data Integration

Judgemind integrates with existing open legal data sources to avoid duplicating effort and to provide immediate federal court coverage:

CourtListener (Free Law Project): Federal opinions, some state appellate data. Provides significant baseline coverage from day one. Integration via their API and bulk data downloads.

RECAP: Federal court documents from PACER. Community-contributed archive. Integrate as a first-class document source.

Public records: Bar association records, court directories, judicial appointment records. Used primarily for judge and attorney profile data.

## 3.5 Scraper Development & Validation Lifecycle

Building a reliable scraper is not a single event. In the original implementation, each scraper required days to weeks of iteration before reaching production-grade accuracy. Court websites are messy, inconsistent, and full of edge cases that only surface over time. Judgemind uses AI validation agents to compress this iteration cycle and to provide ongoing quality assurance after a scraper reaches steady state.

### 3.5.1 Lifecycle Phases

Every scraper progresses through four phases. Each phase has different validation requirements and different thresholds for promoting data to the production index.

Phase 1 — Initial development. An AI development agent writes the scraper based on the court website structure. It runs against the live site and captures data into a staging area. An AI validation agent reviews every output: Does this look like a tentative ruling? Are the extracted fields populated sensibly? Is the judge name actually a name? Is the date plausible? Are there parsing failures (HTML tags in ruling text, truncated content, garbled encoding)? The validation agent catches gross errors immediately rather than waiting for a human to review the next morning. Failed validations generate specific, actionable feedback to the development agent, which iterates on the scraper.

Phase 2 — Burn-in (first 1–2 weeks). The scraper runs daily in production but all output remains in staging. The validation agent now compares across runs: Is the capture volume consistent day over day? Did we suddenly get zero results from a court that had 15 yesterday (site changed, got blocked, or weekend/holiday pattern)? Did we capture a ruling that looks like a duplicate with slightly different text (typo correction vs. substantive revision)? Are there entries that don’t match the expected pattern (holiday entries, clerk-specific formatting, unexpected document types)? Each anomaly the validation agent surfaces is an edge case that gets folded into the scraper logic. Data from this phase is promoted to production after manual review confirms the scraper is handling the court correctly.

Phase 3 — Production with active validation. The scraper is producing reliable data and output is promoted to the production index automatically. The validation agent continues reviewing every capture but shifts from blocking promotion to flagging anomalies for asynchronous review. Anomaly types include: unexpected drops or spikes in volume, new parsing errors, documents that fail NLP classification, entity names that don’t resolve, and content that deviates from established patterns for that court.

Phase 4 — Steady state with periodic spot checks. The scraper has been running reliably for weeks or months. Automated validation continues, but an AI spot-check agent also performs deeper audits on a regular schedule (e.g., weekly). The spot check selects a random sample of recently captured data and performs a thorough review: visit the original court website, confirm the captured data matches the source, verify that entity extraction and classification are correct, and flag any drift. This catches the slow degradation that automated anomaly detection might miss — a court that gradually changes its HTML structure, a new judge whose name format doesn’t match existing patterns, or a subtle parsing error that produces plausible but wrong output.

### 3.5.2 Validation Architecture

Scraped data never goes directly into the production data store. All output flows through a staging area where validation occurs before promotion.

Staging area: A separate database schema (or table partition) that mirrors the production data model. Scrapers write here. Data is tagged with scraper ID, capture timestamp, and validation status (pending, passed, failed, flagged).

Validation agent: An AI agent (Tier 1 model, low cost per evaluation) that reviews staged data against a set of validation rules. Rules are both generic (is this valid text? is this date in the past?) and court-specific (this court typically publishes 5–20 tentatives per day; this court uses a specific format for case numbers). Court-specific rules are learned during the burn-in phase and refined over time.

Promotion: Data that passes validation is promoted to the production PostgreSQL database and Elasticsearch index. During Phases 1–2, promotion requires manual approval. In Phases 3–4, promotion is automatic for data that passes validation; only flagged items require review.

Review queue: Failed and flagged validations enter a review queue visible in the admin dashboard. Each item shows the scraped data, the validation failure reason, and suggested remediation. An AI development agent can pick up items from this queue, diagnose the root cause, and propose scraper fixes.

Spot-check agent: A separate scheduled agent that performs deeper audits on production data. It selects a random sample (e.g., 5–10 records per scraper per week), revisits the original source, and compares the captured data against the live court website. Discrepancies are logged with full context and enter the review queue. This provides ongoing confidence that production data remains accurate even for scrapers that have been running without incident for months.

### 3.5.3 Scraper Health Model

A scraper that runs without errors but returns wrong data is more dangerous than one that crashes, because a crash is immediately visible. The health model tracks both operational status and output quality:

Operational health: Did the scraper run? Did it complete without errors? What was the response time from the court website? Standard uptime monitoring.

Output health: Is the volume of captured data within the expected range for this court and day of week? What percentage of records passed validation? Are any extracted fields consistently empty or malformed? Is the entity resolution match rate stable?

Spot-check health: What is the accuracy rate from the most recent spot checks? A scraper with a declining spot-check accuracy rate is flagged for investigation even if automated validation is passing, since this may indicate that the validation rules themselves have drifted.

Composite score: Each scraper gets a composite health score combining all three dimensions. The admin dashboard shows scrapers ranked by health score with the unhealthiest at the top, so attention goes where it is most needed.

# 4. Data Store

Judgemind uses four complementary storage systems, each optimized for a specific access pattern.

## 4.1 PostgreSQL — Structured Data

PostgreSQL is the primary database for all structured, relational data. It stores the core entities and their relationships.

### 4.1.1 Core Entity Model

The data model centers on six primary entities. The entity-relationship design must accommodate the messiness of court data, particularly around identity resolution (see Section 4.1.2).

### 4.1.2 Entity Resolution

Court data is entered by humans with no enforced consistency. The same judge may appear as "Johnson, Robert M.", "Robert Johnson", "Hon. R.M. Johnson", or "Judge Johnson" across different courts, clerks, and document types. The same is true for attorneys, parties, and firms.

Judgemind uses a multi-stage entity resolution pipeline:

Stage 1 — Exact and normalized matching. Strip titles, normalize name order, standardize punctuation. This catches the easy cases.

Stage 2 — Fuzzy matching. Embedding-based similarity matching for names that are close but not identical. Uses a fine-tuned model trained on court data name variations. Threshold-based: high-confidence matches are auto-linked; borderline matches are queued for human review.

Stage 3 — Contextual disambiguation. When names are common (e.g., "Judge Smith"), use contextual signals: court, county, case type, time period, bar number (for attorneys). Two "Judge Smiths" in different counties are different people.

Canonical records. Each resolved entity gets a canonical record with a stable ID. All name variants are linked to the canonical record as aliases. All downstream analytics operate on canonical IDs, not raw name strings.

## 4.2 Elasticsearch — Full-Text Search

Elasticsearch indexes all text content for fast, full-text search with relevance ranking. It is the engine behind docket search, document search, ruling search, and the smart search bar.

Index per document type: Separate indices for rulings, motions, briefs, docket entries, and other document types. This allows type-specific relevance tuning and faceted search.

Analyzers: Custom analyzers for legal text: legal citation recognition, case number normalization, party name matching, motion type standardization.

Faceted search: Aggregations for filtering by jurisdiction, judge, case type, date range, motion type, party, and attorney. These power the advanced filter UI.

Sync: Elasticsearch is populated from PostgreSQL via change data capture (CDC) or an event-driven indexer. PostgreSQL is the source of truth; Elasticsearch is a derived read-optimized view.

## 4.3 Qdrant — Vector Search

Qdrant stores document embeddings for semantic search and powers the RAG (retrieval-augmented generation) pipeline for all AI features.

Why Qdrant over pgvector: At the volume Judgemind targets (millions of documents), a dedicated vector database provides significantly better search performance, filtering capabilities, and scalability than pgvector embedded in PostgreSQL. Qdrant is open-source and self-hostable, consistent with our architecture principles.

Embedding model: Use an open-source embedding model (e.g., BGE, GTE, or Nomic) for cost-predictable, self-hostable embedding generation. Embeddings generated during the ingestion pipeline and stored in Qdrant with metadata for filtered search.

Collections: Separate collections for rulings, motions, briefs, and statutes. Metadata-filtered search enables queries like "find rulings semantically similar to this motion, limited to this judge and jurisdiction."

## 4.4 Object Storage — Documents & Archival

S3-compatible object storage (AWS S3, MinIO for self-hosted, or equivalent) stores all original documents and archival copies.

Immutable archival: Original captured documents are never modified or deleted. Object versioning enabled for an additional safety net.

Tiered storage: Hot storage for documents less than 90 days old or frequently accessed. Cold/archive storage for older documents. Lifecycle policies automate transitions.

Path convention: /{state}/{county}/{court}/{case_id}/{document_type}/{document_id}.{ext} for predictable organization and efficient prefix-based listing.

# 5. AI/ML Layer

The AI layer handles all natural language processing, from ingestion-time entity extraction to user-facing generative features. It is designed around three processing tiers with different cost, quality, and volume characteristics.

## 5.1 Processing Tiers

### 5.1.1 Cost Management Strategy

The guiding principle is: start simple, measure, then optimize. Do not prematurely invest in GPU infrastructure before understanding actual usage patterns.

Phase 1 (Months 1–6): All hosted APIs. Use small/cheap models for Tier 1 (Haiku-class). Monitor per-document ingestion cost carefully. Cache all Tier 1 outputs so they never need to be recomputed.

Phase 2 (Months 6–12): If Tier 1 costs exceed ~$3,000/month (indicating ~10,000+ documents/day), evaluate self-hosted GPU. A single A100 instance at ~$1,500/month running Llama or Mistral can handle Tier 1 at any realistic volume.

Ongoing: Tier 2 and Tier 3 remain on hosted commercial APIs indefinitely. Their per-call costs scale with users, not data volume, and quality requirements justify premium models. Rate limiting on AI features prevents runaway costs.

## 5.2 NLP Pipeline (Tier 1)

Every ingested document passes through the NLP pipeline during processing. Outputs are stored in PostgreSQL (structured fields), Elasticsearch (text index), and Qdrant (embeddings). This means user-facing features can serve pre-computed results rather than making live API calls.

Entity extraction: Identify and normalize judges, attorneys, parties, dates, monetary amounts, case numbers, and statute references. Output feeds the entity resolution system (Section 4.1.2).

Document classification: Classify document type (motion, brief, ruling, order, complaint, etc.), motion type (MSJ, MTD, MIL, etc.), and ruling outcome (granted, denied, partial, moot, etc.).

Summarization: Generate a one-paragraph summary of each document at ingestion time. Cached for instant retrieval when users view the document.

Embedding generation: Generate vector embeddings using an open-source model. Stored in Qdrant for semantic search and RAG retrieval.

Version classification: When a document or ruling is re-captured with a different content hash, classify the change as substantive or cosmetic using LLM-based diffing.

## 5.3 RAG Pipeline (Tiers 2 & 3)

All user-facing generative AI features are grounded in actual court documents via retrieval-augmented generation. The RAG pipeline is the bridge between the vector store and the LLM.

Retrieval: Given a user query or document context, retrieve the most relevant documents from Qdrant using semantic similarity, filtered by jurisdiction, judge, case type, and date range as appropriate.

Context assembly: Selected documents are chunked and assembled into an LLM context window with source attribution. Each chunk carries metadata (source document, page, case, judge) so citations can be traced.

Generation: The LLM generates output grounded in the retrieved context. System prompts require citation of sources for every factual claim.

Citation verification: Post-generation, citations are verified against the source material. Any hallucinated or inaccurate citation is flagged or removed before the user sees the output.

## 5.4 Human-in-the-Loop

AI outputs are not authoritative. The platform includes feedback mechanisms at every AI touchpoint:

Feedback buttons: Every AI-generated output (summary, assessment, draft) has thumbs-up/thumbs-down and a correction field. Reviewed outputs feed back into quality tracking.

Review queues: Judge biographies, motion playbooks, and entity resolution borderline matches are queued for human review. Reviewed items are marked with the reviewer and review date.

Confidence scoring: AI outputs include a confidence indicator. Low-confidence outputs are visually distinguished and may be queued for priority review.

# 6. Application Layer

## 6.1 API Architecture — Dual API Pattern

Judgemind exposes two API surfaces, both backed by the same data access layer. This ensures consistency while optimizing each API for its audience.

### 6.1.1 GraphQL API (Internal, Frontend)

The Next.js frontend communicates exclusively with the GraphQL API. GraphQL is the right fit for Judgemind’s data model because:

Different views need different data slices. A case detail page, a judge profile page, and a search results page all query the same underlying entities but need different fields and relationships. GraphQL lets the frontend request exactly what it needs in one round trip.

Legal data is deeply relational. A single case involves a judge, multiple attorneys, multiple parties, docket entries, documents, rulings, and motions. GraphQL’s nested query structure maps naturally to this.

Performance: Eliminates the "chatty API" problem. A case detail page that would require 5–7 REST calls can be served in a single GraphQL query. This directly supports the requirement for a fast, responsive UI.

### 6.1.2 REST API (Public, Third-Party)

The public REST API is designed for third-party developers, legal tech integrations, law school researchers, and programmatic access. It follows standard REST conventions:

Resource-oriented endpoints: /cases, /judges, /attorneys, /documents, /rulings, /alerts.

OpenAPI 3.0 specification with auto-generated documentation.

Versioned (v1/cases, v2/cases) to allow non-breaking evolution.

Authenticated via API key. Rate-limited: generous for legitimate research use, aggressive against scraping/abuse.

Pagination, filtering, and sorting on all list endpoints.

Webhooks for event-driven integrations (new filings, new rulings, alert triggers).

### 6.1.3 Shared Data Access Layer

Both APIs sit on top of a shared data access layer that handles database queries, caching, authorization, and business logic. This ensures that a case retrieved via GraphQL and the same case retrieved via REST are always identical and subject to the same access controls.

## 6.2 Web Application — Next.js

The web application is built with Next.js, providing server-side rendering for SEO and fast initial page loads, with client-side navigation for a responsive single-page application experience after first load.

### 6.2.1 Why Next.js

SEO: Court data pages (judge profiles, case summaries, ruling text) should be indexable by search engines. Server-side rendering ensures search engines see full content. This is particularly important for an open-source project that benefits from organic discovery.

Performance: Server-side rendering means users see content on first paint without waiting for client-side JavaScript to fetch data. Combined with GraphQL, this produces a fast, responsive experience.

Developer experience: Next.js is the most widely adopted React framework with a large ecosystem. This matters for an open-source project that needs community contributors.

### 6.2.2 Real-Time Features

WebSocket connections (via Socket.io or native WebSocket) power real-time features:

Live alerts when a tracked case has new activity or a tracked judge issues a new ruling.

Collaborative case workspace updates when a team member adds annotations or research.

Ingestion status dashboard showing scraper health and recent captures.

## 6.3 Authentication & Authorization

## 6.4 Alert System

Alerts are one of the most immediately useful features and among the simplest to build.

Alert types: Case docket alerts, judge ruling alerts, keyword-based ruling alerts, party/attorney alerts.

Delivery: Daily digest email (evening delivery). Each alert produces events during the day as scrapers capture new data; the digest job collects all events and sends a single email per user per day.

Architecture: When the ingestion pipeline captures new data, it evaluates the data against all active alert subscriptions. Matching alerts generate alert events stored in PostgreSQL. A nightly cron job aggregates events into digest emails and sends via transactional email service (Postmark, SES, or similar).

Future upgrade path: If demand exists, add near-real-time push notifications (email, mobile push, or in-app) by processing alert events as they arrive rather than batching.

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

The primary Judgemind instance is deployed on a single cloud provider, optimized for cost. The initial recommendation is AWS due to mature managed services and competitive reserved pricing, but the architecture is cloud-agnostic.

## 7.2 Self-Hosted Deployment

Because Judgemind is open source, the entire platform must be deployable by a third party. The self-hosted deployment path uses Docker Compose for development and small deployments, with Kubernetes manifests available for production scale.

All components containerized with Docker.

docker-compose.yml for single-machine development/small deployment.

Helm charts for Kubernetes deployment at scale.

Environment-variable-based configuration. No hardcoded cloud-specific dependencies.

MinIO as S3-compatible object storage for self-hosted environments.

Self-hosted Elasticsearch/OpenSearch, Qdrant, PostgreSQL, and Redis.

## 7.3 Monitoring & Observability

Scraper health dashboard: Real-time status of every scraper. Last successful run, error rates, average response times. This is operationally critical because a failed scraper means lost data.

AI cost dashboard: Per-tier, per-model API cost tracking. Daily and monthly spend with alerting at configurable thresholds.

Application metrics: Standard web application metrics (response times, error rates, active users) via Prometheus + Grafana or equivalent.

Data pipeline metrics: Documents processed per day, ingestion latency, NLP pipeline throughput, entity resolution match rates.

## 7.4 Testing Strategy

Testing a system that depends on live, external court websites presents a unique challenge. The court websites change without notice, and scraper correctness can only be verified against real court data. Judgemind’s testing strategy uses the archived court pages it captures during normal operation as a regression test corpus.

### 7.4.1 Scraper Testing

Since the ingestion pipeline already archives every page it captures (raw HTML, PDFs, and DOCX files in object storage), these archived pages form a natural regression test corpus. The testing approach works in three layers:

Baseline snapshot corpus: For each court, maintain a representative sample of archived pages (50–100 pages covering typical rulings, edge cases, holidays, multi-tentative cases, and clerk formatting variations). This sample is curated during the burn-in phase as interesting edge cases are discovered. Each snapshot is paired with the expected extraction output (the “golden” output that was validated during burn-in).

Regression testing on scraper changes: When a scraper is modified (to handle a new edge case, adapt to a site redesign, or fix a bug), the modified scraper runs against the full snapshot corpus for that court. Its output is compared to the golden output. Any discrepancies are reviewed: if the scraper correctly handles a previously failing case, the golden output is updated. If it breaks a previously passing case, the change is rejected or revised.

Edge case fixtures: Particularly interesting or tricky pages are tagged as permanent test fixtures. These include: pages with multiple tentative rulings for the same case, holiday schedule entries, typo corrections (paired with the original to test version deduplication), unusual clerk formatting, CAPTCHAs or access-denied pages (to test error handling), and pages from court website redesigns (to test detection of structural changes).

### 7.4.2 Application Testing

Standard application testing applies to the API and web layers:

Unit tests: Data access layer, entity resolution logic, alert matching logic, rate limiting, and authentication. These are conventional and can use standard mocking.

Integration tests: End-to-end tests that push a captured document through the full pipeline (validation, NLP, indexing, alert evaluation) and verify the final state in all data stores. Uses Docker Compose to spin up the full stack.

API contract tests: The REST API is tested against its OpenAPI specification. The GraphQL API is tested against its schema. These ensure that API changes do not break existing consumers.

CI/CD: GitHub Actions runs the full test suite on every pull request. Scraper regression tests run as part of CI when scraper code is modified. The test suite must pass before merge.

## 7.5 Backup & Disaster Recovery

Judgemind has an unusual backup requirement: most of its data is public court records that could theoretically be re-scraped, but tentative rulings and other ephemeral data cannot be re-acquired once the court takes them down. Losing the tentative ruling archive means losing data that is irreplaceable. The backup strategy reflects this asymmetry.

### 7.5.1 Document Archive (Critical)

The S3-compatible object store containing original captured documents (especially tentative rulings) is the most critical data to protect:

Object versioning: Enabled on the document bucket. Protects against accidental deletion or overwrite.

Cross-region replication: The document archive is replicated to a second region. If the primary region suffers a catastrophic failure, the archive survives. This is the one area where the cost of redundancy is justified regardless of budget pressure.

Immutable storage: The archive bucket uses object lock (write-once-read-many) to prevent any deletion, even by administrators. Original captured documents should be permanently retained.

### 7.5.2 PostgreSQL (Important)

The PostgreSQL database contains all structured data (cases, judges, attorneys, docket entries, user accounts, alert configurations). While most of this data could be re-derived from the document archive by re-running the NLP pipeline, that would be extremely expensive and time-consuming. Standard database backup practices apply:

Automated daily snapshots with point-in-time recovery (PITR) enabled for continuous WAL archiving.

Backup retention: 30 days of daily snapshots, 12 months of weekly snapshots.

Regular backup restoration tests to verify recoverability.

### 7.5.3 Elasticsearch & Qdrant (Rebuildable)

Both Elasticsearch indices and Qdrant vector collections are derived from PostgreSQL and the document archive. They can be fully rebuilt from source if lost, though rebuilding takes time. Standard Elasticsearch snapshots to S3 provide faster recovery. Qdrant snapshots follow the same pattern. Neither requires the same level of protection as the document archive or PostgreSQL, since they are derived data stores.

# 8. Security & Trust

Judgemind handles public court data, but user accounts, uploads, and collaboration features require security appropriate for legal work product.

# 9. Technology Stack Summary

# 10. Open Questions & Decisions Deferred

The following decisions are intentionally deferred until more information is available:

Cloud provider selection. AWS is the default recommendation for managed service maturity and reserved instance pricing, but GCP and Hetzner (for raw compute cost) are viable alternatives. Decision should be made based on which provider offers the best reserved pricing for our specific compute profile.

Tier 1 self-hosting threshold. The crossover point where self-hosted GPU becomes cheaper than hosted API calls depends on actual document volume and per-document token counts. Measure for 3–6 months before committing to GPU infrastructure.

Graph database for relationship exploration. The attorney-judge-party-firm relationship web is naturally graph-shaped. A graph database (Neo4j, or Postgres with Apache AGE extension) could power features like "show me all cases where Attorney X appeared before Judge Y." This is worth evaluating once the core relational model is stable and we can assess whether PostgreSQL’s JOIN performance is sufficient for these queries.

Mobile application. The Next.js application will be responsive and mobile-friendly. A native mobile app is out of scope for initial phases but may be warranted if usage patterns show significant mobile traffic.

Community contribution pipeline. The product spec envisions firms and practitioners contributing historical data. The technical pipeline for accepting, validating, and integrating contributed data needs its own design document.

Predictive model architecture. Deferred until 18+ months of data accumulation per the product spec. The prediction system will require its own architecture review when the time comes.

End of Document