-- Up Migration
--
-- Dispatcher v2 — idle-hook triggers for /audit and /spotcheck (#2864).
--
-- Context
-- -------
-- Issue #2864 adds _maybe_trigger_idle_hooks to scheduler_tick so
-- the daemon can spawn /audit (on PR-count threshold) and /spotcheck
-- (on a cron schedule) without operator intervention.
--
-- Design notes
-- ------------
-- - ``last_run_at`` is seeded to ``now()`` so a freshly-deployed daemon
--   does NOT immediately fire both hooks on its first tick.  The operator
--   is expected to tune thresholds via ``dispatcher.config`` if they want
--   an immediate run.
-- - ``last_run_agent_id`` is nullable; populated once the synthetic agent
--   has been spawned so the admin page can link to the audit/spotcheck
--   agent row.
-- - The table lives in the ``dispatcher`` schema alongside the rest of
--   the daemon's state; it is NOT rebuildable from S3 (telemetry tier).

CREATE TABLE dispatcher.idle_hooks_state (
    hook_name         TEXT        PRIMARY KEY,
    last_run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_agent_id UUID        REFERENCES dispatcher.agents(agent_id)
);

COMMENT ON TABLE  dispatcher.idle_hooks_state                    IS 'Tracks last run time and agent for each idle-triggered hook (audit, spotcheck).';
COMMENT ON COLUMN dispatcher.idle_hooks_state.hook_name          IS 'Hook identifier: ''audit'' or ''spotcheck''.';
COMMENT ON COLUMN dispatcher.idle_hooks_state.last_run_at        IS 'Timestamp of most recent spawn. Seeded to now() to suppress first-tick fires.';
COMMENT ON COLUMN dispatcher.idle_hooks_state.last_run_agent_id  IS 'FK to dispatcher.agents row for the most recent synthetic agent spawn; NULL before first run.';

-- Seed both hooks. last_run_at = now() prevents a cold-start double-fire.
INSERT INTO dispatcher.idle_hooks_state (hook_name, last_run_at)
VALUES
    ('audit',     now()),
    ('spotcheck', now());


-- Down Migration
--
-- Remove the table entirely.
DROP TABLE IF EXISTS dispatcher.idle_hooks_state;
