-- Up Migration
-- Widen courts.state from CHAR(2) to VARCHAR(20) to support non-state
-- jurisdictions (e.g. "Federal" for CourtListener federal court opinions).
-- The CHAR(2) constraint caused all federal court documents to be dead-
-- lettered by the ingestion worker with "value too long for type character(2)".
-- Fixes #2219.

ALTER TABLE derived.courts ALTER COLUMN state TYPE VARCHAR(20);

-- Down Migration
-- Note: this will fail if any state values exceed 2 characters.
ALTER TABLE derived.courts ALTER COLUMN state TYPE CHAR(2);
