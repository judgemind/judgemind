-- Up Migration
--
-- Dispatcher v2 Spike 0.1 cleanup — drop the throwaway `dispatcher_spike`
-- schema and all of its contents (the `dispatcher_spike.runs` and
-- `dispatcher_spike.failures` tables from migrations 18 and 19).
--
-- Issue #2699. Spikes 0.1 (#2683) and 0.2 (#2684) are complete; their
-- findings are captured in `docs/investigations/dispatcher-v2-spike-*.md`.
-- Phase 1 of dispatcher v2 (epic #2725) will introduce the real
-- `dispatcher.*` schema and does not share tables with this throwaway.
--
-- CASCADE handles the `runs_run_id_seq` sequence, both tables, and the
-- `idx_dispatcher_spike_failures_*` indexes in a single statement. Safe
-- to run against environments where the schema was never applied (the
-- `IF EXISTS` clause keeps it idempotent).

DROP SCHEMA IF EXISTS dispatcher_spike CASCADE;

-- Down Migration
-- Intentionally empty: recreating the throwaway spike schema from the
-- migration chain is not supported. If Phase 1 needs to resurrect any of
-- this (it won't), restore from migrations 18 and 19 manually.
