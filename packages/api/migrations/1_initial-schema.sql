-- Migration 001: Initial schema
-- =============================================================================
-- Applies the full core entity model.
-- This migration is the source of truth for production deployments.
-- For local development, schema.sql is loaded automatically by docker-compose.
-- =============================================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Staging schema for pre-validation data
CREATE SCHEMA IF NOT EXISTS staging;


-- =============================================================================
-- REFERENCE DATA
-- =============================================================================

CREATE TABLE courts (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    state           CHAR(2)     NOT NULL,
    county          TEXT        NOT NULL,
    court_name      TEXT        NOT NULL,
    court_code      TEXT        UNIQUE NOT NULL,
    timezone        TEXT        NOT NULL DEFAULT 'America/Los_Angeles',
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    scraper_config  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  courts               IS 'Reference table for jurisdictions. One row per court.';
COMMENT ON COLUMN courts.court_code    IS 'Slug used in S3 key paths: /{state}/{county}/{court}/...';


-- =============================================================================
-- CANONICAL ENTITIES — JUDGES
-- =============================================================================

CREATE TABLE judges (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name      TEXT        NOT NULL,
    court_id            UUID        NOT NULL REFERENCES courts(id),
    department          TEXT,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    appointed_at        DATE,
    biographical_notes  TEXT,
    bio_reviewed_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE judge_aliases (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    judge_id    UUID    NOT NULL REFERENCES judges(id) ON DELETE CASCADE,
    raw_name    TEXT    NOT NULL,
    source      TEXT,
    confidence  FLOAT   CHECK (confidence >= 0 AND confidence <= 1),
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- CANONICAL ENTITIES — ATTORNEYS
-- =============================================================================

CREATE TABLE attorneys (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  TEXT        NOT NULL,
    bar_number      TEXT,
    bar_state       CHAR(2),
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


-- =============================================================================
-- CANONICAL ENTITIES — PARTIES
-- =============================================================================

CREATE TABLE parties (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  TEXT        NOT NULL,
    party_type      TEXT,
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
    case_number             TEXT        NOT NULL,
    case_number_normalized  TEXT,
    court_id                UUID        NOT NULL REFERENCES courts(id),
    case_type               TEXT,
    case_subtype            TEXT,
    case_status             TEXT,
    case_title              TEXT,
    filed_at                DATE,
    closed_at               DATE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (court_id, case_number)
);

CREATE TABLE case_judges (
    case_id     UUID    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    judge_id    UUID    NOT NULL REFERENCES judges(id),
    assigned_at DATE,
    is_current  BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (case_id, judge_id)
);

CREATE TABLE case_attorneys (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id     UUID    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    attorney_id UUID    NOT NULL REFERENCES attorneys(id),
    role        TEXT    NOT NULL,
    party_id    UUID    REFERENCES parties(id),
    appeared_at DATE,
    withdrew_at DATE
);

CREATE TABLE case_parties (
    id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id     UUID    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    party_id    UUID    NOT NULL REFERENCES parties(id),
    role        TEXT    NOT NULL
);


-- =============================================================================
-- DOCUMENTS
-- =============================================================================

CREATE TYPE document_format AS ENUM ('html', 'pdf', 'docx', 'txt');
CREATE TYPE document_status AS ENUM ('active', 'superseded', 'removed');

CREATE TABLE documents (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID                REFERENCES cases(id),
    court_id            UUID                NOT NULL REFERENCES courts(id),
    document_type       TEXT                NOT NULL,
    motion_type         TEXT,
    s3_key              TEXT                NOT NULL,
    s3_bucket           TEXT                NOT NULL,
    format              document_format     NOT NULL,
    content_hash        TEXT                NOT NULL,
    source_url          TEXT,
    scraper_id          TEXT,
    captured_at         TIMESTAMPTZ         NOT NULL,
    hearing_date        DATE,
    published_at        TIMESTAMPTZ,
    status              document_status     NOT NULL DEFAULT 'active',
    previous_version_id UUID                REFERENCES documents(id),
    change_type         TEXT,
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

COMMENT ON COLUMN documents.content_hash IS 'SHA-256. Used for dedup and to detect revisions across scraper runs.';
COMMENT ON COLUMN documents.change_type  IS 'LLM-classified when a new version is captured: substantive or cosmetic.';


-- =============================================================================
-- RULINGS
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
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id             UUID            NOT NULL REFERENCES documents(id),
    case_id                 UUID            NOT NULL REFERENCES cases(id),
    judge_id                UUID            REFERENCES judges(id),
    court_id                UUID            NOT NULL REFERENCES courts(id),
    ruling_text             TEXT,
    ruling_text_html        TEXT,
    outcome                 ruling_outcome,
    motion_type             TEXT,
    hearing_date            DATE            NOT NULL,
    posted_at               TIMESTAMPTZ,
    summary                 TEXT,
    summary_model           TEXT,
    summary_generated_at    TIMESTAMPTZ,
    department              TEXT,
    is_tentative            BOOLEAN         NOT NULL DEFAULT TRUE,
    ruling_number           INTEGER,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON COLUMN rulings.judge_id IS 'Nullable: populated by entity resolution after initial capture.';
COMMENT ON COLUMN rulings.summary  IS 'Cached AI summary. Served from cache on every request.';


-- =============================================================================
-- STAGING AREA
-- =============================================================================

CREATE TYPE validation_status AS ENUM ('pending', 'passed', 'failed', 'flagged');

CREATE TABLE staging.captures (
    id                      UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    court_id                UUID                NOT NULL,
    scraper_id              TEXT                NOT NULL,
    source_url              TEXT                NOT NULL,
    raw_content             TEXT                NOT NULL,
    raw_content_type        TEXT                NOT NULL,
    content_hash            TEXT                NOT NULL,
    capture_metadata        JSONB,
    captured_at             TIMESTAMPTZ         NOT NULL,
    validation_status       validation_status   NOT NULL DEFAULT 'pending',
    validation_notes        TEXT,
    validated_at            TIMESTAMPTZ,
    validated_by            TEXT,
    promoted_at             TIMESTAMPTZ,
    promoted_document_id    UUID,
    created_at              TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

CREATE TABLE staging.ruled_items (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    capture_id          UUID                NOT NULL REFERENCES staging.captures(id),
    court_id            UUID                NOT NULL,
    case_number_raw     TEXT,
    judge_name_raw      TEXT,
    department_raw      TEXT,
    hearing_date        DATE,
    ruling_text         TEXT,
    motion_type_raw     TEXT,
    outcome_raw         TEXT,
    parsed_metadata     JSONB,
    validation_status   validation_status   NOT NULL DEFAULT 'pending',
    validation_notes    TEXT,
    validated_at        TIMESTAMPTZ,
    promoted_at         TIMESTAMPTZ,
    promoted_ruling_id  UUID,
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SCRAPER HEALTH
-- =============================================================================

CREATE TABLE scraper_runs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    scraper_id          TEXT        NOT NULL,
    court_id            UUID        REFERENCES courts(id),
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    status              TEXT        NOT NULL,
    records_captured    INTEGER     NOT NULL DEFAULT 0,
    records_failed      INTEGER     NOT NULL DEFAULT 0,
    error_message       TEXT,
    error_details       JSONB,
    response_time_ms    INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- USERS & ALERTS
-- =============================================================================

CREATE TABLE users (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT        NOT NULL UNIQUE,
    email_verified  BOOLEAN     NOT NULL DEFAULT FALSE,
    password_hash   TEXT,
    display_name    TEXT,
    role            TEXT        NOT NULL DEFAULT 'user',
    api_key         TEXT        UNIQUE,
    ai_budget_daily INTEGER     NOT NULL DEFAULT 20,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
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


-- =============================================================================
-- INDICES
-- =============================================================================

CREATE INDEX idx_courts_state               ON courts(state);

CREATE INDEX idx_cases_court_id             ON cases(court_id);
CREATE INDEX idx_cases_number_norm          ON cases(case_number_normalized);
CREATE INDEX idx_cases_filed_at             ON cases(filed_at DESC);
CREATE INDEX idx_cases_status               ON cases(case_status) WHERE case_status = 'active';

CREATE INDEX idx_case_judges_judge_id       ON case_judges(judge_id);
CREATE INDEX idx_case_attorneys_attorney_id ON case_attorneys(attorney_id);
CREATE INDEX idx_case_parties_party_id      ON case_parties(party_id);

CREATE INDEX idx_rulings_judge_id           ON rulings(judge_id);
CREATE INDEX idx_rulings_case_id            ON rulings(case_id);
CREATE INDEX idx_rulings_court_id           ON rulings(court_id);
CREATE INDEX idx_rulings_hearing_date       ON rulings(hearing_date DESC);
CREATE INDEX idx_rulings_posted_at          ON rulings(posted_at DESC);
CREATE INDEX idx_rulings_outcome            ON rulings(outcome);
CREATE INDEX idx_rulings_motion_type        ON rulings(motion_type);
CREATE INDEX idx_rulings_judge_outcome      ON rulings(judge_id, outcome, hearing_date DESC);
CREATE INDEX idx_rulings_judge_motion       ON rulings(judge_id, motion_type, outcome);

CREATE INDEX idx_documents_case_id          ON documents(case_id);
CREATE INDEX idx_documents_court_id         ON documents(court_id);
CREATE INDEX idx_documents_hash             ON documents(content_hash);
CREATE INDEX idx_documents_captured_at      ON documents(captured_at DESC);
CREATE INDEX idx_documents_hearing_date     ON documents(hearing_date);
CREATE INDEX idx_documents_active           ON documents(status) WHERE status = 'active';

CREATE INDEX idx_judge_aliases_raw_name     ON judge_aliases(lower(raw_name));
CREATE INDEX idx_attorney_aliases_raw_name  ON attorney_aliases(lower(raw_name));
CREATE INDEX idx_party_aliases_raw_name     ON party_aliases(lower(raw_name));
CREATE INDEX idx_attorneys_bar              ON attorneys(bar_state, bar_number) WHERE bar_number IS NOT NULL;

CREATE INDEX idx_staging_captures_court_id  ON staging.captures(court_id);
CREATE INDEX idx_staging_captures_status    ON staging.captures(validation_status);
CREATE INDEX idx_staging_captures_at        ON staging.captures(captured_at DESC);
CREATE INDEX idx_staging_ruled_status       ON staging.ruled_items(validation_status);

CREATE INDEX idx_scraper_runs_scraper_id    ON scraper_runs(scraper_id, started_at DESC);

CREATE INDEX idx_alert_events_sub_id        ON alert_events(subscription_id);
CREATE INDEX idx_alert_events_unsent        ON alert_events(digest_sent, triggered_at) WHERE NOT digest_sent;


-- =============================================================================
-- TRIGGERS — auto-update updated_at
-- =============================================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_courts_updated_at
    BEFORE UPDATE ON courts FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_judges_updated_at
    BEFORE UPDATE ON judges FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_attorneys_updated_at
    BEFORE UPDATE ON attorneys FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_parties_updated_at
    BEFORE UPDATE ON parties FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_cases_updated_at
    BEFORE UPDATE ON cases FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_rulings_updated_at
    BEFORE UPDATE ON rulings FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_alert_subscriptions_updated_at
    BEFORE UPDATE ON alert_subscriptions FOR EACH ROW EXECUTE FUNCTION update_updated_at();
