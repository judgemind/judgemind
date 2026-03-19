# api

Judgemind's backend API server. Exposes a GraphQL API for the web frontend and REST endpoints for third-party integrations and document downloads. Built on Fastify + Apollo Server.

## Key Entry Points

- **`src/index.ts`** -- Server bootstrap. Starts Fastify on port 3001 (configurable via `PORT`).
- **`src/app.ts`** -- Application factory. Wires up Apollo Server (GraphQL), CORS, auth middleware, and REST routes.
- **`src/graphql/schema.ts`** -- GraphQL type definitions (courts, judges, cases, rulings, alerts, auth, search, data quality).
- **`src/graphql/resolvers.ts`** -- Query and mutation resolvers.
- **`src/rest/document-download.ts`** -- REST endpoint for downloading original documents from S3.
- **`src/data-access/db.ts`** -- PostgreSQL connection pool (pg).
- **`migrations/`** -- Database migrations via node-pg-migrate.

## What It Consumes (Inputs)

- **PostgreSQL** -- Primary data store for all structured data (courts, judges, cases, rulings, users, alerts). Connection via `DATABASE_URL`.
- **OpenSearch** -- Full-text search index for ruling search. Connection via `OPENSEARCH_URL`.
- **S3** -- Pre-signed URL generation for document downloads. Bucket via `JUDGEMIND_ARCHIVE_BUCKET`.
- **Redis** -- Caching (via `REDIS_URL`).

## What It Produces (Outputs)

- **GraphQL API** (`/graphql`) -- Used exclusively by the web frontend. Queries: courts, judges, cases, rulings, search, judge analytics, data quality, alerts. Mutations: auth (register, login, refresh), alert CRUD.
- **REST endpoints** -- `/api/documents/:id/download` for S3 document downloads with pre-signed URLs.
- **JWT tokens** -- Authentication via access + refresh token pair.

## Install, Test, and Run Locally

```bash
npm install

# Lint and typecheck
npm run lint
npm run typecheck

# Run tests
npm test

# Run migrations
DATABASE_URL=... npm run db:migrate

# Start dev server
DATABASE_URL=... REDIS_URL=... OPENSEARCH_URL=... npm run dev
```

See `docs/specs/architecture-spec-v1.md` Section 6 for the full API architecture.
