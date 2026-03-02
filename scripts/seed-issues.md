# Initial GitHub Issues — Create After Repo Setup

Run these with `gh issue create` or create manually. These are the Week 1-2 tasks from the implementation plan.

## Batch 1: Infrastructure (create first)

```bash
gh issue create \
  --title "[INFRA] Set up Terraform S3 document archive bucket" \
  --label "area/infra,priority/p1,type/infrastructure,agent/ready" \
  --body "## Context
The document archive is the single most critical piece of infrastructure. Tentative rulings are ephemeral — every day without archival is data permanently lost.

## Objective
S3 bucket with versioning, encryption, lifecycle rules, and object lock (production only) is deployed via Terraform.

## Acceptance Criteria
- [ ] Terraform module in \`infra/terraform/modules/storage/\` deploys the bucket
- [ ] Versioning enabled
- [ ] Object lock configured for production environment
- [ ] Lifecycle rules: Standard → IA at 90 days → Glacier at 365 days
- [ ] Public access blocked
- [ ] Terraform plan runs clean in CI

## Relevant Specs
- Architecture Spec Section 4.4 (Object Storage)
- Architecture Spec Section 7.5.1 (Document Archive backup)"

gh issue create \
  --title "[INFRA] Set up Terraform RDS PostgreSQL module" \
  --label "area/infra,priority/p1,type/infrastructure,agent/ready" \
  --body "## Context
PostgreSQL is the primary structured data store for all entities (cases, judges, attorneys, parties, documents, rulings).

## Objective
RDS PostgreSQL instance deployed via Terraform, accessible from ECS tasks and local dev.

## Acceptance Criteria
- [ ] Terraform module in \`infra/terraform/modules/database/\`
- [ ] db.t4g.medium for dev/staging, configurable for production
- [ ] Automated backups with 30-day retention
- [ ] Security group restricting access to VPC
- [ ] Connection string stored in AWS Secrets Manager
- [ ] Terraform plan runs clean in CI

## Relevant Specs
- Architecture Spec Section 4.1 (PostgreSQL)"

gh issue create \
  --title "[INFRA] Set up GitHub Actions CI pipeline" \
  --label "area/devops,priority/p1,type/infrastructure,agent/ready" \
  --body "## Context
Every PR needs automated checks before review. CI should lint, test, and build each package, but only for packages that changed.

## Objective
GitHub Actions workflow that runs relevant checks on PR, with path-based filtering.

## Acceptance Criteria
- [ ] Workflow triggers on PR to main
- [ ] Path-based filtering: only runs jobs for changed packages
- [ ] Python packages: ruff lint + format check + pytest
- [ ] TypeScript packages: eslint + typecheck + test + build
- [ ] Terraform: fmt check + validate
- [ ] All jobs pass on the initial empty scaffolding

## Notes
Starter workflow already exists in \`.github/workflows/ci.yml\` — verify and extend as needed."
```

## Batch 2: Data Model & Scraper Framework (create next)

```bash
gh issue create \
  --title "[DATA-MODEL] Design and implement PostgreSQL schema for core entities" \
  --label "area/data-model,priority/p1,type/feature,agent/ready" \
  --body "## Context
The core entity model has 6 primary entities: Case, Judge, Attorney, Party, Document, Ruling. Schema must handle the messiness of court data, especially entity resolution.

## Objective
SQL schema file that creates all tables, indices, and constraints for the core entity model.

## Acceptance Criteria
- [ ] Schema file at \`packages/api/src/data-access/schema.sql\`
- [ ] All 6 entities from Architecture Spec Section 4.1.1
- [ ] Entity resolution support: canonical records + alias tables
- [ ] Staging area tables (separate from production) per Section 3.5.2
- [ ] Indices for common query patterns (by judge, by case number, by date range)
- [ ] Works with docker-compose PostgreSQL instance
- [ ] Migration tooling chosen and configured

## Relevant Specs
- Architecture Spec Section 4.1 (PostgreSQL)
- Architecture Spec Section 4.1.2 (Entity Resolution)"

gh issue create \
  --title "[SCRAPING] Build scraper framework base classes" \
  --label "area/scraping,priority/p1,type/feature,agent/ready" \
  --body "## Context
Every scraper follows the same contract: configuration, execution, output, error handling, health reporting. The framework provides base classes so per-court scrapers only implement court-specific logic.

## Objective
Base scraper framework with abstract classes, scheduling, content hashing, health reporting, and event emission.

## Acceptance Criteria
- [ ] \`BaseScraper\` abstract class with config, execute, health report methods
- [ ] Content hashing utility (SHA-256)
- [ ] Standardized ingestion event schema (Pydantic models)
- [ ] S3 archival utility for raw captured content
- [ ] Health reporting (last run, status, record count, response time)
- [ ] Configurable scheduling (polling frequency, time-of-day windows)
- [ ] Exponential backoff retry logic
- [ ] Unit tests for all utilities
- [ ] At least one example/dummy scraper showing the pattern

## Relevant Specs
- Architecture Spec Section 3.1 (Scraper Framework)
- Architecture Spec Section 3.1.1 (Lessons from Prior Implementation)"

gh issue create \
  --title "[SCRAPING] Build LA County tentative ruling scraper (Pattern 1)" \
  --label "area/scraping,priority/p1,type/feature,agent/ready" \
  --body "## Context
LA County is the highest-volume single court. Tentative rulings are published via an ASP.NET WebForms page with dropdown enumeration. This is the most important individual scraper.

## Objective
Production-ready scraper that captures all published LA County civil tentative rulings daily.

## Acceptance Criteria
- [ ] Implements \`BaseScraper\` from framework
- [ ] Fetches dropdown options from main.aspx page
- [ ] Handles ASP.NET ViewState/EventValidation tokens
- [ ] Submits form for each dropdown option
- [ ] Extracts: case number, courthouse, department, hearing date, ruling text
- [ ] Content hashes each ruling
- [ ] Archives raw HTML to S3
- [ ] Regression tests against archived sample pages
- [ ] Handles edge cases: multi-week postings (Pomona South), per-department formatting

## Relevant Specs
- LA Court Investigation (full document)
- CA County Investigation Section Pattern 1
- Architecture Spec Section 3.3 (Tentative Ruling Capture)

## Key URLs
- Civil tentatives: https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil"

gh issue create \
  --title "[SCRAPING] Build PDF-link scraper template (Pattern 2) — Orange County first" \
  --label "area/scraping,priority/p1,type/feature,agent/ready" \
  --body "## Context
Pattern 2 courts publish tentative rulings as PDF files linked from an index page. This template covers Orange, Riverside, San Bernardino, and potentially 10+ smaller counties with minimal per-county customization.

## Objective
Parameterized scraper template that works for any Pattern 2 court, with Orange County as the first implementation.

## Acceptance Criteria
- [ ] \`PdfLinkScraper\` template class extending \`BaseScraper\`
- [ ] Parameterized: index URL, link selector, PDF URL pattern, judge name regex, courthouse mapping
- [ ] Scrapes index page for PDF links
- [ ] Downloads each PDF
- [ ] Extracts text from PDFs (pdfplumber)
- [ ] Parses rulings from extracted text
- [ ] Orange County configuration implemented and tested
- [ ] Riverside County configuration implemented and tested
- [ ] San Bernardino County configuration implemented and tested
- [ ] Regression tests against sample PDFs

## Relevant Specs
- CA County Investigation Section Pattern 2
- Architecture Spec Section 3.1 (Scraper Framework)"
```

## Batch 3: Investigation Tasks

```bash
gh issue create \
  --title "[INVESTIGATE] Capture sample tentative rulings for test fixtures" \
  --label "area/scraping,priority/p1,type/investigation" \
  --body "## Question
What do actual tentative ruling pages look like across LA, Orange, Riverside, and San Bernardino courts? We need real samples for regression testing.

## Scope
- Manually capture 10-20 sample pages from each court
- Include: typical rulings, multi-ruling pages, holiday entries, different department formats
- Save as HTML/PDF fixtures in \`packages/scraper-framework/tests/fixtures/\`
- Document the expected parsed output for each fixture

## Expected Output
- [ ] 40-80 fixture files across 4 courts
- [ ] Expected output JSON for each fixture
- [ ] README documenting what each fixture tests"

gh issue create \
  --title "[INVESTIGATE] San Francisco tentative ruling endpoints (Pattern 3)" \
  --label "area/scraping,priority/p2,type/investigation" \
  --body "## Question
SF uses a custom DLL-based web app. Are the endpoints simple GET requests? Does the probate CAPTCHA block automated access? How many endpoints need coverage?

## Scope
- Test each endpoint URL listed in the CA County Investigation
- Document response format (HTML structure)
- Test probate CAPTCHA: is it always present? What type?
- Count total endpoints needing coverage

## Expected Output
- [ ] Findings documented in this issue
- [ ] Sub-tasks created for SF scraper implementation if feasible
- [ ] Sample response pages saved as test fixtures"
```

## Batch 4: API & Frontend Scaffold

```bash
gh issue create \
  --title "[API] Set up GraphQL API scaffold with Apollo Server + Fastify" \
  --label "area/api,priority/p2,type/feature,agent/ready" \
  --body "## Context
The API is the backbone — the frontend and all external consumers go through it.

## Objective
Working GraphQL API server with initial schema for cases and judges, connected to PostgreSQL.

## Acceptance Criteria
- [ ] Apollo Server 4 + Fastify running
- [ ] Initial GraphQL schema: Case, Judge, Ruling types with basic queries
- [ ] PostgreSQL connection via pg pool
- [ ] Health check endpoint
- [ ] Dev server runs with \`npm run dev\`
- [ ] Basic integration test

## Relevant Specs
- Architecture Spec Section 6.1 (API Architecture)"

gh issue create \
  --title "[FRONTEND] Set up Next.js project with Tailwind and basic layout" \
  --label "area/frontend,priority/p2,type/feature,agent/ready" \
  --body "## Context
The web app is the user-facing interface. Start with a clean scaffold that agents can build features into.

## Objective
Next.js app with Tailwind CSS, basic layout (header, sidebar, main content), and routing structure.

## Acceptance Criteria
- [ ] Next.js 14+ with App Router
- [ ] Tailwind CSS configured
- [ ] Layout: header with navigation, responsive sidebar, main content area
- [ ] Route structure: /, /search, /cases/[id], /judges/[id], /rulings
- [ ] Apollo Client configured for GraphQL
- [ ] Dark/light mode support
- [ ] Builds and runs with \`npm run dev\`

## Relevant Specs
- Architecture Spec Section 6.2 (Web Application)"
```

## Labels to Create First

```bash
# Area labels
gh label create "area/scraping" --color "0E8A16" --description "Scraper framework and court scrapers"
gh label create "area/api" --color "1D76DB" --description "GraphQL and REST API"
gh label create "area/frontend" --color "5319E7" --description "Next.js web application"
gh label create "area/infra" --color "FBCA04" --description "Infrastructure and Terraform"
gh label create "area/nlp" --color "D93F0B" --description "NLP pipeline and AI processing"
gh label create "area/data-model" --color "0075CA" --description "Database schema and data model"
gh label create "area/devops" --color "BFD4F2" --description "CI/CD, monitoring, deployment"
gh label create "area/docs" --color "C5DEF5" --description "Documentation"

# Priority labels
gh label create "priority/p1" --color "B60205" --description "Critical — do first"
gh label create "priority/p2" --color "FF9F1C" --description "Important — do soon"
gh label create "priority/p3" --color "0E8A16" --description "Nice to have"

# Type labels
gh label create "type/feature" --color "A2EEEF" --description "New feature or capability"
gh label create "type/bug" --color "D73A4A" --description "Something is broken"
gh label create "type/investigation" --color "D4C5F9" --description "Research before implementation"
gh label create "type/infrastructure" --color "BFD4F2" --description "Infrastructure setup or change"
gh label create "type/decision" --color "FBCA04" --description "Requires human decision"

# Status labels
gh label create "status/blocked" --color "B60205" --description "Cannot proceed — needs input"
gh label create "status/needs-decision" --color "FBCA04" --description "Waiting on human decision"
gh label create "status/needs-review" --color "0075CA" --description "PR open, needs review"

# Agent labels
gh label create "agent/ready" --color "0E8A16" --description "Fully specified, ready for an agent"

# Special
gh label create "scraper-health" --color "D93F0B" --description "Scraper health tracking"
```
