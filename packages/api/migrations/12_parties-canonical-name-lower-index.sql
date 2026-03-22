-- Up Migration: Add functional index on LOWER(canonical_name) for parties table
--
-- The casePartiesLoader uses DISTINCT ON (cp.case_id, LOWER(p.canonical_name), cp.role)
-- to deduplicate parties with different casing. Without a functional index on
-- LOWER(canonical_name), PostgreSQL must perform a full sort of all matched rows.
-- This index allows PostgreSQL to use an index scan for the ORDER BY / DISTINCT ON.
--
-- ~1,400 rows — no need for CONCURRENTLY; runs in the migration transaction.
--
-- See: #1504, #1434

CREATE INDEX IF NOT EXISTS idx_parties_canonical_name_lower
    ON parties(LOWER(canonical_name));
