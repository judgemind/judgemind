# ADR-001: Monorepo with GitHub Issues as Task System

## Status

Accepted — March 2026

## Context

Judgemind development is primarily AI-agent-driven, with human review at merge points. We need:

1. A code repository structure
2. A task management system agents can interact with programmatically
3. CI/CD that runs automatically

Options considered:

- **Repo structure:** Monorepo vs. multi-repo (scraper-framework, api, web, infra as separate repos)
- **Task system:** GitHub Issues + Projects vs. Linear vs. Jira vs. custom
- **IaC:** Terraform vs. Pulumi vs. AWS CDK

## Decision

- **Monorepo** with clear package boundaries under `packages/`
- **GitHub Issues + GitHub Projects** for task management
- **Terraform** for infrastructure-as-code

## Rationale

**Monorepo:** Shared types, atomic cross-package changes, single CI configuration, easier for agents to navigate. The packages are related enough (shared data models, event schemas) that cross-repo coordination would add friction.

**GitHub Issues:** Claude Code has native GitHub integration — it can create issues, branches, PRs, and link them without additional tooling. Linear or Jira would require building MCP integrations. The task system lives with the code, reducing context switching.

**Terraform:** Most widely adopted IaC tool. Extensive AWS provider. Large ecosystem of modules. Agents are well-trained on HCL syntax. No strong reason to choose alternatives.

## Consequences

- GitHub Issues is less powerful than Linear for project management (no sprint planning, limited views). Acceptable tradeoff for agent integration.
- Monorepo means all CI runs in one pipeline (mitigated by path-based filtering).
- Terraform state management requires a bootstrap step (creating the state bucket manually before Terraform can manage itself).
