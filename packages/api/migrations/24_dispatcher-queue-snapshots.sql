-- Up Migration
--
-- Dispatcher v2 — Phase 2 shadow mode. Adds `dispatcher.queue_snapshots`,
-- a history table of observed `agent/ready` queue depths written by the
-- daemon on each scheduler tick (§6, 30s cadence).
--
-- Issue #2768 (Parent: #2725, Phase 1 epic; feeds the Phase 2 gate).
--
-- Design choice: snapshots (append-only history) rather than a singleton
-- "current queue state" row. Benefits:
--   - Clear 20-tick-count audit trail for the Phase 2 gate (daemon has not
--     crashed after ≥20 scheduler ticks is easier to prove with 20 rows).
--   - Easy to diff across ticks when diagnosing queue-depth changes.
--   - No race around "update the singleton" — concurrent writers would be
--     fine, but Phase 2 only has one writer, and an append table stays
--     correct under future concurrency.
--
-- GraphQL `dispatcherState.queueDepth` resolves `ORDER BY observed_at DESC
-- LIMIT 1` from this table (see `packages/api/src/graphql/dispatcher/resolvers.ts`).
--
-- TODO (post-Phase 2): daemon-side housekeeping tick that prunes old
-- snapshots (keep latest N or last hour). Not required for the Phase 2
-- gate — an idle daemon writes two snapshots per minute, so ~2880/day,
-- which is trivial storage for a ~12-byte row. Revisit if the daemon
-- stays at Phase 2 for >30 days without enabling pruning.

CREATE TABLE dispatcher.queue_snapshots (
    snapshot_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    queue_depth     INTEGER     NOT NULL,
    issue_numbers   INTEGER[]   NOT NULL,
    run_id          UUID        REFERENCES dispatcher.runs(run_id) ON DELETE CASCADE
);

CREATE INDEX idx_dispatcher_queue_snapshots_observed_at
    ON dispatcher.queue_snapshots (observed_at DESC);

COMMENT ON TABLE  dispatcher.queue_snapshots               IS 'Append-only history of observed agent/ready queue depths. Written by the daemon on each 30s scheduler tick (Phase 2+).';
COMMENT ON COLUMN dispatcher.queue_snapshots.issue_numbers IS 'Array of the observed issue numbers, ordered by priority as seen by the daemon at scan time.';
COMMENT ON COLUMN dispatcher.queue_snapshots.run_id        IS 'Owning daemon run. Snapshots cascade-delete with the run row so forensic queries after a crash stay clean.';


-- Down Migration
--
-- Wipes the snapshot history table and its index. Dispatcher resolvers
-- fall back to 0 when the table is gone (see `queryQueueDepth` —
-- `oneOrNone` + null-coalesce to 0), so the admin page stays functional
-- but reports 0 until the daemon re-lands the table on a forward migration.
DROP TABLE IF EXISTS dispatcher.queue_snapshots;
