-- Up Migration
-- Make rulings.hearing_date nullable so rulings are never silently dropped
-- when hearing_date cannot be extracted from the document (#2215).
-- A missing date is preferable to a missing ruling.
ALTER TABLE derived.rulings ALTER COLUMN hearing_date DROP NOT NULL;

-- Down Migration
-- Backfill any NULLs before restoring NOT NULL (pick epoch as placeholder).
UPDATE derived.rulings SET hearing_date = '1970-01-01' WHERE hearing_date IS NULL;
ALTER TABLE derived.rulings ALTER COLUMN hearing_date SET NOT NULL;
