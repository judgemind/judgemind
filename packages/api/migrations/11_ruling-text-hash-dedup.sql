-- Up Migration: Add ruling_text_hash for content-based deduplication
--
-- The ruling_text_hash column stores a SHA-256 hash of the normalized
-- (lowercased, whitespace-collapsed) ruling text. The UNIQUE index on
-- (case_id, ruling_text_hash) prevents inserting duplicate rulings for
-- the same case when the text is semantically identical but differs in
-- casing or whitespace.
--
-- See: #1246

-- Add the column (nullable — existing rows will be backfilled separately)
ALTER TABLE rulings ADD COLUMN IF NOT EXISTS ruling_text_hash TEXT;

COMMENT ON COLUMN rulings.ruling_text_hash IS 'SHA-256 of normalized (lowercased, whitespace-collapsed) ruling text. Used for content-based dedup.';

-- Unique partial index: only enforced when the hash is non-NULL.
-- This allows the column to be nullable (for old rows not yet backfilled)
-- while still preventing future duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rulings_case_text_hash
    ON rulings (case_id, ruling_text_hash)
    WHERE ruling_text_hash IS NOT NULL;
