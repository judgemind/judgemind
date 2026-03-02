# Contributing to Judgemind

## For AI Agents

Read `.claude/instructions.md` before starting work. It covers code standards, git workflow, and rules.

## For Human Contributors

### Getting Started

1. Fork the repo and clone locally
2. Run `docker compose up -d` for local services
3. Pick an issue labeled `help-wanted` or `good-first-issue`
4. Create a branch: `feat/issue-{N}-description`
5. Open a PR referencing the issue

### Code Standards

- **Python** (scrapers, NLP): Type hints, pytest, ruff for lint/format
- **TypeScript** (API, frontend): Strict mode, ESLint + Prettier
- All code needs tests

### What We Need Help With

- **Scraper development**: Each court needs a scraper. If you know a court system well, your knowledge is invaluable.
- **Legal review**: AI-generated judge biographies, motion playbooks, and analytics need attorney review.
- **Historical data**: If you have archived tentative rulings or court data, we'd love to discuss ingestion.
- **Bug reports**: If you find bad data, broken scrapers, or incorrect analytics, file an issue.

### Code of Conduct

Be kind, be constructive, be honest about limitations. This is a public-interest project.
