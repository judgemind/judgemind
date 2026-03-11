-- Up Migration
-- Create data_quality_metrics table for time-series data quality tracking
-- per county. Stores rolling metrics like ruling counts, field completeness,
-- and scraper health indicators for the data quality dashboard.
--
-- See: https://github.com/judgemind/judgemind/issues/748
-- Parent: https://github.com/judgemind/judgemind/issues/742
--
-- Expected metric_name values:
--   - ruling_count_24h             — rolling 24h ruling ingest count
--   - ruling_count_7d_avg          — 7-day daily average
--   - field_completeness_pct       — overall field completeness percentage
--   - field_completeness_judge     — per-field completeness (judge)
--   - field_completeness_motion_type — per-field completeness (motion type)
--   - field_completeness_parties   — per-field completeness (parties)
--   - scraper_last_success_age_hours — hours since last successful scraper run
--   - scraper_run_success_rate_24h — success rate over last 24h
--
-- TODO(ops): Add a retention policy (e.g. DROP rows older than 90 days) when
-- data volume warrants it. At current scale this table will stay small.

CREATE TABLE IF NOT EXISTS data_quality_metrics (
    id BIGSERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    county TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value NUMERIC NOT NULL,
    metadata JSONB
);

-- Primary query pattern: fetch recent values of a specific metric for a county,
-- ordered by time descending (dashboard time-series charts).
CREATE INDEX IF NOT EXISTS idx_dqm_county_metric_time
    ON data_quality_metrics (county, metric_name, recorded_at DESC);


-- Down Migration

DROP INDEX IF EXISTS idx_dqm_county_metric_time;
DROP TABLE IF EXISTS data_quality_metrics;
