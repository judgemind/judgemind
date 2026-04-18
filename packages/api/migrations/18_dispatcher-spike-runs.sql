-- Up Migration
--
-- Dispatcher v2 Spike 0.1 — `claude -p` end-to-end on Fargate.
--
-- THROWAWAY SCHEMA. This migration exists solely to support spike 0.1
-- (issue #2683). Once the spike's findings have been recorded in
-- `docs/investigations/dispatcher-v2-spike-0.1.md` and the follow-up
-- cleanup issue lands, the schema + table should be dropped.
--
-- Schema scope intentionally narrow: dispatcher_spike.runs records one row
-- per ECS-Fargate `claude -p` invocation so the operator can query
-- exit codes, stdout/stderr tails, and notes without fishing them out of
-- CloudWatch by hand. No indexes beyond the primary key — volume is
-- <= a few dozen rows for the lifetime of the spike.
--
-- If spike 0.1 concludes with a "go" verdict, dispatcher v2 Phase 1 will
-- replace this schema with the full `dispatcher.*` state model described
-- in `docs/specs/dispatcher-v2-spec.md` §5. Do NOT add long-term
-- consumers of `dispatcher_spike.*`.

CREATE SCHEMA IF NOT EXISTS dispatcher_spike;

CREATE TABLE IF NOT EXISTS dispatcher_spike.runs (
    run_id       bigserial PRIMARY KEY,
    scenario     text NOT NULL,                       -- 'success' | 'turn_limit' | 'auth_fail' | other
    task_arn     text,                                -- ECS task ARN when available
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    exit_code    int,
    stdout_tail  text,
    stderr_tail  text,
    notes        text
);

-- Down Migration
-- DROP SCHEMA dispatcher_spike CASCADE;
