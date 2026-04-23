-- Up Migration
--
-- Dispatcher daemon-side issue enrichment. Replaces the API's fallback
-- GitHub REST calls (``packages/api/src/graphql/dispatcher/github.ts``,
-- now deleted) with DB-persisted enriched issue metadata written by the
-- daemon on each scheduler tick.
--
-- Issue #2820. Context:
--   The daemon already runs ``gh issue list --json number,title,labels,
--   createdAt`` every 30s (migration 24); the current code discards
--   everything except ``number``. This migration makes the full JSON
--   durable so the admin-page resolvers can render real titles /
--   labels / ages from the DB without fan-out GitHub calls from the
--   API container. After this lands, the API's GitHub-call budget on
--   the hot path drops from ~4800 req/hr to zero.
--
-- Three changes:
--   1. ``dispatcher.queue_snapshots`` gains ``issues_json jsonb``
--      holding ``[{number, title, labels, createdAt}, ...]`` exactly as
--      observed at scan time. ``issue_numbers int[]`` is kept for
--      backward-compat + cheap ``COUNT(*)``-style queries and written
--      in lock-step.
--   2. New ``dispatcher.blocked_snapshots`` table mirrors
--      ``queue_snapshots`` but for the ``status/blocked`` list. Blocked
--      issues are not part of the agent/ready queue so they cannot
--      live in the same snapshot row; a separate cadence (every N
--      scheduler ticks) keeps the DB load negligible.
--   3. ``dispatcher.agents`` gains ``issue_title text`` so the
--      "recent completions" admin panel can render titles without
--      re-fetching from GitHub after the agent has completed (and the
--      issue may have moved out of the active queue).
--
-- Why append-only mirrors ``queue_snapshots``: Phase 2+ of the
-- dispatcher already committed to the snapshot-history pattern (20
-- rows buy you a clean audit trail; the daily housekeeping tick prunes
-- at 30 days). Blocked snapshots get the same pattern for free and the
-- admin page's single-latest-row read is still ``ORDER BY observed_at
-- DESC LIMIT 1``.

-- ── queue_snapshots.issues_json ──────────────────────────────────────
ALTER TABLE dispatcher.queue_snapshots
    ADD COLUMN issues_json jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN dispatcher.queue_snapshots.issues_json IS
    'Enriched issue metadata as observed at scan time: '
    '[{number, title, labels: [string], createdAt}, ...]. '
    'Populated by the daemon from the ``gh issue list --json '
    'number,title,labels,createdAt`` response. Source of truth for the '
    '/admin/dispatcher queue-ready panel (issue #2820). '
    'Kept in lock-step with ``issue_numbers`` — the two columns MUST '
    'describe the same issues in the same order.';

-- ── dispatcher.blocked_snapshots ─────────────────────────────────────
CREATE TABLE dispatcher.blocked_snapshots (
    snapshot_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    blocked_depth   INTEGER     NOT NULL,
    issue_numbers   INTEGER[]   NOT NULL,
    issues_json     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    run_id          UUID        REFERENCES dispatcher.runs(run_id) ON DELETE CASCADE
);

CREATE INDEX idx_dispatcher_blocked_snapshots_observed_at
    ON dispatcher.blocked_snapshots (observed_at DESC);

COMMENT ON TABLE  dispatcher.blocked_snapshots               IS 'Append-only history of observed ``status/blocked`` issues, one row per daemon snapshot tick. Written less frequently than queue_snapshots (blocked list changes slowly). Source of truth for the /admin/dispatcher blocked panel (issue #2820).';
COMMENT ON COLUMN dispatcher.blocked_snapshots.issue_numbers IS 'Issue numbers observed with label ``status/blocked`` and state=open.';
COMMENT ON COLUMN dispatcher.blocked_snapshots.issues_json   IS 'Enriched issue metadata: [{number, title, labels, createdAt, body}, ...]. ``body`` is included so the API can parse ``Blocked by #N`` lines without another GitHub call.';
COMMENT ON COLUMN dispatcher.blocked_snapshots.run_id        IS 'Owning daemon run. Cascade-deletes with the run row (same as queue_snapshots).';

-- ── dispatcher.agents.issue_title ────────────────────────────────────
ALTER TABLE dispatcher.agents
    ADD COLUMN issue_title text;

COMMENT ON COLUMN dispatcher.agents.issue_title IS
    'Issue title captured at claim time from the queue-snapshot '
    'enrichment. Lets the /admin/dispatcher recent-completions panel '
    'render a title without re-fetching from GitHub after the agent '
    'has completed and the issue may have left the active queue. '
    'Nullable — historical rows pre-dating issue #2820 have NULL and '
    'are not backfilled.';


-- Down Migration
--
-- Drops the new table and reverts the column adds. ``issues_json`` data
-- is unrecoverable but ``issue_numbers`` remains intact, so the queue-
-- depth metric keeps working. The API will fall back to "(title
-- unavailable)" rendering because the column is gone — but the
-- down-migration should only be run as part of an intentional rollback
-- that also reverts the API resolver code, so that's expected.
DROP TABLE IF EXISTS dispatcher.blocked_snapshots;

ALTER TABLE dispatcher.queue_snapshots
    DROP COLUMN IF EXISTS issues_json;

ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS issue_title;
