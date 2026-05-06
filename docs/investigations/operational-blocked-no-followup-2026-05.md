# Investigation — operational `verdict=blocked` does not produce a follow-up tracker (#4248)

Date: 2026-05-06
Investigator: `/task #4248` (agent-a5c7deecf91f286e0)
Source incident: #3954 — sat in `agent/ready` for ~4 days after the dispatcher v2 operational phase posted a thorough BLOCKED verification-evidence comment on 2026-05-02.

## TL;DR

The hand-off `operational-skill emits verdict=blocked → daemon files a code-fix tracker` does not exist in the code. Two independent layers each terminate the autonomous flow before the diagnoser can intervene:

1. **`transition_from_operational` (intentionally) maps `verdict=blocked` to `ADVANCE_WITH_STATUS / needs_review`, NOT `ROUTE_TO_DIAGNOSER`.** Only `verdict=failed` (or missing/unrecognized) routes to the diagnoser. This is asserted by an existing regression test (`test_blocked_advances_to_operational_failed_with_needs_review`).
2. **Even if the routing did go through `_handle_agent_failure`, `FAILURE_CATEGORY_OPERATIONAL_FAILED` ("operational_failed") is NOT a member of any diagnoser tier set** (`TIER_2_FIRST_OCCURRENCE_CATEGORIES`, `TIER_2_RECURRENCE_CATEGORIES`, `TIER_3_CATEGORIES`). The diagnoser's candidate-selection SQL filters on those tier sets, so a `dispatcher.failures` row with category `operational_failed` is never selected.

The third hand-off the issue body hypothesized — "the agent ran an older skill that emitted `failed` instead of `blocked`" — is also disproved: the agent emitted exactly `verdict=blocked`, which is the documented contract of the SKILL ("`blocked` ... advance to `operational_failed` with `status=needs_review`").

The architectural gap is captured in the SKILL itself (lines 207, `task-v2-operational/SKILL.md`):

> If the task requires a code change, emit `verdict=blocked` with `block_reason` explaining the gap so an operator can file a coding task.

That sentence shifts the burden of "file a coding tracker" onto a human. When the operator is not present (autonomous flow), the issue parks in `agent/ready + needs_review` indefinitely, and the next /task agent re-runs the same diagnosis from scratch on its next claim attempt.

## Timeline (verified evidence)

### Original failed run on #3954

`dispatcher.agents` row (queried 2026-05-06):

```
agent_id          : cb6135d1-99cb-4de0-b485-77f6e9999157
issue_number      : 3954
kind              : task
phase             : operational_failed
status            : needs_review
exit_code         : null
started_at        : 2026-05-02 10:53:42.188579 UTC
ended_at          : 2026-05-02 11:09:36.430871 UTC
current_milestone : null
pr_number         : null
failure_summary   : null
outcome_summary   : null
```

`dispatcher.failures` rows for that agent: **0**.

So the path actually taken was:

1. `_run_operational_phase` invoked the skill.
2. The skill emitted `verdict=blocked` with a clear `block_reason` (`cluster.court` is empty in CourtListener API responses → backfill cannot rebucket any documents).
3. `transition_from_operational({"verdict": "blocked", ...})` returned `ADVANCE_WITH_STATUS, needs_review, operational_failed`.
4. `_run_operational_phase` (daemon.py:12330) took the `advance_with_status` arm and called `_mark_agent_terminal(status="needs_review", phase="operational_failed", ...)`.
5. `_mark_agent_terminal` removed `status/in-progress` (claim interlock teardown) but did NOT add `status/needs-human` and did NOT touch `agent/ready`.
6. `_handle_agent_failure` was never called → no `dispatcher.failures` row written → no diagnoser candidate, no diagnoser ECS spawn, no follow-up tracker.
7. The issue stayed at `agent/ready + needs_review (active)`. The daemon's `_issue_already_attempted` gate (SQL function `dispatcher.issue_has_active_agent`, migration 57) classifies `status='needs_review'` as active and **prevented re-claim** for 4 days.
8. `/task #3954` (recovery, agent-a253d24e5a808ee76) was manually invoked by the operator on 2026-05-06 (the issue body said 2026-05-04 but the actual claim comment was 2026-05-06 17:41:40 UTC).
9. Recovery agent re-investigated the same root cause, filed #4247 (the code-fix tracker), and wired up the block.
10. PR #4250 closed #4247 on 2026-05-06 18:22:14, the unblock workflow restored `agent/ready` on #3954, and #3954 is now eligible for re-claim once the upstream `COURTLISTENER_API_TOKEN` is restored (#1626).

The 4-day gap between the original BLOCKED comment (2026-05-02 11:09) and the manual recovery (2026-05-06 17:41) is the cost of the missing autonomous hand-off. The original SKILL run had already produced all the evidence needed to file #4247 — it just had no path to do so.

## Root-cause chain (verified)

### Layer 1 — `transition_from_operational` (file:line cited)

`scripts/dispatcher/phase_transitions.py:689-699`:

```python
if verdict == "BLOCKED":
    return PhaseTransition(
        action=TransitionAction.ADVANCE_WITH_STATUS,
        next_phase=PHASE_OPERATIONAL_FAILED,
        terminal_status=AgentStatus.NEEDS_REVIEW.value,
        reason="operational blocked — needs operator review",
        context={
            "block_reason": (output or {}).get("block_reason"),
            "evidence_md": (output or {}).get("evidence_md"),
        },
    )
# failed, missing, or unrecognized — route to diagnoser.
return PhaseTransition(
    action=TransitionAction.ROUTE_TO_DIAGNOSER,
    failure_hint=FAILURE_HINT_OPERATIONAL_FAILED,
    ...
)
```

`verdict=blocked` and `verdict=failed` take **different** branches. Only the `failed` branch reaches the diagnoser.

Existing regression test that locks this behavior in: `scripts/dispatcher/tests/test_phase_transitions.py:1407` (`test_blocked_advances_to_operational_failed_with_needs_review`).

### Layer 2 — `FAILURE_CATEGORY_OPERATIONAL_FAILED` is not in any tier set

`scripts/dispatcher/daemon.py:1523`:

```python
FAILURE_CATEGORY_OPERATIONAL_FAILED = "operational_failed"
```

`scripts/dispatcher/daemon.py:1969-2057` defines the three tier sets. **`operational_failed` appears in NONE of them.**

`scripts/dispatcher/daemon.py:20957-20977` (`_find_diagnoser_candidates`):

```python
all_trigger_categories = list(
    TIER_2_RECURRENCE_CATEGORIES
    | TIER_2_FIRST_OCCURRENCE_CATEGORIES
    | TIER_3_CATEGORIES
)
... cur.execute(
    "... WHERE d.failure_id IS NULL "
    "  AND f.agent_id IS NOT NULL "
    "  AND f.category = ANY(%s) "
    "  ...", (all_trigger_categories, ...))
```

So even if `_run_operational_phase` *did* call `_handle_agent_failure(category=FAILURE_CATEGORY_OPERATIONAL_FAILED, ...)` on a `verdict=blocked` (which it does NOT today — it only does so for the `route_to_diagnoser` arm at daemon.py:12338), the resulting failure row would be invisible to the supervisor's diagnoser sweep. `_handle_agent_failure`'s docstring at line 13209-13210 even says:

> Categories in TIER_2_FIRST_OCCURRENCE_CATEGORIES, TIER_2_RECURRENCE_CATEGORIES, or TIER_3_CATEGORIES will be diagnosed; **others fall back to the existing mechanical-retry policy** (e.g. `daemon_restart_abandoned` stays on the infra-preemption path).

There is no mechanical-retry policy for the operational phase. The category is a dead-letter — the row gets written and is never consumed.

### Layer 3 — SKILL contract pushes the burden onto the operator

`.claude/skills/task-v2-operational/SKILL.md:207`:

> If the task requires a code change, emit `verdict=blocked` with `block_reason` explaining the gap so an operator can file a coding task.

This works when an operator is watching, fails silently when they aren't.

## Why the issue body's hypotheses didn't match

The issue body proposed three possible root causes:

1. *"The diagnoser wasn't invoked (the route_to_diagnoser branch was added later than 2026-05-02)."* — Branch existed since #3513 (PR for #3507, the original `/task-v2-operational` SKILL). What was missing was that `verdict=blocked` does not take that branch in the first place. The branch is correctly wired but only reachable via `verdict=failed`.
2. *"The diagnoser was invoked but failed to file the follow-up."* — Disproved by direct DB observation: zero `dispatcher.failures` rows for the agent → diagnoser was never invoked. The diagnoser SKILL itself (`/diagnose-failure`) was never spawned.
3. *"The agent ran an older `task-v2-operational` skill that emitted `failed` rather than `blocked`."* — Disproved by the BLOCKED comment text on #3954 (`status: BLOCKED`) and the agent's terminal phase `operational_failed` + status `needs_review` (the literal output of the `verdict=blocked` arm). If the skill had emitted `failed`, the daemon would have written a `dispatcher.failures` row (which it didn't) and routed to the diagnoser.

## Recommendations

The simplest correct fix is to **make `transition_from_operational` route `verdict=blocked` to the diagnoser** when `block_reason` describes a code-fix-able gap, and let the diagnoser apply Action 2 (file follow-up tracker + block-on) per the existing `/diagnose-failure` SKILL.

There are several shapes this could take. Each should be evaluated against the failure modes the others avoid:

### Option A — Route all `verdict=blocked` to the diagnoser (preferred)

Change `transition_from_operational` to return `ROUTE_TO_DIAGNOSER` for `verdict=blocked`. Remove the special `ADVANCE_WITH_STATUS / needs_review` branch.

**Required companion changes:**
- Add `FAILURE_CATEGORY_OPERATIONAL_FAILED` to `TIER_2_FIRST_OCCURRENCE_CATEGORIES` (or a new tier-3 set; the diagnoser fires on first occurrence either way).
- The diagnoser SKILL (`/diagnose-failure`) already knows how to file follow-up trackers (Action 2) and add `Blocked by` lines. The block_reason text from the operational phase is exactly the input it needs.

**Trade-off:** today's `verdict=blocked` covers two distinct cases — "code-fix-able gap" AND "operator-only gap (missing secret, environment not ready)". The diagnoser handles the second case correctly today via `mark needs_review` (Action 4) when the failure pattern matches one of the four operator-only domains (PAT rotation, prod deploys, etc.). The diagnoser SKILL already has a §"Reserved for needs_review" section with the four bright lines. Operator-only blocks would route to `mark needs_review` from the diagnoser, code-fix blocks to file-follow-up — same end-state for operator-only, much better end-state for code-fix.

This is the path I recommend. It preserves the operator-only escalation pattern through the diagnoser's existing logic and unlocks autonomous follow-up filing for code-fix blocks.

### Option B — Split the operational SKILL contract

Have the SKILL emit a new verdict `blocked_code_fix` (vs `blocked_operator` or just `blocked` for operator-only). Routing then becomes a clean 1:1 mapping to ROUTE_TO_DIAGNOSER vs ADVANCE_WITH_STATUS.

**Trade-off:** requires every operational task author to correctly classify their block_reason ahead of time. Less robust than letting the diagnoser do the classification (which is its job).

### Option C — Daemon-side post-`needs_review` poll

After `_mark_agent_terminal(status="needs_review", phase="operational_failed", ...)`, additionally write a `dispatcher.failures` row with a tier-set-member category so the diagnoser picks it up.

**Trade-off:** double-bookkeeping. The `dispatcher.agents` row says `needs_review`, the `dispatcher.failures` row says "diagnose me." Confusing semantics; rejected in favor of A.

### Always: regression tests

Whichever option ships, add two regression tests:

1. **`scripts/dispatcher/tests/test_phase_transitions.py`** — assert `verdict=blocked` from operational routes the way the chosen option specifies. The existing `test_blocked_advances_to_operational_failed_with_needs_review` test will need to be updated or replaced.
2. **`scripts/dispatcher/tests/test_daemon_audit_routing_gaps.py`** (or a new sibling) — assert that an operational `verdict=blocked` that reaches `_handle_agent_failure` produces a `dispatcher.failures` row with a category that IS in one of the diagnoser tier sets, AND that `_find_diagnoser_candidates` selects that row on the next supervisor tick.

These tests are the structural defense against the current failure mode silently re-emerging if a future PR rewires the operational path.

## Source-file docstring claims to update

Per /task SKILL §B.1.5 — when an investigation invalidates docstring or comment claims, list them so the next PR fixes them in-place rather than leaving stale prose next to the code.

| File:line | Stale text | Required correction |
|---|---|---|
| `scripts/dispatcher/phase_transitions.py:668-671` | `* "blocked" — the task needs operator attention before it can proceed (e.g. missing secret, environment not ready). Advance to "operational_failed" with "status='needs_review'" so the operator can see it in the cockpit.` | Change to reflect whatever Option (A/B/C) ships. If Option A, this becomes "the task needs further intervention; route to the diagnoser, which will either file a code-fix tracker (block_reason names a fixable bug) or escalate to needs_review (operator-only domain)." |
| `scripts/dispatcher/daemon.py:12224-12229` (`_run_operational_phase` docstring) | `* "blocked" — advance to "operational_failed" with "status='needs_review'" (operator must intervene).` | Same direction — depends on which option ships. |
| `.claude/skills/task-v2-operational/SKILL.md:84-88` and `:207` | `"blocked" ... Advances to "operational_failed" with status=needs_review.` AND `"If the task requires a code change, emit verdict=blocked ... so an operator can file a coding task."` | Update to say the daemon now files the tracker via the diagnoser when `block_reason` names a code-fix gap; the SKILL author no longer needs to assume an operator is watching. |

These are not in scope for this investigation PR — they belong with the structural-fix PR that ships Option A (or B or C).

## Follow-up issues filed

- **#4272 — fix(dispatcher): route operational verdict=blocked through diagnoser for autonomous code-fix tracker filing.** Implements Option A: route `verdict=blocked` to the diagnoser, add `FAILURE_CATEGORY_OPERATIONAL_FAILED` to `TIER_2_FIRST_OCCURRENCE_CATEGORIES`, ship two regression tests (transition-layer + daemon candidate-scan layer), and apply the source-file docstring corrections listed above. Priority: p1, type/dx, area/devops — workflow accelerator with a quantifiable cost (~one full /task agent's context per re-attempt on every affected issue).
