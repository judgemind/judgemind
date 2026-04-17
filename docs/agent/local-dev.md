# Local Development Stack

Local Docker Compose stack, schema management, S3 cache, and rebuild scripts. CLAUDE.md contains a short pointer; this doc has the detail. For dev/cloud infrastructure see `docs/agent/infrastructure-reference.md`.

## Docker Compose services

A full local dev stack runs via Docker Compose: Postgres, Redis, OpenSearch, MinIO.

```
docker compose up -d postgres redis    # minimum for local work
docker compose up -d                   # full stack
```

**Local database:** `postgres://judgemind:localdev@localhost:5432/judgemind`

## Schema management

- `scripts/apply_migrations.sh` — applies all migrations (up sections only) to local DB.
- `scripts/regenerate_schema.sh` — regenerates `schema.sql` from migrations (run after adding a migration).
- `scripts/check_schema_drift.sh` — verifies `schema.sql` matches applied migrations.
- `schema.sql` is auto-generated — **do not edit directly**. Add a migration, then run `regenerate_schema.sh`.

## S3 local cache

Set `S3_CACHE_DIR=/tmp/judgemind-archive` to cache S3 objects on local disk. All S3 reads/writes go through the cache transparently.

- `scripts/s3_cache.py sync` — bulk download all S3 objects to local cache.
- `S3_LOCAL_ONLY=1` — fully offline mode, no S3 contact at all.
- Content-addressed keys mean cached files never go stale.

## Full local DB rebuild from S3

```
scripts/rebuild_db.sh              # drop DB, fetch rosters, rebuild with LLM
scripts/rebuild_db.sh --no-llm     # regex-only (no API key needed)
scripts/rebuild_db.sh --skip-reset # incremental, keep existing data
```

This rebuilds the entire database from archived S3 content: seeds courts from S3 key prefixes, fetches court directory rosters for judge resolution, then processes every document through the ingestion pipeline (LLM extraction + enrichment). The database is a derived index — the S3 archive is the source of truth.

**Roster fetching:** `scripts/fetch_rosters.py` fetches dept-to-judge directory snapshots from all 9 CA county court websites. Runs automatically as part of `rebuild_db.sh` and daily scraper runs.

## Environment variables for local dev

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Postgres connection | (required) |
| `REDIS_URL` | Redis connection | `redis://localhost:6379` |
| `S3_CACHE_DIR` | Local S3 cache directory | (disabled) |
| `S3_LOCAL_ONLY` | Skip S3, cache-only | `0` |
| `JUDGEMIND_ARCHIVE_BUCKET` | S3 bucket name | `judgemind-document-archive-dev` |
| `GOOGLE_API_KEY` | Gemini LLM extraction | (optional, via `scripts/with-secret.sh`) |
| `REBUILD_CONCURRENCY` | Parallel threads for rebuild | `8` |
| `OPENSEARCH_URL` | OpenSearch endpoint | (optional, mocked if absent) |
