-- Up Migration
-- Add missing indexes for columns used in WHERE clauses, JOINs, and keyset
-- pagination across the API query resolvers.
--
-- See: https://github.com/judgemind/judgemind/issues/481

-- Compound index for keyset pagination in the rulings query resolver:
--   ORDER BY r.hearing_date DESC, r.id DESC
--   WHERE (r.hearing_date, r.id) < ($N, $N)
-- The existing idx_rulings_hearing_date (single-column) cannot satisfy the
-- compound sort or the row-value comparison efficiently.
CREATE INDEX IF NOT EXISTS idx_rulings_keyset_hearing
    ON rulings (hearing_date DESC, id DESC);

-- courts.county — filtered via JOIN in the rulings query when county is specified
CREATE INDEX IF NOT EXISTS idx_courts_county
    ON courts (county);

-- cases.case_number — filtered via JOIN in the rulings query when caseNumber is specified.
-- The existing idx_cases_number_norm covers case_number_normalized, not case_number.
CREATE INDEX IF NOT EXISTS idx_cases_case_number
    ON cases (case_number);

-- Compound index for keyset pagination in the cases query resolver:
--   ORDER BY created_at DESC, id DESC
--   WHERE (created_at, id) < ($N, $N)
CREATE INDEX IF NOT EXISTS idx_cases_keyset_created
    ON cases (created_at DESC, id DESC);


-- Down Migration

DROP INDEX IF EXISTS idx_cases_keyset_created;
DROP INDEX IF EXISTS idx_cases_case_number;
DROP INDEX IF EXISTS idx_courts_county;
DROP INDEX IF EXISTS idx_rulings_keyset_hearing;
