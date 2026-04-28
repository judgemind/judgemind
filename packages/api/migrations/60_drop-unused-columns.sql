-- Up Migration
--
-- Audit cleanup: drop three never-populated nullable columns from derived.*
-- tables and document the forward-specced ai_budget_daily column in
-- public.users. Issue #3603.
--
-- Rationale
-- ---------
-- derived.judges.bio_reviewed_at    (TIMESTAMPTZ) — no bio-review workflow
--   exists; 0/709 rows populated (spotcheck 2026-04-27). Safe to drop:
--   derived.* is fully rebuildable from S3.
-- derived.cases.case_subtype        (TEXT)        — no subtype concept used
--   anywhere; 0/13124 rows populated. Same rebuild safety.
-- derived.case_attorneys.withdrew_at (DATE)       — no withdrawal-tracking
--   flow. Same rebuild safety.
-- public.users.ai_budget_daily      (INTEGER)     — kept. Architecture spec
--   §3.4.4 forward-specs AI cost caps. Column is in public.* (authoritative,
--   not rebuildable). A COMMENT documents intent and prevents future /audit
--   re-flagging.
--
-- All DROPs are idempotent (IF EXISTS). Migration 1 is left intact as a
-- historical record, per the precedent set by migration 23.

ALTER TABLE derived.judges DROP COLUMN IF EXISTS bio_reviewed_at;
ALTER TABLE derived.cases DROP COLUMN IF EXISTS case_subtype;
ALTER TABLE derived.case_attorneys DROP COLUMN IF EXISTS withdrew_at;

COMMENT ON COLUMN public.users.ai_budget_daily IS 'Forward-specced for AI feature cost caps per docs/specs/architecture-spec-v1.md §3.4.4. Currently 0%-populated and unused; remove this comment when the AI budget feature ships.';


-- Down Migration
--
-- Re-add the three dropped columns with their original types and reset the
-- COMMENT on public.users.ai_budget_daily. Columns are re-added empty —
-- a true data rollback requires rebuild_db.py. Follows migration 23 precedent.

ALTER TABLE derived.judges ADD COLUMN IF NOT EXISTS bio_reviewed_at TIMESTAMPTZ;
ALTER TABLE derived.cases ADD COLUMN IF NOT EXISTS case_subtype TEXT;
ALTER TABLE derived.case_attorneys ADD COLUMN IF NOT EXISTS withdrew_at DATE;

COMMENT ON COLUMN public.users.ai_budget_daily IS NULL;
