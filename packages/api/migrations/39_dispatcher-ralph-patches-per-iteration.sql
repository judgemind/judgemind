-- Up Migration
--
-- Dispatcher v2 — per-iteration ralph patch persistence (#3026).
--
-- Motivation
-- ----------
-- #3013 persisted a ralph patch to ``dispatcher.ralph_patches`` only on the
-- SHIP verdict — the narrow window between ralph reaching SHIP and the
-- daemon's ``gh pr create`` succeeding. Everything before SHIP was
-- ephemeral; a ralph crash, timeout, or abandonment mid-loop destroyed
-- every iteration's accumulated work.
--
-- Concrete cost observed 2026-04-22 on #2564: attempts 2, 3, and 4 each
-- accumulated significant ralph work (reviewer passes complete, pytest
-- green, failing only in pre-push) that was destroyed by retry-related
-- worktree cleanup. Each fresh attempt started from zero. Across the
-- three attempts this cost ~9 hours of compute with no durable progress.
--
-- The fix
-- -------
-- Save a cumulative patch to ``dispatcher.ralph_patches`` after every
-- ralph iteration (not only on SHIP). On resume (same agent or fresh
-- claim on the same issue, within 7-day TTL), apply the most recent
-- saved patch via ``git am --3way``. On conflict, the daemon LEAVES
-- the conflicted state intact and hands the decision to ralph via a
-- structured "RESUME WITH CONFLICT" prompt block — ralph judges whether
-- the prior work is recoverable or should be discarded.
--
-- Schema change
-- -------------
-- Two nullable columns on the existing ``dispatcher.ralph_patches`` table:
--
-- * ``iteration_n INT``
--     - 1-based iteration number at the time the row was written.
--     - NULL for legacy rows from #3013 era (pre-migration) and for
--       SHIP rows (the SHIP write keeps the #3013 DELETE-supersede
--       semantics and carries verdict='SHIP' with iteration_n=NULL so
--       the unified resume lookup orders by created_at DESC uniformly).
--     - Operators can distinguish legacy rows (``iteration_n IS NULL
--       AND verdict IS NULL``) from new SHIP rows (``iteration_n IS
--       NULL AND verdict = 'SHIP'``) from per-iteration intermediate
--       rows (``iteration_n IS NOT NULL AND verdict IN ('LOOP',
--       'SHIP')``).
--
-- * ``verdict TEXT``
--     - 'LOOP' (iteration finished, loop continues), 'SHIP' (terminal
--       SHIP verdict), 'ABORT' (iteration gave up), or NULL (legacy).
--     - Not a CHECK-constrained enum — the daemon writes one of these
--       four values and readers must tolerate unknown strings from
--       future versions (forward-compat).
--
-- Lifecycle
-- ---------
-- * Rows accumulate per-iteration during a ralph run: INSERT (no
--   supersede) per iteration with the daemon's cumulative-patch
--   (``git format-patch origin/main..HEAD --stdout``). Multiple rows
--   for the same (agent_id, iteration_n) are tolerated — the unified
--   resume lookup picks the most recent by ``created_at``.
-- * On SHIP, the existing #3013 DELETE-by-issue-number-then-INSERT
--   path still runs, supersedeing the intermediate rows and writing
--   the authoritative SHIP row with verdict='SHIP'.
-- * On successful ``gh pr create``, the existing #3013 DELETE-by-
--   agent-id path still runs, cleaning up all rows (SHIP + any
--   intermediate rows the SHIP delete didn't catch).
-- * The 7-day TTL housekeeping sweep from #3012 catches everything
--   else (abandoned runs, daemon crashes, operator force-stops).
--
-- Resume lookup (see daemon ``_apply_prior_ralph_patch``)
-- -------------------------------------------------------
-- ``SELECT ... WHERE issue_number = %s AND created_at > now() -
-- interval '7 days' ORDER BY created_at DESC LIMIT 1;``
--
-- Any agent_id matches — same-agent restart, cross-agent fresh claim,
-- or legacy same-issue-retry all use the unified lookup. The 7-day TTL
-- predicate is redundant with the housekeeping sweep but keeps a
-- partial-sweep state from surfacing stale patches.

ALTER TABLE dispatcher.ralph_patches
    ADD COLUMN iteration_n INT,
    ADD COLUMN verdict      TEXT;

COMMENT ON COLUMN dispatcher.ralph_patches.iteration_n IS
    '1-based ralph iteration number at the time the row was written. NULL for legacy #3013 rows (pre-#3026 migration) and for SHIP rows (the SHIP DELETE-supersede path carries verdict=SHIP with iteration_n=NULL). Non-NULL for per-iteration intermediate rows. Issue #3026.';

COMMENT ON COLUMN dispatcher.ralph_patches.verdict IS
    'Ralph verdict when the row was captured: LOOP (iteration continuing), SHIP (terminal SHIP), ABORT (gave up), or NULL (legacy pre-#3026). Not CHECK-constrained so forward-version daemons can introduce new verdicts without a migration. Issue #3026.';


-- Down Migration
ALTER TABLE dispatcher.ralph_patches DROP COLUMN IF EXISTS verdict;
ALTER TABLE dispatcher.ralph_patches DROP COLUMN IF EXISTS iteration_n;
