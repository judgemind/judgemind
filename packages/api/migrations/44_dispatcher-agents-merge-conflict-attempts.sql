-- Up Migration
--
-- Dispatcher v2 — budget bookkeeping for the ``fix_conflict`` phase
-- (issue #3225). Adds one column to ``dispatcher.agents`` so the
-- agent-runner entrypoint can bound the "claude resolves a pre-push
-- rebase conflict against origin/main" recovery loop to
-- ``FIX_CONFLICT_MAX_ATTEMPTS`` (default 2) per agent lifetime.
--
-- Why a dedicated column (not ``retries_used`` or ``output_json``)
-- ---------------------------------------------------------------
-- * ``retries_used`` already counts fix-CI mechanical retries
--   (``FIX_CI_MAX_RETRIES``). Overloading it with merge-conflict
--   counts would conflate two independent budgets — one is a CI-side
--   fix loop on a pushed PR, the other is a pre-push git-rebase
--   recovery loop that never reaches CI — and would silently shrink
--   the available fix-CI budget for any agent that also hit a
--   rebase conflict.
-- * ``merge_unstick_attempts`` (migration 43) tracks a different
--   recovery: post-merge stale-rollup auto-unstick attempts. Same
--   "agent-lifetime bounded retry" shape, different phase.
-- * ``output_json`` is a per-phase-output JSONB on a different
--   table (``dispatcher.phase_outputs``). Storing agent-lifetime
--   bookkeeping there would split a single counter across many
--   rows and complicate the "at most FIX_CONFLICT_MAX_ATTEMPTS
--   attempts per agent" predicate.
--
-- Behaviour
-- ---------
-- * DEFAULT 0 — every existing row (including in-flight agents mid-
--   pipeline) is backfilled to 0, i.e. "no fix_conflict invocation
--   yet". Matches the issue's per-agent-lifetime retry budget: each
--   invocation of ``handle_fix_conflict`` increments the column
--   BEFORE invoking claude; when the current value is already
--   >= FIX_CONFLICT_MAX_ATTEMPTS the handler skips to the
--   ``conflict_unresolvable`` terminal without spending claude
--   compute.
-- * NOT NULL — the handler always reads an integer, never a NULL.
-- * No index — this column is only read/written during the
--   fix_conflict-phase handler (one agent at a time, already
--   filtered by agent_id). Adding an index would be pure overhead.
--
-- Rollback
-- --------
-- Dropping the column is safe. The entrypoint tolerates its absence
-- at runtime: ``read_merge_conflict_attempts`` degrades to returning
-- 0 (so the budget never trips), and ``handle_fix_conflict``
-- continues to emit its structured output via the skill. The worst-
-- case behaviour on rollback is an unbounded retry loop — which the
-- entrypoint's ``MAX_PHASE_ITERATIONS`` cap (40) still contains. This
-- mirrors the merge_unstick_attempts / migration 43 rollback policy.

ALTER TABLE dispatcher.agents
    ADD COLUMN IF NOT EXISTS merge_conflict_attempts INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN dispatcher.agents.merge_conflict_attempts IS
    'Count of fix_conflict-phase claude-resolution attempts for this '
    'agent (#3225). Bounded at FIX_CONFLICT_MAX_ATTEMPTS (default 2) '
    'per agent lifetime: each invocation of handle_fix_conflict '
    'increments this column before spawning the claude skill; when '
    'the value is already >= budget the handler advances to the '
    'conflict_unresolvable terminal without spending compute. Covers '
    'both the pre-push rebase-conflict path (push_and_pr) and the '
    'start-of-ralph baseline rebase-conflict path (secondary '
    'mitigation). See .claude/skills/task-v2-fix-conflict/SKILL.md.';


-- Down Migration
ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS merge_conflict_attempts;
