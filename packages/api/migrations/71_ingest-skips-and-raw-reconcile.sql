-- Up Migration
--
-- Durable skip records and raw-key reconciliation results (issue #5015).
--
-- Every archived raw key should either be represented in derived.documents
-- or have a recorded reason it is not. Ingestion has lost events twice
-- (May-June and October 2026), and neither loss was caught automatically,
-- partly because intentional non-writes were only logged, so a reconciler
-- could not tell a dropped event from a deliberate skip.
--
-- telemetry.ingest_skips
-- ----------------------
-- One row per raw S3 key the ingestion worker deliberately wrote no
-- document for, with the reason. The worker upserts it on every pass, so the
-- row reflects the latest verdict. Reasons written today:
--   governor_bio_route       ca-governor-appointments press release, routed
--                            away from ruling extraction (#3688, #5016)
--   santa_clara_placeholder  Santa Clara "tentative rulings are not
--                            available" notice (#4725)
--   zero_rulings_no_text     multimodal PDF with no rulings and no text
--                            layer to fall back on (#4714)
--   no_extractable_text      a PDF nothing could read (#4983, #4987)
--   no_ruling_text           every row the multimodal split found had no
--                            ruling text (empty tentative cells, #4714)
-- Validation failures (telemetry.validation_results.s3_key) and the all-NULL
-- metadata guard (telemetry.data_quality_metrics, metric
-- non_ruling_pdf_dropped, metadata.s3_key) were already durable and stay
-- where they are; the reconciler reads all three.
--
-- telemetry.raw_reconcile_runs / telemetry.raw_reconcile_counts
-- -------------------------------------------------------------
-- One row per scripts/reconcile_raw_keys.py --record run, and one row per
-- raw prefix scope (<state>/<county>) per run with the represented /
-- skipped / unrepresented counts. Feeds the raw-key reconciliation panel on
-- /admin/data-quality.
--
-- Schema notes
-- ------------
-- * Additive only. telemetry.* is accumulated observability. Skip records
--   are re-derived whenever a key is re-ingested, so a rebuild of derived.*
--   leaves them consistent; they are not in derived.* because the rows
--   explain the absence of derived rows and must survive a derived reset.
-- * Volume: a few thousand skip rows; one run row plus ~100 count rows a day.
-- * The worker's writes are best-effort and savepoint-protected, so the
--   worker keeps working if it deploys before this migration applies.

CREATE TABLE IF NOT EXISTS telemetry.ingest_skips (
    s3_key TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    county TEXT,
    scraper_id TEXT,
    document_id TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    occurrences INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_ingest_skips_reason
    ON telemetry.ingest_skips (reason);

COMMENT ON TABLE telemetry.ingest_skips IS
    'One row per raw S3 key the ingestion worker deliberately wrote no '
    'document for, with the reason (governor_bio_route, '
    'santa_clara_placeholder, zero_rulings_no_text, no_extractable_text, '
    'no_ruling_text). '
    'Read by scripts/reconcile_raw_keys.py. Issue #5015.';

CREATE TABLE IF NOT EXISTS telemetry.raw_reconcile_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_keys INTEGER NOT NULL,
    represented INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    unrepresented INTEGER NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0,
    dated_excluded INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL DEFAULT 'none',
    key_list_uri TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_raw_reconcile_runs_finished
    ON telemetry.raw_reconcile_runs (finished_at DESC);

COMMENT ON TABLE telemetry.raw_reconcile_runs IS
    'One row per raw-key reconciliation run (scripts/reconcile_raw_keys.py '
    '--record). action is none, reingest, persistent, reingest_failed, '
    'reingest_deferred, report_only or over_threshold. Issue #5015.';

CREATE TABLE IF NOT EXISTS telemetry.raw_reconcile_counts (
    run_id UUID NOT NULL REFERENCES telemetry.raw_reconcile_runs (run_id)
        ON DELETE CASCADE,
    scope TEXT NOT NULL,
    raw_keys INTEGER NOT NULL,
    represented INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    unrepresented INTEGER NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0,
    dated_excluded INTEGER NOT NULL DEFAULT 0,
    skipped_by_reason JSONB NOT NULL DEFAULT '{}'::jsonb,
    unrepresented_by_day JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, scope)
);

COMMENT ON TABLE telemetry.raw_reconcile_counts IS
    'Per-scope (<state>/<county>) counts for one raw-key reconciliation run: '
    'raw keys, represented in derived.documents, skipped with a recorded '
    'reason, unrepresented, and pending (captured inside the grace window). '
    'Issue #5015.';


-- Down Migration
DROP TABLE IF EXISTS telemetry.raw_reconcile_counts;
DROP INDEX IF EXISTS telemetry.idx_raw_reconcile_runs_finished;
DROP TABLE IF EXISTS telemetry.raw_reconcile_runs;
DROP INDEX IF EXISTS telemetry.idx_ingest_skips_reason;
DROP TABLE IF EXISTS telemetry.ingest_skips;
