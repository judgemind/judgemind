/**
 * Judge department assignment history — derives department assignments
 * from ruling timestamps grouped by department.
 *
 * Each assignment represents a time period when a judge was sitting in a
 * particular department. The most recent assignment is the "current" one.
 */

import type { Pool } from 'pg';
import { lookupCourthouse } from '../reference-data/courthouses';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface JudgeAssignment {
  department: string;
  caseType: string | null;
  courthouse: string | null;
  firstSeen: string;
  lastSeen: string;
}

// ---------------------------------------------------------------------------
// SQL query
// ---------------------------------------------------------------------------

/**
 * Derive department assignments for a judge from their rulings.
 *
 * Groups rulings by department, computing the date range for each.
 * Determines case type from the most common case_type among cases
 * associated with rulings in that department.
 * Ordered by most recent last_seen first (current assignment first).
 *
 * NOTE: Uses simple GROUP BY department, which merges non-contiguous
 * assignments to the same department. This is acceptable for V1 since
 * CA judge re-assignments to the same department are extremely rare.
 * TODO(perf): If re-assignment tracking becomes important, rewrite
 * with a gaps-and-islands query using LAG() window functions.
 */
export async function getJudgeAssignments(
  pool: Pool,
  judgeId: string,
  county: string | null,
): Promise<JudgeAssignment[]> {
  const { rows } = await pool.query<{
    department: string;
    first_seen: string;
    last_seen: string;
    case_type: string | null;
  }>(
    `SELECT
       r.department,
       MIN(r.hearing_date)::text AS first_seen,
       MAX(r.hearing_date)::text AS last_seen,
       (
         SELECT c.case_type
         FROM cases c
         JOIN rulings r2 ON r2.case_id = c.id
         WHERE r2.judge_id = $1
           AND r2.department = r.department
           AND c.case_type IS NOT NULL
         GROUP BY c.case_type
         ORDER BY COUNT(*) DESC
         LIMIT 1
       ) AS case_type
     FROM rulings r
     WHERE r.judge_id = $1
       AND r.department IS NOT NULL
     GROUP BY r.department
     ORDER BY MAX(r.hearing_date) DESC`,
    [judgeId],
  );

  return rows.map((row) => ({
    department: row.department,
    caseType: row.case_type,
    courthouse: county ? lookupCourthouse(county, row.department) : null,
    firstSeen: row.first_seen,
    lastSeen: row.last_seen,
  }));
}

/**
 * Batch-load judge assignments for multiple judges.
 * Used by the DataLoader to prevent N+1 queries.
 */
export async function getJudgeAssignmentsBatch(
  pool: Pool,
  judgeIds: readonly string[],
  countyByJudge: Map<string, string | null>,
): Promise<JudgeAssignment[][]> {
  const { rows } = await pool.query<{
    judge_id: string;
    department: string;
    first_seen: string;
    last_seen: string;
    case_type: string | null;
  }>(
    `SELECT
       r.judge_id,
       r.department,
       MIN(r.hearing_date)::text AS first_seen,
       MAX(r.hearing_date)::text AS last_seen,
       (
         SELECT c.case_type
         FROM cases c
         JOIN rulings r2 ON r2.case_id = c.id
         WHERE r2.judge_id = r.judge_id
           AND r2.department = r.department
           AND c.case_type IS NOT NULL
         GROUP BY c.case_type
         ORDER BY COUNT(*) DESC
         LIMIT 1
       ) AS case_type
     FROM rulings r
     WHERE r.judge_id = ANY($1)
       AND r.department IS NOT NULL
     GROUP BY r.judge_id, r.department
     ORDER BY r.judge_id, MAX(r.hearing_date) DESC`,
    [judgeIds as string[]],
  );

  // Group by judge_id, preserving order within each group
  const byJudge = new Map<string, JudgeAssignment[]>();
  for (const row of rows) {
    const county = countyByJudge.get(row.judge_id) ?? null;
    const assignment: JudgeAssignment = {
      department: row.department,
      caseType: row.case_type,
      courthouse: county ? lookupCourthouse(county, row.department) : null,
      firstSeen: row.first_seen,
      lastSeen: row.last_seen,
    };

    let arr = byJudge.get(row.judge_id);
    if (!arr) {
      arr = [];
      byJudge.set(row.judge_id, arr);
    }
    arr.push(assignment);
  }

  return judgeIds.map((id) => byJudge.get(id) ?? []);
}
