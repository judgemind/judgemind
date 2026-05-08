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

## Observed symptoms

<!-- Optional — fill in for bug-fix or investigation-style issues; otherwise delete this section and the next two (Suspected root cause, Hypothesis verification steps). -->

Facts you measured. NULL rate is X%, this query returns Y, sample input `<S3 key>` produces `field=NULL` after parse.

## Suspected root cause

<!-- Optional — your best current theory of *why* the symptoms appear, framed as a hypothesis the agent should verify, NOT as a known-true premise. Delete this section if not applicable. -->

Example: "I suspect `_cc_hearing_date_from_pdf`'s regex doesn't match the dept-38 PDF format."

## Hypothesis verification steps

<!--
Optional but high-leverage when a Suspected root cause is named above. List the cheap probes the agent should run BEFORE writing implementation code — the agent's `/task` §4c step runs this section verbatim before writing code.

See `docs/agent/issue-authoring.md` §"Hypothesis vs. evidence" for the full framing rationale, and §"Verify stored state matches the symptom" for the storage-tier (S3 / `derived.*` / `staging.*`) probe variant.

Delete this section if there is no suspected root cause to verify — e.g. pure feature work or trivial typo fixes.
-->

Example:
1. Download `<S3 key>` to `{worktree}/tmp/sample.pdf`.
2. Run `_cc_hearing_date_from_pdf` on it directly (write a 5-line probe to `{worktree}/tmp/verify.py`).
3. If it returns the expected date, the hypothesis is wrong — root-cause from the observed symptoms instead. Inspect `is_plausible_hearing_date` next.
4. If it returns NULL, the hypothesis is confirmed — proceed with the prescribed fix.

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
