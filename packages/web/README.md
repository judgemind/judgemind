# web

Judgemind's web application. A Next.js 14 app that serves as the primary user interface for searching rulings, viewing judge analytics, and managing alerts. Communicates exclusively with the API's GraphQL endpoint.

## Key Entry Points

- **`src/app/page.tsx`** -- Landing page.
- **`src/app/rulings/`** -- Rulings feed (browse and filter tentative rulings).
- **`src/app/judges/`** -- Judge profiles and analytics (grant/deny rates, motion breakdowns).
- **`src/app/cases/`** -- Case detail pages.
- **`src/app/search/`** -- Full-text search across rulings and cases.
- **`src/app/admin/`** -- Admin dashboard (data quality metrics, scraper health).
- **`src/app/auth/`** -- Login, registration, and OAuth flows.
- **`src/lib/`** -- Shared utilities (GraphQL client, auth helpers).
- **`src/components/`** -- Reusable React components.

## What It Consumes (Inputs)

- **GraphQL API** -- All data fetched from `judgemind-api` via Apollo Client. Endpoint configured via `NEXT_PUBLIC_API_URL`.

## What It Produces (Outputs)

- **Server-rendered HTML** -- SEO-friendly pages for court data (judge profiles, case summaries, ruling text).
- **Client-side SPA** -- Responsive single-page navigation after initial load.

## Install, Test, and Run Locally

```bash
npm install

# Lint and typecheck
npm run lint
npm run typecheck

# Run tests
npm test

# Build (validates SSR, catches import errors)
npm run build

# Start dev server
NEXT_PUBLIC_API_URL=http://localhost:3001 npm run dev
```

See `docs/specs/architecture-spec-v1.md` Section 6.2 for the frontend architecture.
