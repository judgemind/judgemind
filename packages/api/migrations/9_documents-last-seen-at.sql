-- Up Migration
-- Add last_seen_at column to documents table.
--
-- captured_at records when the document was first ingested. Since document IDs
-- are deterministic (content hash -> UUID5), re-scraping unchanged content
-- produces the same document_id and the upsert does NOT update captured_at.
-- This makes captured_at unreliable for staleness checks on low-volume courts.
--
-- last_seen_at is updated on every upsert (including ON CONFLICT), so it
-- always reflects the most recent time the scraper encountered this document.
-- The data quality staleness check uses MAX(last_seen_at) instead of
-- MAX(captured_at) for accurate scraper freshness monitoring.
--
-- See: https://github.com/judgemind/judgemind/issues/986

ALTER TABLE documents
    ADD COLUMN last_seen_at TIMESTAMPTZ;

-- Backfill: set last_seen_at to captured_at for all existing rows.
-- This is safe because captured_at is the best known value for existing data.
UPDATE documents SET last_seen_at = captured_at WHERE last_seen_at IS NULL;

-- Now make it NOT NULL with a default so future inserts always have a value.
ALTER TABLE documents
    ALTER COLUMN last_seen_at SET NOT NULL,
    ALTER COLUMN last_seen_at SET DEFAULT NOW();


-- Down Migration

ALTER TABLE documents DROP COLUMN IF EXISTS last_seen_at;
