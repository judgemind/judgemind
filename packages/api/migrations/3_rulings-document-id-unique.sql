-- Up Migration
-- Add UNIQUE constraint on rulings.document_id to support upsert semantics.
-- The ingestion pipeline needs ON CONFLICT (document_id) DO UPDATE to allow
-- re-ingestion of documents with improved field extraction.

-- First, check for and remove any duplicate document_id rows (keep the newest).
DELETE FROM rulings r
WHERE r.id NOT IN (
    SELECT DISTINCT ON (document_id) id
    FROM rulings
    ORDER BY document_id, created_at DESC
);

-- ~1,400 rows — no need for CONCURRENTLY; runs in the migration transaction.
ALTER TABLE rulings
    ADD CONSTRAINT uq_rulings_document_id UNIQUE (document_id);


-- Down Migration
ALTER TABLE rulings DROP CONSTRAINT IF EXISTS uq_rulings_document_id;
