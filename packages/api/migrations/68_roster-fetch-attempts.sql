-- Up Migration
--
-- Record every court-directory (judge roster) fetch attempt (issue #4872).
--
-- framework/runner.py::_fetch_rosters fetches each court's dept→judge
-- directory before the scrapers run. A failure was only logged as a
-- warning, so broken rosters went unnoticed for months: SF was stale for
-- ~7 months (#4733), Santa Clara failed on every run (#4732), and LA has
-- returned HTTP 503 since 2026-07.
--
-- derived.court_directory_snapshots cannot answer "when did this roster
-- last fetch successfully?" because an unchanged (deduplicated) fetch
-- inserts no row. This table records one row per attempt, success or
-- failure, so /admin/data-quality can show per-court freshness, the last
-- outcome, and the error class.
--
-- Columns
-- -------
--   court_id          court identifier (e.g. 'ca_los_angeles'); the
--                     directory class name when COURT_ID could not be read
--   directory_class   CourtDirectory subclass name (e.g. 'LACourtDirectory')
--   attempted_at      when the fetch finished
--   outcome           'success' or 'failure'
--   department_count  number of departments parsed (NULL when the fetch
--                     raised before parsing)
--   snapshot_changed  TRUE when a new snapshot row was inserted, FALSE when
--                     the content matched the latest snapshot (dedup).
--                     NULL on failure.
--   error_class       coarse failure bucket: http_403, http_4xx, http_5xx,
--                     redirect, network, empty_roster, missing_court_id,
--                     import_error, other. NULL on success.
--   http_status       HTTP status code when the failure was an HTTP error
--   error_message     exception text (truncated by the writer)
--
-- Schema notes
-- ------------
-- * Additive only. telemetry.* is accumulated observability, not
--   rebuildable from S3.
-- * Volume: ~10 courts x a few runs/day, so a few thousand rows a year.
--   No retention policy needed yet.
-- * Deploy ordering (#4775): this migration ships in its own PR, before
--   the scraper-framework writer and the API reader.

CREATE TABLE IF NOT EXISTS telemetry.roster_fetch_attempts (
    id BIGSERIAL PRIMARY KEY,
    court_id TEXT NOT NULL,
    directory_class TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    outcome TEXT NOT NULL,
    department_count INTEGER,
    snapshot_changed BOOLEAN,
    error_class TEXT,
    http_status INTEGER,
    error_message TEXT,
    CONSTRAINT roster_fetch_attempts_outcome_check
        CHECK (outcome IN ('success', 'failure'))
);

-- Dashboard query: latest attempt and latest success per court.
CREATE INDEX IF NOT EXISTS idx_roster_fetch_attempts_court_time
    ON telemetry.roster_fetch_attempts (court_id, attempted_at DESC);

COMMENT ON TABLE telemetry.roster_fetch_attempts IS
    'One row per court-directory (judge roster) fetch attempt, success or '
    'failure. Feeds per-court roster freshness on /admin/data-quality. '
    'Issue #4872.';

COMMENT ON COLUMN telemetry.roster_fetch_attempts.snapshot_changed IS
    'TRUE when the fetch inserted a new court_directory_snapshots row, FALSE '
    'when it matched the latest snapshot (dedup). NULL on failure.';

COMMENT ON COLUMN telemetry.roster_fetch_attempts.error_class IS
    'Failure bucket: http_403, http_4xx, http_5xx, redirect, network, '
    'empty_roster, missing_court_id, import_error, other. NULL on success.';


-- Down Migration
DROP INDEX IF EXISTS telemetry.idx_roster_fetch_attempts_court_time;
DROP TABLE IF EXISTS telemetry.roster_fetch_attempts;
