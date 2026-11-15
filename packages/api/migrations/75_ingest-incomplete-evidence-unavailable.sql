-- Up Migration
--
-- Document the evidence_unavailable reason of telemetry.ingest_incomplete
-- (#5012).
--
-- A scraper pre-split child (Contra Costa, Los Angeles) carries its LLM's
-- case number, checked against the page the scraper archived (#4976).  When
-- that page could not be read from S3, the child used to be written with
-- its LLM number trusted.  The worker now writes nothing for it and, for a
-- read error a retry cannot fix (a missing key, access denied) or any read
-- error on a split-set replacement, records the key with reason
-- evidence_unavailable, so the raw-key reconciler retries it under its
-- attempt cap.
--
-- Comment only: reason is free TEXT, so no schema change is needed.

COMMENT ON TABLE telemetry.ingest_incomplete IS
    'One row per raw S3 key whose stored rows are not a complete split '
    '(single_document_fallback, partial_split, exhausted_child, dead_letter, '
    'reingest_failed), whose split rejected every child while the key held '
    'rulings, so they were kept (all_children_rejected, #5057), whose '
    'pre-split child was not written because its archived page could not be '
    'read (evidence_unavailable, #5012), or whose '
    'recorded validation failure was judged by a since-relaxed rule '
    '(stale_validation, written by the reconciler, #5027). '
    'Deleted by a later complete write of the key. '
    'Re-ingested by scripts/reconcile_raw_keys.py up to its attempt cap. '
    'Issue #5008.';


-- Down Migration
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
