-- Up Migration
--
-- Dispatcher v2 — add ``target_concurrency_cap`` config row for the
-- circuit-breaker time-based auto-close path (#3779).
--
-- Context
-- -------
-- The pre-#3779 ``concurrency_cap`` row was overloaded for two
-- different roles:
--
--   * Runtime state — what the scheduler tick reads to decide how many
--     agents to spawn. The circuit breaker writes this to 0 when the
--     bad-outcome window crosses threshold.
--   * Operator intent — the cap the operator wants the system to run at
--     under normal conditions (typically 4).
--
-- That overload made circuit-breaker recovery a manual two-step:
-- ``breaker.sh reset`` set cap → 1 (the legacy ``start`` command), then
-- the operator had to ``UPDATE dispatcher.config`` again to bump cap
-- back up to their actual target. Worse, when the bad-outcome window
-- rolled below threshold there was no path for the breaker to
-- self-close — the only thing that could close it (good outcomes) was
-- gated by the breaker itself (cap=0 → no agents → no outcomes).
--
-- ``target_concurrency_cap`` separates the two roles. The operator
-- writes the target; the breaker writes the runtime ``concurrency_cap``;
-- the time-based auto-close path uses the target as its restoration
-- value.
--
-- Design notes
-- ------------
-- - Default value mirrors the existing ``concurrency_cap`` seed (5)
--   so a fresh dev DB matches today's behaviour. The migration's
--   INSERT also reads the live ``concurrency_cap`` value if present
--   so already-deployed environments preserve operator tuning rather
--   than snapping back to the seed default. ``ON CONFLICT DO NOTHING``
--   keeps the seed idempotent across re-runs.
-- - ``updated_by='init-3779'`` distinguishes this migration's seed
--   from later operator edits via the admin cockpit.
-- - The ``target_concurrency_cap`` value is JSONB integer. The daemon
--   reads it via the same ``_cb_config_int`` helper used for the
--   breaker window/threshold knobs, so malformed values fall back to
--   the safe default of 1 (matching legacy ``start`` semantics).

INSERT INTO dispatcher.config (key, value, updated_by)
SELECT 'target_concurrency_cap',
       COALESCE(
           -- Preserve operator-tuned cap on already-deployed environments.
           (SELECT value FROM dispatcher.config WHERE key = 'concurrency_cap'),
           '5'::jsonb
       ),
       'init-3779'
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE dispatcher.config IS 'Live-editable key/value settings for the dispatcher daemon. ``concurrency_cap`` is runtime state (the breaker may rewrite it to 0); ``target_concurrency_cap`` is operator intent (the breaker reads it on auto-close to know what cap to restore). Issue #3779.';


-- Down Migration
DELETE FROM dispatcher.config
WHERE key = 'target_concurrency_cap';
