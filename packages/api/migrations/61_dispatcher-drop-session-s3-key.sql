-- Up Migration
--
-- Dispatcher v3 — drop the unused ``dispatcher.agents.session_s3_key``
-- column. Issue #3977 (spec + schema cleanup from audit findings,
-- Item 3).
--
-- Why
-- ---
-- Migration 56 added ``session_s3_key`` intending it to be written by
-- ``scripts/dispatcher_v3/agent_runner.py`` on session-log upload. The
-- implementation never wires it: the runner uploads the session
-- transcript to a deterministic S3 path
-- (``s3://<SESSIONS_BUCKET>/<AGENT_ID>.jsonl``) and the diagnoser builds
-- the key from ``AGENT_ID`` directly without reading the column. The
-- column has been NULL on every v3-written row since A1 (PR #3893)
-- shipped. It's vestigial design hubris from the spec; dropping it
-- removes a misleading shape from the schema.
--
-- If a future renaming-of-paths reason ever emerges (e.g. moving to a
-- per-environment prefix), the column can be re-added in a forward
-- migration. Today it's dead surface, and the deterministic path
-- pattern is simpler to reason about.
--
-- Design notes
-- ------------
-- * ``DROP COLUMN IF EXISTS`` makes the migration idempotent — re-runs
--   are a no-op, and the column may already be absent on a freshly
--   regenerated dev DB.
-- * No data backfill or migration is needed: the column is NULL on
--   every row, so no values are lost.
-- * Migration 56's down section already includes
--   ``DROP COLUMN IF EXISTS session_s3_key`` (alongside the other v3
--   cohabitation columns); this migration is the forward-only drop.
--   Migration 56 stays intact as a historical record — applying it
--   from a fresh DB still adds the column, which is then dropped here.
-- * ``.claude/skills/diagnose-failure/SKILL.md`` is updated in the same
--   PR to remove ``session_s3_key`` from its agents-row SELECT.
-- * ``docs/specs/dispatcher-v3-spec.md`` §5 state model is updated in
--   the same PR to remove the column from the agents-table sketch.
--
-- Verify (per the issue's AC)
-- ---------------------------
--   ``scripts/dev-db-query.sh "SELECT column_name FROM information_schema.columns
--   WHERE table_schema='dispatcher' AND table_name='agents' AND
--   column_name='session_s3_key'"`` returns 0 rows.

ALTER TABLE dispatcher.agents DROP COLUMN IF EXISTS session_s3_key;


-- Down Migration
--
-- Re-add the column, NULL on every row. Anything that wrote to the
-- column previously (nothing — the writer was never wired) is
-- recoverable from S3 directly via ``s3://<SESSIONS_BUCKET>/<AGENT_ID>.jsonl``,
-- so the down migration is non-destructive in effect.

ALTER TABLE dispatcher.agents ADD COLUMN IF NOT EXISTS session_s3_key text;

COMMENT ON COLUMN dispatcher.agents.session_s3_key IS
    'v3-only — S3 key of the agent''s transcript bundle. NULL on every '
    'v2-written row. Issue #3872. Dropped in migration 61 (#3977) — this '
    'down section restores the column for symmetry but the writer was '
    'never wired so the value is always NULL.';
