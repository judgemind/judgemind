-- Up Migration
--
-- Dispatcher v3 — seed the three v3-specific circuit breaker config
-- rows so the breaker boots with the spec's 2-of-3-in-1h calibration
-- instead of falling back to v2's 5-of-10-in-30min defaults.
--
-- Issue #3977 (spec + schema cleanup from audit findings, Item 2).
--
-- Why
-- ---
-- ``scripts/dispatcher_v3/breaker.py`` reads three v3-specific config
-- keys with v2 fallbacks:
--
--   * ``circuit_breaker_v3_window_size``        (v2 fallback: 10)
--   * ``circuit_breaker_v3_bad_outcome_threshold`` (v2 fallback: 5)
--   * ``circuit_breaker_v3_window_minutes``     (v2 fallback: 30)
--
-- v2's defaults are calibrated for v2's throughput profile (cap 3-5
-- sustaining 10 outcomes per 30-min window). v3's ramp throughput is
-- much lower — at cap=4, ~4 outcomes per 30min, which never fills v2's
-- window. The breaker effectively never fires until cap=8 sustained,
-- which is exactly when the conservative protection is no longer
-- needed.
--
-- Spec ``docs/specs/dispatcher-v3-spec.md`` §4.1 step 5 calls out
-- "Rolling 2-of-3 in last 1h on terminal outcomes" as the v3 breaker
-- shape. This migration seeds those three rows so the breaker boots
-- with the spec's calibration on a fresh deploy.
--
-- Design notes
-- ------------
-- * ``ON CONFLICT (key) DO NOTHING`` makes the migration idempotent
--   and non-destructive. If an operator (or a future migration) has
--   already seeded these keys via a ``dispatcher.commands`` write or
--   a direct UPDATE, this migration leaves their values alone.
-- * ``value`` is stored as a JSONB integer (``'3'::jsonb``,
--   ``'2'::jsonb``, ``'60'::jsonb``) — same shape as v2's breaker rows
--   from migration 21. ``breaker.py``'s ``_read_int_config`` reads
--   ``SELECT value FROM dispatcher.config WHERE key = $1`` and casts
--   via ``int()``; the JSONB integer literal is the contract.
-- * ``updated_by = 'init'`` matches the seed convention from migration
--   21 / 58 (live-editable keys whose initial value came from a
--   migration, not an operator edit). The cockpit and audit query
--   already filter on this value to distinguish migration seeds from
--   subsequent operator flips.
-- * ``dispatcher.config`` has no ``notes`` column — the rationale for
--   each key lives in this migration's comments only. The issue body
--   referenced a ``notes`` column; it does not exist on the table.
--   See the schema definition in migration 21 (``CREATE TABLE
--   dispatcher.config``) — columns are ``(key, value, updated_at,
--   updated_by)`` only.
--
-- Verify (per the issue's AC)
-- ---------------------------
--   ``scripts/dev-db-query.sh "SELECT key, value FROM dispatcher.config
--   WHERE key LIKE 'circuit_breaker_v3_%' ORDER BY key"`` returns
--   three rows: ``bad_outcome_threshold='2'``, ``window_minutes='60'``,
--   ``window_size='3'``.
--
-- Per-key rationale (so an operator inspecting the rows in psql can
-- find the "why" without needing to read this file):
--
--   * ``circuit_breaker_v3_window_size = 3`` — spec §4.1 (2-of-3 in
--     1h). v2's default of 10 is calibrated for v2 throughput; v3's
--     ramp throughput is too low to fill that window.
--   * ``circuit_breaker_v3_bad_outcome_threshold = 2`` — spec §4.1
--     (2-of-3 in 1h).
--   * ``circuit_breaker_v3_window_minutes = 60`` — spec §4.1 (2-of-3
--     in 1h). 1h gives headroom for retries to settle at low cap.

INSERT INTO dispatcher.config (key, value, updated_by)
VALUES
    ('circuit_breaker_v3_window_size',           '3'::jsonb,  'init'),
    ('circuit_breaker_v3_bad_outcome_threshold', '2'::jsonb,  'init'),
    ('circuit_breaker_v3_window_minutes',        '60'::jsonb, 'init')
ON CONFLICT (key) DO NOTHING;


-- Down Migration
--
-- Drop the three rows. After rollback, the breaker falls back to
-- ``circuit_breaker_window_size``, ``circuit_breaker_bad_outcome_threshold``,
-- and ``circuit_breaker_window_minutes`` (v2's shared keys), which is
-- the pre-#3977 behavior. Non-destructive in effect — no data is lost
-- and the breaker keeps functioning, just with v2's calibration.

DELETE FROM dispatcher.config
 WHERE key IN (
    'circuit_breaker_v3_window_size',
    'circuit_breaker_v3_bad_outcome_threshold',
    'circuit_breaker_v3_window_minutes'
 );
