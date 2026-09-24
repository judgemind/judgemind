-- Up Migration
--
-- Dispatcher — diagnoser circuit-breaker time-bounded auto-recovery.
--
-- Issue #4586. The diagnoser circuit breaker
-- (`scripts/dispatcher/daemon.py::_check_diagnoser_circuit_breaker`) flips
-- `diagnoser_enabled` to `false` when the 24h fallback rate exceeds the
-- threshold. Pre-#4586 the recovery semantics were one-way: once disabled,
-- no diagnoses run → no measurement → the breaker can never re-evaluate the
-- fallback rate, so the flag stayed `false` until an operator manually
-- flipped it. The practical effect (observed in #4586) was the dispatcher
-- silently degraded for an unknown number of days while 213 ralph_not_ship
-- failures piled up with zero diagnoses.
--
-- Design notes
-- ------------
-- - `diagnoser_breaker_recovery_window_seconds` is the auto-recovery knob.
--   `86400` (24h) — one full fallback-measurement window — so a retrip
--   evaluates a fresh 24h of diagnoses rather than the stale window that
--   tripped it. The daemon's `_check_diagnoser_breaker_auto_recover` reads
--   this once per supervisor tick; when `now() - diagnoser_breaker_tripped_at`
--   exceeds it, the daemon re-enables the diagnoser and clears the trip
--   timestamp. If the fallback rate is still bad the breaker simply retrips
--   on the next window — trading "silent degradation forever" for "limited
--   blast radius per trip."
-- - `diagnoser_breaker_tripped_at` is NOT seeded here — the daemon UPSERTs it
--   on trip (a JSON-encoded ISO-8601 UTC string) and clears it (to JSON
--   `null`) on auto-recovery. Seeding it would imply a trip that never
--   happened.
-- - Stored as a JSONB number so operators can tune live (e.g. `'43200'` for
--   a 12h window). `ON CONFLICT DO NOTHING` keeps the migration idempotent.
-- - `updated_by='init'` matches the convention in migrations 21/26:
--   migration-seeded rows are distinguishable from operator edits.

INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('diagnoser_breaker_recovery_window_seconds', '86400', 'init')
ON CONFLICT (key) DO NOTHING;


-- Down Migration
DELETE FROM dispatcher.config
WHERE key IN (
    'diagnoser_breaker_recovery_window_seconds',
    'diagnoser_breaker_tripped_at'
);
