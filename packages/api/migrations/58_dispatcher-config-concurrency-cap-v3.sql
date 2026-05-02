-- Up Migration
--
-- Dispatcher v3 buildout — seed `dispatcher.config.concurrency_cap_v3 = 0`.
-- Issue #3889 (F4 launcher ECS service AC).
--
-- Why
-- ---
-- The v3 launcher (modules/dispatcher-v3-service, F4) deploys at
-- `desired_count=1` so the container is RUNNING and writing heartbeat
-- ticks to ``dispatcher.runs`` immediately on apply. Per spec section 9
-- step 2, the launcher must observe the queue WITHOUT claiming any
-- issue until the operator manually flips the cap to 1+ for the
-- cap=1 smoke test (spec section 9 step 3).
--
-- v3 reads its own cap key — ``concurrency_cap_v3`` — distinct from
-- v2's ``concurrency_cap``. Per spec section 8.1, v2 and v3 have
-- independent concurrency caps during cohabitation so the operator
-- can ramp v3 up while ramping v2 down in lockstep without coupling
-- the two daemons through a single config row.
--
-- Without this seed, ``scripts/dispatcher_v3/launcher.py``'s
-- ``_read_concurrency_cap_v3`` falls back to its in-code default
-- (``DEFAULT_CONCURRENCY_CAP_V3 = 0``) for any row that does not
-- exist. So the launcher would still observe-but-not-claim — but the
-- AC requires the row to exist after deploy so the operator can flip
-- it via a ``dispatcher.commands`` write or a direct
-- ``UPDATE dispatcher.config`` without first having to INSERT the row.
--
-- Design notes
-- ------------
-- * ``ON CONFLICT (key) DO NOTHING`` makes the migration idempotent.
--   If an operator (or a future migration) has already inserted
--   ``concurrency_cap_v3`` with a non-zero value, this migration does
--   not clobber it. That matters for replays and for the case where
--   F4's PR lands AFTER an operator has manually seeded the row to
--   smoke v3.
-- * ``value`` is stored as a JSONB integer (``'0'::jsonb``) — same
--   shape as v2's ``concurrency_cap`` row from migration 21. The
--   launcher reads it as ``SELECT value FROM dispatcher.config
--   WHERE key = 'concurrency_cap_v3'`` and casts via ``int()``;
--   ``dispatcher.commands`` writes set the value via
--   ``set_config(key, jsonb_value)`` which accepts the same JSONB
--   integer shape.
-- * ``updated_by = 'init'`` matches the seed convention from
--   migration 21 (live-editable keys whose initial value came from a
--   migration, not an operator edit). The admin page and audit query
--   already filter on this value to distinguish migration seeds from
--   subsequent operator flips.
--
-- Verify (per the issue body's AC)
-- --------------------------------
--   ``scripts/dev-db-query.sh "SELECT value FROM dispatcher.config
--   WHERE key='concurrency_cap_v3'"`` must return ``0``.

INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('concurrency_cap_v3', '0'::jsonb, 'init')
ON CONFLICT (key) DO NOTHING;


-- Down Migration
--
-- Drop the row. If an operator has flipped the cap to a non-zero
-- value (e.g. cap=1 for the smoke test), DELETE here removes that
-- value too — the launcher's fallback default of 0 keeps it idle
-- after the rollback, so the down migration is non-destructive in
-- effect (the launcher behaves the same way it would with the
-- pre-migration empty-row state).

DELETE FROM dispatcher.config WHERE key = 'concurrency_cap_v3';
