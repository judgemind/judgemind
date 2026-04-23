-- Up Migration
-- Add UNIQUE constraint on case_parties(case_id, party_id, role) to prevent
-- duplicate party-case links during re-ingestion.  Fixes #873.

-- First, remove duplicate rows — keep one per (case_id, party_id, role) group.
-- Uses DISTINCT ON instead of MIN(uuid) which is not supported in PostgreSQL.
DELETE FROM case_parties
WHERE id NOT IN (
    SELECT DISTINCT ON (case_id, party_id, role) id
    FROM case_parties
    ORDER BY case_id, party_id, role, id
);

-- Add the unique constraint so ON CONFLICT DO NOTHING works on the business key.
ALTER TABLE case_parties
    ADD CONSTRAINT uq_case_parties_case_party_role UNIQUE (case_id, party_id, role);


-- Down Migration
ALTER TABLE case_parties DROP CONSTRAINT IF EXISTS uq_case_parties_case_party_role;
