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

### Verify lines on Python tasks

Do NOT write `python -c '<code>'` in `Verify:` lines. The preflight-bash hook blocks that exact form (it matches the `python -c` pattern in `docs/agent/interactive-shell-rules.md`), so any agent attempting to run the verification step will hit an immediate hook rejection and be forced into an unplanned detour to write a temp script anyway.

Use one of these three approved forms instead, in preference order:

1. **Run an existing CLI or module entry point** — when a runnable entry point exists:
   ```
   Verify: python path/to/existing_module.py --some-flag
   ```
2. **Reference an existing pytest test by name** — when the behaviour is already covered by a unit or integration test:
   ```
   Verify: pytest path/to/tests/test_foo.py::test_bar
   ```
3. **Write a one-shot probe to `{worktree}/tmp/verify.py`** — for cases where no CLI or relevant test exists and a short probe is genuinely the clearest expression:
   ```
   Verify: write probe to {worktree}/tmp/verify.py and run python {worktree}/tmp/verify.py
   ```
   The `{worktree}/tmp/` directory is the approved location for agent temp files (not `/tmp/`).

### Verify lines on SQL queries

If a `Verify:` line is a SQL query against a Judgemind schema (`derived.*`, `dispatcher.*`, `staging.*`, `telemetry.*`, `public.*`), the column names MUST match `packages/api/src/data-access/schema.sql`. Run the query against `scripts/dev-db-query.sh` (or grep `schema.sql` for the table) before filing the issue to confirm it parses — regardless of result count. A `Verify:` line that errors out on column-not-found is worse than no `Verify:` line at all, because it makes an agent picking up the issue debug whether the implementation is wrong or the AC is wrong before they can begin.

```
# Cheap pre-file check — does this query parse?
scripts/dev-db-query.sh "SELECT name, trigger_kind, trigger_value, enabled FROM dispatcher.scheduled_skills LIMIT 1"

# Or grep schema.sql to confirm the columns exist:
grep -n "scheduled_skills" packages/api/src/data-access/schema.sql
```

Concrete failure mode this rule prevents: #4309's first AC `Verify:` line read `SELECT name, schedule, enabled, last_triggered_at FROM dispatcher.scheduled_skills WHERE name = 'audit-llm-carry-forward'`. The `schedule` column does not exist on `dispatcher.scheduled_skills` — the actual columns are `trigger_kind` + `trigger_value` (migration 48). The agent picking up #4309 had to debug whether the implementation or the AC was at fault before progressing. A thirty-second pre-file check against `schema.sql` would have caught the mismatch immediately. See #4319.

**Automated lint (#4358):** `scripts/check-issue-verify-sql.py --issue <N>` (or `--body-file <path>` for a draft) parses `schema.sql` and validates the SQL columns referenced in every `Verify:` line of the issue body. Exit 0 = clean; exit 1 = at least one column-not-found mismatch (with a per-mismatch diagnostic naming `<schema.table.col>`); exit 2 = parse / `gh` error. The `.github/workflows/issue-verify-sql.yml` workflow runs the same check on `issues: [opened, edited]` and strips `agent/ready` if it fails, so authors get the signal at issue-creation time.

### Don't prescribe `_`-prefixed test filenames under `scripts/tests/`

When a `Verify:` line names a new shell-test file under `scripts/tests/`, do NOT prescribe a basename starting with an underscore (e.g. `scripts/tests/_test-helpers-test.sh`). The shell-test runner `scripts/run-scripts-tests.sh` classifies any `_*.sh` file as a sourceable helper (the `is_helper` function at lines ~100-111) and silently skips execution — naming a test that way would leave it un-discoverable by CI.

Use one of these two canonical naming patterns instead:

1. **`scripts/tests/test_<thing>.sh`** — the default. Works for any test that does not specifically exercise a `_<helper>.sh` shared helper.
2. **`scripts/tests/test__<thing>.sh`** (double-underscore) — when the test is specifically the test for a shared `_<thing>.sh` helper. The double-underscore disambiguates the test from the helper without tripping the runner's `is_helper` filter.

Concrete example surfaced via #4540: an AC originally specified `scripts/tests/_test-helpers-test.sh` as the test filename for the shared `_test-helpers.sh` library. That filename would have matched the runner's `is_helper` filter and never executed — the implementer renamed to `test__test_helpers.sh` to keep the AC's intent (a runnable test) intact. See #4545 and the load-bearing filter at `scripts/run-scripts-tests.sh::is_helper`.

### Cleanup AC queries — anchor on document status

When writing an AC that counts/filters `derived.rulings`, `derived.cases`, `derived.judges`, `derived.attorneys`, `derived.parties`, or any other row whose source-of-truth is a `derived.documents` row, always join `derived.documents` and filter `d.status = 'active'`. Supersede chains (post-#3722 multimodal-fix and similar reingest events) routinely leave rows with the OLD case_number / OLD case_title / null judge_id behind under `status = 'superseded'`, and a naive count picks up that noise as if it were live regression. The cleanup AC then looks unmet long after the fix has actually landed and re-extracted everything.

Required shape — join + status filter on every cleanup-driving SELECT:

```sql
SELECT COUNT(*)
FROM derived.rulings r
JOIN derived.cases c ON c.id = r.case_id
JOIN derived.documents d ON d.id = r.document_id  -- ← required
WHERE d.status = 'active'                          -- ← required
  AND c.case_number LIKE 'UNKNOWN-%';
```

Without the `derived.documents` join + `d.status = 'active'` filter, supersede children inflate the count and the cleanup AC reads as failing even after the fix has shipped. Concrete example surfaced via #3629 verification (2026-05-06): an AC#2 query against Orange County department `N15` returned `10/12 (83.3%) UNKNOWN` until the active-status filter was added — then `0/2 (0%)`. All 10 UNKNOWN rows were `status = 'superseded'` from the post-#3722 supersede chain and not visible in any user-facing surface. See #4236 for the full incident.

This applies to any AC that gates "the cleanup is done" on a row count — file/issue authors filing such ACs and agents implementing them should both verify the SELECT carries the `derived.documents` join + `d.status = 'active'` filter before treating the count as authoritative.

When the verification reads the bidirectional spotcheck JSON in `s3://judgemind-document-archive-dev/spotcheck/<TS>.json`, use `scripts/spotcheck/inspect.py <s3-uri-or-local-path> [--unknown-only] [--null-judge-only] [--department-in C20 C23 …] [--format table|json]` to filter and summarise the result without writing custom one-liners (#4235).

## Verify the gap exists before filing

Before filing a "does X" / "enable X" / "add X" issue, run a single command that verifies X is not already the case. This rule exists because an agent-runner cycle spent re-creating state that already exists is pure waste — the canonical example is #3146, where an issue was filed to "enable Container Insights on the ECS cluster" after the Terraform attribute `enable_container_insights = true` had already shipped in a prior PR. The agent spent a full cycle discovering, through probing, that there was nothing to do. A thirty-second check before filing would have surfaced that immediately. This is a sibling of the external-integration feasibility note in §Writing Acceptance Criteria (line 15): both rules say "verify the precondition before you ask an agent to build on top of it."

**Probe patterns by verb:**

- **"Enable AWS setting Y"** → check the live state and the Terraform source before filing.
  ```
  # Live state (example: ECS Container Insights)
  aws ecs describe-clusters --clusters <cluster-name> --include SETTINGS \
    --query 'clusters[0].settings'
  # Terraform source
  grep -r "enable_container_insights\|container_insights" infra/terraform/
  ```
  Already present if: live state shows `"value": "enabled"` OR Terraform already sets the attribute to `true`.

- **"Add metric / alarm for X"** → check whether the metric namespace is already populated and the alarm already exists.
  ```
  aws cloudwatch list-metrics --namespace <namespace> --metric-name <MetricName>
  aws cloudwatch describe-alarms --alarm-name-prefix <prefix>
  ```
  Already present if: `list-metrics` returns a non-empty `Metrics` array, or `describe-alarms` returns an existing alarm with the expected name prefix.

- **"Add documentation for X"** → grep the docs tree before filing.
  ```
  grep -r "X" docs/
  # or for agent-skill docs:
  grep -r "X" .claude/skills/ CLAUDE.md
  ```
  Already present if: the concept is already explained at the relevant level of detail in an existing doc.

- **"Fix code bug X" / "X throws an error"** → grep for the function name or error-message string; the bug may already be fixed in a prior PR the filer didn't see.
  ```
  grep -r "error_message_or_function_name" packages/
  # Also check recent git log for the relevant area:
  git log --oneline --all -- packages/<area>/
  ```
  Already fixed if: the offending code path no longer exists, or a recent commit message references the fix.

- **`fix(test)` — failing test reproduction** → for any "the test currently fails" issue, re-run the failing test (or grep) against current `origin/main` — **not** your worktree branch — before filing. Worktree branches lag behind `main` the moment they're cut, so a baseline measurement taken from one is stale by definition.
  ```
  git fetch origin main
  git switch --detach origin/main
  ./scripts/tests/<failing-test>.sh 2>&1 | grep "FAIL"   # whatever the verify line is
  # …or for pytest:
  pytest packages/<pkg>/tests/<test_path> -k <failing_test> -x
  ```
  Already fixed if: the verify command returns clean (exit 0, no FAIL output) — a load-bearing PR merged after your baseline was captured. The fix in #4173 (macOS jq-1.6 empty-file salvage-prelude) had merged 7.5 hours before #4178 was filed on a stale baseline; running the verify line at file time would have surfaced that immediately. Cite the merging PR in the close comment.

**Decision after probing:** If the gap is genuinely absent (the feature/setting/doc does not yet exist), file the issue normally. If the gap is already satisfied, either close the draft, pivot scope to a doc-only update that confirms the current state (e.g., "document that Container Insights is enabled and cite the Terraform attribute"), or — for `fix(test)` issues whose ACs are exhausted by "the test passes" — skip filing and post the verification-only evidence elsewhere (a comment on the parent issue, or no action at all). If the probe is ambiguous, treat as real and file normally — agents can do their own probe at pickup time per Step 4b in `.claude/skills/task/SKILL.md`.

## CloudWatch metric filters: probe `@message` shape before specifying a pattern

When filing an issue that proposes a new `aws_cloudwatch_log_metric_filter`, **always probe the actual `@message` shape of the target log group via Logs Insights before writing the `pattern` value into the issue body.** A pattern that looks correct against synthetic JSON-shaped events can produce a metric filter that NEVER FIRES against real log lines, and the failure mode is silent — the metric just sits at zero forever, indistinguishable from "the warning hasn't fired yet."

The trap is specific to JSON patterns proposed against stdlib-`logging` emitters:

- `logger.warning("event.name", extra={"key": "value"})` using stdlib `logging.Logger` does **NOT** serialize `extra` into the rendered message body. CloudWatch only sees the raw message string `event.name` — the keys in `extra` (including `telemetry_event`, `document_id`, etc.) are present on the `LogRecord` object but never reach the log stream unless a JSON formatter is wired up.
- A JSON pattern like `{ $.telemetry_event = "case_title_unrecoverable" }` matches against JSON-shaped log events. With stdlib emitters, no such JSON exists in CloudWatch — so the pattern never fires.
- structlog (or any code path that calls `json.dumps()` and passes the result to the log message body) DOES emit JSON-shaped events. JSON patterns are correct against structlog/json-formatter emitters and wrong against stdlib emitters.

This is a sneaky bug class because the failure is invisible at every layer except live CloudWatch logs:

- Code review of the Terraform PR cannot catch it — the HCL is syntactically valid and the pattern looks plausible.
- `terraform validate` does not catch it.
- `aws logs test-metric-filter` against synthetic JSON events incorrectly reports the pattern works — the trap only surfaces when the pattern is tested against ACTUAL log lines from the target log group.

### Required probe before filing

Run a Logs Insights query against the target log group via `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` (or `aws logs start-query`) to see what CloudWatch actually receives:

```
filter @message like /<keyword from the log line>/
| limit 5
| sort @timestamp desc
```

For example, before filing an issue to add a metric filter for `case_title.unrecoverable`:

```
log_group_names: ["/ecs/judgemind-ingestion-worker-dev"]
query_string: filter @message like /case_title.unrecoverable/ | limit 5 | sort @timestamp desc
```

Inspect the returned `@message` field on each row:

- **Plain string** like `case_title.unrecoverable` (with no surrounding `{...}` JSON envelope) → the emitter is stdlib `logging`. **Use a literal-substring pattern** in the metric filter: `pattern = "\"case_title.unrecoverable\""`. The double-quotes around the substring are the CloudWatch metric-filter-syntax convention for substring matching, not JSON.
- **JSON envelope** like `{"event": "case_title.unrecoverable", "telemetry_event": "...", ...}` → the emitter is structlog or another JSON formatter. **JSON patterns are appropriate**: `pattern = "{ $.telemetry_event = \"case_title_unrecoverable\" }"`.
- **Mixed shape** (some events JSON, some plain string) — the source has multiple emitters or a partial migration in progress. Pick the shape that covers all events you want to count, or escalate the inconsistency as its own issue.

### What to put in the issue body

Once the probe has confirmed the message shape, include the probe result in the issue body alongside the proposed pattern, so the implementing agent does not have to re-derive it:

```
## Probe result

Logs Insights query against /ecs/judgemind-ingestion-worker-dev:
  filter @message like /case_title.unrecoverable/ | limit 5

Returned @message values are plain strings (no JSON envelope), confirming
worker.py:1161 uses stdlib logging. Pattern must be literal-substring,
NOT JSON.

## Proposed pattern

  pattern = "\"case_title.unrecoverable\""
```

For issues filed without a probe, an agent picking up the issue must run the probe themselves before writing the Terraform — same shape, same cost (one Logs Insights call, ~30s). Document the probe result in the PR body so reviewers can confirm.

### Worked example — #4076

The issue body for #4076 ("CloudWatch metric filter for case_title.unrecoverable") proposed a JSON pattern:

```hcl
pattern = "{ $.telemetry_event = \"case_title_unrecoverable\" }"
```

The agent picking up the issue ran a §4c hypothesis-verification probe via Logs Insights against `/ecs/judgemind-ingestion-worker-dev` and found 413 hits in a recent 168k-record window — all with plain-string `@message = "case_title.unrecoverable"`, no JSON envelope. The proposed JSON pattern would have produced a metric filter that never fires; the agent switched to `pattern = "\"case_title.unrecoverable\""` (literal-substring match) before merging. See PR #4493.

If the issue body had included the probe result up front, the agent could have skipped the hypothesis-verification step and gone straight to the correct pattern.

## Hypothesis vs. evidence

Bug-fix issues frequently mix three different things into a single body: what was *observed* (symptoms — the data the filer can actually see), what is *suspected* (root cause — the filer's best current theory), and what should *change* (prescribed fix — the code/config edit the filer expects). When all three are run together as "here's the bug, here's the fix," an agent that picks the issue up will mentally lock in on the prescribed fix path before doing any verification — and if the suspected root cause is wrong, the entire ralph cycle is wasted.

This is the same shape as the "Instrument before you guess" principle in `docs/agent/investigation-patterns.md` — both rules say: *don't anchor on an unconfirmed hypothesis*. Issue authoring is the upstream mirror of agent-side investigation. The filer's hypothesis can be wrong for the same reasons an agent's first hypothesis can be wrong: the visible layer that produces the symptom is rarely the same layer where the bug lives, and trace-select-before-validating-fix (inner branches can be dead code if a downstream filter rejects the row) applies to issue authoring too.

### The framing distinction

When filing a bug-fix or investigation-style issue, separate the three pieces explicitly:

- **Observed symptoms** — facts the filer measured. NULL rate is X%, this query returns Y, this PDF has `hearing_date=NULL`, the page renders the wrong number. Symptoms are reproducible and don't require trust in any theory of mechanism.
- **Suspected root cause** — the filer's best current theory of *why* the symptoms appear. Frame this as a hypothesis the agent should verify, NOT as a known-true premise. "I suspect `_cc_hearing_date_from_pdf`'s regex doesn't match the dept-38 format" is a hypothesis; "extend `_HEARING_DATE_PROBATE_RE` to handle the dept-38 format" is a prescribed fix that assumes the hypothesis is correct.
- **Hypothesis verification steps** — the cheap probes that confirm or falsify the suspected root cause before any code is written. Download a sample PDF, run the function on it, query the DB, capture the actual return value. These probes typically take 5 minutes and either ratify the prescribed fix or redirect the agent to the real broken layer.

### Worked example — #4251

The pattern in #4251 (the issue that motivated this section): the filer observed that several Orange County department-38 PDFs produced `hearing_date=NULL` and wrote the issue body around a single suspected root cause — `_cc_hearing_date_from_pdf`'s regex doesn't match the dept-38 format — with a prescribed fix to extend `_HEARING_DATE_PROBATE_RE`.

What investigation actually found:

1. The regex *did* match the dept-38 format correctly — the function returned the expected hearing date.
2. The downstream `is_plausible_hearing_date` filter rejected the correctly-extracted date because dept-38 master probate calendars publish 30+ days ahead, outside the civil ±14-day window the filter enforces.
3. An agent who blindly followed the prescribed fix would have extended the regex (no-op), found the symptom didn't move, and either hit the iteration cap or — worse — merged a regex change that did nothing because the layer below still rejects the date.

The right framing would have been:

```
## Observed symptoms

- N PDFs in Orange County department 38 produce `hearing_date=NULL` after parse.
- Sample input: <S3 key>. Expected output: `hearing_date='2026-06-15'`. Actual: `NULL`.

## Suspected root cause (hypothesis)

`_cc_hearing_date_from_pdf`'s regex (`_HEARING_DATE_PROBATE_RE`) doesn't match the dept-38 PDF format.

## Hypothesis verification steps

Before writing code:
1. Download `<S3 key>` to {worktree}/tmp/sample.pdf.
2. Run `_cc_hearing_date_from_pdf` on it directly (write a 5-line probe to {worktree}/tmp/verify.py).
3. If it returns the expected date, the hypothesis is wrong — root-cause from the observed symptoms instead. The next layer to inspect is `is_plausible_hearing_date`.
4. If it returns NULL, the hypothesis is confirmed — proceed with the prescribed fix.
```

The verification-steps section is the thing that converts a wrong-hypothesis trap into at most a 5-minute redirect.

### When this framing applies

Use the explicit Observed / Suspected / Verification structure when:

- The issue is a bug-fix or investigation-style task (`type/bug`, NLP/scraper extraction failures, data-quality regressions, "X returns wrong value").
- The filer is naming a specific function, regex, or layer they believe is broken.
- The fix path is more than a one-line edit, OR the failing layer is downstream of other layers that could be the actual root cause.

It is less critical for:

- Pure feature work where there is no existing wrong behaviour to root-cause (the "gap" framing in §Verify the gap exists before filing already covers this).
- Trivial typo / copy fixes where the symptom and the broken layer are visibly the same line.

When in doubt, including the framing is cheap and saves ralph cycles when the hypothesis turns out to be wrong.

### Verify stored state matches the symptom

Bug-fix issues that mention "raw X stored in Y" / "skipped step Z" / "the data in <table/bucket> is wrong" need one extra cheap probe before filing: **query the storage tier the symptom claims is corrupt and confirm the stored bytes actually look the way the hypothesis predicts.** This is the storage-side mirror of the §"Hypothesis vs. evidence" framing — the hypothesis is about *the state at rest*, so verify the state at rest, not just the log line that talked about it.

The trap this catches: a log line like `ruling_text starts with '<html' — raw HTML detected, transcription step was likely skipped` describes what some downstream code *saw* during a transient pipeline run, not what is actually stored in S3 / `derived.*`. The "skipped transcription" hypothesis assumes the log line is reporting the persistent state, but in fact it can be reporting the function's own normalization buffer or a fallback path the function constructed in memory. A 30-second SQL query against the storage tier disambiguates.

**The probe is keyed to the schema tiers from CLAUDE.md (S3 source of truth → `derived.*` → `staging.*`).** Pick the tier the hypothesis points at:

- **"Raw X is in S3"** — fetch the relevant key with `aws s3 cp s3://<bucket>/<key> -` (or via the boto3 helper used by the scraper) and inspect the bytes. If the issue says "S3 stores HTML when it should store plain text," confirm the S3 bytes actually start with `<html` before filing.
- **"Field Y in `derived.*` is corrupt"** — run a `scripts/dev-db-query.sh` SELECT against the rows the issue identifies. If the issue says `ruling_text` is HTML, query `SELECT LEFT(ruling_text, 50) FROM derived.rulings WHERE ...` for a sample of the affected rows. If the stored value is clean plain text, the symptom is in a *transient* state somewhere upstream of the read path — the persistent storage is fine, and the bug is in whatever produced the misleading log line.
- **"Transient pipeline row in `staging.*` shows X"** — `staging.*` rows are short-lived, so run the probe within the same window as the symptom or capture the row before it drains. The same `dev-db-query.sh` pattern applies.

**Worked example — #4382 (the issue that motivated this section):** the filer observed reingest log lines stating `ruling_text starts with '<html' — raw HTML detected, transcription step was likely skipped` for 126 LA dept-25 rulings, and wrote the issue body around the suspected root cause "the transcription step was skipped when these documents were originally captured." A 30-second probe against `derived.rulings` for the affected rows would have falsified the hypothesis immediately:

```
scripts/dev-db-query.sh "SELECT id, LEFT(ruling_text, 50) AS head \
  FROM derived.rulings \
  WHERE court_id = 'ca-los-angeles' AND department = '25' AND judge_id IS NULL \
  LIMIT 5;"
```

If `head` is clean plain text (not `<html...`), then `ruling_text` was stored correctly and transcription was *not* skipped at capture time. The validation failure surfaces only inside the reingest function's *transient* state — the symptom is in `scripts/reingest_from_s3.py`'s in-memory normalization buffer, not in the storage tier the issue body pointed at. The actual root cause turned out to be a missing alias registration in `scripts/reingest_from_s3.py` that caused the function to fall back to constructing an HTML buffer for these specific rows.

The right framing — combining this storage-tier probe with the Observed / Suspected / Verification structure from §"Hypothesis vs. evidence" — would have been:

```
## Observed symptoms

- 126 LA dept-25 rulings have `judge_id = NULL` and reingest logs report
  `ruling_text starts with '<html' — raw HTML detected`.
- Sample affected ids: ec17ae67-..., 94045693-..., c457225f-...

## Suspected root cause (hypothesis)

The transcription step was skipped at capture time, leaving raw HTML in
`ruling_text` for these rows.

## Hypothesis verification steps

Before writing code:
1. Query `derived.rulings.ruling_text` for the affected rows:
   `SELECT LEFT(ruling_text, 50) FROM derived.rulings WHERE ... LIMIT 5;`
2. If the stored value is clean plain text, the hypothesis is FALSIFIED —
   the storage tier is fine, and the bug lives in whatever code path
   produced the `<html` log line (likely a transient normalization
   buffer in `scripts/reingest_from_s3.py`).
3. If the stored value starts with `<html`, the hypothesis is confirmed —
   investigate why transcription was skipped at capture time.
```

The storage-tier probe is the thing that distinguishes "the data at rest is wrong" from "a function's transient state is wrong" — two failure modes that produce identical-looking log lines but require completely different fixes. File the probe in the issue body as a `## Hypothesis verification steps` block; the agent's `/task` §4c step already runs explicit verification steps verbatim before writing code, so the cost shifts from agent (5 minutes per ralph cycle on a wrong-hypothesis path) to filer (30 seconds with a SQL client open).

### Cross-references

- `docs/agent/investigation-patterns.md` §"Instrument before you guess" — the agent-side mirror. When the filer's hypothesis is missing or known-thin, the agent ships an instrumentation-only PR first to make the next failure self-diagnosing.
- `.claude/skills/task/SKILL.md` §4c — the agent-side complement to this section: when picking up a bug-fix issue, the agent runs the issue's hypothesis-verification probe **before** writing implementation code. If the issue body lists explicit verification steps, run those; otherwise run a generic 5-minute probe (download the smallest reproducer, run the function the issue claims is broken, compare to the expected value).

## Priority Framework

Assign priority by urgency and workflow impact, not by user-visibility.

| Priority | Principle | Categories |
|---|---|---|
| **p0** | Human-only; agents set only when explicitly told | Production outages, data loss |
| **p1** | Time-sensitive or workflow accelerators | Scraper failures, data quality bugs, DX improvements, process fixes |
| **p2** | Everything else | User-facing bugs, backfills, refactoring, docs, data prevention |
| **p3** | Large slower units of work | Missing features, UI/UX redesign |

**Rationale:** Scraper data is ephemeral (lost forever if not captured). DX issues directly affect agent throughput — a broken workflow wastes entire agent sessions. Process improvements prevent repeated failures. These justify p1 even though they are not user-facing. Large feature work and redesigns are important but not urgent — p3.

Default DX to p1, not p3. Default features to p3, not p2. Reserve p0 for human-assigned true emergencies.

## Creating Sub-Tasks

If a task naturally breaks into 2+ independent pieces of work, create child issues:

- Reference the parent: `Parent: #42` in the issue body.
- Sub-tasks should be self-contained — another agent should be able to pick one up independently.
- Label child issues appropriately and add `agent/ready` if fully specified.

## Refactor sub-tasks: scope test updates with structural changes

When a refactor sub-task changes the *shape* of a file (extracting a block into a function, collapsing duplicate arms, renaming a section), check whether existing tests pin the current shape via line-anchored extraction (`awk`/`sed`/`grep` with literal patterns), structural assertions ("the body is N lines long"), or whitespace-sensitive matching. If they do, those tests are part of the structural change — they cannot stay as-is once the structure moves.

A "no test-file edits" AC on a sub-task whose structural change *is* the thing those tests are pinning to creates an unresolvable tension. The implementing agent has three bad choices: (a) violate the structural-change AC, (b) violate the no-edit AC, or (c) introduce duplicate-code transitional measures (the new function lives next to an unchanged inline copy that the tests still extract). Option (c) is the transitional dead-code antipattern called out in agent-memory `feedback_no_known_broken_paths.md` — known-broken code is a failure waiting to happen, not a hedge. The "regression test with fix" pattern in reverse: tests that lock structure should be in scope for the same PR that changes the structure.

**See #4138 (sub-task B of #4097) for the concrete example.** T51's awk `^        fix_conflict)$` pattern in `test_agent_runner_entrypoint.sh` extracts the inline `fix_conflict` arm body, but the PR was supposed to reduce that body to a single function call. The "no test-file edits" AC forced the agent to keep the body inline as a transitional copy alongside a defined-but-unused `handle_fix_conflict_arm` — known-broken transitional shape that sub-task C had to resolve later.

**Checklist when filing a refactor sub-task:**

- **Grep for tests that depend on the structure being changed.** Common patterns:
  ```
  grep -rE 'awk.*\$ENTRYPOINT|grep.*\$ENTRYPOINT' scripts/tests/ scripts/dispatcher/tests/
  grep -rE 'awk[[:space:]]+(/.+/|"\^[[:space:]]+[a-z_]+\)\$")' tests/ scripts/tests/
  ```
  Adjust the patterns for the file under refactor — anything that extracts blocks by literal-line anchor, counts lines in a region, or asserts on byte-identical output is a candidate.
- **List the affected tests in the issue body under "Tests that pin the current structure."** Name each test by file + line and quote the extraction pattern that will break.
- **Then make a scope decision and state it explicitly in the AC**, choosing exactly one of:
  - **(a) Update those tests in the same PR.** Add an AC line: "Tests `<file>:<test-name>` are updated to match the new structure (see `<new-pattern>`)." This is the default — the tests pin the structure being changed, so they belong to the same change.
  - **(b) Flag the contradiction explicitly and accept it.** Add an AC line: "Test `<file>:<test-name>` will fail until sub-task <C>; that's expected. Sub-task <C> updates the test pattern to `<new-pattern>`." This requires sub-task <C> to actually exist and be linked. Don't write this if the follow-up sub-task is hypothetical.
- **Avoid the strict "no test-file edits" AC unless the affected tests genuinely don't touch the structure being changed.** When the tests are structure-agnostic (they invoke the script and assert on logs / DB rows / exit codes only), "no test-file edits" is fine and protects against scope creep. When the tests are structure-anchored, "no test-file edits" is the bug.

## Backfill Migrations: Row-Class Coverage

Why: Issues #2961 and #2960 revealed that a backfill migration silently skipped entire row classes — specifically, `failed` and `plan_blocked` rows were left un-updated because the SQL filter only matched the modal (`crashed`) case. The checklist below makes row-class coverage an explicit authoring and implementation requirement so the same oversight cannot recur. See #2961 (checklist formalized) and #2960 (prior incident).

When filing or implementing an issue that includes a backfill migration (any PR touching `packages/api/migrations/*.sql` with an `UPDATE`, `DELETE`, or `INSERT` that writes against existing rows), follow this checklist:

- **Enumerate every row class in the issue body.** If the affected table has a status/state/type column, list all distinct values that the migration must handle — not just the most common one. Present them as a table or bulleted list with the expected row count for each class (even if that count is 0).
- **Cross-reference the SQL filter against each row class.** For each `(status, failure_summary, …)` tuple in your row-class table, verify the SQL `WHERE` clause matches or explicitly excludes that row. Distinct status values (`failed` / `plan_blocked` / `crashed`) require distinct `OR` branches or a generalized filter — a single-value filter is almost always a bug.
- **The acceptance criteria must include a post-backfill invariant `SELECT`.** State the exact query and its expected result (typically `0` rows matching the pre-migration condition). Example:
  ```
  Verify: SELECT COUNT(*) FROM staging.captures WHERE … returns 0
  ```
- **For large tables, include a before/after row-count breakdown by status.** Confirm the counts add up correctly — total updated should equal the sum of per-class counts.

## Migrations

When filing or implementing an issue whose migration drops `NOT NULL` on a column that backs a GraphQL field:

- **Flip the GraphQL field to nullable in the same PR.** Change `FieldName: Type!` to `FieldName: Type` in the schema file. Leaving it non-null causes the GraphQL serializer to throw at runtime when NULL rows appear post-migration, crashing the entire query.
- **The `graphql-nullability-drift-check` CI job enforces this** for columns listed in `KNOWN_MAPPINGS` inside `scripts/check-graphql-nullability-drift.py`. If your column is not yet covered, extend `KNOWN_MAPPINGS` in the same PR (see `docs/agent/code-standards.md` §Nullable schema migrations and #3441).

## Symmetric guards: file the production-side and test-side guard together

Any time you file an issue to add a CI hygiene guard `check-X.sh` that enforces a rule on production code under `packages/.../src/`, ask whether the rule has a symmetric analog on tests of that production code under `packages/.../tests/`. If production code must do X, tests of that production code almost always need a structural counterpart — typically a shared helper or fixture pattern that ensures the property X is preserved end-to-end. The two guards land naturally as a pair, and filing both at the same time prevents weeks of drift between them.

The canonical example is the reingest-safety pair: #4141 added `scripts/check-parse-document-reingest-safety.sh`, which enforces docstring markers on Live-only `parse_document` implementations. The matching test-side guard #4190 — `scripts/check-tests-use-reingest-helper.sh`, which forbids inline reingest-shape `CapturedDocument(...)` in `tests/courts/` and requires the shared `make_reingest_cap_doc` helper — landed weeks later, only after three regression tests had to be migrated one-by-one (#4153, #4133, #4165). Each migration was a separate issue precisely because there was no test-side guard to catch the structural drift the moment a new test introduced it.

When filing a hygiene-check issue:

- **Identify the test-side analog explicitly.** State in the issue body whether one exists and what it would enforce — even if the answer is "none, because the rule is purely about production code and has no test-side shape."
- **File both halves at the same time.** Either as one issue (when the work is small enough to land together) or as a pair of linked issues with `Parent: #<root>` and a cross-reference in each body. Do not assume the test-side guard will follow naturally — it usually doesn't.
- **Reference the symmetric pair in both bodies.** Future readers should be able to see the loop closed without having to re-derive it from the audit history.

## Investigation Tasks

Investigation tasks produce documentation, not code:

- Write findings in the issue body or `docs/investigations/`.
- **Always file follow-up issues** for every actionable finding. Label them `agent/ready` if fully specified. Reference the investigation issue as the parent.
- After documenting findings and filing follow-ups, close the investigation issue unless human judgment is genuinely needed.
