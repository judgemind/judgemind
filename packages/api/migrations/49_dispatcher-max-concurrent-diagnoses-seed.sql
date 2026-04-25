-- Up Migration
--
-- Dispatcher v2 — async diagnoser spawn (issue #3376). Seeds the
-- ``max_concurrent_diagnoses`` config row so the supervisor tick's
-- spawn pass can defer new diagnoses when the in-flight count is at
-- or above the cap.
--
-- Default = 2. Empirical reasoning:
--   * The diagnoser is Opus-driven and may take up to 90 min per
--     run; a cap of 2 means at most 2 concurrent Opus sessions per
--     daemon, which keeps token spend bounded without artificially
--     starving novel-failure diagnosis.
--   * Two concurrent diagnoses also matches the smallest meaningful
--     parallelism: one diagnoser invocation can be mid-skill-call
--     while another picks up a freshly emitted failure.
--   * Operators tune live via ``UPDATE dispatcher.config SET value
--     = '5' WHERE key = 'max_concurrent_diagnoses';`` (the daemon
--     reads the value once per supervisor tick).
--
-- Why a config row not a constant
-- -------------------------------
-- The right cap is empirical and depends on Opus throughput, the
-- failure mix, and operator preference (during early bring-up of a
-- new diagnoser action you may want to lower it to 1; under heavy
-- failure storms you may want to raise it). A constant would
-- require a redeploy for each adjustment — the config row keeps
-- the lever live.
--
-- Stored as JSONB number (``2``, not ``'2'``) so the daemon's
-- :meth:`_max_concurrent_diagnoses` reader can ``int(json.loads(raw))``
-- on the legacy fake-cursor shape AND read native ``int`` directly on
-- the production psycopg3 shape. Same convention as
-- ``diagnoser_fallback_rate_threshold`` (migration 26).

INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('max_concurrent_diagnoses', '2', 'init')
ON CONFLICT (key) DO NOTHING;


-- Down Migration
--
-- Delete the seed row. The daemon's reader falls back to the in-code
-- default (also 2) when the row is absent, so this is safe.
DELETE FROM dispatcher.config
WHERE key = 'max_concurrent_diagnoses';
