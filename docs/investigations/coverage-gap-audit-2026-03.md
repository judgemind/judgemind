# Coverage Gap Audit Report — Phase 2

Generated: 2026-03-11
Parent: #610
Issue: #612

## Overall Summary

| Package | Stmts Coverage | Branch Coverage | Test Count | Source |
|---|---|---|---|---|
| **scraper-framework** | **85%** (4090 stmts, 625 missed) | N/A (pytest-cov) | ~700+ | CI (run 22969481881) |
| **telegram-bridge** | **97%** (742 stmts, 21 missed) | N/A (pytest-cov) | 391 | CI (run 22972510209) |
| **api** | **93.18%** (stmts) | 81.42% | 183 | CI (run 22975578387) |
| **web** | **86.52%** (stmts) | 85.49% | 317 | Local run |

Risk-prioritized order (per issue spec): ingestion pipeline > scrapers > API resolvers > frontend.

---

## 1. scraper-framework (85% — highest priority gaps)

### Critical gaps (ingestion pipeline — highest risk)

| File | Coverage | Missed | Priority |
|---|---|---|---|
| `src/ingestion/worker.py` | **17%** | 252/302 stmts | **P1** — core ingestion pipeline orchestrator |
| `src/ingestion/extract.py` | **43%** | 81/141 stmts | **P1** — field extraction logic |
| `src/ingestion/db.py` | **64%** | 72/201 stmts | **P1** — database operations for ingestion |
| `src/ingestion/__main__.py` | **0%** | 37/37 stmts | P3 — CLI entry point |

### Moderate gaps (scrapers)

| File | Coverage | Missed | Priority |
|---|---|---|---|
| `src/courts/ca/fresno_tentatives.py` | **78%** | 54/243 stmts | P2 |
| `src/courts/ca/ventura_tentatives.py` | **93%** | 16/220 stmts | P3 |
| `src/courts/ca/la_tentatives.py` | **93%** | 23/321 stmts | P3 |
| `src/courts/ca/sc_tentatives.py` | **94%** | 15/238 stmts | P3 |

### Well-covered (95%+)

All framework modules (base, models, runner, storage, hashing, retry, event_bus, events, party_utils, court_directory, search/*) are at 97-100%.

Most scrapers are at 93-100%: cc_tentatives (98%), oc_tentatives (96%), oc_family_law (97%), oc_probate (93%), pdf_link_scraper (97%), riverside (99%), sb (98%), sf (98%), courtlistener (99%).

### Intentionally low-coverage

- `src/courts/ca/example.py` (0%) — example/template file, not production code
- `src/framework/__main__.py` (0%) — CLI entry point
- `src/ingestion/__main__.py` (0%) — CLI entry point

---

## 2. telegram-bridge (97% — near target)

| File | Coverage | Missed | Notes |
|---|---|---|---|
| `src/telegram_bridge/client.py` | 93% | 12/176 stmts | Likely real-API-only paths (e.g., actual HTTP calls) |
| `src/telegram_bridge/orchestrator.py` | 98% | 9/381 stmts | Near-complete |
| All others | 100% | 0 | interpreter, models, formatting, validation |

**Assessment:** This package is effectively complete. The 12 missed stmts in `client.py` are likely error-handling paths that require live Telegram API interactions.

---

## 3. api (93.18% — moderate gaps)

### Per-file details (from CI with PostgreSQL + OpenSearch)

| Module | Stmts | Branch | Notes |
|---|---|---|---|
| `src/index.ts` | **0%** | 0% | Server entry point |
| `src/app.ts` | **79.66%** | 65% | App setup |
| `src/alerts/index.ts` | **0%** | 0% | Barrel export |
| `src/alerts/digest.ts` | 100% | 50% | |
| `src/alerts/evaluate.ts` | 94.82% | 69.23% | |
| `src/auth/*` | **100%** | 96.55% | Fully covered |
| `src/data-access/db.ts` | 96.55% | 0% | |
| `src/email/*` | **100%** | 100% | Fully covered |
| `src/email/templates/digest.ts` | 97.41% | 82.75% | |
| `src/graphql/alert-resolvers.ts` | **100%** | 95% | |
| `src/graphql/auth-resolvers.ts` | **72.56%** | 79.48% | Auth mutations, lines 85-293, 297-382 |
| `src/graphql/data-quality.ts` | 99.26% | 84.84% | |
| `src/graphql/dataloader.ts` | 100% | 96.66% | |
| `src/graphql/judge-analytics.ts` | **86.69%** | 73.68% | Lines 93-198, 206-215 |
| `src/graphql/resolvers.ts` | 96.94% | 84% | |
| `src/graphql/schema.ts` | 100% | 100% | |
| `src/rest/document-download.ts` | 100% | 90% | |
| `src/search/client.ts` | 73.68% | 0% | |
| `src/search/search-rulings.ts` | 98.52% | 74.41% | |

### Integration test note

5 integration test suites (alerts, auth, graphql, judge-analytics, search-rulings) require PostgreSQL + OpenSearch, only runnable in CI. The CI coverage (93.18%) includes these; local unit-only coverage is 56.12%.

---

## 4. web (86.52% — needs targeted work)

### Gaps by area

| File/Area | Stmts Coverage | Priority |
|---|---|---|
| `src/app/layout.tsx` | **0%** | P3 — root layout |
| `src/app/page.tsx` | **0%** | P3 — homepage |
| `src/app/rulings/[id]/RulingDetail.tsx` | **0%** | **P2** — ruling detail component (121 lines) |
| `src/app/rulings/page.tsx` | **0%** | P3 — rulings list page |
| `src/app/search/page.tsx` | **0%** | P3 — search page (SSR) |
| `src/app/cases/[id]/not-found.tsx` | **0%** | P3 — not-found page |
| `src/app/judges/[id]/not-found.tsx` | **0%** | P3 — not-found page |
| `src/app/rulings/[id]/not-found.tsx` | **0%** | P3 — not-found page |
| `src/providers/ApolloProvider.tsx` | **0%** | P3 — provider wrapper (10 lines) |
| `src/providers/AuthProvider.tsx` | **0%** | P3 — auth state provider (65 lines) |
| `src/providers/ThemeProvider.tsx` | **0%** | P3 — theme provider (35 lines) |
| `src/lib/apollo-client.ts` | **0%** | P3 — Apollo client config (10 lines) |
| `src/app/auth/reset-password/page.tsx` | 89.79% | P3 |

### Well-covered (95%+)

- `src/lib/display-helpers.ts` — 99.56%
- `src/lib/auth-mutations.ts` — 100%
- Auth pages (login, register, forgot-password, verify-email) — 98-100%
- `src/components/auth/AuthCard.tsx` — 100%
- `src/components/layout/Header.tsx` — 100%
- `src/components/layout/Sidebar.tsx` — 100%
- `src/app/cases/[id]/CaseDetail.tsx` — 89.96%
- `src/app/judges/[id]/JudgeProfile.tsx` — 95.74%
- `src/app/rulings/RulingsFeed.tsx` — 97.09%
- `src/app/search/SearchPage.tsx` — 96.61%

---

## 5. Infrastructure issues found

### telegram-bridge coverage collection broken locally

The `--cov=src` config in `pyproject.toml` does not match the package structure (`src/telegram_bridge/`). Coverage collection works in CI but fails locally with "Module src was never imported." Should be fixed to `--cov=telegram_bridge`.

### scraper-framework test hangs locally

`test_riv_run_split_docs_have_individual_ruling_text` hangs indefinitely when run locally (macOS). CI runs these tests with `-n 8` (pytest-xdist parallelism) which may mask the issue. The CI also explicitly ignores `tests/test_reingest_from_s3.py`, `tests/test_reingest_registry.py`, `tests/test_ingestion.py`, and `tests/test_extract.py` — these contain integration-level tests that require external services.

---

## Follow-up Issues Filed

| # | Title | Priority | Package |
|---|---|---|---|
| #811 | `ingestion/worker.py` 17% -> 80%+ | P1 | scraper-framework |
| #812 | `ingestion/extract.py` 43% -> 80%+ | P1 | scraper-framework |
| #813 | `ingestion/db.py` 64% -> 85%+ | P1 | scraper-framework |
| #814 | `auth-resolvers.ts` 72% -> 90%+ | P2 | api |
| #815 | `judge-analytics.ts` 86% -> 95%+ | P2 | api |
| #816 | `fresno_tentatives.py` 78% -> 90%+ | P2 | scraper-framework |
| #817 | `RulingDetail.tsx` 0% -> 80%+ | P2 | web |
| #818 | Providers, layout, SSR pages | P3 | web |
| #819 | Fix telegram-bridge local coverage | P3 | telegram-bridge |
