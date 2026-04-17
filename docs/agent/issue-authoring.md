# Issue Authoring

How to file issues that agents can act on correctly — acceptance criteria, sub-tasks, and investigation follow-ups. CLAUDE.md contains a short summary; this doc has the detail.

## Writing Acceptance Criteria

Acceptance criteria must be concrete and machine-checkable wherever possible. Vague criteria like "page looks correct" allow agents to hand-wave past verification. Include specific verification commands and expected results.

### Guidelines

- **Data changes**: include the SQL query and expected result.
- **Frontend changes**: include the URL and what should be visible (element, text, layout).
- **API changes**: include the endpoint, request, and expected response shape.
- **Behavior changes**: include the specific trigger and expected outcome.
- **External-integration changes** (issues proposing to query a third-party website or API — court case-search endpoints, public records APIs, etc.): include a one-line HTTP feasibility note confirming the endpoint is actually usable before labeling the issue `agent/ready`. Example: `Feasibility: curl https://example.court.gov/api/search?case=123 returns JSON, no reCAPTCHA/WAF, anonymous access works`. At minimum, verify: (1) the endpoint responds to an anonymous request, (2) there is no reCAPTCHA / Cloudflare challenge / login wall on the query path, (3) the expected data is actually returned for a realistic sample. Issues without a feasibility note risk premising acceptance criteria on integrations that cannot work — see #1979 for a case where ~a day of agent time was spent on an infeasible premise.
- **Data cleanup tasks on `derived.*` tables**: default the plan to `rebuild_db.py --county <name>` rather than a surgical one-off delete/patch script. Surgical scripts tend to ship with their own bugs and only patch existing rows — they do not validate the ingestion/enrichment pipeline, so the same root cause can keep affecting inbound data. Only write a surgical script if (1) rebuild cost is prohibitive at the affected scale, or (2) the deletion is scoped to a subset rebuild can't express. Include a one-line justification in the issue body if going surgical.

### Example — vague (bad)

```
- [ ] Zavala v Becker shows only its ruling text
```

### Example — machine-checkable (good)

```
- [ ] Zavala v Becker shows only its ruling text
  Verify: `SELECT length(ruling_text) FROM rulings WHERE case_id = 'f51849ca-...'` returns values < 5000
  Verify: Screenshot of /cases/f51849ca-... shows single-case content
```

Each criterion should have at least one `Verify:` line that an agent can execute to confirm the criterion is met. This applies to issues filed by both humans and agents. If a verification command is not possible (e.g., requires subjective judgment), note that explicitly so reviewers know it requires manual verification.

## Creating Sub-Tasks

If a task naturally breaks into 2+ independent pieces of work, create child issues:

- Reference the parent: `Parent: #42` in the issue body.
- Sub-tasks should be self-contained — another agent should be able to pick one up independently.
- Label child issues appropriately and add `agent/ready` if fully specified.

## Investigation Tasks

Investigation tasks produce documentation, not code:

- Write findings in the issue body or `docs/investigations/`.
- **Always file follow-up issues** for every actionable finding. Label them `agent/ready` if fully specified. Reference the investigation issue as the parent.
- After documenting findings and filing follow-ups, close the investigation issue unless human judgment is genuinely needed.
