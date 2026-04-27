-- Up Migration
--
-- Dispatcher v2 — backfill ended_at on zombie agent rows
-- (issue #3574).
--
-- Why
-- ---
-- Two write paths in agent-runner-entrypoint.sh set a terminal status
-- without also stamping ``ended_at``:
--
-- 1. ``agent_runner_reaped_failure`` at ~L3904–L3906 set
--    ``phase=$_term_phase, status='failed'`` then exited, skipping the
--    main loop's ``mark_ended`` call. This is the dominant path for
--    ``*_transition_unrecognized``, ``ralph_not_ship``,
--    ``awaiting_ci_failed/timeout``, ``merge_failed``, etc.
--
-- 2. ``advance_phase`` (2-arg form) set
--    ``phase='done', status='succeeded'`` (and equivalents for other
--    terminal statuses) without writing ``ended_at``. Called from
--    ``handle_scheduled_skill`` (succeeded/failed) and the merge handler.
--
-- ``daemon.py``'s ``_mark_agent_terminal`` already writes
-- ``ended_at = now()`` for all five terminal statuses — only the ECS
-- entrypoint paths were affected.
--
-- PR #3574 fixed both write sites (plus added a belt-and-suspenders
-- ``mark_ended`` call at the external-terminal-observed early-exit).
-- This migration repairs rows created before the fix.
--
-- Backfill strategy
-- -----------------
-- For each zombie row (terminal status + NULL ended_at), use the
-- MAX(ts) from ``dispatcher.phase_transitions`` as the best estimate of
-- when the agent actually ended. If no phase_transitions row exists for
-- the agent (pathological case), fall back to ``started_at`` so at
-- least ``ended_at`` is non-NULL and ``duration_s`` is defined (not
-- negative). The COALESCE and WHERE guard make the UPDATE idempotent.
--
-- Rollback
-- --------
-- Irreversible data cleanup. Restoring NULL ended_at on these rows
-- would re-introduce the monitoring blindness bug (#3574) and is never
-- desirable. The DOWN migration is intentionally a no-op.

UPDATE dispatcher.agents a
SET ended_at = COALESCE(
    (SELECT MAX(pt.ts)
       FROM dispatcher.phase_transitions pt
      WHERE pt.agent_id = a.agent_id),
    a.started_at
)
WHERE a.status IN ('failed', 'succeeded', 'crashed', 'plan_blocked', 'needs_review')
  AND a.ended_at IS NULL;


-- Down Migration
--
-- Irreversible data cleanup — restoring NULL ended_at on these rows
-- would re-introduce the monitoring blindness bug fixed by #3574.
-- This migration has no down path by design.
--
-- If you need to revert the ended_at fix for testing purposes,
-- use a targeted UPDATE with explicit agent_id values. Never
-- do a blanket NULL-restore in production.
SELECT 'DOWN: no-op — ended_at backfill is irreversible (#3574)' AS note;
