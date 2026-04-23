-- Up Migration
--
-- Dispatcher v2 — phase_outputs retry-capable schema. Fix for the
-- ``idx_dispatcher_phase_outputs_agent_phase`` unique-constraint
-- collision that silently dropped second-run phase output on the
-- 2026-04-19 restart-cascade (#2872 Bug E).
--
-- Context
-- -------
-- Before this migration, ``dispatcher.phase_outputs`` carried a unique
-- index on ``(agent_id, phase)`` — one output row per phase per agent.
-- That invariant was correct when retries always used a fresh
-- ``agent_id`` (the pre-3C model). But Phase 3C's retry path reuses the
-- original ``agent_id`` when a tier-1 mechanical retry fires, so a
-- second plan/ralph/summary run on the same agent hit the unique
-- constraint and the second run's output was silently rolled back by
-- ``_persist_phase_output``'s best-effort ``except Exception`` (#2872
-- 20:05:12 ``persist_phase_output_failed`` log).
--
-- The cascade this enabled
-- ------------------------
-- On daemon restart mid-ralph, the agent re-appeared as ``status=
-- 'running'`` to the new daemon. The supervisor's 30m
-- ``stuck_timeout`` fired on the stale ``phase_transitions.MAX(ts)``
-- from the pre-restart plan phase. ``_process_retry_markers`` reset the
-- agent to ``status='retrying'``, ``_resume_retrying_agent`` flipped
-- back to ``status='running'``, and the re-run produced a duplicate
-- ``(agent_id, 'plan')`` INSERT that the unique constraint rejected.
-- The silent rollback left no second ``phase_transitions`` row either,
-- so the stale ``MAX(ts)`` persisted and the supervisor fired
-- ``stuck_timeout`` again on the next tick — now on a 2.5-minute
-- ralph and 90-second plan, because the clock the supervisor saw had
-- never ticked forward.
--
-- The fix
-- -------
-- 1. Add ``attempt`` column (INT, NOT NULL, default 0). ``attempt=0``
--    is the initial run; retry-reset paths bump it on each retry reset
--    (``_process_retry_markers`` → retries_used+1 maps 1:1 to
--    attempt+1 for each subsequent phase run).
-- 2. Drop the old UNIQUE index on ``(agent_id, phase)``.
-- 3. Add a new UNIQUE index on ``(agent_id, phase, attempt)``. Multiple
--    rows per ``(agent_id, phase)`` are now legal as long as each
--    carries a distinct ``attempt`` number.
-- 4. Backfill existing rows to ``attempt=0`` (the default handles this
--    atomically on ALTER TABLE ADD COLUMN ... DEFAULT).
--
-- Why not just UPSERT the single-row invariant?
-- ---------------------------------------------
-- Upsert loses history. The admin page's "phase log" view + the
-- diagnoser's context bundle both want the full retry trail — "plan
-- succeeded the first time but summary failed, retry re-ran plan and
-- it still succeeded, ralph failed again" is useful operator context.
-- The append-only retry-capable schema preserves that history at a
-- negligible storage cost (housekeeping prunes at 30 days regardless).
--
-- Design notes
-- ------------
-- - ``attempt`` is derived from the agent's ``retries_used`` counter
--   at the moment the phase runs, not a database-side AUTO_INCREMENT.
--   Keeping the daemon the source of truth matches every other
--   retry-counter in the schema (``dispatcher.agents.retries_used``,
--   ``dispatcher.retry_markers.attempt``).
-- - We do not backfill attempt numbers for historical duplicate-phase
--   rows — the old unique index prevented duplicates, so there are
--   none. A post-migration admin query that groups by
--   ``(agent_id, phase)`` will still return one row per phase for
--   pre-#2872 agents.

ALTER TABLE dispatcher.phase_outputs
    ADD COLUMN attempt INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN dispatcher.phase_outputs.attempt
    IS 'Retry attempt counter — matches dispatcher.agents.retries_used at the moment this phase ran. 0 = initial run. Each retry reset (_process_retry_markers) bumps the effective attempt for the next phase run. Issue #2872.';

DROP INDEX IF EXISTS dispatcher.idx_dispatcher_phase_outputs_agent_phase;

CREATE UNIQUE INDEX idx_dispatcher_phase_outputs_agent_phase_attempt
    ON dispatcher.phase_outputs (agent_id, phase, attempt);


-- Down Migration
DROP INDEX IF EXISTS dispatcher.idx_dispatcher_phase_outputs_agent_phase_attempt;

CREATE UNIQUE INDEX idx_dispatcher_phase_outputs_agent_phase
    ON dispatcher.phase_outputs (agent_id, phase);

ALTER TABLE dispatcher.phase_outputs DROP COLUMN IF EXISTS attempt;
