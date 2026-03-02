import pool from '../data-access/pool.js';

// ---- DB row shapes -------------------------------------------------------

interface CaseRow {
  id: string;
  case_number: string;
  case_title: string | null;
  case_type: string | null;
  case_status: string | null;
  filed_at: Date | null;
}

interface JudgeRow {
  id: string;
  canonical_name: string;
  department: string | null;
  is_active: boolean;
}

interface RulingRow {
  id: string;
  case_id: string;
  judge_id: string | null;
  outcome: string | null;
  motion_type: string | null;
  hearing_date: Date;
  is_tentative: boolean;
  summary: string | null;
  ruling_text: string | null;
  posted_at: Date | null;
}

// ---- Internal resolver-to-resolver shapes --------------------------------

interface ResolvedCase {
  id: string;
  caseNumber: string;
  caseTitle: string | null;
  caseType: string | null;
  caseStatus: string | null;
  filedAt: string | null;
}

interface ResolvedJudge {
  id: string;
  canonicalName: string;
  department: string | null;
  isActive: boolean;
}

interface ResolvedRuling {
  id: string;
  outcome: string | null;
  motionType: string | null;
  hearingDate: string;
  isTentative: boolean;
  summary: string | null;
  rulingText: string | null;
  postedAt: string | null;
  // private: used by nested resolvers
  _caseId: string;
  _judgeId: string | null;
}

// ---- Row → resolver shape converters -------------------------------------

function toCase(row: CaseRow): ResolvedCase {
  return {
    id: row.id,
    caseNumber: row.case_number,
    caseTitle: row.case_title,
    caseType: row.case_type,
    caseStatus: row.case_status,
    filedAt: row.filed_at?.toISOString() ?? null,
  };
}

function toJudge(row: JudgeRow): ResolvedJudge {
  return {
    id: row.id,
    canonicalName: row.canonical_name,
    department: row.department,
    isActive: row.is_active,
  };
}

function toRuling(row: RulingRow): ResolvedRuling {
  return {
    id: row.id,
    outcome: row.outcome,
    motionType: row.motion_type,
    hearingDate: row.hearing_date.toISOString(),
    isTentative: row.is_tentative,
    summary: row.summary,
    rulingText: row.ruling_text,
    postedAt: row.posted_at?.toISOString() ?? null,
    _caseId: row.case_id,
    _judgeId: row.judge_id,
  };
}

// ---- Resolvers -----------------------------------------------------------

export const resolvers = {
  Query: {
    case: async (_: unknown, { id }: { id: string }) => {
      const { rows } = await pool.query<CaseRow>('SELECT * FROM cases WHERE id = $1', [id]);
      return rows[0] ? toCase(rows[0]) : null;
    },

    cases: async (_: unknown, { limit, offset }: { limit: number; offset: number }) => {
      const { rows } = await pool.query<CaseRow>(
        'SELECT * FROM cases ORDER BY created_at DESC LIMIT $1 OFFSET $2',
        [limit, offset],
      );
      return rows.map(toCase);
    },

    judge: async (_: unknown, { id }: { id: string }) => {
      const { rows } = await pool.query<JudgeRow>('SELECT * FROM judges WHERE id = $1', [id]);
      return rows[0] ? toJudge(rows[0]) : null;
    },

    judges: async (_: unknown, { limit, offset }: { limit: number; offset: number }) => {
      const { rows } = await pool.query<JudgeRow>(
        'SELECT * FROM judges ORDER BY canonical_name LIMIT $1 OFFSET $2',
        [limit, offset],
      );
      return rows.map(toJudge);
    },

    ruling: async (_: unknown, { id }: { id: string }) => {
      const { rows } = await pool.query<RulingRow>('SELECT * FROM rulings WHERE id = $1', [id]);
      return rows[0] ? toRuling(rows[0]) : null;
    },

    rulings: async (
      _: unknown,
      {
        judgeId,
        caseId,
        limit,
        offset,
      }: { judgeId?: string; caseId?: string; limit: number; offset: number },
    ) => {
      const params: unknown[] = [];
      let sql = 'SELECT * FROM rulings WHERE true';
      if (judgeId) {
        params.push(judgeId);
        sql += ` AND judge_id = $${params.length}`;
      }
      if (caseId) {
        params.push(caseId);
        sql += ` AND case_id = $${params.length}`;
      }
      params.push(limit);
      sql += ` ORDER BY hearing_date DESC LIMIT $${params.length}`;
      params.push(offset);
      sql += ` OFFSET $${params.length}`;
      const { rows } = await pool.query<RulingRow>(sql, params);
      return rows.map(toRuling);
    },
  },

  Case: {
    judges: async (parent: ResolvedCase) => {
      const { rows } = await pool.query<JudgeRow>(
        `SELECT j.* FROM judges j
         JOIN case_judges cj ON j.id = cj.judge_id
         WHERE cj.case_id = $1`,
        [parent.id],
      );
      return rows.map(toJudge);
    },

    rulings: async (parent: ResolvedCase) => {
      const { rows } = await pool.query<RulingRow>(
        'SELECT * FROM rulings WHERE case_id = $1 ORDER BY hearing_date DESC',
        [parent.id],
      );
      return rows.map(toRuling);
    },
  },

  Ruling: {
    case: async (parent: ResolvedRuling) => {
      const { rows } = await pool.query<CaseRow>('SELECT * FROM cases WHERE id = $1', [
        parent._caseId,
      ]);
      return rows[0] ? toCase(rows[0]) : null;
    },

    judge: async (parent: ResolvedRuling) => {
      if (!parent._judgeId) return null;
      const { rows } = await pool.query<JudgeRow>('SELECT * FROM judges WHERE id = $1', [
        parent._judgeId,
      ]);
      return rows[0] ? toJudge(rows[0]) : null;
    },
  },
};
