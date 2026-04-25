-- Up Migration
--
-- Dispatcher v2 — generalized scheduled-skills table (issue #3374).
--
-- The dispatcher daemon needs cron-like scheduling for several
-- periodic skills: ``/audit`` every N merges, ``/spotcheck`` on a
-- daily cron, plus future periodic skills (daily failure-summary
-- report, 4h Telegram digest, etc.). Building one bespoke wiring per
-- skill is DRY violation territory — five+ surfaces == five+ places
-- to introduce bugs. This migration adds a single table the daemon
-- iterates on every supervisor tick; one row per scheduled skill.
--
-- Schema notes
-- ------------
-- * ``name`` is the PRIMARY KEY because the daemon UPSERTs by name on
--   every tick. The matching ``phase`` value the daemon writes on the
--   synthetic ``dispatcher.agents`` row is also ``name`` — so a row
--   with ``name='audit'`` produces ``phase='audit'`` on the spawned
--   agent. The agent-runner-entrypoint dispatches ``phase=<name>`` to
--   ``claude -p <skill_invocation>``.
--
-- * ``trigger_kind`` is one of ``'cron'`` or ``'every_n_merges'``.
--   New trigger kinds (e.g. ``'every_n_failures'``) can be added in
--   later migrations without breaking existing rows. Kept as TEXT
--   rather than CHECK-constrained ENUM so the value set can grow
--   without a schema migration.
--
-- * ``trigger_value`` is TEXT. For ``'cron'`` it is a 5-field cron
--   expression (e.g. ``'0 14 * * *'``). For ``'every_n_merges'`` it
--   is the integer N as a string (e.g. ``'20'``). The daemon parses
--   the value per-trigger-kind; the column stays type-agnostic so
--   future trigger kinds can encode whatever they need.
--
-- * ``last_triggered_at`` is the timestamp the daemon last fired this
--   skill. NULL on first-time rows (the daemon treats NULL as "never
--   fired" — for cron rows that means the next fire is computed from
--   the current time; for every-N-merges rows that means we count
--   from the row's INSERT time via the agent's ``merged_at`` column).
--
-- * ``last_triggered_agent_id`` is the ``dispatcher.agents.agent_id``
--   row the last fire produced. FK is ``ON DELETE SET NULL`` so a
--   ``DELETE FROM dispatcher.agents`` (very rare) doesn't cascade
--   here and break the scheduler.
--
-- * Existing ``dispatcher.config`` rows ``idle_audit_every_n_prs`` and
--   ``idle_spotcheck_cron`` are migrated as rows in this table but
--   NOT deleted from ``dispatcher.config``. The follow-up cleanup
--   (one verified deploy cycle later) drops them. Keeping both copies
--   in this PR means a rollback to the prior daemon image still
--   reads its expected config values.
--
-- * The two seed rows below are inserted only if their
--   ``dispatcher.config`` source rows exist — defensive for envs
--   where the seed migration (21) was hand-edited.
--
-- Rollback
-- --------
-- Dropping the table is safe — nothing else in the schema references
-- it, and the daemon code tolerates its absence (the
-- ``_scheduled_skills_tick`` helper SELECTs from a try/except wrapper
-- so an absent table just skips the pass).

CREATE TABLE dispatcher.scheduled_skills (
    name                     TEXT        PRIMARY KEY,
    skill_invocation         TEXT        NOT NULL,
    trigger_kind             TEXT        NOT NULL,
    trigger_value            TEXT        NOT NULL,
    enabled                  BOOLEAN     NOT NULL DEFAULT true,
    last_triggered_at        TIMESTAMPTZ,
    last_triggered_agent_id  UUID        REFERENCES dispatcher.agents(agent_id) ON DELETE SET NULL,
    notes                    TEXT
);

COMMENT ON TABLE dispatcher.scheduled_skills IS
    'Generalized cron-like scheduler rows (issue #3374). Daemon iterates '
    'enabled=true rows on every supervisor tick; per row computes whether '
    'now() >= next-fire-time (for cron) or merged-since-last-fire >= N '
    '(for every_n_merges) and spawns a synthetic dispatcher.agents row '
    'with phase=<name>. The agent-runner-entrypoint dispatches phase '
    'into claude -p <skill_invocation>.';

COMMENT ON COLUMN dispatcher.scheduled_skills.name IS
    'PRIMARY KEY. Also the value the daemon writes to '
    'dispatcher.agents.phase on the synthetic row, so the agent-runner '
    'entrypoint can route phase=<name> to claude -p <skill_invocation>.';

COMMENT ON COLUMN dispatcher.scheduled_skills.skill_invocation IS
    'Slash-command form of the skill (e.g. ''/audit'', ''/spotcheck''). '
    'The agent-runner entrypoint passes this verbatim to '
    '``claude -p <skill_invocation> <agent_id>``.';

COMMENT ON COLUMN dispatcher.scheduled_skills.trigger_kind IS
    'One of ''cron'' (trigger_value is a 5-field cron expression) or '
    '''every_n_merges'' (trigger_value is integer N as string — fire '
    'when N agents have merged since last_triggered_at).';

COMMENT ON COLUMN dispatcher.scheduled_skills.trigger_value IS
    'Per-trigger-kind value. For cron: 5-field expression. For '
    'every_n_merges: integer N as string. TEXT so future trigger kinds '
    'can encode whatever they need without a schema migration.';

COMMENT ON COLUMN dispatcher.scheduled_skills.last_triggered_at IS
    'Timestamp of the last fire. NULL means never fired (the daemon '
    'treats NULL-cron as "compute next fire from now()" and NULL-N '
    'as "count merges from row INSERT time").';

COMMENT ON COLUMN dispatcher.scheduled_skills.last_triggered_agent_id IS
    'agent_id of the dispatcher.agents row spawned by the last fire. '
    'FK ON DELETE SET NULL so a rare agents cleanup doesn''t cascade.';

-- Seed: migrate existing dispatcher.config keys. Both rows are inserted
-- only if their source config row exists (defensive against hand-edited
-- seed migrations). Source config rows are NOT deleted in this migration —
-- a follow-up after one verified deploy cycle drops them.

INSERT INTO dispatcher.scheduled_skills (
    name, skill_invocation, trigger_kind, trigger_value, enabled, notes
)
SELECT
    'audit',
    '/audit',
    'every_n_merges',
    -- value is JSONB; cast to text and trim quotes/whitespace.
    btrim((SELECT value::text FROM dispatcher.config WHERE key = 'idle_audit_every_n_prs'), '" '),
    true,
    'Migrated from dispatcher.config.idle_audit_every_n_prs (#3374). '
    || 'Source key not yet dropped — drops in a follow-up after one '
    || 'verified deploy cycle.'
WHERE EXISTS (
    SELECT 1 FROM dispatcher.config WHERE key = 'idle_audit_every_n_prs'
)
ON CONFLICT (name) DO NOTHING;

INSERT INTO dispatcher.scheduled_skills (
    name, skill_invocation, trigger_kind, trigger_value, enabled, notes
)
SELECT
    'spotcheck',
    '/spotcheck',
    'cron',
    -- value is JSONB string e.g. '"0 14 * * *"'; ->> '' would need a
    -- key. Use #>>'{}' to extract the scalar string from a JSONB value.
    (SELECT value #>> '{}' FROM dispatcher.config WHERE key = 'idle_spotcheck_cron'),
    true,
    'Migrated from dispatcher.config.idle_spotcheck_cron (#3374). '
    || 'Source key not yet dropped — drops in a follow-up after one '
    || 'verified deploy cycle.'
WHERE EXISTS (
    SELECT 1 FROM dispatcher.config WHERE key = 'idle_spotcheck_cron'
)
ON CONFLICT (name) DO NOTHING;


-- Down Migration
DROP TABLE IF EXISTS dispatcher.scheduled_skills;
