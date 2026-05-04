-- Up Migration
--
-- Issue #3574 — backfill ``ended_at`` on zombie agent rows.
--
-- Problem
-- -------
-- ``dispatcher.agents`` accumulated rows in a "zombie" state:
-- ``status`` in TERMINAL_AGENT_STATUSES (``failed``, ``crashed``,
-- ``succeeded``, ``plan_blocked``, ``needs_review``) AND
-- ``ended_at IS NULL``. The agent's ECS task ended and the daemon
-- classified it as terminal, but ``ended_at`` was never written.
--
-- This is bookkeeping invisibility:
--
-- 1. Monitoring queries that ``ORDER BY ended_at DESC`` silently skip
--    these rows. The cockpit cooldown function reads ``started_at`` and
--    works correctly, but any "what's the latest terminal for this
--    issue" query using ``ended_at`` sorts returns the older non-zombie
--    terminal as if it's current.
-- 2. The autonomous monitoring loop missed at least 5 issues in active
--    retry-loops on 2026-04-27 (``#2777, #2813, #2832, #2854, #3297``,
--    all stuck in ``ralph_baseline_transition_unrecognized`` zombies).
-- 3. By 2026-04-29 the zombie count had grown to 187 rows (35 in the
--    last 24h, 152 in the prior 7 days).
--
-- The runtime fix landed in three places:
--
-- * ``scripts/dispatcher/agent-runner-entrypoint.sh`` — ``advance_phase``
--   and ``agent_runner_reaped_failure`` now stamp ``ended_at`` when the
--   new status is terminal (#3822).
-- * ``scripts/dispatcher/daemon.py`` — the ``_launch_scheduled_skill_agent``
--   fail-path and ``_restore_succeeded_phase_done`` now stamp ``ended_at``
--   (#3574 — companion to this migration).
-- * ``scripts/dispatcher/daemon.py:_backfill_terminal_ended_at`` — a
--   per-tick safety net that bulk-stamps any row that slipped through
--   (#3822).
--
-- This migration is the one-shot cleanup of the historical accumulated
-- pile — sets ``ended_at`` on every existing zombie row immediately at
-- deploy time so monitoring queries return correct results without
-- waiting for the next housekeeping tick.
--
-- Fix
-- ---
-- Single UPDATE round-trip: for every row with terminal status and
-- ``ended_at IS NULL``, stamp ``ended_at`` from the best available
-- timestamp. The most accurate source is the latest entry in
-- ``dispatcher.phase_transitions`` for that agent (the moment the
-- terminal phase was logged); fall back to ``started_at`` for rows
-- with no transitions logged yet. ``GREATEST(..., started_at)``
-- guarantees ``ended_at >= started_at`` even if a transition row
-- somehow predates the agent row.
--
-- Idempotent — re-running this migration is a no-op once all rows have
-- ``ended_at IS NOT NULL``. The runtime backfill in
-- ``_backfill_terminal_ended_at`` and the per-write fixes ensure no
-- new zombies accrue.

UPDATE dispatcher.agents AS a
   SET ended_at = GREATEST(
                      a.started_at,
                      COALESCE(
                          (SELECT MAX(pt.ts)
                             FROM dispatcher.phase_transitions pt
                            WHERE pt.agent_id = a.agent_id),
                          a.started_at
                      )
                  )
 WHERE a.status IN ('failed', 'crashed', 'succeeded', 'plan_blocked', 'needs_review')
   AND a.ended_at IS NULL;


-- Down Migration
--
-- This migration contains no DDL — there are no columns or constraints
-- to drop. The ``ended_at`` stamp written above is NOT reversed on
-- down-migrate: any row whose terminal status was correct at the time
-- of the up-migrate now has the right ``ended_at`` value, and clearing
-- it back to NULL would re-introduce the zombie state the migration
-- exists to eliminate. There is no scenario where a down-migrate of
-- this migration would be desirable.
