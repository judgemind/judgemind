# Ingestion Pipeline Observability

Agent reference for spot-checking pipeline health signals via
`telemetry.data_quality_metrics`.

## Canonical dedup and orphan metrics

Two metrics track the most common silent-failure modes in the ingestion pipeline:

| `metric_name` | What it counts | Emitter |
|---|---|---|
| `content_hash_dedup_supersede` | Documents deduplicated by content hash (re-captures of an already-stored ruling) | `packages/scraper-framework/src/ingestion/db.py:1819` |
| `zero_ruling_extraction` | Documents processed by the LLM that returned zero rulings | `packages/scraper-framework/src/ingestion/worker.py:2620` |

Both metrics write one row per event to `telemetry.data_quality_metrics` with
`metric_value = 1` and a `metadata` JSONB blob containing `county`, `scraper_id`,
and the document's S3 key.

See `packages/scraper-framework/src/telemetry/README.md` for the full telemetry
event catalog.

## Running the 30-day dedup/orphan rate query

```bash
scripts/dev-db-query.sh --file scripts/dq_queries/dedup_orphan_rate_30d.sql
```

Returns per-county, per-day event counts and `metric_value` sums for the two
metrics above, over the last 30 days.

**Reading the output:**

- **Zero rows** is acceptable for very new metric names; a healthy dev instance
  should have at least one row within a week of deploy.
- **Rising `event_count` on `content_hash_dedup_supersede`** means dedup is
  firing more often — normal during re-capture backfills or after a scraper
  produces duplicates. Sustained rises on a single county without a known
  backfill are worth investigating.
- **Rising `event_count` on `zero_ruling_extraction`** means more documents are
  being processed without extracting any rulings. Investigate if the trend
  persists over multiple days — likely causes are a new non-ruling PDF type
  slipping past capture-side filters, or an LLM regression.

## Related resources

- `docs/runbooks/data-quality-monitoring.md` — operational runbook for the
  hourly data quality check and the `/admin/data-quality` dashboard.
- `packages/scraper-framework/src/telemetry/README.md` — full catalog of
  structured telemetry events and their `telemetry.data_quality_metrics` shapes.
- `packages/api/migrations/6_data-quality-metrics.sql` — table definition.
- `packages/api/migrations/15_schema-reorganization.sql` — moved the table to
  the `telemetry` schema; `search_path` includes `telemetry` so unqualified
  references still resolve.
