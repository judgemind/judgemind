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

## Attributing validation fails to raw S3 keys

`telemetry.validation_results` has one row per validation-gate outcome
(`flag`, `fail`, `error`). A `fail` never writes a `derived.documents` row,
and split children have uuid5 ids, so `document_id` alone can't take you to
the raw capture. Since #4706 (migration 66, worker PR #4773) the worker also
writes `county`, `scraper_id` and `s3_key` on every row. Split children
carry the parent event's `s3_key`. The first attributed row on dev was
written at 2026-09-26T00:18Z.

For a rule-by-rule, county-by-county breakdown, run
`scripts/attribute_validation_fails.py`. It is read-only (`read_only`
session):

```bash
# fails since the #4706 deploy, grouped by county, rule and s3_key
scripts/ecs-run-task.sh scripts/attribute_validation_fails.py
# one county, a narrower window, more keys
scripts/ecs-run-task.sh scripts/attribute_validation_fails.py -- \
    --county "Santa Clara" --since 2026-10-01T00:00:00Z --top 50
# flags for one rule, as JSON
scripts/ecs-run-task.sh scripts/attribute_validation_fails.py -- \
    --result flag --rule case_number_not_unknown --json
```

The table does not store which rule fired, only the `reason` text, where
several deterministic reasons are joined with `"; "`. The script maps each
rule in `validation/deterministic.py` to a fixed fragment of its reason
(`RULE_MARKERS`). One row can match more than one rule. LLM-gate rows
(`model` other than `deterministic`) are reported as `llm`. A drift test in
`scripts/tests/test_attribute_validation_fails.py` fails when a rule's reason
text stops matching its marker.

For a quick look without the script:

```bash
scripts/dev-db-query.sh "SELECT county, s3_key, count(*) FROM telemetry.validation_results WHERE result = 'fail' AND created_at >= '2026-09-26' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20"
```

### Rows from before #4706 (NULL attribution)

Rows written before the #4773 deploy have NULL `county` / `scraper_id` /
`s3_key`. On dev, as of 2026-11-12, that is 24,882 `fail` and 28,454 `flag`
rows (2026-04-16 to 2026-09-25). They were **not backfilled**:

- `telemetry.*` can't be rebuilt from S3. A backfill would rewrite
  accumulated rows with values inferred later, not values the worker saw
  at write time.
- Only 11,280 of the 24,882 NULL `fail` rows still have a
  `derived.documents` row for their `document_id`. Those are documents a
  later re-ingest wrote, so the fail row is stale anyway. The rest are
  failed split children and documents removed since then (the #4725
  lingering rows). To recover those you would have to list every county's
  raws and recompute `make_split_document_id(parent, 0..N)`. That is the
  tool this issue first proposed, and it is a lot of work for a window
  that predates the splitter and validation fixes since then.
- Rebuilds and re-ingests after 2026-09-26 write new, attributed rows for
  the same raws. For current state, query the new rows.

When you do need the old window, `--legacy` attributes NULL rows at read
time through `document_id -> derived.documents` (attribution `document`).
Rows it can't resolve are reported as `none`. Nothing is written.

## Related resources

- `docs/runbooks/data-quality-monitoring.md` — operational runbook for the
  hourly data quality check and the `/admin/data-quality` dashboard.
- `packages/scraper-framework/src/telemetry/README.md` — full catalog of
  structured telemetry events and their `telemetry.data_quality_metrics` shapes.
- `packages/api/migrations/6_data-quality-metrics.sql` — table definition.
- `packages/api/migrations/15_schema-reorganization.sql` — moved the table to
  the `telemetry` schema; `search_path` includes `telemetry` so unqualified
  references still resolve.
