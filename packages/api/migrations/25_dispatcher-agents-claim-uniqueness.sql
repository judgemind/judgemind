-- Up Migration
--
-- Dispatcher v2 — Phase 3 sub-task A. Adds a partial UNIQUE INDEX on
-- `dispatcher.agents.issue_number` scoped to active agents so the
-- atomic claim-via-INSERT path in `scripts/dispatcher/daemon.py` can
-- detect a "lost the race" outcome as a unique-constraint violation,
-- rather than pre-checking existence + inserting (which is not atomic
-- under concurrent daemons).
--
-- Issue #2783 (Parent: #2782, Phase 3 epic).
--
-- Design notes
-- ------------
-- - Partial index on `status IN ('running', 'retrying')` — those are
--   the two statuses the spec (§6, §8) marks as "agent currently
--   attached to this issue". `succeeded`, `failed`, and `crashed`
--   rows do NOT block a fresh re-claim: a failed agent may be retried
--   manually by the operator, and a succeeded agent is ancient history.
-- - Intentionally NOT adding `agent/ready` trust-check into the index —
--   the trust check is a daemon-side gate (runs before the INSERT) and
--   belongs in Python, not SQL.
-- - The existing `idx_dispatcher_agents_running` (status='running')
--   index remains — it's a scheduler read path, not a uniqueness
--   enforcer. Keeping both is cheap (few rows, narrow partial indexes).

CREATE UNIQUE INDEX idx_dispatcher_agents_active_issue
    ON dispatcher.agents (issue_number)
    WHERE status IN ('running', 'retrying');

COMMENT ON INDEX dispatcher.idx_dispatcher_agents_active_issue IS
    'Atomic-claim uniqueness: at most one active (running/retrying) agent per issue. INSERT conflicts on this index mean another daemon claimed first.';


-- Down Migration
DROP INDEX IF EXISTS dispatcher.idx_dispatcher_agents_active_issue;
