-- Up Migration: Add index on cases.case_type for filter queries
--
-- PRs #1995, #1997, and #2020 added caseType filtering to the cases list,
-- rulings list, and search page. These queries filter on cases.case_type via
-- WHERE clauses, but no index exists. Other filter columns (rulings.outcome,
-- rulings.motion_type, cases.case_status) already have indexes.
--
-- ~10k rows — no need for CONCURRENTLY; runs in the migration transaction.
--
-- See: #2025

CREATE INDEX IF NOT EXISTS idx_cases_case_type
    ON cases(case_type);


-- Down Migration

DROP INDEX IF EXISTS idx_cases_case_type;
