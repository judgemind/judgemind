-- Up Migration
--
-- Document the empty_extraction and stale_skip reasons of
-- telemetry.ingest_incomplete, and the rule version on extraction skip
-- verdicts in telemetry.ingest_skips (#5094).
--
-- Two skip verdicts come from an extraction: no_ruling_text (every
-- multimodal row came back textless) and zero_rulings_no_text (no rulings
-- in a PDF with no text layer).  Either can be a degraded extraction (an
-- LLM answering with empty cells, OCR that read nothing), and either used
-- to replace a key's rulings and hide the key as skipped forever.  The
-- worker now refuses such a verdict on a key that holds rulings and records
-- empty_extraction instead.  Each such skip row carries the rule version
-- that produced it (detail.rule_version) and the extractor identity
-- (detail.extractor); the reconciler records stale_skip for a skip under
-- an older rule version, or one that replaced held rulings, and re-ingests
-- the key with --bust-llm-cache.
--
-- Comment only: reason is free TEXT and detail is JSONB, so no schema
-- change is needed.

COMMENT ON TABLE telemetry.ingest_incomplete IS
    'One row per raw S3 key whose stored rows are not a complete split '
    '(single_document_fallback, partial_split, exhausted_child, dead_letter, '
    'reingest_failed), whose split rejected every child while the key held '
    'rulings, so they were kept (all_children_rejected, #5057), whose '
    'extraction''s verdict of no rulings (no_ruling_text, zero_rulings_no_text) '
    'was refused because the key held rulings (empty_extraction, #5094), whose '
    'pre-split child was not written because its archived page could not be '
    'read (evidence_unavailable, #5012), whose '
    'recorded validation failure was judged by a since-relaxed rule '
    '(stale_validation, written by the reconciler, #5027), whose rows are '
    'all superseded and whose dedup ruling is gone (orphaned_superseded, '
    'written by the reconciler, #5093), or whose '
    'extraction skip verdict must be re-judged (stale_skip, written by the '
    'reconciler, #5094). '
    'Deleted by a later complete write of the key. '
    'Re-ingested by scripts/reconcile_raw_keys.py up to its attempt cap. '
    'Issue #5008.';

COMMENT ON TABLE telemetry.ingest_skips IS
    'One row per raw S3 key the ingestion worker deliberately wrote no '
    'document for, with the reason (governor_bio_route, '
    'santa_clara_placeholder, zero_rulings_no_text, no_extractable_text, '
    'no_ruling_text), or an incomplete key the reconciler closed out because '
    'its raw object is gone (raw_missing, #5093). '
    'The extraction verdicts (no_ruling_text, zero_rulings_no_text) carry '
    'detail.rule_version and detail.extractor, so the reconciler re-judges '
    'them when the rules change (#5094). '
    'Read by scripts/reconcile_raw_keys.py. Issue #5015.';


-- Down Migration
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

COMMENT ON TABLE telemetry.ingest_skips IS
    'One row per raw S3 key the ingestion worker deliberately wrote no '
    'document for, with the reason (governor_bio_route, '
    'santa_clara_placeholder, zero_rulings_no_text, no_extractable_text, '
    'no_ruling_text). '
    'Read by scripts/reconcile_raw_keys.py. Issue #5015.';
