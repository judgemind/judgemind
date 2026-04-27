-- Up Migration
--
-- Dispatcher v2 — add operational phase to stuck_timeout_s_by_phase config
-- (#3524).
--
-- Context
-- -------
-- The operational phase (/task-v2-operational) had no entry in the
-- ``stuck_timeout_s_by_phase`` JSONB config row, so hung operational agents
-- fell back to the global ``STUCK_TIMEOUT_SECONDS`` default (30 min).
-- Observed incidents: agent running issue #2522 ran 96 min; issue #2630
-- ran 72 min — both exceeding 60 min without being caught as stuck.
-- Most operational tasks (ECS oneshot runs, DB rebuilds, label/comment
-- actions) finish within ~30 min. 60 min gives 2× headroom while still
-- catching genuinely hung agents.
--
-- Design notes
-- ------------
-- - Uses ``jsonb_set`` to ADD the ``operational`` key without destroying
--   existing operator overrides on other keys (e.g. a manually raised
--   ralph threshold). The ``32_dispatcher-stuck-timeout-10x-bump.sql``
--   migration used ``EXCLUDED.value`` to overwrite the entire JSONB
--   object; this migration is additive-only to preserve live operator
--   tuning.
-- - ``ON CONFLICT (key) DO UPDATE`` handles the case where the config
--   row already exists (expected: migration 32 seeded it). If somehow
--   the row doesn't exist yet, the INSERT path creates a fresh JSONB
--   object with just the operational key; the other phase values will
--   fall through to ``STUCK_TIMEOUT_SECONDS_BY_PHASE`` module defaults
--   until migration 32 is applied.
-- - ``updated_by='init-3524'`` distinguishes this migration's write
--   from a manual operator edit. The admin page sets ``updated_by``
--   to a GitHub login when operators tune the row.

INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('stuck_timeout_s_by_phase',
     '{"operational":3600}'::jsonb,
     'init-3524')
ON CONFLICT (key) DO UPDATE
    SET value      = jsonb_set(dispatcher.config.value, '{operational}', '3600'),
        updated_by = 'init-3524',
        updated_at = NOW();


-- Down Migration
--
-- Down strategy: remove the operational key from the JSONB object.
-- This leaves all other per-phase overrides intact and restores the
-- pre-#3524 posture where operational agents fall back to the global
-- ``STUCK_TIMEOUT_SECONDS`` default (or the module default once the
-- code is also reverted).
UPDATE dispatcher.config
    SET value      = value - 'operational',
        updated_by = 'revert-3524',
        updated_at = NOW()
    WHERE key = 'stuck_timeout_s_by_phase';
