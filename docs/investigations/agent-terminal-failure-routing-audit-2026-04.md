# Agent-terminal failure-routing audit — 2026-04

**Issue:** #3062 (investigation, p3).
**Prompted by:** #3054 retro — `ralph_not_ship` branch called
`_mark_agent_terminal` directly without writing a `dispatcher.failures`
row, bypassing the #3032 unified failure-routing path.
**Daemon file audited:** `scripts/dispatcher/daemon.py` (version_sha at
start of audit: `0a88d79`, post-#3060).

## 1. Architecture recap

Issue #3032 unified the dispatcher's agent-terminal failure path so
every genuine failure writes a `dispatcher.failures` row AND marks the
agent terminal, via `_handle_agent_failure`. The supervisor tick's
`_find_diagnoser_candidates` scan picks up rows whose category is in
`TIER_2_FIRST_OCCURRENCE_CATEGORIES |
TIER_2_RECURRENCE_CATEGORIES | TIER_3_CATEGORIES` and spawns the Opus
diagnoser against each, which returns a structured recommendation
(`retry` / `retry_with_hint` / `reissue` / `escalate` / `close` /
`block_and_comment` / `file_prerequisite_task` /
`block_on_existing_task`) that the daemon's deterministic consumer
executes.

Two sets are intentionally exempt from diagnoser routing:

- `_INFRA_PREEMPTION_CATEGORIES = {daemon_restart_abandoned,
  paused_by_killswitch}` — operator-initiated or daemon-restart
  preemptions; the retry they trigger is "free" and does not burn
  attempt budget (#2936).
- `AUTO_RETRY_CATEGORIES = {stuck_timeout, gh_rate_exhausted,
  subprocess_crash, daemon_restart_abandoned}` — tier-1 mechanical
  retry. First occurrence enqueues a retry marker; recurrences promote
  to tier-2 and reach the diagnoser via the recurrence window check in
  `_has_prior_same_category_failure`.

## 2. Call-site classification

The daemon has 40 `self._mark_agent_terminal(...)` call sites (grep
`self._mark_agent_terminal scripts/dispatcher/daemon.py`). Not all are
failure-terminals — the helper is also used for correct-outcome
terminals (`succeeded`, `needs_review`, `plan_blocked`) and
non-terminal phase transitions (`status='running' phase='awaiting_ci'`).
The audit covers every site, classifying as:

- **OK (unified)** — routes through `_handle_agent_failure` (writes
  failure row + marks terminal + emits
  `daemon.agent_failure_routed`).
- **OK (inline routed)** — writes `_write_failure` with a diagnoser-
  eligible category then calls `_mark_agent_terminal` directly.
  Functionally equivalent to `_handle_agent_failure`; pattern
  pre-dates the helper (ac-infeasible paths preserve their
  `detected_by` markers + nested details shape).
- **OK (subprocess routed)** — routes through
  `_handle_subprocess_failure` (sibling of `_handle_agent_failure`
  for per-phase subprocess failures).
- **OK (mechanical retry)** — writes failure row with a tier-1
  category, creates a retry marker; diagnoser picks up on recurrence
  only.
- **Intentionally unrouted (infra-preemption)** —
  `daemon_restart_abandoned` / `paused_by_killswitch` /
  `force_stopped`. No failure row; no diagnoser pass.
- **Intentionally unrouted (correct outcome)** — `succeeded`,
  `needs_review`, `plan_blocked`. Not a failure path.
- **Intentionally unrouted (non-terminal)** — `status='running'`
  phase transition only; DB row stays alive for the next supervisor
  tick.
- **Intentionally unrouted (diagnoser-consumer close-out)** — the
  five `_consume_action_*` methods that execute a diagnoser
  recommendation. The failure row that triggered this diagnoser pass
  already exists; routing again would infinite-loop the candidate
  scan.
- **BUG** — `status='failed'` written with no failure row AND no
  intentional exemption; diagnoser never runs. Matches the
  ralph_not_ship (#3054) shape exactly.
- **Gap** — genuine failure currently routed through an invariant-
  violation / defensive-catch path; could benefit from a dedicated
  category + diagnoser route, but not as urgent as a BUG.

### 2.1 Classification table

Line numbers below are current as of this audit's PR. The compound
terminal category column indicates the failure category written to
`dispatcher.failures` when the site routes, or `—` when no row is
written.

| line | function | terminal (status / phase) | category written | routed via | verdict |
|---|---|---|---|---|---|
| 3979 | `_recover_abandoned_agents` | crashed / daemon_restart_abandoned | daemon_restart_abandoned | `_write_failure` inline + retry marker | OK (infra-preemption + mechanical retry) |
| 7362 | `_claim_and_orchestrate_one` | failed / claim | — | direct | Intentionally unrouted (host-level infra gap; note in #3062) |
| 7510 | `_check_killswitch_and_abort` | failed / paused_by_killswitch or force_stopped | — | direct | Intentionally unrouted (infra-preemption) |
| 7676 | `_resume_retrying_agent` | failed / claiming | — | direct | Intentionally unrouted (host-level infra gap; note in #3062) |
| 8718 | `_run_plan_phase` (issue_fetch_failed) | failed / planning | — | direct | Intentionally unrouted (gh-subprocess issue — rare, handled by rate-limit guard; note in #3062) |
| 8850 | `_run_plan_phase` (plan_go_false) | succeeded or plan_blocked / planning | — | direct | Intentionally unrouted (correct outcome) |
| 9040 | `_run_ralph_phase` (ac_infeasible) | failed / ralph | ralph_ac_infeasible | `_write_failure` inline | OK (inline routed, tier 3) |
| 9300 | `_run_summary_phase` (ac_infeasible) | failed / summary | summary_ac_infeasible | `_write_failure` inline | OK (inline routed, tier 3) |
| 9430 | `_run_subprocess_or_fail` (claude_not_on_path) | failed / `<phase>` | subprocess_crash | `_write_failure` inline | OK (mechanical retry — first occurrence not diagnosed deliberately; see note in code) |
| 9458 | `_run_subprocess_or_fail` (unhandled exception, pragma no cover) | failed / `<phase>` | — | direct | Intentionally unrouted (defensive catch) |
| 9607 | `_handle_agent_failure` | failed / `<phase>` | `<caller-supplied>` | (this IS the helper) | OK (unified) |
| 9694 | `_handle_subprocess_failure` | failed / `<phase>` | classifier-derived | (sibling helper) | OK (subprocess routed) |
| 9875 | `_push_and_open_pr` (noop_ship) | succeeded / noop | — | direct | Intentionally unrouted (correct outcome) |
| **in `_push_and_open_pr`** | **summary_output_incomplete** | **failed / push_and_pr** | **phase_output_missing** | **`_handle_agent_failure` (NEW — #3062 fix)** | **FIXED (was BUG)** |
| 10005 | `_push_and_open_pr` (git_commit exception) | failed / push_and_pr | — | direct | Gap (ralph SHIPped; follow-up #3067) |
| 10029 | `_push_and_open_pr` (git_commit non-zero) | failed / push_and_pr | — | direct | Gap (same as above; #3067) |
| 10325 | `_push_and_open_pr` (needs_review) | needs_review / needs_review | — | direct | Intentionally unrouted (correct outcome) |
| 10340 | `_push_and_open_pr` (awaiting_ci hand-off) | running / awaiting_ci | — | direct | Intentionally unrouted (non-terminal) |
| 10591 | `_advance_running_agents` supervisor loop catch-all | crashed / `<phase>` | — | direct | Intentionally unrouted (original failure already routed upstream) |
| 10637 | `_advance_awaiting_ci` (missing_pr, pragma no cover) | failed / awaiting_ci | — | direct | Intentionally unrouted (invariant violation / pragma no cover) |
| 10995 | `_run_fix_ci` (ci_red_after_retries) | failed / awaiting_ci | ci_red_after_retries | `_write_failure` inline | OK (inline routed, tier 3) |
| 11106 | `_run_fix_ci` (fix_ci BLOCKED) | failed / awaiting_ci | — | direct | **BUG** — exact ralph_not_ship analog; follow-up #3068 proposes `FAILURE_CATEGORY_FIX_CI_BLOCKED` tier-3 |
| 11213,11236,11251,11285,11303,11335,11348,11363 | `_apply_fix_ci_patch` (8 sites — missing_commit_message, git add/commit/push exceptions + non-zero exits) | failed / awaiting_ci | — | direct | Gap — cluster; follow-up #3069 proposes `FAILURE_CATEGORY_FIX_CI_APPLY_FAILED` tier-2 |
| 11431 | `_advance_awaiting_deploy` (missing_pr, pragma no cover) | failed / awaiting_deploy | — | direct | Intentionally unrouted (invariant violation / pragma no cover) |
| 11490 | `_advance_awaiting_deploy` (deploy_failed) | failed / awaiting_deploy | — | direct | Gap — diagnoser-actionable; follow-up #3070 proposes `FAILURE_CATEGORY_DEPLOY_FAILED` tier-2 |
| 11887 | `_advance_verify` (verify FAILED post-merge) | failed / done | — | direct | Gap — regression signal; follow-up #3071 proposes `FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE` tier-3 |
| 13128 | `_check_stuck_agents` (stuck_timeout sweep) | crashed / `<phase>` | stuck_timeout | `_write_failure` inline + retry marker | OK (mechanical retry) |
| 13439 | `_create_retry_marker` (retry_exhausted) | failed / retry_exhausted | — | direct | OK (inline routed) — prior row exists from the retries that just exhausted; diagnoser picks up that row on recurrence |
| 13760 | `_process_retry_markers` (infra-preemption reclaim) | failed / `<reason>` | — | direct | Intentionally unrouted (infra-preemption — `reason` in `_INFRA_PREEMPTION_CATEGORIES`, ``retry_counted`` branch guarantees it) |
| 15722,15742,15772,15830,15913 | `_consume_action_escalate` / `_close` / `_block_and_comment` / `_file_prerequisite_task` / `_block_on_existing_task` | failed / `diagnoser_*` | — | direct | Intentionally unrouted (diagnoser-consumer close-out — upstream failure row already exists) |

### 2.2 Summary counts

- Total `_mark_agent_terminal` call sites: 40.
- Of those, failure-terminal (`status='failed'` / `status='crashed'`):
  34.
- Correctly routed (unified, inline, subprocess, or mechanical-retry):
  15.
- Intentionally unrouted with reason documented inline after this audit:
  14.
- Bugs fixed in this audit PR: 1 (`summary_output_incomplete`).
- Gaps filed as follow-ups: 5 (see §3).

## 3. Follow-up issues filed

Filed as sub-tasks of #3062:

1. **#3067 (p3) — `git_commit_failed` pre-push route.** Covers the two
   commit-exception / commit-nonzero sites in `_push_and_open_pr` that
   fire post-ralph-SHIP but pre-push. Ralph's diff is in the worktree;
   a diagnoser `retry` or `escalate` is useful here.
2. **#3068 (p3) — `FAILURE_CATEGORY_FIX_CI_BLOCKED` tier-3 route.**
   Exact analog of #3054's ralph_not_ship — fix-ci returned BLOCKED
   with a `block_reason` and no mechanical retry helps.
3. **#3069 (p3) — `FAILURE_CATEGORY_FIX_CI_APPLY_FAILED` tier-2 cluster
   route.** Eight sites inside `_apply_fix_ci_patch` (missing commit
   message + git add/commit/push exceptions + non-zero exits) share
   the same shape. Same refactor strategy as #3032 applied to push.
4. **#3070 (p3) — `FAILURE_CATEGORY_DEPLOY_FAILED` tier-2 route.**
   Post-merge deploy workflow failures are diagnoser-actionable
   (`file_prerequisite_task` / `block_and_comment`).
5. **#3071 (p3) — `FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE` tier-3
   route.** A post-merge verify FAILED verdict is a genuine regression
   signal — file a priority/p1 regression issue, block further work.
6. **#3072 (dx, p3) — Lint rule for ROUTING comments.** Adds a
   `scripts/check-terminal-routing-comments.sh` that fails CI if a
   new `_mark_agent_terminal(status='failed')` site lacks the
   required ROUTING comment.

Each follow-up reuses the audit's template: add the category constant,
put it in the appropriate tier set, wire the call site to
`_handle_agent_failure`, add unit tests mirroring
`test_daemon_ralph_not_ship.py`.

## 4. In-PR fix

The most "obviously-ralph_not_ship-class" site — the summary phase
produced an output envelope missing required fields — is fixed in the
same PR as this audit. Reuses `FAILURE_CATEGORY_PHASE_OUTPUT_MISSING`
(tier-2 first-occurrence) because it's qualitatively identical to the
existing `phase_output_missing` case: subprocess exited 0 but the JSON
is unusable. Covered by `test_daemon_summary_output_incomplete.py`
(6 tests, all passing). The remaining gaps / bugs are filed as
follow-ups above so each can land in its own narrow PR.

## 5. Documentation

Every `_mark_agent_terminal` call site in `daemon.py` now has an inline
`ROUTING (#3062)` comment documenting its routing status. This creates
a grep-able audit trail — future `#3054`-class audits can use
`grep 'ROUTING (#3062)' scripts/dispatcher/daemon.py` to enumerate
every decision, and any new call site added without such a comment is
immediately visible in code review.

## 6. Follow-up-of-follow-up: prevent recurrence

Future `_mark_agent_terminal(status='failed' | 'crashed')` call sites
should default to `_handle_agent_failure` unless an inline comment
explains why. A lint rule enforcing "every call site has a
`ROUTING (#3062)` comment within 5 lines above it" is tracked in #3072
(filed as a dx follow-up) — it would have caught the #3054 bug at
author time.
