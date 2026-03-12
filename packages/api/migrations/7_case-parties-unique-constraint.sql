-- Up Migration
-- Add UNIQUE constraint on case_parties(case_id, party_id, role) to prevent
-- duplicate party-case links during re-ingestion.  Fixes #873.

-- First, remove duplicate rows — keep the one with the smallest id per group.
DELETE FROM case_parties cp
WHERE cp.id NOT IN (
    SELECT MIN(id)
    FROM case_parties
    GROUP BY case_id, party_id, role
);

-- Add the unique constraint so ON CONFLICT DO NOTHING works on the business key.
ALTER TABLE case_parties
    ADD CONSTRAINT uq_case_parties_case_party_role UNIQUE (case_id, party_id, role);


-- Down Migration
ALTER TABLE case_parties DROP CONSTRAINT IF EXISTS uq_case_parties_case_party_role;
