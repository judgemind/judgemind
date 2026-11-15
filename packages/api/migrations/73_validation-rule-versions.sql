-- Up Migration
--
-- Record the rule versions that judged each deterministic validation row
-- (#5027).
--
-- A key whose rulings all fail deterministic validation is skipped: it has
-- no derived.documents row, only failed telemetry.validation_results rows,
-- and the raw-key reconciler counts it as explained.  When a rule is later
-- relaxed (#4793: hearing_date_in_range flags a structured-source date it
-- used to fail), the key would now be written, but nothing re-ingested it,
-- so its rulings stayed hidden for good.
--
-- Each rule has a version (validation.rule_versions.RULE_VERSIONS), bumped
-- on a relaxation.  The worker now writes, on its deterministic rows:
--
--   failed_rules   the rules that failed the ruling (empty on a flag row);
--   rule_versions  the version of every rule that judged it, e.g.
--                  {"hearing_date_in_range": 2, ...}.
--
-- scripts/reconcile_raw_keys.py re-ingests a skipped key whose failure was
-- judged by an older version of a rule that failed it (and not re-judged
-- since), through telemetry.ingest_incomplete with reason stale_validation
-- and its attempt cap.  Rows written before this migration carry neither
-- column; their rules are read from the reason text and their versions
-- from created_at (validation.rule_versions).
--
-- Schema notes
-- ------------
-- * Additive only.  telemetry.* is accumulated observability, not
--   rebuildable from S3: no backfill, no destructive change.
-- * Nullable, no DEFAULT: LLM-gate rows and older code paths write NULL.
-- * No index: the reconciler reads the failed rows once a day.
-- * The worker writes the columns in a savepoint and falls back to the old
--   INSERT while they do not exist, so it may deploy before this applies.

ALTER TABLE telemetry.validation_results
    ADD COLUMN IF NOT EXISTS failed_rules TEXT[],
    ADD COLUMN IF NOT EXISTS rule_versions JSONB;

COMMENT ON COLUMN telemetry.validation_results.failed_rules IS
    'Deterministic rules that failed the ruling (empty on a flag row). NULL '
    'for LLM-gate rows and rows written before migration 73. Issue #5027.';

COMMENT ON COLUMN telemetry.validation_results.rule_versions IS
    'Version of every deterministic rule that judged the row '
    '(validation.rule_versions.RULE_VERSIONS). A failure judged by an older '
    'version of a since-relaxed rule makes the skipped key eligible for '
    'reingest (scripts/reconcile_raw_keys.py). NULL before migration 73. '
    'Issue #5027.';

COMMENT ON TABLE telemetry.ingest_incomplete IS
    'One row per raw S3 key whose stored rows are not a complete split '
    '(single_document_fallback, partial_split, exhausted_child, dead_letter, '
    'reingest_failed), or whose recorded validation failure was judged by a '
    'since-relaxed rule (stale_validation, written by the reconciler, #5027). '
    'Deleted by a later complete write of the key. '
    'Re-ingested by scripts/reconcile_raw_keys.py up to its attempt cap. '
    'Issue #5008.';


-- Down Migration
COMMENT ON TABLE telemetry.ingest_incomplete IS
    'One row per raw S3 key whose stored rows are not a complete split '
    '(single_document_fallback, partial_split, exhausted_child, dead_letter, '
    'reingest_failed). Deleted by a later complete write of the key. '
    'Re-ingested by scripts/reconcile_raw_keys.py up to its attempt cap. '
    'Issue #5008.';

ALTER TABLE telemetry.validation_results
    DROP COLUMN IF EXISTS rule_versions,
    DROP COLUMN IF EXISTS failed_rules;
