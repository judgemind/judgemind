-- Up Migration
--
-- Dispatcher v2 — per-event Telegram notifications and configurable
-- digest. Seeds ``dispatcher.config`` rows consumed by
-- ``_drain_per_event_telegram_queue`` and ``_maybe_emit_digest`` in
-- ``scripts/dispatcher/daemon.py``.
--
-- Issue #2861. Context:
--
--   The daemon already fires Telegram alerts for circuit-breaker trips
--   (#2860, #3061). This migration extends the notification surface to
--   every terminal agent transition (succeeded / plan_blocked /
--   needs_review / failed) via a bounded in-memory queue with a
--   configurable per-event throttle, and adds a periodic digest that
--   summarises outcomes since the last digest.
--
-- Three new ``dispatcher.config`` knobs:
--
--   - ``telegram_per_event_throttle_seconds`` (int, default 30): minimum
--     gap between consecutive per-event sends. The 30s scheduler-tick
--     cadence means burst=1 per tick at the tightest setting; operators
--     can relax to 60 or 300 if the channel gets noisy.
--
--   - ``digest_interval_seconds`` (int, default 14400 = 4h): how often
--     the supervisor emits the periodic digest. ``last_digest_at``
--     records when the last digest fired so the window is always
--     "since last digest" rather than a fixed-offset cron.
--
--   - ``last_digest_at`` (text ISO-8601 | null): UTC timestamp of the
--     last digest. NULL on a fresh deployment; the daemon falls back to
--     ``now() - digest_interval_seconds`` for the first window. The
--     daemon writes this back after every successful digest send via an
--     ``INSERT … ON CONFLICT … DO UPDATE`` so the next supervisor tick
--     can skip the window check cheaply.
--
-- Design notes
-- ------------
-- - ``ON CONFLICT (key) DO NOTHING`` keeps the seed block idempotent:
--   a re-run over a DB where an operator has already tuned
--   ``telegram_per_event_throttle_seconds`` to 60 will NOT revert it
--   to 30.
-- - ``last_digest_at`` is stored as a text ISO-8601 string (not a
--   ``TIMESTAMPTZ`` column) because ``dispatcher.config`` is a
--   free-form key/value table (value TEXT). The daemon calls
--   ``datetime.fromisoformat(raw)`` when reading it; a NULL or
--   non-parseable value falls back gracefully to the interval window.
-- - No new table is created — the notification data lives ephemerally
--   in the in-memory queue and is flushed to Telegram. A persistent
--   ``dispatcher.notifications`` table is noted as a follow-up in
--   the spec §13 "Messages are persisted … schema TBD".

-- ── dispatcher.config seeds ────────────────────────────────────────────
INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('telegram_per_event_throttle_seconds', '30',    'init'),
    ('digest_interval_seconds',             '14400', 'init'),
    ('last_digest_at',                      'null',  'init')
ON CONFLICT (key) DO NOTHING;


-- Down Migration
DELETE FROM dispatcher.config
WHERE key IN (
    'telegram_per_event_throttle_seconds',
    'digest_interval_seconds',
    'last_digest_at'
);
