-- Up Migration
--
-- Dispatcher v2 — /audit-llm-carry-forward periodic skill (issue #4309).
--
-- Registers the /audit-llm-carry-forward skill as a row in
-- dispatcher.scheduled_skills so the daemon's _scheduled_skills_tick
-- fires it once a week. The skill runs scripts/audit_llm_carry_forward.py
-- across every CA county, applies per-check thresholds (see the skill's
-- backend at scripts/dispatcher/llm_carry_forward_probe.py), and either
--   - posts a summary comment on a long-lived audit-log issue, OR
--   - files a new agent/ready follow-up issue when any threshold trips.
--
-- The bug class — silent LLM carry-forward in multi-case CA tentative-
-- ruling PDFs — was documented in #3649 and went unnoticed for months.
-- A weekly periodic sweep keeps that quiet failure from re-emerging
-- between manual /spotcheck invocations.
--
-- Design notes
-- ------------
-- * name='audit-llm-carry-forward' matches the skill dir name
--   (.claude/skills/audit-llm-carry-forward/) and the slash-command
--   (/audit-llm-carry-forward) per the entrypoint contract:
--   phase=<name> -> claude -p <skill_invocation>.
--
-- * trigger_kind='cron', trigger_value='0 6 * * 0' — fires weekly on
--   Sunday at 06:00 UTC. Sunday morning is intentionally off-peak so
--   the audit doesn't compete with weekday spotcheck/audit traffic for
--   the dispatcher concurrency budget. Operators can temporarily
--   override the trigger_value to '*/5 * * * *' in the DB for one-cycle
--   verification, then restore.
--
-- * ON CONFLICT (name) DO NOTHING — idempotent; re-applying this
--   migration after rollback does not duplicate the row or overwrite
--   operator overrides already in place.
--
-- Rollback
-- --------
-- DELETE FROM dispatcher.scheduled_skills WHERE name='audit-llm-carry-forward'.
-- The skill dir and audit_llm_carry_forward.py / llm_carry_forward_probe.py
-- remain (no code is auto-deleted).

INSERT INTO dispatcher.scheduled_skills (
    name, skill_invocation, trigger_kind, trigger_value, enabled, notes
)
VALUES (
    'audit-llm-carry-forward',
    '/audit-llm-carry-forward',
    'cron',
    '0 6 * * 0',
    true,
    'Weekly LLM carry-forward audit across CA counties (issue #4309). '
    || 'Fires Sunday 06:00 UTC; runs scripts/audit_llm_carry_forward.py '
    || 'via scripts/dispatcher/llm_carry_forward_probe.py and either '
    || 'posts a summary comment on the long-lived audit-log issue or '
    || 'files an agent/ready follow-up when per-check thresholds trip. '
    || 'Bug class — silent LLM rule-5b carry-forward in multi-case PDFs — '
    || 'first documented in #3649; weekly cadence keeps it from going '
    || 'silent again between manual /spotcheck invocations.'
)
ON CONFLICT (name) DO NOTHING;


-- Down Migration
DELETE FROM dispatcher.scheduled_skills WHERE name = 'audit-llm-carry-forward';
