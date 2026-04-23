-- Up Migration
--
-- Dispatcher v2 — overnight safety circuit breaker. Adds the storage
-- and seeded config knobs for ``_evaluate_circuit_breaker`` in
-- ``scripts/dispatcher/daemon.py``.
--
-- Issue #2860 (Parent: dispatcher v2 overnight safety). Context:
--
--   For unattended overnight cap=1 operation, the daemon needs a rail
--   that auto-pauses (``concurrency_cap=0``) when the last N terminal
--   agent outcomes are bad (``failed`` / ``crashed`` / ``plan_blocked``
--   / ``needs_review``). Without this, a cascading failure mode
--   (Gemini API rate limit → every reviewer SKIPPED → every summary
--   unmet → N failed runs in a row) burns $ until morning.
--
-- Two changes:
--
--   1. ``dispatcher.terminal_outcomes`` — append-only ring-buffer
--      table. One row per terminal agent transition, populated by the
--      daemon's ``_mark_agent_terminal`` path. Bounded by the rolling
--      30-minute window the breaker scans + the 30-day housekeeping
--      prune (see below for the reuse of the existing retention
--      mechanism — the breaker owns its own retention knob so operators
--      can tune independently).
--
--   2. ``dispatcher.config`` seeds:
--      - ``circuit_breaker_enabled`` (bool, default ``true``): kill
--        switch. Operator flips to ``false`` to disable the rail
--        entirely (e.g. during controlled chaos testing).
--      - ``circuit_breaker_window_minutes`` (int, default ``30``):
--        rolling window for the threshold check.
--      - ``circuit_breaker_window_size`` (int, default ``10``): N in
--        "M of last N terminal states".
--      - ``circuit_breaker_bad_outcome_threshold`` (int, default ``5``):
--        M in "M of last N" — trip the breaker when at least this many
--        of the last N terminals in the rolling window are bad.
--      - ``cap_flipped_by`` (text, default ``null``): diagnostic trail
--        for "who last flipped ``concurrency_cap``". When the breaker
--        opens, this becomes ``"circuit_breaker"``. When the operator
--        manually flips cap up to ≥1, the daemon's scheduler tick
--        observes the mismatch, logs ``daemon.circuit_breaker_closed``,
--        and resets ``cap_flipped_by`` to ``null``. Also surfaced in
--        the admin cockpit as "Circuit open: N/M recent agents bad,
--        cap held at 0".
--
-- Design notes
-- ------------
-- - ``terminal_outcomes`` is append-only for the same reason
--   ``phase_transitions`` is: the rolling-window scan (``ended_at >
--   now() - interval``) wants the newest rows cheaply. A single
--   ``(ended_at DESC)`` index gives O(window_size) scan cost with no
--   deletion churn. Pruning is deferred to the housekeeping tick.
-- - ``status`` is free-form text (not an enum) for the same reason
--   ``dispatcher.agents.status`` is: future correct-outcome terminals
--   (``needs_review`` from #2856, or a ``ci_unrecoverable`` variant
--   from #2860's sibling work) slot in without a schema migration.
--   The daemon's classifier is the single source of truth for which
--   statuses count as "bad" — see ``BAD_OUTCOME_STATUSES`` in
--   ``scripts/dispatcher/daemon.py``.
-- - ``agent_id`` is NULLABLE to tolerate synthetic test inputs that do
--   not reference a real ``dispatcher.agents`` row. Production writes
--   always carry ``agent_id``. Keeping the column non-FK-constrained
--   avoids a cascade-delete interaction with the existing
--   ``dispatcher.agents`` retention story.
-- - ``ON CONFLICT DO NOTHING`` keeps the seed block idempotent: a
--   re-run over a DB where an operator has already tuned the threshold
--   to 3 will NOT revert it to 5.

-- ── dispatcher.terminal_outcomes ──────────────────────────────────────
CREATE TABLE dispatcher.terminal_outcomes (
    outcome_id    BIGSERIAL   PRIMARY KEY,
    agent_id      UUID,
    issue_number  INT,
    status        TEXT        NOT NULL,
    ended_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_dispatcher_terminal_outcomes_ended_at
    ON dispatcher.terminal_outcomes (ended_at DESC);

COMMENT ON TABLE  dispatcher.terminal_outcomes            IS 'Append-only ring buffer of terminal agent outcomes. Scanned by ``_evaluate_circuit_breaker`` over a rolling window to decide whether to auto-pause ``concurrency_cap``. Issue #2860.';
COMMENT ON COLUMN dispatcher.terminal_outcomes.status     IS 'Free-form text (succeeded | failed | crashed | plan_blocked | needs_review | ...). Classifier in daemon decides which count as bad.';
COMMENT ON COLUMN dispatcher.terminal_outcomes.agent_id   IS 'Nullable — synthetic test inputs may have no agent row. Production writes always carry this.';

-- ── dispatcher.config seeds ────────────────────────────────────────────
INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('circuit_breaker_enabled',                 'true', 'init'),
    ('circuit_breaker_window_minutes',          '30',   'init'),
    ('circuit_breaker_window_size',             '10',   'init'),
    ('circuit_breaker_bad_outcome_threshold',   '5',    'init'),
    ('cap_flipped_by',                          'null', 'init')
ON CONFLICT (key) DO NOTHING;


-- Down Migration
DELETE FROM dispatcher.config
WHERE key IN (
    'circuit_breaker_enabled',
    'circuit_breaker_window_minutes',
    'circuit_breaker_window_size',
    'circuit_breaker_bad_outcome_threshold',
    'cap_flipped_by'
);

DROP INDEX IF EXISTS dispatcher.idx_dispatcher_terminal_outcomes_ended_at;
DROP TABLE IF EXISTS dispatcher.terminal_outcomes;
