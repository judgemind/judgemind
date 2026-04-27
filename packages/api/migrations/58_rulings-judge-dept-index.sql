-- Up Migration: Add compound index on rulings (judge_id, department) for assignment queries
--
-- getJudgeAssignments and getJudgeAssignmentsBatch (judge-assignments.ts) group and
-- filter on (judge_id, department). The prior correlated-subquery rewrite in #3595
-- replaced per-row subqueries with a CTE+ROW_NUMBER() plan; this index allows Postgres
-- to satisfy both the dept_dates and case_type_ranked CTEs with a single index scan
-- instead of a sequential scan on the full rulings table.
--
-- Partial index (WHERE department IS NOT NULL) matches the query predicate exactly,
-- reducing index size and keeping updates cheap. Naming follows idx_rulings_judge_motion
-- and idx_rulings_judge_outcome conventions.
--
-- ~100k+ rows on dev — no need for CONCURRENTLY; IF NOT EXISTS keeps it idempotent.
--
-- See: #3595

CREATE INDEX IF NOT EXISTS idx_rulings_judge_dept
    ON derived.rulings (judge_id, department) WHERE department IS NOT NULL;


-- Down Migration

DROP INDEX IF EXISTS derived.idx_rulings_judge_dept;
