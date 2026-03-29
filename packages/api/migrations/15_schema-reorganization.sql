-- Up Migration
-- Reorganize tables into logical schemas:
--   public.    → user-generated data (preserved during rebuilds)
--   derived.   → data rebuilt from S3 archive (truncatable)
--   telemetry. → operational logs (expendable)
--   staging.   → (already exists) capture pipeline staging
--
-- Uses search_path so existing unqualified SQL references still resolve.
-- Future code should use schema-qualified names for clarity.
--
-- A rebuild from S3 is now: TRUNCATE all derived.* tables CASCADE, re-run pipeline.
-- A backup of user data is: pg_dump --schema=public

CREATE SCHEMA IF NOT EXISTS derived;
CREATE SCHEMA IF NOT EXISTS telemetry;

-- Move derived tables (rebuilt from S3 archive)
ALTER TABLE courts              SET SCHEMA derived;
ALTER TABLE documents           SET SCHEMA derived;
ALTER TABLE rulings             SET SCHEMA derived;
ALTER TABLE cases               SET SCHEMA derived;
ALTER TABLE judges              SET SCHEMA derived;
ALTER TABLE judge_aliases       SET SCHEMA derived;
ALTER TABLE attorneys           SET SCHEMA derived;
ALTER TABLE attorney_aliases    SET SCHEMA derived;
ALTER TABLE parties             SET SCHEMA derived;
ALTER TABLE party_aliases       SET SCHEMA derived;
ALTER TABLE case_judges         SET SCHEMA derived;
ALTER TABLE case_parties        SET SCHEMA derived;
ALTER TABLE case_attorneys      SET SCHEMA derived;

ALTER TABLE court_directory_snapshots   SET SCHEMA derived;

-- Move telemetry tables (operational logs, expendable)
ALTER TABLE scraper_runs                SET SCHEMA telemetry;
ALTER TABLE data_quality_metrics        SET SCHEMA telemetry;
ALTER TABLE validation_results          SET SCHEMA telemetry;

-- Set search_path so unqualified references still resolve.
-- This applies to the judgemind role (used by all services).
ALTER DATABASE judgemind SET search_path = public, derived, telemetry, staging;


-- Down Migration
ALTER TABLE derived.courts              SET SCHEMA public;
ALTER TABLE derived.documents           SET SCHEMA public;
ALTER TABLE derived.rulings             SET SCHEMA public;
ALTER TABLE derived.cases               SET SCHEMA public;
ALTER TABLE derived.judges              SET SCHEMA public;
ALTER TABLE derived.judge_aliases       SET SCHEMA public;
ALTER TABLE derived.attorneys           SET SCHEMA public;
ALTER TABLE derived.attorney_aliases    SET SCHEMA public;
ALTER TABLE derived.parties             SET SCHEMA public;
ALTER TABLE derived.party_aliases       SET SCHEMA public;
ALTER TABLE derived.case_judges         SET SCHEMA public;
ALTER TABLE derived.case_parties        SET SCHEMA public;
ALTER TABLE derived.case_attorneys      SET SCHEMA public;

ALTER TABLE derived.court_directory_snapshots      SET SCHEMA public;

ALTER TABLE telemetry.scraper_runs                SET SCHEMA public;
ALTER TABLE telemetry.data_quality_metrics        SET SCHEMA public;
ALTER TABLE telemetry.validation_results          SET SCHEMA public;

ALTER DATABASE judgemind SET search_path = public, staging;

DROP SCHEMA IF EXISTS derived;
DROP SCHEMA IF EXISTS telemetry;
