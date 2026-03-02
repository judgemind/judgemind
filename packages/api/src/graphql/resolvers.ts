import type { Pool } from 'pg';

interface Context {
  pool: Pool;
}

type Row = Record<string, unknown>;

export const resolvers = {
  Query: {
    health: () => 'ok',

    case: async (_: unknown, { id }: { id: string }, { pool }: Context) => {
      const { rows } = await pool.query('SELECT * FROM cases WHERE id = $1', [id]);
      return rows[0] ?? null;
    },

    cases: async (
      _: unknown,
      {
        courtId,
        caseStatus,
        first = 20,
        offset = 0,
      }: { courtId?: string; caseStatus?: string; first?: number; offset?: number },
      { pool }: Context,
    ) => {
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;
      if (courtId) {
        conditions.push(`court_id = $${i++}`);
        params.push(courtId);
      }
      if (caseStatus) {
        conditions.push(`case_status = $${i++}`);
        params.push(caseStatus);
      }
      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(first, offset);
      const { rows } = await pool.query(
        `SELECT * FROM cases ${where} ORDER BY created_at DESC LIMIT $${i++} OFFSET $${i}`,
        params,
      );
      return rows;
    },

    judge: async (_: unknown, { id }: { id: string }, { pool }: Context) => {
      const { rows } = await pool.query('SELECT * FROM judges WHERE id = $1', [id]);
      return rows[0] ?? null;
    },

    judges: async (
      _: unknown,
      { courtId, first = 20, offset = 0 }: { courtId?: string; first?: number; offset?: number },
      { pool }: Context,
    ) => {
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;
      if (courtId) {
        conditions.push(`court_id = $${i++}`);
        params.push(courtId);
      }
      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(first, offset);
      const { rows } = await pool.query(
        `SELECT * FROM judges ${where} ORDER BY canonical_name LIMIT $${i++} OFFSET $${i}`,
        params,
      );
      return rows;
    },

    ruling: async (_: unknown, { id }: { id: string }, { pool }: Context) => {
      const { rows } = await pool.query('SELECT * FROM rulings WHERE id = $1', [id]);
      return rows[0] ?? null;
    },

    rulings: async (
      _: unknown,
      {
        judgeId,
        caseId,
        outcome,
        first = 20,
        offset = 0,
      }: {
        judgeId?: string;
        caseId?: string;
        outcome?: string;
        first?: number;
        offset?: number;
      },
      { pool }: Context,
    ) => {
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;
      if (judgeId) {
        conditions.push(`judge_id = $${i++}`);
        params.push(judgeId);
      }
      if (caseId) {
        conditions.push(`case_id = $${i++}`);
        params.push(caseId);
      }
      if (outcome) {
        conditions.push(`outcome = $${i++}`);
        params.push(outcome);
      }
      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(first, offset);
      const { rows } = await pool.query(
        `SELECT * FROM rulings ${where} ORDER BY hearing_date DESC LIMIT $${i++} OFFSET $${i}`,
        params,
      );
      return rows;
    },
  },

  Case: {
    caseNumber: (row: Row) => row.case_number,
    caseTitle: (row: Row) => row.case_title,
    caseType: (row: Row) => row.case_type,
    caseStatus: (row: Row) => row.case_status,
    filedAt: (row: Row) => row.filed_at,
    court: async (row: Row, _: unknown, { pool }: Context) => {
      const { rows } = await pool.query('SELECT * FROM courts WHERE id = $1', [row.court_id]);
      return rows[0] ?? null;
    },
    judges: async (row: Row, _: unknown, { pool }: Context) => {
      const { rows } = await pool.query(
        `SELECT j.* FROM judges j
         JOIN case_judges cj ON cj.judge_id = j.id
         WHERE cj.case_id = $1`,
        [row.id],
      );
      return rows;
    },
  },

  Judge: {
    canonicalName: (row: Row) => row.canonical_name,
    isActive: (row: Row) => row.is_active,
    appointedAt: (row: Row) => row.appointed_at,
    court: async (row: Row, _: unknown, { pool }: Context) => {
      const { rows } = await pool.query('SELECT * FROM courts WHERE id = $1', [row.court_id]);
      return rows[0] ?? null;
    },
  },

  Court: {
    courtName: (row: Row) => row.court_name,
    courtCode: (row: Row) => row.court_code,
    isActive: (row: Row) => row.is_active,
  },

  Ruling: {
    hearingDate: (row: Row) => row.hearing_date,
    motionType: (row: Row) => row.motion_type,
    isTentative: (row: Row) => row.is_tentative,
    rulingText: (row: Row) => row.ruling_text,
    case: async (row: Row, _: unknown, { pool }: Context) => {
      const { rows } = await pool.query('SELECT * FROM cases WHERE id = $1', [row.case_id]);
      return rows[0] ?? null;
    },
    judge: async (row: Row, _: unknown, { pool }: Context) => {
      if (!row.judge_id) return null;
      const { rows } = await pool.query('SELECT * FROM judges WHERE id = $1', [row.judge_id]);
      return rows[0] ?? null;
    },
  },
};
