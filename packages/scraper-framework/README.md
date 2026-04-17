# scraper-framework

Court data scraping framework and ingestion pipeline for Judgemind. This is the most operationally critical package in the system — tentative rulings are ephemeral at the source (courts remove them within days), so a scraper outage during the capture window is unrecoverable data loss. Once captured to S3, the raw is durable and all downstream `derived.*` state is rebuildable.

## Key Entry Points

- **`src/framework/base.py`** -- `BaseScraper` abstract class that all court scrapers implement. Handles content hashing, S3 archival, event emission, retry, and health reporting.
- **`src/framework/runner.py`** -- Scraper execution engine. Runs scrapers on schedule per their `ScraperConfig`.
- **`src/ingestion/worker.py`** -- Long-lived Redis Streams consumer (ECS Fargate). Processes `document.captured` events and writes structured data to PostgreSQL and OpenSearch.
- **`src/courts/ca/`** -- California court scraper implementations (LA, OC, SF, San Diego, Riverside, etc.).

## What It Consumes (Inputs)

- **Court websites** -- HTTP/browser requests to court sites for tentative rulings and docket data.
- **S3 archived documents** -- For reingestion via `scripts/reingest_from_s3.py`.
- **Redis Streams** -- The ingestion worker reads from the `document.captured` stream.
- **LLM APIs** -- Anthropic or Google GenAI for Tier 2 field extraction (configurable via `LLM_PROVIDER` / `LLM_MODEL` env vars).

## What It Produces (Outputs)

- **Redis Streams events** -- `document.captured` (scraper output) and `scraper.health` (operational metrics).
- **S3 objects** -- Archived raw documents (immutable, path: `/{state}/{county}/{court}/{case_id}/...`).
- **PostgreSQL rows** -- Courts, judges, cases, documents, rulings, and parties via the ingestion worker's three-tier extraction pipeline (scraper fields -> LLM extraction -> regex fallback).
- **OpenSearch index** -- `tentative_rulings` index for full-text search, populated by the ingestion worker.

## Install, Test, and Run Locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Lint and format
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/

# Run tests
.venv/bin/pytest tests/ -v

# Run a scraper (example)
DATABASE_URL=... REDIS_URL=... .venv/bin/python -m framework --scraper ca-la-tentative

# Run the ingestion worker
DATABASE_URL=... REDIS_URL=... OPENSEARCH_URL=... .venv/bin/python -m ingestion
```

See `docs/scraper-lessons.md` for common pitfalls and `docs/specs/architecture-spec-v1.md` Section 3 for the full ingestion architecture.
