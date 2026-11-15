-- Up Migration
--
-- Durable records of raw keys whose rows are not a complete split (#5008).
--
-- A live capture can end incomplete: a new key whose extraction failed is
-- written as one whole-document ruling (#4948), a partial extraction writes
-- only the rulings the answered parts yielded (#5000), a split child whose
-- enrichment was exhausted is missing (#4878), and a dead-lettered event
-- writes nothing.  A reingest that refuses an incomplete extraction only
-- listed the key in a failed manifest under tmp/keys/, which expires after
-- 30 days.  None of these were durable, and nothing re-split the keys.
--
-- telemetry.ingest_incomplete
-- ---------------------------
-- One row per raw S3 key whose stored rows are not a complete split, with
-- the latest reason:
--   single_document_fallback  live new key, extraction failed, written whole
--   partial_split             live new key, only the answered parts written
--   exhausted_child           a split child's enrichment was exhausted
--   dead_letter               the live worker gave up on the event
--   reingest_failed           a reingest refused an incomplete extraction
-- The worker upserts it in the same transaction as the incomplete write (or
-- right after the dead-letter) and deletes it in the same transaction as a
-- later complete write of the key.  scripts/reconcile_raw_keys.py re-ingests
-- marked keys daily and counts its launches in retry_attempts; at its cap
-- (--max-incomplete-attempts) a key is reported as stuck (stuck_at) rather
-- than retried forever.
--
-- telemetry.raw_reconcile_runs / raw_reconcile_counts gain the incomplete
-- (retryable) and incomplete_stuck counts for the /admin/data-quality panel.
--
-- Schema notes
-- ------------
-- * Additive only.  telemetry.*, not a status column on derived.documents:
--   a dead-lettered key has no document row to carry a status, and the
--   retry attempt count is accumulated state a derived.* rebuild must not
--   reset.  A rebuild re-processes every key, so it clears the records of
--   keys that now split completely and records the ones that still do not.
-- * Volume: tens to a few thousand rows; the reconciler drains them.
-- * The worker's writes are best-effort and savepoint-protected, so the
--   worker keeps working if it deploys before this migration applies.

CREATE TABLE IF NOT EXISTS telemetry.ingest_incomplete (
    s3_key TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    county TEXT,
    scraper_id TEXT,
    document_id TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    occurrences INTEGER NOT NULL DEFAULT 1,
    retry_attempts INTEGER NOT NULL DEFAULT 0,
    last_retry_at TIMESTAMPTZ,
    stuck_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ingest_incomplete_reason
    ON telemetry.ingest_incomplete (reason);

COMMENT ON TABLE telemetry.ingest_incomplete IS
    'One row per raw S3 key whose stored rows are not a complete split '
    '(single_document_fallback, partial_split, exhausted_child, dead_letter, '
    'reingest_failed). Deleted by a later complete write of the key. '
    'Re-ingested by scripts/reconcile_raw_keys.py up to its attempt cap. '
    'Issue #5008.';

ALTER TABLE telemetry.raw_reconcile_runs
    ADD COLUMN IF NOT EXISTS incomplete INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS incomplete_stuck INTEGER NOT NULL DEFAULT 0;

ALTER TABLE telemetry.raw_reconcile_counts
    ADD COLUMN IF NOT EXISTS incomplete INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS incomplete_stuck INTEGER NOT NULL DEFAULT 0;


-- Down Migration
ALTER TABLE telemetry.raw_reconcile_counts
    DROP COLUMN IF EXISTS incomplete_stuck,
    DROP COLUMN IF EXISTS incomplete;
ALTER TABLE telemetry.raw_reconcile_runs
    DROP COLUMN IF EXISTS incomplete_stuck,
    DROP COLUMN IF EXISTS incomplete;
DROP INDEX IF EXISTS telemetry.idx_ingest_incomplete_reason;
DROP TABLE IF EXISTS telemetry.ingest_incomplete;
