-- Up Migration
-- Create court_directory_snapshots table for historical department-to-judge
-- directory tracking. Each row captures a point-in-time snapshot of a court's
-- directory mapping, enabling historical lookups during backfills.
--
-- See: https://github.com/judgemind/judgemind/issues/412

CREATE TABLE IF NOT EXISTS court_directory_snapshots (
    id SERIAL PRIMARY KEY,
    court_id TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    s3_key TEXT NOT NULL,
    mapping JSONB NOT NULL,
    content_hash TEXT NOT NULL
);

-- Lookup index: find the most recent snapshot for a court on or before a date.
CREATE INDEX IF NOT EXISTS idx_court_dir_lookup
    ON court_directory_snapshots (court_id, captured_at DESC);


-- Down Migration

DROP INDEX IF EXISTS idx_court_dir_lookup;
DROP TABLE IF EXISTS court_directory_snapshots;
