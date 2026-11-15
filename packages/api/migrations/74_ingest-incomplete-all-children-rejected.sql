-- Up Migration
--
-- Document the all_children_rejected reason of telemetry.ingest_incomplete
-- (#5057).
--
-- A split that rejects every child (deterministic or LLM validation, the
-- all-NULL guard, a child that raised) of a key that holds rulings used to
-- replace the key's rows with nothing, logging only a warning: a reingest
-- deleted 106 federal rulings that way (#5026).  The worker now refuses
-- that write unless nothing says otherwise (a recorded skip verdict, or
-- --allow-ruling-removal on the reingest / rebuild), keeps the rows, and
-- records the key with reason all_children_rejected, so the raw-key
-- reconciler retries it under its attempt cap.
--
-- Comment only: reason is free TEXT, so no schema change is needed.

COMMENT ON TABLE telemetry.ingest_incomplete IS
    'One row per raw S3 key whose stored rows are not a complete split '
    '(single_document_fallback, partial_split, exhausted_child, dead_letter, '
    'reingest_failed), whose split rejected every child while the key held '
    'rulings, so they were kept (all_children_rejected, #5057), or whose '
    'recorded validation failure was judged by a since-relaxed rule '
    '(stale_validation, written by the reconciler, #5027). '
    'Deleted by a later complete write of the key. '
    'Re-ingested by scripts/reconcile_raw_keys.py up to its attempt cap. '
    'Issue #5008.';


-- Down Migration
COMMENT ON TABLE telemetry.ingest_incomplete IS
    'One row per raw S3 key whose stored rows are not a complete split '
    '(single_document_fallback, partial_split, exhausted_child, dead_letter, '
    'reingest_failed), or whose recorded validation failure was judged by a '
    'since-relaxed rule (stale_validation, written by the reconciler, #5027). '
    'Deleted by a later complete write of the key. '
    'Re-ingested by scripts/reconcile_raw_keys.py up to its attempt cap. '
    'Issue #5008.';
