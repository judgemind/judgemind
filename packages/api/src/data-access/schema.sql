-- =============================================================================
-- Judgemind PostgreSQL Schema — Core Entity Model
-- =============================================================================
-- This file is loaded automatically by docker-compose for local development
-- (mounted at /docker-entrypoint-initdb.d/01-schema.sql).
--
-- For production, use the node-pg-migrate migrations in packages/api/migrations/.
--
-- Entity model: courts, judges, attorneys, parties, cases, documents, rulings.
-- Entity resolution: canonical records with alias tables for name variants.
-- Staging area: pre-validation captures before promotion to production.
-- =============================================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Staging schema for pre-validation data (never goes directly to production)
CREATE SCHEMA IF NOT EXISTS staging;


-- =============================================================================
-- REFERENCE DATA
-- =============================================================================

CREATE TABLE courts (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identifiers
    state           CHAR(2)     NOT NULL,           -- 'CA', 'TX', etc.
    county          TEXT        NOT NULL,            -- 'Los Angeles', 'Orange', etc.
    court_name      TEXT        NOT NULL,            -- Full official name
    court_code      TEXT        UNIQUE NOT NULL,     -- 'ca-la', 'ca-orange' — used in S3 paths
    -- Operational
    timezone        TEXT        NOT NULL DEFAULT 'America/Los_Angeles',
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    scraper_config  JSONB,                           -- Court-specific scraper configuration
    -- Timestamps
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  courts               IS 'Reference table for jurisdictions. One row per court.';
COMMENT ON COLUMN courts.court_code    IS 'Slug used in S3 key paths: /{state}/{county}/{court}/...';
COMMENT ON COLUMN courts.scraper_config IS 'Scraper-specific config: rate limits, time windows, auth hints.';


-- =============================================================================
-- CANONICAL ENTITIES — JUDGES
-- Entity resolution: canonical record + alias table for name variants.
-- =============================================================================

CREATE TABLE judges (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name      TEXT        NOT NULL,        -- Normalized canonical form
    court_id            UUID        NOT NULL REFERENCES courts(id),
    department          TEXT,                        -- 'Dept. 1', 'Dept. H', etc.
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    appointed_at        DATE,
    biographical_notes  TEXT,                        -- From public records
    bio_reviewed_at     TIMESTAMPTZ,                 -- Last verified
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE judge_aliases (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    judge_id    UUID    NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    raw_name    TEXT    NOT NULL,                    -- Name as it appeared in source data
    source      TEXT,                                -- Which court/document this came from
    confidence  FLOAT   CHECK (confidence >= 0 AND confidence <= 1),
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  judges                IS 'Canonical judge records. One row per unique judge.';
COMMENT ON COLUMN judges.canonical_name IS 'Normalized form: "Johnson, Robert M." — used for all analytics.';
COMMENT ON TABLE  judge_aliases         IS 'All name variants that resolve to a canonical judge.';
COMMENT ON COLUMN judge_aliases.confidence IS '1.0 = manual verification, <1.0 = fuzzy/embedding match.';


-- =============================================================================
-- CANONICAL ENTITIES — ATTORNEYS
-- =============================================================================

CREATE TABLE attorneys (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  TEXT        NOT NULL,
    bar_number      TEXT,                            -- State bar number
    bar_state       CHAR(2),                         -- State of bar admission
    firm_name       TEXT,
    email           TEXT,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE attorney_aliases (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    attorney_id     UUID    NOT NULL REFERENCES attorneys(id) ON DELETE CASCADE,
    raw_name        TEXT    NOT NULL,
    source          TEXT,
    confidence      FLOAT   CHECK (confidence >= 0 AND confidence <= 1),
    is_verified     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON COLUMN attorneys.bar_number IS 'Used as a strong deduplication signal during entity resolution.';


-- =============================================================================
-- CANONICAL ENTITIES — PARTIES
-- =============================================================================

CREATE TABLE parties (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  TEXT        NOT NULL,
    party_type      TEXT,                            -- 'individual', 'corporation', 'government', etc.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE party_aliases (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    party_id    UUID    NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    raw_name    TEXT    NOT NULL,
    source      TEXT,
    confidence  FLOAT   CHECK (confidence >= 0 AND confidence <= 1),
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- CASES
-- =============================================================================

CREATE TABLE cases (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Identifiers
    case_number             TEXT        NOT NULL,    -- Raw from court
    case_number_normalized  TEXT,                    -- Stripped of punctuation/spaces for search
    court_id                UUID        NOT NULL REFERENCES courts(id),
    -- Classification
    case_type               TEXT,                    -- 'civil', 'criminal', 'family', 'probate'
    case_subtype            TEXT,                    -- 'unlimited civil', 'limited civil', etc.
    case_status             TEXT,                    -- 'active', 'closed', 'dismissed'
    case_title              TEXT,                    -- 'Smith v. Jones', 'People v. Doe'
    -- Dates
    filed_at                DATE,
    closed_at               DATE,
    -- Timestamps
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (court_id, case_number)
);

-- Which judges are assigned to a case (may change over case lifetime)
CREATE TABLE case_judges (
    case_id     UUID    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    judge_id    UUID    NOT NULL REFERENCES judges(id),
    assigned_at DATE,
    is_current  BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (case_id, judge_id)
);

-- Which attorneys appear in a case and in what role
CREATE TABLE case_attorneys (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id     UUID    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    attorney_id UUID    NOT NULL REFERENCES attorneys(id),
    role        TEXT    NOT NULL,    -- 'plaintiff_counsel', 'defense_counsel', 'amicus', etc.
    party_id    UUID    REFERENCES parties(id),   -- Which party they represent
    appeared_at DATE,
    withdrew_at DATE
);

-- Which parties appear in a case and in what role
CREATE TABLE case_parties (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id     UUID    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    party_id    UUID    NOT NULL REFERENCES parties(id),
    role        TEXT    NOT NULL     -- 'plaintiff', 'defendant', 'cross-defendant', 'respondent', etc.
);

COMMENT ON TABLE cases                    IS 'Court cases. case_number + court_id is the unique key.';
COMMENT ON COLUMN cases.case_number_normalized IS 'Normalized for dedup and search (no punctuation, lowercase).';


-- =============================================================================
-- DOCUMENTS
-- Every captured document — rulings, motions, briefs, docket entries, etc.
-- The S3 object is the irreplaceable primary asset; this row is the index.
-- =============================================================================

CREATE TYPE document_format AS ENUM ('html', 'pdf', 'docx', 'txt');
CREATE TYPE document_status AS ENUM ('active', 'superseded', 'removed');

CREATE TABLE documents (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Case linkage (nullable: captured before case entity is resolved)
    case_id             UUID                REFERENCES cases(id),
    court_id            UUID                NOT NULL REFERENCES courts(id),
    -- Classification
    document_type       TEXT                NOT NULL,   -- 'ruling', 'motion', 'brief', 'docket_entry', 'order'
    motion_type         TEXT,                           -- 'msj', 'mtd', 'mil', 'demurrer', etc.
    -- Object storage
    s3_key              TEXT                NOT NULL,   -- Full S3 key: /{state}/{county}/{court}/{case_id}/...
    s3_bucket           TEXT                NOT NULL,
    format              document_format     NOT NULL,
    -- Content integrity
    content_hash        TEXT                NOT NULL,   -- SHA-256 of raw content
    -- Provenance
    source_url          TEXT,
    scraper_id          TEXT,                           -- e.g., 'ca-la-tentative'
    captured_at         TIMESTAMPTZ         NOT NULL,
    -- Scheduling context
    hearing_date        DATE,
    published_at        TIMESTAMPTZ,                    -- When court published (if known)
    -- Versioning: rulings get revised; we keep all versions
    status              document_status     NOT NULL DEFAULT 'active',
    previous_version_id UUID                REFERENCES documents(id),
    change_type         TEXT,                           -- 'substantive' or 'cosmetic' (LLM-classified)
    -- Timestamps
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  documents              IS 'Index of all captured documents. S3 object is the source of truth.';
COMMENT ON COLUMN documents.s3_key       IS 'Path: /{state}/{county}/{court}/{case_id}/{doc_type}/{doc_id}.{ext}';
COMMENT ON COLUMN documents.content_hash IS 'SHA-256. Used for dedup and to detect revisions across scraper runs.';
COMMENT ON COLUMN documents.change_type  IS 'LLM-classified when a new version is captured: substantive or cosmetic.';


-- =============================================================================
-- RULINGS — highest-priority entity
-- Tentative rulings are ephemeral; once captured they must never be lost.
-- =============================================================================

CREATE TYPE ruling_outcome AS ENUM (
    'granted',
    'denied',
    'granted_in_part',
    'denied_in_part',
    'moot',
    'continued',
    'off_calendar',
    'submitted',
    'other'
);

CREATE TABLE rulings (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Linkage
    document_id     UUID            NOT NULL REFERENCES documents(id),
    case_id         UUID            NOT NULL REFERENCES cases(id),
    judge_id        UUID            REFERENCES judges(id),   -- Nullable until entity resolution runs
    court_id        UUID            NOT NULL REFERENCES courts(id),
    -- Ruling content
    ruling_text     TEXT,                   -- Extracted plain text
    ruling_text_html TEXT,                  -- Original HTML (preserved for re-parsing)
    outcome         ruling_outcome,
    motion_type     TEXT,
    -- Scheduling
    hearing_date    DATE            NOT NULL,
    posted_at       TIMESTAMPTZ,            -- When the tentative was posted by the court
    -- NLP outputs (pre-computed at ingestion time, cached for instant retrieval)
    summary                 TEXT,           -- AI-generated one-paragraph summary
    summary_model           TEXT,           -- e.g., 'claude-haiku-4-5'
    summary_generated_at    TIMESTAMPTZ,
    -- Metadata
    department      TEXT,                   -- 'Dept. 1', 'Dept. H', etc.
    is_tentative    BOOLEAN         NOT NULL DEFAULT TRUE,  -- TRUE = tentative, FALSE = final ruling
    ruling_number   INTEGER,                -- Some courts number rulings within a day
    -- Timestamps
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  rulings             IS 'Tentative and final rulings. is_tentative=TRUE is the primary data type.';
COMMENT ON COLUMN rulings.judge_id    IS 'Nullable: populated by entity resolution after initial capture.';
COMMENT ON COLUMN rulings.ruling_text IS 'Extracted text. Never modify — re-parse from document_id if needed.';
COMMENT ON COLUMN rulings.summary     IS 'Cached AI summary. Never re-generated; served from cache on every request.';


-- =============================================================================
-- STAGING AREA — pre-validation data
-- Scrapers write here. Data is promoted to production only after validation.
-- Architecture spec Section 3.5.2.
-- =============================================================================

CREATE TYPE validation_status AS ENUM ('pending', 'passed', 'failed', 'flagged');

-- Raw captures from scrapers — before any parsing or validation
CREATE TABLE staging.captures (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Provenance (court_id not FK-constrained: court may not exist yet in production)
    court_id            UUID                NOT NULL,
    scraper_id          TEXT                NOT NULL,   -- e.g., 'ca-la-tentative'
    source_url          TEXT                NOT NULL,
    -- Raw content
    raw_content         TEXT                NOT NULL,   -- Full HTML or base64-encoded binary
    raw_content_type    TEXT                NOT NULL,   -- 'text/html', 'application/pdf'
    content_hash        TEXT                NOT NULL,   -- SHA-256; used for dedup
    capture_metadata    JSONB,                          -- Court-specific metadata from scraper
    captured_at         TIMESTAMPTZ         NOT NULL,
    -- Validation lifecycle
    validation_status   validation_status   NOT NULL DEFAULT 'pending',
    validation_notes    TEXT,
    validated_at        TIMESTAMPTZ,
    validated_by        TEXT,                           -- Agent ID or 'human:username'
    -- Promotion to production
    promoted_at         TIMESTAMPTZ,
    promoted_document_id UUID,                          -- Set after promotion to documents table
    -- Timestamps
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

-- Parsed ruling data extracted from captures — validated before promotion to rulings table
CREATE TABLE staging.ruled_items (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    capture_id          UUID                NOT NULL REFERENCES staging.captures(id),
    court_id            UUID                NOT NULL,
    -- Raw extracted fields (before entity resolution)
    case_number_raw     TEXT,
    judge_name_raw      TEXT,
    department_raw      TEXT,
    hearing_date        DATE,
    ruling_text         TEXT,
    motion_type_raw     TEXT,
    outcome_raw         TEXT,
    parsed_metadata     JSONB,                          -- Full structured output from scraper
    -- Validation lifecycle
    validation_status   validation_status   NOT NULL DEFAULT 'pending',
    validation_notes    TEXT,
    validated_at        TIMESTAMPTZ,
    -- Promotion
    promoted_at         TIMESTAMPTZ,
    promoted_ruling_id  UUID,                           -- Set after promotion to rulings table
    -- Timestamps
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

COMMENT ON SCHEMA staging              IS 'Pre-validation data. Nothing here is in production until explicitly promoted.';
COMMENT ON TABLE  staging.captures     IS 'Raw scraper output — HTML, PDFs, etc. Retained indefinitely for re-parsing.';
COMMENT ON TABLE  staging.ruled_items  IS 'Parsed rulings awaiting validation. One row per ruling per capture.';


-- =============================================================================
-- SCRAPER HEALTH
-- =============================================================================

CREATE TABLE scraper_runs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    scraper_id          TEXT        NOT NULL,           -- e.g., 'ca-la-tentative'
    court_id            UUID        REFERENCES courts(id),
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    status              TEXT        NOT NULL,           -- 'success', 'failure', 'partial'
    records_captured    INTEGER     NOT NULL DEFAULT 0,
    records_failed      INTEGER     NOT NULL DEFAULT 0,
    error_message       TEXT,
    error_details       JSONB,
    response_time_ms    INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE scraper_runs IS 'One row per scraper execution. Powers the health dashboard.';


-- =============================================================================
-- USERS & ALERTS
-- =============================================================================

CREATE TABLE users (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email               TEXT        NOT NULL UNIQUE,
    email_verified      BOOLEAN     NOT NULL DEFAULT FALSE,
    password_hash       TEXT,                           -- NULL for OAuth-only accounts
    display_name        TEXT,
    role                TEXT        NOT NULL DEFAULT 'user',  -- 'user', 'admin'
    api_key             TEXT        UNIQUE,             -- For programmatic REST access
    ai_budget_daily     INTEGER     NOT NULL DEFAULT 20, -- Daily AI operation allocation
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    last_login_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TYPE alert_type AS ENUM (
    'case_docket',
    'judge_ruling',
    'keyword',
    'party_attorney'
);

CREATE TABLE alert_subscriptions (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    alert_type  alert_type  NOT NULL,
    -- Flexible filter: {case_id: "...", judge_id: "...", keyword: "...", party_id: "..."}
    filters     JSONB       NOT NULL DEFAULT '{}',
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE alert_events (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id         UUID        NOT NULL REFERENCES alert_subscriptions(id) ON DELETE CASCADE,
    document_id             UUID        REFERENCES documents(id),
    ruling_id               UUID        REFERENCES rulings(id),
    triggered_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    included_in_digest_at   TIMESTAMPTZ,
    digest_sent             BOOLEAN     NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE  alert_subscriptions        IS 'User-configured alert subscriptions. filters is type-specific JSONB.';
COMMENT ON TABLE  alert_events               IS 'One row per alert trigger. Batched into daily digest emails.';
COMMENT ON COLUMN alert_events.digest_sent   IS 'FALSE = pending inclusion in next digest run.';


-- =============================================================================
-- INDICES — optimized for common query patterns
-- =============================================================================

-- Courts
CREATE INDEX idx_courts_state       ON courts(state);

-- Cases — queried by court, case number, date, and status
CREATE INDEX idx_cases_court_id     ON cases(court_id);
CREATE INDEX idx_cases_number_norm  ON cases(case_number_normalized);
CREATE INDEX idx_cases_filed_at     ON cases(filed_at DESC);
CREATE INDEX idx_cases_status       ON cases(case_status) WHERE case_status = 'active';

-- Case relationships
CREATE INDEX idx_case_judges_judge_id       ON case_judges(judge_id);
CREATE INDEX idx_case_attorneys_attorney_id ON case_attorneys(attorney_id);
CREATE INDEX idx_case_parties_party_id      ON case_parties(party_id);

-- Rulings — primary query target; optimized for judge analytics, date range, outcome
CREATE INDEX idx_rulings_judge_id       ON rulings(judge_id);
CREATE INDEX idx_rulings_case_id        ON rulings(case_id);
CREATE INDEX idx_rulings_court_id       ON rulings(court_id);
CREATE INDEX idx_rulings_hearing_date   ON rulings(hearing_date DESC);
CREATE INDEX idx_rulings_posted_at      ON rulings(posted_at DESC);
CREATE INDEX idx_rulings_outcome        ON rulings(outcome);
CREATE INDEX idx_rulings_motion_type    ON rulings(motion_type);
-- Composite: judge analytics (most common aggregation pattern)
CREATE INDEX idx_rulings_judge_outcome  ON rulings(judge_id, outcome, hearing_date DESC);
CREATE INDEX idx_rulings_judge_motion   ON rulings(judge_id, motion_type, outcome);

-- Documents
CREATE INDEX idx_documents_case_id      ON documents(case_id);
CREATE INDEX idx_documents_court_id     ON documents(court_id);
CREATE INDEX idx_documents_hash         ON documents(content_hash);
CREATE INDEX idx_documents_captured_at  ON documents(captured_at DESC);
CREATE INDEX idx_documents_hearing_date ON documents(hearing_date);
CREATE INDEX idx_documents_active       ON documents(status) WHERE status = 'active';

-- Entity resolution — fast lookup by raw name string
CREATE INDEX idx_judge_aliases_raw_name     ON judge_aliases(lower(raw_name));
CREATE INDEX idx_attorney_aliases_raw_name  ON attorney_aliases(lower(raw_name));
CREATE INDEX idx_party_aliases_raw_name     ON party_aliases(lower(raw_name));
-- Bar number lookup for attorney dedup
CREATE INDEX idx_attorneys_bar              ON attorneys(bar_state, bar_number)
    WHERE bar_number IS NOT NULL;

-- Staging
CREATE INDEX idx_staging_captures_court_id  ON staging.captures(court_id);
CREATE INDEX idx_staging_captures_status    ON staging.captures(validation_status);
CREATE INDEX idx_staging_captures_at        ON staging.captures(captured_at DESC);
CREATE INDEX idx_staging_ruled_status       ON staging.ruled_items(validation_status);

-- Scraper health — dashboard needs recent runs per scraper
CREATE INDEX idx_scraper_runs_scraper_id    ON scraper_runs(scraper_id, started_at DESC);

-- Alert events — digest job queries un-sent events
CREATE INDEX idx_alert_events_sub_id        ON alert_events(subscription_id);
CREATE INDEX idx_alert_events_unsent        ON alert_events(digest_sent, triggered_at)
    WHERE NOT digest_sent;


-- =============================================================================
-- TRIGGERS — auto-update updated_at on modification
-- =============================================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_courts_updated_at
    BEFORE UPDATE ON courts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_judges_updated_at
    BEFORE UPDATE ON judges
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_attorneys_updated_at
    BEFORE UPDATE ON attorneys
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_parties_updated_at
    BEFORE UPDATE ON parties
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_cases_updated_at
    BEFORE UPDATE ON cases
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_rulings_updated_at
    BEFORE UPDATE ON rulings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_alert_subscriptions_updated_at
    BEFORE UPDATE ON alert_subscriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
