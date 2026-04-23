-- Up Migration: Create validation_results table for ingestion validation gate
--
-- Logs the outcome of LLM-based validation checks that run between enrichment
-- and DB write in the ingestion worker. Each document gets a pass/flag/fail
-- result. Flagged and failed items auto-file GitHub issues for review.
--
-- See: #1803, #1801 (parent epic)

CREATE TABLE IF NOT EXISTS validation_results (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID        NOT NULL,
    ruling_id       UUID,
    result          TEXT        NOT NULL CHECK (result IN ('pass', 'flag', 'fail', 'error')),
    reason          TEXT,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index on result for querying flagged/failed items efficiently.
CREATE INDEX IF NOT EXISTS idx_validation_results_result
    ON validation_results(result);

-- Index on document_id for looking up validation history per document.
CREATE INDEX IF NOT EXISTS idx_validation_results_document_id
    ON validation_results(document_id);

COMMENT ON TABLE  validation_results              IS 'LLM validation outcomes for ingested documents. One row per validation check.';
COMMENT ON COLUMN validation_results.result       IS 'Validation outcome: pass (write normally), flag (write + review), fail (skip write), error (LLM call failed).';
COMMENT ON COLUMN validation_results.reason       IS 'Human-readable reason for flag/fail results. NULL for pass results.';
COMMENT ON COLUMN validation_results.model        IS 'LLM model used for validation (e.g. claude-haiku-4-5-20251001).';
COMMENT ON COLUMN validation_results.latency_ms   IS 'Wall-clock time for the validation LLM call in milliseconds.';
