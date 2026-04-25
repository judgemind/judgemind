# Dispatcher phase state machine — audit pass

This document records how the phase-transition catalog in
`scripts/dispatcher/phase_transitions.py` would have surfaced — or
did surface — each of the listed bugs as a **missing or incorrect
transition branch** rather than a quietly inconsistent daemon state.

The key property of the catalog: every `transition_from_*` function
has a corresponding parameterized test class in
`scripts/dispatcher/tests/test_phase_transitions.py`. A missing
transition branch shows up as a failing test *before* deployment,
not as a production incident discovered via CloudWatch.

---

## #2925 — daemon-restart retry leaves `kind='task'` rows stuck in `retrying`

Fixed by PR #2944.

When the daemon restarted mid-pipeline it wrote
`status='retrying', phase='daemon_restart_abandoned'` but never
terminated the old row before spawning the replacement. The partial
unique index on `(issue_number)` blocked the new claim indefinitely.

Under the refactored dispatch path, `daemon_restart_abandoned` is a
**terminal phase** recorded in `TERMINAL_PHASES`. The supervisor
tick's `_list_advanceable_agents` SELECT excludes terminal phases, so
a row in `phase='daemon_restart_abandoned'` is never picked up for
further advancement. The boot-recovery path that writes the terminal
must also stamp `ended_at` — that invariant is enforced by the
`transition_from_*` contract (every `ADVANCE_WITH_STATUS` or
`UNRECOGNIZED` result is paired with a `_mark_agent_terminal` call
that sets `ended_at`). A missing `ended_at` on a terminal row would
surface as a failing `TestTerminalPhaseProperties` parameterized test
in `test_phase_contracts.py`.

---

## #2913 — `failure_summary` not cleared on `crashed → succeeded` recovery

Fixed by PR #2928.

When an agent recovered from `crashed` to `succeeded` via the
tier-1 retry path, `dispatcher.agents.failure_summary` was not
cleared, causing the ✓ success glyph to show the old crash message
as its tooltip.

This is a **side-effect omission**, not a phase-transition bug. The
pure transition catalog enforces shape (next phase, terminal status),
not column-level side effects. The fix belongs in
`_mark_agent_terminal` — whenever `status='succeeded'`, write
`failure_summary=NULL`. The refactored `match transition.action`
dispatch makes the correct-outcome terminal paths explicit:
`ADVANCE_WITH_STATUS` with `terminal_status='succeeded'` is the
single code path that must clear `failure_summary`. Under the old
scattered `_mark_agent_terminal(status="succeeded", ...)` call sites
it was easy to miss one; under the new dispatch the path is
consolidated to a single branch per transition function.

---

## #2902 — `git_push_failed` did not write a `dispatcher.failures` row

Fixed by PR #2943.

When `git push` failed during `push_and_pr`, the daemon marked the
agent `status='failed'` but did not call `_write_failure`, so
`_build_failure_summary` produced only `"push_and_pr failed"` with
no category or stderr detail.

Under the refactored dispatch path, `_push_and_open_pr` now calls
`transition_from_push_and_pr(output)` for its phase DECISION. The
git-push failure path is a non-verdict failure (the push subprocess
returns a nonzero exit before any phase-output JSON is produced), so
it routes through `_handle_agent_failure` directly — the same pattern
as every other subprocess failure in the daemon. A parameterized test
in `test_phase_transitions.py` exercises the
`transition_from_push_and_pr({"rebase_failed": True})` path (added
by #3225), keeping the conflict-routing branch tested. The pre-#2902
gap (no `_write_failure` call) would be caught today by the
`TestPushAndPrRouting` integration test that asserts a
`dispatcher.failures` row exists after a push failure.

---

## #2971 — ralph commits directly; daemon amends with summary message

Fixed by PR #2977.

The pre-#2971 model had ralph reset its uncommitted diff, then the
daemon ran `git add -A && git commit -m <msg>`. An incomplete reset
(observed 2026-04-21) trapped the diff in ralph's throwaway commit,
producing `exit_code=1, stderr_tail=""` ("nothing to commit") — a
`FAILURE_CATEGORY_GIT_COMMIT_FAILED` row with no actionable detail.

Under the refactored dispatch path, `_push_and_open_pr` delegates
the phase DECISION to `transition_from_push_and_pr`. The git-commit
failure is a side-effect path (not a phase-transition branch), but
the migration makes the `ADVANCE_WITH_STATUS` → `PHASE_NO_OP` and
`ADVANCE` → `PHASE_AWAITING_CI` branches explicit and tested. Any
future change to the commit/amend logic that accidentally skips the
`_handle_agent_failure` call on nonzero exit would be caught by the
`TestPushAndPrGitCommitFailed` integration test.

---

## #2961 — backfill-migration row-class coverage checklist

Fixed by PR #3261.

Backfill scripts that ran `UPDATE dispatcher.agents SET status=X`
without updating every affected column (e.g. `phase`, `ended_at`,
`failure_summary`) produced rows in inconsistent states that the
admin cockpit rendered incorrectly.

Under the refactored dispatch path, every terminal write goes through
`_mark_agent_terminal(status=transition.terminal_status, phase=transition.next_phase, ...)`,
which is a single function with documented column responsibilities.
A backfill script that uses `transition_from_*` to derive the target
`status` and `phase` values is automatically aligned with the
daemon's own decision logic. The `## Adding a new phase` section in
`phase_transitions.py` explicitly lists column responsibilities as
step 2 of the wiring procedure, so future phase additions carry a
built-in backfill checklist.
