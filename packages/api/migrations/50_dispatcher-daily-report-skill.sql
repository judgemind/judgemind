-- Up Migration
--
-- Dispatcher v2 — daily-report scheduled skill (issue #3375).
--
-- Registers the /dispatcher-daily-report skill as a row in
-- dispatcher.scheduled_skills so the daemon's _scheduled_skills_tick
-- fires it daily at 12:00 UTC. The skill runs five SQL aggregations
-- over the last-24h window, renders a phone-readable markdown report,
-- and auto-PRs it to docs/dispatcher-daily/YYYY-MM-DD.md.
--
-- Design notes
-- ------------
-- * name='dispatcher-daily-report' matches the skill dir name
--   (.claude/skills/dispatcher-daily-report/) and the slash-command
--   (/dispatcher-daily-report) per the entrypoint contract:
--   phase=<name> -> claude -p <skill_invocation>.
--
-- * trigger_kind='cron', trigger_value='0 12 * * *' — fires once a
--   day at noon UTC. Operators can temporarily override the trigger_value
--   to '*/5 * * * *' in the DB for one-cycle verification, then restore.
--
-- * ON CONFLICT (name) DO NOTHING — idempotent; re-applying this
--   migration after rollback does not duplicate the row or overwrite
--   operator overrides already in place.
--
-- Rollback
-- --------
-- DELETE FROM dispatcher.scheduled_skills WHERE name='dispatcher-daily-report'.
-- The skill dir and daily_report.py remain (no code is auto-deleted).

INSERT INTO dispatcher.scheduled_skills (
    name, skill_invocation, trigger_kind, trigger_value, enabled, notes
)
VALUES (
    'dispatcher-daily-report',
    '/dispatcher-daily-report',
    'cron',
    '0 12 * * *',
    true,
    'Daily SQL-based failure summary report (issue #3375). Fires at '
    || '12:00 UTC; skill generates docs/dispatcher-daily/YYYY-MM-DD.md '
    || 'and auto-PRs it. CloudWatch metrics (tick_elapsed_s, '
    || 'scheduler_tick_stalled) are placeholders — follow-up in #3375.'
)
ON CONFLICT (name) DO NOTHING;


-- Down Migration
DELETE FROM dispatcher.scheduled_skills WHERE name = 'dispatcher-daily-report';
