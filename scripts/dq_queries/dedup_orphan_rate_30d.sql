-- dedup_orphan_rate_30d.sql
-- Per-county, per-day counts of dedup and orphan events for the last 30 days.
--
-- Issue: https://github.com/judgemind/judgemind/issues/2654
-- Parent: https://github.com/judgemind/judgemind/issues/2569
--
-- Metric emitters:
--   content_hash_dedup_supersede  — packages/scraper-framework/src/ingestion/db.py:1819
--   zero_ruling_extraction        — packages/scraper-framework/src/ingestion/worker.py:2620
--
-- Run via:
--   scripts/dev-db-query.sh --file scripts/dq_queries/dedup_orphan_rate_30d.sql
--
-- Reading the output:
--   Zero rows is acceptable for very new metrics; a healthy dev instance
--   should have at least one row within a week of deploy.
--   Rising event_count on content_hash_dedup_supersede means dedup is firing
--   more often (normal steady-state or a burst of re-captures).
--   Rising event_count on zero_ruling_extraction means more documents are
--   being processed without extracting any rulings (possible scraper or
--   LLM regression — investigate if trend persists over multiple days).

SELECT
    county,
    metric_name,
    DATE_TRUNC('day', recorded_at AT TIME ZONE 'UTC')::date AS day,
    COUNT(*)                                                 AS event_count,
    SUM(metric_value)                                        AS metric_value_sum
FROM telemetry.data_quality_metrics
WHERE metric_name IN (
    'content_hash_dedup_supersede',
    'zero_ruling_extraction'
)
  AND recorded_at >= NOW() - INTERVAL '30 days'
GROUP BY county, metric_name, DATE_TRUNC('day', recorded_at AT TIME ZONE 'UTC')::date
ORDER BY day DESC, county, metric_name;
