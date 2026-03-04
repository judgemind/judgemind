# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Read `.claude/instructions.md` before doing anything.** It contains the full agent workflow, session setup, worktree isolation, task pickup, and PR lifecycle. Everything below supplements those instructions.

## Build & Test Commands

### Local services (required for API and integration tests)

```bash
docker compose up -d   # PostgreSQL, Redis, OpenSearch, Qdrant, MinIO
```

### Python packages (scraper-framework, nlp-pipeline)

Run from the package directory (e.g. `packages/scraper-framework/`):

```bash
.venv/bin/pip install -e ".[dev]"           # Install deps
.venv/bin/ruff check src/ tests/            # Lint
.venv/bin/ruff format --check src/ tests/   # Format check
.venv/bin/pytest tests/ -v --tb=short       # Run all tests
.venv/bin/pytest tests/test_foo.py -v       # Run a single test file
.venv/bin/pytest tests/ -n auto             # Parallel tests (scraper-framework only, uses pytest-xdist)
.venv/bin/ruff check --fix src/ tests/      # Auto-fix lint
.venv/bin/ruff format src/ tests/           # Auto-fix format
```

Each agent/worktree must create its own venv — never share venvs across worktrees:
```bash
python3.12 -m venv packages/<pkg>/.venv
```

### TypeScript packages (api, web)

Run from the package directory (e.g. `packages/api/`):

```bash
npm install             # Install deps
npm run lint            # ESLint
npm run typecheck       # tsc --noEmit
npm test                # Vitest (all tests)
npm run dev             # Dev server (tsx watch for api, next dev for web)
npm run build           # Build (web only — also required as a pre-PR check)
```

### Terraform (infra/terraform/)

```bash
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

### Pre-push hook

The `.githooks/pre-push` hook runs ruff, eslint, and terraform fmt automatically on changed packages. Configure it per worktree:
```bash
git config core.hooksPath .githooks
```

## Architecture

Monorepo with four packages and shared infrastructure:

- **`packages/scraper-framework`** (Python) — Court website scrapers. Base class in `src/framework/base.py`; court-specific scrapers in `src/courts/ca/`. Every scraper extends the `Scraper` base class, reports health metrics, and archives raw content with SHA-256 hashing to S3 before processing. Scraper configs (URLs, selectors, schedules) are separated from logic. Test fixtures are archived real pages in `tests/fixtures/`.

- **`packages/nlp-pipeline`** (Python) — Entity extraction, classification, embeddings using Anthropic/OpenAI APIs, sentence-transformers, and Qdrant for vector storage. SQLAlchemy + psycopg for DB access.

- **`packages/api`** (TypeScript) — Fastify server with Apollo GraphQL and REST endpoints. Data access layer in `src/data-access/` (raw SQL via pg). Migrations via node-pg-migrate. Auth in `src/auth/`, search in `src/search/` (OpenSearch), alerts in `src/alerts/`, email via SES in `src/email/`.

- **`packages/web`** (TypeScript) — Next.js 14 frontend with Tailwind CSS. Consumes the API via Apollo Client. App router in `app/`, shared components in `components/`.

**Key architectural rules:**
- API-first: the web app is a client of the API; every UI feature has an API endpoint.
- Two data pipelines: Model A captures ephemeral California tentative rulings (scraper reliability is highest priority — data is permanently lost if scraper is down). Model B extracts judge analytics from persistent docket/document data.
- Cost-conscious: prefer fixed-cost over usage-based; never assume unlimited budget.

**Infrastructure:** Terraform modules in `infra/terraform/modules/`, per-environment configs in `infra/terraform/environments/`. AWS region `us-west-2`. All resources tagged `project=judgemind`.

**CI:** GitHub Actions in `.github/workflows/ci.yml` — path-filtered jobs run only for changed packages. Branch protection requires the `ci-passed` gate.

**Specs:** `docs/specs/` contains product spec, architecture spec, and court investigation reports. Read these before working on related features.

## Conventions

- Conventional commits: `feat(scraping): implement OC PDF link scraper (#42)`
- Branch naming: `feat/issue-{N}-short-description` from `main`
- When working on a GitHub issue, rename the conversation to `#<N> — <short title>` (e.g. `#42 — Deploy LA/OC/Riverside scrapers`)
- Node.js 20+ (activate via `source ~/.nvm/nvm.sh && nvm install 20 --no-progress`)
- Python 3.12+ with hatchling build system
- Ruff lint rules: E, F, I, N, UP, ANN (ignore ANN401)
- asyncio_mode = "auto" for pytest-asyncio
- Never hardcode secrets or URLs to live court sites in source code
