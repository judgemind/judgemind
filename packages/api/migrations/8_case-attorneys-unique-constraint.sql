-- Up Migration
-- Add UNIQUE constraint on case_attorneys(case_id, attorney_id, role) to prevent
-- duplicate attorney-case links during re-ingestion.  Fixes #880.

-- First, remove duplicate rows — keep one per (case_id, attorney_id, role) group.
-- Uses DISTINCT ON instead of MIN(uuid) which is not supported in PostgreSQL.
DELETE FROM case_attorneys
WHERE id NOT IN (
    SELECT DISTINCT ON (case_id, attorney_id, role) id
    FROM case_attorneys
    ORDER BY case_id, attorney_id, role, id
);

-- Add the unique constraint so ON CONFLICT DO NOTHING works on the business key.
ALTER TABLE case_attorneys
    ADD CONSTRAINT uq_case_attorneys_case_attorney_role UNIQUE (case_id, attorney_id, role);


-- Down Migration
ALTER TABLE case_attorneys DROP CONSTRAINT IF EXISTS uq_case_attorneys_case_attorney_role;
