-- Up Migration
--
-- Dispatcher v3 buildout — schema foundation for v2/v3 cohabitation.
-- Issue #3872.
--
-- Why
-- ---
-- v3 will cohabit with v2 in parallel during the rollout: both daemons
-- read/write the same ``dispatcher.*`` tables, and per-row ownership is
-- established via ``parent_run_id`` → ``runs.dispatcher_version``. v2
-- needs to start writing ``dispatcher_version='v2'`` BEFORE v3's
-- launcher boots so when v3 inserts its own ``runs`` row, every
-- pre-existing row is correctly tagged as v2-owned. Same for
-- ``terminal_outcomes.parent_run_id`` so each daemon's circuit breaker
-- can scope its rolling-window scan to outcomes from its own run
-- lineage. The v3-only columns on ``dispatcher.agents`` land now so
-- v3 can write them once it deploys; v2 ignores unknown columns.
--
-- Three additive changes (no destructive migration; v2 keeps running
-- against the existing schema with the defaults):
--
--   1. ``dispatcher.runs.dispatcher_version text NOT NULL DEFAULT 'v2'``
--      The DEFAULT backfills every existing row to ``'v2'`` at ALTER
--      time (Postgres rewrites the table for a NOT NULL ADD COLUMN
--      without a defaulted volatile expression — the literal default
--      writes once, fast). New v2 inserts continue to write the
--      column either implicitly (via DEFAULT) or explicitly (the
--      daemon's INSERT is updated in this same PR to make the value
--      observable in psycopg-3 logging without depending on the
--      DEFAULT firing). v3's launcher will INSERT
--      ``dispatcher_version='v3'`` and the cohabitation predicate
--      becomes a single equality check.
--
--   2. ``dispatcher.terminal_outcomes.parent_run_id uuid REFERENCES
--      dispatcher.runs(run_id)`` — nullable, no DEFAULT. Backfilled
--      from ``dispatcher.agents.parent_run_id`` via JOIN where the
--      outcome's ``agent_id`` matches a known agent row. Rows whose
--      ``agent_id`` is NULL (synthetic test inputs per migration 29)
--      or whose agent row was hard-deleted leave ``parent_run_id``
--      NULL — v2's circuit breaker tolerates NULL today (it scans by
--      ``ended_at`` only; #2860). v3's per-run scope filter will read
--      ``WHERE parent_run_id = current_run_id`` against rows it wrote
--      itself, so historical NULLs are fine.
--
--      The daemon's ``_write_terminal_outcome`` is updated in this same
--      PR to look up and write ``parent_run_id`` alongside the existing
--      ``issue_number`` lookup so all NEW outcome rows carry the value.
--
--   3. Six v3-only columns on ``dispatcher.agents`` — all nullable,
--      no DEFAULT, no index. v2 ignores them; v3 writes them once it
--      deploys. The column choice mirrors the v3 spec's per-agent
--      milestone-tracking surface (replaces v2's coarser
--      ``phase``/``current_milestone_*`` split with explicit
--      timestamped milestones the admin cockpit can render):
--
--        current_milestone        TEXT         — current pipeline milestone label (e.g. 'plan_complete', 'ralph_iter_2')
--        current_milestone_detail TEXT         — free-form supplemental detail
--        current_milestone_at     TIMESTAMPTZ  — when the milestone was entered
--        session_s3_key           TEXT         — S3 key for the agent's transcript
--        diagnoser_arn            TEXT         — ECS task ARN of the running diagnoser (when active)
--        outcome_summary          TEXT         — one-line outcome description for the cockpit
--
-- Design notes
-- ------------
-- - All three changes are additive. v2 code keeps running against the
--   schema unchanged: no v2 read path queries the new columns, and
--   v2's INSERTs into ``runs`` and ``terminal_outcomes`` either rely
--   on the column DEFAULT (runs) or get an explicit value via the
--   daemon update bundled in this PR (terminal_outcomes).
-- - ``ADD COLUMN IF NOT EXISTS`` everywhere so the migration is
--   idempotent (mirrors migration 35's pattern).
-- - No CHECK constraint on ``runs.dispatcher_version``. The set is
--   daemon-code owned (today: ``'v2'``, soon: ``'v3'``); a CHECK
--   would couple schema to that enum and require a follow-up
--   migration when v4 lands.
-- - The FK on ``terminal_outcomes.parent_run_id`` uses no ON DELETE
--   action (default NO ACTION) — ``dispatcher.runs`` is append-only
--   in production (lease history is forensic), so a cascade is moot
--   and NO ACTION surfaces a bug if a delete ever sneaks in.
-- - No index on the new columns. Reads scope to a small recent
--   window already covered by ``idx_dispatcher_terminal_outcomes_ended_at``;
--   adding a (parent_run_id, ended_at) composite is a Phase-2
--   optimization that lands when v3 actually needs it.
--
-- Backfill
-- --------
-- Two UPDATEs:
--
--   1. ``dispatcher.runs.dispatcher_version`` — handled by the
--      ``DEFAULT 'v2'`` on the ALTER itself. Postgres applies the
--      default to every existing row at column-add time, so no
--      separate UPDATE is needed. (Verified: the DEFAULT is a
--      literal, not a volatile expression, so the rewrite is a
--      single fast pass.)
--
--   2. ``dispatcher.terminal_outcomes.parent_run_id`` — backfilled
--      from ``dispatcher.agents.parent_run_id`` via JOIN. Idempotent
--      (guarded on the new column being NULL) so re-runs do not
--      clobber values written by the daemon between migration apply
--      and the next deploy.

ALTER TABLE dispatcher.runs
    ADD COLUMN IF NOT EXISTS dispatcher_version TEXT NOT NULL DEFAULT 'v2';

COMMENT ON COLUMN dispatcher.runs.dispatcher_version IS
    'Which dispatcher daemon owns this run: ''v2'' (the original '
    'subprocess + ECS-launcher daemon) or ''v3'' (the cohabiting '
    'launcher being rolled out under #3872 and follow-ups). The '
    'DEFAULT ''v2'' backfills every pre-#3872 row at migration time. '
    'Per-row ownership for downstream tables (agents, '
    'terminal_outcomes) flows through ``parent_run_id``: each daemon '
    'reads only rows whose run lineage matches its own version. No '
    'CHECK constraint — the set is daemon-code owned. Issue #3872.';

ALTER TABLE dispatcher.terminal_outcomes
    ADD COLUMN IF NOT EXISTS parent_run_id UUID
        REFERENCES dispatcher.runs(run_id);

COMMENT ON COLUMN dispatcher.terminal_outcomes.parent_run_id IS
    'The ``dispatcher.runs.run_id`` of the daemon whose terminal '
    'transition produced this outcome. Backfilled from '
    '``dispatcher.agents.parent_run_id`` JOIN on ``agent_id``. NULL '
    'for rows whose agent_id is NULL (synthetic test input, per '
    'migration 29) or whose agent row was hard-deleted. v2''s '
    'circuit breaker ignores this column (it scans by ended_at '
    'only, #2860); v3 will scope its rolling-window scan to its own '
    'run lineage via ``WHERE parent_run_id = current_run_id``. '
    'Issue #3872.';

ALTER TABLE dispatcher.agents
    ADD COLUMN IF NOT EXISTS current_milestone        TEXT,
    ADD COLUMN IF NOT EXISTS current_milestone_detail TEXT,
    ADD COLUMN IF NOT EXISTS current_milestone_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS session_s3_key           TEXT,
    ADD COLUMN IF NOT EXISTS diagnoser_arn            TEXT,
    ADD COLUMN IF NOT EXISTS outcome_summary          TEXT;

COMMENT ON COLUMN dispatcher.agents.current_milestone IS
    'v3-only — current pipeline milestone label written by the v3 '
    'agent runtime (e.g. ''plan_complete'', ''ralph_iter_2'', '
    '''pr_opened''). v2 ignores this column. Paired with '
    '``current_milestone_at`` for cockpit timing display. NULL on '
    'every v2-written row. Issue #3872.';

COMMENT ON COLUMN dispatcher.agents.current_milestone_detail IS
    'v3-only — free-form supplemental detail for the current '
    'milestone (e.g. failing test name, retry attempt count). NULL '
    'on every v2-written row. Issue #3872.';

COMMENT ON COLUMN dispatcher.agents.current_milestone_at IS
    'v3-only — timestamp the current milestone was entered. NULL on '
    'every v2-written row. Issue #3872.';

COMMENT ON COLUMN dispatcher.agents.session_s3_key IS
    'v3-only — S3 key of the agent''s transcript bundle. NULL on '
    'every v2-written row. Issue #3872.';

COMMENT ON COLUMN dispatcher.agents.diagnoser_arn IS
    'v3-only — ECS task ARN of the running diagnoser when an '
    'async-spawned diagnoser is in flight for this agent. NULL when '
    'no diagnoser is active and on every v2-written row. Issue #3872.';

COMMENT ON COLUMN dispatcher.agents.outcome_summary IS
    'v3-only — one-line outcome description rendered in the admin '
    'cockpit (succeeds v2''s ``failure_summary`` for both success '
    'and failure paths). NULL on every v2-written row. Issue #3872.';

-- Backfill ``terminal_outcomes.parent_run_id`` from ``agents``.
-- Guarded on the new column being NULL so re-applying the migration
-- (or applying after the daemon has started writing the column on
-- new rows) does not clobber more-precise values.
UPDATE dispatcher.terminal_outcomes t
   SET parent_run_id = a.parent_run_id
  FROM dispatcher.agents a
 WHERE t.agent_id = a.agent_id
   AND t.parent_run_id IS NULL;


-- Down Migration
--
-- Drop all eight added columns. Down on production is unlikely (this
-- is foundational infra for v3) but provided for local-dev rollback
-- during the buildout. The order matters: ``terminal_outcomes.parent_run_id``
-- has an FK to ``runs(run_id)`` but dropping the column drops the FK
-- automatically, so no separate constraint-drop is needed.

ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS current_milestone,
    DROP COLUMN IF EXISTS current_milestone_detail,
    DROP COLUMN IF EXISTS current_milestone_at,
    DROP COLUMN IF EXISTS session_s3_key,
    DROP COLUMN IF EXISTS diagnoser_arn,
    DROP COLUMN IF EXISTS outcome_summary;

ALTER TABLE dispatcher.terminal_outcomes
    DROP COLUMN IF EXISTS parent_run_id;

ALTER TABLE dispatcher.runs
    DROP COLUMN IF EXISTS dispatcher_version;
