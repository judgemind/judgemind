-- Up Migration
--
-- Dispatcher v2 — /dispatcher-audit periodic operational-health skill (issue #2865).
--
-- Registers the /dispatcher-audit skill as a row in
-- dispatcher.scheduled_skills so the daemon's _scheduled_skills_tick
-- fires it every 6 hours. The skill runs three probes (convergence
-- regression, bad-outcome streak, site-health) via audit_probes.py and
-- files GitHub issues labelled source/dispatcher-audit for any findings.
--
-- Design notes
-- ------------
-- * name='dispatcher-audit' matches the skill dir name
--   (.claude/skills/dispatcher-audit/) and the slash-command
--   (/dispatcher-audit) per the entrypoint contract:
--   phase=<name> -> claude -p <skill_invocation>.
--
-- * trigger_kind='cron', trigger_value='0 */6 * * *' — fires every 6
--   hours (midnight, 06:00, 12:00, 18:00 UTC). Operators can temporarily
--   override the trigger_value to '*/5 * * * *' in the DB for one-cycle
--   verification, then restore.
--
-- * ON CONFLICT (name) DO NOTHING — idempotent; re-applying this
--   migration after rollback does not duplicate the row or overwrite
--   operator overrides already in place.
--
-- Rollback
-- --------
-- DELETE FROM dispatcher.scheduled_skills WHERE name='dispatcher-audit'.
-- The skill dir and audit_probes.py remain (no code is auto-deleted).

INSERT INTO dispatcher.scheduled_skills (
    name, skill_invocation, trigger_kind, trigger_value, enabled, notes
)
VALUES (
    'dispatcher-audit',
    '/dispatcher-audit',
    'cron',
    '0 */6 * * *',
    true,
    'Periodic operational-health probes (issue #2865). Fires every 6h; '
    || 'runs convergence-regression, bad-outcome-streak, and site-health '
    || 'probes via audit_probes.py and files GitHub issues labelled '
    || 'source/dispatcher-audit for any findings.'
)
ON CONFLICT (name) DO NOTHING;


-- Down Migration
DELETE FROM dispatcher.scheduled_skills WHERE name = 'dispatcher-audit';
