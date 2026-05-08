-- Up Migration
--
-- Dispatcher v2 — persistent per-skill scheduled_skills state (issue #4318).
--
-- Adds a ``last_run_state JSONB`` column to ``dispatcher.scheduled_skills``
-- so periodic skills can carry forward state across ECS task restarts.
-- Each scheduled-skill ECS task starts with an empty filesystem, which
-- means a skill that persists state to a local path
-- (e.g. ``tmp/llm-carry-forward/last_totals.json``) is permanently in
-- "first-run mode" — there is no second-run jump-detection baseline.
--
-- The ``/audit-llm-carry-forward`` skill (#4309) exposed this gap: its
-- noisy-axis +25% jump check (``motion_type_contradiction``,
-- ``case_title_text_mismatch``) never fires because the state file is
-- missing on every fire. With this column the skill's probe
-- (``scripts/dispatcher/llm_carry_forward_probe.py``) can write its
-- per-county totals to the row and read them on the next fire.
--
-- The probe is the reader and writer; the daemon does not touch this
-- column. That keeps the daemon's existing
-- ``_update_scheduled_skill_last_fire`` path unchanged — only
-- ``last_triggered_at`` and ``last_triggered_agent_id`` flow through
-- the daemon. Skill-specific state is per-skill and lives in
-- ``last_run_state`` whose shape the daemon does not interpret.
--
-- Schema notes
-- ------------
-- * Type is JSONB so each skill can encode whatever shape it needs.
--   For ``audit-llm-carry-forward`` it is
--   ``{county: {axis: count}}`` matching the existing
--   ``totals_by_county`` envelope shape (see
--   ``scripts/dispatcher/llm_carry_forward_probe.py``
--   ``extract_county_totals``).
--
-- * NULL is the explicit "first-run / no baseline" signal — the probe
--   treats NULL exactly the same as a missing local state file
--   (``_load_state`` returns ``None``) so behaviour is unchanged on
--   first fire.
--
-- * The column has no DEFAULT and no NOT NULL constraint — existing
--   rows stay NULL until the next fire writes a value, and rolling
--   back the migration in a hurry just drops the column without
--   needing to backfill.
--
-- * No index. The column is read+written one row at a time keyed by
--   ``name`` (the PK), so no secondary index is necessary.
--
-- Rollback
-- --------
-- DROP the column. No data loss for daemon-managed columns since this
-- column is skill-private state — the probe falls back to its
-- ``--state`` file path when the column is absent.

ALTER TABLE dispatcher.scheduled_skills
    ADD COLUMN last_run_state JSONB;

COMMENT ON COLUMN dispatcher.scheduled_skills.last_run_state IS
    'Skill-private state carried forward across runs. Type is JSONB so '
    'each scheduled skill can encode whatever shape it needs. NULL means '
    'no baseline — the skill treats it as first-run mode. The daemon '
    'does not read or write this column; only the skill itself does. '
    'For audit-llm-carry-forward (#4309/#4318) the value is '
    '{county: {axis: count}} for noisy-axis jump-detection across ECS '
    'task restarts. Issue #4318.';


-- Down Migration
ALTER TABLE dispatcher.scheduled_skills
    DROP COLUMN IF EXISTS last_run_state;
