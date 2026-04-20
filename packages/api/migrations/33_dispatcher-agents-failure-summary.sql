-- Up Migration
--
-- Dispatcher v2 — add ``failure_summary TEXT`` column to
-- ``dispatcher.agents`` so the admin "Recently completed" panel can
-- surface a one-line "what happened" string on hover over the outcome
-- glyph for failed / crashed / plan_blocked rows (#2900).
--
-- Context
-- -------
-- Today the diagnostic signal for a terminal failure is scattered
-- across three tables: ``dispatcher.failures.category`` (enum, coarse),
-- ``dispatcher.failures.details`` (per-category JSONB), and
-- ``dispatcher.diagnoses.recommendation.reasoning`` (LLM prose, ONLY
-- populated for tier-2/3 failures that triggered ``/diagnose-failure``).
-- There is no single place on the agents row the admin panel can read
-- to render "ralph crashed at iteration 3 (subprocess_turn_limit):
-- reviewer exceeded max turns" without a cross-table join.
--
-- The daemon's ``_mark_agent_terminal`` will populate this column at
-- two points (issue #2900 fix):
--   1. Deterministically at terminal-time with a template built from
--      ``failures.category + details`` + ``phase_outputs.phase + log_text``.
--   2. When ``/diagnose-failure`` completes, UPDATE with the first 1-3
--      sentences of ``diagnoses.recommendation.reasoning`` so tier-2/3
--      failures upgrade to an LLM-authored summary.
--
-- Design notes
-- ------------
-- - ``TEXT`` (not ``varchar(N)``) matching the repo convention — the
--   daemon caps at 240 chars at write-time; a DB cap would turn an
--   operational cap into a hard failure the daemon has to handle.
-- - Nullable with no default — historical rows stay NULL (the UI
--   renders the glyph unchanged), succeeded rows also stay NULL
--   (there is nothing to summarize), failed/crashed/plan_blocked rows
--   get populated at terminal-time going forward.
-- - No CHECK constraint — the template may evolve; tightening the
--   column to a regex would couple schema to daemon code.
-- - No index — reads are always keyed by agent_id/ended_at which are
--   covered elsewhere; the column is a read-through, never searched.
-- - ``ADD COLUMN IF NOT EXISTS`` so the migration is idempotent and
--   can re-run safely. This matches the issue's verify line ("Migration
--   is idempotent (uses IF NOT EXISTS) so it can re-run").

ALTER TABLE dispatcher.agents
    ADD COLUMN IF NOT EXISTS failure_summary TEXT;

COMMENT ON COLUMN dispatcher.agents.failure_summary IS
    'One-line "what happened" string for failed / crashed / plan_blocked terminals. Populated by _mark_agent_terminal at terminal-time from failures.category + phase + exit_code + stderr tail (<=240 chars), then optionally upgraded by /diagnose-failure to the first 1-3 sentences of recommendation.reasoning. NULL for succeeded / needs_review rows and historical pre-#2900 rows. Rendered as a tooltip on the outcome glyph in the /admin/dispatcher Recently completed panel. Issue #2900.';


-- Down Migration
ALTER TABLE dispatcher.agents DROP COLUMN IF EXISTS failure_summary;
