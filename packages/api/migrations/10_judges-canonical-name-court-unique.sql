-- Up Migration
-- Add UNIQUE constraint on (canonical_name, court_id) in judges table.
--
-- This prevents duplicate judge records from being created when a new raw_name
-- normalizes to a canonical_name that already exists for the same court but
-- the alias lookup misses (because the raw_name is different).
--
-- The resolve_judge() function in the ingestion pipeline is also updated to
-- check judges by (canonical_name, court_id) before creating a new record,
-- but this constraint serves as a safety net for race conditions and any
-- other code paths that insert into judges.
--
-- See: https://github.com/judgemind/judgemind/issues/1107

ALTER TABLE judges
    ADD CONSTRAINT judges_canonical_name_court_id_key
    UNIQUE (canonical_name, court_id);


-- Down Migration

ALTER TABLE judges
    DROP CONSTRAINT IF EXISTS judges_canonical_name_court_id_key;
