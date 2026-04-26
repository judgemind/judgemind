-- Up Migration
--
-- Source-authority arbitration for judge name resolution (issue #3484).
--
-- Adds a unique index on derived.judge_aliases (judge_id, lower(raw_name), source)
-- so that per-source aliases dedupe properly while preserving the distinct-source
-- signal needed for source-authority arbitration in resolve_judge.
--
-- Background
-- ----------
-- Prior to this migration, judge_aliases used ON CONFLICT DO NOTHING on the
-- table's primary key (id). Multiple rows could exist for the same judge, raw
-- name, and source, which made counting distinct non-roster sources unreliable
-- (duplicates inflated the count). The new partial unique index enforces one
-- alias row per (judge_id, case-insensitive raw_name, source), preventing
-- duplicate writes and making the distinct-source count authoritative.
--
-- Idempotent rebuild safety
-- -------------------------
-- The index has no defaulted rows to backfill. Multi-source aliases are
-- net-new behavior introduced by the source= kwarg on resolve_judge. Running
-- rebuild_db.py after this migration runs the fixed pipeline which writes
-- properly-deduped aliases.
--
-- Rollback
-- --------
-- DROP INDEX IF EXISTS derived.idx_judge_aliases_judge_raw_source_uniq;
-- (The ON CONFLICT clauses in db.py fall back gracefully to DO NOTHING.)

CREATE UNIQUE INDEX IF NOT EXISTS idx_judge_aliases_judge_raw_source_uniq
    ON derived.judge_aliases (judge_id, lower(raw_name), source)
    WHERE source IS NOT NULL;


-- Down Migration
DROP INDEX IF EXISTS derived.idx_judge_aliases_judge_raw_source_uniq;
