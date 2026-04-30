-- Up Migration
--
-- Rulings may not reference non-active documents (#3728).
--
-- Context
-- -------
-- When dedup-supersede runs, it marks the losing document as 'superseded' but
-- leaves any rulings pointing at it in place. This creates "orphan rulings" —
-- ruling rows that reference a non-active document. The resolver fix (also in
-- this PR) adds a JOIN + status filter to exclude them from the GraphQL list,
-- but the trigger here provides a data-layer guard so future ingestion cannot
-- create new orphans.
--
-- Design notes
-- ------------
-- - BEFORE INSERT OR UPDATE OF document_id so the trigger fires on both new
--   rows and on reassignment of document_id (defensive: reassignment is not
--   currently done but costs nothing to guard).
-- - Uses STABLE + LANGUAGE plpgsql for the lookup function.
-- - Uses errcode '23514' (check_violation) for consistent pg error handling.
-- - Migration is idempotent: CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS
--   then CREATE TRIGGER.

CREATE OR REPLACE FUNCTION derived.rulings_block_non_active_document()
RETURNS trigger
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    doc_status text;
BEGIN
    SELECT status INTO doc_status
    FROM derived.documents
    WHERE id = NEW.document_id;

    IF doc_status IS NULL THEN
        RAISE EXCEPTION
            'cannot insert/update ruling: document % not found',
            NEW.document_id
            USING ERRCODE = '23514';
    END IF;

    IF doc_status != 'active' THEN
        RAISE EXCEPTION
            'cannot insert/update ruling: document % has status %, expected active',
            NEW.document_id,
            doc_status
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_rulings_block_non_active_document ON derived.rulings;

CREATE TRIGGER trg_rulings_block_non_active_document
    BEFORE INSERT OR UPDATE OF document_id
    ON derived.rulings
    FOR EACH ROW
    EXECUTE FUNCTION derived.rulings_block_non_active_document();


-- Down Migration
--
-- Remove the trigger and function added by the Up migration.
-- This restores the pre-#3728 posture where rulings can reference any document.

DROP TRIGGER IF EXISTS trg_rulings_block_non_active_document ON derived.rulings;
DROP FUNCTION IF EXISTS derived.rulings_block_non_active_document();
