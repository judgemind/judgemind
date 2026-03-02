# Judgemind

**Free, open-source legal research & litigation intelligence.**

Public court data should be publicly searchable. Judgemind provides the same caliber of state trial court data, judge analytics, and AI-powered litigation tools that commercial platforms charge $70–$200+/month for — free and open source.

## Status

🚧 **Under active development.** Data capture is live for select California counties. Features are shipping incrementally.

## What This Does

- **Court data search** — Dockets, documents, and rulings across state trial courts
- **Tentative ruling archive** — California judges' pre-hearing decisions, captured before they expire
- **Judge analytics** — Grant/deny rates, ruling tendencies, motion-specific analysis
- **AI-powered tools** — Document summarization, case assessment, motion drafting, deposition prep
- **Alerts** — Real-time notifications on case activity, judge rulings, and party filings
- **Open API** — REST and GraphQL APIs for developers and researchers

## Architecture

Monorepo with four main packages:

| Package | Language | Purpose |
|---------|----------|---------|
| `packages/scraper-framework` | Python | Court website scrapers and data capture |
| `packages/nlp-pipeline` | Python | Entity extraction, classification, embeddings |
| `packages/api` | TypeScript | GraphQL + REST API |
| `packages/web` | TypeScript | Next.js frontend |

Infrastructure is defined in `infra/terraform/` and deployed via GitHub Actions.

See `docs/specs/` for the full product spec, architecture spec, and investigation reports.

## Local Development

```bash
# Start local services (PostgreSQL, Redis, OpenSearch, Qdrant, MinIO)
docker compose up -d

# Scraper framework
cd packages/scraper-framework
pip install -e ".[dev]"
pytest tests/

# API
cd packages/api
npm install
npm run dev

# Frontend
cd packages/web
npm install
npm run dev
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). All PRs require review before merge.

If you're an AI agent working on this codebase, read [.claude/instructions.md](.claude/instructions.md) first.

## License

Apache 2.0. See [LICENSE](LICENSE).

## Why This Exists

Solo practitioners, public defenders, legal aid attorneys, law students, and self-represented litigants deserve access to the same litigation intelligence tools that large firms take for granted. Judgemind makes that free.
