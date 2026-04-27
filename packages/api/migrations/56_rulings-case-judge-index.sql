-- Up Migration: Add covering index on derived.rulings(case_id, judge_id)
--
-- The caseJudgesLoader fallback path (packages/api/src/graphql/dataloader.ts:84-91)
-- executes a query with DISTINCT ON (r.case_id, j.id) against derived.rulings to
-- fill in judge associations that are missing from derived.case_judges. Without this
-- index, Postgres must sort all rulings rows to satisfy the DISTINCT ON, which is
-- expensive on the hot cases-list page. Adding (case_id, judge_id) as a covering
-- index lets the planner use an index scan instead of a sort.
--
-- See: #3597

CREATE INDEX IF NOT EXISTS idx_rulings_case_judge
    ON derived.rulings (case_id, judge_id);


-- Down Migration

DROP INDEX IF EXISTS derived.idx_rulings_case_judge;
