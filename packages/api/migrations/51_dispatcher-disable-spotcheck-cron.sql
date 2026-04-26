-- Up Migration
--
-- Dispatcher v2 — disable spotcheck scheduled skill (issue #3450).
--
-- The /spotcheck SKILL.md Step 0 launches a one-shot ECS task to
-- populate an S3 path, then reads from that path. The agent-runner
-- leaf IAM role does not include ecs:RunTask or the required S3
-- grants, so every cron-spawned spotcheck agent fails at Step 0.
-- This migration disables the row until a type/decision follow-up
-- (filed by /task-v2-retro after this PR merges) chooses between:
--
--   Option A — widen the agent-runner IAM role to include ecs:RunTask
--              + S3 list/get/put (broadens the documented threat model).
--   Option B — refactor /spotcheck so the daemon launches the oneshot
--              before spawning the agent (non-trivial daemon change).
--
-- Design notes
-- ------------
-- * WHERE name = 'spotcheck' AND enabled = true — idempotent guard.
--   Re-applying this migration after a Down rollback is a no-op
--   (the row is already enabled=false, no rows match, zero updates).
--
-- * notes text is appended, not replaced — preserves any operator
--   annotations already present in the column.
--
-- Rollback
-- --------
-- UPDATE dispatcher.scheduled_skills SET enabled = true
--   WHERE name = 'spotcheck';
-- Note: the appended notes text is NOT restored by Down — it is a
-- partial rollback. The row itself is re-enabled for scheduling.

UPDATE dispatcher.scheduled_skills
SET
    enabled = false,
    notes   = notes
              || ' Disabled by #3450 — spotcheck cron is incompatible'
              || ' with the agent-runner leaf-role IAM scope;'
              || ' type/decision follow-up tracks A vs B.'
WHERE name = 'spotcheck'
  AND enabled = true;


-- Down Migration
UPDATE dispatcher.scheduled_skills
SET enabled = true
WHERE name = 'spotcheck';
