-- Up Migration
--
-- Dispatcher v2 — seed + bump per-phase stuck_timeout overrides in
-- ``dispatcher.config`` (#2885).
--
-- Context
-- -------
-- Overnight 2026-04-20 observed a race in the supervisor's stuck-agent
-- detection: agent ``821e96ee`` on issue #2565 ran ralph for 5834s and
-- exited cleanly, but at 5419s the supervisor flagged it stuck against
-- the 5400s (90min) ralph threshold and fired a retry. Ralph finished
-- 415s later, but the retry had already started — producing
-- ``phase_output_missing`` and a failed agent despite ralph actually
-- succeeding. Separately, plan agents hit ``error_max_turns`` at 51/50
-- on complex scraper issues (#2564, #2565), burning ~$3 of opus with
-- zero output.
--
-- Operator directive (verbatim): "stop being parsimonious on these
-- limits. Cost is not the constraint; success is."
--
-- Design notes
-- ------------
-- - This migration writes the ``stuck_timeout_s_by_phase`` JSONB row in
--   ``dispatcher.config``, which the supervisor reads once per sweep
--   via ``DispatcherDaemon._read_stuck_timeout_overrides``. Any key
--   present in the JSONB object wins over the ``STUCK_TIMEOUT_SECONDS_BY_PHASE``
--   module default (daemon.py). The row did NOT exist before this
--   migration — fresh installs fell through to module defaults, which
--   were the pre-#2885 tight windows. Seeding the row here guarantees
--   a consistent 10× posture across all live environments.
-- - ``ON CONFLICT (key) DO UPDATE`` is critical: on an install where
--   an operator had already created the row by hand (e.g. raised
--   ralph to 10800s to work around the race but never PR'd the
--   change), this migration bumps the value to 54000s — consistent
--   with the "bump the already-live rows, not just defaults for fresh
--   installs" requirement in #2885. Operators who need a LOWER value
--   than 10× can manually edit the row post-migration via the admin
--   page; the configuration path still wins over the module default.
-- - JSONB key names match the daemon's ``STUCK_TIMEOUT_SECONDS_BY_PHASE``
--   dict keys. ``plan`` and ``planning`` are both populated because
--   both appear as phase_transitions row labels (legacy alias).
-- - Non-LLM phases (``claiming``, ``awaiting_ci``, ``awaiting_deploy``,
--   ``paused_by_killswitch``, ``daemon_restart_abandoned``) are NOT
--   included: their upper bound is set by external systems
--   (gh API, GitHub Actions wall-clock, ECS rolling deploy, terminal
--   sweep loops), so 10× would just delay real stuck-detection.
-- - ``updated_by='init-2885'`` distinguishes this migration's write
--   from a manual operator edit. The admin page sets ``updated_by``
--   to a GitHub login when operators tune the row.

INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('stuck_timeout_s_by_phase',
     '{"planning":9000,"plan":9000,"ralph":54000,"summary":6000,"verify":3000,"retro":3000,"fix_ci":18000}'::jsonb,
     'init-2885')
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value,
        updated_by = EXCLUDED.updated_by,
        updated_at = NOW();


-- Down Migration
--
-- Down strategy: remove the row entirely. Reverting reinstates fall-
-- through to ``STUCK_TIMEOUT_SECONDS_BY_PHASE`` module defaults.
-- If this migration is run down AFTER the daemon defaults have also
-- been reverted (via a code revert), the dispatcher returns to its
-- pre-#2885 posture. We do NOT restore any previous row value here
-- because the row didn't exist pre-#2885.
DELETE FROM dispatcher.config WHERE key = 'stuck_timeout_s_by_phase';
