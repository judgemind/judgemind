-- Up Migration
--
-- Dispatcher v2 — seed per-phase max_turns overrides in
-- ``dispatcher.config`` (#2890).
--
-- Context
-- -------
-- Issue #2890 adds a live-editable max_turns cap per phase so operators
-- can raise (or lower) turn limits without a code deploy. This migration
-- seeds the initial ``max_turns_by_phase`` row in ``dispatcher.config``
-- with values matching the ``PHASE_MAX_TURNS`` module defaults established
-- by #2885 (the 10× bump). After this migration, operators can tune any
-- phase cap via the admin page; the config row wins over the module
-- default in resolution order.
--
-- Design notes
-- ------------
-- - This migration writes the ``max_turns_by_phase`` JSONB row in
--   ``dispatcher.config``, which ``_spawn_phase_subprocess`` reads once
--   per spawn call via ``DispatcherDaemon._read_max_turns_overrides``.
--   Any key present in the JSONB object wins over the ``PHASE_MAX_TURNS``
--   module default (daemon.py). The row did NOT exist before this
--   migration — fresh installs fell through to module defaults.
--   Seeding the row here guarantees a consistent posture across all
--   live environments.
-- - ``ON CONFLICT (key) DO UPDATE`` is critical: on an install where
--   an operator had already created the row by hand, this migration
--   sets the value to match the current module defaults.
-- - JSONB key names use hyphen form (``fix-ci``) matching the daemon's
--   ``PHASE_MAX_TURNS`` dict keys. This intentionally differs from the
--   underscore form used by ``stuck_timeout_s_by_phase`` (``fix_ci``).
--   The hyphen/underscore drift is a known asymmetry (#2889): PHASE_MAX_TURNS
--   and PHASE_MODELS use hyphen form because those keys come directly
--   from the ``/task-v2-fix-ci`` skill invocation path. See daemon.py's
--   ``PHASE_MAX_TURNS`` docstring.
-- - ``updated_by='init-2890'`` distinguishes this migration's write
--   from a manual operator edit. The admin page sets ``updated_by``
--   to a GitHub login when operators tune the row.

INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('max_turns_by_phase',
     '{"plan":500,"ralph":5000,"summary":300,"fix-ci":1000,"verify":500,"retro":300}'::jsonb,
     'init-2890')
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value,
        updated_by = EXCLUDED.updated_by,
        updated_at = NOW();


-- Down Migration
--
-- Down strategy: remove the row entirely. Reverting reinstates fall-
-- through to ``PHASE_MAX_TURNS`` module defaults.
DELETE FROM dispatcher.config WHERE key = 'max_turns_by_phase';
