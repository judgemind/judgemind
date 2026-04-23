-- Up Migration
--
-- Dispatcher v2 — Phase 3 sub-task D. Seeds two config rows that gate
-- the §8 tier-2/3 diagnoser added in `scripts/dispatcher/daemon.py`.
--
-- Issue #2795 (Parent: #2782, Phase 3 epic).
--
-- Design notes
-- ------------
-- - `diagnoser_enabled` is the kill switch. Default `true`. The daemon
--   reads this value once per supervisor tick; circuit-breaker logic
--   in the daemon flips it to `false` when the 24h fallback rate
--   (diagnoses that timed out, crashed, or returned malformed JSON)
--   exceeds the threshold below AND at least 5 diagnoses have run.
--   Operators manually re-enable by `UPDATE dispatcher.config SET
--   value = 'true' WHERE key = 'diagnoser_enabled';`.
-- - `diagnoser_fallback_rate_threshold` is the circuit-breaker knob.
--   `0.30` (30%) matches spec §8 "Budget & safety". Stored as a JSONB
--   number so operators can tune live (e.g. `'0.50'` for more
--   tolerance during early bring-up).
-- - No DDL changes — both rows are pure seeds. `ON CONFLICT DO NOTHING`
--   keeps the migration idempotent on re-runs (e.g. if an operator
--   already inserted the rows manually) and means a down-migration
--   that deletes them by key is safe even if the row was never
--   inserted in the first place.
-- - `updated_by='init'` matches the convention in migration 21:
--   migration-seeded rows are distinguishable from operator edits.

INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('diagnoser_enabled',                 'true',   'init'),
    ('diagnoser_fallback_rate_threshold', '0.30',   'init')
ON CONFLICT (key) DO NOTHING;


-- Down Migration
DELETE FROM dispatcher.config
WHERE key IN ('diagnoser_enabled', 'diagnoser_fallback_rate_threshold');
