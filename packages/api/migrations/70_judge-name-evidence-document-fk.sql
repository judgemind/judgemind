-- Up Migration
--
-- judge_name_evidence rows follow their documents (issue #4966).
--
-- derived.judge_name_evidence (migration 69, #4928) holds one judge vote
-- per document. Votes were only ever upserted: a document that was deleted,
-- superseded, or whose ruling lost or changed its judge kept its row. The
-- ingestion worker now brings every touched document's row up to date in
-- the write's own transaction (ingestion.db.record_judge_votes), and only a
-- row whose document is active and whose ruling is on that judge counts
-- (ingestion.db._read_name_evidence).
--
-- 1. One-time cleanup: remove the rows that no longer describe their
--    document (no document, not active, no ruling, or a ruling on another
--    judge). On dev before this migration: 6 orphans, 10 rows of
--    superseded documents, 17 rows whose ruling has no judge or another
--    one. The judges those rows voted for are re-arbitrated the next time
--    one of their documents is written; reingesting a county does it for
--    all of them.
-- 2. A foreign key to derived.documents with ON DELETE CASCADE, so a
--    deleted document takes its vote with it. resolve_judge can still
--    record a vote before its document row is written in the same
--    transaction (resolve_judge(document_id=...)), so the constraint is
--    checked at commit (DEFERRABLE INITIALLY DEFERRED). The cascade itself
--    runs at once.
-- 3. Indexes on derived.documents(s3_key) and (previous_version_id): the
--    worker reads a key's votes by s3_key and the documents a dedup links
--    by previous_version_id on every write (record_judge_votes), and the
--    split-set write and stale-child cleanup already select by s3_key.
--
-- derived.* is rebuildable from S3; rebuild_db.py repopulates this table
-- through the ingestion worker.
--
-- Rollback
-- --------
-- ALTER TABLE derived.judge_name_evidence
--     DROP CONSTRAINT IF EXISTS judge_name_evidence_document_id_fkey;
-- DROP INDEX IF EXISTS derived.idx_documents_s3_key;
-- DROP INDEX IF EXISTS derived.idx_documents_previous_version_id;
-- (The removed rows were stale; they are not restored.)

DELETE FROM derived.judge_name_evidence e
WHERE NOT EXISTS (
    SELECT 1
    FROM derived.documents d
    JOIN derived.rulings r ON r.document_id = d.id
    WHERE d.id = e.document_id
      AND d.status = 'active'
      AND r.judge_id = e.judge_id
);

ALTER TABLE derived.judge_name_evidence
    DROP CONSTRAINT IF EXISTS judge_name_evidence_document_id_fkey;

ALTER TABLE derived.judge_name_evidence
    ADD CONSTRAINT judge_name_evidence_document_id_fkey
    FOREIGN KEY (document_id) REFERENCES derived.documents(id)
    ON DELETE CASCADE
    DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX IF NOT EXISTS idx_documents_s3_key
    ON derived.documents (s3_key);

CREATE INDEX IF NOT EXISTS idx_documents_previous_version_id
    ON derived.documents (previous_version_id)
    WHERE previous_version_id IS NOT NULL;

COMMENT ON COLUMN derived.judge_name_evidence.document_id IS
    'derived.documents.id of the document that printed the name. Cascades '
    'on delete; checked at commit. A row counts only while its document is '
    'active and its ruling is on judge_id. Issue #4966.';


-- Down Migration
ALTER TABLE derived.judge_name_evidence
    DROP CONSTRAINT IF EXISTS judge_name_evidence_document_id_fkey;
DROP INDEX IF EXISTS derived.idx_documents_s3_key;
DROP INDEX IF EXISTS derived.idx_documents_previous_version_id;
