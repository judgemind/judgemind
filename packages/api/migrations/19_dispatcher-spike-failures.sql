-- Up Migration
--
-- Dispatcher v2 Spike 0.2 — hook → Postgres from inside a `claude -p`
-- subprocess on Fargate. Issue #2684.
--
-- THROWAWAY SCHEMA (continued from migration 18). This table captures one
-- row per PreToolUse hook invocation that calls `scripts/dispatcher/emit_failure.py`.
-- The spike measures the wall-clock latency between the client-side
-- `hook_fire_ts` (set by the hook when it runs) and the server-side
-- `inserted_at` (set by `now()` at commit time) to answer the question
-- "can a hook running inside a `claude -p` subprocess write to Postgres
-- synchronously, fast enough to run in a PreToolUse hook?"
--
-- Once spike 0.2 findings are recorded in
-- `docs/investigations/dispatcher-v2-spike-0.2.md` and the follow-up cleanup
-- issue lands, the rows inserted here should be deleted (TRUNCATE), but the
-- table itself should stay so we can re-run the measurement if the §9
-- direct-write design is ever revisited. The schema will be dropped together
-- when dispatcher v2 Phase 1 ships with the real `dispatcher.failures`
-- table.
--
-- If spike 0.2 concludes that direct-write is viable (the target verdict —
-- §9 as written), the Phase 1 `dispatcher.failures` table will match this
-- schema closely (add FK to `dispatcher.agents`, drop the `hook_fire_ts`
-- column since it is only useful for latency measurement).

CREATE TABLE IF NOT EXISTS dispatcher_spike.failures (
    failure_id     uuid PRIMARY KEY,
    category       text NOT NULL,          -- 'spike_test' for the spike harness; later: enum values from §7
    agent_id       text NOT NULL,          -- worktree id or synthetic harness id
    details        jsonb NOT NULL DEFAULT '{}'::jsonb,
    hook_fire_ts   timestamptz NOT NULL,   -- client-side wall clock captured by the hook before insert
    inserted_at    timestamptz NOT NULL DEFAULT now()  -- server-side `now()` at commit
);

-- Indexed for the two queries the harness runs:
--   SELECT count(*) FROM dispatcher_spike.failures WHERE category='spike_test';
--   SELECT inserted_at - hook_fire_ts AS latency ... WHERE agent_id='harness-<run>';
CREATE INDEX IF NOT EXISTS idx_dispatcher_spike_failures_category
    ON dispatcher_spike.failures(category);
CREATE INDEX IF NOT EXISTS idx_dispatcher_spike_failures_agent_id
    ON dispatcher_spike.failures(agent_id);

-- Down Migration
-- DROP TABLE IF EXISTS dispatcher_spike.failures;
