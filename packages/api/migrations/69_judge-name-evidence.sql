-- Up Migration
--
-- Per-document judge-name evidence (issue #4928).
--
-- resolve_judge arbitration (packages/scraper-framework/src/ingestion/db.py,
-- _choose_canonical_spelling) picks a judge's canonical spelling from the
-- spellings documents printed for that judge. It used to count distinct
-- judge_aliases sources, but a source is a pipeline (live scraper, reingest,
-- rebuild, legacy 'scraper'), so a county has about four, and a spelling
-- printed on 250 documents scored the same as a typo read from one.
-- judge_aliases is unique on (judge_id, lower(raw_name), source), so it
-- cannot count documents.
--
-- This table holds one row per document: the judge the document resolved to
-- and the raw spelling it printed. Arbitration counts distinct active
-- documents per spelling. Re-processing a document replaces its row, so a
-- document is one vote however many pipelines read it.
--
-- Columns
-- -------
--   document_id  derived.documents.id of the document that printed the name.
--                No foreign key: resolve_judge runs before the document row
--                is written in the same transaction. Arbitration joins
--                derived.documents (status = 'active'), so rows of deleted
--                or superseded documents never count.
--   judge_id     the judge the document resolved to (cascade on delete).
--   raw_name     the spelling as the document printed it.
--
-- Seed
-- ----
-- Existing documents have no recorded spelling. Each document with a ruling
-- is seeded as a vote for its judge's current canonical name, so today's
-- names keep their support and a small reingest cannot rename a judge on a
-- handful of documents. Each re-processed document replaces its seeded vote
-- with the spelling it actually printed; a rebuild truncates derived.* and
-- records exact evidence from scratch.
--
-- derived.* is rebuildable from S3 (rebuild_db.py repopulates this table
-- through the ingestion worker).
--
-- Rollback
-- --------
-- DROP TABLE IF EXISTS derived.judge_name_evidence;
-- (Deploy the scraper-framework code that reads it only after this lands.)

CREATE TABLE IF NOT EXISTS derived.judge_name_evidence (
    document_id UUID PRIMARY KEY,
    judge_id    UUID NOT NULL REFERENCES derived.judges(id) ON DELETE CASCADE,
    raw_name    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_judge_name_evidence_judge_id
    ON derived.judge_name_evidence (judge_id);

COMMENT ON TABLE derived.judge_name_evidence IS
    'One row per document: the judge it resolved to and the spelling it '
    'printed. resolve_judge arbitration counts distinct active documents '
    'per spelling. Issue #4928.';

INSERT INTO derived.judge_name_evidence (document_id, judge_id, raw_name)
SELECT DISTINCT ON (r.document_id) r.document_id, r.judge_id, j.canonical_name
FROM derived.rulings r
JOIN derived.judges j ON j.id = r.judge_id
ORDER BY r.document_id, r.updated_at DESC, r.id
ON CONFLICT (document_id) DO NOTHING;


-- Down Migration
DROP TABLE IF EXISTS derived.judge_name_evidence;
