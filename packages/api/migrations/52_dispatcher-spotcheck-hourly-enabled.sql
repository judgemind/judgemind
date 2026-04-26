-- Up Migration
--
-- Dispatcher v2 — re-enable spotcheck scheduled skill at hourly cadence (issue #3459).
--
-- Reverses migration 51 (#3450 / PR #3457), which disabled the row while
-- the operator decided how to fix the agent-runner IAM gap. Issue #3459
-- chose Option A — widen the agent-runner ECS task role to include the
-- subset of perms /spotcheck needs (ecs:RunTask + iam:PassRole on the
-- oneshot family, s3:ListBucket + s3:GetBucketLocation on the document-
-- archive bucket). With those grants in place the cron-spawned spotcheck
-- agent can launch its sampling oneshot at Step 0 and proceed through
-- bidirectional review.
--
-- Cadence change: '0 14 * * *' (daily) → '0 * * * *' (hourly). Operator
-- directive (2026-04-26): run hourly for the time being so the
-- dispatcher's primary problem-discovery loop fires every hour rather
-- than once a day. The skill's own state-awareness (gh issue list +
-- "comment on existing rather than file duplicate") prevents the higher
-- cadence from spamming GitHub with duplicate findings.
--
-- last_triggered_at = NULL forces the supervisor's cron walk to fire on
-- the next tick after merge — without this, last_triggered_at would
-- still hold the timestamp from the last pre-disable invocation and the
-- skill would not re-fire until exactly one hour past that stale value.
--
-- Design notes
-- ------------
-- * WHERE name = 'spotcheck' — idempotent guard. Re-applying after a
--   Down rollback re-enables the row (the Down sets enabled=false and
--   trigger_value back to '0 14 * * *'); a third Up application is a
--   no-op aside from re-clearing last_triggered_at, which is harmless
--   (worst case: one extra fire on the next tick).
--
-- * notes is appended, not replaced — preserves the migration-51 disable
--   annotation as audit trail of the route this row took.
--
-- Rollback
-- --------
-- The Down restores the migration-51 state: enabled=false at the
-- original daily trigger. It does NOT re-append the migration-51 notes
-- text (the Up appended a new annotation; rolling forward + rolling
-- back cleanly is a non-goal for an UPDATE-only migration). Operators
-- rolling back should expect the row to fall back to disabled-daily.

UPDATE dispatcher.scheduled_skills
SET
    enabled           = true,
    trigger_value     = '0 * * * *',
    last_triggered_at = NULL,
    notes             = notes
                        || ' Re-enabled hourly by #3459 — Option A IAM widening'
                        || ' on agent-runner role lets /spotcheck launch its'
                        || ' sampling oneshot at Step 0. Skill rewrite adds'
                        || ' state-awareness (gh issue list before file) so'
                        || ' the hourly cadence does not duplicate findings.'
WHERE name = 'spotcheck';


-- Down Migration
UPDATE dispatcher.scheduled_skills
SET
    enabled           = false,
    trigger_value     = '0 14 * * *',
    last_triggered_at = NULL
WHERE name = 'spotcheck';
