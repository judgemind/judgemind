-- Up Migration
--
-- Record where a ruling's hearing date came from (issue #4793, decision on
-- #4755).
--
-- The deterministic ``hearing_date_in_range`` rule drops any ruling whose
-- hearing date is more than 180 days from ``captured_at``. That catches
-- misparsed body-text dates, but it also drops genuine old rulings that
-- courts still post (Contra Costa portal 2025 rulings, a re-captured Orange
-- ruling). #4793 keeps the 180-day rule for LLM- and regex-extracted dates
-- and turns it into a non-blocking flag for dates from a structured source
-- (the scraper's labelled header / filename / listing, or the
-- ``hearing_date_for_raw`` hook). The worker needs somewhere to record that
-- provenance:
--
--   derived.rulings.hearing_date_source             — provenance of the
--                                                      stored hearing_date
--   telemetry.validation_results.hearing_date_source — provenance of the
--                                                      date the rule judged
--
-- Values written by the worker: 'structured_scraper', 'structured_hook',
-- 'structured_header', 'splitter', 'llm', 'regex_fallback'. Free text, no
-- CHECK constraint, so a new source does not need a migration.
--
-- Schema notes
-- ------------
-- * Additive only. Nullable, no DEFAULT, no backfill, no index. Rows
--   written before the worker change stay NULL. derived.rulings is
--   rebuildable from S3, so a rebuild fills it; telemetry.* is not.
--
-- Deploy ordering: this migration must be applied (deploy-api.yml) before
-- the ingestion worker that writes these columns ships, because the worker
-- names them in its INSERTs. The worker change lands in a separate PR for
-- #4793 (same split as #4706 / #4772).

ALTER TABLE derived.rulings
    ADD COLUMN IF NOT EXISTS hearing_date_source TEXT;

ALTER TABLE telemetry.validation_results
    ADD COLUMN IF NOT EXISTS hearing_date_source TEXT;

COMMENT ON COLUMN derived.rulings.hearing_date_source IS
    'Where hearing_date came from: structured_scraper, structured_hook, '
    'structured_header (structured sources, exempt from the 180-day rule), '
    'or splitter, llm, regex_fallback. NULL for rows written before '
    'migration 67 or with no hearing_date. Issue #4793.';

COMMENT ON COLUMN telemetry.validation_results.hearing_date_source IS
    'Provenance of the hearing date the deterministic rules judged (see '
    'derived.rulings.hearing_date_source). NULL for rows written before '
    'migration 67 or with no hearing_date. Issue #4793.';


-- Down Migration
ALTER TABLE telemetry.validation_results
    DROP COLUMN IF EXISTS hearing_date_source;

ALTER TABLE derived.rulings
    DROP COLUMN IF EXISTS hearing_date_source;
