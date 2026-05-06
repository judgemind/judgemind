---
name: Task
about: A unit of work for an agent or contributor
title: "[AREA] "
labels: status/triage
assignees: ''
---

## Context

Why this task exists. Link to relevant specs, investigations, or parent tasks.

Parent issue: (if applicable)

<!--
## Bug-fix framing (optional — use for bug-fix or investigation-style issues)

When filing an issue that names a specific function/regex/layer as broken, separate the three pieces explicitly so an agent doesn't lock onto a wrong-hypothesis path. See `docs/agent/issue-authoring.md` §Hypothesis vs. evidence and `.claude/skills/task/SKILL.md` §4c. Uncomment and fill in the three sections below if applicable.

### Observed Symptoms

Facts you measured. NULL rate is X%, this query returns Y, sample input <S3 key> produces `field=NULL` after parse.

### Suspected root cause

Your best current theory of *why* the symptoms appear, framed as a hypothesis the agent should verify — NOT as a known-true premise. Example: "I suspect `_cc_hearing_date_from_pdf`'s regex doesn't match the dept-38 PDF format."

### Hypothesis verification steps

Cheap probes the agent should run BEFORE writing implementation code. Example:
1. Download `<S3 key>` to {worktree}/tmp/sample.pdf.
2. Run `_cc_hearing_date_from_pdf` on it directly (write a 5-line probe to {worktree}/tmp/verify.py).
3. If it returns the expected date, the hypothesis is wrong — root-cause from the observed symptoms instead. Inspect `is_plausible_hearing_date` next.
4. If it returns NULL, the hypothesis is confirmed — proceed with the prescribed fix.
-->

## Objective

One clear sentence: what does "done" look like?

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Tests written and passing
- [ ] Documentation updated (if applicable)

## Constraints

- Dependencies:
- Technologies to use:
- Cost considerations:
- Decisions requiring human approval:

<!--
## Feasibility check (external-integration issues only)

Required if this task proposes querying a third-party website or API (court case-search endpoints, public records APIs, etc.). Complete before labeling `agent/ready` — see `docs/agent/issue-authoring.md` §Writing Acceptance Criteria.

- Endpoint: `https://...`
- Anonymous access confirmed: yes/no (how verified)
- No reCAPTCHA / Cloudflare / login wall on query path: yes/no
- Returns expected data for a realistic sample: yes/no (sample response snippet)
-->

## Relevant Specs

- [ ] Product Spec (`docs/specs/product-spec-v3.md`) — Section:
- [ ] Architecture Spec (`docs/specs/architecture-spec-v1.md`) — Section:
- [ ] Investigation Report — Which:

## Investigation Notes

_(Filled in by the agent during work — findings, decisions made, problems encountered, sub-tasks created)_
