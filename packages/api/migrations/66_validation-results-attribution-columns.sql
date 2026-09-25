-- Up Migration
--
-- Record source attribution on telemetry.validation_results (issue #4706).
--
-- validation_results rows carry only document_id. On a validation FAIL the
-- ingestion worker skips the ruling write, so no derived.documents row
-- exists to join against, and split-child document ids are
-- uuid5(ns, parent:idx), which cannot be mapped back to the raw S3 capture.
-- Per-county failure analysis (#4681, #4682, #4696) therefore had to be
-- rebuilt from CloudWatch logs or recomputed split ids.
--
-- This migration adds three nullable attribution columns that the worker
-- fills at write time from the event it already has in scope:
--
--   county      — event county (e.g. 'Los Angeles')
--   scraper_id  — event scraper_id (e.g. 'ca-la-tentatives-civil')
--   s3_key      — raw capture key in the document archive bucket
--
-- Schema notes
-- ------------
-- * Additive only. telemetry.* is accumulated observability and is not
--   rebuildable from S3, so there is no backfill and no destructive change.
--   Rows written before this migration stay NULL.
-- * Nullable, no DEFAULT: callers that have no attribution (older code
--   paths, ad-hoc scripts) keep working and write NULL.
-- * No index. The table is scanned by created_at windows for ad-hoc
--   grouping; add an index only if a hot query needs one.
--
-- Deploy ordering: this migration must be applied (deploy-api.yml) before
-- the ingestion worker that writes these columns ships, because the worker
-- inserts the validation row inside the ruling's transaction on the pass
-- path. The worker change lands in a separate follow-up PR for #4706.

ALTER TABLE telemetry.validation_results
    ADD COLUMN IF NOT EXISTS county TEXT,
    ADD COLUMN IF NOT EXISTS scraper_id TEXT,
    ADD COLUMN IF NOT EXISTS s3_key TEXT;

COMMENT ON COLUMN telemetry.validation_results.county IS
    'County of the source document (event county). NULL for rows written '
    'before migration 66 or by callers without attribution. Issue #4706.';

COMMENT ON COLUMN telemetry.validation_results.scraper_id IS
    'scraper_id of the source capture event. NULL for rows written before '
    'migration 66 or by callers without attribution. Issue #4706.';

COMMENT ON COLUMN telemetry.validation_results.s3_key IS
    'S3 key of the raw capture in the document archive bucket. Lets FAIL '
    'rows (no derived.documents row) and split children (uuid5 ids) be '
    'traced to their raw. NULL for rows written before migration 66. '
    'Issue #4706.';


-- Down Migration
ALTER TABLE telemetry.validation_results
    DROP COLUMN IF EXISTS s3_key,
    DROP COLUMN IF EXISTS scraper_id,
    DROP COLUMN IF EXISTS county;
